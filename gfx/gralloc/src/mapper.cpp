// mapper.cpp — stable-C AIMapper v5 实现（mapper.woa.so，由 libui 经 sphal 命名空间加载）。
//
// 所有不可变信息都在句柄里；可变元数据（dataspace/blend/crop/HDR）在 fd 的共享元数据页里，
// 因此任何导入同一缓冲区的进程看到的是同一份。整个 fd（像素区 + 元数据页 + 保留区）按需从偏移 0
// 映射一次，freeBuffer 时释放——guest 内核（android15-6.6）对 virtio-gpu 导出的 dma-buf 做带偏移的
// mmap 时忽略偏移、总是映射到第 0 页，元数据页不能单独按 meta_offset 映射。
#include <aidl/android/hardware/graphics/common/ChromaSiting.h>
#include <aidl/android/hardware/graphics/common/Compression.h>
#include <aidl/android/hardware/graphics/common/Interlaced.h>
#include <android/hardware/graphics/mapper/IMapper.h>
#include <android/hardware/graphics/mapper/utils/IMapperMetadataTypes.h>
#include <android/log.h>
#include <drm/drm.h>
#include <drm/virtgpu_drm.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

#include "woa_gralloc.h"

#define LOG_TAG "woa-mapper"
#define ALOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)
#define ALOGW(...) __android_log_print(ANDROID_LOG_WARN, LOG_TAG, __VA_ARGS__)
#define ALOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)

using namespace ::android::hardware::graphics::mapper;
using ::aidl::android::hardware::graphics::common::ChromaSiting;
using ::aidl::android::hardware::graphics::common::Compression;
using ::aidl::android::hardware::graphics::common::Interlaced;

namespace woa {
namespace {

constexpr const char* kStandardName = "android.hardware.graphics.common.StandardMetadataType";

struct Imported {
    Handle* handle = nullptr;  // 本进程的克隆（fds[0] 已 dup）
    uint8_t* base = nullptr;   // 整个 fd 的映射（total_size 字节）；data/meta/reserved 都指向其中
    void* data = nullptr;
    SharedMeta* meta = nullptr;
    void* reserved = nullptr;
    int lockCount = 0;
    uint32_t gem = 0;         // 本进程 virtio-gpu render 节点上的 GEM 句柄（按需导入）
    bool cpuWritten = false;  // 当前这轮 lock 带写用途：最后一次 unlock 时上传宿主
};

// 全局状态故意不析构：进程退出时静态析构会和 binder 线程上的 freeBuffer 赛跑（screencap
// 退出时 ScreenCaptureResults 在 binder 线程析构 → freeBuffer 锁已销毁的 mutex → FORTIFY abort）。
std::mutex& gMutex = *new std::mutex;
std::unordered_map<const native_handle_t*, std::unique_ptr<Imported>>& gBuffers =
        *new std::unordered_map<const native_handle_t*, std::unique_ptr<Imported>>;

// gfxstream 模式（virtio-gpu 带 3D）下 virtio-gpu 缓冲的真身是宿主 ColorBuffer，guest 页只是
// backing：CPU 读之前 TRANSFER_FROM_HOST，CPU 写完 TRANSFER_TO_HOST（与 minigbm 的 virgl 后端
// 相同）。否则 screencap、ImageReader、lockCanvas 等 CPU 路径读到/写进的都是与屏幕无关的 guest 页。
// 2D 模式（SwiftShader）没有 transfer 3D，也不需要：宿主图像由 RESOURCE_FLUSH 从 backing 拷。
class Xfer {
  public:
    static Xfer& get() {
        static Xfer& x = *new Xfer;  // 不析构，理由同 gMutex
        return x;
    }

    // 导入 dma-buf 得到 GEM 句柄（同一 dma-buf 在本进程只有一个句柄，按引用计数关闭）
    uint32_t acquire(int dmabuf) {
        std::lock_guard<std::mutex> lock(mMutex);
        if (!openLocked()) return 0;
        drm_prime_handle prime{};
        prime.fd = dmabuf;
        if (ioctl(mFd, DRM_IOCTL_PRIME_FD_TO_HANDLE, &prime) != 0) {
            ALOGW("PRIME_FD_TO_HANDLE 失败: %s", strerror(errno));
            return 0;
        }
        mRefs[prime.handle]++;
        return prime.handle;
    }

    void release(uint32_t gem) {
        if (!gem) return;
        std::lock_guard<std::mutex> lock(mMutex);
        auto it = mRefs.find(gem);
        if (it == mRefs.end() || --it->second > 0) return;
        mRefs.erase(it);
        drm_gem_close req{};
        req.handle = gem;
        ioctl(mFd, DRM_IOCTL_GEM_CLOSE, &req);
    }

    // 整个 2D 资源双向传输并等待完成
    void transfer(uint32_t gem, const Handle* h, bool toHost) {
        if (!gem) return;
        drm_virtgpu_3d_transfer_to_host t{};  // 与 transfer_from_host 布局相同
        t.bo_handle = gem;
        t.box.w = h->width;
        t.box.h = h->height;
        t.box.d = 1;
        // 非 blob 资源内核要求 stride/layer_stride 为 0（布局由资源本身决定）
        const unsigned long req =
                toHost ? DRM_IOCTL_VIRTGPU_TRANSFER_TO_HOST : DRM_IOCTL_VIRTGPU_TRANSFER_FROM_HOST;
        if (ioctl(mFd, req, &t) != 0) {
            ALOGW("TRANSFER_%s_HOST 失败: %s", toHost ? "TO" : "FROM", strerror(errno));
            return;
        }
        drm_virtgpu_3d_wait w{};
        w.handle = gem;
        while (ioctl(mFd, DRM_IOCTL_VIRTGPU_WAIT, &w) != 0 && (errno == EINTR || errno == EBUSY)) {
        }
    }

  private:
    bool openLocked() {
        if (mTried) return mFd >= 0;
        mTried = true;
        for (int i = 128; i < 192; i++) {
            std::string path = "/dev/dri/renderD" + std::to_string(i);
            int fd = open(path.c_str(), O_RDWR | O_CLOEXEC);
            if (fd < 0) continue;
            char name[64] = {};
            drm_version v{};
            v.name = name;
            v.name_len = sizeof(name) - 1;
            int has3d = 0;
            drm_virtgpu_getparam gp{};
            gp.param = VIRTGPU_PARAM_3D_FEATURES;
            gp.value = reinterpret_cast<uintptr_t>(&has3d);
            if (ioctl(fd, DRM_IOCTL_VERSION, &v) == 0 && strcmp(name, "virtio_gpu") == 0 &&
                ioctl(fd, DRM_IOCTL_VIRTGPU_GETPARAM, &gp) == 0 && has3d) {
                mFd = fd;
                return true;
            }
            close(fd);
            break;  // 只有一块 virtio-gpu；没有 3D 就是 2D 模式
        }
        return false;
    }

    std::mutex mMutex;
    int mFd = -1;
    bool mTried = false;
    std::unordered_map<uint32_t, int> mRefs;
};

constexpr uint64_t kCpuReadMask = 0xf;    // BufferUsage::CPU_READ_MASK
constexpr uint64_t kCpuWriteMask = 0xf0;  // BufferUsage::CPU_WRITE_MASK

Imported* findLocked(buffer_handle_t b) {
    auto it = gBuffers.find(b);
    return it == gBuffers.end() ? nullptr : it->second.get();
}

void waitFence(int fd) {
    if (fd < 0) return;
    pollfd p{fd, POLLIN, 0};
    while (poll(&p, 1, -1) < 0 && (errno == EINTR || errno == EAGAIN)) {
    }
    close(fd);
}

void* mapRange(int fd, uint64_t offset, uint64_t size) {
    void* p = mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, static_cast<off_t>(offset));
    if (p == MAP_FAILED) {
        ALOGE("mmap(off=%llu size=%llu) 失败: %s", (unsigned long long)offset,
              (unsigned long long)size, strerror(errno));
        return nullptr;
    }
    return p;
}

// 调用方持 gMutex
uint8_t* baseOf(Imported* b) {
    if (!b->base) b->base = static_cast<uint8_t*>(mapRange(b->handle->fds[0], 0, b->handle->total_size));
    return b->base;
}

SharedMeta* metaOf(Imported* b) {
    if (!b->meta) {
        uint8_t* base = baseOf(b);
        if (!base) return nullptr;
        b->meta = reinterpret_cast<SharedMeta*>(base + b->handle->meta_offset);
        if (b->meta->magic != kMetaMagic) {
            ALOGW("缓冲区 %llu 的元数据页 magic 不对", (unsigned long long)b->handle->buffer_id);
        }
    }
    return b->meta;
}

std::vector<PlaneLayout> planeLayoutsOf(const Handle* h) {
    std::vector<PlaneLayout> out;
    PlaneDesc descs[kMaxPlanes];
    int n = 0;
    if (!planeDescsFor(h->droid_format, descs, &n)) return out;
    for (int i = 0; i < n && i < static_cast<int>(h->num_planes); i++) {
        PlaneLayout pl;
        for (int c = 0; c < descs[i].num_components; c++) {
            PlaneLayoutComponent comp;
            comp.type.name = "android.hardware.graphics.common.PlaneLayoutComponentType";
            comp.type.value = descs[i].components[c].type;
            comp.offsetInBits = descs[i].components[c].offset_bits;
            comp.sizeInBits = descs[i].components[c].size_bits;
            pl.components.push_back(std::move(comp));
        }
        pl.offsetInBytes = h->offsets[i];
        pl.sampleIncrementInBits = descs[i].sample_increment_bits;
        pl.strideInBytes = h->strides[i];
        pl.widthInSamples = (h->width + descs[i].h_subsampling - 1) / descs[i].h_subsampling;
        pl.heightInSamples = (h->height + descs[i].v_subsampling - 1) / descs[i].v_subsampling;
        pl.totalSizeInBytes = h->sizes[i];
        pl.horizontalSubsampling = descs[i].h_subsampling;
        pl.verticalSubsampling = descs[i].v_subsampling;
        out.push_back(std::move(pl));
    }
    return out;
}

bool isYuv(const Handle* h) {
    PlaneDesc descs[kMaxPlanes];
    int n = 0;
    return planeDescsFor(h->droid_format, descs, &n) && descs[0].components[0].type == kCompY;
}

template <StandardMetadataType T>
int32_t encode(const typename StandardMetadata<T>::value_type& v, void* dest, size_t size) {
    return StandardMetadata<T>::value::encode(v, dest, size);
}

int32_t getStandard(Imported* b, int64_t type, void* dest, size_t size) {
    const Handle* h = b->handle;
    switch (static_cast<StandardMetadataType>(type)) {
        case StandardMetadataType::BUFFER_ID:
            return encode<StandardMetadataType::BUFFER_ID>(h->buffer_id, dest, size);
        case StandardMetadataType::NAME: {
            SharedMeta* m = metaOf(b);
            std::string name = m ? std::string(m->name, strnlen(m->name, sizeof(m->name))) : "";
            return encode<StandardMetadataType::NAME>(name, dest, size);
        }
        case StandardMetadataType::WIDTH:
            return encode<StandardMetadataType::WIDTH>(h->width, dest, size);
        case StandardMetadataType::HEIGHT:
            return encode<StandardMetadataType::HEIGHT>(h->height, dest, size);
        case StandardMetadataType::LAYER_COUNT:
            return encode<StandardMetadataType::LAYER_COUNT>(h->layer_count, dest, size);
        case StandardMetadataType::PIXEL_FORMAT_REQUESTED:
            return encode<StandardMetadataType::PIXEL_FORMAT_REQUESTED>(
                    static_cast<PixelFormat>(h->requested_format), dest, size);
        case StandardMetadataType::PIXEL_FORMAT_FOURCC:
            return encode<StandardMetadataType::PIXEL_FORMAT_FOURCC>(h->format, dest, size);
        case StandardMetadataType::PIXEL_FORMAT_MODIFIER:
            return encode<StandardMetadataType::PIXEL_FORMAT_MODIFIER>(h->format_modifier, dest, size);
        case StandardMetadataType::USAGE:
            return encode<StandardMetadataType::USAGE>(static_cast<BufferUsage>(h->usage), dest, size);
        case StandardMetadataType::ALLOCATION_SIZE:
            return encode<StandardMetadataType::ALLOCATION_SIZE>(h->data_size, dest, size);
        case StandardMetadataType::PROTECTED_CONTENT:
            return encode<StandardMetadataType::PROTECTED_CONTENT>(
                    (h->usage & (1 << 14)) ? 1 : 0, dest, size);
        case StandardMetadataType::COMPRESSION: {
            ExtendableType t{"android.hardware.graphics.common.Compression",
                             static_cast<int64_t>(Compression::NONE)};
            return encode<StandardMetadataType::COMPRESSION>(t, dest, size);
        }
        case StandardMetadataType::INTERLACED: {
            ExtendableType t{"android.hardware.graphics.common.Interlaced",
                             static_cast<int64_t>(Interlaced::NONE)};
            return encode<StandardMetadataType::INTERLACED>(t, dest, size);
        }
        case StandardMetadataType::CHROMA_SITING: {
            ExtendableType t{"android.hardware.graphics.common.ChromaSiting",
                             static_cast<int64_t>(isYuv(h) ? ChromaSiting::SITED_INTERSTITIAL
                                                           : ChromaSiting::NONE)};
            return encode<StandardMetadataType::CHROMA_SITING>(t, dest, size);
        }
        case StandardMetadataType::PLANE_LAYOUTS:
            return encode<StandardMetadataType::PLANE_LAYOUTS>(planeLayoutsOf(h), dest, size);
        case StandardMetadataType::CROP: {
            std::vector<Rect> crops;
            SharedMeta* m = metaOf(b);
            const uint32_t n = m ? std::min<uint32_t>(m->crop_count, kMaxPlanes) : 0;
            for (uint32_t i = 0; i < n; i++) {
                crops.push_back(Rect{m->crop[i][0], m->crop[i][1], m->crop[i][2], m->crop[i][3]});
            }
            return encode<StandardMetadataType::CROP>(crops, dest, size);
        }
        case StandardMetadataType::DATASPACE: {
            SharedMeta* m = metaOf(b);
            return encode<StandardMetadataType::DATASPACE>(
                    static_cast<Dataspace>(m ? m->dataspace : 0), dest, size);
        }
        case StandardMetadataType::BLEND_MODE: {
            SharedMeta* m = metaOf(b);
            return encode<StandardMetadataType::BLEND_MODE>(
                    static_cast<BlendMode>(m ? m->blend_mode : 0), dest, size);
        }
        case StandardMetadataType::SMPTE2086: {
            SharedMeta* m = metaOf(b);
            std::optional<Smpte2086> v;
            if (m && m->has_smpte2086) {
                const float* f = m->smpte2086;
                v = Smpte2086{XyColor{f[0], f[1]}, XyColor{f[2], f[3]}, XyColor{f[4], f[5]},
                              XyColor{f[6], f[7]}, f[8], f[9]};
            }
            return encode<StandardMetadataType::SMPTE2086>(v, dest, size);
        }
        case StandardMetadataType::CTA861_3: {
            SharedMeta* m = metaOf(b);
            std::optional<Cta861_3> v;
            if (m && m->has_cta861_3) v = Cta861_3{m->cta861_3[0], m->cta861_3[1]};
            return encode<StandardMetadataType::CTA861_3>(v, dest, size);
        }
        case StandardMetadataType::SMPTE2094_40: {
            SharedMeta* m = metaOf(b);
            std::optional<std::vector<uint8_t>> v;
            if (m && m->smpte2094_40_size) {
                v.emplace(m->smpte2094_40, m->smpte2094_40 + std::min(m->smpte2094_40_size, kMaxDynamicMeta));
            }
            return encode<StandardMetadataType::SMPTE2094_40>(v, dest, size);
        }
        case StandardMetadataType::SMPTE2094_10: {
            SharedMeta* m = metaOf(b);
            std::optional<std::vector<uint8_t>> v;
            if (m && m->smpte2094_10_size) {
                v.emplace(m->smpte2094_10, m->smpte2094_10 + std::min(m->smpte2094_10_size, kMaxDynamicMeta));
            }
            return encode<StandardMetadataType::SMPTE2094_10>(v, dest, size);
        }
        case StandardMetadataType::STRIDE:
            return encode<StandardMetadataType::STRIDE>(h->pixel_stride, dest, size);
        default:
            return -AIMAPPER_ERROR_UNSUPPORTED;
    }
}

template <StandardMetadataType T>
std::optional<typename StandardMetadata<T>::value_type> decode(const void* p, size_t size) {
    auto v = StandardMetadata<T>::value::decode(p, size);
    if (!v.has_value()) return std::nullopt;
    return std::move(*v);
}

AIMapper_Error setStandard(Imported* b, int64_t type, const void* p, size_t size) {
    SharedMeta* m = metaOf(b);
    if (!m) return AIMAPPER_ERROR_NO_RESOURCES;
    switch (static_cast<StandardMetadataType>(type)) {
        case StandardMetadataType::DATASPACE: {
            auto v = decode<StandardMetadataType::DATASPACE>(p, size);
            if (!v) return AIMAPPER_ERROR_BAD_VALUE;
            m->dataspace = static_cast<int32_t>(*v);
            return AIMAPPER_ERROR_NONE;
        }
        case StandardMetadataType::BLEND_MODE: {
            auto v = decode<StandardMetadataType::BLEND_MODE>(p, size);
            if (!v) return AIMAPPER_ERROR_BAD_VALUE;
            m->blend_mode = static_cast<int32_t>(*v);
            return AIMAPPER_ERROR_NONE;
        }
        case StandardMetadataType::CROP: {
            auto v = decode<StandardMetadataType::CROP>(p, size);
            if (!v || v->size() > kMaxPlanes) return AIMAPPER_ERROR_BAD_VALUE;
            m->crop_count = static_cast<uint32_t>(v->size());
            for (size_t i = 0; i < v->size(); i++) {
                m->crop[i][0] = (*v)[i].left;
                m->crop[i][1] = (*v)[i].top;
                m->crop[i][2] = (*v)[i].right;
                m->crop[i][3] = (*v)[i].bottom;
            }
            return AIMAPPER_ERROR_NONE;
        }
        case StandardMetadataType::SMPTE2086: {
            auto v = decode<StandardMetadataType::SMPTE2086>(p, size);
            if (!v) return AIMAPPER_ERROR_BAD_VALUE;
            m->has_smpte2086 = v->has_value();
            if (v->has_value()) {
                const Smpte2086& s = **v;
                const float f[10] = {s.primaryRed.x, s.primaryRed.y, s.primaryGreen.x,
                                     s.primaryGreen.y, s.primaryBlue.x, s.primaryBlue.y,
                                     s.whitePoint.x, s.whitePoint.y, s.maxLuminance,
                                     s.minLuminance};
                memcpy(m->smpte2086, f, sizeof(f));
            }
            return AIMAPPER_ERROR_NONE;
        }
        case StandardMetadataType::CTA861_3: {
            auto v = decode<StandardMetadataType::CTA861_3>(p, size);
            if (!v) return AIMAPPER_ERROR_BAD_VALUE;
            m->has_cta861_3 = v->has_value();
            if (v->has_value()) {
                m->cta861_3[0] = (*v)->maxContentLightLevel;
                m->cta861_3[1] = (*v)->maxFrameAverageLightLevel;
            }
            return AIMAPPER_ERROR_NONE;
        }
        case StandardMetadataType::SMPTE2094_40:
        case StandardMetadataType::SMPTE2094_10: {
            const bool is40 = static_cast<StandardMetadataType>(type) == StandardMetadataType::SMPTE2094_40;
            auto v = is40 ? decode<StandardMetadataType::SMPTE2094_40>(p, size)
                          : decode<StandardMetadataType::SMPTE2094_10>(p, size);
            if (!v) return AIMAPPER_ERROR_BAD_VALUE;
            uint8_t* dst = is40 ? m->smpte2094_40 : m->smpte2094_10;
            uint32_t* dstSize = is40 ? &m->smpte2094_40_size : &m->smpte2094_10_size;
            if (!v->has_value()) {
                *dstSize = 0;
            } else {
                if ((*v)->size() > kMaxDynamicMeta) return AIMAPPER_ERROR_BAD_VALUE;
                memcpy(dst, (*v)->data(), (*v)->size());
                *dstSize = static_cast<uint32_t>((*v)->size());
            }
            return AIMAPPER_ERROR_NONE;
        }
        default:
            return AIMAPPER_ERROR_UNSUPPORTED;
    }
}

#define DESC(type, settable) \
    {{kStandardName, static_cast<int64_t>(StandardMetadataType::type)}, nullptr, true, settable, {}}

const AIMapper_MetadataTypeDescription kDescriptions[] = {
        DESC(BUFFER_ID, false),
        DESC(NAME, false),
        DESC(WIDTH, false),
        DESC(HEIGHT, false),
        DESC(LAYER_COUNT, false),
        DESC(PIXEL_FORMAT_REQUESTED, false),
        DESC(PIXEL_FORMAT_FOURCC, false),
        DESC(PIXEL_FORMAT_MODIFIER, false),
        DESC(USAGE, false),
        DESC(ALLOCATION_SIZE, false),
        DESC(PROTECTED_CONTENT, false),
        DESC(COMPRESSION, false),
        DESC(INTERLACED, false),
        DESC(CHROMA_SITING, false),
        DESC(PLANE_LAYOUTS, false),
        DESC(CROP, true),
        DESC(DATASPACE, true),
        DESC(BLEND_MODE, true),
        DESC(SMPTE2086, true),
        DESC(CTA861_3, true),
        DESC(SMPTE2094_40, true),
        DESC(SMPTE2094_10, true),
        DESC(STRIDE, false),
};

void dumpOne(Imported* b, AIMapper_DumpBufferCallback cb, void* ctx) {
    std::vector<uint8_t> buf(4096);
    for (const auto& d : kDescriptions) {
        int32_t n = getStandard(b, d.metadataType.value, buf.data(), buf.size());
        if (n > static_cast<int32_t>(buf.size())) {
            buf.resize(n);
            n = getStandard(b, d.metadataType.value, buf.data(), buf.size());
        }
        if (n > 0) cb(ctx, d.metadataType, buf.data(), static_cast<size_t>(n));
    }
}

// ---------------- AIMapperV5 入口 ----------------

AIMapper_Error importBuffer(const native_handle_t* raw, buffer_handle_t* out) {
    const Handle* src = asHandle(raw);
    if (!src || src->fds[0] < 0) return AIMAPPER_ERROR_BAD_BUFFER;
    auto* copy = static_cast<Handle*>(malloc(sizeof(Handle)));
    if (!copy) return AIMAPPER_ERROR_NO_RESOURCES;
    memcpy(copy, src, sizeof(Handle));
    copy->fds[0] = fcntl(src->fds[0], F_DUPFD_CLOEXEC, 0);
    if (copy->fds[0] < 0) {
        ALOGE("importBuffer: dup 失败: %s", strerror(errno));
        free(copy);
        return AIMAPPER_ERROR_NO_RESOURCES;
    }
    auto imp = std::make_unique<Imported>();
    imp->handle = copy;
    std::lock_guard<std::mutex> lock(gMutex);
    auto* nh = reinterpret_cast<native_handle_t*>(copy);  // base 在偏移 0，malloc 对齐
    gBuffers[nh] = std::move(imp);
    *out = nh;
    return AIMAPPER_ERROR_NONE;
}

AIMapper_Error freeBuffer(buffer_handle_t buffer) {
    std::unique_ptr<Imported> b;
    {
        std::lock_guard<std::mutex> lock(gMutex);
        auto it = gBuffers.find(buffer);
        if (it == gBuffers.end()) return AIMAPPER_ERROR_BAD_BUFFER;
        b = std::move(it->second);
        gBuffers.erase(it);
    }
    Xfer::get().release(b->gem);
    if (b->base) munmap(b->base, b->handle->total_size);
    close(b->handle->fds[0]);
    free(b->handle);
    return AIMAPPER_ERROR_NONE;
}

AIMapper_Error getTransportSize(buffer_handle_t buffer, uint32_t* outNumFds, uint32_t* outNumInts) {
    std::lock_guard<std::mutex> lock(gMutex);
    if (!findLocked(buffer)) return AIMAPPER_ERROR_BAD_BUFFER;
    *outNumFds = kNumFds;
    *outNumInts = kNumInts;
    return AIMAPPER_ERROR_NONE;
}

AIMapper_Error lockBuffer(buffer_handle_t buffer, uint64_t cpuUsage, ARect region, int acquireFence,
                          void** outData) {
    // 不按分配时的 usage 校验：CPU 渲染（SwiftShader）会锁住未声明 CPU usage 的缓冲区，一律放行
    Imported* b;
    {
        std::lock_guard<std::mutex> lock(gMutex);
        b = findLocked(buffer);
    }
    if (!b) {
        if (acquireFence >= 0) close(acquireFence);
        return AIMAPPER_ERROR_BAD_BUFFER;
    }
    if (region.left < 0 || region.top < 0 || region.right < region.left ||
        region.bottom < region.top) {
        if (acquireFence >= 0) close(acquireFence);
        return AIMAPPER_ERROR_BAD_VALUE;
    }
    waitFence(acquireFence);
    bool fromHost = false;
    uint32_t gem = 0;
    {
        std::lock_guard<std::mutex> lock(gMutex);
        if (!b->data) {
            b->data = baseOf(b);
            if (!b->data) return AIMAPPER_ERROR_NO_RESOURCES;
        }
        if (b->handle->backing == kBackingVirtioGpu && (cpuUsage & (kCpuReadMask | kCpuWriteMask))) {
            if (!b->gem) b->gem = Xfer::get().acquire(b->handle->fds[0]);
            gem = b->gem;
            fromHost = (cpuUsage & kCpuReadMask) != 0;
            if (cpuUsage & kCpuWriteMask) b->cpuWritten = true;
        }
        b->lockCount++;
        *outData = b->data;
    }
    if (fromHost) Xfer::get().transfer(gem, b->handle, false);
    return AIMAPPER_ERROR_NONE;
}

AIMapper_Error unlockBuffer(buffer_handle_t buffer, int* releaseFence) {
    Imported* b;
    bool toHost = false;
    {
        std::lock_guard<std::mutex> lock(gMutex);
        b = findLocked(buffer);
        if (!b || b->lockCount <= 0) return AIMAPPER_ERROR_BAD_BUFFER;
        if (--b->lockCount == 0 && b->cpuWritten) {
            toHost = true;
            b->cpuWritten = false;
        }
    }
    if (toHost) Xfer::get().transfer(b->gem, b->handle, true);
    *releaseFence = -1;
    return AIMAPPER_ERROR_NONE;
}

AIMapper_Error flushOrReread(buffer_handle_t buffer) {
    std::lock_guard<std::mutex> lock(gMutex);
    Imported* b = findLocked(buffer);
    return (b && b->lockCount > 0) ? AIMAPPER_ERROR_NONE : AIMAPPER_ERROR_BAD_BUFFER;
}

int32_t getStandardMetadata(buffer_handle_t buffer, int64_t type, void* dest, size_t size) {
    std::lock_guard<std::mutex> lock(gMutex);
    Imported* b = findLocked(buffer);
    if (!b) return -AIMAPPER_ERROR_BAD_BUFFER;
    return getStandard(b, type, dest, size);
}

int32_t getMetadata(buffer_handle_t buffer, AIMapper_MetadataType type, void* dest, size_t size) {
    if (strcmp(type.name, kStandardName) != 0) return -AIMAPPER_ERROR_UNSUPPORTED;
    return getStandardMetadata(buffer, type.value, dest, size);
}

AIMapper_Error setStandardMetadata(buffer_handle_t buffer, int64_t type, const void* p, size_t size) {
    std::lock_guard<std::mutex> lock(gMutex);
    Imported* b = findLocked(buffer);
    if (!b) return AIMAPPER_ERROR_BAD_BUFFER;
    return setStandard(b, type, p, size);
}

AIMapper_Error setMetadata(buffer_handle_t buffer, AIMapper_MetadataType type, const void* p, size_t size) {
    if (strcmp(type.name, kStandardName) != 0) return AIMAPPER_ERROR_UNSUPPORTED;
    return setStandardMetadata(buffer, type.value, p, size);
}

AIMapper_Error listSupportedMetadataTypes(const AIMapper_MetadataTypeDescription** outList,
                                          size_t* outCount) {
    *outList = kDescriptions;
    *outCount = sizeof(kDescriptions) / sizeof(kDescriptions[0]);
    return AIMAPPER_ERROR_NONE;
}

AIMapper_Error dumpBuffer(buffer_handle_t buffer, AIMapper_DumpBufferCallback cb, void* ctx) {
    std::lock_guard<std::mutex> lock(gMutex);
    Imported* b = findLocked(buffer);
    if (!b) return AIMAPPER_ERROR_BAD_BUFFER;
    dumpOne(b, cb, ctx);
    return AIMAPPER_ERROR_NONE;
}

AIMapper_Error dumpAllBuffers(AIMapper_BeginDumpBufferCallback begin, AIMapper_DumpBufferCallback cb,
                              void* ctx) {
    std::lock_guard<std::mutex> lock(gMutex);
    for (auto& [handle, b] : gBuffers) {
        begin(ctx);
        dumpOne(b.get(), cb, ctx);
    }
    return AIMAPPER_ERROR_NONE;
}

AIMapper_Error getReservedRegion(buffer_handle_t buffer, void** outRegion, uint64_t* outSize) {
    std::lock_guard<std::mutex> lock(gMutex);
    Imported* b = findLocked(buffer);
    if (!b) return AIMAPPER_ERROR_BAD_BUFFER;
    const uint64_t size = b->handle->reserved_region_size;
    if (size == 0) {
        *outRegion = nullptr;
        *outSize = 0;
        return AIMAPPER_ERROR_NONE;
    }
    if (!b->reserved) {
        uint8_t* base = baseOf(b);
        if (!base) return AIMAPPER_ERROR_NO_RESOURCES;
        b->reserved = base + b->handle->meta_offset + kMetaPageSize;
    }
    *outRegion = b->reserved;
    *outSize = size;
    return AIMAPPER_ERROR_NONE;
}

AIMapper gMapper = {
        .version = AIMAPPER_VERSION_5,
        .v5 =
                {
                        .importBuffer = importBuffer,
                        .freeBuffer = freeBuffer,
                        .getTransportSize = getTransportSize,
                        .lock = lockBuffer,
                        .unlock = unlockBuffer,
                        .flushLockedBuffer = flushOrReread,
                        .rereadLockedBuffer = flushOrReread,
                        .getMetadata = getMetadata,
                        .getStandardMetadata = getStandardMetadata,
                        .setMetadata = setMetadata,
                        .setStandardMetadata = setStandardMetadata,
                        .listSupportedMetadataTypes = listSupportedMetadataTypes,
                        .dumpBuffer = dumpBuffer,
                        .dumpAllBuffers = dumpAllBuffers,
                        .getReservedRegion = getReservedRegion,
                },
};

}  // namespace
}  // namespace woa

extern "C" __attribute__((visibility("default"))) AIMapper_Error AIMapper_loadIMapper(
        AIMapper* _Nullable* _Nonnull outImplementation) {
    *outImplementation = &woa::gMapper;
    return AIMAPPER_ERROR_NONE;
}
