#!/usr/bin/env python3
"""개발용 가짜 apcaccess — 실제 Back-UPS BX1200MI 출력을 재현하되
LOADPCT와 LINEV를 변동시키고, 가끔 전압 저하/배터리 전환을 섞는다.

NUMXFERS는 상태 파일에 누적된다(공급전원 이벤트 검증용).
상태 파일 경로는 FAKE_UPS_STATE 환경변수로 바꿀 수 있다.

사용: python ups_power_monitor.py status --apcaccess "python tools/fake_apcaccess.py"
"""

import json
import os
import random
import tempfile
from datetime import datetime

STATE = os.environ.get(
    "FAKE_UPS_STATE", os.path.join(tempfile.gettempdir(), "fake_apcaccess.json"))
SAG_PROBABILITY = 0.08  # 호출당 전압 이상 발생 확률

state = {"numxfers": 2, "cumonbatt": 38}
try:
    with open(STATE) as f:
        state.update(json.load(f))
except (OSError, ValueError):
    pass

now = datetime.now()
stamp = now.strftime("%Y-%m-%d %H:%M:%S +0900")
loadpct = random.uniform(18.0, 25.0)

# 평시: 기준 230V 근처에서 완만히 흔들림. 가끔 저하/상승, 드물게 배터리 전환.
linev = round(random.gauss(217.0, 2.5), 1)
status = "ONLINE"
if random.random() < SAG_PROBABILITY:
    kind = random.choice(("sag", "deep_sag", "swell"))
    if kind == "sag":
        linev = round(random.uniform(196.0, 206.0), 1)     # 정상 범위 이탈
    elif kind == "swell":
        linev = round(random.uniform(254.0, 262.0), 1)
    else:
        linev = round(random.uniform(120.0, 148.0), 1)     # LOTRANS 미만 → 전환
        status = "ONBATT"
        state["numxfers"] += 1
        state["cumonbatt"] += 15

try:
    with open(STATE, "w") as f:
        json.dump(state, f)
except OSError:
    pass

print(f"""APC      : 001,038,0980
DATE     : {stamp}
HOSTNAME : Warwick-UPS
VERSION  : 3.14.14 (31 May 2016) debian
UPSNAME  : Warwick-UPS
CABLE    : USB Cable
DRIVER   : USB UPS Driver
UPSMODE  : Stand Alone
STARTTIME: 2026-06-16 23:34:26 +0900
MODEL    : Back-UPS BX1200MI
STATUS   : {status}
LINEV    : {linev} Volts
LOADPCT  : {loadpct:.1f} Percent
BCHARGE  : 100.0 Percent
TIMELEFT : 17.3 Minutes
MBATTCHG : 5 Percent
MINTIMEL : 3 Minutes
MAXTIME  : 0 Seconds
SENSE    : Medium
LOTRANS  : 150.0 Volts
HITRANS  : 290.0 Volts
ALARMDEL : 30 Seconds
BATTV    : 13.5 Volts
LASTXFER : Low line voltage
NUMXFERS : {state['numxfers']}
XONBATT  : 2026-07-08 22:50:53 +0900
TONBATT  : 0 Seconds
CUMONBATT: {state['cumonbatt']} Seconds
XOFFBATT : 2026-07-08 22:51:12 +0900
LASTSTEST: 2026-07-08 22:50:53 +0900
SELFTEST : OK
STATFLAG : 0x05000008
SERIALNO : 9B2109A27782
BATTDATE : 2025-11-21
NOMINV   : 230 Volts
NOMBATTV : 12.0 Volts
NOMPOWER : 650 Watts
FIRMWARE : 294201G -302201G
END APC  : {stamp}""")
