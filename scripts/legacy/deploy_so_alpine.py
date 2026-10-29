"""Alpine VM：用 debugfs 往 data.img 写 SwiftShader so + 部署脚本。"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444

CMDS = [
    ("ip link set eth0 up", 3),
    ("udhcpc -i eth0 -n -q 2>/dev/null || udhcpc -i eth0", 15),
    ("mdev -s 2>/dev/null; ls /dev/vd*", 8),
    ("wget -q -O /tmp/e2.tar.gz http://10.0.2.2:8765/e2.tar.gz && wget -q -O /tmp/libEGL_swiftshader.so http://10.0.2.2:8765/libEGL_swiftshader.so && wget -q -O /tmp/libGLESv2_swiftshader.so http://10.0.2.2:8765/libGLESv2_swiftshader.so && mkdir -p /tmp/e2 && tar -xzf /tmp/e2.tar.gz -C /tmp/e2 && chmod +x /tmp/e2/usr/sbin/debugfs && echo FETCH_OK", 180),
    ("DBG=/tmp/e2/usr/sbin/debugfs; LD=/tmp/e2/usr/lib; D=/dev/vda; \$DBG -w -R 'mkdir /local' \$D 2>/dev/null; \$DBG -w -R 'mkdir /local/tmp' \$D 2>/dev/null; \$DBG -w -R 'write /tmp/libEGL_swiftshader.so /local/tmp/libEGL_swiftshader.so' \$D && \$DBG -w -R 'write /tmp/libGLESv2_swiftshader.so /local/tmp/libGLESv2_swiftshader.so' \$D && echo DEPLOY_OK", 240),
    ("LD_LIBRARY_PATH=/tmp/e2/usr/lib /tmp/e2/usr/sbin/debugfs -R 'ls -l /local/tmp' /dev/vda 2>&1 | head -6", 20),
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
        print(f"\n>>> {cmd[:80]}")
        s.sendall(cmd.encode() + b"\n")
        t1 = time.time()
        while time.time() - t1 < wait:
            try:
                d = s.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                print("(guest 已关机)")
                return
            if d:
                sys.stdout.write(d.decode(errors="replace"))
                sys.stdout.flush()
        time.sleep(0.3)
    s.close()


if __name__ == "__main__":
    main()
