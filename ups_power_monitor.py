#!/usr/bin/env python3
"""UPS 전력 모니터 — apcaccess의 LOADPCT/NOMPOWER로 전력(W)을 수집하고
실시간/시간별/일별/월별 통계와 월 누적 전력사용량(kWh)을 제공한다.
입력 전압(LINEV)도 함께 적산해 공급전원 품질 보고서를 낸다.

서브커맨드:
  collect   상시 수집 + 웹 대시보드 + LCD 표시(--lcd) (systemd 서비스용)
  stats     CLI 통계 리포트
  quality   공급전원(입력 전압) 단기/장기 보고서
  status    apcaccess 1회 파싱해 현재 상태 출력 (DB 불필요)
  tui       3.5인치 SPI LCD 등 콘솔용 TUI 대시보드 (ups_tui.py)

표준 라이브러리만 사용. DB는 SQLite(WAL) — 외부 프로그램이 읽기 전용으로
동시 접근 가능하며 스키마는 README.md에 문서화되어 있다.
"""

import argparse
import glob
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "ups_power.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
BACKUP_KEEP = 7
SAMPLE_RETENTION_DAYS = 90
APCACCESS_TIMEOUT = 10

# 공급전원 판정: 기준 전압(NOMINV) ± VOLT_TOLERANCE_PCT 를 정상 범위로 본다.
# 경계에서 이벤트가 깜빡이지 않도록 복귀는 DEADBAND만큼 안쪽으로 들어와야 인정.
VOLT_TOLERANCE_PCT = 10.0
VOLT_DEADBAND_V = 2.0


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- apcaccess

def parse_apcaccess(text):
    """apcaccess 출력을 {필드명: 값 문자열} dict로 파싱."""
    fields = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def leading_float(s):
    """'21.0 Percent' → 21.0. 숫자가 없으면 None."""
    m = re.match(r"[-+]?\d+(?:\.\d+)?", (s or "").strip())
    return float(m.group()) if m else None


def read_ups(apcaccess_cmd, nominal_power=None):
    """apcaccess를 1회 실행해 (loadpct, watts, status, fields)를 반환.

    실패(실행 불가/COMMLOST/필수 필드 누락)는 RuntimeError.
    """
    try:
        proc = subprocess.run(
            shlex.split(apcaccess_cmd),
            capture_output=True, text=True, timeout=APCACCESS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"apcaccess 실행 실패: {e}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"apcaccess 종료코드 {proc.returncode}: {proc.stderr.strip()}")

    fields = parse_apcaccess(proc.stdout)
    status = fields.get("STATUS", "")
    if "COMMLOST" in status:
        raise RuntimeError("UPS 통신 두절(COMMLOST) — 샘플 생략")

    loadpct = leading_float(fields.get("LOADPCT"))
    if loadpct is None:
        raise RuntimeError("LOADPCT 필드를 찾을 수 없음")

    nompower = leading_float(fields.get("NOMPOWER"))
    if nompower is None:
        nompower = nominal_power
    if nompower is None:
        raise RuntimeError(
            "NOMPOWER 필드가 없음 — --nominal-power 옵션으로 정격 전력(W)을 지정하세요")

    watts = nompower * loadpct / 100.0
    return loadpct, watts, status, fields


# ---------------------------------------------------------------------- DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts      INTEGER PRIMARY KEY,  -- unix epoch (초)
  loadpct REAL NOT NULL,        -- UPS 부하 (%)
  watts   REAL NOT NULL,        -- 추정 전력 (W) = NOMPOWER * LOADPCT / 100
  dt      REAL NOT NULL         -- 이 샘플이 대표하는 시간 (초)
);

CREATE TABLE IF NOT EXISTS hourly (
  hour_ts INTEGER PRIMARY KEY,  -- 해당 정시(로컬 기준)의 epoch
  sum_w   REAL NOT NULL,        -- Σ watts (평균 = sum_w / n)
  min_w   REAL NOT NULL,
  max_w   REAL NOT NULL,
  wh      REAL NOT NULL,        -- Σ (watts * dt) / 3600
  n       INTEGER NOT NULL
);

CREATE VIEW IF NOT EXISTS v_daily AS
  SELECT date(hour_ts, 'unixepoch', 'localtime') AS day,
         SUM(sum_w) / SUM(n)   AS avg_w,
         MIN(min_w)            AS min_w,
         MAX(max_w)            AS max_w,
         SUM(wh) / 1000.0      AS kwh,
         SUM(n)                AS samples
  FROM hourly GROUP BY day;

CREATE VIEW IF NOT EXISTS v_monthly AS
  SELECT strftime('%Y-%m', hour_ts, 'unixepoch', 'localtime') AS month,
         SUM(sum_w) / SUM(n)   AS avg_w,
         MIN(min_w)            AS min_w,
         MAX(max_w)            AS max_w,
         SUM(wh) / 1000.0      AS kwh,
         SUM(n)                AS samples
  FROM hourly GROUP BY month;

-- 입력 전압 시간별 롤업. 표준편차는 Σv² 로 되계산하므로(stdev 참조)
-- SQLite에 math 확장이 없어도 된다.
CREATE TABLE IF NOT EXISTS volt_hourly (
  hour_ts INTEGER PRIMARY KEY,  -- 해당 정시(로컬 기준)의 epoch
  sum_v   REAL NOT NULL,        -- Σ V
  sum_v2  REAL NOT NULL,        -- Σ V²
  min_v   REAL NOT NULL,
  max_v   REAL NOT NULL,
  n       INTEGER NOT NULL,
  n_out   INTEGER NOT NULL      -- 정상 범위를 벗어난 샘플 수
);

-- 공급전원 이벤트. 진행 중인 이벤트는 end_ts IS NULL.
CREATE TABLE IF NOT EXISTS events (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  kind   TEXT NOT NULL,         -- onbatt | comm | sag | swell | xfer
  ts     INTEGER NOT NULL,      -- 시작 시각
  end_ts INTEGER,               -- 종료 시각 (순간 이벤트는 ts와 동일)
  detail TEXT,
  linev  REAL                   -- 발생 시점 입력 전압
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- UPS가 보고한 기준값(NOMINV/LOTRANS/HITRANS 등) 보관 — CLI/API가 참조
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE VIEW IF NOT EXISTS v_volt_daily AS
  SELECT date(hour_ts, 'unixepoch', 'localtime') AS day,
         SUM(sum_v)  AS sum_v,
         SUM(sum_v2) AS sum_v2,
         MIN(min_v)  AS min_v,
         MAX(max_v)  AS max_v,
         SUM(n)      AS n,
         SUM(n_out)  AS n_out
  FROM volt_hourly GROUP BY day;

CREATE VIEW IF NOT EXISTS v_volt_monthly AS
  SELECT strftime('%Y-%m', hour_ts, 'unixepoch', 'localtime') AS month,
         SUM(sum_v)  AS sum_v,
         SUM(sum_v2) AS sum_v2,
         MIN(min_v)  AS min_v,
         MAX(max_v)  AS max_v,
         SUM(n)      AS n,
         SUM(n_out)  AS n_out
  FROM volt_hourly GROUP BY month;
"""


def backup_dir(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")


def migrate(con):
    """구버전 DB 보정. samples.linev는 전압 수집 이전 DB에 없다(과거 행은 NULL)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(samples)")}
    if "linev" not in cols:
        con.execute("ALTER TABLE samples ADD COLUMN linev REAL")
        log("DB 마이그레이션: samples.linev 컬럼 추가")


def open_db_rw(db_path):
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    migrate(con)
    con.commit()
    return con


def open_db_ro(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(
            f"DB가 없습니다: {db_path}\n먼저 collect를 실행해 수집을 시작하세요.")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def check_and_repair_db(db_path):
    """기동 시 무결성 점검. 손상 시 격리 후 최신 백업 복원(없으면 새로 생성).

    이 호스트는 UPS 셧다운 제어 주체이므로 어떤 경우에도 여기서 기동이
    막히면 안 된다 — 손상은 격리하고 반드시 진행한다.
    """
    if not os.path.exists(db_path):
        return
    con = None
    try:
        con = sqlite3.connect(db_path, timeout=10)
        result = con.execute("PRAGMA quick_check").fetchone()[0]
        if result == "ok":
            return
        reason = f"quick_check 결과: {result}"
    except sqlite3.Error as e:
        reason = f"열기 실패: {e}"
    finally:
        if con is not None:
            con.close()

    quarantine = f"{db_path}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
    os.replace(db_path, quarantine)
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass
    log(f"경고: DB 손상 감지({reason}) — {quarantine} 로 격리")

    bdir = backup_dir(db_path)
    backups = sorted(
        f for f in (os.listdir(bdir) if os.path.isdir(bdir) else [])
        if f.endswith(".db"))
    if backups:
        latest = os.path.join(bdir, backups[-1])
        shutil.copy2(latest, db_path)
        log(f"백업에서 복원: {latest}")
    else:
        log("백업 없음 — 새 DB로 시작")


def local_hour_start(ts):
    dt = datetime.fromtimestamp(ts)
    return int(dt.replace(minute=0, second=0, microsecond=0).timestamp())


def insert_sample(con, ts, loadpct, watts, dt, linev=None, out_of_range=False):
    """원시 샘플 + 시간별 롤업(전력/전압)을 한 트랜잭션으로 기록."""
    wh = watts * dt / 3600.0
    hour_ts = local_hour_start(ts)
    with con:  # BEGIN ... COMMIT (synchronous=FULL이라 커밋 시 fsync)
        con.execute(
            "INSERT OR REPLACE INTO samples(ts, loadpct, watts, dt, linev) "
            "VALUES(?,?,?,?,?)", (ts, loadpct, watts, dt, linev))
        con.execute(
            """INSERT INTO hourly(hour_ts, sum_w, min_w, max_w, wh, n)
               VALUES(?,?,?,?,?,1)
               ON CONFLICT(hour_ts) DO UPDATE SET
                 sum_w = sum_w + excluded.sum_w,
                 min_w = MIN(min_w, excluded.min_w),
                 max_w = MAX(max_w, excluded.max_w),
                 wh    = wh + excluded.wh,
                 n     = n + 1""",
            (hour_ts, watts, watts, watts, wh))
        if linev is not None:
            con.execute(
                """INSERT INTO volt_hourly(hour_ts, sum_v, sum_v2, min_v, max_v, n, n_out)
                   VALUES(?,?,?,?,?,1,?)
                   ON CONFLICT(hour_ts) DO UPDATE SET
                     sum_v  = sum_v + excluded.sum_v,
                     sum_v2 = sum_v2 + excluded.sum_v2,
                     min_v  = MIN(min_v, excluded.min_v),
                     max_v  = MAX(max_v, excluded.max_v),
                     n      = n + 1,
                     n_out  = n_out + excluded.n_out""",
                (hour_ts, linev, linev * linev, linev, linev, 1 if out_of_range else 0))


def set_meta(con, values):
    """UPS가 보고한 기준값을 meta에 반영(변경 시에만 기록)."""
    current = dict(con.execute("SELECT key, value FROM meta"))
    changed = {k: str(v) for k, v in values.items()
               if v is not None and current.get(k) != str(v)}
    if not changed:
        return
    with con:
        con.executemany("INSERT INTO meta(key, value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        list(changed.items()))


def get_meta(con):
    try:
        return dict(con.execute("SELECT key, value FROM meta"))
    except sqlite3.Error:
        return {}


# ------------------------------------------------------------ 공급전원 이벤트

EVENT_LABELS = {
    "onbatt": "배터리 구동",
    "comm": "UPS 통신 두절",
    "sag": "전압 저하",
    "swell": "전압 상승",
    "xfer": "배터리 전환",
}


def voltage_band(fields, nominal_override=None, tolerance=VOLT_TOLERANCE_PCT):
    """(기준전압, 하한, 상한). UPS의 NOMINV를 우선 쓰고, 없으면 옵션값."""
    nominal = leading_float((fields or {}).get("NOMINV")) or nominal_override
    if not nominal:
        return None, None, None
    return nominal, nominal * (1 - tolerance / 100), nominal * (1 + tolerance / 100)


class QualityTracker:
    """폴링 결과를 공급전원 이벤트로 바꿔 events에 기록한다.

    15초 폴링으로는 사이클 단위 순간 전압강하를 볼 수 없다. 대신 UPS 내부
    카운터(NUMXFERS)가 늘어나면 '샘플 사이에 일어난 전환'으로 기록해 공백을 메운다.
    """

    def __init__(self, con, args):
        self.con = con
        self.tolerance = args.voltage_tolerance
        self.nominal_override = args.voltage_nominal
        self._open = {}       # kind -> event id
        self._numxfers = None
        self._close_dangling()

    def _close_dangling(self):
        """이전 실행이 비정상 종료해 열린 채 남은 이벤트를 마지막 샘플 시점에 닫는다.
        (그대로 두면 수집 중단 시간이 전부 이벤트 지속시간으로 잡힌다)"""
        row = self.con.execute("SELECT MAX(ts) FROM samples").fetchone()
        end_ts = row[0] if row and row[0] else int(time.time())
        with self.con:
            n = self.con.execute(
                "UPDATE events SET end_ts = ?, "
                "detail = COALESCE(detail || ' ', '') || '(수집 중단으로 종료)' "
                "WHERE end_ts IS NULL", (end_ts,)).rowcount
        if n:
            log(f"이전 실행의 미종료 이벤트 {n}건을 마지막 샘플 시점으로 마감")

    def _open_event(self, kind, ts, detail=None, linev=None):
        if kind in self._open:
            return
        with self.con:
            cur = self.con.execute(
                "INSERT INTO events(kind, ts, end_ts, detail, linev) "
                "VALUES(?,?,NULL,?,?)", (kind, ts, detail, linev))
        self._open[kind] = cur.lastrowid
        log(f"공급전원 이벤트 시작: {EVENT_LABELS.get(kind, kind)}"
            + (f" — {detail}" if detail else ""))

    def _close_event(self, kind, ts):
        eid = self._open.pop(kind, None)
        if eid is None:
            return
        with self.con:
            self.con.execute("UPDATE events SET end_ts = ? WHERE id = ?", (ts, eid))
        log(f"공급전원 이벤트 종료: {EVENT_LABELS.get(kind, kind)}")

    def _mark(self, kind, ts, detail=None, linev=None):
        """순간 이벤트(지속시간 없음)."""
        with self.con:
            self.con.execute(
                "INSERT INTO events(kind, ts, end_ts, detail, linev) VALUES(?,?,?,?,?)",
                (kind, ts, ts, detail, linev))
        log(f"공급전원 이벤트: {EVENT_LABELS.get(kind, kind)}"
            + (f" — {detail}" if detail else ""))

    def on_failure(self, ts):
        self._open_event("comm", ts, "apcaccess 응답 없음")

    def on_sample(self, ts, status, linev, fields):
        """샘플 1건을 반영하고, 그 전압이 정상 범위를 벗어났는지 반환."""
        self._close_event("comm", ts)

        if "ONBATT" in (status or ""):
            self._open_event("onbatt", ts, fields.get("LASTXFER"), linev)
        else:
            self._close_event("onbatt", ts)

        # UPS 내부 전환 카운터 — 폴링 사이에 일어난 사건을 복구한다
        numxfers = leading_float(fields.get("NUMXFERS"))
        if numxfers is not None:
            if self._numxfers is not None and numxfers > self._numxfers:
                self._mark("xfer", ts, fields.get("LASTXFER"), linev)
            self._numxfers = numxfers

        nominal, low, high = voltage_band(
            fields, self.nominal_override, self.tolerance)
        set_meta(self.con, {
            "nominal_v": nominal, "low_v": low, "high_v": high,
            "tolerance_pct": self.tolerance,
            "lotrans": leading_float(fields.get("LOTRANS")),
            "hitrans": leading_float(fields.get("HITRANS")),
            "sense": fields.get("SENSE"), "model": fields.get("MODEL"),
        })

        if linev is None or low is None:
            return False

        if linev < low:
            self._open_event("sag", ts, f"{linev:.0f}V (하한 {low:.0f}V)", linev)
        elif linev >= low + VOLT_DEADBAND_V:
            self._close_event("sag", ts)

        if linev > high:
            self._open_event("swell", ts, f"{linev:.0f}V (상한 {high:.0f}V)", linev)
        elif linev <= high - VOLT_DEADBAND_V:
            self._close_event("swell", ts)

        return linev < low or linev > high


# ---------------------------------------------------------------- 통계 조회

def query_hourly(con, hours=24, now=None):
    now = now or time.time()
    since = local_hour_start(now) - (hours - 1) * 3600
    return con.execute(
        """SELECT hour_ts, sum_w / n, min_w, max_w, wh / 1000.0, n
           FROM hourly WHERE hour_ts >= ? ORDER BY hour_ts""",
        (since,)).fetchall()


def query_daily(con, days=30, now=None):
    now = now or time.time()
    since = (datetime.fromtimestamp(now) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return con.execute(
        "SELECT day, avg_w, min_w, max_w, kwh, samples FROM v_daily "
        "WHERE day >= ? ORDER BY day", (since,)).fetchall()


def query_monthly(con, months=12):
    return con.execute(
        "SELECT month, avg_w, min_w, max_w, kwh, samples FROM v_monthly "
        "ORDER BY month DESC LIMIT ?", (months,)).fetchall()[::-1]


def query_latest_sample(con):
    return con.execute(
        "SELECT ts, loadpct, watts, linev FROM samples ORDER BY ts DESC LIMIT 1"
    ).fetchone()


def stdev(n, sum_v, sum_v2):
    """Σv, Σv² 에서 표준편차 복원(모표준편차). 부동소수 오차로 음수가 될 수 있어 clamp."""
    if not n:
        return 0.0
    var = sum_v2 / n - (sum_v / n) ** 2
    return math.sqrt(var) if var > 0 else 0.0


def _volt_row(label, sum_v, sum_v2, min_v, max_v, n, n_out):
    return {
        "t": label,
        "avg_v": sum_v / n,
        "min_v": min_v,
        "max_v": max_v,
        "sd_v": stdev(n, sum_v, sum_v2),
        "n": n,
        "n_out": n_out,
        "out_pct": 100.0 * n_out / n,
    }


def query_voltage_hourly(con, hours=24, now=None):
    now = now or time.time()
    since = local_hour_start(now) - (hours - 1) * 3600
    return [_volt_row(*r) for r in con.execute(
        "SELECT hour_ts, sum_v, sum_v2, min_v, max_v, n, n_out FROM volt_hourly "
        "WHERE hour_ts >= ? ORDER BY hour_ts", (since,))]


def query_voltage_daily(con, days=30, now=None):
    now = now or time.time()
    since = (datetime.fromtimestamp(now) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return [_volt_row(*r) for r in con.execute(
        "SELECT day, sum_v, sum_v2, min_v, max_v, n, n_out FROM v_volt_daily "
        "WHERE day >= ? ORDER BY day", (since,))]


def query_voltage_monthly(con, months=12):
    rows = con.execute(
        "SELECT month, sum_v, sum_v2, min_v, max_v, n, n_out FROM v_volt_monthly "
        "ORDER BY month DESC LIMIT ?", (months,)).fetchall()
    return [_volt_row(*r) for r in rows[::-1]]


def query_events(con, days=30, limit=300, now=None):
    """최근 N일 이벤트(진행 중인 것 포함), 최신순."""
    now = now or time.time()
    since = int(now) - days * 86400
    rows = con.execute(
        "SELECT kind, ts, end_ts, detail, linev FROM events "
        "WHERE ts >= ? OR end_ts IS NULL ORDER BY ts DESC LIMIT ?",
        (since, limit)).fetchall()
    out = []
    for kind, ts, end_ts, detail, linev in rows:
        out.append({
            "kind": kind, "label": EVENT_LABELS.get(kind, kind),
            "ts": ts, "end_ts": end_ts, "detail": detail, "linev": linev,
            "ongoing": end_ts is None,
            "duration": (end_ts if end_ts is not None else int(now)) - ts,
        })
    return out


def query_voltage_summary(con, hours=24, now=None):
    """최근 N시간 전압 요약 + 같은 구간의 이벤트 건수 + UPS가 보고한 기준값."""
    now = now or time.time()
    since = local_hour_start(now) - (hours - 1) * 3600
    row = con.execute(
        "SELECT SUM(sum_v), SUM(sum_v2), MIN(min_v), MAX(max_v), SUM(n), SUM(n_out) "
        "FROM volt_hourly WHERE hour_ts >= ?", (since,)).fetchone()

    meta = get_meta(con)
    out = {
        "hours": hours,
        "nominal_v": float(meta["nominal_v"]) if meta.get("nominal_v") else None,
        "low_v": float(meta["low_v"]) if meta.get("low_v") else None,
        "high_v": float(meta["high_v"]) if meta.get("high_v") else None,
        "tolerance_pct": float(meta.get("tolerance_pct", VOLT_TOLERANCE_PCT)),
        "lotrans": float(meta["lotrans"]) if meta.get("lotrans") else None,
        "hitrans": float(meta["hitrans"]) if meta.get("hitrans") else None,
        "sense": meta.get("sense"),
        "events": {}, "onbatt_sec": 0,
        "interval": None,
    }
    last = con.execute("SELECT dt FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    if last:
        out["interval"] = last[0]
    if row and row[4]:
        out.update(_volt_row("window", *row))
    else:
        out.update({"avg_v": None, "min_v": None, "max_v": None,
                    "sd_v": None, "n": 0, "n_out": 0, "out_pct": 0.0})

    for kind, cnt in con.execute(
            "SELECT kind, COUNT(*) FROM events WHERE ts >= ? OR end_ts IS NULL "
            "GROUP BY kind", (since,)):
        out["events"][kind] = cnt

    # 배터리 구동 누적 시간 — 창 이전에 시작된 이벤트는 창 시작으로 잘라 계산
    for ts, end_ts in con.execute(
            "SELECT ts, end_ts FROM events WHERE kind = 'onbatt' "
            "AND (end_ts IS NULL OR end_ts >= ?)", (since,)):
        out["onbatt_sec"] += max(
            0, (end_ts if end_ts is not None else int(now)) - max(ts, since))
    return out


def query_summary(con, now=None):
    """오늘/이번 달 누적 kWh."""
    now = now or time.time()
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    month = today[:7]
    row_d = con.execute("SELECT kwh FROM v_daily WHERE day = ?", (today,)).fetchone()
    row_m = con.execute("SELECT kwh FROM v_monthly WHERE month = ?", (month,)).fetchone()
    return {
        "today_kwh": row_d[0] if row_d else 0.0,
        "month_kwh": row_m[0] if row_m else 0.0,
        "month": month,
    }


# ------------------------------------------------------------------ 웹서버

class Api(BaseHTTPRequestHandler):
    """수집 프로세스 내장 웹서버. DB는 요청마다 읽기 전용으로 연다."""

    collector = None  # collect()에서 주입
    db_path = None

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # 경로 → (파일명, Content-Type). 화이트리스트라 경로 조작이 불가능하다.
    STATIC_ROUTES = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/power-quality": ("power-quality.html", "text/html; charset=utf-8"),
        "/power-quality.html": ("power-quality.html", "text/html; charset=utf-8"),
        "/common.css": ("common.css", "text/css; charset=utf-8"),
        "/common.js": ("common.js", "text/javascript; charset=utf-8"),
    }

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path in self.STATIC_ROUTES:
                name, ctype = self.STATIC_ROUTES[url.path]
                with open(os.path.join(STATIC_DIR, name), "rb") as f:
                    self._send(200, f.read(), ctype)
            elif url.path == "/api/realtime":
                self._send(200, self._realtime())
            elif url.path == "/api/hourly":
                hours = min(int(q.get("hours", ["24"])[0]), 24 * 14)
                self._send(200, self._series(query_hourly, hours=hours))
            elif url.path == "/api/daily":
                days = min(int(q.get("days", ["30"])[0]), 366)
                self._send(200, self._series(query_daily, days=days))
            elif url.path == "/api/monthly":
                months = min(int(q.get("months", ["12"])[0]), 60)
                self._send(200, self._series(query_monthly, months=months))
            elif url.path == "/api/summary":
                self._send(200, self._with_db(query_summary))
            elif url.path == "/api/voltage/hourly":
                hours = min(int(q.get("hours", ["24"])[0]), 24 * 14)
                self._send(200, self._with_db(query_voltage_hourly, hours=hours))
            elif url.path == "/api/voltage/daily":
                days = min(int(q.get("days", ["30"])[0]), 366)
                self._send(200, self._with_db(query_voltage_daily, days=days))
            elif url.path == "/api/voltage/monthly":
                months = min(int(q.get("months", ["12"])[0]), 60)
                self._send(200, self._with_db(query_voltage_monthly, months=months))
            elif url.path == "/api/voltage/summary":
                hours = min(int(q.get("hours", ["24"])[0]), 24 * 366)
                self._send(200, self._with_db(query_voltage_summary, hours=hours))
            elif url.path == "/api/events":
                days = min(int(q.get("days", ["30"])[0]), 366)
                self._send(200, self._with_db(query_events, days=days))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _realtime(self):
        latest = self.collector.latest() if self.collector else None
        if latest:
            return latest
        row = self._with_db(query_latest_sample)
        if not row:
            return {"ts": None}
        return {"ts": row[0], "loadpct": row[1], "watts": row[2],
                "linev": row[3], "status": None}

    def _with_db(self, fn, **kw):
        con = open_db_ro(self.db_path)
        try:
            return fn(con, **kw)
        finally:
            con.close()

    def _series(self, fn, **kw):
        rows = self._with_db(fn, **kw)
        keys = ("t", "avg_w", "min_w", "max_w", "kwh", "n")
        return [dict(zip(keys, r)) for r in rows]

    def log_message(self, fmt, *args):  # 요청 로그는 journal 소음이라 생략
        pass


# -------------------------------------------------------- LCD (16x2 I2C)

LCD_CYCLE_SEC = 3           # 화면 전환 주기
LCD_SUMMARY_REFRESH = 60    # 오늘/이번 달 kWh 재조회 주기


def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "No IP"


def get_cpu_temp():
    """thermal_zone0이 보드마다 CPU가 아니다(OPi Zero 3는 gpu-thermal).
    type으로 cpu-thermal 존을 찾고, 못 찾으면 zone0으로 폴백."""
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        try:
            with open(os.path.join(zone, "type")) as f:
                if f.read().strip() != "cpu-thermal":
                    continue
            with open(os.path.join(zone, "temp")) as f:
                return round(int(f.read()) / 1000, 1)
        except (OSError, ValueError):
            continue
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read()) / 1000, 1)
    except (OSError, ValueError):
        return 0.0


def lcd_fmt(s, width=16):
    return s[:width].ljust(width)


def make_lcd(args):
    """RPLCD가 없거나 초기화에 실패해도 수집은 계속되어야 하므로 None 반환."""
    try:
        from RPLCD.i2c import CharLCD
    except ImportError:
        log("경고: RPLCD 라이브러리가 없어 LCD를 비활성화합니다 — pip3 install RPLCD")
        return None
    try:
        return CharLCD(i2c_expander=args.lcd_expander, address=args.lcd_address,
                       port=args.lcd_port, cols=16, rows=2)
    except Exception as e:
        log(f"경고: LCD 초기화 실패({e}) — LCD 없이 계속합니다")
        return None


class LcdDisplay:
    """수집기의 최신 UPS 데이터를 공유해 표시 — apcaccess를 중복 호출하지 않음."""

    def __init__(self, collector, db_path, lcd):
        self.collector = collector
        self.db_path = db_path
        self.lcd = lcd
        self._summary = {}
        self._volt = {}
        self._summary_at = 0.0

    def _get_summary(self):
        if time.time() - self._summary_at >= LCD_SUMMARY_REFRESH:
            try:
                con = open_db_ro(self.db_path)
                try:
                    self._summary = query_summary(con)
                    self._volt = query_voltage_summary(con, hours=24)
                finally:
                    con.close()
            except Exception:
                pass  # DB가 아직 없거나 잠금 — 이전 값 유지
            self._summary_at = time.time()
        return self._summary

    def _screens(self):
        d = self.collector.latest_fields() or {}
        latest = self.collector.latest() or {}
        s = self._get_summary()
        watts = latest.get("watts")
        return [
            (f"STATUS: {d.get('STATUS', 'N/A')}",
             f"LOAD:   {d.get('LOADPCT', '?').split()[0]}%"),

            (f"BATTERY: {d.get('BCHARGE', '?').split()[0]}%",
             f"LEFT:   {d.get('TIMELEFT', '?').split()[0]}min"),

            (f"AC:    {d.get('LINEV', '?').split()[0]}V",
             f"BATT:   {d.get('BATTV', '?').split()[0]}V"),

            ("IP:", get_ip()),

            (datetime.now().strftime("%Y/%m/%d %H:%M"),
             f"CPU TEMP:  {get_cpu_temp()}C"),

            (f"POWER: {watts:.1f}W" if watts is not None else "POWER: --",
             f"TODAY: {s.get('today_kwh', 0.0):.2f}kWh"),

            (f"MONTH ({s.get('month', '?')})",
             f"{s.get('month_kwh', 0.0):.2f} kWh"),

            ("AC 24H MIN/MAX", self._volt_line()),
        ]

    def _volt_line(self):
        v = self._volt
        if not v or v.get("min_v") is None:
            return "(수집 중...)"
        return f"{v['min_v']:.0f}-{v['max_v']:.0f}V av{v['avg_v']:.0f}"

    def run(self, stop_event):
        screen = 0
        try:
            while not stop_event.is_set():
                try:
                    screens = self._screens()
                    line1, line2 = screens[screen % len(screens)]
                    self.lcd.home()
                    self.lcd.write_string(lcd_fmt(line1))
                    self.lcd.crlf()
                    self.lcd.write_string(lcd_fmt(line2))
                except Exception as e:
                    log(f"LCD 표시 오류: {e}")
                    stop_event.wait(10)  # I2C 일시 오류 대비
                screen += 1
                stop_event.wait(LCD_CYCLE_SEC)
        finally:
            try:
                self.lcd.close(clear=True)
            except Exception:
                pass


# ---------------------------------------------------------------- collect

class Collector:
    def __init__(self, args):
        self.args = args
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest = None
        self._fields = None

    def latest(self):
        with self._lock:
            return dict(self._latest) if self._latest else None

    def latest_fields(self):
        with self._lock:
            return dict(self._fields) if self._fields else None

    def _set_latest(self, ts, loadpct, watts, linev, status, fields):
        with self._lock:
            self._latest = {
                "ts": ts, "loadpct": loadpct, "watts": round(watts, 1),
                "linev": linev, "status": status,
            }
            self._fields = fields

    def run(self, con):
        interval = self.args.interval
        dt_cap = interval * 3
        prev_ts = None
        last_checkpoint = time.time()
        errors_in_a_row = 0
        quality = QualityTracker(con, self.args)

        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                loadpct, watts, status, fields = read_ups(
                    self.args.apcaccess, self.args.nominal_power)
                ts = int(time.time())
                dt = interval if prev_ts is None else min(ts - prev_ts, dt_cap)
                linev = leading_float(fields.get("LINEV"))
                out_of_range = quality.on_sample(ts, status, linev, fields)
                insert_sample(con, ts, loadpct, watts, dt, linev, out_of_range)
                prev_ts = ts
                self._set_latest(ts, loadpct, watts, linev, status, fields)
                if errors_in_a_row:
                    log(f"수집 복구됨 (연속 실패 {errors_in_a_row}회 후)")
                errors_in_a_row = 0
            except RuntimeError as e:
                errors_in_a_row += 1
                quality.on_failure(int(time.time()))
                if errors_in_a_row <= 3 or errors_in_a_row % 20 == 0:
                    log(f"수집 실패({errors_in_a_row}회째): {e}")

            self._housekeeping(con)
            if time.time() - last_checkpoint >= 3600:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                last_checkpoint = time.time()

            self.stop_event.wait(max(0.5, interval - (time.time() - t0)))

    def _housekeeping(self, con):
        """일 1회: 원시 샘플 정리 + 백업. 오늘자 백업 파일 존재 여부로 판단."""
        bdir = backup_dir(self.args.db)
        today_file = os.path.join(bdir, f"ups_power-{datetime.now():%Y%m%d}.db")
        if os.path.exists(today_file):
            return
        os.makedirs(bdir, exist_ok=True)

        cutoff = int(time.time()) - SAMPLE_RETENTION_DAYS * 86400
        with con:
            deleted = con.execute(
                "DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
        if deleted:
            log(f"원시 샘플 정리: {deleted}행 삭제 (보존 {SAMPLE_RETENTION_DAYS}일)")

        dest = sqlite3.connect(today_file)
        try:
            con.backup(dest)
        finally:
            dest.close()
        log(f"일일 백업 생성: {today_file}")

        backups = sorted(f for f in os.listdir(bdir)
                         if f.startswith("ups_power-") and f.endswith(".db"))
        for old in backups[:-BACKUP_KEEP]:
            os.remove(os.path.join(bdir, old))


def cmd_collect(args):
    check_and_repair_db(args.db)
    con = open_db_rw(args.db)

    # 시작 전에 apcaccess 동작을 1회 확인해 설정 오류를 빨리 드러낸다
    try:
        loadpct, watts, status, fields = read_ups(args.apcaccess, args.nominal_power)
        log(f"UPS 연결 확인: {fields.get('MODEL', '?').strip()} / "
            f"{status} / 부하 {loadpct:.1f}% / {watts:.1f} W")
    except RuntimeError as e:
        log(f"경고: 최초 apcaccess 확인 실패 — 계속 재시도합니다: {e}")

    collector = Collector(args)
    Api.collector = collector
    Api.db_path = args.db
    httpd = ThreadingHTTPServer((args.bind, args.port), Api)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"웹 대시보드: http://{args.bind or '0.0.0.0'}:{args.port}/  "
        f"(수집 주기 {args.interval}초, DB {args.db})")

    lcd_thread = None
    if args.lcd:
        lcd = make_lcd(args)
        if lcd:
            display = LcdDisplay(collector, args.db, lcd)
            lcd_thread = threading.Thread(
                target=display.run, args=(collector.stop_event,), daemon=True)
            lcd_thread.start()
            log(f"LCD 표시 시작 (I2C 0x{args.lcd_address:02X}, "
                f"{LCD_CYCLE_SEC}초 순환)")

    def on_signal(signum, frame):
        log(f"종료 신호 수신(signal {signum}) — 정리 후 종료")
        collector.stop_event.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        collector.run(con)
    finally:
        if lcd_thread:
            lcd_thread.join(5)  # LCD 클리어까지 대기 (TimeoutStopSec 이내)
        httpd.shutdown()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        log("정상 종료 완료 (WAL checkpoint 반영)")


# ------------------------------------------------------------------ stats

def _fmt_table(title, headers, rows, widths):
    out = [f"■ {title}"]
    out.append("  " + "".join(h.rjust(w) for h, w in zip(headers, widths)))
    for row in rows:
        out.append("  " + "".join(str(c).rjust(w) for c, w in zip(row, widths)))
    if not rows:
        out.append("  (데이터 없음)")
    return "\n".join(out)


def cmd_stats(args):
    con = open_db_ro(args.db)
    now = time.time()
    lines = []

    row = query_latest_sample(con)
    if row:
        ts, loadpct, watts, linev = row
        age = int(now - ts)
        stale = f"  ⚠ {age}초 전 샘플 — 수집기가 멈췄을 수 있음" if age > 120 else ""
        volt = f" · 입력 {linev:.0f} V" if linev is not None else ""
        lines.append(
            f"■ 실시간  {datetime.fromtimestamp(ts):%Y-%m-%d %H:%M:%S}{stale}\n"
            f"  부하 {loadpct:.1f}% · 전력 {watts:.1f} W{volt}")
    else:
        lines.append("■ 실시간\n  (아직 샘플 없음)")

    def fmt(rows, label_fn):
        return [(label_fn(r[0]), f"{r[1]:.1f}", f"{r[2]:.1f}", f"{r[3]:.1f}",
                 f"{r[4]:.3f}") for r in rows]

    hourly = query_hourly(con, hours=24, now=now)
    lines.append(_fmt_table(
        "시간별 (최근 24시간)", ("시간", "평균W", "최소W", "최대W", "kWh"),
        fmt(hourly, lambda t: f"{datetime.fromtimestamp(t):%m-%d %H시}"),
        (12, 9, 9, 9, 9)))

    daily = query_daily(con, days=30, now=now)
    lines.append(_fmt_table(
        "일별 (최근 30일)", ("날짜", "평균W", "최소W", "최대W", "kWh"),
        fmt(daily, str), (12, 9, 9, 9, 9)))

    monthly = query_monthly(con, months=12)
    this_month = datetime.fromtimestamp(now).strftime("%Y-%m")
    rows = []
    for r in monthly:
        mark = " ← 진행 중" if r[0] == this_month else ""
        rows.append((r[0], f"{r[1]:.1f}", f"{r[2]:.1f}", f"{r[3]:.1f}",
                     f"{r[4]:.3f}{mark}"))
    lines.append(_fmt_table(
        "월별 (최근 12개월)", ("월", "평균W", "최소W", "최대W", "누적kWh"),
        rows, (9, 9, 9, 9, 18)))

    summary = query_summary(con, now=now)
    lines.append(
        f"■ 누적 전력사용량\n"
        f"  오늘: {summary['today_kwh']:.3f} kWh · "
        f"이번 달({summary['month']}): {summary['month_kwh']:.3f} kWh")

    vs = query_voltage_summary(con, hours=24, now=now)
    if vs["n"]:
        lines.append(
            f"■ 공급전원 (최근 24시간)\n"
            f"  {vs['min_v']:.0f} ~ {vs['max_v']:.0f} V · 평균 {vs['avg_v']:.1f} V "
            f"(σ {vs['sd_v']:.2f}) · 범위 이탈 {vs['out_pct']:.2f}%\n"
            f"  자세히: ups_power_monitor.py quality")

    con.close()
    print("\n\n".join(lines))


# ---------------------------------------------------------------- quality

def _fmt_duration(sec):
    if sec < 60:
        return f"{sec}초"
    if sec < 3600:
        return f"{sec // 60}분 {sec % 60}초"
    return f"{sec // 3600}시간 {sec % 3600 // 60}분"


def cmd_quality(args):
    """공급전원 보고서 — 단기(시간별)/장기(일·월별) 입력 전압 변동 + 이벤트."""
    con = open_db_ro(args.db)
    now = time.time()
    out = []

    vs = query_voltage_summary(con, hours=args.hours, now=now)
    if not vs["n"]:
        con.close()
        raise SystemExit(
            "전압 데이터가 없습니다.\n"
            "이 기능은 수집기가 LINEV를 저장하기 시작한 이후 구간만 집계합니다 — "
            "collect를 최신 버전으로 재시작한 뒤 다시 확인하세요.")

    band = ""
    if vs["nominal_v"]:
        band = (f"기준 {vs['nominal_v']:.0f}V ±{vs['tolerance_pct']:.0f}% "
                f"({vs['low_v']:.0f}~{vs['high_v']:.0f}V)")
        if vs["lotrans"] and vs["hitrans"]:
            band += (f" · UPS 전환 임계 {vs['lotrans']:.0f}/{vs['hitrans']:.0f}V"
                     f" (감도 {vs['sense'] or '?'})")
    ev = vs["events"]
    out.append(
        f"■ 요약 (최근 {vs['hours']}시간, 샘플 {vs['n']}개)\n"
        f"  {band}\n"
        f"  전압      {vs['min_v']:.0f} ~ {vs['max_v']:.0f} V · "
        f"평균 {vs['avg_v']:.1f} V · 표준편차 {vs['sd_v']:.2f} V\n"
        f"  범위 이탈 {vs['n_out']}샘플 ({vs['out_pct']:.2f}%)\n"
        f"  이벤트    저하 {ev.get('sag', 0)} · 상승 {ev.get('swell', 0)} · "
        f"배터리 전환 {ev.get('xfer', 0)} · 배터리 구동 {ev.get('onbatt', 0)}"
        f"({_fmt_duration(vs['onbatt_sec'])}) · 통신두절 {ev.get('comm', 0)}")

    def volt_table(title, rows, label_fn):
        return _fmt_table(
            title, ("구간", "평균V", "최소V", "최대V", "σ", "이탈%"),
            [(label_fn(r["t"]), f"{r['avg_v']:.1f}", f"{r['min_v']:.0f}",
              f"{r['max_v']:.0f}", f"{r['sd_v']:.2f}", f"{r['out_pct']:.2f}")
             for r in rows],
            (12, 9, 9, 9, 8, 8))

    out.append(volt_table(
        f"단기 — 시간별 (최근 {args.hours}시간)",
        query_voltage_hourly(con, hours=args.hours, now=now),
        lambda t: f"{datetime.fromtimestamp(t):%m-%d %H시}"))

    out.append(volt_table(
        f"장기 — 일별 (최근 {args.days}일)",
        query_voltage_daily(con, days=args.days, now=now), str))

    out.append(volt_table(
        "장기 — 월별 (최근 12개월)",
        query_voltage_monthly(con, months=12), str))

    events = query_events(con, days=args.days, now=now)
    rows = []
    for e in events[:args.max_events]:
        when = f"{datetime.fromtimestamp(e['ts']):%m-%d %H:%M:%S}"
        dur = "진행 중" if e["ongoing"] else (
            "-" if e["duration"] == 0 else _fmt_duration(e["duration"]))
        rows.append((when, e["label"], dur, (e["detail"] or "")[:34]))
    out.append(_fmt_table(
        f"이벤트 (최근 {args.days}일, 최신순 {len(rows)}건)",
        ("시각", "종류", "지속", "상세"), rows, (15, 14, 12, 36)))

    interval = f"{vs['interval']:.0f}" if (vs["interval"] or 0) >= 1 else "수집"
    out.append(
        "■ 측정 한계\n"
        f"  {interval}초 주기 폴링이라 사이클 단위의 순간 전압강하·스파이크는\n"
        "  직접 관측되지 않는다. 그런 사건은 UPS 내부 카운터(NUMXFERS)가 증가할 때\n"
        "  '배터리 전환' 이벤트로만 기록된다 — 지속시간은 알 수 없다.")

    con.close()
    print("\n\n".join(out))


def cmd_status(args):
    loadpct, watts, status, fields = read_ups(args.apcaccess, args.nominal_power)
    nompower = leading_float(fields.get("NOMPOWER")) or args.nominal_power
    nominal, low, high = voltage_band(
        fields, args.voltage_nominal, args.voltage_tolerance)
    band = (f" (정상 {low:.0f}~{high:.0f}V, 기준 {nominal:.0f}V)" if nominal else "")
    print(f"모델     : {fields.get('MODEL', '?').strip()}")
    print(f"상태     : {status}")
    print(f"입력전압 : {fields.get('LINEV', '?')}{band}")
    print(f"배터리   : {fields.get('BCHARGE', '?')} (남은시간 {fields.get('TIMELEFT', '?')})")
    print(f"정격전력 : {nompower:.0f} W")
    print(f"부하     : {loadpct:.1f} %")
    print(f"추정전력 : {watts:.1f} W")


# ------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DEFAULT_DB,
                        help=f"SQLite 경로 (기본 {DEFAULT_DB})")
    common.add_argument("--apcaccess", default="apcaccess",
                        help="apcaccess 명령 (기본 'apcaccess')")
    common.add_argument("--nominal-power", type=float, default=None,
                        help="NOMPOWER 미보고 기종용 정격 전력(W)")
    common.add_argument("--voltage-nominal", type=float, default=None,
                        help="NOMINV 미보고 기종용 기준 입력 전압(V)")
    common.add_argument("--voltage-tolerance", type=float,
                        default=VOLT_TOLERANCE_PCT,
                        help=f"정상 범위 허용 오차(%%), 기본 ±{VOLT_TOLERANCE_PCT:g}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", parents=[common], help="상시 수집 + 웹 대시보드")
    c.add_argument("--interval", type=float, default=15, help="수집 주기(초), 기본 15")
    c.add_argument("--port", type=int, default=8080, help="대시보드 포트, 기본 8080")
    c.add_argument("--bind", default="", help="바인드 주소, 기본 모든 인터페이스")
    c.add_argument("--lcd", action="store_true",
                   help="16x2 I2C 문자 LCD 상태 표시 활성화 (RPLCD 필요)")
    c.add_argument("--lcd-address", type=lambda x: int(x, 0), default=0x20,
                   help="LCD I2C 주소 (기본 0x20)")
    c.add_argument("--lcd-port", type=int, default=1, help="I2C 포트 (기본 1)")
    c.add_argument("--lcd-expander", default="PCF8574",
                   help="I2C 익스팬더 칩 (기본 PCF8574)")
    c.set_defaults(fn=cmd_collect)

    s = sub.add_parser("stats", parents=[common], help="CLI 통계 리포트")
    s.set_defaults(fn=cmd_stats)

    qa = sub.add_parser("quality", parents=[common],
                        help="공급전원(입력 전압) 단기/장기 보고서")
    qa.add_argument("--hours", type=int, default=24, help="단기 구간(시간), 기본 24")
    qa.add_argument("--days", type=int, default=30, help="장기 구간(일), 기본 30")
    qa.add_argument("--max-events", type=int, default=30,
                    help="표시할 이벤트 건수, 기본 30")
    qa.set_defaults(fn=cmd_quality)

    st = sub.add_parser("status", parents=[common],
                        help="현재 상태 1회 출력 (DB 불필요)")
    st.set_defaults(fn=cmd_status)

    t = sub.add_parser("tui", parents=[common],
                       help="콘솔 TUI 대시보드 (3.5인치 SPI LCD용, ups_tui.py)")
    t.add_argument("--cycle", type=float, default=0,
                   help="페이지 자동 순환 주기(초), 0=끔 (기본)")
    t.add_argument("--ups-interval", type=float, default=15,
                   help="apcaccess 조회 주기(초), 기본 15")
    t.add_argument("--no-apcaccess", action="store_true",
                   help="apcaccess를 호출하지 않고 DB 값만 표시")
    t.add_argument("--touch", default="auto",
                   help="터치 입력 장치: auto(기본) | none | /dev/input/eventN")
    t.add_argument("--ascii", action="store_true",
                   help="블록 문자(█▄░) 대신 ASCII 사용 (콘솔 폰트가 깨질 때)")
    t.add_argument("--graph", default="auto",
                   choices=("auto", "block", "half", "shade", "ascii"),
                   help="막대 한 칸의 세로 해상도 — block=1/8블록(8단계, 해당 "
                        "글리프를 가진 폰트 필요), half=반칸(2단계), "
                        "shade=음영 ░▒█(3단계), auto=콘솔이면 shade")
    t.add_argument("--mono", action="store_true", help="색상 끔")
    t.add_argument("--no-blank-off", action="store_true",
                   help="리눅스 콘솔 화면 꺼짐(blank) 해제를 하지 않음")
    t.set_defaults(fn=lambda a: __import__("ups_tui").cmd_tui(a))

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
