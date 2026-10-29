#!/bin/bash
# build.sh — 用 NDK r27c 构建 nullgfx（mapper.nullgfx.so + allocatord）
set -e
NDK="C:/Users/admin/Documents/Emulator/gfx/android-ndk-r27c/toolchains/llvm/prebuilt/windows-x86_64/bin"
API=35
CXX="$NDK/clang++.exe"
TARGET="aarch64-linux-android$API"
OUT="C:/Users/admin/Documents/Emulator/gfx/nullgfx/out"
mkdir -p "$OUT"

"$CXX" --target="$TARGET" -std=c++17 -O2 -fPIC -shared -static-libstdc++ -Iinclude \
    -o "$OUT/mapper.nullgfx.so" \
    "C:/Users/admin/Documents/Emulator/gfx/nullgfx/mapper_nullgfx.cpp" \
    -llog -ldl

"$CXX" --target="$TARGET" -std=c++17 -O2 -static-libstdc++ -Iinclude \
    -o "$OUT/allocatord" \
    "C:/Users/admin/Documents/Emulator/gfx/nullgfx/allocatord.cpp" \
    -L"$OUT" -lbinder_ndk -llog

ls -la "$OUT"
