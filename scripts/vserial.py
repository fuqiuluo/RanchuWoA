"""vserial.py — Android 串口控制台可靠执行器。

用法:
  py vserial.py [--port N] "cmd1" "cmd2" ...        逐条执行并回显（默认端口 4444）
  py vserial.py [--port N] --filter "adbd" "cmd"    过滤输出行
"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444
MARK = "VXDONE_"


def main():
    global PORT
    args = sys.argv[1:]
    filt = None
    if args[:1] == ["--port"]:      # avdctl 实例的串口控制台端口见 run.json 的 console
        PORT = int(args[1])
        args = args[2:]
    if args and args[0] == "--filter":
        filt = args[1].encode()
        args = args[2:]
    s = socket.create_connection((HOST, PORT), timeout=15)
    s.settimeout(3)

    def drain(sec: float) -> bytes:
        out = b""
        end = time.time() + sec
        while time.time() < end:
            try:
                d = s.recv(4096)
                if d:
                    out += d
            except socket.timeout:
                if MARK.encode() in out:
                    break
        return out

    drain(2)
    s.sendall(b"\n")
    drain(1)
    for cmd in args:
        s.sendall(cmd.encode() + b"\n")
        s.sendall(b"echo " + MARK.encode() + b"$?\n")
        out = drain(12)
        lines = out.split(b"\r\n")
        for ln in lines:
            if filt and filt not in ln:
                continue
            txt = ln.decode(errors="replace")
            if txt.strip() and "VXDONE" not in txt:
                print(txt)
        # 提取退出码
        for ln in lines:
            if ln.startswith(MARK.encode()):
                print(f"[rc={ln[len(MARK):].decode(errors='replace') or '?'}]")
    s.close()


if __name__ == "__main__":
    main()
