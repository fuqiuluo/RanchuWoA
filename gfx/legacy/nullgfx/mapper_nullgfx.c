// mapper_nullgfx.c — 精简版（无 STL）：stable-C IMapper5，memfd CPU 缓冲。
// handle 布局与 allocatord 一致：numFds=1, numInts=12（见 HI_* 枚举）。
#include <android/hardware/graphics/mapper/IMapper.h>
#include <cutils/native_handle.h>
#include <android/log.h>
#include <sys/mman.h>
#include <unistd.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>

#define ALOGE(...) __android_log_print(ANDROID_LOG_ERROR, "nullgfx", __VA_ARGS__)

#define MAGIC 0x4E554C47
enum { HI_MAGIC = 0, HI_WIDTH, HI_HEIGHT, HI_STRIDE, HI_FORMAT, HI_LAYERS,
       HI_USAGE_LO, HI_USAGE_HI, HI_SIZE, HI_ID, HI_DATASPACE, HI_TOTAL };
#define NUM_INTS HI_TOTAL
#define STD_META_NAME "android.hardware.graphics.common.StandardMetadataType"
#define STD_META_LEN 51

enum { MT_BUFFER_ID = 1, MT_NAME, MT_WIDTH, MT_HEIGHT, MT_LAYER_COUNT,
       MT_PIXEL_FORMAT_REQUESTED, MT_PIXEL_FORMAT_FOURCC, MT_PIXEL_FORMAT_MODIFIER,
       MT_USAGE, MT_ALLOCATION_SIZE, MT_PROTECTED_CONTENT, MT_COMPRESSION,
       MT_INTERLACED, MT_CHROMA_SITING, MT_PLANE_LAYOUTS, MT_CROP, MT_DATASPACE,
       MT_BLEND_MODE };
#define MT_STRIDE 23

struct map_slot { const native_handle_t* h; void* addr; size_t size; };
static struct map_slot g_slots[128];

static int valid(const native_handle_t* h) {
    return h && h->numFds == 1 && h->numInts == NUM_INTS && h->data[0] >= 0 &&
           h->data[1 + HI_MAGIC] == MAGIC;
}

struct W { char* p; size_t cap; size_t n; int fit; };
static void w_raw(struct W* w, const void* d, size_t n) {
    if (w->fit && w->cap >= w->n + n && w->p) memcpy(w->p + w->n, d, n);
    else if (w->cap < w->n + n) w->fit = 0;
    w->n += n;
}
static void w_hdr(struct W* w, int64_t type) {
    int64_t len = STD_META_LEN;
    w_raw(w, &len, 8);
    w_raw(w, STD_META_NAME, STD_META_LEN);
    w_raw(w, &type, 8);
}
static void w_i64(struct W* w, int64_t v) { w_raw(w, &v, 8); }
static void w_i32(struct W* w, int32_t v) { w_raw(w, &v, 4); }
static void w_u32(struct W* w, uint32_t v) { w_raw(w, &v, 4); }
static void w_u64(struct W* w, uint64_t v) { w_raw(w, &v, 8); }
static void w_str(struct W* w, const char* s) {
    int64_t len = (int64_t)strlen(s);
    w_i64(w, len);
    w_raw(w, s, (size_t)len);
}

static uint32_t fourcc(int32_t f) {
    if (f == 1) return 0x34324241;  // RGBA8888 -> 'AB24'
    if (f == 2) return 0x34324258;  // RGBX -> 'XB24'
    if (f == 4) return 0x36314752;  // RGB565 -> 'RG16'
    return 0x20203852;              // R8
}
static int bpp(int32_t f) {
    if (f == 1 || f == 2) return 4;
    if (f == 3) return 3;
    if (f == 4) return 2;
    return 1;  // BLOB 等
}

static int32_t gstd(buffer_handle_t b, int64_t type, void* dest, size_t size);
static AIMapper_Error setstd(buffer_handle_t b, int64_t t, const void* m, size_t s);

static AIMapper_Error imp(const native_handle_t* h, buffer_handle_t* out) {
    if (!valid(h)) return AIMAPPER_ERROR_BAD_BUFFER;
    native_handle_t* c = native_handle_clone(h);
    if (!c) return AIMAPPER_ERROR_NO_RESOURCES;
    *out = c;
    return AIMAPPER_ERROR_NONE;
}
static AIMapper_Error freeb(buffer_handle_t b) {
    int i;
    if (!valid(b)) return AIMAPPER_ERROR_BAD_BUFFER;
    for (i = 0; i < 128; i++)
        if (g_slots[i].h == b) { munmap(g_slots[i].addr, g_slots[i].size); g_slots[i].h = 0; }
    native_handle_close((native_handle_t*)b);
    native_handle_delete((native_handle_t*)b);
    return AIMAPPER_ERROR_NONE;
}
static AIMapper_Error transport(buffer_handle_t b, uint32_t* f, uint32_t* i) {
    if (!valid(b)) return AIMAPPER_ERROR_BAD_BUFFER;
    *f = b->numFds;
    *i = b->numInts;
    return AIMAPPER_ERROR_NONE;
}
static AIMapper_Error lock(buffer_handle_t b, uint64_t u, ARect r, int af, void** out) {
    int i;
    (void)r;
    if (af >= 0) close(af);
    if (!valid(b)) return AIMAPPER_ERROR_BAD_BUFFER;
    if (u == 0) return AIMAPPER_ERROR_BAD_VALUE;
    for (i = 0; i < 128; i++)
        if (g_slots[i].h == b) { *out = g_slots[i].addr; return AIMAPPER_ERROR_NONE; }
    size_t sz = (size_t)b->data[1 + HI_SIZE];
    void* a = mmap(0, sz, PROT_READ | PROT_WRITE, MAP_SHARED, b->data[0], 0);
    if (a == MAP_FAILED) { ALOGE("mmap %zu: %s", sz, strerror(errno)); return AIMAPPER_ERROR_NO_RESOURCES; }
    for (i = 0; i < 128; i++)
        if (!g_slots[i].h) { g_slots[i].h = b; g_slots[i].addr = a; g_slots[i].size = sz; break; }
    *out = a;
    return AIMAPPER_ERROR_NONE;
}
static AIMapper_Error unlock(buffer_handle_t b, int* rf) {
    if (!valid(b)) return AIMAPPER_ERROR_BAD_BUFFER;
    *rf = -1;
    return AIMAPPER_ERROR_NONE;
}
static AIMapper_Error noop_err(buffer_handle_t b) {
    return valid(b) ? AIMAPPER_ERROR_NONE : AIMAPPER_ERROR_BAD_BUFFER;
}
static AIMapper_Error getmeta(buffer_handle_t b, AIMapper_MetadataType t, void* d, size_t s) {
    if (!strcmp(t.name, STD_META_NAME)) return gstd(b, t.value, d, s) < 0 ? AIMAPPER_ERROR_UNSUPPORTED : AIMAPPER_ERROR_NONE;
    return AIMAPPER_ERROR_UNSUPPORTED;
}
static int32_t getmeta_i32(buffer_handle_t b, AIMapper_MetadataType t, void* d, size_t s) {
    if (!strcmp(t.name, STD_META_NAME)) return gstd(b, t.value, d, s);
    return -AIMAPPER_ERROR_UNSUPPORTED;
}
static AIMapper_Error setmeta(buffer_handle_t b, AIMapper_MetadataType t, const void* m, size_t s) {
    if (!strcmp(t.name, STD_META_NAME)) return setstd(b, t.value, m, s);
    return AIMAPPER_ERROR_UNSUPPORTED;
}
static int32_t gstd(buffer_handle_t b, int64_t type, void* dest, size_t size) {
    char name[40];
    if (!valid(b)) return -AIMAPPER_ERROR_BAD_BUFFER;
    const int* in = b->data + 1;
    struct W w = { (char*)dest, size, 0, dest != 0 };
    switch (type) {
        case MT_BUFFER_ID:
            w_hdr(&w, type); w_u64(&w, (uint32_t)in[HI_ID]); return (int32_t)w.n;
        case MT_NAME:
            w_hdr(&w, type);
            snprintf(name, sizeof name, "nullgfx#%u", (uint32_t)in[HI_ID]);
            w_str(&w, name);
            return (int32_t)w.n;
        case MT_WIDTH:
            w_hdr(&w, type); w_u32(&w, (uint32_t)in[HI_WIDTH]); return (int32_t)w.n;
        case MT_HEIGHT:
            w_hdr(&w, type); w_u32(&w, (uint32_t)in[HI_HEIGHT]); return (int32_t)w.n;
        case MT_LAYER_COUNT:
            w_hdr(&w, type); w_u32(&w, (uint32_t)in[HI_LAYERS]); return (int32_t)w.n;
        case MT_PIXEL_FORMAT_REQUESTED:
            w_hdr(&w, type); w_i32(&w, in[HI_FORMAT]); return (int32_t)w.n;
        case MT_PIXEL_FORMAT_FOURCC:
            w_hdr(&w, type); w_u32(&w, fourcc(in[HI_FORMAT])); return (int32_t)w.n;
        case MT_PIXEL_FORMAT_MODIFIER: case MT_PROTECTED_CONTENT:
            w_hdr(&w, type); w_u64(&w, 0); return (int32_t)w.n;
        case MT_USAGE:
            w_hdr(&w, type);
            w_u64(&w, ((uint64_t)(uint32_t)in[HI_USAGE_HI] << 32) | (uint32_t)in[HI_USAGE_LO]);
            return (int32_t)w.n;
        case MT_ALLOCATION_SIZE:
            w_hdr(&w, type); w_u64(&w, (uint32_t)in[HI_SIZE]); return (int32_t)w.n;
        case MT_DATASPACE:
            w_hdr(&w, type); w_i64(&w, in[HI_DATASPACE]); return (int32_t)w.n;
        case MT_STRIDE:
            w_hdr(&w, type); w_u32(&w, (uint32_t)in[HI_STRIDE]); return (int32_t)w.n;
        case MT_COMPRESSION: case MT_INTERLACED: case MT_CHROMA_SITING: case MT_BLEND_MODE: {
            w_hdr(&w, type);
            w_str(&w, type == MT_COMPRESSION ? "android.hardware.graphics.common.Compression"
                  : type == MT_INTERLACED ? "android.hardware.graphics.common.Interlaced"
                  : type == MT_CHROMA_SITING ? "android.hardware.graphics.common.ChromaSiting"
                                             : "android.hardware.graphics.common.BlendMode");
            w_i64(&w, 0);
            return (int32_t)w.n;
        }
        case MT_CROP: {
            int32_t crop[4] = { 0, 0, in[HI_WIDTH], in[HI_HEIGHT] };
            w_hdr(&w, type);
            w_i64(&w, 1);
            w_raw(&w, crop, 16);
            return (int32_t)w.n;
        }
        case MT_PLANE_LAYOUTS: {
            int i;
            int bp = bpp(in[HI_FORMAT]);
            int comps = (in[HI_FORMAT] == 1) ? 4 : 3;
            static const char* cn[4] = {
                "android.hardware.graphics.common.PlaneLayoutComponent.R",
                "android.hardware.graphics.common.PlaneLayoutComponent.G",
                "android.hardware.graphics.common.PlaneLayoutComponent.B",
                "android.hardware.graphics.common.PlaneLayoutComponent.A",
            };
            w_hdr(&w, type);
            w_i64(&w, 1);   /* plane 数 */
            w_i64(&w, comps);
            for (i = 0; i < comps; i++) { w_str(&w, cn[i]); w_i64(&w, 0); w_i64(&w, 8 * i); w_i64(&w, 8); }
            w_i64(&w, 0);
            w_i64(&w, bp * 8);
            w_i64(&w, in[HI_STRIDE]);
            w_i64(&w, in[HI_WIDTH]);
            w_i64(&w, in[HI_HEIGHT]);
            w_i64(&w, (int64_t)in[HI_STRIDE] * in[HI_HEIGHT]);
            w_i64(&w, 1);
            w_i64(&w, 1);
            return (int32_t)w.n;
        }
        default:
            return -AIMAPPER_ERROR_UNSUPPORTED;
    }
}
static AIMapper_Error setstd(buffer_handle_t b, int64_t t, const void* m, size_t s) {
    (void)t; (void)m; (void)s;
    if (!valid(b)) return AIMAPPER_ERROR_BAD_BUFFER;
    return AIMAPPER_ERROR_NONE;
}

static const AIMapper_MetadataTypeDescription g_descs[] = {
    {{STD_META_NAME, MT_BUFFER_ID}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_NAME}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_WIDTH}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_HEIGHT}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_LAYER_COUNT}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_PIXEL_FORMAT_REQUESTED}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_PIXEL_FORMAT_FOURCC}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_PIXEL_FORMAT_MODIFIER}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_USAGE}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_ALLOCATION_SIZE}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_COMPRESSION}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_INTERLACED}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_CHROMA_SITING}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_PLANE_LAYOUTS}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_CROP}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_DATASPACE}, 0, 1, 1, {0}},
    {{STD_META_NAME, MT_BLEND_MODE}, 0, 1, 0, {0}},
    {{STD_META_NAME, MT_STRIDE}, 0, 1, 0, {0}},
};
static AIMapper_Error listmeta(const AIMapper_MetadataTypeDescription** l, size_t* c) {
    *l = g_descs;
    *c = sizeof(g_descs) / sizeof(g_descs[0]);
    return AIMAPPER_ERROR_NONE;
}
static AIMapper_Error unsup_dump(buffer_handle_t b, AIMapper_DumpBufferCallback cb, void* ctx) {
    (void)b; (void)cb; (void)ctx;
    return AIMAPPER_ERROR_UNSUPPORTED;
}
static AIMapper_Error unsup_dumpall(AIMapper_BeginDumpBufferCallback b, AIMapper_DumpBufferCallback cb, void* ctx) {
    (void)b; (void)cb; (void)ctx;
    return AIMAPPER_ERROR_UNSUPPORTED;
}
static AIMapper_Error reserved(buffer_handle_t b, void** r, uint64_t* s) {
    if (!valid(b)) return AIMAPPER_ERROR_BAD_BUFFER;
    *r = 0;
    *s = 0;
    return AIMAPPER_ERROR_NONE;
}

static AIMapper g_mapper = {
    AIMAPPER_VERSION_5,
    {
        imp, freeb, transport, lock, unlock, noop_err, noop_err,
        getmeta_i32,  /* getMetadata：int32 语义 */
        gstd,        /* getStandardMetadata */
        setmeta, setstd, listmeta, unsup_dump, unsup_dumpall, reserved,
    },
};

__attribute__((visibility("default"))) AIMapper_Error AIMapper_loadIMapper(AIMapper** out) {
    *out = &g_mapper;
    return AIMAPPER_ERROR_NONE;
}
