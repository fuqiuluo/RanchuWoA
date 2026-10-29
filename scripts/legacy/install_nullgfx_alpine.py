"""Alpine VM：向 system.img 内 super 的 vendor 分区写入 nullgfx（mapper/rc/vintf）。"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444
DBG = "LD_LIBRARY_PATH=/tmp/e2/usr/lib /tmp/e2/usr/sbin/debugfs"
VENDOR_SKIP = 6619136   # 3388997632 / 512
VENDOR_COUNT = 212832   # 108969984 / 512

CMDS = [
    ("mkdir -p /tmp/e2 && tar -xzf /inj/e2.tar.gz -C /tmp/e2 && chmod +x /tmp/e2/usr/sbin/debugfs && echo E2_OK", 30),
    ("ls -la /inj/ && echo FILES_OK", 5),
    (f"dd if=/dev/vda of=/tmp/vendor.ext4 bs=512 skip={VENDOR_SKIP} count={VENDOR_COUNT} status=none && ls -la /tmp/vendor.ext4 && echo DDOUT_OK", 60),
    (f"{DBG} -w -R 'write /inj/mapper.nullgfx.so lib64/hw/mapper.nullgfx.so' /tmp/vendor.ext4 && {DBG} -w -R 'write /inj/nullgfx.rc etc/init/nullgfx.rc' /tmp/vendor.ext4 && {DBG} -w -R 'write /inj/nullgfx.xml etc/vintf/manifest/nullgfx.xml' /tmp/vendor.ext4 && echo WRITE_OK", 60),
    (f"{DBG} -w -R 'set_inode_field lib64/hw/mapper.nullgfx.so mode 0100644' /tmp/vendor.ext4; {DBG} -w -R 'set_inode_field etc/init/nullgfx.rc mode 0100644' /tmp/vendor.ext4; {DBG} -w -R 'set_inode_field etc/vintf/manifest/nullgfx.xml mode 0100644' /tmp/vendor.ext4 && echo MODE_OK", 30),
    (f"{DBG} -R 'ls -l lib64/hw' /tmp/vendor.ext4 | grep nullgfx; {DBG} -R 'ls -l etc/init' /tmp/vendor.ext4 | grep nullgfx; {DBG} -R 'cat etc/init/nullgfx.rc' /tmp/vendor.ext4 && echo VERIFY_OK", 30),
    (f"dd if=/tmp/vendor.ext4 of=/dev/vda bs=512 skip={VENDOR_SKIP} seek={VENDOR_SKIP} count={VENDOR_COUNT} conv=notrunc status=none && echo DDBACK_OK", 90),
    ("poweroff -f", 5),
]


def main():
    s = socket.create_connection((HOST, PORT), timeout=10)
    s.settimeout(2)
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 120:
        try:
            d = s.recv(4096)
        except socket.timeout:
            d = b""
        if d:
            buf += d
        if buf.rstrip().endswith(b"~ #"):
            break
    print("=== shell ready ===")
    ok = []
    for cmd, wait in CMDS:
        print(f"\n>>> {cmd[:90]}")
        s.sendall(cmd.encode() + b"\n")
        t1 = time.time()
        out = b""
        while time.time() - t1 < wait:
            try:
                d = s.recv(4096)
            except socket.timeout:
                continue
            except (ConnectionResetError, OSError):
                print("(guest powered off)")
                return
            if d:
                out += d
                sys.stdout.write(d.decode(errors="replace"))
                sys.stdout.flush()
        time.sleep(0.3)
    s.close()


if __name__ == "__main__":
    main()
