#!/usr/bin/env python3
"""UPS 전력 모니터 TUI — 3.5인치 SPI LCD(480x320, ILI9486/XPT2046) 콘솔용 프론트엔드.

`ups_power_monitor.py tui` 로 실행한다. 수집기(collect)와 별도 프로세스이며
DB는 읽기 전용으로만 연다. 실시간 UPS 상태(배터리·전압 등)는 apcaccess를
백그라운드 스레드에서 직접 조회한다(실패해도 DB 값으로 계속 표시).

리눅스 콘솔(fbcon)은 한글 글리프가 없으므로 화면 문자열은 ASCII만 쓴다.
블록 문자(█ ▄)는 CP437 범위라 콘솔 폰트에서도 표시된다. 깨지면 --ascii.

조작: ←/→ 또는 h/l 페이지 이동, 1~6 페이지 직접 선택, q 종료.
터치스크린: 탭하면 다음 페이지(evdev 직접 읽기, input 그룹 권한 필요).
"""

import curses
import locale
import math
import os
import queue
import shutil
import signal
import socket
import struct
import threading
import time
from datetime import datetime

import ups_power_monitor as upm

PAGES = ("NOW", "HOUR", "DAY", "MONTH", "VOLT", "SYS")
EVENT_NAMES = {"sag": "SAG", "swell": "SWELL", "onbatt": "ON BATT",
               "xfer": "XFER", "comm": "COMMLOST"}
RECENT_SPAN_SEC = 3600   # 1페이지 하단 차트가 보여주는 구간
# 세로 축 라벨에 늘 비워두는 최소 칸. 눈금 숫자 자릿수가 바뀌어도
# 막대 x좌표가 밀리지 않게 고정한다.
AXIS_MIN_W = 4
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

# 막대 한 칸을 몇 단계로 쪼갤지 — bpytop의 graph_symbol과 같은 개념.
# ladder[k-1] = "k/len(ladder)만큼 찬 칸", ladder[-1] = 꽉 찬 칸.
GRAPH_LADDERS = {
    # 1/8 블록 — 가장 매끄럽지만 콘솔 폰트에 U+2581..2587이 있어야 한다.
    # 데비안 기본 psf(Uni2/Lat15-Terminus)에는 없으므로 콘솔에서는 기본값이 아니다.
    "block": ["\u2581", "\u2582", "\u2583", "\u2584",
              "\u2585", "\u2586", "\u2587", "\u2588"],
    # 반칸 — U+2584(▄)가 있는 폰트용.
    "half": ["\u2584", "\u2588"],
    # 음영 — ░▒█ 만 쓰므로 fbcon 기본 폰트에서도 그대로 나온다.
    "shade": ["\u2591", "\u2592", "\u2588"],
    # 블록 문자가 전혀 안 되는 환경.
    "ascii": ["#"],
}


# 7행 x 5열 큰 숫자 폰트 (세로 여유가 있는 화면용)
BIG_FONT_L = {
    "0": ["#####", "#   #", "#   #", "#   #", "#   #", "#   #", "#####"],
    "1": ["  #  ", " ##  ", "  #  ", "  #  ", "  #  ", "  #  ", "#####"],
    "2": ["#####", "    #", "    #", "#####", "#    ", "#    ", "#####"],
    "3": ["#####", "    #", "    #", "#####", "    #", "    #", "#####"],
    "4": ["#   #", "#   #", "#   #", "#####", "    #", "    #", "    #"],
    "5": ["#####", "#    ", "#    ", "#####", "    #", "    #", "#####"],
    "6": ["#####", "#    ", "#    ", "#####", "#   #", "#   #", "#####"],
    "7": ["#####", "    #", "    #", "    #", "    #", "    #", "    #"],
    "8": ["#####", "#   #", "#   #", "#####", "#   #", "#   #", "#####"],
    "9": ["#####", "#   #", "#   #", "#####", "    #", "    #", "#####"],
    ".": [" ", " ", " ", " ", " ", " ", "#"],
    "-": ["     ", "     ", "     ", "#####", "     ", "     ", "     "],
    " ": ["   "] * 7,
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
        self.volt_hourly = []
        self.volt_summary = {}
        self.events = []
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
                self.volt_hourly = upm.query_voltage_hourly(con, hours=24, now=now)
                self.volt_summary = upm.query_voltage_summary(con, hours=24, now=now)
                self.events = upm.query_events(con, days=30, limit=20, now=now)
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


def nice_range(lo, hi, divisions=5):
    """lo~hi를 1/2/5 x 10^n 눈금에 맞춰 바깥으로 넓힌다.

    매 프레임 실측 min/max를 그대로 쓰면 샘플 하나에 축이 흔들려서
    막대 전체가 재계산된다. 눈금을 '적당한 수'에 떨어뜨려 고정한다.
    """
    span = hi - lo
    if span <= 0:
        return lo, lo + 1.0
    raw = span / max(1, divisions)
    mag = 10.0 ** math.floor(math.log10(raw))
    step = 10 * mag
    for m in (1, 2, 5):
        if raw <= m * mag:
            step = m * mag
            break
    lo2 = math.floor(lo / step) * step
    hi2 = math.ceil(hi / step) * step
    if hi2 - lo2 < 1e-9:
        hi2 = lo2 + step
    return lo2, hi2


def get_mem():
    """(사용률 %, 전체 MB) — /proc/meminfo 기준."""
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0])
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return 100.0 * (total - avail) / total, total / 1024.0
    except Exception:
        return None, None


def get_loadavg():
    try:
        return "%.2f %.2f %.2f" % os.getloadavg()
    except Exception:
        return "?"


def get_hostname():
    try:
        return socket.gethostname()
    except OSError:
        return "?"


def num(s):
    """'217.0 Volts' → 217.0 (없으면 None)."""
    return upm.leading_float(s)


def fmt_dur(sec):
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m"


def fmt_kwh(v):
    return f"{v:.3f}" if v < 10 else f"{v:.2f}" if v < 100 else f"{v:.1f}"


# ------------------------------------------------------------------ 화면

class Tui:
    def __init__(self, stdscr, args):
        self.scr = stdscr
        self.args = args
        self.ascii = args.ascii
        mode = getattr(args, "graph", "auto")
        if mode == "auto":
            # fbcon 기본 폰트에 1/8 블록·반칸이 없으므로 콘솔에서는 음영을 쓴다
            mode = "ascii" if self.ascii else (
                "shade" if os.environ.get("TERM") == "linux" else "block")
        self.ladder = GRAPH_LADDERS.get(mode, GRAPH_LADDERS["shade"])
        self.levels = len(self.ladder)
        self.full = self.ladder[-1]
        self.shade = "." if self.ascii else "░"    # ░ (막대 배경)
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
                  ("ok", "warn", "bad", "head", "dim", "bar", "hl", "tab",
                   "val", "badge_ok", "badge_warn", "badge_bad")}
        for k in ("badge_ok", "badge_warn", "badge_bad"):
            self.c[k] = curses.A_REVERSE
        self.c["tab"] = curses.A_REVERSE
        self.c["head"] = curses.A_BOLD
        self.c["val"] = curses.A_BOLD       # 흑백일 땐 굵기로만 값을 구분
        if self.args.mono or not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        # 검은 바탕 기준으로 고른 색. 리눅스 콘솔 기본 팔레트에서 일반색은
        # #AA.., 굵게(bright)는 #FF.. 가 된다. 괄호 안은 검정 대비 명암비.
        #   흰색 #AAAAAA 9.0:1 / 밝은흰색 #FFFFFF 21:1 / 시안 #00AAAA 7.3:1
        #   초록 #00AA00 6.8:1 / 밝은노랑 #FFFF55 17:1 / 밝은빨강 #FF5555 6.7:1
        # 파랑 #0000AA 는 1.6:1 이라 본문에 쓰지 않는다.
        pairs = {
            "ok": curses.COLOR_GREEN, "warn": curses.COLOR_YELLOW,
            "bad": curses.COLOR_RED, "head": curses.COLOR_CYAN,
            "dim": curses.COLOR_WHITE, "bar": curses.COLOR_GREEN,
            "hl": curses.COLOR_YELLOW, "val": curses.COLOR_WHITE,
        }
        for i, (k, fg) in enumerate(pairs.items(), start=1):
            curses.init_pair(i, fg, bg)
            self.c[k] = curses.color_pair(i)
        # 계층은 파랑/흰색이 아니라 '일반 흰색(라벨) vs 밝은 흰색(값)'으로 준다
        for k in ("head", "ok", "warn", "bad", "hl", "val"):
            self.c[k] |= curses.A_BOLD
        # 역상 배지는 굵게를 쓰지 않는다 — 굵게면 검정이 #555555가 되어
        # 시안/초록 바탕 대비가 2.5:1까지 떨어진다. 순수 검정이어야 7:1이 나온다.
        n = len(pairs)
        badges = {
            "tab": (curses.COLOR_BLACK, curses.COLOR_CYAN),      # 7.3:1
            "badge_ok": (curses.COLOR_BLACK, curses.COLOR_GREEN),   # 6.8:1
            "badge_warn": (curses.COLOR_BLACK, curses.COLOR_YELLOW),  # 4.0:1
            # 빨강 바탕엔 검정(2.7:1)보다 밝은 흰색(7.8:1)이 낫다
            "badge_bad": (curses.COLOR_WHITE, curses.COLOR_RED),
        }
        for j, (k, (fg, bgc)) in enumerate(badges.items(), start=n + 1):
            curses.init_pair(j, fg, bgc)
            self.c[k] = curses.color_pair(j)
        self.c["badge_bad"] |= curses.A_BOLD

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

    def hbar(self, y, x, width, frac, attr, rows=1):
        frac = max(0.0, min(1.0, frac or 0.0))
        n = int(round(frac * width))
        for r in range(max(1, rows)):
            self.put(y + r, x, self.full * n, attr)
            self.put(y + r, x + n, self.shade * (width - n), self.c["dim"])

    def big(self, y, x, text, attr, font=None):
        """큰 글씨. 그린 폭을 반환."""
        font = BIG_FONT if font is None else font
        cx = x
        for ch in text:
            glyph = font.get(ch)
            if not glyph:
                continue
            for r, row in enumerate(glyph):
                self.put(y + r, cx, row.replace("#", self.full), attr)
            cx += len(glyph[0]) + 1
        return cx - x

    def vbars(self, y, x, h, w, values, labels, highlight=None, unit="",
              relative=False):
        """세로 막대그래프. 한 칸을 self.levels 단계로 쪼갠다.

        labels: {index: 문자열} 하단 표시.
        """
        nums = [v for v in values if v is not None]
        if not nums or h < 2 or w < 10:
            self.put(y + h // 2, x + 2, "(no data)", self.c["dim"])
            return
        vmax = max(nums) or 1.0
        vmin = min(nums)
        if relative:
            # 상대 스케일 — 0을 버리고 실측 최소~최대에 여유만 둬서 변동폭을 키운다
            span = vmax - vmin
            pad = max(span * 0.15, abs(vmax) * 0.02, 0.5)
            lo, hi = vmin - pad, vmax + pad
            if vmin >= 0:                # 전력·전압은 음수가 없다
                lo = max(0.0, lo)
            lo, hi = nice_range(lo, hi)  # 축을 고정해 전체 재계산을 막는다
        else:
            # 변동이 작으면(최소 > 최대의 절반) 0 대신 최소 근처를 바닥으로
            lo = 0.0 if vmin < vmax * 0.5 else vmin - (vmax - vmin) * 0.25
            hi = vmax
        if hi - lo < 1e-9:              # 값이 모두 같을 때 — 0 바닥으로 되돌린다
            lo = 0.0
            if hi <= lo:                # 전부 0이면 눈금만 세운다
                hi = 1.0
        fmt = (lambda v: f"{v:.0f}") if hi >= 10 else (lambda v: f"{v:.2f}")
        top, bottom = fmt(hi) + unit, fmt(lo)
        aw = max(len(top), len(bottom), AXIS_MIN_W) + 1
        self.put(y, x, top.rjust(aw - 1), self.c["dim"])
        self.put(y + h - 1, x, bottom.rjust(aw - 1),
                 self.c["warn"] if lo else self.c["dim"])
        avail = w - aw
        n = len(values)
        step = max(1, avail // n)
        bw = max(1, step - 1) if step > 1 else 1
        values = values[-(avail // step):]
        off = n - len(values)
        m = max(1, len(values))
        spread = step == 1 and avail > m
        for i, v in enumerate(values):
            if v is None:
                continue
            lv = self.levels
            units = max(1 if v > 0 else 0,
                        int(round((v - lo) / (hi - lo) * h * lv)))
            attr = self.c["hl"] if (i + off) == highlight else self.c["bar"]
            bx = x + aw + ((i * avail) // m if spread else i * step)
            for r in range(h):                    # 아래 칸부터 위로
                u = units - lv * r
                if u <= 0:
                    break
                self.put(y + h - 1 - r, bx, self.ladder[min(u, lv) - 1] * bw, attr)
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
            ts, loadpct, watts, linev = self.db.latest[:4]
            fields = {"LINEV": f"{linev} Volts"} if linev is not None else {}
            return (None, fields, watts, loadpct, "db")
        return (None, {}, None, None, None)

    def badge_attr(self, status):
        """헤더 상태 배지 — 바탕색이 상태를 나타내므로 글자는 검정/흰색."""
        if not status:
            return self.c["badge_warn"]
        st = status.strip()
        if "ONBATT" in st or "LOWBATT" in st or "COMMLOST" in st:
            return self.c["badge_bad"]
        return self.c["badge_ok"] if st == "ONLINE" else self.c["badge_warn"]

    def status_attr(self, status):
        if not status:
            return self.c["warn"]
        if "ONBATT" in status or "LOWBATT" in status or "COMMLOST" in status:
            return self.c["bad"]
        if "ONLINE" in status and status.strip() == "ONLINE":
            return self.c["ok"]
        return self.c["warn"]

    def header(self, w):
        status, _, _, _, _ = self.ups_state()
        clock = datetime.now().strftime("%m-%d %H:%M:%S" if w >= 44 else "%H:%M:%S")
        st = (status or "N/A").strip()
        self.put(0, 0, " " * w, self.c["tab"])
        self.put(0, 1, "UPS", self.c["tab"])
        self.put(0, 5, f" {st} ", self.badge_attr(status))
        self.put(0, w - len(clock) - 1, clock, self.c["tab"])

    def footer(self, h, w):
        x = 0
        for i, name in enumerate(PAGES):
            label = f" {i + 1}{name}" if w >= 40 else f" {name[:3]}"
            if i == self.page:
                label += " "
            attr = self.c["tab"] if i == self.page else self.c["dim"]
            self.put(h - 1, x, label, attr)
            x += len(label)
        warn = self.warning()
        if warn and x + 2 < w:
            self.put(h - 1, x + 1, warn[: w - x - 2], self.c["bad"])

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
        bch = num(f.get("BCHARGE"))
        tleft = num(f.get("TIMELEFT"))
        y = y0
        model = f.get("MODEL", "").strip()
        if model:
            self.put(y, 1, model[: w - 2], self.c["dim"])
            y += 1

        wtxt = f"{watts:.1f}" if watts is not None else "--"
        kwh_shown = info_shown = False
        if h >= 16 and w >= 36:
            # 세로 여유가 있으면 7행 글리프, 아니면 기존 5행
            large = h >= 20
            font, gh = (BIG_FONT_L, 7) if large else (BIG_FONT, 5)
            gw = self.big(y + 1, 2, wtxt, self.c["head"], font)
            self.put(y + gh, 3 + gw, "W", self.c["head"])
            rx = 6 + gw
            if large and w - rx >= 46:
                self._now_panel(y + 1, rx, w - rx - 1, gh,
                                f, s, status, bch, tleft)
                kwh_shown = info_shown = True
            elif w - rx >= 16:
                self.put(y + 1, rx, "TODAY", self.c["dim"])
                self.put(y + 2, rx, f"{fmt_kwh(s['today_kwh'])} kWh", self.c["val"])
                self.put(y + 3, rx, f"MONTH {s['month'][5:]}", self.c["dim"])
                self.put(y + 4, rx, f"{fmt_kwh(s['month_kwh'])} kWh", self.c["val"])
                kwh_shown = True
            y += gh + 2
        else:
            self.put(y, 1, "POWER", self.c["dim"])
            self.put(y, 8, f"{wtxt} W", self.c["head"])
            y += 1

        bw = max(8, w - 20)
        rows = [
            ("LOAD", loadpct, f"{loadpct:5.1f}%" if loadpct is not None else "  --",
             self.c["bar"] if (loadpct or 0) < 80 else self.c["bad"]),
            ("BATT", bch, f"{bch:5.0f}%" if bch is not None else "  --",
             self.c["ok"] if (bch or 0) >= 50 else self.c["warn"] if (bch or 0) >= 20
             else self.c["bad"]),
        ]
        # 남은 행을 보고 막대 두께 결정 — 차트 몫(라벨 1 + 최소 2행)은 남긴다
        need = len(rows) + (0 if info_shown else 1) + (0 if kwh_shown else 1)
        bar_rows = 2 if (y0 + h - y) - need - 3 >= len(rows) else 1
        for label, val, txt, attr in rows:
            if y >= y0 + h:
                break
            self.put(y, 1, label, self.c["dim"])
            self.hbar(y, 6, bw, (val or 0) / 100.0, attr, bar_rows)
            self.put(y, 7 + bw, txt, self.c["val"])
            y += bar_rows

        if not info_shown:
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
                           f"MONTH {fmt_kwh(s['month_kwh'])}kWh", self.c["val"])
            y += 1

        # 남은 공간: 최근 1시간 전력 추이 (본문 마지막 행까지 사용)
        avail = y0 + h - y
        if avail >= 3 and self.db.recent:
            self.put(y, 1, "LAST 60 MIN (W)", self.c["dim"])
            cw = w - 2
            # 축 폭이 AXIS_MIN_W + 1로 고정이라 버킷 수도 고정이다
            vals = self._bucket_recent(cw - AXIS_MIN_W - 1)
            self.vbars(y + 1, 1, avail - 1, cw, vals, {},
                       highlight=len(vals) - 1, relative=True)

    def _now_panel(self, y, x, w, h, f, s, status, bch, tleft):
        """큰 숫자 오른쪽 여백에 상세 지표를 2단으로 배치."""
        hr = self.db.hourly
        d_max = max((r[3] for r in hr), default=None)
        d_min = min((r[2] for r in hr), default=None)
        d_kwh = sum(r[4] for r in hr) if hr else None
        nom = num(f.get("NOMPOWER"))
        linev = num(f.get("LINEV"))
        battv = num(f.get("BATTV"))
        col_a = [                                  # 에너지
            ("TODAY", f"{fmt_kwh(s['today_kwh'])} kWh"),
            (f"MONTH {s['month'][5:]}", f"{fmt_kwh(s['month_kwh'])} kWh"),
            ("24H", f"{fmt_kwh(d_kwh)} kWh" if d_kwh is not None else "--"),
            ("", ""),
            ("AC", f"{linev:.0f} V" if linev is not None else "--"),
            ("BATT", f"{battv:.1f} V" if battv is not None else "--"),
            ("RUNTIME", f"{tleft:.0f} min" if tleft is not None else "--"),
        ]
        col_b = [                                  # 전력 / 상태
            ("24H MAX", f"{d_max:.0f} W" if d_max is not None else "--"),
            ("24H MIN", f"{d_min:.0f} W" if d_min is not None else "--"),
            ("NOMPOWER", f"{nom:.0f} W" if nom is not None else "--"),
            ("", ""),
            ("CHARGE", f"{bch:.0f} %" if bch is not None else "--"),
            ("XFERS", str(f.get("NUMXFERS", "--")).strip() or "--"),
            ("STATUS", (status or "N/A").strip()),
        ]
        cw = w // 2
        if cw < 14:
            return
        for i in range(h):
            for col, cx in ((col_a, x), (col_b, x + cw)):
                if i >= len(col):
                    continue
                k, v = col[i]
                if not k:
                    continue
                self.put(y + i, cx, k[: cw - 2], self.c["dim"])
                vx = max(cx + len(k) + 1, cx + cw - 2 - len(v))
                self.put(y + i, vx, v[: cw - 2], self.c["val"])

    def _bucket_recent(self, cols):
        """최근 1시간 샘플을 cols개 구간 평균으로.

        구간 경계를 버킷 폭에 맞춰 양자화한다. 호출마다 time.time()을
        그대로 쓰면 창이 매 프레임(250ms) 조금씩 밀려서, 경계에 걸린
        샘플이 인접 버킷을 왕복하며 막대가 떨린다.
        """
        cols = max(1, cols)
        bsec = RECENT_SPAN_SEC / cols
        # 경계로 내림하되 진행 중인 버킷까지 포함한다 — 내림만 하면
        # 최신 최대 bsec초(약 49초)치가 창 밖으로 빠진다.
        end = (time.time() // bsec + 1) * bsec
        start = end - RECENT_SPAN_SEC
        sums = [0.0] * cols
        cnt = [0] * cols
        for ts, wv in self.db.recent:
            i = int((ts - start) / bsec)
            if 0 <= i < cols:
                sums[i] += wv
                cnt[i] += 1
        out, last = [], None
        for s_, c_ in zip(sums, cnt):
            if c_:
                last = s_ / c_
            out.append(last)          # 첫 샘플 이전 구간은 None(빈 칸)
        return out

    # ---- 페이지 2: 시간별
    def page_hour(self, y0, h, w):
        rows = self.db.hourly
        total = sum(r[4] for r in rows)
        self.put(y0, 1, "HOURLY AVG W (24h)", self.c["head"])
        self.put(y0, w - 16, f"{fmt_kwh(total):>8} kWh", self.c["val"])
        vals = [r[1] for r in rows]
        labels = {}
        for i, r in enumerate(rows):
            hr = datetime.fromtimestamp(r[0]).hour
            if hr % 6 == 0:
                labels[i] = f"{hr:02d}"
        self.vbars(y0 + 1, 1, h - 3, w - 2, vals, labels,
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
        self.put(y0, w - 16, f"{fmt_kwh(total):>8} kWh", self.c["val"])
        vals = [r[4] for r in rows]
        labels = {}
        for i, r in enumerate(rows):
            d = int(r[0][8:10])
            if d in (1, 10, 20):
                labels[i] = str(d)
        self.vbars(y0 + 1, 1, h - 3, w - 2, vals, labels, highlight=len(vals) - 1)
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
                     self.c["hl"] if cur else self.c["val"])

    # ---- 페이지 5: 공급전원(입력 전압)
    def page_volt(self, y0, h, w):
        vs = self.db.volt_summary or {}
        rows = self.db.volt_hourly
        self.put(y0, 1, "LINE VOLTAGE (24h)", self.c["head"])
        low, high, nom = vs.get("low_v"), vs.get("high_v"), vs.get("nominal_v")
        if low and high:
            band = f"OK {low:.0f}-{high:.0f}V"
            self.put(y0, w - len(band) - 1, band, self.c["dim"])
        y = y0 + 1
        if vs.get("n"):
            out_attr = self.c["bad"] if vs["n_out"] else self.c["ok"]
            self.put(y, 1, f"avg {vs['avg_v']:.1f}  min {vs['min_v']:.0f}  "
                           f"max {vs['max_v']:.0f}  sd {vs['sd_v']:.1f}"[: w - 2],
                     self.c["val"])
            y += 1
            self.put(y, 1, f"out of range {vs['out_pct']:.2f}%", out_attr)
            ev = vs.get("events", {})
            cnt = "  ".join(f"{EVENT_NAMES.get(k, k)} {v}" for k, v in sorted(ev.items()))
            if cnt:
                self.put(y, 24, cnt[: max(0, w - 25)], self.c["warn"])
            y += 1
        # 이벤트 목록 몫을 남기고 나머지를 시간별 최소~최대 밴드 차트로
        n_ev = min(len(self.db.events), max(0, (y0 + h - y) // 3))
        ch = y0 + h - y - n_ev - 2
        if ch >= 3 and rows:
            self.vrange(y, 1, ch, w - 2, rows, low, high)
            y += ch + 1
        elif not rows:
            self.put(y + 1, 2, "(no voltage data yet)", self.c["dim"])
            y += 3
        if n_ev and y < y0 + h:
            self.put(y, 1, "EVENTS (30d)", self.c["dim"])
            y += 1
            for e in self.db.events[: y0 + h - y]:
                when = datetime.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M")
                dur = "ongoing" if e["ongoing"] else fmt_dur(e["duration"])
                v = f"{e['linev']:.0f}V" if e.get("linev") else ""
                attr = self.c["bad"] if e["ongoing"] else 0
                self.put(y, 1, f"{when} {EVENT_NAMES.get(e['kind'], e['kind']):<8} "
                               f"{dur:>7} {v}"[: w - 2], attr)
                y += 1

    def vrange(self, y, x, h, w, rows, low, high):
        """시간별 최소~최대 전압 세로 밴드. 평균은 강조, 범위 이탈은 빨강."""
        vmin = min(r["min_v"] for r in rows)
        vmax = max(r["max_v"] for r in rows)
        lo, hi = vmin - 2, vmax + 2
        if hi - lo < 10:                     # 변동이 작아도 최소 10V 폭
            mid = (hi + lo) / 2
            lo, hi = mid - 5, mid + 5
        aw = 5
        self.put(y, x, f"{hi:4.0f}", self.c["dim"])
        self.put(y + h - 1, x, f"{lo:4.0f}", self.c["dim"])

        def row_of(v):
            return y + h - 1 - int(round((v - lo) / (hi - lo) * (h - 1)))
        step = max(1, (w - aw) // len(rows))
        rows = rows[-((w - aw) // step):]
        for i, r in enumerate(rows):
            bx = x + aw + i * step
            bad = (low and r["min_v"] < low) or (high and r["max_v"] > high)
            attr = self.c["bad"] if bad else self.c["bar"]
            for yy in range(row_of(r["max_v"]), row_of(r["min_v"]) + 1):
                self.put(yy, bx, self.shade if not self.ascii else "|", attr)
            self.put(row_of(r["avg_v"]), bx, self.full, attr | curses.A_BOLD)
            hr = datetime.fromtimestamp(r["t"]).hour
            if hr % 6 == 0:
                self.put(y + h, bx, f"{hr:02d}", self.c["dim"])

    # ---- 페이지 6: 시스템
    def page_sys(self, y0, h, w):
        status, f, *_ = self.ups_state()
        latest = self.db.latest
        age = f"{int(time.time() - latest[0])}s ago" if latest else "none"
        mem_pct, mem_mb = get_mem()
        try:
            du = shutil.disk_usage(os.path.dirname(os.path.abspath(self.args.db)))
            disk = f"{du.free / 2**30:.1f} GB free"
        except Exception:
            disk = "?"
        host_items = [
            ("HOST", get_hostname()),
            ("IP", upm.get_ip()),
            ("CPU", f"{upm.get_cpu_temp()} C"),
            ("MEM", f"{mem_pct:.0f}% of {mem_mb:.0f} MB"
                    if mem_pct is not None else "?"),
            ("LOAD", get_loadavg()),
            ("UPTIME", get_uptime()),
            ("DISK", disk),
            ("CONSOLE", f"{w}x{h + 3}"),
        ]
        ups_items = [
            ("MODEL", f.get("MODEL", "?").strip()),
            ("NOMPWR", f.get("NOMPOWER", "?")),
            ("STATUS", (status or "N/A").strip()),
            ("UPSHOST", f.get("HOSTNAME", "?").strip()),
            ("SAMPLE", age),
            ("DB", f"{self.db.db_size / 1048576:.1f} MB"),
            ("XFERS", f.get("NUMXFERS", "?")),
            ("LASTXF", f.get("LASTXFER", "?")),
            ("ONBATT", f.get("XONBATT", "-")[:16]),
            ("TOUCH", self.touch.path if self.touch and not self.touch.error
             else (self.touch.error[:30] if self.touch else "off")),
        ]
        # 폭이 넉넉하면 호스트/UPS를 2단으로 — 좁으면 기존처럼 한 줄씩
        if w >= 64 and h >= len(host_items) + 2:
            cw = w // 2
            self.put(y0, 1, "HOST", self.c["head"])
            self.put(y0, 1 + cw, "UPS", self.c["head"])
            for col, cx in ((host_items, 1), (ups_items, 1 + cw)):
                for i, (k, v) in enumerate(col[: h - 2]):
                    self.put(y0 + 2 + i, cx, f"{k:<8}", self.c["dim"])
                    self.put(y0 + 2 + i, cx + 9, str(v)[: cw - 11])
        else:
            for i, (k, v) in enumerate((host_items + ups_items)[:h]):
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
                self.page_month, self.page_volt, self.page_sys)[self.page]
        body(2, h - 3, w)
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
