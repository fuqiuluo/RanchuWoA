/* RanchuWoA：单文件 HAL 布局下的补充定义。
   - gl_client_context_t / gl2_client_context_t::setContextAccessor 的 weak 定义：
     AOSP 里入口表编在共享的 libGLES*v*_enc 里（强定义）；我们的 HAL 里入口表经
     gl.cpp/gl2.cpp 的 GET_CONTEXT（TLS）路径使用，不经过这两个访问器，
     HostConnection 的引用用 weak 定义满足链接即可。
   - kumquat 同步助手桩（VirtGpu.cpp 仅在 VIRTGPU_KUMQUAT 环境变量下调用，恒不触发）
   - android::base::GetProperty 的 NDK 实现（设备 vendor libbase 是平台 libc++ ABI，符号对不上） */
#include "VirtGpu.h"
#include "Sync.h"
#include "gl_client_context.h"
#include "gl2_client_context.h"

#include <sys/system_properties.h>
#include <string>

namespace gfxstream {

SyncHelper *kumquatCreateSyncHelper() { return nullptr; }

}  // namespace gfxstream

// VirtGpu.cpp（全局命名空间）在 VIRTGPU_KUMQUAT 下才调用，恒不触发
VirtGpuDevice *kumquatCreateVirtGpuDevice(enum VirtGpuCapset, int32_t) { return nullptr; }

__attribute__((weak)) void gl_client_context_t::setContextAccessor(CONTEXT_ACCESSOR_TYPE *f) {}
__attribute__((weak)) void gl2_client_context_t::setContextAccessor(CONTEXT_ACCESSOR_TYPE *f) {}

namespace android {
namespace base {

std::string GetProperty(const std::string &key, const std::string &default_value) {
    char value[PROP_VALUE_MAX] = {0};
    int len = __system_property_get(key.c_str(), value);
    if (len > 0) return std::string(value, len);
    return default_value;
}

}  // namespace base
}  // namespace android
