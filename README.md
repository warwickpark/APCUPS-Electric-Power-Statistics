# UPS 전력 모니터

> `apcaccess`가 읽어오는 UPS 부하율로 소비 전력을 추정해, 실시간·시간별·일별·월별 통계와 **월 누적 전력사용량(kWh)** 을 보여주는 라즈베리파이용 모니터.

![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi%20%2F%20Debian-C51A4A?logo=raspberrypi&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

apcupsd가 붙어 있는 라즈베리파이에서 상시 실행되며, `apcaccess`의 `LOADPCT`(부하 %)와
`NOMPOWER`(정격 전력)로 소비 전력을 계산해 SQLite에 적산한다. 통계는 **웹 대시보드**,
**CLI**, **16x2 문자 LCD** 세 가지로 확인할 수 있고, 서비스 하나(`collect`)가 이 셋을 모두 구동한다.

전원을 제어하는 호스트에서 돌아가는 것을 전제로, 예기치 못한 정전에도 DB가 깨지지 않도록
설계했다(WAL + `synchronous=FULL`, 기동 시 자가 복구, 일일 백업).

## 주요 기능

- 🔌 **전력 추정** — `W = NOMPOWER × LOADPCT / 100`, 샘플별 시간 적분으로 kWh 산출
- 📊 **다층 통계** — 실시간 / 시간별(24h) / 일별(30일) / 월별(12개월) + 월 누적 kWh
- 🌐 **웹 대시보드** — 의존성 없는 단일 HTML, 라이트·다크 모드, 오프라인 동작
- 🖥️ **LCD 표시** — 기존 16x2 I2C 문자 LCD에 상태·전력을 순환 표시
- 🗄️ **개방형 저장소** — WAL 모드 SQLite + 편의 VIEW로 외부 프로그램이 직접 조회
- 🛡️ **내결함성** — 정전에도 DB 무손상, 손상 시 백업 자동 복원, 기동 실패로 호스트 운영 방해 없음
- 🐍 **무의존성** — Python 3 표준 라이브러리만 사용(LCD 쓸 때만 `RPLCD` 선택)

## 동작 원리

```
apcaccess ──(LOADPCT, NOMPOWER)──▶ 전력(W) 계산 ──▶ SQLite 적산 ──┬──▶ 웹 대시보드 (:8080)
   15초 주기                          W×Δt → Wh        (WAL)        ├──▶ CLI (stats)
                                                                   └──▶ 16x2 LCD
```

예: Back-UPS BX1200MI(정격 650W)가 부하 21%면 `650 × 0.21 = 136.5W`.

## 요구 사항

- 라즈베리파이 / Debian 계열 + **apcupsd**(`apcaccess` 명령 제공)
- **Python 3** (표준 라이브러리만 사용)
- (선택) 16x2 I2C 문자 LCD + `RPLCD` — `--lcd` 옵션을 쓸 때만

## 설치

```sh
# 1. 라즈베리파이로 복사
scp -r . pi@raspberrypi:~/ups-power-monitor

# 2. 동작 확인 (DB 없이 현재 상태 1회 출력)
cd ~/ups-power-monitor
python3 ups_power_monitor.py status

# 3. (LCD 사용 시) RPLCD 설치 + 기존 LCD 스크립트 중지
python3 -c "import RPLCD" || pip3 install RPLCD

# 4. systemd 서비스 등록
sudo cp ups-power-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ups-power-monitor
systemctl status ups-power-monitor
```

> **경로/사용자 조정** — 서비스 유닛(`ups-power-monitor.service`)의 `User`,
> `WorkingDirectory`, `ExecStart`는 예시 값(`warwick`, `/home/warwick/...`)이다.
> 환경에 맞게 수정할 것.

> **LCD가 없다면** — `ExecStart`에서 `--lcd`를 빼면 된다.
> (`--lcd`를 켜도 RPLCD가 없으면 경고만 남기고 수집은 정상 동작한다.)

설치 후:

- 웹 대시보드 — `http://<파이-호스트>:8080/`
- CLI 통계 — `python3 ups_power_monitor.py stats`
- 로그 — `journalctl -u ups-power-monitor -f`

## 사용법

| 명령 | 설명 |
|------|------|
| `collect` | 상시 수집 + 웹 대시보드 + LCD (systemd가 실행). `--interval`(기본 15초), `--port`(기본 8080), `--bind`, `--lcd` |
| `stats` | 실시간·시간별(24h)·일별(30일)·월별(12개월) 통계와 오늘·이번 달 누적 kWh 출력 |
| `status` | `apcaccess`를 1회 파싱해 현재 상태 출력 (DB 불필요, 설치 확인용) |

**공통 옵션** — `--db 경로`(기본: 스크립트 옆 `ups_power.db`), `--apcaccess 명령`,
`--nominal-power W`(`NOMPOWER`를 보고하지 않는 기종용).

## LCD 상태 표시 (`--lcd`)

16x2 I2C 문자 LCD(PCF8574, 기본 주소 `0x20`)에 3초 간격으로 화면을 순환 표시한다.
수집기가 15초마다 읽는 `apcaccess` 데이터를 공유하므로 LCD를 위한 별도 호출은 없다.

| # | 1행 / 2행 |
|---|-----------|
| 1 | `STATUS` / `LOAD %` |
| 2 | `BATTERY %` / `LEFT (min)` |
| 3 | `AC V` / `BATT V` |
| 4 | `IP:` / IP 주소 |
| 5 | 날짜·시각 / `RPi TEMP` |
| 6 | **`POWER W`** / **`TODAY kWh`** |
| 7 | **`MONTH`** / **누적 kWh** |

- 옵션: `--lcd-address 0x20` · `--lcd-port 1` · `--lcd-expander PCF8574`
- 종료 시(SIGTERM 포함) LCD를 클리어하고 닫는다.
- ⚠️ 같은 LCD에 쓰는 다른 프로세스(별도 스크립트·cron 등)는 **반드시 중지**할 것.
  두 프로세스가 동시에 쓰면 표시가 깨진다.

## HTTP API

대시보드가 쓰는 JSON API. 외부에서도 그대로 호출할 수 있다.

| 경로 | 응답 |
|------|------|
| `GET /api/realtime` | `{ts, loadpct, watts, status}` — 최근 샘플 |
| `GET /api/hourly?hours=24` | 시간별 `[{t, avg_w, min_w, max_w, kwh, n}, …]` (`t` = 정시 epoch 초) |
| `GET /api/daily?days=30` | 일별 (`t` = `"YYYY-MM-DD"`) |
| `GET /api/monthly?months=12` | 월별 (`t` = `"YYYY-MM"`) |
| `GET /api/summary` | `{today_kwh, month_kwh, month}` |

## 데이터베이스 (외부 연동)

DB는 내부 저장소이자 **외부 프로그램용 읽기 인터페이스**다. WAL 모드라 수집 중에도
읽기 전용으로 안전하게 열 수 있다.

```python
import sqlite3
con = sqlite3.connect("file:ups_power.db?mode=ro", uri=True)
print(con.execute("SELECT * FROM v_monthly ORDER BY month DESC LIMIT 3").fetchall())
```

### 스키마

```sql
samples(ts, loadpct, watts, dt)
-- ts: unix epoch 초 (PK) · loadpct: 부하 % · watts: 추정 전력 W
-- dt: 이 샘플이 대표하는 시간(초). 원시 샘플은 90일 보존 후 자동 삭제

hourly(hour_ts, sum_w, min_w, max_w, wh, n)
-- 시간별 롤업 (영구 보존, 일/월 통계의 원천)
-- hour_ts: 로컬 정시의 epoch · wh: Σ(watts×dt)/3600 · 평균W = sum_w/n

-- 편의 VIEW (GROUP BY 없이 바로 조회)
v_daily(day, avg_w, min_w, max_w, kwh, samples)     -- day:   'YYYY-MM-DD' (로컬)
v_monthly(month, avg_w, min_w, max_w, kwh, samples) -- month: 'YYYY-MM'    (로컬)
```

단위는 W·Wh·epoch 초로 고정이며, 스키마는 하위호환을 유지한다. 예 — 이번 달 누적 kWh:

```sql
SELECT kwh FROM v_monthly WHERE month = strftime('%Y-%m', 'now', 'localtime');
```

## 내결함성 설계

이 모니터는 apcupsd로 UPS 셧다운을 제어하는 호스트에서 돌아가는 것을 전제로,
예기치 못한 전원 단절에도 DB가 손상되지 않도록 설계했다.

- **WAL + `synchronous=FULL`** — 전원 단절 시에도 DB 파일은 마지막 커밋 지점으로
  복구되고, 커밋된 샘플은 유실되지 않는다 (매 커밋 fsync, 15초당 1회 수준).
- **정상 종료** — SIGTERM 수신 시 커밋 완료 → WAL checkpoint → close.
  UPS 이벤트로 systemd가 서비스를 내릴 때 이 경로를 밟는다.
- **기동 시 자가 점검** — `PRAGMA quick_check` 실패 시 손상 DB를
  `ups_power.db.corrupt-<시각>`으로 격리하고 최신 백업에서 자동 복원(없으면 새로 생성).
  어떤 경우에도 기동 실패로 호스트 운영을 방해하지 않는다.
- **일일 백업** — `backups/ups_power-YYYYMMDD.db` 하루 1회 스냅샷, 7개 보존.

## 개발 / 테스트

실제 UPS 없이 어느 환경에서든 전 기능을 돌려볼 수 있다.

```sh
# 가짜 apcaccess로 파싱·수집 테스트
python ups_power_monitor.py status  --apcaccess "python tools/fake_apcaccess.py"
python ups_power_monitor.py collect --apcaccess "python tools/fake_apcaccess.py" --interval 2

# 수개월치 합성 데이터 주입 후 통계/대시보드 확인
python tools/backfill_test_data.py
python ups_power_monitor.py stats
```

## 프로젝트 구조

```
ups_power_monitor.py          메인 프로그램 (collect / stats / status)
static/index.html             웹 대시보드 (의존성 없음, 오프라인 동작)
ups-power-monitor.service     systemd 유닛
tools/fake_apcaccess.py       개발용 가짜 apcaccess
tools/backfill_test_data.py   개발용 합성 데이터 주입
```
