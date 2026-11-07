# UPS 전력 모니터

> `apcaccess`가 읽어오는 UPS 부하율로 소비 전력을 추정해, 실시간·시간별·일별·월별 통계와 **월 누적 전력사용량(kWh)** 을 보여주고,
> 입력 전압(`LINEV`)을 함께 적산해 **공급전원 품질 보고서**까지 내는 라즈베리파이용 모니터.

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
- ⚡ **공급전원 보고서** — 입력 전압의 단기(시간별)·장기(일·월별) 변동, 표준편차,
  정상 범위 이탈률, 전압 저하·상승·배터리 전환 이벤트 로그
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

- 라즈베리파이 / Orange Pi 등 SBC + Debian 계열 + **apcupsd**(`apcaccess` 명령 제공)
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
#    최신 배포판은 PEP 668(EXTERNALLY-MANAGED)로 pip를 막으므로 --user 설치를 쓴다
python3 -c "import RPLCD" || pip3 install --user --break-system-packages RPLCD smbus2

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
| `quality` | 공급전원 보고서 — 입력 전압 단기/장기 변동 + 이벤트. `--hours`(기본 24), `--days`(기본 30), `--max-events`(기본 30) |
| `status` | `apcaccess`를 1회 파싱해 현재 상태 출력 (DB 불필요, 설치 확인용) |

**공통 옵션** — `--db 경로`(기본: 스크립트 옆 `ups_power.db`), `--apcaccess 명령`,
`--nominal-power W`(`NOMPOWER`를 보고하지 않는 기종용),
`--voltage-nominal V`(`NOMINV` 미보고 기종용 기준 전압), `--voltage-tolerance %`(정상 범위, 기본 ±10).

## 공급전원 보고서

수집기는 매 주기 `LINEV`(입력 전압)도 함께 적산한다. 정상 범위는 UPS가 보고하는
`NOMINV` ±10%(예: 230V → 207~253V)이고, 이를 벗어나면 이벤트로 남는다.

- **웹** — `http://<호스트>:8080/power-quality`
  (24시간·30일·12개월 최소~최대 밴드 차트, 일별 표준편차 추이, 이벤트 표)
- **CLI** — `python3 ups_power_monitor.py quality`

기록되는 이벤트: `sag`(전압 저하) · `swell`(전압 상승) · `onbatt`(배터리 구동) ·
`xfer`(배터리 전환) · `comm`(UPS 통신 두절). 각각 시작·종료 시각이 남는다.

> ⚠️ **측정 한계** — 15초 주기 폴링이므로 사이클 단위의 순간 전압강하·스파이크는
> 직접 관측되지 않는다(전력품질 계측기가 아니다). 관측되는 것은 완만한 전압 변동,
> 브라운아웃, 장기 추세다. 폴링 사이에 일어난 사건은 UPS 내부 카운터 `NUMXFERS`가
> 증가할 때 `xfer` 이벤트로만 기록되며 지속시간은 알 수 없다.

> 전압 통계는 **이 기능이 들어간 버전으로 수집을 시작한 이후 구간만** 집계된다.
> 기존 DB는 기동 시 자동 마이그레이션되고(`samples.linev` 추가), 과거 구간은 비어 있다.

## LCD 상태 표시 (`--lcd`)

16x2 I2C 문자 LCD(PCF8574, 기본 주소 `0x20`)에 3초 간격으로 화면을 순환 표시한다.
수집기가 15초마다 읽는 `apcaccess` 데이터를 공유하므로 LCD를 위한 별도 호출은 없다.

| # | 1행 / 2행 |
|---|-----------|
| 1 | `STATUS` / `LOAD %` |
| 2 | `BATTERY %` / `LEFT (min)` |
| 3 | `AC V` / `BATT V` |
| 4 | `IP:` / IP 주소 |
| 5 | 날짜·시각 / `CPU TEMP` |
| 6 | **`POWER W`** / **`TODAY kWh`** |
| 7 | **`MONTH`** / **누적 kWh** |
| 8 | **`AC 24H MIN/MAX`** / **24시간 전압 범위·평균** |

- 옵션: `--lcd-address 0x20` · `--lcd-port N` · `--lcd-expander PCF8574`
- ⚠️ `--lcd-port`는 **보드마다 다르다**. 라즈베리파이는 `1`이지만 다른 SBC에서는
  1번이 엉뚱한 버스(HDMI DDC 등)일 수 있다. 아래 "I2C 버스 찾기"로 확인할 것.
- 종료 시(SIGTERM 포함) LCD를 클리어하고 닫는다.
- ⚠️ 같은 LCD에 쓰는 다른 프로세스(별도 스크립트·cron 등)는 **반드시 중지**할 것.
  두 프로세스가 동시에 쓰면 표시가 깨진다.

### I2C 버스 찾기

버스 번호는 보드·커널마다 다르므로 이름으로 확인한다.

```sh
for d in /sys/class/i2c-dev/*; do
  echo "$(basename $d): $(cat $d/name) -> $(basename $(readlink -f $d/device))"
done
```

핀 헤더에 연결된 버스를 고른 뒤 스캔한다(PCF8574는 보통 `0x20` 또는 `0x27`).

```sh
i2cdetect -y <버스번호> 0x20 0x3f
```

`UU`는 커널 드라이버가 이미 점유한 주소라는 뜻이다(예: 전원관리칩). LCD가 아니다.

사용자를 `i2c` 그룹에 넣어야 서비스가 `/dev/i2c-*`에 접근할 수 있다
(`/dev/i2c-*`는 `root:i2c 660`). 재로그인 필요.

```sh
sudo usermod -aG i2c $USER
```

### Orange Pi Zero 3 (Allwinner H618)

기본 상태에서는 핀 헤더 I2C가 **꺼져 있다**. 노출된 `/dev/i2c-0`은 PMIC 전용
내부 버스, `/dev/i2c-1`은 HDMI DDC라 **둘 다 LCD용이 아니다.**

26핀 헤더의 I2C는 `i2c3` = PH5(SDA, 핀3) / PH4(SCK, 핀5)이고,
오버레이로 켜야 한다. 적용 후 재부팅하면 보통 `/dev/i2c-2`로 올라온다.

```sh
sudo cp /boot/armbianEnv.txt /boot/armbianEnv.txt.bak
echo 'overlays=i2c3-ph' | sudo tee -a /boot/armbianEnv.txt
sudo reboot
```

| 헤더 핀 | 연결 |
|---------|------|
| 1 | 3.3V |
| 2 / 4 | 5V |
| **3** | **SDA (PH5)** |
| **5** | **SCL (PH4)** |
| 6 | GND |

> **`I2C bus locked, block: 1` 이 반복된다면** SDA/SCL이 low에 붙은 것이다.
> 이 보드의 i2c3 핀에는 내부 풀업이 없어서 버스를 high로 올리는 것은 전적으로
> LCD 백팩 모듈의 풀업 저항이다. 즉 **모듈 전원(VCC/GND)이나 배선을 먼저 의심**할 것.
> 26핀 헤더라 40핀 라즈베리파이 감각으로 세면 어긋나기 쉽다.

> 부팅 로그의 `Error applying setting, reverse things back`는 무시해도 된다.
> 정상 동작하는 uart0·mmc에도 똑같이 찍히는 sunxi 커널 노이즈다.

## HTTP API

대시보드가 쓰는 JSON API. 외부에서도 그대로 호출할 수 있다.

| 경로 | 응답 |
|------|------|
| `GET /api/realtime` | `{ts, loadpct, watts, linev, status}` — 최근 샘플 |
| `GET /api/hourly?hours=24` | 시간별 `[{t, avg_w, min_w, max_w, kwh, n}, …]` (`t` = 정시 epoch 초) |
| `GET /api/daily?days=30` | 일별 (`t` = `"YYYY-MM-DD"`) |
| `GET /api/monthly?months=12` | 월별 (`t` = `"YYYY-MM"`) |
| `GET /api/summary` | `{today_kwh, month_kwh, month}` |
| `GET /api/voltage/hourly?hours=24` | 시간별 전압 `[{t, avg_v, min_v, max_v, sd_v, n, n_out, out_pct}, …]` |
| `GET /api/voltage/daily?days=30` | 일별 전압 (`t` = `"YYYY-MM-DD"`) |
| `GET /api/voltage/monthly?months=12` | 월별 전압 (`t` = `"YYYY-MM"`) |
| `GET /api/voltage/summary?hours=24` | 구간 요약 + 기준값(`nominal_v`, `low_v`, `high_v`, `lotrans`, `hitrans`) + 이벤트 건수 |
| `GET /api/events?days=30` | `[{kind, label, ts, end_ts, duration, ongoing, detail, linev}, …]` 최신순 |

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

-- 공급전원
samples.linev                                       -- 입력 전압 V (이전 버전 구간은 NULL)

volt_hourly(hour_ts, sum_v, sum_v2, min_v, max_v, n, n_out)
-- 전압 시간별 롤업. 평균 = sum_v/n · 표준편차 = sqrt(sum_v2/n - (sum_v/n)²)
-- n_out: 정상 범위(NOMINV ±허용오차)를 벗어난 샘플 수

events(id, kind, ts, end_ts, detail, linev)
-- kind: sag | swell | onbatt | xfer | comm · end_ts IS NULL 이면 진행 중

meta(key, value)  -- UPS가 보고한 기준값: nominal_v, low_v, high_v, lotrans, hitrans, sense, model

v_volt_daily(day, sum_v, sum_v2, min_v, max_v, n, n_out)
v_volt_monthly(month, sum_v, sum_v2, min_v, max_v, n, n_out)
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
python ups_power_monitor.py quality
```

`tools/fake_apcaccess.py`는 전압을 변동시키고 가끔 저하·배터리 전환을 섞으므로
공급전원 이벤트 경로까지 그대로 검증할 수 있다.

## 프로젝트 구조

```
ups_power_monitor.py          메인 프로그램 (collect / stats / quality / status)
static/index.html             웹 대시보드 — 전력 사용량
static/power-quality.html     웹 대시보드 — 공급전원 보고서
static/common.css             두 페이지 공용 스타일
static/common.js              두 페이지 공용 차트·유틸 (의존성 없음, 오프라인 동작)
ups-power-monitor.service     systemd 유닛
tools/fake_apcaccess.py       개발용 가짜 apcaccess
tools/backfill_test_data.py   개발용 합성 데이터 주입
```
