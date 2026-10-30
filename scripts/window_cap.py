"""window_cap.py — 抓 QEMU SDL 窗口的实际画面（屏幕截取，约 23 fps），查单帧闪烁与黑帧。

用法：py scripts/window_cap.py <窗口标题子串> <秒数> <输出目录> [--adb <序列号> --cmd "<guest shell 命令>"]
  --cmd 在开始抓取 1 秒后经 adb shell 在后台执行（例如一串 am start / input swipe / input keyevent）。
例：py scripts/window_cap.py "hyperos - RanchuWoA" 20 logs/cap --adb emulator-5556 \
      --cmd "am start -n com.miui.gallery/.activity.HomePageActivity; sleep 3; input keyevent HOME"

判定（需要 numpy、Pillow）：
  闪烁帧 = 与前一帧、后一帧都差很多，而前后两帧彼此相近（画面跳到别的内容又跳回来，
           例如应用切换动画中间夹了一帧旧画面）；每个闪烁帧把前/本/后三帧拼成一张图存到输出目录。
  近黑帧 = 整帧平均亮度 < 3（熄屏、占位画面）。
HMP/QMP 截屏看的是 QEMU 控制台 surface，看不到窗口实际呈现的问题，这类问题要用本脚本。
与 flicker_cap.py 的区别：那个专看滚动时的“回跳”，这个看任意操作下的单帧闪烁。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageGrab

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
                found.append(h)
        return True

    user32.EnumWindows(cb, 0)
    return found


def client_rect(h):
    r = wt.RECT()
    user32.GetClientRect(h, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(h, ctypes.byref(pt))
    return (pt.x, pt.y, pt.x + r.right, pt.y + r.bottom)


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("title")
    ap.add_argument("secs", type=float)
    ap.add_argument("out")
    ap.add_argument("--adb", default="emulator-5554")
    ap.add_argument("--cmd")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wins = find_window(args.title)
    if not wins:
        sys.exit(f"找不到窗口：{args.title}")
    box = client_rect(wins[0])
    proc = None
    if args.cmd:
        proc = subprocess.Popen(["adb", "-s", args.adb, "shell", f"sleep 1; {args.cmd}"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    frames, ts = [], []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < args.secs:
        img = ImageGrab.grab(bbox=box, all_screens=True)
        frames.append(np.asarray(img.convert("RGB"), dtype=np.int16))
        ts.append(time.perf_counter() - t0)
    if proc:
        proc.wait()
    n = len(frames)
    print(f"窗口 {box}，抓到 {n} 帧，{n / args.secs:.1f} fps")

    def diff(a, b):
        return float(np.abs(frames[a] - frames[b]).mean())

    lum = [float(f.mean()) for f in frames]
    flashes = []
    for i in range(1, n - 1):
        a, b, c = diff(i - 1, i), diff(i, i + 1), diff(i - 1, i + 1)
        if a > 8 and b > 8 and c < min(a, b) * 0.5:
            flashes.append((i, round(ts[i], 2), round(a, 1), round(b, 1), round(c, 1)))
    dark = [(i, round(ts[i], 2)) for i in range(n) if lum[i] < 3]
    print(f"闪烁帧 {len(flashes)}（帧号, 时刻, 与前帧差, 与后帧差, 前后帧之差）：")
    for f in flashes[:40]:
        print("  ", f)
    print(f"近黑帧 {len(dark)}" + (f"，从 {dark[0][1]}s 到 {dark[-1][1]}s" if dark else ""))
    for f in flashes[:12]:
        i = f[0]
        strip = np.concatenate([frames[i - 1], frames[i], frames[i + 1]], axis=1).astype(np.uint8)
        Image.fromarray(strip).save(out / f"flash_{i:04d}.png")
    print("亮度序列（每 5 帧）：", [round(x) for x in lum[::5]])


if __name__ == "__main__":
    main()
