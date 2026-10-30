/* RanchuWoA：mesa util/u_process.h 的最小替代（真实 mesa src/util 已在 include 路径上时，
   本文件不会被优先命中；保留给没有检出 mesa util 的场合）。 */
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

#include <stdlib.h>

/* bionic 自带 getprogname；mesa 的 util_get_process_name 语义一致 */
static inline const char *util_get_process_name(void) { return getprogname(); }

#ifdef __cplusplus
}
#endif
