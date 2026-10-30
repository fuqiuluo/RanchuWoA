"""抓 QEMU SDL 窗口的实际画面（屏幕截取），滚动期间统计“回跳/闪烁”帧。

用法：py scripts/flicker_cap.py <窗口标题子串> <秒数> <输出目录> [adb 序列号]（需要 numpy、Pillow）
期间在后台用 adb 做连续慢速滚动。分析：相邻帧的垂直位移（行匹配），位移方向与滚动方向
相反的帧 = 显示了旧帧；整帧亮度骤降 = 占位/黑帧。
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import ImageGrab

user32 = ctypes.windll.user32
ctypes.windll.shcore.SetProcessDpiAwareness(2)


def find_window(sub):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        n = user32.GetWindowTextLengthW(h)
        if n and user32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if sub in buf.value:
                found.append((h, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return found


def client_rect(h):
    r = wt.RECT()
    user32.GetClientRect(h, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(h, ctypes.byref(pt))
    return (pt.x, pt.y, pt.x + r.right, pt.y + r.bottom)


def shift(a, b, maxs=120):
    """b 相对 a 的垂直位移（内容上移为正）；用中间一条带做 SAD 匹配。"""
    h = a.shape[0]
    band = a[h // 3: 2 * h // 3]
    best, bs = None, 0
    for s in range(-maxs, maxs + 1):
        y0 = h // 3 - s
        if y0 < 0 or y0 + band.shape[0] > h:
            continue
        d = np.abs(b[y0:y0 + band.shape[0]] - band).mean()
        if best is None or d < best:
            best, bs = d, s
    return bs, best


def main():
    title, secs, out = sys.argv[1], float(sys.argv[2]), Path(sys.argv[3])
    serial = sys.argv[4] if len(sys.argv) > 4 else "emulator-5554"
    out.mkdir(parents=True, exist_ok=True)
    wins = find_window(title)
    if not wins:
        sys.exit(f"找不到窗口：{title}")
    h, name = wins[0]
    user32.SetForegroundWindow(h)
    time.sleep(0.3)
    box = client_rect(h)
    print("窗口", name, box)
    # 后台连续慢速上滑（内容向上移动）
    scroll = subprocess.Popen(
        ["adb", "-s", serial, "shell",
         "for i in 1 2 3 4 5 6; do input swipe 360 1150 360 250 900; done"])
    frames, ts = [], []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        img = ImageGrab.grab(bbox=box, all_screens=True).convert("L")
        frames.append(np.asarray(img, dtype=np.float32))
        ts.append(time.perf_counter() - t0)
    scroll.wait()
    print(f"抓到 {len(frames)} 帧，{len(frames) / secs:.1f} fps")
    lum = [f.mean() for f in frames]
    med = float(np.median(lum))
    back, dark, moves = [], [], 0
    for i in range(1, len(frames)):
        s, d = shift(frames[i - 1], frames[i])
        if s > 0:
            moves += 1
        if s < -2 and d < 8:
            back.append((i, s))
        if lum[i] < med * 0.6:
            dark.append((i, round(lum[i], 1)))
    print(f"向前移动的帧对 {moves}；回跳 {len(back)}：{back[:20]}；暗帧 {len(dark)}：{dark[:20]}")
    for i, _ in (back + dark)[:6]:
        from PIL import Image
        Image.fromarray(frames[i - 1].astype(np.uint8)).save(out / f"f{i - 1:04d}.png")
        Image.fromarray(frames[i].astype(np.uint8)).save(out / f"f{i:04d}.png")


if __name__ == "__main__":
    main()
