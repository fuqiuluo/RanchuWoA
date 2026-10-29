// allocatord.cpp — 自研 gralloc5 AIDL allocator 服务（android.hardware.graphics.allocator.IAllocator/default）。
// 手写 AIDL parcel 编解码（无需生成的桩代码）。缓冲区 = memfd + nullgfx 自描述 handle。
#include <android/binder_ibinder.h>
#include <android/binder_parcel.h>

// NDK 头缺这几个平台 API（运行时由系统 libbinder_ndk.so 提供，链接用本地桩）
extern "C" {
binder_status_t AServiceManager_addService(AIBinder* binder, const char* instance);
void ABinderProcess_setThreadPoolMaxThreadCount(uint32_t maxThreads);
void ABinderProcess_startThreadPool();
void ABinderProcess_joinThreadPool();
}

#include <android/log.h>
#include <sys/syscall.h>
#ifndef SYS_memfd_create
#define SYS_memfd_create 279
#endif
#define ALOGE(...) __android_log_print(ANDROID_LOG_ERROR, "nullgfxd", __VA_ARGS__)
#define ALOGI(...) __android_log_print(ANDROID_LOG_INFO, "nullgfxd", __VA_ARGS__)
#define ALOGW(...) __android_log_print(ANDROID_LOG_WARN, "nullgfxd", __VA_ARGS__)
#include <sys/mman.h>
#include <unistd.h>

#include <atomic>
#include <cstdint>
#include <cstring>
#include <memory>
#include <vector>

namespace {

constexpr const char* kDescriptor = "android.hardware.graphics.allocator.IAllocator";
constexpr const char* kInstance = "android.hardware.graphics.allocator.IAllocator/default";
constexpr const char* kSuffix = "nullgfx";
constexpr int kNumInts = 12;  // 与 mapper_nullgfx 的 HI_* 布局一致

constexpr int32_t HAL_PIXEL_FORMAT_BLOB = 33;
constexpr int32_t HAL_PIXEL_FORMAT_RGBA_8888 = 1;
constexpr int32_t HAL_PIXEL_FORMAT_RGBX_8888 = 2;
constexpr int32_t HAL_PIXEL_FORMAT_RGB_888 = 3;
constexpr int32_t HAL_PIXEL_FORMAT_RGB_565 = 4;

std::atomic<uint32_t> gNextId{1};

struct DescriptorInfo {
    int32_t width;
    int32_t height;
    int32_t layerCount;
    int32_t format;
    uint64_t usage;
    uint64_t reservedSize;
};

int bytes_per_pixel(int32_t fmt) {
    switch (fmt) {
        case HAL_PIXEL_FORMAT_RGBA_8888:
        case HAL_PIXEL_FORMAT_RGBX_8888: return 4;
        case HAL_PIXEL_FORMAT_RGB_888: return 3;
        case HAL_PIXEL_FORMAT_RGB_565: return 2;
        default: return 1;  // BLOB 等
    }
}

// 解析 allocate2 入参 parcel 原始字节：
// BufferDescriptorInfo: byte[128] name（定长，无长度前缀）+ i32 w + i32 h + i32 layers
//                       + i32 format + i64 usage + i64 reserved; 之后 i32 count
bool parseAllocate2(const std::vector<uint8_t>& raw, DescriptorInfo* out, int32_t* count) {
    if (raw.size() < 128 + 4 * 3 + 4 + 8 + 8 + 4) return false;
    const uint8_t* p = raw.data();
    p += 128;
    auto rd32 = [&]() {
        int32_t v;
        memcpy(&v, p, 4);
        p += 4;
        return v;
    };
    auto rd64 = [&]() {
        int64_t v;
        memcpy(&v, p, 8);
        p += 8;
        return (uint64_t)v;
    };
    out->width = rd32();
    out->height = rd32();
    out->layerCount = rd32();
    out->format = rd32();
    out->usage = rd64();
    out->reservedSize = rd64();
    *count = rd32();
    return out->width > 0 && out->height > 0 && out->layerCount >= 0 && *count > 0;
}

struct Alloc {
    int memfd;
    uint32_t stride;
    int ints[kNumInts];
};

// 分配一个缓冲区
Alloc allocOne(const DescriptorInfo& d) {
    uint32_t stride, size;
    if (d.format == HAL_PIXEL_FORMAT_BLOB) {
        stride = (uint32_t)d.width;
        size = (uint32_t)d.width * (uint32_t)d.height;
    } else {
        stride = (uint32_t)(((uint64_t)d.width * bytes_per_pixel(d.format) + 15) & ~15ull);
        uint32_t layers = d.layerCount ? (uint32_t)d.layerCount : 1;
        size = stride * (uint32_t)d.height * layers;
    }
    if ((uint64_t)size < d.reservedSize) size = (uint32_t)d.reservedSize;
    if (size == 0) size = 4096;

    int fd = (int)syscall(SYS_memfd_create, "nullgfx", MFD_CLOEXEC);
    if (fd < 0) {
        ALOGE("nullgfx-alloc: memfd_create: %s", strerror(errno));
        fd = -1;
    } else if (ftruncate(fd, (off_t)size) != 0) {
        ALOGE("nullgfx-alloc: ftruncate: %s", strerror(errno));
        close(fd);
        fd = -1;
    }
    uint32_t id = gNextId.fetch_add(1);
    // HI_* 布局（见 mapper_nullgfx.cpp）
    Alloc a{fd, stride,
            {0x4E554C47, d.width, d.height, (int)stride, d.format,
             d.layerCount ? d.layerCount : 1, (int32_t)(d.usage & 0xffffffffu),
             (int32_t)(d.usage >> 32), (int)size, (int)id, 0, 0}};
    return a;
}

// 写 AllocationResult 回复：[i32 异常=0][i32 stride][数组 count][NativeHandle...]，
// NativeHandle = [fds count + fd对象们][ints count + i32们]
binder_status_t writeAllocResult(AParcel* out, const DescriptorInfo& d, int32_t count) {
    std::vector<Alloc> allocs;
    allocs.reserve(count);
    for (int i = 0; i < count; i++) allocs.push_back(allocOne(d));
    if (allocs.empty() || allocs[0].memfd < 0) return STATUS_FAILED_TRANSACTION;

    if (AParcel_writeInt32(out, 0) != STATUS_OK) return STATUS_FAILED_TRANSACTION;  // 无异常
    if (AParcel_writeInt32(out, (int32_t)allocs[0].stride) != STATUS_OK)
        return STATUS_FAILED_TRANSACTION;
    if (AParcel_writeInt32(out, count) != STATUS_OK) return STATUS_FAILED_TRANSACTION;
    for (const Alloc& a : allocs) {
        if (AParcel_writeInt32(out, 1) != STATUS_OK) return STATUS_FAILED_TRANSACTION;  // 1 个 fd
        if (AParcel_writeParcelFileDescriptor(out, a.memfd) != STATUS_OK)
            return STATUS_FAILED_TRANSACTION;  // binder 会复制 fd，本进程继续持有用于日志
        if (AParcel_writeInt32(out, kNumInts) != STATUS_OK) return STATUS_FAILED_TRANSACTION;
        for (int i = 0; i < kNumInts; i++) {
            if (AParcel_writeInt32(out, a.ints[i]) != STATUS_OK) return STATUS_FAILED_TRANSACTION;
        }
        close(a.memfd);  // fd 已被 binder 复制给对端
    }
    ALOGI("nullgfx-alloc: %dx%d fmt=%d usage=%#llx count=%d stride=%u", d.width, d.height,
          d.format, (unsigned long long)d.usage, count, allocs[0].stride);
    return STATUS_OK;
}

binder_status_t onTransact(AIBinder*, transaction_code_t code, const AParcel* in, AParcel* out) {
    ALOGI("nullgfxd: onTransact code=%u inSize=%d pos=%d", (unsigned)code, AParcel_getDataSize(in), AParcel_getDataPosition(in));
    switch (code) {
        case 1: {  // allocate(byte[] descriptor, int count) — legacy(gralloc4 路径)，不支持
            ALOGW("nullgfx: legacy allocate() 不支持");
            return STATUS_FAILED_TRANSACTION;
        }
        case 2: {  // allocate2(BufferDescriptorInfo, int count)
            std::vector<uint8_t> raw;
            {
                int32_t total = AParcel_getDataSize(in);
                if (total <= 0 || total > (1 << 20)) return STATUS_FAILED_TRANSACTION;
                raw.resize(total);
                if (AParcel_marshal(in, raw.data(), 0, (size_t)total) != STATUS_OK)
                    return STATUS_FAILED_TRANSACTION;
                int32_t pos = AParcel_getDataPosition(in);
                if (pos > 0 && pos < total) raw.erase(raw.begin(), raw.begin() + pos);
            }
            DescriptorInfo d{};
            int32_t count = 0;
            if (!parseAllocate2(raw, &d, &count)) {
                ALOGE("nullgfx: allocate2 参数解析失败 (%zu bytes)", raw.size());
                return STATUS_FAILED_TRANSACTION;
            }
            return writeAllocResult(out, d, count);
        }
        case 3: {  // isSupported(BufferDescriptorInfo) -> bool
            if (AParcel_writeInt32(out, 0) != STATUS_OK) return STATUS_FAILED_TRANSACTION;
            if (AParcel_writeBool(out, true) != STATUS_OK) return STATUS_FAILED_TRANSACTION;
            return STATUS_OK;
        }
        case 4: {  // getIMapperLibrarySuffix() -> String
            if (AParcel_writeInt32(out, 0) != STATUS_OK) return STATUS_FAILED_TRANSACTION;
            if (AParcel_writeString(out, kSuffix, strlen(kSuffix)) != STATUS_OK)
                return STATUS_FAILED_TRANSACTION;
            return STATUS_OK;
        }
        case 16777215: {  // AIDL getInterfaceVersion()（LAST_CALL_TRANSACTION）
            if (AParcel_writeInt32(out, 0) != STATUS_OK) return STATUS_FAILED_TRANSACTION;
            if (AParcel_writeInt32(out, 2) != STATUS_OK) return STATUS_FAILED_TRANSACTION;  // V2
            return STATUS_OK;
        }
        case 16777214: {  // AIDL getHash()
            if (AParcel_writeInt32(out, 0) != STATUS_OK) return STATUS_FAILED_TRANSACTION;
            const char* hash = "nullgfx";
            if (AParcel_writeString(out, hash, strlen(hash)) != STATUS_OK)
                return STATUS_FAILED_TRANSACTION;
            return STATUS_OK;
        }
        default:
            return STATUS_UNKNOWN_TRANSACTION;
    }
}

}  // namespace

int main() {
    AIBinder_Class* cls = AIBinder_Class_define(
            kDescriptor,
            [](void*) -> void* { return nullptr; },
            [](void*) {},
            onTransact);
    if (!cls) {
        ALOGE("nullgfx: AIBinder_Class_create 失败");
        return 1;
    }
    AIBinder* binder = AIBinder_new(cls, nullptr);
    binder_status_t st = AServiceManager_addService(binder, kInstance);
    if (st != STATUS_OK) {
        ALOGE("nullgfx: addService(%s) 失败: %d", kInstance, st);
        return 1;
    }
    ALOGI("nullgfx: %s 已注册", kInstance);
    ABinderProcess_setThreadPoolMaxThreadCount(8);
    ABinderProcess_startThreadPool();
    ABinderProcess_joinThreadPool();
    return 0;
}
