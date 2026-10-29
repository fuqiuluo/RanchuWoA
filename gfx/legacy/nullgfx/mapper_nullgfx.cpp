// mapper_nullgfx.cpp — 自研 host 无关 gralloc（stable-C IMapper5）。
// 缓冲区 = memfd + 自描述 native_handle（1 fd + 12 ints），CPU lock = mmap。
// RanchuWoA：SF/SwiftShader/hwui 全 CPU 渲染路径，绕开 goldfish gralloc 的宿主依赖。
#include <android/hardware/graphics/mapper/IMapper.h>

#include <cutils/native_handle.h>
#include <android/log.h>
#define ALOGE(...) __android_log_print(ANDROID_LOG_ERROR, "nullgfx", __VA_ARGS__)
#define ALOGI(...) __android_log_print(ANDROID_LOG_INFO, "nullgfx", __VA_ARGS__)
#define ALOGW(...) __android_log_print(ANDROID_LOG_WARN, "nullgfx", __VA_ARGS__)
#include <sys/mman.h>
#include <unistd.h>

#include <atomic>
#include <cstring>
#include <mutex>
#include <unordered_map>

namespace {

constexpr int kMagic = 0x4E554C47;  // "NULG"
// handle ints 布局（numFds=1, numInts=12）
enum HandleInts {
    HI_MAGIC = 0,
    HI_WIDTH = 1,
    HI_HEIGHT = 2,
    HI_STRIDE = 3,  // 字节
    HI_FORMAT = 4,
    HI_LAYERS = 5,
    HI_USAGE_LO = 6,
    HI_USAGE_HI = 7,
    HI_SIZE = 8,  // 字节
    HI_ID = 9,
    HI_DATASPACE = 10,
    HI_TOTAL = 12
};

std::mutex gLock;
std::unordered_map<const native_handle_t*, void*> gMappings;
std::atomic<uint32_t> gNextId{1000};

constexpr const char* kStdMetaName = "android.hardware.graphics.common.StandardMetadataType";
constexpr int64_t kStdMetaNameLen = 51;

// StandardMetadataType 值（StandardMetadataType.aidl）
enum MetaType {
    MT_BUFFER_ID = 1,
    MT_NAME = 2,
    MT_WIDTH = 3,
    MT_HEIGHT = 4,
    MT_LAYER_COUNT = 5,
    MT_PIXEL_FORMAT_REQUESTED = 6,
    MT_PIXEL_FORMAT_FOURCC = 7,
    MT_PIXEL_FORMAT_MODIFIER = 8,
    MT_USAGE = 9,
    MT_ALLOCATION_SIZE = 10,
    MT_PROTECTED_CONTENT = 11,
    MT_COMPRESSION = 12,
    MT_INTERLACED = 13,
    MT_CHROMA_SITING = 14,
    MT_PLANE_LAYOUTS = 15,
    MT_CROP = 16,
    MT_DATASPACE = 17,
    MT_BLEND_MODE = 18,
    MT_STRIDE = 23,
};

bool validHandle(buffer_handle_t h) {
    return h && h->numFds == 1 && h->numInts == HI_TOTAL && h->data[0] >= 0 &&
           h->data[1 + HI_MAGIC] == kMagic;
}

constexpr int32_t HAL_PIXEL_FORMAT_RGBA_8888 = 1;
constexpr int32_t HAL_PIXEL_FORMAT_RGBX_8888 = 2;
constexpr int32_t HAL_PIXEL_FORMAT_RGB_888 = 3;
constexpr int32_t HAL_PIXEL_FORMAT_RGB_565 = 4;
constexpr int32_t HAL_PIXEL_FORMAT_BLOB = 33;

uint32_t fourcc_of(int32_t fmt) {
    switch (fmt) {
        case HAL_PIXEL_FORMAT_RGBA_8888: return 0x34324241;  // 'AB24'
        case HAL_PIXEL_FORMAT_RGBX_8888: return 0x34324258;  // 'XB24'
        case HAL_PIXEL_FORMAT_RGB_565: return 0x36314752;    // 'RG16'
        default: return 0x20203852;                          // 'R\0\0'
    }
}

int bytes_per_pixel(int32_t fmt) {
    switch (fmt) {
        case HAL_PIXEL_FORMAT_RGBA_8888:
        case HAL_PIXEL_FORMAT_RGBX_8888: return 4;
        case HAL_PIXEL_FORMAT_RGB_888: return 3;
        case HAL_PIXEL_FORMAT_RGB_565: return 2;
        default: return 1;
    }
}

// 元数据编码格式：[i64 nameLen][name][i64 typeValue][payload]
struct MetaWriter {
    void* dest;
    size_t cap;
    size_t need = 0;
    bool fit;

    MetaWriter(void* d, size_t c, int64_t type)
        : dest(d), cap(c), fit(d != nullptr) {
        need += sizeof(int64_t) + (size_t)kStdMetaNameLen + sizeof(int64_t);
        if (fit && cap >= need) {
            char* p = (char*)dest;
            int64_t len = kStdMetaNameLen;
            memcpy(p, &len, 8);
            memcpy(p + 8, kStdMetaName, kStdMetaNameLen);
            memcpy(p + 8 + kStdMetaNameLen, &type, 8);
        } else if (cap < need) {
            fit = false;
        }
    }

    void* reserve(size_t n) {
        void* ret = nullptr;
        if (fit && cap >= need + n) {
            ret = (char*)dest + need;
        } else if (cap < need + n) {
            fit = false;
        }
        need += n;
        return ret;
    }
    MetaWriter& w(const void* p, size_t n) {
        if (void* d = reserve(n)) memcpy(d, p, n);
        return *this;
    }
    template <typename T>
    MetaWriter& w(const T& v) {
        return w(&v, sizeof(T));
    }
    MetaWriter& wStr(const char* s) {
        int64_t len = strlen(s);
        w(len);
        return w(s, (size_t)len);
    }
    int32_t finish() { return (int32_t)need; }
};

struct NullGfxMapper {
    AIMapper_Error importBuffer(const native_handle_t* handle,
                                buffer_handle_t* outBufferHandle) {
        if (!validHandle(handle)) {
            ALOGE("nullgfx: importBuffer bad handle");
            return AIMAPPER_ERROR_BAD_BUFFER;
        }
        native_handle_t* cloned = native_handle_clone(handle);
        if (!cloned) return AIMAPPER_ERROR_NO_RESOURCES;
        *outBufferHandle = cloned;
        return AIMAPPER_ERROR_NONE;
    }

    AIMapper_Error freeBuffer(buffer_handle_t buffer) {
        if (!validHandle(buffer)) return AIMAPPER_ERROR_BAD_BUFFER;
        {
            std::lock_guard<std::mutex> lk(gLock);
            auto it = gMappings.find(buffer);
            if (it != gMappings.end()) {
                munmap(it->second, (size_t)buffer->data[1 + HI_SIZE]);
                gMappings.erase(it);
            }
        }
        native_handle_close(const_cast<native_handle_t*>(buffer));
        native_handle_delete(const_cast<native_handle_t*>(buffer));
        return AIMAPPER_ERROR_NONE;
    }

    AIMapper_Error getTransportSize(buffer_handle_t buffer, uint32_t* outNumFds,
                                    uint32_t* outNumInts) {
        if (!validHandle(buffer)) return AIMAPPER_ERROR_BAD_BUFFER;
        *outNumFds = buffer->numFds;
        *outNumInts = buffer->numInts;
        return AIMAPPER_ERROR_NONE;
    }

    AIMapper_Error lock(buffer_handle_t buffer, uint64_t cpuUsage, ARect, int acquireFence,
                        void** outData) {
        if (acquireFence >= 0) close(acquireFence);
        if (!validHandle(buffer)) return AIMAPPER_ERROR_BAD_BUFFER;
        if (cpuUsage == 0) return AIMAPPER_ERROR_BAD_VALUE;
        std::lock_guard<std::mutex> lk(gLock);
        auto it = gMappings.find(buffer);
        if (it != gMappings.end()) {
            *outData = it->second;
            return AIMAPPER_ERROR_NONE;
        }
        size_t size = (size_t)buffer->data[1 + HI_SIZE];
        void* addr = mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, buffer->data[0], 0);
        if (addr == MAP_FAILED) {
            ALOGE("nullgfx: mmap(%zu) failed: %s", size, strerror(errno));
            return AIMAPPER_ERROR_NO_RESOURCES;
        }
        gMappings[buffer] = addr;
        *outData = addr;
        return AIMAPPER_ERROR_NONE;
    }

    AIMapper_Error unlock(buffer_handle_t buffer, int* releaseFence) {
        if (!validHandle(buffer)) return AIMAPPER_ERROR_BAD_BUFFER;
        *releaseFence = -1;
        return AIMAPPER_ERROR_NONE;
    }

    AIMapper_Error flushLockedBuffer(buffer_handle_t) { return AIMAPPER_ERROR_NONE; }
    AIMapper_Error rereadLockedBuffer(buffer_handle_t) { return AIMAPPER_ERROR_NONE; }

    int32_t getMetadata(buffer_handle_t buffer, AIMapper_MetadataType type, void* dest,
                        size_t size) {
        if (strcmp(type.name, kStdMetaName) == 0)
            return getStandardMetadata(buffer, type.value, dest, size);
        return -AIMAPPER_ERROR_UNSUPPORTED;
    }

    int32_t getStandardMetadata(buffer_handle_t buffer, int64_t type, void* dest, size_t size) {
        if (!validHandle(buffer)) return -AIMAPPER_ERROR_BAD_BUFFER;
        const int* in = buffer->data + 1;
        switch ((MetaType)type) {
            case MT_BUFFER_ID: {
                MetaWriter w(dest, size, type);
                w.w((uint64_t)(uint32_t)in[HI_ID]);
                return w.finish();
            }
            case MT_NAME: {
                char name[40];
                snprintf(name, sizeof(name), "nullgfx#%u", (uint32_t)in[HI_ID]);
                MetaWriter w(dest, size, type);
                w.wStr(name);
                return w.finish();
            }
            case MT_WIDTH: return putUInt(dest, size, type, (uint32_t)in[HI_WIDTH]);
            case MT_HEIGHT: return putUInt(dest, size, type, (uint32_t)in[HI_HEIGHT]);
            case MT_LAYER_COUNT: return putUInt(dest, size, type, (uint32_t)in[HI_LAYERS]);
            case MT_PIXEL_FORMAT_REQUESTED: {
                MetaWriter w(dest, size, type);
                w.w((int32_t)in[HI_FORMAT]);
                return w.finish();
            }
            case MT_PIXEL_FORMAT_FOURCC:
                return putUInt(dest, size, type, fourcc_of(in[HI_FORMAT]));
            case MT_PIXEL_FORMAT_MODIFIER:
            case MT_PROTECTED_CONTENT:
                return putUInt(dest, size, type, (uint64_t)0);
            case MT_USAGE: {
                uint64_t usage = ((uint64_t)(uint32_t)in[HI_USAGE_HI] << 32) |
                                 (uint32_t)in[HI_USAGE_LO];
                return putUInt(dest, size, type, usage);
            }
            case MT_ALLOCATION_SIZE: return putUInt(dest, size, type, (uint64_t)(uint32_t)in[HI_SIZE]);
            case MT_DATASPACE: {
                MetaWriter w(dest, size, type);
                w.w((int64_t)in[HI_DATASPACE]);
                return w.finish();
            }
            case MT_STRIDE: return putUInt(dest, size, type, (uint64_t)(uint32_t)in[HI_STRIDE]);
            case MT_COMPRESSION:
            case MT_INTERLACED:
            case MT_CHROMA_SITING:
            case MT_BLEND_MODE: {
                const char* ns =
                        type == MT_COMPRESSION
                                ? "android.hardware.graphics.common.Compression"
                        : type == MT_INTERLACED
                                ? "android.hardware.graphics.common.Interlaced"
                        : type == MT_CHROMA_SITING
                                ? "android.hardware.graphics.common.ChromaSiting"
                                : "android.hardware.graphics.common.BlendMode";
                MetaWriter w(dest, size, type);
                w.wStr(ns);
                w.w((int64_t)0);  // None
                return w.finish();
            }
            case MT_CROP: {
                MetaWriter w(dest, size, type);
                int32_t crop[4] = {0, 0, in[HI_WIDTH], in[HI_HEIGHT]};
                w.w((int64_t)1);
                w.w(crop, sizeof(crop));
                return w.finish();
            }
            case MT_PLANE_LAYOUTS: {
                MetaWriter w(dest, size, type);
                int bpp = bytes_per_pixel(in[HI_FORMAT]);
                bool hasAlpha = in[HI_FORMAT] == HAL_PIXEL_FORMAT_RGBA_8888;
                int comps = hasAlpha ? 4 : 3;
                w.w((int64_t)1);  // 1 个 plane
                w.w((int64_t)comps);
                static const char* compNames[4] = {
                        "android.hardware.graphics.common.PlaneLayoutComponent.R",
                        "android.hardware.graphics.common.PlaneLayoutComponent.G",
                        "android.hardware.graphics.common.PlaneLayoutComponent.B",
                        "android.hardware.graphics.common.PlaneLayoutComponent.A",
                };
                for (int i = 0; i < comps; i++) {
                    w.wStr(compNames[i]);
                    w.w((int64_t)0);          // ExtendableType.value
                    w.w((int64_t)(8 * i));    // offsetInBits
                    w.w((int64_t)8);          // sizeInBits
                }
                int64_t stride = in[HI_STRIDE];
                w.w((int64_t)0);                            // offsetInBytes
                w.w((int64_t)(bpp * 8));                    // sampleIncrementInBits
                w.w(stride);                                // strideInBytes
                w.w((int64_t)in[HI_WIDTH]);                 // widthInSamples
                w.w((int64_t)in[HI_HEIGHT]);                // heightInSamples
                w.w(stride * in[HI_HEIGHT]);                // totalSizeInBytes
                w.w((int64_t)1);                            // horizontalSubsampling
                w.w((int64_t)1);                            // verticalSubsampling
                return w.finish();
            }
            default:
                return -AIMAPPER_ERROR_UNSUPPORTED;
        }
    }

    static int32_t putUInt(void* dest, size_t size, int64_t type, uint64_t v) {
        MetaWriter w(dest, size, type);
        w.w(v);
        return w.finish();
    }

    AIMapper_Error setMetadata(buffer_handle_t buffer, AIMapper_MetadataType type,
                               const void* metadata, size_t metadataSize) {
        if (strcmp(type.name, kStdMetaName) == 0)
            return setStandardMetadata(buffer, type.value, metadata, metadataSize);
        return AIMAPPER_ERROR_UNSUPPORTED;
    }

    AIMapper_Error setStandardMetadata(buffer_handle_t buffer, int64_t, const void*, size_t) {
        if (!validHandle(buffer)) return AIMAPPER_ERROR_BAD_BUFFER;
        return AIMAPPER_ERROR_NONE;  // DATASPACE 等静默接受
    }

    AIMapper_Error listSupportedMetadataTypes(const AIMapper_MetadataTypeDescription** outList,
                                              size_t* outCount) {
        static const AIMapper_MetadataTypeDescription descs[] = {
                {{kStdMetaName, MT_BUFFER_ID}, nullptr, true, false, {}},
                {{kStdMetaName, MT_NAME}, nullptr, true, false, {}},
                {{kStdMetaName, MT_WIDTH}, nullptr, true, false, {}},
                {{kStdMetaName, MT_HEIGHT}, nullptr, true, false, {}},
                {{kStdMetaName, MT_LAYER_COUNT}, nullptr, true, false, {}},
                {{kStdMetaName, MT_PIXEL_FORMAT_REQUESTED}, nullptr, true, false, {}},
                {{kStdMetaName, MT_PIXEL_FORMAT_FOURCC}, nullptr, true, false, {}},
                {{kStdMetaName, MT_PIXEL_FORMAT_MODIFIER}, nullptr, true, false, {}},
                {{kStdMetaName, MT_USAGE}, nullptr, true, false, {}},
                {{kStdMetaName, MT_ALLOCATION_SIZE}, nullptr, true, false, {}},
                {{kStdMetaName, MT_COMPRESSION}, nullptr, true, false, {}},
                {{kStdMetaName, MT_INTERLACED}, nullptr, true, false, {}},
                {{kStdMetaName, MT_CHROMA_SITING}, nullptr, true, false, {}},
                {{kStdMetaName, MT_PLANE_LAYOUTS}, nullptr, true, false, {}},
                {{kStdMetaName, MT_CROP}, nullptr, true, false, {}},
                {{kStdMetaName, MT_DATASPACE}, nullptr, true, true, {}},
                {{kStdMetaName, MT_BLEND_MODE}, nullptr, true, false, {}},
                {{kStdMetaName, MT_STRIDE}, nullptr, true, false, {}},
        };
        *outList = descs;
        *outCount = sizeof(descs) / sizeof(descs[0]);
        return AIMAPPER_ERROR_NONE;
    }

    AIMapper_Error dumpBuffer(buffer_handle_t, AIMapper_DumpBufferCallback, void*) {
        return AIMAPPER_ERROR_UNSUPPORTED;
    }
    AIMapper_Error dumpAllBuffers(AIMapper_BeginDumpBufferCallback, AIMapper_DumpBufferCallback,
                                  void*) {
        return AIMAPPER_ERROR_UNSUPPORTED;
    }
    AIMapper_Error getReservedRegion(buffer_handle_t buffer, void** outRegion,
                                     uint64_t* outSize) {
        if (!validHandle(buffer)) return AIMAPPER_ERROR_BAD_BUFFER;
        *outRegion = nullptr;
        *outSize = 0;
        return AIMAPPER_ERROR_NONE;
    }
};

NullGfxMapper gMapper;

}  // namespace

extern "C" __attribute__((visibility("default"))) AIMapper_Error AIMapper_loadIMapper(
        AIMapper** outImplementation) {
    static AIMapper mapper;
    mapper.version = AIMAPPER_VERSION_5;
    mapper.v5 = {
            /* importBuffer */ [](const native_handle_t* h, buffer_handle_t* o) {
                return gMapper.importBuffer(h, o);
            },
            /* freeBuffer */
            [](buffer_handle_t b) { return gMapper.freeBuffer(b); },
            /* getTransportSize */
            [](buffer_handle_t b, uint32_t* f, uint32_t* i) {
                return gMapper.getTransportSize(b, f, i);
            },
            /* lock */
            [](buffer_handle_t b, uint64_t u, ARect r, int f, void** d) {
                return gMapper.lock(b, u, r, f, d);
            },
            /* unlock */
            [](buffer_handle_t b, int* f) { return gMapper.unlock(b, f); },
            /* flushLockedBuffer */
            [](buffer_handle_t b) { return gMapper.flushLockedBuffer(b); },
            /* rereadLockedBuffer */
            [](buffer_handle_t b) { return gMapper.rereadLockedBuffer(b); },
            /* getMetadata */
            [](buffer_handle_t b, AIMapper_MetadataType t, void* d, size_t s) {
                return gMapper.getMetadata(b, t, d, s);
            },
            /* getStandardMetadata */
            [](buffer_handle_t b, int64_t t, void* d, size_t s) {
                return gMapper.getStandardMetadata(b, t, d, s);
            },
            /* setMetadata */
            [](buffer_handle_t b, AIMapper_MetadataType t, const void* m, size_t s) {
                return gMapper.setMetadata(b, t, m, s);
            },
            /* setStandardMetadata */
            [](buffer_handle_t b, int64_t t, const void* m, size_t s) {
                return gMapper.setStandardMetadata(b, t, m, s);
            },
            /* listSupportedMetadataTypes */
            [](const AIMapper_MetadataTypeDescription** l, size_t* c) {
                return gMapper.listSupportedMetadataTypes(l, c);
            },
            /* dumpBuffer */
            [](buffer_handle_t b, AIMapper_DumpBufferCallback cb, void* ctx) {
                return gMapper.dumpBuffer(b, cb, ctx);
            },
            /* dumpAllBuffers */
            [](AIMapper_BeginDumpBufferCallback b, AIMapper_DumpBufferCallback cb, void* ctx) {
                return gMapper.dumpAllBuffers(b, cb, ctx);
            },
            /* getReservedRegion */
            [](buffer_handle_t b, void** r, uint64_t* s) {
                return gMapper.getReservedRegion(b, r, s);
            },
    };
    *outImplementation = &mapper;
    return AIMAPPER_ERROR_NONE;
}
