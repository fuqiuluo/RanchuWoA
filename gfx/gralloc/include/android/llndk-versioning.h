/*
 * 最小版 bionic <android/llndk-versioning.h>：NDK 里没有这个平台头。
 * 我们直接链接设备上的 libbinder_ndk.so，不做 LLNDK 版本门控。
 */
#pragma once

#define __INTRODUCED_IN_LLNDK(vendor_api_level)
#define API_LEVEL_AT_LEAST(sdk_api_level, vendor_api_level) (1)
