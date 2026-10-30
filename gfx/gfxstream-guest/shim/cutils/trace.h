/* RanchuWoA：cutils/trace.h 的最小替代（ATRACE 空实现）。 */
#pragma once

#include <stdint.h>

#define ATRACE_TAG_GRAPHICS 0

#define ATRACE_NAME(name) []
#define ATRACE_CALL()
#define ATRACE_INT(name, value) atrace_int(ATRACE_TAG_GRAPHICS, (name), (value))
#define ATRACE_ENABLED() (0)

static inline void atrace_int(uint64_t, const char *, int32_t) {}
