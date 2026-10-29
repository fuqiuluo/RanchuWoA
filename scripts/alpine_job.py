"""alpine_job.py — 在 Alpine aarch64 辅助 VM 里无人值守地跑一段 shell 脚本（离线改镜像用）。

做法：stock initramfs-virt 后面再拼一段 gzip newc cpio（覆盖 /init，并带上 e2fsprogs），
内核把两段都解开；我们的 /init 加载 virtio/loop 模块后执行作业脚本，最后 poweroff。
作业所需文件放进“载荷盘”：首扇区是魔数 RANCHUWOA-PAYLOAD，之后是 tar 流，
guest 里解到 /payload。串口输出实时打印并写进日志，作业脚本用 "JOB_RC=<n>" 报告结果。

作为库使用：
    from alpine_job import run_job
    rc = run_job(script_text, disks=[path, ...], payload_dir=dir_or_None, log=path)
disks 依次以 virtio-blk 挂载；guest 里用 $DISK0、$DISK1… 拿到对应的 /dev/vdX（按大小匹配，
不依赖 virtio 槽位顺序）。
"""
import gzip
import io
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALPINE = ROOT / "downloads" / "alpine"
APKS = ROOT / "downloads" / "alpine-apk"
DEV_QEMU = ROOT / "qemu-ranchu" / "build" / "qemu-system-aarch64.exe"
DIST_QEMU = ROOT / "dist" / "qemu" / "qemu-system-aarch64.exe"   # 自带 DLL（avdctl dist）
MSYS_BIN = r"C:\msys64\clangarm64\bin"
PAYLOAD_MAGIC = b"RANCHUWOA-PAYLOAD"

E2_APKS = [
    "e2fsprogs-1.47.3-r0", "e2fsprogs-extra-1.47.3-r0", "e2fsprogs-libs-1.47.3-r0",
    "libcom_err-1.47.3-r0", "libblkid-2.41.6-r1", "libuuid-2.41.6-r1",
]

INIT = r"""#!/bin/sh
export PATH=/e2/usr/sbin:/e2/sbin:/usr/sbin:/usr/bin:/sbin:/bin
export LD_LIBRARY_PATH=/e2/usr/lib
/usr/bin/busybox --install -s /usr/bin >/dev/null 2>&1
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /tmp && mount -t tmpfs tmpfs /tmp
for m in virtio_mmio virtio_pci virtio_blk loop; do modprobe $m 2>/dev/null; done
i=0; while [ ! -e /dev/vda ] && [ $i -lt 50 ]; do sleep 0.2; i=$((i+1)); done
sleep 1
echo "=== RANCHUWOA ALPINE JOB START ==="
# 按扇区数匹配 $DISKn
{disk_lookup}
# 载荷盘
for d in /dev/vd?; do
  if [ "$(head -c 17 $d 2>/dev/null)" = "RANCHUWOA-PAYLOAD" ]; then
    mkdir -p /payload && dd if=$d bs=512 skip=1 2>/dev/null | tar -x -C /payload && echo "payload: $d"
  fi
done
sh -e /job.sh
echo "JOB_RC=$?"
sync
echo "=== RANCHUWOA ALPINE JOB END ==="
poweroff -f
"""


def _cpio_entry(name, mode, data=b"", ino=[1000]):
    ino[0] += 1
    nb = name.encode() + b"\0"
    hdr = ("070701" + f"{ino[0]:08X}" + f"{mode:08X}" + "00000000" * 2 + f"{1:08X}" + "00000000"
           + f"{len(data):08X}" + "00000000" * 4 + f"{len(nb):08X}" + "00000000").encode()
    out = hdr + nb + b"\0" * ((-(110 + len(nb))) % 4) + data + b"\0" * ((-len(data)) % 4)
    return out


def build_overlay(init_text, job_text):
    entries = []
    dirs = set()

    def add_dir(path):
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if d and d not in dirs:
                dirs.add(d)
                entries.append(_cpio_entry(d, 0o40755))

    for apk in E2_APKS:
        with tarfile.open(APKS / f"{apk}.apk", "r:gz") as t:
            for m in t.getmembers():
                if m.name.startswith(".") or not m.name:
                    continue
                dst = "e2/" + m.name
                if m.isdir():
                    add_dir(dst)
                elif m.issym():
                    add_dir(os.path.dirname(dst))
                    entries.append(_cpio_entry(dst, 0o120777, m.linkname.encode()))
                elif m.isfile():
                    add_dir(os.path.dirname(dst))
                    entries.append(_cpio_entry(dst, 0o100755, t.extractfile(m).read()))
    entries.append(_cpio_entry("init", 0o100755, init_text.encode()))
    entries.append(_cpio_entry("job.sh", 0o100755, job_text.encode()))
    trailer = _cpio_entry("TRAILER!!!", 0)
    raw = b"".join(entries) + trailer
    raw += b"\0" * ((-len(raw)) % 512)
    return gzip.compress(raw, 6)


def build_payload(payload_dir, out_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as t:
        for p in sorted(Path(payload_dir).rglob("*")):
            arc = p.relative_to(payload_dir).as_posix()
            info = t.gettarinfo(str(p), arc)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mode = 0o755 if p.is_dir() else 0o644
            if p.is_file():
                with open(p, "rb") as fh:
                    t.addfile(info, fh)
            else:
                t.addfile(info)
    data = PAYLOAD_MAGIC.ljust(512, b"\0") + buf.getvalue()
    data += b"\0" * ((-len(data)) % (1 << 20))
    Path(out_path).write_bytes(data)
    return out_path


def run_job(job_text, disks=(), payload_dir=None, log=None, mem="2048", timeout=1800, echo=True):
    """echo=False 时串口输出只写日志，不打印到终端。"""
    work = ROOT / "images" / ".alpine-job"
    work.mkdir(parents=True, exist_ok=True)
    # 每个盘可以是路径（raw），或 (路径, 格式, 虚拟字节数)（如 qcow2）
    specs = []
    for d in disks:
        if isinstance(d, tuple):
            path, fmt, vsize = d
        else:
            path, fmt, vsize = d, "raw", os.path.getsize(d)
        specs.append((str(Path(path).resolve()), fmt, vsize))
    lookup = []
    for i, (d, fmt, vsize) in enumerate(specs):
        sectors = vsize // 512
        lookup.append(
            f'for d in /dev/vd?; do [ "$(cat /sys/block/${{d#/dev/}}/size)" = "{sectors}" ] && export DISK{i}=$d; done; '
            f'echo "DISK{i}=$DISK{i}"')
    init = INIT.replace("{disk_lookup}", "\n".join(lookup))
    initrd = work / "initrd.img"
    initrd.write_bytes((ALPINE / "initramfs-virt").read_bytes() + build_overlay(init, job_text))
    drives = []
    for i, (d, fmt, vsize) in enumerate(specs):
        drives += ["-drive", f"file={d},if=none,id=d{i},format={fmt},cache=writeback",
                   "-device", f"virtio-blk-device,drive=d{i}"]
    if payload_dir:
        payload = build_payload(payload_dir, work / "payload.img")
        drives += ["-drive", f"file={payload},if=none,id=pl,format=raw,readonly=on",
                   "-device", "virtio-blk-device,drive=pl"]
    env = dict(os.environ)
    if DIST_QEMU.exists():
        qemu = DIST_QEMU
    else:
        qemu = DEV_QEMU
        env["PATH"] = MSYS_BIN + os.pathsep + env.get("PATH", "")
    cmd = [str(qemu), "-M", "virt", "-accel", "whpx", "-cpu", "host", "-m", mem, "-smp", "4",
           "-kernel", str(ALPINE / "vmlinuz-virt"), "-initrd", str(initrd),
           "-append", "console=ttyAMA0 rdinit=/init quiet",
           *drives, "-nic", "none",      # 不要默认网卡（virtio-net-pci 要 efi-virtio.rom，dist 里没有）
           "-nographic", "-serial", "stdio", "-monitor", "none", "-no-reboot"]
    logf = open(log, "w", encoding="utf-8") if log else None
    rc = None
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            stdin=subprocess.DEVNULL)
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if echo:
                print(line, flush=True)
            if logf:
                logf.write(line + "\n")
                logf.flush()
            if line.startswith("JOB_RC="):
                rc = int(line.split("=", 1)[1] or 1)
            if time.time() - t0 > timeout:
                print("!!! 超时，终止 QEMU")
                proc.kill()
                break
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
        if logf:
            logf.close()
    return rc if rc is not None else 255


if __name__ == "__main__":
    # 冒烟测试：py scripts/alpine_job.py <disk>  —— 打印磁盘信息和 e2fsprogs 版本
    rc = run_job('echo "disk0=$DISK0"; ls -la /dev/vd*; e2fsck -V; resize2fs 2>&1 | head -1; debugfs -V',
                 disks=sys.argv[1:2])
    print("rc =", rc)
    sys.exit(rc)
