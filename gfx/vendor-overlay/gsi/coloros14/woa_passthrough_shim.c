/* woa_passthrough_shim.c — M8（ColorOS 14 GSI）：给 A15 vendor 组件提供 A14 系统缺失的
 * android::openDeclaredPassthroughHal（libbinder C++ 符号）。
 *
 * 背景：A15 的 /vendor/lib64/libui.so（还有 libbinder_ndk.so 的 C 封装）经
 * AServiceManager_openDeclaredPassthroughHal → android::openDeclaredPassthroughHal 打开直通
 * IMapper。该 C++ 符号只存在于 A15 系统的 libbinder.so（vendor 变体不带，A14 系统的也没有），
 * A14 GSI 上 vendor 进程链接不到 → hwcomposer-3 等 CANNOT LINK 退出。
 *
 * 本 shim 用汇编标签顶替该符号：唯一调用方是 libui 按
 * interface="mapper"、instance=ro.hardware.gralloc 查 mapper 直通库（mapper.<suffix>.so 在
 * /vendor/lib64/hw/ 下），所以直接读属性拼库名。加载优先用 android_load_sphal_library
 * （vendor 里已随 libbinder.so 装了 ColorOS 15 的 libvndksupport，会搜 hw 子目录），取不到再按
 * 绝对路径 dlopen。跳过原实现的 VINTF isDeclared 检查（实验盘的 VINTF 本来就声明了这些 HAL）。
 * 经 coloros14_fixes.py 装到 /vendor/lib64/_ndk.so，并把 vendor 副本 libbinder_ndk.so 的
 * DT_NULL 槽位改成 DT_NEEDED 指向 SONAME 子串 "_ndk.so" 挂进依赖闭包。
 *
 * 编译（NDK r27c，产物与仓库里的 _ndk.so 逐字节相同）：
 *   gfx/android-ndk-r27c/toolchains/llvm/prebuilt/windows-x86_64/bin/clang.exe \
 *     --target=aarch64-linux-android34 -shared -fPIC -O2 -o _ndk.so woa_passthrough_shim.c -ldl
 */
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/system_properties.h>

__attribute__((visibility("default")))
void* ranchuwoa_open_passthrough(const void* /*String16&*/ interface,
                                 const void* /*String16&*/ instance,
                                 int flag);

__asm__(".globl _ZN7android26openDeclaredPassthroughHalERKNS_8String16ES2_i\n"
        ".type _ZN7android26openDeclaredPassthroughHalERKNS_8String16ES2_i, %function\n"
        "_ZN7android26openDeclaredPassthroughHalERKNS_8String16ES2_i = ranchuwoa_open_passthrough");

void* ranchuwoa_open_passthrough(const void* interface, const void* instance, int flag) {
    (void)interface;
    (void)instance;
    // 调用方（libui）传来的 instance 是 String16，没有稳定 ABI 可读；RanchuWoA 的 mapper
    // 后缀固定是 woa（woa allocator 的 getIMapperLibrarySuffix 返回它），属性可覆盖
    char suffix[92] = "woa";
    char name[128], path[160];
    if (__system_property_get("ro.ranchuwoa.gralloc_suffix", suffix) == 0)
        suffix[0] = '\0', __system_property_get("debug.ranchuwoa.gralloc_suffix", suffix);
    if (suffix[0] == '\0')
        snprintf(suffix, sizeof(suffix), "woa");
    snprintf(name, sizeof(name), "mapper.%s.so", suffix);
    // libvndksupport 的 android_load_sphal_library 会按 /vendor/${LIB}(/hw) 搜索
    void* (*load_sphal)(const char*, int) =
        (void* (*)(const char*, int))dlsym(RTLD_DEFAULT, "android_load_sphal_library");
    if (load_sphal != NULL) {
        void* h = load_sphal(name, flag);
        if (h != NULL)
            return h;
    }
    snprintf(path, sizeof(path), "/vendor/lib64/hw/%s", name);
    return dlopen(path, flag);
}
