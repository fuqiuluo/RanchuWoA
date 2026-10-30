// woa_gralloc.h — RanchuWoA gralloc（IAllocator V2 + stable-C IMapper v5）共用定义。
//
// 缓冲区句柄的前缀与 Android 15 minigbm 的 cros_gralloc_handle 二进制一致：
// vendor 里的 ranchu hwc3（ClientFrameComposer/DrmClient）直接把句柄当 cros_gralloc_handle 读
// fds[0]/width/height/format/strides[0]/offsets[0]，再 drmPrimeFDToHandle + drmModeAddFB2。
// 前缀之后是 woa 自己的扩展字段，numInts 覆盖全部，所以 clone/binder 传输都会带上。
//
// fd 内容布局：[像素数据 data_size（页对齐）][共享元数据页 4096][保留区 reserved_region_size（页对齐）]
// 共享元数据页让 dataspace/blend/crop/HDR 元数据在所有导入该缓冲区的进程之间可见。
#pragma once

#include <cutils/native_handle.h>
#include <stddef.h>
#include <stdint.h>

namespace woa {

constexpr int kMaxPlanes = 4;
constexpr uint32_t kCrosMagic = 0xABCDEFAB;     // minigbm cros_gralloc_handle 的 magic
constexpr uint32_t kWoaMagic = 0x574F4147;      // 'WOAG'
constexpr uint32_t kMetaMagic = 0x574F414D;     // 'WOAM'
constexpr size_t kPageSize = 4096;
constexpr size_t kMetaPageSize = 4096;

enum Backing : uint32_t {
    kBackingMemfd = 0,      // 仅作后备：memfd 被标成分配器私有的 tmpfs 类型，SELinux enforcing 下客户端无权映射
    kBackingVirtioGpu = 1,  // virtio-gpu 2D 资源导出的 dma-buf，可被 DRM 扫描输出
    kBackingDmaHeap = 2,    // DMA-BUF system heap（/dev/dma_heap/system），常规缓冲区
};

struct Handle {
    native_handle_t base;
    // ---- 与 cros_gralloc_handle（Android 15）一致的前缀 ----
    int32_t fds[kMaxPlanes + 1];  // 只用 fds[0]；其余 4 个槽位按 int 传输
    uint32_t strides[kMaxPlanes];
    uint32_t offsets[kMaxPlanes];
    uint32_t sizes[kMaxPlanes];
    uint32_t id;
    uint32_t width;
    uint32_t height;
    uint32_t format;  // DRM fourcc
    uint32_t tiling;
    uint64_t format_modifier;
    uint64_t use_flags;
    uint32_t magic;
    uint32_t pixel_stride;
    int32_t droid_format;
    int64_t usage;
    uint32_t num_planes;
    uint64_t reserved_region_size;
    uint64_t total_size;
    // ---- woa 扩展 ----
    uint32_t woa_magic;
    uint32_t layer_count;
    int32_t requested_format;  // 申请时的 PixelFormat（IMPLEMENTATION_DEFINED 等原样保留）
    uint32_t backing;
    uint64_t buffer_id;
    uint64_t data_size;    // 像素区字节数（页对齐）
    uint64_t meta_offset;  // 共享元数据页在 fd 中的偏移
} __attribute__((packed));

constexpr int kNumFds = 1;
constexpr int kNumInts = (sizeof(Handle) - sizeof(native_handle_t)) / sizeof(int32_t) - kNumFds;
static_assert((sizeof(Handle) - sizeof(native_handle_t)) % sizeof(int32_t) == 0);

// 共享元数据页（位于 fd 的 meta_offset 处）
struct SharedMeta {
    uint32_t magic;
    uint32_t version;
    int32_t dataspace;
    int32_t blend_mode;
    uint32_t crop_count;
    int32_t crop[kMaxPlanes][4];  // left, top, right, bottom
    uint32_t has_smpte2086;
    float smpte2086[10];  // RGB/白点 xy（8 个）+ max/min 亮度
    uint32_t has_cta861_3;
    float cta861_3[2];
    uint32_t smpte2094_40_size;  // 0 表示无；上限 kMaxDynamicMeta
    uint32_t smpte2094_10_size;
    char name[128];
    uint8_t smpte2094_40[1536];
    uint8_t smpte2094_10[1536];
};
constexpr uint32_t kMaxDynamicMeta = 1536;
static_assert(sizeof(SharedMeta) <= kMetaPageSize);

// 平面分量（与 PlaneLayoutComponentType 数值一致）
enum ComponentType : int64_t {
    kCompY = 1 << 0,
    kCompCb = 1 << 1,
    kCompCr = 1 << 2,
    kCompR = 1 << 10,
    kCompG = 1 << 11,
    kCompB = 1 << 12,
    kCompRaw = 1 << 20,
    kCompA = 1 << 30,
};

struct Component {
    int64_t type;
    int64_t offset_bits;
    int64_t size_bits;
};

struct PlaneDesc {
    int num_components;
    Component components[4];
    int64_t sample_increment_bits;
    int h_subsampling;
    int v_subsampling;
};

// 解析后的格式与内存布局
struct Layout {
    int32_t resolved_format;  // IMPLEMENTATION_DEFINED / YCbCr_420_888 解析后的 HAL 格式
    uint32_t drm_fourcc;
    uint32_t virtio_format;   // 0 = 不能作为 virtio-gpu 资源（2D 模式只有 32bpp；3D 见 host_layout）
    bool host_layout;         // 按宿主 gfxstream 的线性布局排的非 32bpp 格式（只在 virtio-gpu 有 3D 时）
    uint32_t pixel_stride;    // AllocationResult.stride / STRIDE 元数据（像素）
    int num_planes;
    PlaneDesc planes[kMaxPlanes];
    uint32_t plane_stride[kMaxPlanes];  // 字节
    uint32_t plane_offset[kMaxPlanes];
    uint32_t plane_size[kMaxPlanes];
    uint32_t plane_width[kMaxPlanes];   // 采样数
    uint32_t plane_height[kMaxPlanes];
    uint64_t data_size;                 // 未对齐的像素区总字节
};

// 计算布局；不支持的组合返回 false
// hostGpu：virtio-gpu 带 3D（gfxstream 模式）。此时宿主能建 ColorBuffer 的其余格式（YUV、565、FP16…）
// 也给出 virgl 格式，并按宿主 transfer 的线性布局排平面（与宿主逐字节一致，CPU 读写才能经 transfer 同步）。
bool computeLayout(int32_t format, uint32_t width, uint32_t height, uint32_t layerCount,
                   uint64_t usage, Layout* out, bool hostGpu = false);

// 按句柄重建平面描述（mapper 用）
bool planeDescsFor(int32_t resolvedFormat, PlaneDesc* planes, int* numPlanes);

inline uint64_t alignUp(uint64_t v, uint64_t a) { return (v + a - 1) / a * a; }

inline const Handle* asHandle(const native_handle_t* h) {
    if (!h || h->version != sizeof(native_handle_t) || h->numFds != kNumFds ||
        h->numInts != kNumInts) {
        return nullptr;
    }
    auto* wh = reinterpret_cast<const Handle*>(h);
    if (wh->magic != kCrosMagic || wh->woa_magic != kWoaMagic) return nullptr;
    return wh;
}

}  // namespace woa
