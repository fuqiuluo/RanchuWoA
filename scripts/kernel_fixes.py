"""kernel_fixes.py — 给基础内核（images/base/android-35/kernel-ranchu，gzip 的 arm64 Image）打的
指令级补丁。内核是所有 GSI 实验共用的基础设施，与具体 GSI 无关；avdctl prepare --force 重建基础
镜像后需要重跑。

用法: py scripts/kernel_fixes.py [内核文件]     （默认 images/base/android-35/kernel-ranchu）

补丁 K1：lpm_trie 的 UBSAN 越界分支改跳 -ENOSPC 错误返回。
  Android 16 的 connectivity 从 ConnectivityService 线程调 bpf(BPF_MAP_UPDATE_ELEM) 更新 LPM trie，
  某些 key 的访问索引踩中 android15-6.6 GKI 里 clang -fsanitize-trap 的边界检查（trie_update_elem
  里有 3 条 b.hs 跳到函数尾部的 brk #0x5512），trap 模式 + panic-on-oops 直接炸内核
  （M9 实测："Internal error: UBSAN" → Kernel panic；上游后来的 lpm_trie 修复正是给这类前缀
  补显式校验返回错误码）。修法：把 3 条 b.hs <brk> 都改成 b <0x26a42c>——那里是现成的错误路径
  （x22=-28 → kfree(x21) 释放已分配未挂链的节点 → 解锁 → 返回 -ENOSPC），对调用方而言就是一次
  干净的 syscall 失败。brk 本体留在原地成为死代码（第一版曾把它 NOP 掉，但越界路径会掉进 brk
  后面的非指令数据，反而 Undefined instruction 再炸——所以必须改跳转而不是 NOP）。
  函数现场（反汇编，文件内偏移 == vaddr）：
    26a548: cmp  x8, #0xc          ← 3 条检查（data_size=12 字节，netd 建的 16 字节 key）
    26a54c: b.hs 0x26a75c (brk)   → b 0x26a42c
    26a560: b.hs 0x26a75c         → b 0x26a42c
    26a56c: b.hs 0x26a75c         → b 0x26a42c
  版本锁：三处的原编码与 trie_update_elem 尾部签名（4 条指令 + brk）都匹配才打。
"""
import gzip
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "images" / "base" / "android-35" / "kernel-ranchu"

# trie_update_elem 尾部签名（panic 现场 pc 前 4 条指令 + brk 本体）
K1_TAIL_SIG = bytes.fromhex("17fd9fc859ff9fc8f6031faaeeffff17") + bytes.fromhex("40a22ad4")
# 3 条 b.hs <brk@0x26a75c> 的（偏移, 原编码, 新编码 b 0x26a42c）
K1_BRANCHES = [
    (0x26A54C, bytes.fromhex("82100054"), bytes.fromhex("b8ffff17")),
    (0x26A560, bytes.fromhex("e20f0054"), bytes.fromhex("b3ffff17")),
    (0x26A56C, bytes.fromhex("820f0054"), bytes.fromhex("b0ffff17")),
]


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not path.is_file():
        sys.exit(f"找不到内核文件 {path}")
    # 永远从 .bak（首次运行时的原始内核）出发，避免在打过补丁的镜像上叠加
    src = path.with_suffix(".bak") if path.with_suffix(".bak").is_file() else path
    raw = gzip.decompress(src.read_bytes())

    # 版本与现场校验
    if not re.search(re.escape(K1_TAIL_SIG), raw):
        sys.exit("K1：找不到 trie_update_elem 尾部签名（内核版本不同？）拒绝打")
    for off, old, _ in K1_BRANCHES:
        if raw[off:off + 4] != old:
            sys.exit(f"K1：@{off:#x} 的指令与预期不符（{raw[off:off + 4].hex()}，内核版本不同？）拒绝打")

    # 幂等：目标镜像已是新编码就跳过
    cur = gzip.decompress(path.read_bytes()) if src != path else raw
    if all(cur[off:off + 4] == new for _, _, new in K1_BRANCHES):
        print("K1 lpm_trie 越界分支 → -ENOSPC 错误路径（已打过）")
        print("内核已是最新（无改动）")
        return

    for off, old, new in K1_BRANCHES:
        raw = raw[:off] + new + raw[off + 4:]
        print(f"K1 @{off:#x}: b.hs <brk> → b 0x26a42c（-ENOSPC 错误返回）")
    out = gzip.compress(raw, compresslevel=9, mtime=0)
    back = path.with_suffix(".bak")
    if not back.exists():
        back.write_bytes(path.read_bytes())
    path.write_bytes(out)
    print(f"已写回 {path}（原始内核备份 {back}）")


if __name__ == "__main__":
    main()
