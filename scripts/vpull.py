"""vpull.py — 经串口控制台（tcp:4444）从 guest 拉文件（gzip + base64，root 读取）。

用法: py scripts/vpull.py <guest 路径> <宿主输出路径>
"""
import base64
import gzip
import re
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4444


def pull(guest_path, timeout=300):
    s = socket.create_connection((HOST, PORT), timeout=15)
    s.settimeout(1)
    # 标记拆开写，使命令回显里不出现完整标记；先把内核控制台日志级别压到 1
    cmd = (f"su 0 dmesg -n 1; echo __PB\"\"EGIN__; su 0 sh -c 'gzip -c {guest_path} | base64 -w 0'; "
           f"echo; echo __PE\"\"ND__\n")
    s.sendall(b"\n")
    time.sleep(0.5)
    try:
        while s.recv(65536):
            pass
    except socket.timeout:
        pass
    s.sendall(cmd.encode())
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        try:
            d = s.recv(1 << 20)
        except socket.timeout:
            continue
        if not d:
            break
        buf += d
        if b"__PEND__" in buf:
            break
    s.close()
    m = re.search(rb"__PBEGIN__\r?\n(.*?)\r?\n__PEND__", buf, re.S)
    if not m:
        raise RuntimeError("没找到数据标记：" + buf[-500:].decode(errors="replace"))
    # 内核 printk（如 system_server Watchdog 触发的 sysrq 输出不受 dmesg -n 限制）会插进数据流：
    # 每条以 "[时间戳]" 开头、以换行结尾，而 base64 字母表里没有 '['，据此剔除；gzip CRC 兜底校验
    payload = re.sub(rb"\[[^\r\n]*\r?\n", b"", m.group(1))
    payload = re.sub(rb"[^A-Za-z0-9+/=]", b"", payload)
    return gzip.decompress(base64.b64decode(payload))


def pull_retry(guest_path, tries=4):
    for i in range(tries):
        try:
            return pull(guest_path)
        except Exception as e:  # 数据被内核输出打断时重试
            print(f"第 {i + 1} 次失败：{e}", file=sys.stderr)
    raise RuntimeError("多次重试仍失败")


if __name__ == "__main__":
    data = pull_retry(sys.argv[1])
    with open(sys.argv[2], "wb") as f:
        f.write(data)
    print(f"{len(data)} bytes -> {sys.argv[2]}")
