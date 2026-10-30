/* RanchuWoA：mesa util/libsync.h 的最小替代——直接声明设备 libsync.so（LLNDK）的
   sync_fence 接口，结构体来自内核 UAPI（NDK sysroot 的 linux/sync_file.h）。 */
#pragma once

#include <stdint.h>
#include <linux/sync_file.h>

#ifdef __cplusplus
extern "C" {
#endif

int sync_wait(int fd, int timeout);
struct sync_file_info *sync_file_info(int fd);
void sync_file_info_free(struct sync_file_info *info);

#ifdef __cplusplus
}
#endif
