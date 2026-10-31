"""kernel_fixes.py — 给基础内核（images/base/android-35/kernel-ranchu，gzip 的 arm64 Image）打的
指令级补丁。内核是所有 GSI 实验共用的基础设施，与具体 GSI 无关；avdctl prepare --force 重建基础
镜像后需要重跑。

用法: py scripts/kernel_fixes.py [内核文件]     （默认 images/base/android-35/kernel-ranchu）

背景：android15-6.6 GKI 是 -fsanitize-trap（UBSAN 编成 brk #0x5512）+ panic-on-oops，而
kernel/bpf/lpm_trie.c 对 bpf_lpm_trie_key.data[]（历史 0 长数组）的索引告警是上游已知误报族
（新内核已改显式校验返回错误码）。A16/A17 的 connectivity 从 ConnectivityService 线程调
bpf(BPF_MAP_UPDATE_ELEM)（更新路径，M9 首遇）、cgroup BPF 程序在发包时调 trie_lookup（查询路径，
M10 首遇）都会踩中。trap 模式的 brk 只能被条件分支跳进（NOP 它会让顺序流掉进后面的非指令数据，
再炸 Undefined instruction），正确修法是把跳向 brk 的越界分支改跳现成的错误返回路径——等同上游
修复后的行为。

补丁一律：从 .bak 原始内核出发（幂等）；panic 现场 Code: 行的指令序列做签名，逐处校验原编码，
不唯一/不符就拒绝打。

K1：trie_update_elem（0x26a3bc..0x26a760）里 3 条 b.hs <brk@0x26a75c> → b 0x26a42c
    （x22=-28 → kfree 已分配节点 → 解锁 → 返回 -ENOSPC，M9，§28.1）。
K2：trie_lookup_elem（0x26a224..0x26a3b8）里 3 条 b.hs <brk@0x26a3b4> → b 0x26a39c
    （mov x0,xzr; ret = 返回 NULL「未命中」，对 cgroup BPF 程序是完全正常的 no-match，M10）。
"""
import gzip
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "images" / "base" / "android-35" / "kernel-ranchu"

# 每个 K：尾部签名（函数末尾的指令序列+brk，来自 panic 现场 Code: 行）+ 分支补丁表（偏移, 原编码, 新编码）
KS = {
    "K1 lpm_trie 更新路径（trie_update_elem）→ -ENOSPC": {
        "tail": bytes.fromhex("17fd9fc859ff9fc8f6031faaeeffff17") + bytes.fromhex("40a22ad4"),
        "branches": [
            (0x26A54C, bytes.fromhex("82100054"), bytes.fromhex("b8ffff17")),
            (0x26A560, bytes.fromhex("e20f0054"), bytes.fromhex("b3ffff17")),
            (0x26A56C, bytes.fromhex("820f0054"), bytes.fromhex("b0ffff17")),
        ],
    },
    "K2 lpm_trie 查询路径（trie_lookup_elem）→ 返回 NULL": {
        "tail": bytes.fromhex("e8030caa0801098b00a10091c0035fd6") + bytes.fromhex("40a22ad4"),
        "branches": [
            (0x26A2D4, bytes.fromhex("02070054"), bytes.fromhex("32000014")),
            (0x26A2E8, bytes.fromhex("62060054"), bytes.fromhex("2d000014")),
            (0x26A2F4, bytes.fromhex("02060054"), bytes.fromhex("2a000014")),
        ],
    },
}


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not path.is_file():
        sys.exit(f"找不到内核文件 {path}")
    src = path.with_suffix(".bak") if path.with_suffix(".bak").is_file() else path
    raw = gzip.decompress(src.read_bytes())

    for name, k in KS.items():
        if not re.search(re.escape(k["tail"]), raw):
            sys.exit(f"{name}：尾部签名不匹配（内核版本不同？）拒绝打")
        for off, old, _ in k["branches"]:
            if raw[off:off + 4] != old:
                sys.exit(f"{name}：@{off:#x} 的指令与预期不符（{raw[off:off + 4].hex()}）拒绝打")

    cur = gzip.decompress(path.read_bytes()) if src != path else raw
    if all(cur[off:off + 4] == new for k in KS.values() for _, _, new in k["branches"]):
        for name in KS:
            print(f"{name}（已打过）")
        print("内核已是最新（无改动）")
        return

    for name, k in KS.items():
        for off, old, new in k["branches"]:
            raw = raw[:off] + new + raw[off + 4:]
        print(f"{name}：{len(k['branches'])} 条越界分支已改跳")
    out = gzip.compress(raw, compresslevel=9, mtime=0)
    back = path.with_suffix(".bak")
    if not back.exists():
        back.write_bytes(path.read_bytes())
    path.write_bytes(out)
    print(f"已写回 {path}（原始内核备份 {back}）")


if __name__ == "__main__":
    main()
