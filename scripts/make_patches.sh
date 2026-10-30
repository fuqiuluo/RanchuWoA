#!/bin/bash
# make_patches.sh — 重新生成 patches/ 下对原版源码的补丁：
#   patches/qemu-11.1.2-ranchuwoa.patch        qemu-ranchu 相对 downloads/qemu-11.1.2.tar.xz（忽略 CRLF 差异）
#   patches/swiftshader-606261c7-ranchuwoa.patch   gfx/swiftshader 工作区相对其 HEAD（含未跟踪的新文件）
# 改了 qemu-ranchu 或 gfx/swiftshader 的源码后运行。
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p patches

# ---- QEMU ----
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
tar -xf downloads/qemu-11.1.2.tar.xz -C "$TMP" 2>/dev/null   # 个别符号链接在 Windows 上解不出，无关
ORIG="$TMP/qemu-11.1.2"
OUT=patches/qemu-11.1.2-ranchuwoa.patch
: > "$OUT"
# 构建目录、configure 生成的 pyvenv、以及没改过的大目录不参与比较
EXCL=(-x build -x pyvenv -x roms -x tests -x subprojects -x __pycache__)
diff -rq --strip-trailing-cr "${EXCL[@]}" "$ORIG" qemu-ranchu 2>/dev/null |
    sed -n -e "s|^Files $ORIG/\(.*\) and qemu-ranchu/.* differ$|\1|p" \
           -e "s|^Only in qemu-ranchu/\?\(.*\): \(.*\)$|\1/\2|p" |
    sed 's|^/||' | sort |
    while read -r f; do
        if [ -d "qemu-ranchu/$f" ]; then
            (cd qemu-ranchu && find "$f" -type f | sort)
        else
            echo "$f"
        fi
    done |
    while read -r f; do
        diff -uN --strip-trailing-cr --label "a/$f" --label "b/$f" "$ORIG/$f" "qemu-ranchu/$f" >> "$OUT"
    done
echo "$OUT：$(grep -c '^+++ ' "$OUT") 个文件"

# ---- SwiftShader ----
OUT=patches/swiftshader-606261c7-ranchuwoa.patch
git -C gfx/swiftshader -c core.autocrlf=false diff > "$OUT"
git -C gfx/swiftshader ls-files --others --exclude-standard | while read -r f; do
    git -C gfx/swiftshader -c core.autocrlf=false diff --no-index -- /dev/null "$f"
done >> "$OUT"
echo "$OUT：$(grep -c '^diff --git' "$OUT") 个文件"

# ---- gfxstream（宿主渲染器 + guest GLES 编码器）、mesa-aosp（guest 的 gralloc 适配）----
# 这两个仓库在 Windows 上按 autocrlf 检出，diff 时用 autocrlf=true 归一成 LF，补丁可直接
# git apply 到上游对应提交的干净检出上。
gitpatch() {   # <仓库目录> <输出>
    git -C "$1" -c core.autocrlf=true diff > "$2"
    git -C "$1" ls-files --others --exclude-standard | grep -v -E '^(host-build|guest-build)/' |
        while read -r f; do
            git -C "$1" -c core.autocrlf=true diff --no-index -- /dev/null "$f"
        done >> "$2"
    echo "$2：$(grep -c '^diff --git' "$2") 个文件"
}
gitpatch gfxstream "patches/gfxstream-$(git -C gfxstream rev-parse --short=7 HEAD)-ranchuwoa.patch"
gitpatch mesa-aosp "patches/mesa-aosp-$(git -C mesa-aosp rev-parse --short=7 HEAD)-ranchuwoa.patch"
