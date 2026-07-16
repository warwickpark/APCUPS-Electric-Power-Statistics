#!/usr/bin/env python3
"""개발용 가짜 apcaccess — 실제 Back-UPS BX1200MI 출력을 재현하되
LOADPCT만 18~25% 사이에서 랜덤 변동시킨다.

사용: python ups_power_monitor.py status --apcaccess "python tools/fake_apcaccess.py"
"""

import random
from datetime import datetime

now = datetime.now().strftime("%Y-%m-%d %H:%M:%S +0900")
loadpct = random.uniform(18.0, 25.0)

print(f"""APC      : 001,038,0980
DATE     : {now}
HOSTNAME : Warwick-UPS
VERSION  : 3.14.14 (31 May 2016) debian
UPSNAME  : Warwick-UPS
CABLE    : USB Cable
DRIVER   : USB UPS Driver
UPSMODE  : Stand Alone
STARTTIME: 2026-06-16 23:34:26 +0900
MODEL    : Back-UPS BX1200MI
STATUS   : ONLINE
LINEV    : 217.0 Volts
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
LASTXFER : Automatic or explicit self test
NUMXFERS : 2
XONBATT  : 2026-07-08 22:50:53 +0900
TONBATT  : 0 Seconds
CUMONBATT: 38 Seconds
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
END APC  : {now}""")
