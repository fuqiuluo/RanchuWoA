// allocator.cpp — android.hardware.graphics.allocator.IAllocator/default（AIDL V2）服务。
//
// 合成器客户端目标（COMPOSER_CLIENT_TARGET）且为 RGBA/RGBX/BGRA 时，缓冲区是 virtio-gpu 2D 资源
// 导出的 dma-buf，ranchu hwc3 可直接 drmModeAddFB2 扫描输出；其余缓冲区来自 DMA-BUF system heap
// （与常规 Android gralloc 相同，各进程映射无需额外 SELinux 规则），heap 不可用时才退回 memfd。
// 所有后备都可被 mapper 直接 mmap（CPU 渲染的 SwiftShader 依赖这一点）。
#include <aidl/android/hardware/graphics/allocator/AllocationError.h>
#include <aidl/android/hardware/graphics/allocator/BnAllocator.h>
#include <android/binder_manager.h>
#include <android/binder_process.h>
#include <android/log.h>
#include <drm/drm.h>
#include <drm/virtgpu_drm.h>
#include <fcntl.h>
#include <linux/dma-buf.h>
#include <linux/dma-heap.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>

#include "woa_gralloc.h"

#define LOG_TAG "woa-allocator"
#define ALOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)
#define ALOGW(...) __android_log_print(ANDROID_LOG_WARN, LOG_TAG, __VA_ARGS__)
#define ALOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)

using aidl::android::hardware::common::NativeHandle;
using aidl::android::hardware::graphics::allocator::AllocationError;
using aidl::android::hardware::graphics::allocator::AllocationResult;
using aidl::android::hardware::graphics::allocator::BnAllocator;
using aidl::android::hardware::graphics::allocator::BufferDescriptorInfo;

namespace woa {
namespace {

constexpr uint64_t kUsageClientTarget = 1ull << 12;  // BufferUsage::COMPOSER_CLIENT_TARGET

ndk::ScopedAStatus allocError(AllocationError e) {
    return ndk::ScopedAStatus::fromServiceSpecificError(static_cast<int32_t>(e));
}

// virtio-gpu render 节点：2D 资源创建 + PRIME 导出
class VirtioGpu {
  public:
    // 返回 dma-buf fd；失败返回 -1
    int createDmabuf(uint32_t virtioFormat, uint32_t width, uint32_t height, uint64_t size) {
        std::lock_guard<std::mutex> lock(mMutex);
        if (!openLocked()) return -1;
        drm_virtgpu_resource_create rc{};
        rc.target = 2;  // PIPE_TEXTURE_2D
        rc.format = virtioFormat;
        rc.bind = (1 << 1) | (1 << 3) | (1 << 18);  // RENDER_TARGET | SAMPLER_VIEW | SCANOUT（2D 模式下忽略）
        rc.width = width;
        rc.height = height;
        rc.depth = 1;
        rc.array_size = 1;
        rc.size = static_cast<uint32_t>(size);
        if (ioctl(mFd, DRM_IOCTL_VIRTGPU_RESOURCE_CREATE, &rc) != 0) {
            ALOGE("VIRTGPU_RESOURCE_CREATE %ux%u fmt=%u size=%llu 失败: %s", width, height,
                  virtioFormat, (unsigned long long)size, strerror(errno));
            return -1;
        }
        drm_prime_handle prime{};
        prime.handle = rc.bo_handle;
        prime.flags = DRM_CLOEXEC | DRM_RDWR;
        int fd = -1;
        if (ioctl(mFd, DRM_IOCTL_PRIME_HANDLE_TO_FD, &prime) == 0) {
            fd = prime.fd;
        } else {
            ALOGE("PRIME_HANDLE_TO_FD 失败: %s", strerror(errno));
        }
        drm_gem_close close_req{};
        close_req.handle = rc.bo_handle;
        ioctl(mFd, DRM_IOCTL_GEM_CLOSE, &close_req);  // dma-buf 持有对象引用
        return fd;
    }

  private:
    bool openLocked() {
        if (mFd >= 0) return true;
        if (mTried) return false;
        mTried = true;
        for (int i = 128; i < 192; i++) {
            std::string path = "/dev/dri/renderD" + std::to_string(i);
            int fd = open(path.c_str(), O_RDWR | O_CLOEXEC);
            if (fd < 0) continue;
            char name[64] = {};
            drm_version v{};
            v.name = name;
            v.name_len = sizeof(name) - 1;
            if (ioctl(fd, DRM_IOCTL_VERSION, &v) == 0 && strcmp(name, "virtio_gpu") == 0) {
                ALOGI("使用 virtio-gpu render 节点 %s", path.c_str());
                mFd = fd;
                return true;
            }
            close(fd);
        }
        ALOGW("没有 virtio-gpu render 节点，客户端目标退回 memfd（合成器将无法扫描输出）");
        return false;
    }

    std::mutex mMutex;
    int mFd = -1;
    bool mTried = false;
};

// 返回 dma-buf fd；heap 不存在或分配失败返回 -1
int createDmaHeap(uint64_t size, const std::string& name) {
    static const int heap = open("/dev/dma_heap/system", O_RDONLY | O_CLOEXEC);
    if (heap < 0) return -1;
    dma_heap_allocation_data a{};
    a.len = size;
    a.fd_flags = O_RDWR | O_CLOEXEC;
    if (ioctl(heap, DMA_HEAP_IOCTL_ALLOC, &a) != 0) {
        ALOGE("DMA_HEAP_IOCTL_ALLOC(%llu) 失败: %s", (unsigned long long)size, strerror(errno));
        return -1;
    }
    const int fd = static_cast<int>(a.fd);
    // 名字出现在 /sys/kernel/debug/dma_buf/bufinfo、dmabuf_dump 里，便于排查泄漏
    char label[DMA_BUF_NAME_LEN];
    snprintf(label, sizeof(label), "woa:%s", name.c_str());
    ioctl(fd, DMA_BUF_SET_NAME, label);
    return fd;
}

int createMemfd(uint64_t size) {
    int fd = static_cast<int>(syscall(__NR_memfd_create, "woa-gralloc", MFD_CLOEXEC));
    if (fd < 0) {
        ALOGE("memfd_create 失败: %s", strerror(errno));
        return -1;
    }
    if (ftruncate(fd, static_cast<off_t>(size)) != 0) {
        ALOGE("ftruncate(%llu) 失败: %s", (unsigned long long)size, strerror(errno));
        close(fd);
        return -1;
    }
    return fd;
}

class Allocator : public BnAllocator {
  public:
    ndk::ScopedAStatus allocate(const std::vector<uint8_t>&, int32_t, AllocationResult*) override {
        // IMapper 4.0 时代的描述符接口；本实现只配合 AIMapper v5 使用 allocate2
        return allocError(AllocationError::UNSUPPORTED);
    }

    ndk::ScopedAStatus allocate2(const BufferDescriptorInfo& d, int32_t count,
                                 AllocationResult* out) override {
        if (count <= 0 || count > 64 || d.width <= 0 || d.height <= 0 || d.layerCount < 0) {
            return allocError(AllocationError::BAD_DESCRIPTOR);
        }
        const auto format = static_cast<int32_t>(d.format);
        const auto usage = static_cast<uint64_t>(d.usage);
        Layout layout;
        if (!computeLayout(format, d.width, d.height, d.layerCount, usage, &layout)) {
            ALOGW("不支持: %dx%d fmt=%#x usage=%#llx layers=%d", d.width, d.height, format,
                  (unsigned long long)usage, d.layerCount);
            return allocError(AllocationError::UNSUPPORTED);
        }
        std::string name(reinterpret_cast<const char*>(d.name.data()),
                         strnlen(reinterpret_cast<const char*>(d.name.data()), d.name.size()));

        out->stride = static_cast<int32_t>(layout.pixel_stride);
        out->buffers.clear();
        for (int32_t i = 0; i < count; i++) {
            NativeHandle nh;
            if (!allocateOne(d, format, usage, name, layout, &nh)) {
                out->buffers.clear();
                return allocError(AllocationError::NO_RESOURCES);
            }
            out->buffers.push_back(std::move(nh));
        }
        return ndk::ScopedAStatus::ok();
    }

    ndk::ScopedAStatus isSupported(const BufferDescriptorInfo& d, bool* out) override {
        Layout layout;
        *out = d.width > 0 && d.height > 0 &&
               computeLayout(static_cast<int32_t>(d.format), d.width, d.height, d.layerCount,
                             static_cast<uint64_t>(d.usage), &layout);
        return ndk::ScopedAStatus::ok();
    }

    ndk::ScopedAStatus getIMapperLibrarySuffix(std::string* out) override {
        *out = "woa";
        return ndk::ScopedAStatus::ok();
    }

  private:
    bool allocateOne(const BufferDescriptorInfo& d, int32_t format, uint64_t usage,
                     const std::string& name, const Layout& layout, NativeHandle* out) {
        // 末尾留两行余量：SwiftShader 按 2x2 像素块光栅化、并按 8 字节读取打包像素，
        // 在缓冲区最后一行附近可能越界访问；页对齐后的余量太少时会踩到映射之外
        const uint64_t slack = 2ull * layout.plane_stride[0] + 256;
        const uint64_t dataSize = alignUp(layout.data_size + slack, kPageSize);
        const uint64_t reserved = alignUp(static_cast<uint64_t>(d.reservedSize), kPageSize);
        const uint64_t total = dataSize + kMetaPageSize + reserved;
        if (total > 0xffffffffull) return false;

        uint32_t backing = kBackingMemfd;
        int fd = -1;
        if ((usage & kUsageClientTarget) && layout.virtio_format != 0 &&
            layout.pixel_stride == static_cast<uint32_t>(d.width)) {
            fd = mGpu.createDmabuf(layout.virtio_format, d.width, d.height, total);
            if (fd >= 0) backing = kBackingVirtioGpu;
        }
        if (fd < 0) {
            fd = createDmaHeap(total, name);
            if (fd >= 0) backing = kBackingDmaHeap;
        }
        if (fd < 0) fd = createMemfd(total);
        if (fd < 0) return false;

        Handle h{};
        h.base.version = sizeof(native_handle_t);
        h.base.numFds = kNumFds;
        h.base.numInts = kNumInts;
        h.fds[0] = fd;
        for (int i = 1; i <= kMaxPlanes; i++) h.fds[i] = -1;
        for (int i = 0; i < layout.num_planes; i++) {
            h.strides[i] = layout.plane_stride[i];
            h.offsets[i] = layout.plane_offset[i];
            h.sizes[i] = layout.plane_size[i];
        }
        h.id = static_cast<uint32_t>(mNextId.load());
        h.width = static_cast<uint32_t>(d.width);
        h.height = static_cast<uint32_t>(d.height);
        h.format = layout.drm_fourcc;
        h.magic = kCrosMagic;
        h.pixel_stride = layout.pixel_stride;
        h.droid_format = layout.resolved_format;
        h.usage = static_cast<int64_t>(usage);
        h.num_planes = static_cast<uint32_t>(layout.num_planes);
        h.reserved_region_size = reserved;
        h.total_size = total;
        h.woa_magic = kWoaMagic;
        h.layer_count = d.layerCount > 0 ? static_cast<uint32_t>(d.layerCount) : 1;
        h.requested_format = format;
        h.backing = backing;
        h.buffer_id = (static_cast<uint64_t>(getpid()) << 32) | mNextId.fetch_add(1);
        h.data_size = dataSize;
        h.meta_offset = dataSize;

        if (!initSharedMeta(fd, h, name)) {
            close(fd);
            return false;
        }

        out->fds.clear();
        out->fds.emplace_back(fd);  // 所有权交给 NativeHandle，回复写出后关闭
        const int32_t* ints = reinterpret_cast<const int32_t*>(&h) +
                              sizeof(native_handle_t) / sizeof(int32_t) + kNumFds;
        out->ints.assign(ints, ints + kNumInts);
        __android_log_print(ANDROID_LOG_DEBUG, LOG_TAG,
                            "分配 #%llu %ux%u fmt=%#x->%#x usage=%#llx stride=%u size=%llu %s \"%s\"",
                            (unsigned long long)h.buffer_id, h.width, h.height, format,
                            layout.resolved_format, (unsigned long long)usage, h.pixel_stride,
                            (unsigned long long)total,
                            backing == kBackingVirtioGpu ? "virtio-gpu"
                            : backing == kBackingDmaHeap ? "dma-heap" : "memfd",
                            name.c_str());
        return true;
    }

    static bool initSharedMeta(int fd, const Handle& h, const std::string& name) {
        void* p = mmap(nullptr, kMetaPageSize, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
                       static_cast<off_t>(h.meta_offset));
        if (p == MAP_FAILED) {
            ALOGE("映射元数据页失败（backing=%u off=%llu）: %s", h.backing,
                  (unsigned long long)h.meta_offset, strerror(errno));
            return false;
        }
        auto* m = static_cast<SharedMeta*>(p);
        memset(m, 0, sizeof(*m));
        m->magic = kMetaMagic;
        m->version = 1;
        m->crop_count = h.num_planes;
        for (uint32_t i = 0; i < h.num_planes && i < kMaxPlanes; i++) {
            m->crop[i][2] = static_cast<int32_t>(h.width);
            m->crop[i][3] = static_cast<int32_t>(h.height);
        }
        strncpy(m->name, name.c_str(), sizeof(m->name) - 1);
        munmap(p, kMetaPageSize);
        return true;
    }

    VirtioGpu mGpu;
    std::atomic<uint64_t> mNextId{1};
};

}  // namespace
}  // namespace woa

int main() {
    ABinderProcess_setThreadPoolMaxThreadCount(4);
    ABinderProcess_startThreadPool();

    auto allocator = ndk::SharedRefBase::make<woa::Allocator>();
    const std::string instance = std::string(woa::Allocator::descriptor) + "/default";
    binder_status_t status = AServiceManager_addService(allocator->asBinder().get(), instance.c_str());
    if (status != STATUS_OK) {
        ALOGE("注册 %s 失败: %d", instance.c_str(), status);
        return 1;
    }
    ALOGI("%s 已注册（mapper 后缀 woa）", instance.c_str());
    ABinderProcess_joinThreadPool();
    return 1;
}
