/* RanchuWoA：hardware/gralloc.h 的最小替代——HAL_PIXEL_FORMAT_*（来自 SwiftShader 平台头
   system/graphics.h）+ egl.cpp 用到的 GRALLOC_USAGE_* 标志。 */
#pragma once

#include <system/graphics.h>

/* 与 hardware/gralloc.h（Android 15）一致的用量标志子集 */
#define GRALLOC_USAGE_SW_READ_OFTEN 0x3ULL
#define GRALLOC_USAGE_SW_WRITE_OFTEN 0x30ULL
#define GRALLOC_USAGE_HW_TEXTURE 0x100ULL
#define GRALLOC_USAGE_HW_RENDER 0x200ULL
#define GRALLOC_USAGE_HW_2D 0x400ULL
#define GRALLOC_USAGE_HW_COMPOSER 0x800ULL
#define GRALLOC_USAGE_HW_FB 0x1000ULL
#define GRALLOC_USAGE_HW_VIDEO_ENCODER 0x10000ULL
