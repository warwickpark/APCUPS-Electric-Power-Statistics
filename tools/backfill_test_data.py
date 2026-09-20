#!/usr/bin/env python3
"""개발용 합성 데이터 주입 — 통계/대시보드 검증용.

- 과거 N개월(기본 14)의 hourly/volt_hourly 롤업을 시간대별 사인 패턴으로 채움
- 어제(로컬 자정~자정)는 정확히 136.5W 고정 → 일별 kWh가 3.276이어야 함 (검산 기준)
- 최근 48시간은 원시 samples도 생성 (15초 간격)
- 공급전원 보고서용으로 전압 변동과 과거 이벤트(저하/전환)도 몇 건 심는다

사용: python tools/backfill_test_data.py [--db ups_power.db] [--months 14]
"""

import argparse
import math
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ups_power_monitor import DEFAULT_DB, SCHEMA, local_hour_start, migrate

CONST_W = 136.5  # 검산용 고정 와트 (650W * 21%)
NOMINAL_V = 230.0
LOW_V, HIGH_V = NOMINAL_V * 0.9, NOMINAL_V * 1.1


def pattern_watts(dt):
    """시간대 사인 패턴 + 월별 완만한 변동. 90~185W 범위."""
    hour_factor = math.sin((dt.hour - 6) / 24 * 2 * math.pi)  # 저녁 피크
    month_factor = math.cos(dt.month / 12 * 2 * math.pi)
    return 135 + 40 * hour_factor + 10 * month_factor


def pattern_volts(dt):
    """부하가 몰리는 저녁에 전압이 조금 내려가는 실제 패턴을 흉내."""
    return 218.0 - 4.0 * math.sin((dt.hour - 6) / 24 * 2 * math.pi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--months", type=int, default=14)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)
    migrate(con)

    now = datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday0 = today0 - timedelta(days=1)
    start = (today0 - timedelta(days=args.months * 31)).replace(day=1)

    # ── hourly 롤업 채우기 (현재 시각 직전 정시까지)
    n_hours = 0
    cur = start
    end = now.replace(minute=0, second=0, microsecond=0)
    with con:
        con.execute("DELETE FROM hourly")
        con.execute("DELETE FROM samples")
        con.execute("DELETE FROM volt_hourly")
        con.execute("DELETE FROM events")
        while cur < end:
            if yesterday0 <= cur < today0:
                w = CONST_W  # 검산 구간: 어제 하루 고정
            else:
                w = pattern_watts(cur)
            n = 240  # 15초 간격 1시간치
            con.execute(
                "INSERT INTO hourly(hour_ts, sum_w, min_w, max_w, wh, n) "
                "VALUES(?,?,?,?,?,?)",
                (int(cur.timestamp()), w * n, w, w, w * 1.0, n))  # wh = W*1h

            # 전압: 시간대 평균 ± 잡음, 드물게 범위를 벗어나는 시간대
            base = pattern_volts(cur)
            spread = random.uniform(1.5, 4.0)
            lo_v, hi_v = base - spread, base + spread
            n_out = 0
            if random.random() < 0.01:          # 100시간에 한 번쯤 전압 저하
                lo_v = random.uniform(195.0, 205.0)
                n_out = random.randint(1, 12)
            con.execute(
                "INSERT INTO volt_hourly(hour_ts, sum_v, sum_v2, min_v, max_v, n, n_out) "
                "VALUES(?,?,?,?,?,?,?)",
                (int(cur.timestamp()), base * n, (base * base + spread * spread / 3) * n,
                 lo_v, hi_v, n, n_out))
            if n_out:
                ts = int(cur.timestamp()) + random.randint(0, 3599)
                con.execute(
                    "INSERT INTO events(kind, ts, end_ts, detail, linev) VALUES(?,?,?,?,?)",
                    ("sag", ts, ts + n_out * 15, f"{lo_v:.0f}V (하한 {LOW_V:.0f}V)", lo_v))
            cur += timedelta(hours=1)
            n_hours += 1

        con.executemany(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [("nominal_v", str(NOMINAL_V)), ("low_v", str(LOW_V)),
             ("high_v", str(HIGH_V)), ("tolerance_pct", "10.0"),
             ("lotrans", "150.0"), ("hitrans", "290.0"),
             ("sense", "Medium"), ("model", "Back-UPS BX1200MI")])

    # ── 최근 48시간 원시 샘플 (15초 간격) + 현재 진행 중인 시간의 hourly 롤업
    t = int((now - timedelta(hours=48)).timestamp())
    t_now = int(now.timestamp())
    n_samples = 0
    with con:
        while t <= t_now:
            dt_obj = datetime.fromtimestamp(t)
            if yesterday0 <= dt_obj < today0:
                w = CONST_W
            else:
                w = pattern_watts(dt_obj)
            v = round(random.gauss(pattern_volts(dt_obj), 1.5), 1)
            con.execute(
                "INSERT OR REPLACE INTO samples(ts, loadpct, watts, dt, linev) "
                "VALUES(?,?,?,?,?)", (t, w / 6.5, w, 15.0, v))
            hour_ts = local_hour_start(t)
            if hour_ts >= int(end.timestamp()):  # 진행 중인 시간대만 롤업 추가
                con.execute(
                    """INSERT INTO hourly(hour_ts, sum_w, min_w, max_w, wh, n)
                       VALUES(?,?,?,?,?,1)
                       ON CONFLICT(hour_ts) DO UPDATE SET
                         sum_w = sum_w + excluded.sum_w,
                         min_w = MIN(min_w, excluded.min_w),
                         max_w = MAX(max_w, excluded.max_w),
                         wh    = wh + excluded.wh,
                         n     = n + 1""",
                    (hour_ts, w, w, w, w * 15.0 / 3600.0))
                con.execute(
                    """INSERT INTO volt_hourly(hour_ts, sum_v, sum_v2, min_v, max_v, n, n_out)
                       VALUES(?,?,?,?,?,1,0)
                       ON CONFLICT(hour_ts) DO UPDATE SET
                         sum_v  = sum_v + excluded.sum_v,
                         sum_v2 = sum_v2 + excluded.sum_v2,
                         min_v  = MIN(min_v, excluded.min_v),
                         max_v  = MAX(max_v, excluded.max_v),
                         n      = n + 1""",
                    (hour_ts, v, v * v, v, v))
            t += 15
            n_samples += 1

    # ── 배터리 구동/전환 이벤트 몇 건 (이벤트 표·타일 검증용)
    with con:
        for days_ago, dur, kind in ((2, 47, "onbatt"), (9, 0, "xfer"), (21, 132, "onbatt")):
            ts = int((now - timedelta(days=days_ago, hours=3)).timestamp())
            con.execute(
                "INSERT INTO events(kind, ts, end_ts, detail, linev) VALUES(?,?,?,?,?)",
                (kind, ts, ts + dur, "Low line voltage", 138.0))

    con.close()
    print(f"주입 완료: hourly {n_hours}행, samples {n_samples}행 → {args.db}")
    print(f"검산 기준: 어제({yesterday0:%Y-%m-%d}) 일별 kWh = {CONST_W * 24 / 1000:.3f} 이어야 함")


if __name__ == "__main__":
    main()
