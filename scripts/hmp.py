"""hmp.py — 向 QEMU HMP 监视器（tcp:127.0.0.1:4450）发命令。

用法:
  py scripts/hmp.py [--port N] "info status" "sendkey ctrl-alt-delete" ...
  py scripts/hmp.py [--port N] --shot out.png      截取当前显示（screendump → PNG）
"""
import os
import socket
import sys
import tempfile
import time

HOST, PORT = "127.0.0.1", 4450


def hmp(cmds, timeout=10.0):
    s = socket.create_connection((HOST, PORT), timeout=timeout)
    s.settimeout(0.5)

    def drain(until=b"(qemu) ", limit=timeout):
        out = b""
        end = time.time() + limit
        while time.time() < end:
            try:
                d = s.recv(65536)
            except socket.timeout:
                if out.endswith(until):
                    break
                continue
            except ConnectionResetError:  # quit 之后 QEMU 直接断开
                break
            if not d:
                break
            out += d
            if out.endswith(until):
                break
        return out

    drain()
    results = []
    for c in cmds:
        s.sendall(c.encode() + b"\n")
        out = drain()
        text = out.decode(errors="replace").replace("\r", "")
        lines = [ln for ln in text.split("\n") if ln.strip() and not ln.startswith("(qemu)")]
        # 去掉回显的命令本身
        if lines and c in lines[0]:
            lines = lines[1:]
        results.append("\n".join(lines))
    s.close()
    return results


def shot(png_path):
    from PIL import Image
    ppm = os.path.join(tempfile.gettempdir(), f"ranchu-shot-{os.getpid()}.ppm")
    hmp([f"screendump {ppm.replace(os.sep, '/')}"])
    for _ in range(50):
        if os.path.exists(ppm) and os.path.getsize(ppm) > 0:
            break
        time.sleep(0.1)
    Image.open(ppm).save(png_path)
    os.remove(ppm)
    return png_path


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--port"]:      # avdctl 启动的实例：监视器端口见 images/avds/<名字>/run.json
        PORT = int(args[1])
        args = args[2:]
    if args and args[0] == "--shot":
        print(shot(args[1]))
    else:
        for r in hmp(args):
            print(r)
