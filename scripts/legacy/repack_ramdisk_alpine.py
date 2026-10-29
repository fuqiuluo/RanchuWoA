"""Alpine 客户机内重打包 ramdisk：改 fstab 后用真 cpio 打包写到 scratch 盘。"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444

CMDS = [
    ("ip link set eth0 up", 3),
    ("udhcpc -i eth0 -n -q 2>/dev/null || udhcpc -i eth0", 15),
    ("mdev -s 2>/dev/null; ls /dev/vd*", 8),
    ("mkdir -p /work && cd /work && wget -q http://10.0.2.2:8765/first.cpio http://10.0.2.2:8765/second.cpio http://10.0.2.2:8765/fstab-new.ranchu && echo FETCH_OK", 60),
    ("cd /work && rm -rf root && mkdir root && cd root && cpio -idm --quiet < ../first.cpio; cpio -idm --quiet < ../second.cpio; ls first_stage_ramdisk/ lib/modules/ | head -6", 30),
    ("cd /work/root && cp ../fstab-new.ranchu first_stage_ramdisk/fstab.ranchu && grep -c vda first_stage_ramdisk/fstab.ranchu", 10),
    ("cd /work/root && find . | cpio -o -H newc --quiet > /work/new.cpio && ls -la /work/new.cpio", 60),
    ("D=$(ls /dev/vd? 2>/dev/null | head -1); echo DD_TO_$D; dd if=/work/new.cpio of=$D bs=1M conv=fsync && echo WRITE_OK", 120),
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
    ok = False
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
                print("(guest 已关机)")
                print("WRITE_OK" in out and "打包写入成功" or "未确认写入")
                return
            if d:
                out += d
                sys.stdout.write(d.decode(errors="replace"))
                sys.stdout.flush()
        if b"WRITE_OK" in out:
            ok = True
        time.sleep(0.3)
    print("\n结果:", "成功" if ok else "失败")


if __name__ == "__main__":
    main()
