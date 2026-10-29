"""Alpine debugfs：把 local.prop / misc 配置写入 data 盘（模拟真实模拟器首启准备）。"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444
DBG = "LD_LIBRARY_PATH=/tmp/e2/usr/lib /tmp/e2/usr/sbin/debugfs"

CMDS = [
    ("ip link set eth0 up", 3),
    ("udhcpc -i eth0 -n -q 2>/dev/null || udhcpc -i eth0", 15),
    ("mdev -s 2>/dev/null; ls /dev/vd*", 8),
    ("wget -q http://10.0.2.2:8765/e2.tar.gz -O /tmp/e2.tar.gz && wget -q http://10.0.2.2:8765/local.prop -O /tmp/local.prop && mkdir -p /tmp/e2 && tar -xzf /tmp/e2.tar.gz -C /tmp/e2 && chmod +x /tmp/e2/usr/sbin/debugfs && echo FETCH_OK", 60),
    (f"{DBG} -w -R 'rm /local.prop' /dev/vda; {DBG} -w -R 'write /tmp/local.prop /local.prop' /dev/vda && echo PROP_OK", 25),
    (f"{DBG} -R 'cat /local.prop' /dev/vda", 20),
    ("poweroff -f", 5),
]


def main():
    s = socket.create_connection((HOST, PORT), timeout=10)
    s.settimeout(2)
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 180:
        try:
            d = s.recv(4096)
        except socket.timeout:
            d = b""
        if d:
            buf += d
            sys.stdout.write(d.decode(errors="replace"))
            sys.stdout.flush()
        if buf.rstrip().endswith(b"~ #"):
            break
    print("\n=== shell 就绪 ===")
    for cmd, wait in CMDS:
        print(f"\n>>> {cmd[:70]}")
        s.sendall(cmd.encode() + b"\n")
        t1 = time.time()
        while time.time() - t1 < wait:
            try:
                d = s.recv(4096)
            except socket.timeout:
                continue
            except (ConnectionResetError, OSError):
                print("(guest 已关机)")
                return
            if d:
                sys.stdout.write(d.decode(errors="replace"))
                sys.stdout.flush()
        time.sleep(0.3)


if __name__ == "__main__":
    main()
