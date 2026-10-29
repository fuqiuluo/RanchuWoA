"""最小 ADB 协议客户端——验证 guest adbd 的明文握手。

正确包头: cmd(4) + arg0(4) + arg1(4) + data_len(4) + data_crc(4) + magic(4)
magic = cmd 的 u32LE 值 ^ 0xFFFFFFFF
"""
import socket
import struct
import sys


def make_pkt(cmd: bytes, arg0: int, arg1: int, payload: bytes = b"") -> bytes:
    crc = sum(payload) & 0xFFFFFFFF
    magic = struct.unpack("<I", cmd)[0] ^ 0xFFFFFFFF
    return cmd + struct.pack("<6I", arg0, arg1, len(payload), crc, magic)[:24 - 4] + payload


def pkt(cmd, arg0, arg1, payload=b""):
    crc = sum(payload) & 0xFFFFFFFF
    magic = struct.unpack("<I", cmd)[0] ^ 0xFFFFFFFF
    hdr = cmd + struct.pack("<4I", arg0, arg1, len(payload), crc) + struct.pack("<I", magic)
    assert len(hdr) == 24
    return hdr + payload


def read_pkt(s):
    hdr = b""
    while len(hdr) < 24:
        d = s.recv(24 - len(hdr))
        if not d:
            return None
        hdr += d
    cmd = hdr[0:4]
    arg0, arg1, dlen, crc, magic = struct.unpack("<5I", hdr[4:24])
    data = b""
    while len(data) < dlen:
        d = s.recv(dlen - len(data))
        if not d:
            break
        data += d
    return cmd.decode(), arg0, arg1, data


def main():
    host, port = "127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 5556
    s = socket.create_connection((host, port), timeout=8)
    s.settimeout(10)
    # CNXN 版本 0x01000001, maxpayload 256K, 系统身份串
    payload = b"host::features=shell_v2,cmd,stat_v2,ls_v2,fixed_push_mkdir,apex,abb,fixed_push_symlink_timestamp,abb_exec,remount_shell,track_app,sendrecv_v2,sendrecv_brotli,sendrecv_lz4,openscreen_mdns"
    s.sendall(pkt(b"CNXN", 0x01000001, 1 << 17, payload))
    r = read_pkt(s)
    if r is None:
        print("!! 无响应/连接被断")
        return
    cmd, a0, a1, data = r
    print(f"<< {cmd} arg0=0x{a0:x} arg1=0x{a1:x}")
    print(f"   data({len(data)}B): {data[:300]!r}")
    if cmd == "AUTH":
        print("   -> adbd 要求认证(secure=1?)")
    elif cmd == "CNXN":
        print("   -> 明文 CNXN 握手成功! adbd =", data[:80].decode(errors="replace"))
    s.close()


if __name__ == "__main__":
    main()
