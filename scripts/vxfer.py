"""vxfer.py — 经 virtio 串口 ranchu.xfer（tcp:127.0.0.1:4447）与 guest 互传文件（二进制，快）。

guest 侧命令经串口控制台（tcp:4444）以 root 执行。在 Git Bash 下调用时设 MSYS_NO_PATHCONV=1。

用法:
  py scripts/vxfer.py push <本地文件> <guest 路径>
  py scripts/vxfer.py pull <guest 路径> <本地文件>
"""
import hashlib
import os
import re
import socket
import sys
import time

CONSOLE = ("127.0.0.1", 4444)
XFER = ("127.0.0.1", 4447)


class Console:
    """串口 shell：发命令、按标记取输出（容忍回显和插进来的内核日志）。"""

    def __init__(self):
        self.s = socket.create_connection(CONSOLE, timeout=10)
        self.s.settimeout(0.5)
        self.seq = 0
        self.s.sendall(b"\n")
        self._drain(1.0)

    def _drain(self, sec):
        end = time.time() + sec
        out = b""
        while time.time() < end:
            try:
                d = self.s.recv(65536)
                if d:
                    out += d
            except socket.timeout:
                pass
        return out

    def run(self, cmd, timeout=60):
        self.seq += 1
        tag = f"VX{self.seq}"
        # 标记拆开写，回显里不会出现完整的 BEGIN/END
        line = f'echo {tag}_B""EGIN; {cmd}; echo {tag}_E""ND_$?\n'
        self.s.sendall(line.encode())
        buf = b""
        end = time.time() + timeout
        pat = re.compile(rf"{tag}_BEGIN\r?\n(.*?){tag}_END_(\d+)".encode(), re.S)
        while time.time() < end:
            try:
                d = self.s.recv(65536)
            except socket.timeout:
                continue
            buf += d
            m = pat.search(buf)
            if m:
                text = re.sub(rb"\[\s*\d+\.\d+\]\[[^\]]*\][^\r\n]*\r?\n", b"", m.group(1))
                return int(m.group(2)), text.decode(errors="replace").replace("\r", "").strip()
        raise TimeoutError(f"命令超时: {cmd}")

    def close(self):
        self.s.close()


def xfer_port(con):
    rc, out = con.run("for p in /sys/class/virtio-ports/*; do "
                      "[ \"$(cat $p/name 2>/dev/null)\" = ranchu.xfer ] && echo /dev/${p##*/}; done")
    dev = out.strip().splitlines()[-1] if out.strip() else ""
    if not dev.startswith("/dev/vport"):
        raise RuntimeError(f"guest 里找不到 ranchu.xfer 端口: {out!r}")
    return dev


def push(local, remote):
    data = open(local, "rb").read()
    md5 = hashlib.md5(data).hexdigest()
    con = Console()
    dev = xfer_port(con)
    sock = socket.create_connection(XFER, timeout=30)
    time.sleep(0.3)     # 让 guest 侧看到 host_connected
    # guest 侧后台接收固定字节数
    con.s.sendall(f"su 0 sh -c 'head -c {len(data)} {dev} > {remote}.part' &\n".encode())
    time.sleep(0.5)
    t0 = time.time()
    sock.sendall(data)
    rc, out = con.run(f"wait; su 0 sh -c 'mv {remote}.part {remote} && md5sum {remote}'", timeout=300)
    sock.close()
    con.close()
    ok = md5 in out
    print(f"push {local} -> {remote}: {len(data)} B, {time.time() - t0:.1f}s, md5 {'OK' if ok else 'MISMATCH: ' + out}")
    return ok


def pull(remote, local):
    con = Console()
    dev = xfer_port(con)
    rc, out = con.run(f"su 0 stat -c %s {remote}")
    size = int(out.strip().splitlines()[-1])
    rc, out = con.run(f"su 0 md5sum {remote}")
    md5 = out.split()[-2] if len(out.split()) >= 2 else ""
    sock = socket.create_connection(XFER, timeout=30)
    time.sleep(0.3)
    con.s.sendall(f"su 0 sh -c 'cat {remote} > {dev}' &\n".encode())
    buf = bytearray()
    sock.settimeout(60)
    t0 = time.time()
    while len(buf) < size:
        d = sock.recv(1 << 20)
        if not d:
            break
        buf += d
    sock.close()
    con.run("wait")
    con.close()
    open(local, "wb").write(buf)
    ok = hashlib.md5(buf).hexdigest() == md5
    print(f"pull {remote} -> {local}: {len(buf)}/{size} B, {time.time() - t0:.1f}s, md5 {'OK' if ok else 'MISMATCH'}")
    return ok


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("push", "pull"):
        print(__doc__)
        sys.exit(2)
    fn = push if sys.argv[1] == "push" else pull
    sys.exit(0 if fn(sys.argv[2], sys.argv[3]) else 1)
