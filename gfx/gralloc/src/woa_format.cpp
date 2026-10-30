// woa_format.cpp — HAL 像素格式 → 平面布局 / DRM fourcc / virtio-gpu 格式。
#include "woa_gralloc.h"

#include <string.h>

#include <initializer_list>

namespace woa {
namespace {

constexpr uint32_t fourcc(char a, char b, char c, char d) {
    return static_cast<uint32_t>(a) | (static_cast<uint32_t>(b) << 8) |
           (static_cast<uint32_t>(c) << 16) | (static_cast<uint32_t>(d) << 24);
}

// android.hardware.graphics.common.PixelFormat
enum : int32_t {
    RGBA_8888 = 1,
    RGBX_8888 = 2,
    RGB_888 = 3,
    RGB_565 = 4,
    BGRA_8888 = 5,
    YCBCR_422_SP = 0x10,
    YCRCB_420_SP = 0x11,
    RGBA_FP16 = 0x16,
    RAW16 = 0x20,
    BLOB = 0x21,
    IMPLEMENTATION_DEFINED = 0x22,
    YCBCR_420_888 = 0x23,
    RGBA_1010102 = 0x2B,
    DEPTH_16 = 0x30,
    DEPTH_24 = 0x31,
    DEPTH_24_STENCIL_8 = 0x32,
    DEPTH_32F = 0x33,
    DEPTH_32F_STENCIL_8 = 0x34,
    STENCIL_8 = 0x35,
    YCBCR_P010 = 0x36,
    R_8 = 0x38,
    R_16_UINT = 0x39,
    RG_1616_UINT = 0x3a,
    Y8 = 0x20203859,
    Y16 = 0x20363159,
    YV12 = 0x32315659,
};

// android.hardware.graphics.common.BufferUsage
constexpr uint64_t kUsageVideoEncoder = 1ull << 16;
constexpr uint64_t kUsageCameraOutput = 1ull << 17;
constexpr uint64_t kUsageCameraInput = 1ull << 18;

// virtio_gpu_formats（include/uapi/linux/virtio_gpu.h），名字是内存字节序
constexpr uint32_t VIRTIO_GPU_FORMAT_B8G8R8A8_UNORM = 1;
constexpr uint32_t VIRTIO_GPU_FORMAT_R8G8B8A8_UNORM = 67;
constexpr uint32_t VIRTIO_GPU_FORMAT_R8G8B8X8_UNORM = 134;

PlaneDesc packed(std::initializer_list<Component> comps, int64_t incBits) {
    PlaneDesc p{};
    for (const auto& c : comps) p.components[p.num_components++] = c;
    p.sample_increment_bits = incBits;
    p.h_subsampling = 1;
    p.v_subsampling = 1;
    return p;
}

PlaneDesc chroma(std::initializer_list<Component> comps, int64_t incBits, int hs, int vs) {
    PlaneDesc p = packed(comps, incBits);
    p.h_subsampling = hs;
    p.v_subsampling = vs;
    return p;
}

int32_t resolveFormat(int32_t format, uint64_t usage) {
    if (format == IMPLEMENTATION_DEFINED) {
        // 相机走 YUV；其余（含视频编码输入面，由 GLES 渲染）都用 RGBX
        if (usage & (kUsageCameraOutput | kUsageCameraInput)) return YCBCR_420_888;
        (void)kUsageVideoEncoder;
        return RGBX_8888;
    }
    return format;
}

}  // namespace

bool planeDescsFor(int32_t f, PlaneDesc* planes, int* numPlanes) {
    *numPlanes = 1;
    switch (f) {
        case RGBA_8888:
            planes[0] = packed({{kCompR, 0, 8}, {kCompG, 8, 8}, {kCompB, 16, 8}, {kCompA, 24, 8}}, 32);
            return true;
        case RGBX_8888:
            planes[0] = packed({{kCompR, 0, 8}, {kCompG, 8, 8}, {kCompB, 16, 8}}, 32);
            return true;
        case BGRA_8888:
            planes[0] = packed({{kCompB, 0, 8}, {kCompG, 8, 8}, {kCompR, 16, 8}, {kCompA, 24, 8}}, 32);
            return true;
        case RGB_888:
            planes[0] = packed({{kCompR, 0, 8}, {kCompG, 8, 8}, {kCompB, 16, 8}}, 24);
            return true;
        case RGB_565:
            planes[0] = packed({{kCompB, 0, 5}, {kCompG, 5, 6}, {kCompR, 11, 5}}, 16);
            return true;
        case RGBA_FP16:
            planes[0] = packed({{kCompR, 0, 16}, {kCompG, 16, 16}, {kCompB, 32, 16}, {kCompA, 48, 16}}, 64);
            return true;
        case RGBA_1010102:
            planes[0] = packed({{kCompR, 0, 10}, {kCompG, 10, 10}, {kCompB, 20, 10}, {kCompA, 30, 2}}, 32);
            return true;
        case R_8:
            planes[0] = packed({{kCompR, 0, 8}}, 8);
            return true;
        case R_16_UINT:
            planes[0] = packed({{kCompR, 0, 16}}, 16);
            return true;
        case RG_1616_UINT:
            planes[0] = packed({{kCompR, 0, 16}, {kCompG, 16, 16}}, 32);
            return true;
        case Y8:
            planes[0] = packed({{kCompY, 0, 8}}, 8);
            return true;
        case Y16:
            planes[0] = packed({{kCompY, 0, 16}}, 16);
            return true;
        case RAW16:
            planes[0] = packed({{kCompRaw, 0, 16}}, 16);
            return true;
        case BLOB:
        case STENCIL_8:
            planes[0] = packed({{kCompRaw, 0, 8}}, 8);
            return true;
        case DEPTH_16:
            planes[0] = packed({{kCompRaw, 0, 16}}, 16);
            return true;
        case DEPTH_24:
        case DEPTH_24_STENCIL_8:
        case DEPTH_32F:
            planes[0] = packed({{kCompRaw, 0, 32}}, 32);
            return true;
        case DEPTH_32F_STENCIL_8:
            planes[0] = packed({{kCompRaw, 0, 64}}, 64);
            return true;
        case YCBCR_420_888:  // 落地为 NV12
            *numPlanes = 2;
            planes[0] = packed({{kCompY, 0, 8}}, 8);
            planes[1] = chroma({{kCompCb, 0, 8}, {kCompCr, 8, 8}}, 16, 2, 2);
            return true;
        case YCRCB_420_SP:  // NV21
            *numPlanes = 2;
            planes[0] = packed({{kCompY, 0, 8}}, 8);
            planes[1] = chroma({{kCompCr, 0, 8}, {kCompCb, 8, 8}}, 16, 2, 2);
            return true;
        case YCBCR_422_SP:  // NV16
            *numPlanes = 2;
            planes[0] = packed({{kCompY, 0, 8}}, 8);
            planes[1] = chroma({{kCompCb, 0, 8}, {kCompCr, 8, 8}}, 16, 2, 1);
            return true;
        case YV12:  // Y 平面 + Cr 平面 + Cb 平面
            *numPlanes = 3;
            planes[0] = packed({{kCompY, 0, 8}}, 8);
            planes[1] = chroma({{kCompCr, 0, 8}}, 8, 2, 2);
            planes[2] = chroma({{kCompCb, 0, 8}}, 8, 2, 2);
            return true;
        case YCBCR_P010:
            *numPlanes = 2;
            planes[0] = packed({{kCompY, 6, 10}}, 16);
            planes[1] = chroma({{kCompCb, 6, 10}, {kCompCr, 22, 10}}, 32, 2, 2);
            return true;
        default:
            return false;
    }
}

bool computeLayout(int32_t format, uint32_t width, uint32_t height, uint32_t layerCount,
                   uint64_t usage, Layout* out, bool hostGpu) {
    if (width == 0 || height == 0 || width > 16384 || height > 16384) return false;
    if (layerCount == 0) layerCount = 1;
    if (layerCount > 16) return false;

    Layout l{};
    l.resolved_format = resolveFormat(format, usage);
    if (!planeDescsFor(l.resolved_format, l.planes, &l.num_planes)) return false;

    switch (l.resolved_format) {
        case RGBA_8888: l.drm_fourcc = fourcc('A', 'B', '2', '4'); l.virtio_format = VIRTIO_GPU_FORMAT_R8G8B8A8_UNORM; break;
        case RGBX_8888: l.drm_fourcc = fourcc('X', 'B', '2', '4'); l.virtio_format = VIRTIO_GPU_FORMAT_R8G8B8X8_UNORM; break;
        case BGRA_8888: l.drm_fourcc = fourcc('A', 'R', '2', '4'); l.virtio_format = VIRTIO_GPU_FORMAT_B8G8R8A8_UNORM; break;
        case RGB_888: l.drm_fourcc = fourcc('B', 'G', '2', '4'); break;
        case RGB_565: l.drm_fourcc = fourcc('R', 'G', '1', '6'); break;
        case RGBA_FP16: l.drm_fourcc = fourcc('A', 'B', '4', 'H'); break;
        case RGBA_1010102: l.drm_fourcc = fourcc('A', 'B', '3', '0'); break;
        case R_8: case Y8: case BLOB: l.drm_fourcc = fourcc('R', '8', ' ', ' '); break;
        case R_16_UINT: case Y16: case RAW16: l.drm_fourcc = fourcc('R', '1', '6', ' '); break;
        case RG_1616_UINT: l.drm_fourcc = fourcc('G', 'R', '3', '2'); break;
        case YCBCR_420_888: l.drm_fourcc = fourcc('N', 'V', '1', '2'); break;
        case YCRCB_420_SP: l.drm_fourcc = fourcc('N', 'V', '2', '1'); break;
        case YCBCR_422_SP: l.drm_fourcc = fourcc('N', 'V', '1', '6'); break;
        case YV12: l.drm_fourcc = fourcc('Y', 'V', '1', '2'); break;
        case YCBCR_P010: l.drm_fourcc = fourcc('P', '0', '1', '0'); break;
        default: l.drm_fourcc = 0; break;  // 深度/模板：无对应 fourcc
    }

    const bool yuv = l.planes[0].components[0].type == kCompY && l.num_planes > 1;
    if (hostGpu) {
        // 宿主 gfxstream 的 virgl 格式（host/virtio_gpu_format_utils.h）。YUV 的色度平面宿主按
        // height/2 取整、stride 按 width 推算，奇数尺寸对不上，退回普通 dma-heap 缓冲。
        uint32_t vf = 0;
        switch (l.resolved_format) {
            case RGB_888: vf = 66; break;        // VIRGL_FORMAT_R8G8B8_UNORM
            case RGB_565: vf = 7; break;         // VIRGL_FORMAT_B5G6R5_UNORM
            case RGBA_FP16: vf = 94; break;      // VIRGL_FORMAT_R16G16B16A16_FLOAT
            case RGBA_1010102: vf = 8; break;    // VIRGL_FORMAT_R10G10B10A2_UNORM（gfxstream 的编号）
            case R_8: vf = 64; break;            // VIRGL_FORMAT_R8_UNORM
            case YCBCR_420_888: vf = 166; break; // VIRGL_FORMAT_NV12
            case YV12: vf = 163; break;          // VIRGL_FORMAT_YV12
            case YCBCR_P010: vf = 314; break;    // VIRGL_FORMAT_P010
            default: break;
        }
        if (vf && (!yuv || ((width | height) & 1) == 0)) {
            l.virtio_format = vf;
            l.host_layout = true;
        }
    }
    uint64_t offset = 0;
    for (int i = 0; i < l.num_planes; i++) {
        const PlaneDesc& p = l.planes[i];
        const uint32_t w = (width + p.h_subsampling - 1) / p.h_subsampling;
        const uint32_t h = (height + p.v_subsampling - 1) / p.v_subsampling;
        const uint64_t bitsPerSample = static_cast<uint64_t>(p.sample_increment_bits);
        uint64_t stride = (static_cast<uint64_t>(w) * bitsPerSample + 7) / 8;
        if (l.host_layout && yuv) {
            // 宿主布局：YV12 的 y_stride = ALIGN(width, 32)、c_stride = y_stride / 2（仍满足 Android
            // 对 YV12 的 16 对齐约定）；NV12/P010 紧排
            if (l.resolved_format == YV12) {
                stride = i == 0 ? alignUp(width, 32) : l.plane_stride[0] / 2;
            }
        } else if (l.resolved_format == YV12) {
            // YV12 约定：y_stride = ALIGN(width, 16)，c_stride = ALIGN(y_stride / 2, 16)
            stride = i == 0 ? alignUp(width, 16) : alignUp(l.plane_stride[0] / 2, 16);
        } else if (yuv) {
            stride = alignUp(stride, 16);
        }
        l.plane_stride[i] = static_cast<uint32_t>(stride);
        l.plane_offset[i] = static_cast<uint32_t>(offset);
        l.plane_size[i] = static_cast<uint32_t>(stride * h);
        l.plane_width[i] = w;
        l.plane_height[i] = h;
        offset += stride * h;
    }
    const uint64_t layerSize = offset;
    l.data_size = layerSize * layerCount;
    if (l.data_size == 0 || l.data_size > (1ull << 31)) return false;

    const uint64_t bppBits = static_cast<uint64_t>(l.planes[0].sample_increment_bits);
    l.pixel_stride = bppBits >= 8 ? static_cast<uint32_t>(l.plane_stride[0] * 8 / bppBits)
                                  : l.plane_stride[0];
    *out = l;
    return true;
}

}  // namespace woa
