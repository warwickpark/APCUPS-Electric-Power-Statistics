#!/usr/bin/env python3
"""UPS 전력 모니터 TUI — 3.5인치 SPI LCD(480x320, ILI9486/XPT2046) 콘솔용 프론트엔드.

`ups_power_monitor.py tui` 로 실행한다. 수집기(collect)와 별도 프로세스이며
DB는 읽기 전용으로만 연다. 실시간 UPS 상태(배터리·전압 등)는 apcaccess를
백그라운드 스레드에서 직접 조회한다(실패해도 DB 값으로 계속 표시).

리눅스 콘솔(fbcon)은 한글 글리프가 없으므로 화면 문자열은 ASCII만 쓴다.
블록 문자(█ ▄)는 CP437 범위라 콘솔 폰트에서도 표시된다. 깨지면 --ascii.

조작: ←/→ 또는 h/l 페이지 이동, 1~5 페이지 직접 선택, q 종료.
터치스크린: 탭하면 다음 페이지(evdev 직접 읽기, input 그룹 권한 필요).
"""

import curses
import locale
import os
import queue
import signal
import socket
import struct
import threading
import time
from datetime import datetime

import ups_power_monitor as upm

PAGES = ("NOW", "HOUR", "DAY", "MONTH", "SYS")
STALE_SEC = 120          # 이 시간보다 오래된 샘플이면 수집기 정지 경고
DB_REFRESH_SEC = 5       # DB 재조회 주기
TICK_MS = 250            # 입력 폴링/화면 갱신 주기

# 5행 x 3열 큰 숫자 폰트
BIG_FONT = {
    "0": ["###", "# #", "# #", "# #", "###"],
    "1": [" # ", "## ", " # ", " # ", "###"],
    "2": ["###", "  #", "###", "#  ", "###"],
    "3": ["###", "  #", " ##", "  #", "###"],
    "4": ["# #", "# #", "###", "  #", "  #"],
    "5": ["###", "#  ", "###", "  #", "###"],
    "6": ["###", "#  ", "###", "# #", "###"],
    "7": ["###", "  #", "  #", "  #", "  #"],
    "8": ["###", "# #", "###", "# #", "###"],
    "9": ["###", "# #", "###", "  #", "###"],
    ".": [" ", " ", " ", " ", "#"],
    "-": ["   ", "   ", "###", "   ", "   "],
    " ": ["  "] * 5,
}


# ------------------------------------------------------------- 데이터 소스

class UpsPoller(threading.Thread):
    """apcaccess를 주기적으로 실행해 최신 필드를 보관."""

    def __init__(self, cmd, nominal_power, interval):
        super().__init__(daemon=True)
        self.cmd = cmd
        self.nominal_power = nominal_power
        self.interval = interval
        self.lock = threading.Lock()
        self.data = None      # {"ts", "loadpct", "watts", "status", "fields"}
        self.error = None

    def run(self):
        while True:
            try:
                loadpct, watts, status, fields = upm.read_ups(
                    self.cmd, self.nominal_power)
                with self.lock:
                    self.data = {"ts": time.time(), "loadpct": loadpct,
                                 "watts": watts, "status": status,
                                 "fields": fields}
                    self.error = None
            except Exception as e:
                with self.lock:
                    self.error = str(e)
            time.sleep(self.interval)

    def snapshot(self):
        with self.lock:
            return self.data, self.error


class DbData:
    """통계 조회 결과 캐시. DB가 없거나 잠겨도 예외를 밖으로 내지 않는다."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.at = 0.0
        self.error = None
        self.latest = None
        self.recent = []       # 최근 1시간 [(ts, watts)]
        self.hourly = []
        self.daily = []
        self.monthly = []
        self.summary = {"today_kwh": 0.0, "month_kwh": 0.0, "month": "?"}
        self.db_size = 0

    def refresh(self, force=False):
        now = time.time()
        if not force and now - self.at < DB_REFRESH_SEC:
            return
        self.at = now
        if not os.path.exists(self.db_path):
            self.error = "DB not found (collector not started?)"
            return
        try:
            con = upm.open_db_ro(self.db_path)
            try:
                self.latest = upm.query_latest_sample(con)
                self.recent = con.execute(
                    "SELECT ts, watts FROM samples WHERE ts >= ? ORDER BY ts",
                    (int(now) - 3600,)).fetchall()
                self.hourly = upm.query_hourly(con, hours=24, now=now)
                self.daily = upm.query_daily(con, days=30, now=now)
                self.monthly = upm.query_monthly(con, months=12)
                self.summary = upm.query_summary(con, now=now)
            finally:
                con.close()
            self.db_size = sum(
                os.path.getsize(self.db_path + s)
                for s in ("", "-wal") if os.path.exists(self.db_path + s))
            self.error = None
        except Exception as e:
            self.error = f"DB error: {e}"


# ---------------------------------------------------------- 터치 (evdev)

EV_KEY, BTN_TOUCH = 0x01, 0x14a
EVENT_FMT = "llHHi"   # struct input_event (네이티브 long = 커널 ABI)
TOUCH_NAMES = ("ads7846", "xpt2046", "touch")


def find_touch_device():
    """ /proc/bus/input/devices에서 터치 컨트롤러의 eventN 경로를 찾는다."""
    try:
        with open("/proc/bus/input/devices") as f:
            blocks = f.read().split("\n\n")
    except OSError:
        return None
    for block in blocks:
        name = ""
        handlers = ""
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.lower()
            elif line.startswith("H: Handlers="):
                handlers = line
        if any(n in name for n in TOUCH_NAMES):
            for h in handlers.split("=", 1)[-1].split():
                if h.startswith("event"):
                    return f"/dev/input/{h}"
    return None


class TouchReader(threading.Thread):
    """터치 누름(BTN_TOUCH=1)마다 큐에 'tap'을 넣는다. 좌표 보정은 하지 않는다."""

    def __init__(self, path, out):
        super().__init__(daemon=True)
        self.path = path
        self.out = out
        self.error = None

    def run(self):
        size = struct.calcsize(EVENT_FMT)
        last = 0.0
        try:
            with open(self.path, "rb", buffering=0) as f:
                while True:
                    buf = f.read(size)
                    if len(buf) < size:
                        return
                    _, _, typ, code, value = struct.unpack(EVENT_FMT, buf)
                    if typ == EV_KEY and code == BTN_TOUCH and value == 1:
                        now = time.monotonic()
                        if now - last > 0.35:   # 채터링 방지
                            self.out.put("tap")
                        last = now
        except OSError as e:
            self.error = str(e)


# --------------------------------------------------------------- 유틸

def get_uptime():
    try:
        with open("/proc/uptime") as f:
            sec = int(float(f.read().split()[0]))
    except (OSError, ValueError):
        return "?"
    d, rem = divmod(sec, 86400)
    h, m = rem // 3600, rem % 3600 // 60
    return f"{d}d {h:02d}:{m:02d}" if d else f"{h:02d}:{m:02d}"


def get_hostname():
    try:
        return socket.gethostname()
    except OSError:
        return "?"


def num(s):
    """'217.0 Volts' → 217.0 (없으면 None)."""
    return upm.leading_float(s)


def fmt_kwh(v):
    return f"{v:.3f}" if v < 10 else f"{v:.2f}" if v < 100 else f"{v:.1f}"


# ------------------------------------------------------------------ 화면

class Tui:
    def __init__(self, stdscr, args):
        self.scr = stdscr
        self.args = args
        self.ascii = args.ascii
        self.full = "#" if self.ascii else "█"     # █
        self.half = "_" if self.ascii else "▄"     # ▄
        self.shade = "." if self.ascii else "░"    # ░
        self.page = 0
        self.last_switch = time.monotonic()
        self.db = DbData(args.db)
        self.poller = None
        if not args.no_apcaccess:
            self.poller = UpsPoller(args.apcaccess, args.nominal_power,
                                    args.ups_interval)
            self.poller.start()
        self.events = queue.Queue()
        self.touch = None
        path = None if args.touch == "none" else (
            find_touch_device() if args.touch == "auto" else args.touch)
        if path:
            self.touch = TouchReader(path, self.events)
            self.touch.start()
        self._init_colors()

    # ---- 색
    def _init_colors(self):
        self.c = {k: curses.A_NORMAL for k in
                  ("ok", "warn", "bad", "head", "dim", "bar", "hl", "tab")}
        self.c["tab"] = curses.A_REVERSE
        self.c["head"] = curses.A_BOLD
        if self.args.mono or not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        pairs = {
            "ok": curses.COLOR_GREEN, "warn": curses.COLOR_YELLOW,
            "bad": curses.COLOR_RED, "head": curses.COLOR_CYAN,
            "dim": curses.COLOR_BLUE, "bar": curses.COLOR_GREEN,
            "hl": curses.COLOR_YELLOW,
        }
        for i, (k, fg) in enumerate(pairs.items(), start=1):
            curses.init_pair(i, fg, bg)
            self.c[k] = curses.color_pair(i)
        self.c["head"] |= curses.A_BOLD
        curses.init_pair(len(pairs) + 1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        self.c["tab"] = curses.color_pair(len(pairs) + 1) | curses.A_BOLD

    # ---- 그리기 기본
    def put(self, y, x, s, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        s = s[: max(0, w - x)]
        if y == h - 1 and x + len(s) >= w:   # 우하단 칸 쓰기 오류 회피
            s = s[: w - x - 1]
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass

    def hbar(self, y, x, width, frac, attr):
        frac = max(0.0, min(1.0, frac or 0.0))
        n = int(round(frac * width))
        self.put(y, x, self.full * n, attr)
        self.put(y, x + n, self.shade * (width - n), self.c["dim"])

    def big(self, y, x, text, attr):
        """5행 큰 글씨. 그린 폭을 반환."""
        cx = x
        for ch in text:
            glyph = BIG_FONT.get(ch)
            if not glyph:
                continue
            for r, row in enumerate(glyph):
                self.put(y + r, cx, row.replace("#", self.full), attr)
            cx += len(glyph[0]) + 1
        return cx - x

    def vbars(self, y, x, h, w, values, labels, highlight=None, unit=""):
        """세로 막대그래프(반칸 해상도). labels: {index: 문자열} 하단 표시."""
        if not values or h < 2 or w < 10:
            self.put(y + h // 2, x + 2, "(no data)", self.c["dim"])
            return
        vmax = max(values) or 1.0
        vmin = min(values)
        # 변동이 작으면(최소 > 최대의 절반) 0 대신 최소 근처를 바닥으로 — 추이가 보이게
        lo = 0.0 if vmin < vmax * 0.5 else vmin - (vmax - vmin) * 0.25
        if vmax - lo < 1e-9:
            lo = 0.0
        fmt = (lambda v: f"{v:.0f}") if vmax >= 10 else (lambda v: f"{v:.2f}")
        top, bottom = fmt(vmax) + unit, fmt(lo)
        aw = max(len(top), len(bottom)) + 1
        self.put(y, x, top.rjust(aw - 1), self.c["dim"])
        self.put(y + h - 1, x, bottom.rjust(aw - 1),
                 self.c["warn"] if lo else self.c["dim"])
        avail = w - aw
        n = len(values)
        step = max(1, avail // n)
        bw = max(1, step - 1) if step > 1 else 1
        values = values[-(avail // step):]
        off = n - len(values)
        for i, v in enumerate(values):
            units = max(1 if v > 0 else 0, int(round((v - lo) / (vmax - lo) * h * 2)))
            attr = self.c["hl"] if (i + off) == highlight else self.c["bar"]
            bx = x + aw + i * step
            for r in range(h):
                u = units - 2 * r
                ch = self.full if u >= 2 else self.half if u == 1 else ""
                if ch:
                    self.put(y + h - 1 - r, bx, ch * bw, attr)
            lab = labels.get(i + off)
            if lab:
                self.put(y + h, bx, lab, self.c["dim"])

    # ---- 공통 헤더/푸터
    def ups_state(self):
        """(status, fields, watts, loadpct, 출처) — apcaccess 우선, 없으면 DB."""
        data, _ = self.poller.snapshot() if self.poller else (None, None)
        if data and time.time() - data["ts"] < STALE_SEC:
            return (data["status"], data["fields"], data["watts"],
                    data["loadpct"], "live")
        if self.db.latest:
            ts, loadpct, watts = self.db.latest
            return (None, {}, watts, loadpct, "db")
        return (None, {}, None, None, None)

    def status_attr(self, status):
        if not status:
            return self.c["warn"]
        if "ONBATT" in status or "LOWBATT" in status or "COMMLOST" in status:
            return self.c["bad"] | curses.A_BOLD
        if "ONLINE" in status and status.strip() == "ONLINE":
            return self.c["ok"] | curses.A_BOLD
        return self.c["warn"] | curses.A_BOLD

    def header(self, w):
        status, _, _, _, _ = self.ups_state()
        clock = datetime.now().strftime("%m-%d %H:%M:%S" if w >= 44 else "%H:%M:%S")
        st = (status or "N/A").strip()
        self.put(0, 0, " " * w, self.c["tab"])
        self.put(0, 1, "UPS", self.c["tab"])
        self.put(0, 5, f" {st} ", self.status_attr(status) | curses.A_REVERSE)
        self.put(0, w - len(clock) - 1, clock, self.c["tab"])

    def footer(self, h, w):
        x = 0
        for i, name in enumerate(PAGES):
            label = f" {i + 1}{name} " if w >= 40 else f" {name[:3]} "
            attr = self.c["tab"] if i == self.page else self.c["dim"]
            self.put(h - 1, x, label, attr)
            x += len(label)
        warn = self.warning()
        if warn and x + 2 < w:
            self.put(h - 1, x + 1, warn[: w - x - 2], self.c["bad"] | curses.A_BOLD)

    def warning(self):
        if self.db.error:
            return self.db.error
        if self.db.latest and time.time() - self.db.latest[0] > STALE_SEC:
            return f"collector stale {int(time.time() - self.db.latest[0])}s"
        if self.poller:
            data, err = self.poller.snapshot()
            if err and not data:
                return "apcaccess failed"
        return None

    # ---- 페이지 1: 현재 상태
    def page_now(self, y0, h, w):
        status, f, watts, loadpct, _ = self.ups_state()
        s = self.db.summary
        y = y0
        model = f.get("MODEL", "").strip()
        if model:
            self.put(y, 1, model[: w - 2], self.c["dim"])
            y += 1

        wtxt = f"{watts:.1f}" if watts is not None else "--"
        if h >= 16 and w >= 36:
            bw = self.big(y + 1, 2, wtxt, self.c["head"])
            self.put(y + 5, 3 + bw, "W", self.c["head"] | curses.A_BOLD)
            rx = 6 + bw
            if w - rx >= 16:
                self.put(y + 1, rx, "TODAY", self.c["dim"])
                self.put(y + 2, rx, f"{fmt_kwh(s['today_kwh'])} kWh", curses.A_BOLD)
                self.put(y + 3, rx, f"MONTH {s['month'][5:]}", self.c["dim"])
                self.put(y + 4, rx, f"{fmt_kwh(s['month_kwh'])} kWh", curses.A_BOLD)
                kwh_shown = True
            else:
                kwh_shown = False
            y += 7
        else:
            self.put(y, 1, "POWER", self.c["dim"])
            self.put(y, 8, f"{wtxt} W", self.c["head"] | curses.A_BOLD)
            y += 1
            kwh_shown = False

        bw = max(8, w - 20)
        bch = num(f.get("BCHARGE"))
        tleft = num(f.get("TIMELEFT"))
        rows = [
            ("LOAD", loadpct, f"{loadpct:5.1f}%" if loadpct is not None else "  --",
             self.c["bar"] if (loadpct or 0) < 80 else self.c["bad"]),
            ("BATT", bch, f"{bch:5.0f}%" if bch is not None else "  --",
             self.c["ok"] if (bch or 0) >= 50 else self.c["warn"] if (bch or 0) >= 20
             else self.c["bad"]),
        ]
        for label, val, txt, attr in rows:
            if y >= y0 + h:
                break
            self.put(y, 1, label, self.c["dim"])
            self.hbar(y, 6, bw, (val or 0) / 100.0, attr)
            self.put(y, 7 + bw, txt, curses.A_BOLD)
            y += 1

        info = []
        if tleft is not None:
            info.append(f"RUN {tleft:.0f}min")
        if num(f.get("LINEV")) is not None:
            info.append(f"AC {num(f.get('LINEV')):.0f}V")
        if num(f.get("BATTV")) is not None:
            info.append(f"BATT {num(f.get('BATTV')):.1f}V")
        if info and y < y0 + h:
            self.put(y, 1, "  ".join(info)[: w - 2])
            y += 1
        if not kwh_shown and y < y0 + h:
            self.put(y, 1, f"TODAY {fmt_kwh(s['today_kwh'])}kWh  "
                           f"MONTH {fmt_kwh(s['month_kwh'])}kWh", curses.A_BOLD)
            y += 1

        # 남은 공간: 최근 1시간 전력 추이
        ch = y0 + h - y - 2
        if ch >= 2 and self.db.recent:
            self.put(y, 1, "LAST 60 MIN (W)", self.c["dim"])
            vals = self._bucket_recent(w - 8)
            self.vbars(y + 1, 1, ch, w - 2, vals, {}, highlight=len(vals) - 1)

    def _bucket_recent(self, cols):
        """최근 1시간 샘플을 cols개 구간 평균으로."""
        now = time.time()
        cols = max(1, cols)
        sums = [0.0] * cols
        cnt = [0] * cols
        for ts, wv in self.db.recent:
            i = int((ts - (now - 3600)) / 3600 * cols)
            if 0 <= i < cols:
                sums[i] += wv
                cnt[i] += 1
        out, last = [], 0.0
        for s_, c_ in zip(sums, cnt):
            last = s_ / c_ if c_ else last
            out.append(last)
        return out

    # ---- 페이지 2: 시간별
    def page_hour(self, y0, h, w):
        rows = self.db.hourly
        total = sum(r[4] for r in rows)
        self.put(y0, 1, "HOURLY AVG W (24h)", self.c["head"])
        self.put(y0, w - 16, f"{fmt_kwh(total):>8} kWh", curses.A_BOLD)
        vals = [r[1] for r in rows]
        labels = {}
        for i, r in enumerate(rows):
            hr = datetime.fromtimestamp(r[0]).hour
            if hr % 6 == 0:
                labels[i] = f"{hr:02d}"
        self.vbars(y0 + 1, 1, h - 4, w - 2, vals, labels,
                   highlight=len(vals) - 1, unit="W")
        if rows:
            cur = rows[-1]
            self.put(y0 + h - 1, 1,
                     f"now {cur[1]:.0f}W  min {min(r[2] for r in rows):.0f}  "
                     f"max {max(r[3] for r in rows):.0f}"[: w - 2], self.c["dim"])

    # ---- 페이지 3: 일별
    def page_day(self, y0, h, w):
        rows = self.db.daily
        total = sum(r[4] for r in rows)
        self.put(y0, 1, "DAILY kWh (30d)", self.c["head"])
        self.put(y0, w - 16, f"{fmt_kwh(total):>8} kWh", curses.A_BOLD)
        vals = [r[4] for r in rows]
        labels = {}
        for i, r in enumerate(rows):
            d = int(r[0][8:10])
            if d in (1, 10, 20):
                labels[i] = str(d)
        self.vbars(y0 + 1, 1, h - 4, w - 2, vals, labels, highlight=len(vals) - 1)
        if rows:
            done = rows[:-1] or rows
            avg = sum(r[4] for r in done) / len(done)
            self.put(y0 + h - 1, 1,
                     f"today {fmt_kwh(rows[-1][4])}  avg/day {fmt_kwh(avg)}"[: w - 2],
                     self.c["dim"])

    # ---- 페이지 4: 월별
    def page_month(self, y0, h, w):
        rows = self.db.monthly[-(h - 1):]
        self.put(y0, 1, "MONTHLY kWh", self.c["head"])
        if not rows:
            self.put(y0 + 2, 2, "(no data)", self.c["dim"])
            return
        vmax = max(r[4] for r in rows) or 1.0
        this = self.db.summary.get("month")
        bw = max(4, w - 22)
        for i, r in enumerate(rows):
            y = y0 + 1 + i
            cur = r[0] == this
            self.put(y, 1, r[0][2:], self.c["hl"] if cur else self.c["dim"])
            self.hbar(y, 7, bw, r[4] / vmax, self.c["hl"] if cur else self.c["bar"])
            self.put(y, 8 + bw, f"{fmt_kwh(r[4]):>8}" + ("*" if cur else ""),
                     curses.A_BOLD if cur else 0)

    # ---- 페이지 5: 시스템
    def page_sys(self, y0, h, w):
        status, f, *_ = self.ups_state()
        latest = self.db.latest
        age = f"{int(time.time() - latest[0])}s ago" if latest else "none"
        items = [
            ("HOST", get_hostname()),
            ("IP", upm.get_ip()),
            ("CPU", f"{upm.get_cpu_temp()} C"),
            ("UPTIME", get_uptime()),
            ("SAMPLE", age),
            ("DB", f"{self.db.db_size / 1048576:.1f} MB"),
            ("MODEL", f.get("MODEL", "?").strip()),
            ("NOMPWR", f.get("NOMPOWER", "?")),
            ("XFERS", f.get("NUMXFERS", "?")),
            ("LASTXF", f.get("LASTXFER", "?")),
            ("ONBATT", f.get("XONBATT", "-")[:16]),
            ("TOUCH", self.touch.path if self.touch and not self.touch.error
             else (self.touch.error[:30] if self.touch else "off")),
        ]
        for i, (k, v) in enumerate(items[:h]):
            self.put(y0 + i, 1, f"{k:<7}", self.c["dim"])
            self.put(y0 + i, 9, str(v)[: w - 10])

    # ---- 루프
    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < 8 or w < 30:
            self.put(0, 0, f"too small {w}x{h}")
            self.scr.refresh()
            return
        self.header(w)
        body = (self.page_now, self.page_hour, self.page_day,
                self.page_month, self.page_sys)[self.page]
        body(2, h - 4, w)
        self.footer(h, w)
        self.scr.refresh()

    def switch(self, delta=None, to=None):
        self.page = to if to is not None else (self.page + delta) % len(PAGES)
        self.last_switch = time.monotonic()

    def run(self):
        curses.curs_set(0)
        self.scr.timeout(TICK_MS)
        while True:
            self.db.refresh()
            self.draw()
            key = self.scr.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in (curses.KEY_RIGHT, ord("l"), ord(" "), ord("\t")):
                self.switch(1)
            elif key in (curses.KEY_LEFT, ord("h"), curses.KEY_BTAB):
                self.switch(-1)
            elif ord("1") <= key < ord("1") + len(PAGES):
                self.switch(to=key - ord("1"))
            elif key in (ord("r"), ord("R")):
                self.db.refresh(force=True)
            elif key == curses.KEY_RESIZE:
                curses.update_lines_cols()
            while not self.events.empty():
                if self.events.get_nowait() == "tap":
                    self.switch(1)
            if (self.args.cycle > 0
                    and time.monotonic() - self.last_switch >= self.args.cycle):
                self.switch(1)


def _disable_console_blank():
    """리눅스 VT면 화면 꺼짐(blank) 타이머 해제."""
    if os.environ.get("TERM") != "linux":
        return
    try:
        os.write(1, b"\033[9;0]")
    except OSError:
        pass


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt  # curses.wrapper가 터미널을 복구하도록


def cmd_tui(args):
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    signal.signal(signal.SIGTERM, _on_sigterm)
    if not args.no_blank_off:
        _disable_console_blank()
    try:
        curses.wrapper(lambda scr: Tui(scr, args).run())
    except KeyboardInterrupt:
        pass
