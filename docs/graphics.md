# 图形栈

官方镜像的图形 HAL（gralloc / composer / EGL）原本连接官方模拟器的宿主渲染器（goldfish 管道 +
gfxstream），本项目把它们替换/补齐为不依赖官方模拟器的实现：

```
                 ┌ swiftshader：SwiftShader GLES（guest，CPU）── 经 AHardwareBuffer 锁 gralloc ─┐
App / hwui ─────┤                                                                              │
SurfaceFlinger ─┤                                                                              ├→ ranchu hwc3（client 合成，
bootanimation ──┤                                                                              │   AddFB2 + atomic commit）
                 └ gfxstream：GLES 编码器（guest）→ virtio-gpu → QEMU rutabaga → gfxstream ────┘   → virtio-gpu → SDL 窗口
                                                    → ANGLE → Direct3D 11（宿主 GPU）
                woa gralloc：AIDL IAllocator v2 + stable-C IMapper v5（gfxstream 模式下缓冲就是宿主 ColorBuffer）
```

- **共同地基**：woa gralloc（§1）+ 原镜像的 ranchu hwc3（client 模式，§4）+ virtio-gpu 扫描输出 +
  virtio-gpu 内核驱动（vendor 模块里有 virtio_gpu.ko）。
- **两种 GLES 实现**二选一，`avdctl create/edit --gpu gfxstream|swiftshader`，内核命令行
  `androidboot.hardwareegl=` 决定 init 装 `/vendor/lib64/egl/` 里的哪套（`emulation` / `swiftshader`）。
  两套都已装在基础镜像里，切换不需要重建。

源码位置：`gfx/gralloc/`（gralloc）、`gfx/swiftshader/`（SwiftShader 及其本地补丁）、
`gfx/gfxstream-guest/`（guest 编码器构建）、`hostgfx/`（宿主 gfxstream 链路安装前缀）。构建见 README。

## 1. woa gralloc（`gfx/gralloc/`）

AIDL IAllocator v2 + stable-C IMapper v5，按 Android 15 的 Gralloc5 加载路径注册（VINTF 片段声明
`android.hardware.graphics.allocator.IAllocator/default` v2 与 native mapper 5.0 instance `woa`；
官方镜像原带的 ranchu gralloc3 的 VINTF 与 rc 一并移除，避免 libui 掉进旧的 mapper@3.0）。

- **AIDL 生成**：SDK build-tools 34 的 `aidl --lang=ndk --structured --stability=vintf`（旧版 aidl 算不了
  `1L << 32` 类常量，BufferUsage 按字面量处理；HdrConversionStrategy 生成失败但用不到）。
  平台头（binder stability 等）取自 AOSP，链接设备上的 `libbinder_ndk.so`（LLNDK，sphal 可见），
  编译定义 `BINDER_STABILITY_SUPPORT` 让 BnAllocator 自动 markVintfStability——不做这步的话 vendor
  进程的事务会被 `Stability::check` 拒绝（BAD_TYPE）。
- **句柄布局**：前缀与 Android 15 minigbm 的 `cros_gralloc_handle` 二进制一致（hwc3 的 DrmClient 直接按
  它读 fds[0]/width/height/format/strides[0]/offsets[0]），后接 woa 扩展字段；1 fd + 45 ints。
- **分配来源**（按用途）：
  - gfxstream 模式且 32bpp/YUV 等 host 支持的格式 → **virtio-gpu 资源**（见 §2.2）；
  - 其余 → **DMA-BUF system heap**（`/dev/dma_heap/system`；SELinux 上客户端进程不需要额外规则，
    memfd 方案的标签要逐进程放行）。`DMA_BUF_SET_NAME` 给缓冲起名 `woa:<名字>`。
- **元数据共享页**：dataspace/blend/crop 等跨进程可见的元数据放在 fd 尾部的共享页。注意内核坑：
  android15-6.6 对 virtio-gpu 导出的 dma-buf 做**带偏移** mmap 时忽略偏移、总映射第 0 页（drm shmem
  缺页处理没算 vm_pgoff）——所以整个 fd 从偏移 0 映射一次，元数据页按指针偏移取（dma-heap 不受影响）。
- **mapper**：用 AOSP `IMapperMetadataTypes.h` 编码标准元数据；WIDTH/HEIGHT/LAYER_COUNT 是 uint64，
  DATASPACE/BLEND_MODE 是 int32；lock 不校验 CPU usage（SwiftShader 会锁没声明 CPU usage 的缓冲）；
  mapper 全局状态不做静态析构（binder 线程上晚到的缓冲释放会锁已销毁的 mutex，screencap 曾因此 abort）。

## 2. SwiftShader 后端（`gfx/swiftshader/`）

guest 内 CPU 渲染，不依赖宿主图形驱动，兼容性兜底。基于 SwiftShader 仍有 GLES 前端的旧提交
（`606261c7`）+ 本地补丁，按 **Android 平台版**而不是 NDK 版构建：

- 去掉 `-DANDROID_NDK_BUILD`，`ANDROID_PLATFORM_SDK_VERSION=35`；`android-platform-include/` 放 AOSP 的
  `system/window.h` 等 + log/cutils/sync 小垫片；链接设备的 `libnativewindow`/`libsync`（LLNDK）。
- `GrallocAndroid` 重写为经 `ANativeWindowBuffer_getHardwareBuffer` + `AHardwareBuffer_lock/unlock`
  访问 gralloc，EGLImage 与窗口面都走它（NDK 版没有 `EGL_NATIVE_BUFFER_ANDROID` 路径，窗口面靠桩，
  SF 合成和 app 渲染都出不了图）；EGL 扩展串补 `EGL_KHR_image EGL_ANDROID_image_native_buffer`。
- FP16：SwiftShader 内部格式是每通道 32 位 float，与外部 A16B16G16R16F 布局不同——内外格式不同时用
  Surface 自己的内部缓冲，lock/unlock 时做 F16↔F32 转换（直接把 gralloc 内存当内部缓冲会越界写崩溃）。
- EGL 上下文属性：对提示类属性（`EGL_CONTEXT_PRIORITY_LEVEL_IMG`、`EGL_CONTEXT_OPENGL_NO_ERROR_KHR`、
  健壮访问）接受而不是拒绝——OPLUS 的 `com.oplus.blur` 会带这些属性创建上下文。
- 产物改名 `libEGL_swiftshader.so` / `libGLESv2_swiftshader.so`（**SONAME 必须与文件名一致**，Android
  动态链接器匹配时校验 SONAME——早期"找不到 GLES 实现"就是这个），另编一个空的 libGLESv1_CM。

## 3. gfxstream 后端（宿主 GPU 加速）

管线：guest EGL/GLES 四件套（`libEGL_emulation` 等，virtio-gpu-pipe 传输，`x-gfxstream-gles` capset）→
QEMU `virtio-gpu-rutabaga-pci,wsi=surfaceless` → `rutabaga_gfx_ffi` → `libgfxstream_backend`（自编译，
EglOnEgl）→ ANGLE(D3D11) → 宿主 GPU。guest 侧按 AOSP 结构编成 3 个 HAL + 共享 `libOpenglSystemCommon.so`
（TLS 单实例），protobuf-lite v21.12 静态编入（设备的 libprotobuf 是平台 ABI）。

构建入口 `gfx/build_gfxstream_host.sh`（→ `hostgfx/`）与 `gfx/build_gfxstream_guest.sh`（→ `gfx/gs-out/`，
经 `avdctl prepare --force` 装进 vendor）。上游提交与补丁版本见 README「从源码构建」。

### 3.1 关键机制

- **内核 6.6 virtio-gpu 不支持导入外来 dma-buf**（`virtgpu_gem_prime_import_sg_table` 返回 ENODEV），
  但**本设备导出的 dma-buf 回导没问题**（import 直接返回原 GEM 对象，res_handle 保留）。因此 gralloc
  把 GPU 会碰到的缓冲都分配成 virtio-gpu 资源：资源创建时宿主 gfxstream 自动以 res_handle 为键建好
  ColorBuffer；消费端（SF Skia 的 `glEGLImageTargetTexture2DOES`）回导拿 res_handle → `rcBindTexture`
  即零拷贝纹理化。SF 的 Skia RenderEngine 开机时只要有一次导入失败就永久回退 CPU 合成，所以不能留
  dma-heap 缓冲给 GPU 路径。
- 这类缓冲的真身在宿主（ColorBuffer），guest 页只是 backing——CPU 读写靠 mapper 的 transfer（§3.4）。
- **宿主上下文是 ES 3.0**（翻译层 `egl_os_api_egl.cpp` 硬编码 `EGL_CONTEXT_CLIENT_VERSION, 3`），
  `emulation_gl.cpp` 在 Windows 上把广播版本也 cap 到 3.0——广播 3.1 会让 Skia 生成 `#version 310 es`
  着色器、全部编译失败、所有绘制静默失败（黑屏的经典根因）。
- **ANGLE D3D11 不是线程安全的**：StateManager11/immediate context 属于整个 EGLDisplay、所有 context
  共用，只在 eglMakeCurrent 时重置；gfxstream 每个 guest 连接一个渲染线程同时 current，交错 makeCurrent
  后下一次绘制拿着空的 `mFramebuffer11` 崩溃（开机几十秒内必崩的根因）。ANGLE 有编译选项
  `ANGLE_FORCE_CONTEXT_CHECK_EVERY_CALL` 但发行版没开、运行期开不了——本项目在 gfxstream 翻译层复刻了
  它（`angle_lock.{h,cpp}`）：装载的所有 GL/EGL 函数指针换成持全局递归锁的 thunk；GL 调用前若 ANGLE
  最后见到的 context 不是本线程的，先 `eglMakeCurrent(无)` 再绑回（ANGLE 对同一 context 的
  makeCurrent 直接短路，必须先解绑才触发重置）；`glClientWaitSync` 改零超时轮询、间隙放锁。
  `GFXSTREAM_ANGLE_SERIALIZE=0` 可关闭。
- **EDID 刷新率**：QEMU 的 EDID 在 UI 没上报时默认 75Hz，Android 渲染帧率上限 60，框架只能取 75 的约数
  37.5fps。补丁改为无上报值时按 60Hz 生成（`virtio-gpu-base.c`）。
- **防闪烁**：Android 客户端目标在 3 个缓冲间轮换，每次翻页内核发 SET_SCANOUT + RESOURCE_FLUSH；
  原实现换 surface 时先把控制台 surface 置 NULL（SDL 立即画 "Display output is not active" 占位图）再换，
  每次翻页闪一下。改为 rutabaga 扫描输出用一张常驻图像、flush 时把当前资源读回进去，只有尺寸/格式变化
  才换 surface（2D 后端同理：翻页到另一资源前先把 guest backing 同步进宿主图像）。
- **puid 生命周期**：EGL 窗口表面/上下文/EGLImage 按 render 线程的 puid 归属，清理按"被销毁的上下文 id"
  进行；而进程管道原来返回 1、2、3… 计数器作 puid，与上下文 id 两套编号撞号——某线程的上下文 N 退出时
  会把 puid 恰为 N 的另一个活进程的窗口表面整个删掉（状态栏/通知栏消失、应用切换闪帧、偶尔整页黑的
  根因，qemu.log 里成串 `window handle ... not found`）。修复：`VirtioGpuProcessPipe::Create` 用进程
  管道自己的上下文 id 作 puid（进程存活期间唯一）；`FrameBuffer::cleanupGuestProcess` 清理前先等该
  puid 的 render 线程都退出。
- **guest 程序二进制缓存**：宿主特性 `GlProgramBinaryLinkStatus` 默认开启（上游默认关是为了兼容旧
  guest 编码器；关闭时对 glProgramBinary 载入的程序一律报 LINK_STATUS=false——HyperOS 的 libmigl
  先 `glGetProgramBinary` 存缓存、开机后跨上下文 `glProgramBinary` 恢复，链接失败且不回退源码编译，
  表现为壁纸全黑）。我们的 guest 编码器与宿主同源，已带配套修复。

### 3.2 运行环境

- 运行期依赖链：qemu.exe → rutabaga_gfx_ffi.dll → libgfxstream_backend-0.dll → libc++.dll + ANGLE
  （`libEGL.dll`/`libGLESv2.dll` 按名 LoadLibrary）。ANGLE 用 MSYS2 的 angleproject 或自选版本
  （放 `hostgfx/angle/` 或环境变量 `RANCHU_ANGLE` 指向的目录，Electron/Chromium 自带的 ARM64 版可用）。
- 环境变量：`ANDROID_EGL_ON_EGL=1`；`ANDROID_EMU_VK_LOADER_PATH` 指向不存在的 DLL 以禁用宿主 Vulkan
  枚举（Windows ARM 的 Vulkan ICD 组合在 QEMU 进程里初始化即崩）。
- `avdctl dist` 打包时去掉宿主 DLL 的调试段。

### 3.3 CPU 读写与格式（gfxstream 模式）

- **CPU 读写 virtio-gpu 缓冲**：screencap/录屏/ImageReader/lockCanvas/软件解码都要 CPU 访问，而
  backing 页从没有 GPU 画的内容。mapper 参照 minigbm virgl 后端的做法：在本进程打开 render 节点
  （virtio-gpu 有 3D 时），按需 PRIME_FD_TO_HANDLE（同一 dma-buf 进程内只一个 GEM 句柄，引用计数关闭）；
  带 CPU 读用途的 lock 先 `TRANSFER_FROM_HOST_3D` + `VIRTGPU_WAIT`，带写用途的最后一次 unlock 后
  `TRANSFER_TO_HOST_3D`。非 blob 资源内核要求 stride/layer_stride 为 0（布局由资源决定，否则 EINVAL）。
- **YUV / 非 32bpp**：宿主能为 NV12/YV12/P010/RGB565/FP16/1010102/R8/RGB888 建 ColorBuffer，但按自己的
  线性布局做 transfer（NV12/P010 紧排；YV12 的 y_stride 按 32 对齐、c_stride=y_stride/2、色度高度
  height/2）。gralloc 在 3D 可用时给这些格式 virtio 格式号并按宿主布局排平面（`computeLayout(..., hostGpu)`），
  CPU 写完由 mapper 上传，宿主 YUV 转换后合成。奇数尺寸的 YUV 仍走 dma-heap（宿主布局对不上，显示为空）。

## 4. hwc3 与显示

- 用官方镜像自带的 ranchu hwc3，两个属性切到无宿主依赖的模式（vendor 的 init rc 里设置）：
  `ro.vendor.hwcomposer.mode=client`（ClientFrameComposer：全部图层 client 合成，客户端目标 dma-buf
  直接 drmPrimeFDToHandle + AddFB2 + atomic commit）+ `display_finder_mode=drm`（经 DRM 枚举显示器，
  virtio-gpu 的连接器/模式就是显示器来源）。
- Android GKI 的 virtio-gpu 主平面支持 8 种 32 位格式（含 AB24），ABGR8888 客户端目标可直接扫描输出。
- **QEMU 补丁（ranchu-coherent-flush，默认开）**：内核只对 dumb BO 在平面更新时发 TRANSFER_TO_HOST_2D；
  gralloc 建的资源不是 dumb，只发 RESOURCE_FLUSH——flush 时先从 guest backing 拷入宿主图像，否则
  SwiftShader 模式下画的东西上不了屏。
- **分辨率与密度**：`-device virtio-gpu-pci,xres=720,yres=1280` 定分辨率；`dpi=` 属性定 EDID 物理尺寸
  （像素 × 25.4 / dpi 四舍五入到毫米）——厂商系统（如 MIUI 的 AutoDensity）会按物理 PPI 重算应用密度，
  不报物理尺寸时按 100dpi 算，600 像素宽会被当成 14 英寸"显示器"，应用缩到 114dpi 小得看不清。
  avdctl 把实例的 `--density` 传给 dpi。
- **窗口缩放不改变 guest 分辨率**：SDL 窗口只做等比缩放显示（原版把窗口尺寸经 ui_info 报给 virtio-gpu
  当首选模式，guest 重启后会按窗口尺寸甚至 640x480 占位画面开机，已补丁去掉）；窗口 resize 后在所在
  显示器可用区域重新居中。

## 5. 性能参考

Settings 来回滚动 16 次的 `dumpsys gfxinfo`（4–6 vCPU）：

| 实例 | 后端 | janky | 中位/90%/99% 帧时间 |
|---|---|---|---|
| 720x1280 官方镜像 | gfxstream | 2.3% | 17/26/53 ms |
| 720x1280 官方镜像 | swiftshader | 3.9% | 17/26/57 ms |
| 620x1386 ColorOS | gfxstream | 0% | 5/5/8 ms |
| 620x1386 ColorOS | swiftshader | 0.4% | 12/14/16 ms |

简单界面两者都能跑满 60fps；gfxstream 单帧耗时约为 SwiftShader 的一半以下，不占 guest CPU，余量留给
复杂界面和 3D 应用。

## 6. 已知限制

- 宿主上下文是 ES 3.0（翻译层上游 TODO：ES 3.1 要改 `egl_os_api_egl.cpp` 建 3.1 上下文并放开广播
  cap）。需要 ES 3.1 的效果（如 HyperOS libmigl 的滤镜/模糊着色器）在 gfxstream 模式下不可用；
  SwiftShader 同为 3.0。
- 没有 Vulkan（宿主 Windows ARM 的 Vulkan ICD 在 QEMU 进程里初始化即崩，已用环境变量规避枚举）。
- angle_lock 是全局锁：多个 GL 应用同时高负载时会互相等。
- 奇数尺寸的 YUV 缓冲走 dma-heap，gfxstream 模式下显示为空。
- 滚动卡顿/闪烁的量化排查手段见 [debugging.md](debugging.md)（`flicker_cap.py` / `window_cap.py`）。
