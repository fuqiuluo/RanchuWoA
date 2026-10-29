"""qmp_input.py — 经 QMP（tcp:127.0.0.1:4451）向 VM 注入输入事件，走真实的 QEMU 输入层
（宿主窗口的鼠标/键盘也走同一条路：→ virtio-touchscreen / virtio-keyboard → Android）。

坐标用屏幕像素（按显示分辨率换算到 QEMU 的 0..32767 绝对坐标）。

用法（选项放在最前面）:
  [--port N]         QMP 端口，默认 4451（开发镜像）；avdctl 实例见 images/avds/<名字>/run.json 的 qmp
  [--screen WxH]     显示分辨率，默认 720x1280
  py scripts/qmp_input.py tap X Y
  py scripts/qmp_input.py swipe X1 Y1 X2 Y2 [毫秒]
  py scripts/qmp_input.py key <qcode>...      例如 key home / key esc / key ctrl a
  py scripts/qmp_input.py text "hello"
  py scripts/qmp_input.py back                （鼠标右键 = BACK）
"""
import json
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4451
SCREEN = (720, 1280)
ABS_MAX = 32767


class Qmp:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=10)
        self.f = self.s.makefile("rwb")
        self._read()                       # greeting
        self.cmd("qmp_capabilities")

    def _read(self):
        while True:
            line = self.f.readline()
            if not line:
                raise ConnectionError("QMP 断开")
            msg = json.loads(line)
            if "event" in msg:
                continue
            return msg

    def cmd(self, name, **args):
        req = {"execute": name}
        if args:
            req["arguments"] = args
        self.f.write(json.dumps(req).encode() + b"\n")
        self.f.flush()
        resp = self._read()
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp.get("return")

    def events(self, events):
        return self.cmd("input-send-event", events=events)


def _abs(x, y):
    ax = round(x * ABS_MAX / (SCREEN[0] - 1))
    ay = round(y * ABS_MAX / (SCREEN[1] - 1))
    return [{"type": "abs", "data": {"axis": "x", "value": ax}},
            {"type": "abs", "data": {"axis": "y", "value": ay}}]


def _btn(down, button="left"):
    return [{"type": "btn", "data": {"down": down, "button": button}}]


def tap(q, x, y):
    q.events(_abs(x, y))
    q.events(_btn(True) + _abs(x, y))
    time.sleep(0.08)
    q.events(_btn(False) + _abs(x, y))


def swipe(q, x1, y1, x2, y2, ms=300):
    steps = max(8, ms // 16)
    q.events(_abs(x1, y1))
    q.events(_btn(True) + _abs(x1, y1))
    for i in range(1, steps + 1):
        t = i / steps
        q.events(_abs(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
        time.sleep(ms / 1000 / steps)
    q.events(_btn(False) + _abs(x2, y2))


def key(q, *codes):
    keys = [{"type": "qcode", "data": c} for c in codes]
    q.cmd("send-key", keys=keys)


def text(q, s):
    special = {" ": "spc", ".": "dot", ",": "comma", "-": "minus", "/": "slash", "@": None}
    for ch in s:
        if ch.isalpha():
            key(q, *(["shift", ch.lower()] if ch.isupper() else [ch]))
        elif ch.isdigit():
            key(q, ch)
        elif special.get(ch):
            key(q, special[ch])
        time.sleep(0.03)


def main():
    global PORT, SCREEN
    a = sys.argv[1:]
    while a[:1] and a[0] in ("--port", "--screen"):
        if a[0] == "--port":
            PORT = int(a[1])
        else:
            w, h = a[1].lower().split("x")
            SCREEN = (int(w), int(h))
        a = a[2:]
    if not a:
        print(__doc__)
        return
    q = Qmp()
    if a[0] == "tap":
        tap(q, float(a[1]), float(a[2]))
    elif a[0] == "swipe":
        swipe(q, *(float(v) for v in a[1:5]), ms=int(a[5]) if len(a) > 5 else 300)
    elif a[0] == "key":
        key(q, *a[1:])
    elif a[0] == "text":
        text(q, a[1])
    elif a[0] == "back":
        q.events(_btn(True, "right"))
        time.sleep(0.05)
        q.events(_btn(False, "right"))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
