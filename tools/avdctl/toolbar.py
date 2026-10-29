#!/usr/bin/env python3
"""RanchuWoA 侧边工具栏：仿官方 Android 模拟器的工具栏，贴在 QEMU 窗口旁边并跟着移动。

avdctl start（不带 --headless / --no-toolbar）会自动拉起；单独打开：avdctl toolbar <名字>。
- 电源 / 音量：virtio 键盘按键；返回 / 主屏幕 / 最近任务：virtio 触摸屏上的按键（与鼠标右键 / 中键 / 侧键
  同一条路）。按住按钮 = 按住按键（长按电源 = 电源菜单）。
- 关闭：给 QEMU 窗口发 WM_CLOSE，与点窗口的关闭按钮完全一样（先让 Android 正常关机）。
- 截图：QMP screendump → PNG，存到“图片\\屏幕截图”。
- 更多：位置（GPS）、窗口大小、屏幕常亮、帮助。
走 QEMU 上工具栏专用的 QMP 端口（ports_of()["qmp_ui"]），不占 avdctl 用的那个。
工具栏归 QEMU 窗口所有（owner）：总在它上面、随它最小化；不抢焦点，点按钮后键盘输入仍进模拟器。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import uuid
import winreg
from ctypes import wintypes
from pathlib import Path
from tkinter import font as tkfont, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import avdctl  # noqa: E402

# ---------------------------------------------------------------- Win32

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
shell32 = ctypes.WinDLL("shell32")
ole32 = ctypes.WinDLL("ole32")

HWND, UINT, DWORD, BOOL = wintypes.HWND, wintypes.UINT, wintypes.DWORD, wintypes.BOOL
LONG_PTR = ctypes.c_ssize_t
WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", DWORD)]


def _proto(fn, res, *args) -> None:
    fn.restype, fn.argtypes = res, args


_proto(user32.SetProcessDpiAwarenessContext, BOOL, ctypes.c_void_p)
_proto(user32.EnumWindows, BOOL, WNDENUMPROC, wintypes.LPARAM)
_proto(user32.GetWindowThreadProcessId, DWORD, HWND, ctypes.POINTER(DWORD))
_proto(user32.IsWindow, BOOL, HWND)
_proto(user32.IsWindowVisible, BOOL, HWND)
_proto(user32.IsIconic, BOOL, HWND)
_proto(user32.GetClassNameW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_proto(user32.GetWindowRect, BOOL, HWND, ctypes.POINTER(wintypes.RECT))
_proto(user32.GetClientRect, BOOL, HWND, ctypes.POINTER(wintypes.RECT))
_proto(user32.GetDpiForWindow, UINT, HWND)
_proto(user32.GetDpiForSystem, UINT)
_proto(user32.MonitorFromWindow, wintypes.HMONITOR, HWND, DWORD)
_proto(user32.GetMonitorInfoW, BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFO))
_proto(user32.GetWindowLongPtrW, LONG_PTR, HWND, ctypes.c_int)
_proto(user32.SetWindowLongPtrW, LONG_PTR, HWND, ctypes.c_int, LONG_PTR)
_proto(user32.GetAncestor, HWND, HWND, UINT)
_proto(user32.PostMessageW, BOOL, HWND, UINT, wintypes.WPARAM, wintypes.LPARAM)
_proto(user32.ShowWindowAsync, BOOL, HWND, ctypes.c_int)
_proto(user32.SetWindowPos, BOOL, HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       ctypes.c_int, UINT)
_proto(user32.AdjustWindowRectExForDpi, BOOL, ctypes.POINTER(wintypes.RECT), DWORD, BOOL, DWORD,
       UINT)
_proto(user32.GetForegroundWindow, HWND)
_proto(user32.SetForegroundWindow, BOOL, HWND)
_proto(dwmapi.DwmGetWindowAttribute, ctypes.c_long, HWND, DWORD, ctypes.c_void_p, DWORD)
_proto(dwmapi.DwmSetWindowAttribute, ctypes.c_long, HWND, DWORD, ctypes.c_void_p, DWORD)
_proto(kernel32.CreateMutexW, wintypes.HANDLE, ctypes.c_void_p, BOOL, wintypes.LPCWSTR)
_proto(shell32.SHGetKnownFolderPath, ctypes.c_long, ctypes.c_void_p, DWORD, wintypes.HANDLE,
       ctypes.POINTER(ctypes.c_void_p))
_proto(ole32.CoTaskMemFree, None, ctypes.c_void_p)

GWL_STYLE, GWL_EXSTYLE, GWLP_HWNDPARENT = -16, -20, -8
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
WM_CLOSE, SW_MINIMIZE, GA_ROOT, MONITOR_DEFAULTTONEAREST = 0x10, 6, 2, 2
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_ASYNCWINDOWPOS = 0x1, 0x4, 0x10, 0x4000
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND = 9, 33, 2
CREATE_NO_WINDOW, CREATE_NEW_CONSOLE = 0x08000000, 0x10


def _rect(r: wintypes.RECT) -> tuple[int, int, int, int]:
    return r.left, r.top, r.right, r.bottom


def window_rect(h: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    return _rect(r)


def frame_rect(h: int) -> tuple[int, int, int, int]:
    """窗口看得见的边界（Win10 起 GetWindowRect 还含几像素看不见的缩放边框）。"""
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r),
                                    ctypes.sizeof(r)) == 0:
        return _rect(r)
    return window_rect(h)


def client_size(h: int) -> tuple[int, int]:
    r = wintypes.RECT()
    user32.GetClientRect(h, ctypes.byref(r))
    return r.right, r.bottom


def monitor_rects(h: int) -> tuple[tuple, tuple]:
    """(整个显示器, 工作区)。"""
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST), ctypes.byref(mi))
    return _rect(mi.rcMonitor), _rect(mi.rcWork)


def find_qemu_window(pid: int) -> int | None:
    """QEMU 进程里最大的可见 SDL 窗口（SDL 还会给非图形控制台建隐藏窗口）。"""
    best, best_area = None, 0

    @WNDENUMPROC
    def cb(h, _):
        nonlocal best, best_area
        p = DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(h):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(h, cls, 64)
            l, t, r, b = window_rect(h)
            if cls.value == "SDL_app" and (r - l) * (b - t) > best_area:
                best, best_area = h, (r - l) * (b - t)
        return True

    user32.EnumWindows(cb, 0)
    return best


def top_hwnd(widget) -> int:
    return user32.GetAncestor(widget.winfo_id(), GA_ROOT)


def make_tool_window(widget, owner: int) -> int:
    """不抢焦点（点了键盘输入仍进模拟器）、归 QEMU 窗口所有（总在它上面、随它最小化）、Win11 圆角。"""
    h = top_hwnd(widget)
    ex = user32.GetWindowLongPtrW(h, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(h, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    user32.SetWindowLongPtrW(h, GWLP_HWNDPARENT, owner)
    pref = ctypes.c_int(DWMWCP_ROUND)
    dwmapi.DwmSetWindowAttribute(h, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), 4)
    return h


def known_folder(guid: str) -> Path | None:
    g = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(guid).bytes_le)
    p = ctypes.c_void_p()
    if shell32.SHGetKnownFolderPath(ctypes.byref(g), 0x8000, None, ctypes.byref(p)) != 0:  # KF_FLAG_CREATE
        return None
    try:
        return Path(ctypes.wstring_at(p.value))
    finally:
        ole32.CoTaskMemFree(p)


def screenshot_dir() -> Path:
    for g in ("b7bede81-df94-4682-a7d8-57a52620b86f",      # FOLDERID_Screenshots（图片\屏幕截图）
              "33e28130-4e1e-4676-835a-98395c3bc3bb"):     # FOLDERID_Pictures
        p = known_folder(g)
        if p and p.is_dir():
            return p
    return Path.home()


def light_theme() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "AppsUseLightTheme")[0])
    except OSError:
        return True


_mutex = None


def single_instance(name: str) -> bool:
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, f"Local\\RanchuWoA.toolbar.{name}")
    return bool(_mutex) and ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


# ---------------------------------------------------------------- QMP

class Qmp:
    """工具栏专用 QMP 端口上的长连接：后台线程串行执行命令，回调经 ui_calls 队列交回 Tk 线程。"""

    def __init__(self, port: int, ui_calls: queue.Queue):
        self.port = port
        self.ui_calls = ui_calls
        self.jobs: queue.Queue = queue.Queue()
        self.f = None
        threading.Thread(target=self._run, daemon=True).start()

    def call(self, execute: str, done=None, **arguments) -> None:
        self.jobs.put((execute, arguments, done))

    def _connect(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        f = s.makefile("rwb")
        f.readline()                                        # 问候语
        self._exec(f, {"execute": "qmp_capabilities"})
        return f

    @staticmethod
    def _exec(f, msg: dict) -> dict:
        f.write(json.dumps(msg).encode() + b"\n")
        f.flush()
        while True:
            line = f.readline()
            if not line:
                raise ConnectionError("QMP 连接已断开")
            r = json.loads(line)
            if "event" not in r:                            # 跳过异步事件
                return r

    def _run(self) -> None:
        while True:
            execute, arguments, done = self.jobs.get()
            msg = {"execute": execute, **({"arguments": arguments} if arguments else {})}
            r = None
            for _ in range(2):                              # 旧连接失效（QEMU 重启过）时重连一次
                fresh = self.f is None
                try:
                    if fresh:
                        self.f = self._connect()
                    r = self._exec(self.f, msg)
                    break
                except (OSError, ValueError) as e:
                    self.f = None
                    r = {"error": {"desc": f"连不上 QEMU（QMP 端口 {self.port}）：{e}"}}
                    if fresh:
                        break
            if done:
                self.ui_calls.put(lambda done=done, r=r: done(r))


# ---------------------------------------------------------------- 外观

THEMES = {
    "light": {"bg": "#f3f3f3", "hover": "#e0e0e0", "press": "#cccccc", "fg": "#1b1b1b",
              "sep": "#d6d6d6", "tip_bg": "#ffffff", "tip_fg": "#1b1b1b", "border": "#c8c8c8"},
    "dark": {"bg": "#2b2b2b", "hover": "#3d3d3d", "press": "#4a4a4a", "fg": "#f2f2f2",
             "sep": "#454545", "tip_bg": "#3b3b3b", "tip_fg": "#f2f2f2", "border": "#505050"},
}
CLOSE_HOVER = "#c42b1c"

# (键, 图标, 提示)；None = 分隔线。图标是 Segoe Fluent Icons 字形，back/home/recents 画 Android 导航栏的形状
SPEC = [
    ("close", "\ue8bb", "关闭（让 Android 正常关机；再点一次强制退出）"),
    ("minimize", "\ue921", "最小化"),
    None,
    ("power", "\ue7e8", "电源（按住 = 电源菜单）  Ctrl+Alt+P"),
    ("volup", "\ue995", "音量 +"),
    ("voldown", "\ue993", "音量 −"),
    ("shot", "\ue722", "截图（保存到“图片\\屏幕截图”）"),
    None,
    ("back", "back", "返回  （鼠标右键）"),
    ("home", "home", "主屏幕  （鼠标中键）"),
    ("recents", "recents", "最近任务  （鼠标侧键“前进”）"),
    None,
    ("more", "\ue712", "更多：位置、窗口大小、屏幕常亮、帮助"),
]
# 按键类按钮：按下/松开分别发 QMP input-send-event
KEYS = {"power": ("key", "power"), "volup": ("key", "volumeup"), "voldown": ("key", "volumedown"),
        "back": ("btn", "right"), "home": ("btn", "middle"), "recents": ("btn", "extra")}
MIN_HOLD = 0.1          # 秒。点得太快时延后松开：ColorOS 会把几毫秒的按下/松开当抖动忽略

PRESETS = [
    ("Google 总部（山景城）", 37.4220, -122.0841, 5),
    ("北京 · 天安门", 39.9087, 116.3975, 44),
    ("上海 · 外滩", 31.2400, 121.4900, 4),
    ("广州 · 广州塔", 23.1066, 113.3245, 10),
    ("深圳 · 市民中心", 22.5431, 114.0579, 20),
    ("杭州 · 西湖", 30.2592, 120.1300, 10),
    ("成都 · 天府广场", 30.6570, 104.0657, 500),
    ("香港 · 维多利亚港", 22.2930, 114.1694, 5),
    ("台北 · 101 大楼", 25.0340, 121.5645, 10),
    ("东京 · 东京站", 35.6812, 139.7671, 5),
    ("伦敦 · 特拉法加广场", 51.5080, -0.1281, 20),
    ("纽约 · 时代广场", 40.7580, -73.9855, 10),
]
DEFAULT_GPS = "37.4220;-122.0841;5"         # 与 ranchu-vsock 的默认值一致


def round_rect(c, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
           x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, **kw)


# ---------------------------------------------------------------- 浮窗（提示 / 通知）

class Popup:
    """不抢焦点的小浮窗，出现在工具栏外侧（按钮提示、截图通知）。"""

    def __init__(self, bar: "Toolbar"):
        self.bar = bar
        self.win = None
        self.hide_job = None
        self.on_click = None

    def show(self, text: str, y: int, timeout_ms: int | None = None, on_click=None) -> None:
        bar, t = self.bar, self.bar.theme
        if self.win is None:
            self.win = tk.Toplevel(bar.root)
            self.win.withdraw()
            self.win.overrideredirect(True)
            self.label = tk.Label(self.win, justify="left", bd=0, highlightthickness=1)
            self.label.pack()
            self.label.bind("<ButtonRelease-1>", self._click)
            self.hwnd = None
        pad = round(8 * bar.s)
        self.label.config(text=text, font=bar.text_font, bg=t["tip_bg"], fg=t["tip_fg"],
                          padx=pad, pady=round(pad * 0.6), highlightbackground=t["border"],
                          cursor="hand2" if on_click else "")
        self.on_click = on_click
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        self.win.geometry(f"+{bar.beside(w)}+{y - h // 2}")
        self.win.deiconify()
        if self.hwnd is None and bar.qwin:
            self.win.update_idletasks()
            self.hwnd = make_tool_window(self.win, bar.qwin)
        self.win.lift()
        if self.hide_job:
            bar.root.after_cancel(self.hide_job)
            self.hide_job = None
        if timeout_ms:
            self.hide_job = bar.root.after(timeout_ms, self.hide)

    def hide(self) -> None:
        self.hide_job = None
        if self.win is not None:
            self.win.withdraw()

    def _click(self, _e) -> None:
        cb, self.on_click = self.on_click, None
        self.hide()
        if cb:
            cb()


# ---------------------------------------------------------------- 工具栏

class Toolbar:
    W, PAD, BTN, SEP, GAP = 44, 4, 36, 11, 6        # 96 DPI 下的像素

    def __init__(self, name: str, d: Path, cfg: dict, pid: int):
        self.name, self.d, self.cfg, self.pid = name, d, cfg, pid
        self.ports = avdctl.ports_of(cfg)
        self.serial = f"emulator-{self.ports['adb'] - 1}"
        self.root = tk.Tk(className="RanchuWoA")
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.title(f"{name} - 工具栏")
        self.root.report_callback_exception = lambda *exc: traceback.print_exception(*exc)
        icon = avdctl.ROOT / "qemu-ranchu" / "pc-bios" / "qemu-nsis.ico"   # 与 QEMU 窗口同一个图标
        if icon.exists():
            self.root.iconbitmap(default=str(icon))
        self.theme = THEMES["light" if light_theme() else "dark"]
        fams = set(tkfont.families(self.root))
        self.icon_family = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams),
                                None)
        self.calls: queue.Queue = queue.Queue()
        self.qmp = Qmp(self.ports["qmp_ui"], self.calls)
        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        for ev, fn in (("<Motion>", self._motion), ("<Leave>", self._leave),
                       ("<ButtonPress-1>", self._press), ("<ButtonRelease-1>", self._release)):
            self.canvas.bind(ev, fn)
        self.qwin = None                    # QEMU 的 SDL 窗口
        self.hwnd = None                    # 工具栏自己的顶层窗口
        self.dpi = 0
        self.s = 1.0
        self.items: dict[str, tuple] = {}
        self.hover = self.pressed = None
        self.held: dict[str, float] = {}
        self.x = self.y = 0
        self.w = self.h = 0
        self.side = "right"
        self.shown = False
        self.last_move = 0.0
        self.next_alive = 0.0
        self.tip = Popup(self)
        self.note = Popup(self)
        self.tip_job = None
        self.ext = None
        self.closing = False

    # ---------------------------------------------------------- 绘制

    def build(self) -> None:
        s, t, c = self.s, self.theme, self.canvas
        c.delete("all")
        self.icon_font = (self.icon_family or "Segoe UI Symbol", -round(16 * s))
        self.chrome_font = (self.icon_family or "Segoe UI Symbol", -round(10 * s))
        self.text_font = ("Microsoft YaHei UI", -round(12 * s))
        w, pad, bsz, sep = round(self.W * s), round(self.PAD * s), round(self.BTN * s), round(self.SEP * s)
        self.items = {}
        y = pad
        for spec in SPEC:
            if spec is None:
                c.create_line(round(11 * s), y + sep // 2, w - round(11 * s), y + sep // 2,
                              fill=t["sep"], width=max(1, round(s)))
                y += sep
                continue
            key, glyph, _ = spec
            bg = round_rect(c, pad, y, w - pad, y + bsz, round(5 * s), fill=t["bg"], outline="")
            self.items[key] = (y, y + bsz, bg, self._icon(glyph, key, w / 2, y + bsz / 2))
            y += bsz
        self.w, self.h = w, y + pad
        c.config(width=self.w, height=self.h, bg=t["bg"])
        self.root.config(bg=t["bg"])
        self.root.geometry(f"{self.w}x{self.h}")
        for key in self.items:
            self._paint(key)

    def _icon(self, glyph: str, key: str, cx: float, cy: float) -> list[int]:
        c, s, fg = self.canvas, self.s, self.theme["fg"]
        lw = max(1.0, 1.4 * s)
        r = 6.5 * s
        if glyph == "back":
            return [c.create_polygon(cx + r * 0.8, cy - r, cx + r * 0.8, cy + r, cx - r * 0.9, cy,
                                     outline=fg, fill="", width=lw, joinstyle="round")]
        if glyph == "home":
            return [c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=fg, width=lw)]
        if glyph == "recents":
            r = 6 * s
            return [c.create_rectangle(cx - r, cy - r, cx + r, cy + r, outline=fg, width=lw)]
        font = self.chrome_font if key in ("close", "minimize") else self.icon_font
        return [c.create_text(cx, cy, text=glyph, fill=fg, font=font)]

    def _paint(self, key: str) -> None:
        t = self.theme
        _, _, bg, fg = self.items[key]
        color, ink = t["bg"], t["fg"]
        if key == self.pressed:
            color = CLOSE_HOVER if key == "close" else t["press"]
        elif key == self.hover:
            color = CLOSE_HOVER if key == "close" else t["hover"]
        if key == "close" and color == CLOSE_HOVER:
            ink = "#ffffff"
        self.canvas.itemconfig(bg, fill=color)
        for i in fg:
            kind = self.canvas.type(i)
            self.canvas.itemconfig(i, **({"fill": ink} if kind == "text" else {"outline": ink}))

    def _hit(self, e) -> str | None:
        pad = round(self.PAD * self.s)
        if not pad <= e.x < self.w - pad:
            return None
        return next((k for k, (y0, y1, _, _) in self.items.items() if y0 <= e.y < y1), None)

    # ---------------------------------------------------------- 鼠标

    def _set_hover(self, key: str | None) -> None:
        if key == self.hover:
            return
        old, self.hover = self.hover, key
        for k in (old, key):
            if k:
                self._paint(k)
        if self.tip_job:
            self.root.after_cancel(self.tip_job)
            self.tip_job = None
        self.tip.hide()
        if key and not self.pressed:
            self.tip_job = self.root.after(500, self._show_tip)

    def _show_tip(self) -> None:
        self.tip_job = None
        if self.hover and not self.pressed:
            y0, y1, _, _ = self.items[self.hover]
            text = next(sp[2] for sp in SPEC if sp and sp[0] == self.hover)
            self.tip.show(text, self.y + (y0 + y1) // 2, timeout_ms=5000)

    def _motion(self, e) -> None:
        self._set_hover(self._hit(e))

    def _leave(self, _e) -> None:
        self._set_hover(None)

    def _press(self, e) -> None:
        key = self._hit(e)
        if not key:
            return
        self.pressed = key
        self.tip.hide()
        self._paint(key)
        if key in KEYS:
            self.key_down(key)

    def _release(self, e) -> None:
        key, self.pressed = self.pressed, None
        if not key:
            return
        self._paint(key)
        if key in KEYS:
            self.key_up(key)
        elif self._hit(e) == key:                       # 普通按钮：在按钮上松开才算点击
            {"close": self.close, "minimize": self.minimize, "shot": self.screenshot,
             "more": self.more}[key]()

    # ---------------------------------------------------------- 动作

    @staticmethod
    def _event(kind: str, code: str, down: bool) -> dict:
        if kind == "key":
            return {"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": code}}}
        return {"type": "btn", "data": {"down": down, "button": code}}

    def _send_input(self, key: str, down: bool) -> None:
        self.qmp.call("input-send-event", self._check, events=[self._event(*KEYS[key], down)])

    def _check(self, r: dict) -> None:
        if "error" in r:
            self.notify(f"操作失败：{r['error'].get('desc', r['error'])}")

    def key_down(self, key: str) -> None:
        self.held[key] = time.monotonic()
        self._send_input(key, True)

    def key_up(self, key: str) -> None:
        t0 = self.held.pop(key, None)
        if t0 is None:
            return
        wait = MIN_HOLD - (time.monotonic() - t0)
        if wait > 0:
            self.root.after(int(wait * 1000), self._send_input, key, False)
        else:
            self._send_input(key, False)

    def close(self) -> None:
        if self.qwin:
            user32.PostMessageW(self.qwin, WM_CLOSE, 0, 0)
        if not self.closing:
            self.closing = True
            self.notify("正在让 Android 关机…（隔一秒再点一次 ✕ 强制退出）", 6000)

    def minimize(self) -> None:
        if self.qwin:
            user32.ShowWindowAsync(self.qwin, SW_MINIMIZE)

    def screenshot(self) -> None:
        # QEMU 按本地代码页解释路径：先写到实例目录（avdctl 本来就要求 QEMU 能打开这里的盘），再挪过去
        tmp = self.d / "logs" / "screenshot.tmp.png"
        tmp.unlink(missing_ok=True)

        def done(r: dict) -> None:
            if "error" in r:
                self.notify(f"截图失败：{r['error'].get('desc')}")
                return
            folder = screenshot_dir()
            stem = f"RanchuWoA_{self.name}_{time.strftime('%Y%m%d_%H%M%S')}"
            dst, n = folder / f"{stem}.png", 1
            while dst.exists():
                n += 1
                dst = folder / f"{stem}_{n}.png"
            try:
                shutil.move(str(tmp), str(dst))
            except OSError as e:
                self.notify(f"截图保存失败：{e}")
                return
            self.notify(f"截图已保存：{dst.name}\n点击在资源管理器中显示", 5000,
                        lambda: subprocess.Popen(["explorer", f"/select,{dst}"]))

        self.qmp.call("screendump", done, filename=str(tmp), format="png")

    def more(self) -> None:
        if self.ext is None:
            self.ext = Extended(self)
        self.ext.toggle()

    def beside(self, w: int) -> int:
        """宽 w 的浮窗的 x：放在工具栏外侧（远离模拟器窗口），贴着屏幕边放不下就翻到另一侧。"""
        gap = round(self.GAP * self.s)
        outer = self.x + self.w + gap if self.side == "right" else self.x - gap - w
        inner = self.x - gap - w if self.side == "right" else self.x + self.w + gap
        if not self.qwin:
            return outer
        _, work = monitor_rects(self.qwin)
        return outer if work[0] <= outer and outer + w <= work[2] else inner

    def notify(self, text: str, timeout_ms: int = 4000, on_click=None) -> None:
        if not self.shown:
            return
        y0, y1, _, _ = self.items.get("shot", (0, 0, 0, 0))
        self.note.show(text, self.y + (y0 + y1) // 2, timeout_ms, on_click)

    # ---------------------------------------------------------- 窗口大小（缩放 SDL 窗口；guest 分辨率不变）

    def zoom_level(self) -> str:
        if not self.qwin:
            return ""
        cw, _ = client_size(self.qwin)
        z = cw / self.cfg["width"]
        return next((v for v in ("1", "0.75", "0.5") if abs(z - float(v)) < 0.02), "")

    def set_zoom(self, value: str) -> None:
        h = self.qwin
        if not h or user32.IsIconic(h):
            return
        fw, fh = self.cfg["width"], self.cfg["height"]
        _, work = monitor_rects(h)
        wl, wt, wr, wb = window_rect(h)
        cw, ch = client_size(h)
        extra_w, extra_h = (wr - wl) - cw, (wb - wt) - ch          # 标题栏 + 边框
        if value == "fit":
            avail_w = work[2] - work[0] - extra_w - self.w - 3 * round(self.GAP * self.s)
            avail_h = work[3] - work[1] - extra_h
            z = min(avail_w / fw, avail_h / fh)
        else:
            z = float(value)
        cw, ch = round(fw * z), round(fh * z)
        r = wintypes.RECT(0, 0, cw, ch)
        user32.AdjustWindowRectExForDpi(ctypes.byref(r), user32.GetWindowLongPtrW(h, GWL_STYLE) & 0xffffffff,
                                        False, user32.GetWindowLongPtrW(h, GWL_EXSTYLE) & 0xffffffff,
                                        user32.GetDpiForWindow(h))
        nw, nh = r.right - r.left, r.bottom - r.top
        # 尽量不挪窗口；放不下时往左上收，给工具栏留出右侧位置
        x = max(work[0], min(wl, work[2] - nw - self.w - round(self.GAP * self.s)))
        y = max(work[1] - (frame_rect(h)[1] - wt), min(wt, work[3] - nh))
        user32.SetWindowPos(h, None, x, y, nw, nh, SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)

    # ---------------------------------------------------------- 跟随 QEMU 窗口

    def _show(self, on: bool) -> None:
        if on == self.shown:
            return
        self.shown = on
        if on:
            prev = user32.GetForegroundWindow()
            self.root.deiconify()
            self.root.update_idletasks()
            if self.hwnd is None:
                self.hwnd = make_tool_window(self.root, self.qwin)
            fg = user32.GetForegroundWindow()
            if prev and fg != prev and fg == self.hwnd:      # 首次显示时 Tk 可能把自己激活了，还回去
                user32.SetForegroundWindow(prev)
        else:
            self.tip.hide()
            self.note.hide()
            self.root.withdraw()

    def dock(self) -> None:
        h = self.qwin
        if not h or not user32.IsWindow(h):
            h = self.qwin = find_qemu_window(self.pid)
            if not h:
                self._show(False)
                return
            if self.hwnd:                                   # 窗口重建过：重新挂到新窗口下
                user32.SetWindowLongPtrW(self.hwnd, GWLP_HWNDPARENT, h)
            for p in (self.tip, self.note):
                if p.win is not None and p.hwnd:
                    user32.SetWindowLongPtrW(p.hwnd, GWLP_HWNDPARENT, h)
        if user32.IsIconic(h) or not user32.IsWindowVisible(h):
            return                                          # 最小化时系统会把归它所有的工具栏一起藏起来
        dpi = user32.GetDpiForWindow(h)
        if dpi != self.dpi:
            self.dpi, self.s = dpi, dpi / 96
            self.build()
        l, t, r, b = frame_rect(h)
        mon, work = monitor_rects(h)
        if (l, t, r, b) == mon:                             # 全屏（Ctrl+Alt+F）时不显示
            self._show(False)
            return
        gap = round(self.GAP * self.s)
        if r + gap + self.w <= work[2]:
            x, side = r + gap, "right"
        elif l - gap - self.w >= work[0]:
            x, side = l - gap - self.w, "left"
        else:                                               # 两边都放不下（窗口占满屏宽）：叠在窗口内侧
            x, side = r - gap - self.w, "left"
        y = max(work[1], min(t, work[3] - self.h))
        if (x, y, side) != (self.x, self.y, self.side) or not self.shown:
            self.x, self.y, self.side = x, y, side
            self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")
            self.last_move = time.monotonic()
        self._show(True)

    def tick(self) -> None:
        now = time.monotonic()
        try:
            while True:
                try:
                    self.calls.get_nowait()()
                except queue.Empty:
                    break
            if now >= self.next_alive:
                self.next_alive = now + 0.5
                if not avdctl.pid_alive(self.pid):
                    self.root.destroy()
                    return
            self.dock()
        except Exception:
            traceback.print_exc()
        # 窗口刚动过（正在拖动）时 60 Hz 跟随，平时 10 Hz
        self.root.after(16 if now - self.last_move < 1.0 else 100, self.tick)

    def run(self) -> None:
        self.dpi = user32.GetDpiForSystem()
        self.s = self.dpi / 96
        self.build()
        self.tick()
        self.root.mainloop()


# ---------------------------------------------------------------- 扩展控制窗口（“更多”）

class Extended:
    def __init__(self, bar: Toolbar):
        self.bar = bar
        w = self.win = tk.Toplevel(bar.root)
        w.withdraw()
        w.title(f"扩展控制 - {bar.name}")
        w.resizable(False, False)
        w.protocol("WM_DELETE_WINDOW", self.hide)
        self.owned = False
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        nb.add(self._location(nb), text="  位置  ")
        nb.add(self._display(nb), text="  显示与电源  ")
        nb.add(self._help(nb), text="  帮助  ")

    def toggle(self) -> None:
        if self.win.state() == "normal":
            self.hide()
        else:
            self.show()

    def show(self) -> None:
        bar = self.bar
        self.zoom.set(bar.zoom_level())
        self._load_gps()
        self._refresh_stay()
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        x, y = bar.x + bar.w + round(Toolbar.GAP * bar.s), bar.y
        if bar.qwin:
            # 依次试：工具栏外侧、模拟器窗口另一侧；都放不下就尽量靠边（会盖住一部分模拟器窗口）
            gap = round(Toolbar.GAP * bar.s)
            _, work = monitor_rects(bar.qwin)
            ql, _, qr, _ = frame_rect(bar.qwin)
            left_edge, right_edge = min(ql, bar.x), max(qr, bar.x + bar.w)
            if right_edge + gap + w <= work[2]:
                x = right_edge + gap
            elif left_edge - gap - w >= work[0]:
                x = left_edge - gap - w
            else:
                x = max(work[0], work[2] - w)
            y = max(work[1], min(bar.y, work[3] - h))
        self.win.geometry(f"+{x}+{y}")
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        if not self.owned and bar.qwin:                     # 归 QEMU 窗口所有：总在它上面、随它最小化
            self.win.update_idletasks()
            user32.SetWindowLongPtrW(top_hwnd(self.win), GWLP_HWNDPARENT, bar.qwin)
            self.owned = True

    def hide(self) -> None:
        self.win.withdraw()

    @staticmethod
    def _grid_row(f, row: int, label: str, widget, **kw) -> None:
        ttk.Label(f, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=5)
        widget.grid(row=row, column=1, sticky=kw.get("sticky", "we"), pady=5)

    # ---------------------------------------------------------- 位置

    def _location(self, nb) -> ttk.Frame:
        f = ttk.Frame(nb, padding=14)
        self.lat, self.lon, self.alt = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.preset = ttk.Combobox(f, state="readonly", width=30, values=[p[0] for p in PRESETS])
        self.preset.bind("<<ComboboxSelected>>", self._pick_preset)
        self._grid_row(f, 0, "预设地点", self.preset)
        for i, (label, var) in enumerate((("纬度", self.lat), ("经度", self.lon),
                                          ("海拔（米）", self.alt)), 1):
            self._grid_row(f, i, label, ttk.Entry(f, textvariable=var, width=32))
        ttk.Button(f, text="设置位置", command=self._apply_gps).grid(row=4, column=1, sticky="e",
                                                                  pady=(8, 0))
        self.gps_status = ttk.Label(f, text="", foreground="#5f6368", wraplength=360, justify="left")
        self.gps_status.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        return f

    def _load_gps(self) -> None:
        value = avdctl.load_cfg(self.bar.d).get("gps") or DEFAULT_GPS
        lat, lon, *alt = value.split(";")
        self.lat.set(lat)
        self.lon.set(lon)
        self.alt.set(alt[0] if alt else "0")
        self.gps_status.config(text=f"当前位置：{lat}, {lon}")

    def _pick_preset(self, _e) -> None:
        _, lat, lon, alt = PRESETS[self.preset.current()]
        self.lat.set(str(lat))
        self.lon.set(str(lon))
        self.alt.set(str(alt))

    def _apply_gps(self) -> None:
        try:
            lat, lon = float(self.lat.get()), float(self.lon.get())
            alt = float(self.alt.get() or 0)
        except ValueError:
            self.gps_status.config(text="纬度、经度、海拔都要填数字")
            return
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            self.gps_status.config(text="纬度应在 -90..90，经度应在 -180..180")
            return
        value = f"{lat};{lon};{alt:g}"

        def done(r: dict) -> None:
            if "error" in r:
                self.gps_status.config(text=f"设置失败：{r['error'].get('desc')}")
                return
            cfg = avdctl.load_cfg(self.bar.d)               # 记入配置，下次启动沿用（同 avdctl geo）
            cfg["gps"] = value
            avdctl.save_cfg(self.bar.d, cfg)
            self.gps_status.config(text=f"已设置：{lat}, {lon}，海拔 {alt:g} 米。\n"
                                        "GPS 每秒上报一次，地图类应用可能要几秒才刷新。")

        self.bar.qmp.call("qom-set", done, path="/machine/peripheral/vsock0", property="gps",
                          value=value)

    # ---------------------------------------------------------- 显示与电源

    def _display(self, nb) -> ttk.Frame:
        f = ttk.Frame(nb, padding=14)
        zf = ttk.Frame(f)
        self.zoom = tk.StringVar()
        for i, (text, value) in enumerate((("100%", "1"), ("75%", "0.75"), ("50%", "0.5"),
                                            ("适应屏幕", "fit"))):
            ttk.Radiobutton(zf, text=text, value=value, variable=self.zoom,
                            command=lambda: self.bar.set_zoom(self.zoom.get())).grid(
                row=0, column=i, padx=(0, 12))
        self._grid_row(f, 0, "窗口大小", zf, sticky="w")
        ttk.Label(f, text="只缩放窗口，Android 的分辨率不变；也可以直接拖窗口边框。",
                  foreground="#5f6368").grid(row=1, column=1, sticky="w")
        self.stay = tk.BooleanVar()
        self.stay_cb = ttk.Checkbutton(f, text="保持唤醒（不自动灭屏）", variable=self.stay,
                                       command=self._set_stay)
        self._grid_row(f, 2, "屏幕", self.stay_cb, sticky="w")
        self.stay_status = ttk.Label(f, text="即开发者选项里的“不锁定屏幕”（模拟器一直在充电）",
                                     foreground="#5f6368")
        self.stay_status.grid(row=3, column=1, sticky="w")
        sf = ttk.Frame(f)
        ttk.Label(sf, text=str(screenshot_dir())).pack(side="left")
        ttk.Button(sf, text="打开", width=6,
                   command=lambda: os.startfile(screenshot_dir())).pack(side="left", padx=(8, 0))
        self._grid_row(f, 4, "截图保存到", sf, sticky="w")
        return f

    def _adb(self, args: list[str], done) -> None:
        adb = avdctl.adb_exe()
        if not adb:
            done(-1, "找不到 adb（装 Android SDK Platform-Tools 并加入 PATH）")
            return

        def run() -> None:
            try:
                r = subprocess.run([adb, "-s", self.bar.serial, *args], capture_output=True,
                                   timeout=20, creationflags=CREATE_NO_WINDOW)
                res = (r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace").strip())
            except (OSError, subprocess.TimeoutExpired) as e:
                res = (-1, str(e))
            self.bar.calls.put(lambda: done(*res))

        threading.Thread(target=run, daemon=True).start()

    def _refresh_stay(self) -> None:
        def done(rc: int, out: str) -> None:
            if rc == 0 and out.isdigit():
                self.stay.set(out != "0")

        self._adb(["shell", "settings", "get", "global", "stay_on_while_plugged_in"], done)

    def _set_stay(self) -> None:
        on = self.stay.get()

        def done(rc: int, out: str) -> None:
            if rc != 0:
                self.stay.set(not on)
                self.stay_status.config(text=f"设置失败：{out or rc}")
            else:
                self.stay_status.config(text="已开启：屏幕不会自动熄灭" if on else
                                        "已关闭：按系统的屏幕超时设置灭屏")

        self._adb(["shell", "svc", "power", "stayon", "true" if on else "false"], done)

    # ---------------------------------------------------------- 帮助

    def _help(self, nb) -> ttk.Frame:
        f = ttk.Frame(nb, padding=14)
        p = self.bar.ports
        text = (
            "鼠标左键 = 触摸；右键 / 侧键“后退” = 返回；中键 = 主屏幕；侧键“前进” = 最近任务\n"
            "Ctrl+Alt+P = 电源键（灭屏后按它唤醒）；键盘直接输入；剪贴板与 Windows 自动同步\n"
            "关窗口 = 让 Android 正常关机（20 秒内没关完自动强制退出；隔一秒再关一次立即退出）\n"
            f"\nadb：adb -s {self.bar.serial} shell（或 adb connect 127.0.0.1:{p['adb']}）\n"
            f"串口控制台 127.0.0.1:{p['console']}，QEMU 监视器 127.0.0.1:{p['monitor']}，"
            f"QMP 127.0.0.1:{p['qmp']}"
        )
        ttk.Label(f, text=text, justify="left").grid(row=0, column=0, columnspan=3, sticky="w")
        bf = ttk.Frame(f)
        bf.grid(row=1, column=0, sticky="w", pady=(14, 0))
        ttk.Button(bf, text="打开 adb shell", command=self._adb_shell).pack(side="left")
        ttk.Button(bf, text="打开实例目录", command=lambda: os.startfile(self.bar.d)).pack(
            side="left", padx=(8, 0))
        ttk.Button(bf, text="打开日志目录", command=lambda: os.startfile(self.bar.d / "logs")).pack(
            side="left", padx=(8, 0))
        return f

    def _adb_shell(self) -> None:
        if not avdctl.adb_exe():
            messagebox.showerror("RanchuWoA", "找不到 adb（装 Android SDK Platform-Tools 并加入 PATH）",
                                 parent=self.win)
            return
        subprocess.Popen(["cmd.exe", "/k", "adb", "-s", self.bar.serial, "shell"],
                         creationflags=CREATE_NEW_CONSOLE)


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="RanchuWoA 侧边工具栏")
    ap.add_argument("name")
    ap.add_argument("--pid", type=int, help="QEMU 进程号（默认读实例的 run.json）")
    args = ap.parse_args()
    d = avdctl.avd_dir(args.name)
    # 通常由 pythonw 拉起、没有控制台：输出一律记到实例日志
    # （不能靠 isatty 判断：stderr 重定向到 NUL 时 isatty() 也是 True）
    sys.stdout = sys.stderr = open(d / "logs" / "toolbar.log", "a", encoding="utf-8", buffering=1)
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} 工具栏启动 pid={os.getpid()}", file=sys.stderr)
    pid = args.pid or (avdctl.run_info(d) or {}).get("pid", 0)
    if not avdctl.pid_alive(pid):
        sys.exit(f"{args.name} 没有在运行")
    if not single_instance(args.name):
        sys.exit(f"{args.name} 的工具栏已经开着")
    # 按显示器 DPI 感知：坐标都是物理像素，和 QEMU 的 SDL 窗口（同样 per-monitor v2）一致，文字也不糊
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    Toolbar(args.name, d, avdctl.load_cfg(d), pid).run()


if __name__ == "__main__":
    main()
