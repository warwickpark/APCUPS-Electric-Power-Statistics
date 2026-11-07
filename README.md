# UPS 전력 모니터

> `apcaccess`가 읽어오는 UPS 부하율로 소비 전력을 추정해, 실시간·시간별·일별·월별 통계와 **월 누적 전력사용량(kWh)** 을 보여주는 라즈베리파이용 모니터.

![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi%20%2F%20Debian-C51A4A?logo=raspberrypi&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

apcupsd가 붙어 있는 라즈베리파이에서 상시 실행되며, `apcaccess`의 `LOADPCT`(부하 %)와
`NOMPOWER`(정격 전력)로 소비 전력을 계산해 SQLite에 적산한다. 통계는 **웹 대시보드**,
**CLI**, **16x2 문자 LCD**, **콘솔 TUI**(3.5인치 SPI LCD용)로 확인할 수 있고, 서비스 하나(`collect`)가 이 셋을 모두 구동한다.

전원을 제어하는 호스트에서 돌아가는 것을 전제로, 예기치 못한 정전에도 DB가 깨지지 않도록
설계했다(WAL + `synchronous=FULL`, 기동 시 자가 복구, 일일 백업).

## 주요 기능

- 🔌 **전력 추정** — `W = NOMPOWER × LOADPCT / 100`, 샘플별 시간 적분으로 kWh 산출
- 📊 **다층 통계** — 실시간 / 시간별(24h) / 일별(30일) / 월별(12개월) + 월 누적 kWh
- 🌐 **웹 대시보드** — 의존성 없는 단일 HTML, 라이트·다크 모드, 오프라인 동작
- 🖥️ **LCD 표시** — 기존 16x2 I2C 문자 LCD에 상태·전력을 순환 표시
- 📟 **TUI** — 3.5인치 SPI LCD(480x320) 콘솔용 curses 대시보드, 터치로 페이지 전환
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
| `tui` | 콘솔 TUI 대시보드 (아래 [TUI](#tui-35인치-spi-lcd) 참고) |

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

## TUI (3.5인치 SPI LCD)

[3.5inch RPi Display](https://www.lcdwiki.com/3.5inch_RPi_Display)(MPI3501, 480x320,
ILI9486 + XPT2046 터치) 같은 프레임버퍼 LCD의 리눅스 콘솔에 띄우는 curses 대시보드.
표준 라이브러리만 쓰며, 일반 SSH 터미널에서도 그대로 동작한다.

```sh
python3 ups_power_monitor.py tui
```

| 페이지 | 내용 |
|--------|------|
| `1 NOW` | 상태·모델, **큰 글씨 전력(W)**, 오늘/이번 달 kWh, 부하·배터리 막대, 런타임·전압, 최근 60분 추이 |
| `2 HOUR` | 최근 24시간 평균 W 막대그래프 + 합계 kWh |
| `3 DAY` | 최근 30일 kWh 막대그래프 + 일평균 |
| `4 MONTH` | 월별 누적 kWh 가로 막대 (진행 중인 달 `*`) |
| `5 SYS` | 호스트·IP·CPU 온도·업타임·샘플 경과·DB 크기·UPS 전환 이력 |

- **조작** — `←/→`(`h/l`, `Space`) 이동 · `1~5` 직접 선택 · `r` 새로고침 · `q` 종료
- **터치** — 화면 탭 = 다음 페이지. `/proc/bus/input/devices`에서 ADS7846/XPT2046을 찾아
  evdev를 직접 읽는다(좌표 보정 불필요, `input` 그룹 권한 필요). `--touch none|/dev/input/eventN`
- **데이터** — 통계는 DB를 읽기 전용으로 조회(5초 주기), 배터리·전압 등 실시간 값은
  `apcaccess`를 별도로 조회(`--ups-interval`, 기본 15초). 실패하면 DB 최신 샘플로 대체하고
  하단에 경고를 띄운다. `--no-apcaccess`로 끌 수 있다.
- **옵션** — `--cycle 10`(페이지 자동 순환) · `--ascii`(블록 문자 대신 `#`) · `--mono`
- 화면 크기에 맞춰 레이아웃이 바뀐다 (최소 30x8, 큰 글씨는 36x16 이상).

### LCD 준비

1. **드라이버** — lcdwiki 안내대로 [LCD-show](https://github.com/goodtft/LCD-show)의
   `sudo ./LCD35-show 90`(가로 480x320). 콘솔이 LCD로 나오면 된다.
   > ⚠️ LCD-show는 구형(레거시 fbdev) Raspberry Pi OS 기준이다. Bookworm 이후(KMS)에서는
   > 설치 스크립트가 데스크톱/부팅 설정을 깨뜨리는 사례가 많으니 백업 후 진행하거나,
   > `config.txt`에 `dtoverlay=piscreen,speed=16000000,rotate=90` 같은 오버레이로 대체를 검토할 것.
2. **콘솔 폰트** — `sudo dpkg-reconfigure console-setup` → UTF-8 / Terminus.
   480x320 기준 `8x16` → **60x20**(권장), `10x20` → 48x16, `12x24` → 40x13.
3. **서비스 등록** — tty1에 TUI를 띄우고 로그인 프롬프트는 끈다.

```sh
sudo cp ups-power-tui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ups-power-tui
```

> 리눅스 콘솔 폰트에는 **한글 글리프가 없어서** TUI 화면 문자열은 영문만 쓴다.
> `█ ▄ ░`(CP437 범위)가 깨져 보이면 `ExecStart`에 `--ascii`를 추가.
> 콘솔 화면 꺼짐(blank)은 TUI가 기동 시 해제한다(`--no-blank-off`로 끔).
> 수집기(`ups-power-monitor`)와는 별개 프로세스라 TUI가 죽어도 수집에는 영향이 없다.

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
python ups_power_monitor.py tui --apcaccess "python tools/fake_apcaccess.py"
```

## 프로젝트 구조

```
ups_power_monitor.py          메인 프로그램 (collect / stats / status / tui)
ups_tui.py                    콘솔 TUI (3.5인치 SPI LCD용)
static/index.html             웹 대시보드 (의존성 없음, 오프라인 동작)
ups-power-monitor.service     systemd 유닛 (수집기)
ups-power-tui.service         systemd 유닛 (tty1 TUI)
tools/fake_apcaccess.py       개발용 가짜 apcaccess
tools/backfill_test_data.py   개발용 합성 데이터 주입
```
