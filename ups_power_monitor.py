#!/usr/bin/env python3
"""UPS 전력 모니터 — apcaccess의 LOADPCT/NOMPOWER로 전력(W)을 수집하고
실시간/시간별/일별/월별 통계와 월 누적 전력사용량(kWh)을 제공한다.

서브커맨드:
  collect   상시 수집 + 웹 대시보드 + LCD 표시(--lcd) (systemd 서비스용)
  stats     CLI 통계 리포트
  status    apcaccess 1회 파싱해 현재 상태 출력 (DB 불필요)

표준 라이브러리만 사용. DB는 SQLite(WAL) — 외부 프로그램이 읽기 전용으로
동시 접근 가능하며 스키마는 README.md에 문서화되어 있다.
"""

import argparse
import json
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
"""


def backup_dir(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")


def open_db_rw(db_path):
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
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


def insert_sample(con, ts, loadpct, watts, dt):
    """원시 샘플 + 시간별 롤업을 한 트랜잭션으로 기록."""
    wh = watts * dt / 3600.0
    with con:  # BEGIN ... COMMIT (synchronous=FULL이라 커밋 시 fsync)
        con.execute(
            "INSERT OR REPLACE INTO samples(ts, loadpct, watts, dt) VALUES(?,?,?,?)",
            (ts, loadpct, watts, dt))
        con.execute(
            """INSERT INTO hourly(hour_ts, sum_w, min_w, max_w, wh, n)
               VALUES(?,?,?,?,?,1)
               ON CONFLICT(hour_ts) DO UPDATE SET
                 sum_w = sum_w + excluded.sum_w,
                 min_w = MIN(min_w, excluded.min_w),
                 max_w = MAX(max_w, excluded.max_w),
                 wh    = wh + excluded.wh,
                 n     = n + 1""",
            (local_hour_start(ts), watts, watts, watts, wh))


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
        "SELECT ts, loadpct, watts FROM samples ORDER BY ts DESC LIMIT 1"
    ).fetchone()


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

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
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
                con = open_db_ro(self.db_path)
                try:
                    self._send(200, query_summary(con))
                finally:
                    con.close()
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _realtime(self):
        latest = self.collector.latest() if self.collector else None
        if latest:
            return latest
        con = open_db_ro(self.db_path)
        try:
            row = query_latest_sample(con)
        finally:
            con.close()
        if not row:
            return {"ts": None}
        return {"ts": row[0], "loadpct": row[1], "watts": row[2], "status": None}

    def _series(self, fn, **kw):
        con = open_db_ro(self.db_path)
        try:
            rows = fn(con, **kw)
        finally:
            con.close()
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
        self._summary_at = 0.0

    def _get_summary(self):
        if time.time() - self._summary_at >= LCD_SUMMARY_REFRESH:
            try:
                con = open_db_ro(self.db_path)
                try:
                    self._summary = query_summary(con)
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
             f"RPi TEMP:  {get_cpu_temp()}C"),

            (f"POWER: {watts:.1f}W" if watts is not None else "POWER: --",
             f"TODAY: {s.get('today_kwh', 0.0):.2f}kWh"),

            (f"MONTH ({s.get('month', '?')})",
             f"{s.get('month_kwh', 0.0):.2f} kWh"),
        ]

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

    def _set_latest(self, ts, loadpct, watts, status, fields):
        with self._lock:
            self._latest = {
                "ts": ts, "loadpct": loadpct,
                "watts": round(watts, 1), "status": status,
            }
            self._fields = fields

    def run(self, con):
        interval = self.args.interval
        dt_cap = interval * 3
        prev_ts = None
        last_checkpoint = time.time()
        errors_in_a_row = 0

        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                loadpct, watts, status, fields = read_ups(
                    self.args.apcaccess, self.args.nominal_power)
                ts = int(time.time())
                dt = interval if prev_ts is None else min(ts - prev_ts, dt_cap)
                insert_sample(con, ts, loadpct, watts, dt)
                prev_ts = ts
                self._set_latest(ts, loadpct, watts, status, fields)
                if errors_in_a_row:
                    log(f"수집 복구됨 (연속 실패 {errors_in_a_row}회 후)")
                errors_in_a_row = 0
            except RuntimeError as e:
                errors_in_a_row += 1
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
        ts, loadpct, watts = row
        age = int(now - ts)
        stale = f"  ⚠ {age}초 전 샘플 — 수집기가 멈췄을 수 있음" if age > 120 else ""
        lines.append(
            f"■ 실시간  {datetime.fromtimestamp(ts):%Y-%m-%d %H:%M:%S}{stale}\n"
            f"  부하 {loadpct:.1f}% · 전력 {watts:.1f} W")
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

    con.close()
    print("\n\n".join(lines))


def cmd_status(args):
    loadpct, watts, status, fields = read_ups(args.apcaccess, args.nominal_power)
    nompower = leading_float(fields.get("NOMPOWER")) or args.nominal_power
    print(f"모델     : {fields.get('MODEL', '?').strip()}")
    print(f"상태     : {status}")
    print(f"입력전압 : {fields.get('LINEV', '?')}")
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

    st = sub.add_parser("status", parents=[common],
                        help="현재 상태 1회 출력 (DB 불필요)")
    st.set_defaults(fn=cmd_status)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
