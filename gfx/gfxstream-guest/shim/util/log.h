/* RanchuWoA：mesa util/log.h 的最小替代（NDK liblog 实现）。 */
#pragma once

#include <android/log.h>
#include "util/macros.h"

#define mesa_loge(...) __android_log_print(ANDROID_LOG_ERROR, "gfxstream", __VA_ARGS__)
#define mesa_logw(...) __android_log_print(ANDROID_LOG_WARN, "gfxstream", __VA_ARGS__)
#define mesa_logi(...) __android_log_print(ANDROID_LOG_INFO, "gfxstream", __VA_ARGS__)
#define mesa_logd(...) __android_log_print(ANDROID_LOG_DEBUG, "gfxstream", __VA_ARGS__)
