# UPS 전력 모니터

apcupsd의 `apcaccess` 출력에서 UPS 부하(`LOADPCT`)와 정격 전력(`NOMPOWER`)을 주기적으로 읽어
**실시간 / 시간별 / 일별 / 월별 전력 통계와 월 누적 전력사용량(kWh)** 을 제공한다.

- 전력 계산: `W = NOMPOWER × LOADPCT / 100` (예: Back-UPS BX1200MI 650W × 21% = 136.5W)
- 누적 전력량: 샘플별 `W × Δt` 시간 적분 → kWh
- Python 3 표준 라이브러리만 사용 — pip 설치 불필요
- 저장소는 SQLite(WAL) — 외부 프로그램이 읽기 전용으로 동시 접근 가능

## 설치 (Raspberry Pi / Debian)

```sh
# 1. 파일 복사 (Windows 개발 PC에서)
scp -r . warwick@warwick-ups:/home/warwick/ups-power-monitor

# 2. Pi에서 동작 확인
cd ~/ups-power-monitor
python3 ups_power_monitor.py status        # 현재 부하/전력 1회 출력

# 3. (LCD 사용 시) 기존 LCD 스크립트 중지 + RPLCD 확인
python3 -c "import RPLCD" || pip3 install RPLCD

# 4. systemd 서비스 등록
sudo cp ups-power-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ups-power-monitor
systemctl status ups-power-monitor         # 확인
```

> 서비스 기본 실행줄에 `--lcd`가 포함되어 있다. LCD가 없는 환경이면
> `ups-power-monitor.service`의 `ExecStart`에서 `--lcd`를 빼면 된다
> (RPLCD 미설치 시에도 경고만 남기고 수집은 정상 동작).

- 웹 대시보드: `http://warwick-ups:8080/`
- CLI 통계: `python3 ups_power_monitor.py stats`
- 로그: `journalctl -u ups-power-monitor -f`

## 명령어

| 명령 | 설명 |
|------|------|
| `collect` | 상시 수집 + 웹 대시보드 + LCD (systemd가 실행). `--interval`(기본 15초), `--port`(기본 8080), `--bind`, `--lcd` |
| `stats` | 실시간/시간별(24h)/일별(30일)/월별(12개월) 통계와 오늘·이번 달 누적 kWh 출력 |
| `status` | apcaccess를 1회 파싱해 현재 상태 출력 (DB 불필요, 설치 확인용) |

공통 옵션: `--db 경로`(기본 스크립트 옆 `ups_power.db`), `--apcaccess 명령`,
`--nominal-power W`(NOMPOWER를 보고하지 않는 기종용).

## LCD 상태 표시 (`--lcd`)

16x2 I2C 문자 LCD(PCF8574, 기본 주소 0x20)에 3초 간격으로 화면을 순환 표시한다.
기존 `LCDv2.py`를 통합한 것 — 수집기가 15초마다 읽는 apcaccess 데이터를 공유하므로
별도 apcaccess 호출이 없다.

화면 순서 (원래 5개 + 전력 통계 2개):

1. `STATUS / LOAD%` &nbsp; 2. `BATTERY% / LEFT(min)` &nbsp; 3. `AC V / BATT V`
4. `IP 주소` &nbsp; 5. `날짜시각 / RPi 온도`
6. **`POWER W / TODAY kWh`** &nbsp; 7. **`MONTH 누적 kWh`**

- 의존성: `pip3 install RPLCD` (`--lcd`를 쓸 때만 필요 — 없으면 경고 후 LCD 없이 수집 계속)
- 옵션: `--lcd-address 0x20` `--lcd-port 1` `--lcd-expander PCF8574`
- 종료 시(SIGTERM 포함) LCD를 클리어하고 닫는다
- ⚠ 기존 `LCDv2.py`(또는 그것을 실행하던 서비스/cron)는 **중지할 것** —
  두 프로세스가 같은 LCD에 쓰면 표시가 깨진다

## HTTP API

| 경로 | 응답 |
|------|------|
| `GET /api/realtime` | `{ts, loadpct, watts, status}` — 최근 샘플 |
| `GET /api/hourly?hours=24` | 시간별 `[{t, avg_w, min_w, max_w, kwh, n}, …]` (`t`는 정시 epoch 초) |
| `GET /api/daily?days=30` | 일별 (`t`는 `"YYYY-MM-DD"`) |
| `GET /api/monthly?months=12` | 월별 (`t`는 `"YYYY-MM"`) |
| `GET /api/summary` | `{today_kwh, month_kwh, month}` |

## 외부 프로그램에서 DB 읽기

DB는 WAL 모드라 collect가 기록 중에도 읽기 전용으로 안전하게 열 수 있다:

```python
import sqlite3
con = sqlite3.connect("file:/home/warwick/ups-power-monitor/ups_power.db?mode=ro", uri=True)
print(con.execute("SELECT * FROM v_monthly ORDER BY month DESC LIMIT 3").fetchall())
```

### 스키마 (하위호환 유지 방침)

```sql
samples(ts, loadpct, watts, dt)
-- ts: unix epoch 초 (PK) · loadpct: 부하 % · watts: 추정 전력 W
-- dt: 이 샘플이 대표하는 시간(초). 원시 샘플은 90일 보존 후 자동 삭제

hourly(hour_ts, sum_w, min_w, max_w, wh, n)
-- 시간별 롤업 (영구 보존, 일/월 통계의 원천)
-- hour_ts: 로컬 정시의 epoch · wh: Σ(watts×dt)/3600 · 평균W = sum_w/n

-- 편의 VIEW (GROUP BY 없이 바로 조회)
v_daily(day, avg_w, min_w, max_w, kwh, samples)     -- day: 'YYYY-MM-DD' (로컬)
v_monthly(month, avg_w, min_w, max_w, kwh, samples) -- month: 'YYYY-MM' (로컬)
```

집계 예시 — 이번 달 누적 kWh:

```sql
SELECT kwh FROM v_monthly WHERE month = strftime('%Y-%m', 'now', 'localtime');
```

## 내결함성 설계

이 호스트는 apcupsd로 UPS 셧다운을 제어하는 주체이므로, 예측 불가한 전원 단절에도
DB가 손상되지 않도록 설계되어 있다:

- **WAL + `synchronous=FULL`**: 전원 단절 시에도 DB 파일은 마지막 커밋 지점으로 복구되며,
  커밋된 샘플은 유실되지 않는다 (매 커밋 fsync, 15초당 1회 수준)
- **정상 종료**: SIGTERM 수신 시 커밋 완료 → WAL checkpoint → close.
  UPS 이벤트로 시스템이 종료될 때 systemd가 이 경로를 밟는다
- **기동 시 자가 점검**: `PRAGMA quick_check` 실패 시 손상 DB를
  `ups_power.db.corrupt-<시각>`으로 격리하고 최신 백업에서 자동 복원 (백업이 없으면 새로 생성).
  어떤 경우에도 모니터가 기동 실패로 호스트 운영을 방해하지 않는다
- **일일 백업**: `backups/ups_power-YYYYMMDD.db` 하루 1회 스냅샷, 7개 보존

## 개발 (Windows/어디서든, 실제 UPS 없이)

```sh
# 가짜 apcaccess로 수집 테스트
python ups_power_monitor.py status  --apcaccess "python tools/fake_apcaccess.py"
python ups_power_monitor.py collect --apcaccess "python tools/fake_apcaccess.py" --interval 2

# 수개월치 합성 데이터 주입 후 통계/대시보드 확인
python tools/backfill_test_data.py
python ups_power_monitor.py stats
```

## 파일 구성

```
ups_power_monitor.py        메인 프로그램 (collect / stats / status)
static/index.html           웹 대시보드 (의존성 없음, 오프라인 동작)
ups-power-monitor.service   systemd 유닛
tools/fake_apcaccess.py     개발용 가짜 apcaccess
tools/backfill_test_data.py 개발용 합성 데이터 주입
```
