/*
 * 最小 native_handle 定义（与 AOSP system/core/libcutils/include/cutils/native_handle.h 的 ABI 一致）。
 * woa gralloc 不链接 libcutils，句柄的创建/克隆/释放都自己实现。
 */
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct native_handle {
    int version; /* sizeof(native_handle_t) */
    int numFds;  /* number of file-descriptors at &data[0] */
    int numInts; /* number of ints at &data[numFds] */
#if defined(__clang__)
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wzero-length-array"
#pragma clang diagnostic ignored "-Wgnu-empty-struct"
#endif
    int data[0]; /* numFds + numInts ints */
#if defined(__clang__)
#pragma clang diagnostic pop
#endif
} native_handle_t;

typedef const native_handle_t* buffer_handle_t;

#ifdef __cplusplus
}
#endif
