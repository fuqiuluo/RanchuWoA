"""Alpine debugfs：删旧 key 文件，建 key/ 目录并写入新生成的 metadata 密钥组。
密钥由 make_meta_key.py 为模拟器自身 data 分区全新随机生成（非提取任何既有密钥）；
后因 FBE 关闭方案未启用。"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444
DBG = "LD_LIBRARY_PATH=/tmp/e2/usr/lib /tmp/e2/usr/sbin/debugfs"

CMDS = [
    ("ip link set eth0 up", 3),
    ("udhcpc -i eth0 -n -q 2>/dev/null || udhcpc -i eth0", 15),
    ("mdev -s 2>/dev/null; ls /dev/vd*", 8),
    ("wget -q http://10.0.2.2:8765/e2.tar.gz -O /tmp/e2.tar.gz && wget -q http://10.0.2.2:8765/version -O /tmp/version && wget -q http://10.0.2.2:8765/encrypted_key -O /tmp/encrypted_key && mkdir -p /tmp/e2 && tar -xzf /tmp/e2.tar.gz -C /tmp/e2 && chmod +x /tmp/e2/usr/sbin/debugfs && echo FETCH_OK", 60),
    (f"{DBG} -w -R 'rm /vold/metadata_encryption/key' /dev/vda && echo RM_DONE", 20),
    (f"{DBG} -w -R 'mkdir /vold/metadata_encryption/key' /dev/vda && echo MKDIR_DONE", 20),
    (f"{DBG} -w -R 'write /tmp/version /vold/metadata_encryption/key/version' /dev/vda && {DBG} -w -R 'write /tmp/encrypted_key /vold/metadata_encryption/key/encrypted_key' /dev/vda && echo KEYFILES_OK", 30),
    (f"{DBG} -R 'ls -l /vold/metadata_encryption/key' /dev/vda", 20),
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
