"""串口诊断 adbd。"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444
CMDS = [
    "\n",
    "getprop init.svc.adbd; getprop service.adb.tcp.port; getprop sys.usb.config",
    "cat /proc/net/tcp | head -10",
    "stop adbd; setprop service.adb.tcp.port 5555; start adbd; sleep 2; getprop init.svc.adbd",
    "cat /proc/net/tcp | awk '{print $2}' | head -12",
    "ps -A | grep adbd",
]


def main():
    s = socket.create_connection((HOST, PORT), timeout=10)
    s.settimeout(3)
    t0 = time.time()
    while time.time() - t0 < 8:
        try:
            d = s.recv(4096)
            if d:
                sys.stdout.write(d.decode(errors="replace"))
                sys.stdout.flush()
        except socket.timeout:
            pass
    for cmd in CMDS:
        print(f"\n>>> {cmd}")
        s.sendall(cmd.encode() + b"\n")
        t1 = time.time()
        while time.time() - t1 < 12:
            try:
                d = s.recv(4096)
                if d:
                    sys.stdout.write(d.decode(errors="replace"))
                    sys.stdout.flush()
            except socket.timeout:
                continue
            except OSError:
                print("(断开)")
                return
    s.close()


if __name__ == "__main__":
    main()
