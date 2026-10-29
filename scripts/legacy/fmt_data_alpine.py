"""通过 TCP 串口驱动 Alpine：联网下载 e2fsprogs apk 并格式化 /dev/vda。"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444

CMDS = [
    ("ip link set eth0 up", 3),
    ("udhcpc -i eth0 -n -q 2>/dev/null || udhcpc -i eth0", 15),
    ("wget -q -O /tmp/e2.tar.gz http://10.0.2.2:8765/e2.tar.gz && echo TAR_OK", 60),
    ("mkdir -p /tmp/e2 && tar -xzf /tmp/e2.tar.gz -C /tmp/e2 && ls /tmp/e2/sbin/mke2fs && echo EXTRACT_OK", 15),
    ("mdev -s 2>/dev/null; ls /dev/vd*", 8),
    ("chmod +x /tmp/e2/sbin/mke2fs && LD_LIBRARY_PATH=/tmp/e2/usr/lib /tmp/e2/sbin/mke2fs -F -b 4096 -L userdata -q /dev/vda && echo FORMAT_OK", 240),
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
        print(f"\n>>> {cmd}")
        s.sendall(cmd.encode() + b"\n")
        out = b""
        t1 = time.time()
        while time.time() - t1 < wait:
            try:
                d = s.recv(4096)
            except socket.timeout:
                continue
            except ConnectionResetError:
                print("(连接被 guest 关闭)")
                return
            if d:
                out += d
                sys.stdout.write(d.decode(errors="replace"))
                sys.stdout.flush()
        time.sleep(0.3)
    s.close()


if __name__ == "__main__":
    main()
