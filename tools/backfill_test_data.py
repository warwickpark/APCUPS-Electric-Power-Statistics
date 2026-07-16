#!/usr/bin/env python3
"""개발용 합성 데이터 주입 — 통계/대시보드 검증용.

- 과거 N개월(기본 14)의 hourly 롤업을 시간대별 사인 패턴으로 채움
- 어제(로컬 자정~자정)는 정확히 136.5W 고정 → 일별 kWh가 3.276이어야 함 (검산 기준)
- 최근 48시간은 원시 samples도 생성 (15초 간격)

사용: python tools/backfill_test_data.py [--db ups_power.db] [--months 14]
"""

import argparse
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ups_power_monitor import DEFAULT_DB, SCHEMA, local_hour_start

CONST_W = 136.5  # 검산용 고정 와트 (650W * 21%)


def pattern_watts(dt):
    """시간대 사인 패턴 + 월별 완만한 변동. 90~185W 범위."""
    hour_factor = math.sin((dt.hour - 6) / 24 * 2 * math.pi)  # 저녁 피크
    month_factor = math.cos(dt.month / 12 * 2 * math.pi)
    return 135 + 40 * hour_factor + 10 * month_factor


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--months", type=int, default=14)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)

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
            cur += timedelta(hours=1)
            n_hours += 1

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
            con.execute(
                "INSERT OR REPLACE INTO samples(ts, loadpct, watts, dt) "
                "VALUES(?,?,?,?)", (t, w / 6.5, w, 15.0))
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
            t += 15
            n_samples += 1

    con.close()
    print(f"주입 완료: hourly {n_hours}행, samples {n_samples}행 → {args.db}")
    print(f"검산 기준: 어제({yesterday0:%Y-%m-%d}) 일별 kWh = {CONST_W * 24 / 1000:.3f} 이어야 함")


if __name__ == "__main__":
    main()
