# RanchuWoA — gfxstream guest（virtio-gpu-pipe 传输，x-gfxstream-gles capset）
# 产物：libEGL_gfxstream.so / libGLESv2_gfxstream.so / libGLESv1_CM_gfxstream.so
# 来源：gfxstream/guest（GLES/EGL 编码器栈，hardware/google/gfxstream）
#       + mesa-aosp/src/gfxstream/guest/{platform,android}（VirtGpu DRM 后端、ANativeWindow/gralloc 适配）
#       + libdrm 头（链接设备 vendor/libdrm.so）
cmake_minimum_required(VERSION 3.16)
project(gfxstream_guest C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

set(GFS   ${CMAKE_CURRENT_SOURCE_DIR}/../../gfxstream)      # hardware/google/gfxstream 克隆
set(MESA  ${CMAKE_CURRENT_SOURCE_DIR}/../../mesa-aosp)      # external/mesa3d 稀疏克隆
set(LIBDRM ${CMAKE_CURRENT_SOURCE_DIR}/../../libdrm)
set(SHIM  ${CMAKE_CURRENT_SOURCE_DIR}/shim)
set(PLATINC ${CMAKE_CURRENT_SOURCE_DIR}/../swiftshader/android-platform-include)
set(DEVLIBS ${CMAKE_CURRENT_SOURCE_DIR}/../device-libs)

# 与宿主 gfxstream_backend 同源，保持协议/结构体一致
add_compile_definitions(GFXSTREAM_UNSTABLE=1)
add_compile_options(-Wno-unused-parameter -Wno-deprecated-declarations
    -Wno-mismatched-tags -Wno-implicit-fallthrough -Wno-missing-field-initializers)

# ---------------- protobuf v21.12 lite 运行时（静态编入） ----------------
# 设备 libprotobuf-cpp-lite 是平台 libc++（std::__1）ABI，NDK 侧（__ndk1）符号对不上，
# 直接用同版源码（downloads/protobuf-3.21.12）自编 lite 子集。
set(PBSRC ${CMAKE_CURRENT_SOURCE_DIR}/../../downloads/protobuf-3.21.12/src/google/protobuf)
add_library(protobuf_lite_static STATIC
    ${PBSRC}/arena.cc
    ${PBSRC}/arenastring.cc
    ${PBSRC}/extension_set.cc
    ${PBSRC}/generated_message_util.cc
    ${PBSRC}/generated_message_tctable_lite.cc
    ${PBSRC}/implicit_weak_message.cc
    ${PBSRC}/io/coded_stream.cc
    ${PBSRC}/io/zero_copy_stream.cc
    ${PBSRC}/io/zero_copy_stream_impl_lite.cc
    ${PBSRC}/map.cc
    ${PBSRC}/message_lite.cc
    ${PBSRC}/parse_context.cc
    ${PBSRC}/repeated_field.cc
    ${PBSRC}/repeated_ptr_field.cc
    ${PBSRC}/io/zero_copy_stream_impl.cc
    ${PBSRC}/stubs/bytestream.cc
    ${PBSRC}/stubs/common.cc
    ${PBSRC}/stubs/int128.cc
    ${PBSRC}/stubs/status.cc
    ${PBSRC}/stubs/statusor.cc
    ${PBSRC}/stubs/stringpiece.cc
    ${PBSRC}/stubs/structurally_valid.cc
    ${PBSRC}/stubs/strutil.cc
    ${PBSRC}/wire_format_lite.cc
)
target_include_directories(protobuf_lite_static PUBLIC ${PBSRC}/../..)
target_compile_definitions(protobuf_lite_static PRIVATE HAVE_PTHREAD=1)
target_compile_options(protobuf_lite_static PRIVATE -Wno-unused-parameter -Wno-deprecated-declarations -Wno-shorten-64-to-32)

# ---------------- VirtGpu 平台层（mesa，DRM 后端） ----------------
add_library(platform_virtgpu STATIC
    ${MESA}/src/gfxstream/guest/platform/VirtGpu.cpp
    ${MESA}/src/gfxstream/guest/platform/linux/LinuxSync.cpp
    ${MESA}/src/gfxstream/guest/platform/linux/LinuxVirtGpuBlob.cpp
    ${MESA}/src/gfxstream/guest/platform/linux/LinuxVirtGpuBlobMapping.cpp
    ${MESA}/src/gfxstream/guest/platform/linux/LinuxVirtGpuDevice.cpp
)
target_include_directories(platform_virtgpu PUBLIC
    ${SHIM} ${MESA}/src/gfxstream/guest/platform/include ${MESA}/include ${MESA}/include/drm-uapi ${LIBDRM})

# ---------------- ANativeWindow / gralloc 适配（mesa guest/android） ----------------
add_library(guest_android STATIC
    ${MESA}/src/gfxstream/guest/android/ANativeWindowAndroid.cpp
    ${MESA}/src/gfxstream/guest/android/ANativeWindowEmulated.cpp
    ${MESA}/src/gfxstream/guest/android/GfxStreamGralloc.cpp
    ${MESA}/src/gfxstream/guest/android/GrallocEmulated.cpp
    ${MESA}/src/gfxstream/guest/android/GrallocGoldfish.cpp
    ${MESA}/src/gfxstream/guest/android/GrallocMinigbm.cpp
)
target_include_directories(guest_android PUBLIC
    ${SHIM} ${PLATINC}
    ${MESA}/src/gfxstream/guest/platform/include
    ${MESA}/src/gfxstream/guest/android/include
    ${MESA}/src/gfxstream/guest/android
    ${MESA}/src ${MESA}/include/drm-uapi
    ${GFS}/guest/gralloc_cb/include)

# ---------------- aemu 基础库（编码器用到的子集） ----------------
add_library(aemu STATIC
    ${GFS}/guest/android-emu/aemu/base/AlignedBuf.cpp
    ${GFS}/guest/android-emu/aemu/base/Path.cpp
    ${GFS}/guest/android-emu/aemu/base/Pool.cpp
    ${GFS}/guest/android-emu/aemu/base/Process.cpp
    ${GFS}/guest/android-emu/aemu/base/StringFormat.cpp
    ${GFS}/guest/android-emu/aemu/base/synchronization/AndroidMessageChannel.cpp
    ${GFS}/guest/android-emu/aemu/base/threads/AndroidFunctorThread.cpp
    ${GFS}/guest/android-emu/aemu/base/threads/AndroidThreadStore.cpp
    ${GFS}/guest/android-emu/aemu/base/threads/AndroidThread_pthread.cpp
    ${GFS}/guest/android-emu/aemu/base/threads/AndroidWorkPool.cpp
    ${GFS}/guest/android-emu/aemu/base/Tracing.cpp
    # ASG 环形缓冲实现（与 GoldfishAddressSpace 引用的 mesa 版 ring_buffer.h 配套）
    ${MESA}/src/gfxstream/aemu/ring_buffer.cpp
)
target_include_directories(aemu PUBLIC ${GFS}/guest/android-emu ${SHIM} ${MESA}/src/gfxstream/aemu/include)

# ---------------- 编码器/系统公共层 ----------------
add_library(guest_common SHARED
    # OpenglCodecCommon
    ${GFS}/guest/OpenglCodecCommon/ChecksumCalculator.cpp
    ${GFS}/guest/OpenglCodecCommon/EncoderDebug.cpp
    ${GFS}/guest/OpenglCodecCommon/GLClientState.cpp
    ${GFS}/guest/OpenglCodecCommon/GLESTextureUtils.cpp
    ${GFS}/guest/OpenglCodecCommon/GLSharedGroup.cpp
    ${GFS}/guest/OpenglCodecCommon/IndexRangeCache.cpp
    ${GFS}/guest/OpenglCodecCommon/glUtils.cpp
    # qemupipe（pipe 传输后备，VirtioGpuPipeStream 主用）
    ${GFS}/guest/qemupipe/qemu_pipe_common.cpp
    ${GFS}/guest/qemupipe/qemu_pipe_guest.cpp
    # OpenglSystemCommon（不含 Fuchsia trace；VirtioGpuPipeStream 用 mesa connection-manager 的新实现）
    ${GFS}/guest/OpenglSystemCommon/FormatConversions.cpp
    ${GFS}/guest/OpenglSystemCommon/HostConnection.cpp
    ${GFS}/guest/OpenglSystemCommon/ProcessPipe.cpp
    ${GFS}/guest/OpenglSystemCommon/QemuPipeStream.cpp
    ${GFS}/guest/OpenglSystemCommon/ThreadInfo.cpp
    # mesa：connection-manager（新版 VirtioGpuPipeStream）+ GoldfishAddressSpace（ASG/pipe 双后端）
    ${MESA}/src/gfxstream/guest/connection-manager/GfxStreamConnection.cpp
    ${MESA}/src/gfxstream/guest/connection-manager/GfxStreamConnectionManager.cpp
    ${MESA}/src/gfxstream/guest/connection-manager/VirtioGpuPipeStream.cpp
    ${MESA}/src/gfxstream/guest/GoldfishAddressSpace/AddressSpaceStream.cpp
    ${MESA}/src/gfxstream/guest/GoldfishAddressSpace/GoldfishAddressSpaceStream.cpp
    ${MESA}/src/gfxstream/guest/GoldfishAddressSpace/VirtioGpuAddressSpaceStream.cpp
    ${MESA}/src/gfxstream/guest/GoldfishAddressSpace/goldfish_address_space.cpp
    # renderControl 编码器
    ${GFS}/guest/renderControl_enc/ExtendedRenderControl.cpp
    ${GFS}/guest/renderControl_enc/GfxStreamRenderControl.cpp
    ${GFS}/guest/renderControl_enc/GfxStreamRenderControlConnection.cpp
    ${GFS}/guest/renderControl_enc/renderControl_client_context.cpp
    ${GFS}/guest/renderControl_enc/renderControl_enc.cpp
    ${GFS}/guest/renderControl_enc/renderControl_entry.cpp
    # GLES1/GLES2 编码器（入口表 gl_entry/gl2_entry 分属各自 HAL，避免同名 gl* 符号冲突）
    ${GFS}/guest/GLESv1_enc/GLEncoder.cpp
    ${GFS}/guest/GLESv1_enc/GLEncoderUtils.cpp
    ${GFS}/guest/GLESv1_enc/gl_client_context.cpp
    ${GFS}/guest/GLESv1_enc/gl_enc.cpp
    ${GFS}/guest/GLESv2_enc/GL2Encoder.cpp
    ${GFS}/guest/GLESv2_enc/GL2EncoderUtils.cpp
    ${GFS}/guest/GLESv2_enc/GLESv2Validation.cpp
    ${GFS}/guest/GLESv2_enc/IOStream2.cpp
    ${GFS}/guest/GLESv2_enc/gl2_client_context.cpp
    ${GFS}/guest/GLESv2_enc/gl2_enc.cpp
    # rendercontrol 前端
    # （新式 RenderControl.cpp 不编：与经典 renderControl_entry.cpp 定义同名 rcCompose 等）
    # 共享公共库（ETC1 纹理工具 + 日志）
    ${GFS}/common/etc/etc.cpp
    ${GFS}/common/logging/logging.cpp
    # RanchuWoA 补充定义（kumquat 桩、GetProperty NDK 实现）
    ${SHIM}/context_accessors_shim.cpp
    # protobuf 生成物（glGetProgramBinary 缓存；LITE 运行时，protoc 21.12 生成，链设备 libprotobuf-cpp-lite v21.12）
    ${CMAKE_CURRENT_SOURCE_DIR}/gen/ProgramBinary.pb.cc
)
target_include_directories(guest_common PUBLIC
    ${GFS}/guest/OpenglCodecCommon ${GFS}/guest/OpenglCodecCommon/include
    ${GFS}/guest/qemupipe ${GFS}/guest/qemupipe/include ${GFS}/guest/qemupipe/include-types
    ${GFS}/guest/OpenglSystemCommon ${GFS}/guest/OpenglSystemCommon/include
    ${GFS}/guest/renderControl_enc ${GFS}/guest/renderControl_enc/include
    ${GFS}/guest/GLESv1_enc ${GFS}/guest/GLESv1_enc/include
    ${GFS}/guest/GLESv2_enc ${GFS}/guest/GLESv2_enc/include
    ${GFS}/guest/rendercontrol ${GFS}/guest/rendercontrol/include
    ${GFS}/guest/include
    ${GFS}/third_party/opengl/include
    ${GFS}/host/include
    ${GFS}/common/logging/include ${GFS}/common/base/include ${GFS}/common/etc/include
    ${MESA}/src/gfxstream/guest/iostream/include
    ${MESA}/src/gfxstream/guest/connection-manager
    ${MESA}/src/gfxstream/guest/GoldfishAddressSpace/include
    ${MESA}/src/gfxstream/aemu/include
    ${CMAKE_CURRENT_SOURCE_DIR}/gen
    ${CMAKE_CURRENT_SOURCE_DIR}/../../downloads/protobuf-3.21.12/src
    ${SHIM} ${PLATINC})
set_source_files_properties(${CMAKE_CURRENT_SOURCE_DIR}/gen/ProgramBinary.pb.cc
    PROPERTIES COMPILE_DEFINITIONS "HAVE_PTHREAD=1;ZLIB=0")

# 公共层按 AOSP 结构编成共享库 libOpenglSystemCommon.so（三个 HAL 共用同一份
# HostConnection/ThreadInfo 的 TLS；静态布局会让每个 HAL 各带一份 TLS 副本导致空指针）
set_target_properties(guest_common PROPERTIES OUTPUT_NAME "OpenglSystemCommon" SUFFIX ".so")
target_link_options(guest_common PRIVATE "-Wl,-soname,libOpenglSystemCommon.so")
target_link_libraries(guest_common PUBLIC aemu guest_android platform_virtgpu)
target_link_directories(guest_common PUBLIC ${DEVLIBS}/system ${DEVLIBS}/vendor)
target_link_libraries(guest_common PUBLIC log nativewindow sync cutils drm protobuf_lite_static)


# ---------------- 三个 HAL 库 ----------------
# 入口表由 gl.cpp/gl2.cpp #include 各自的 *_entry.cpp（该 TU 内 GET_CONTEXT 已定义为 TLS 路径，
# 不会再定义 setContextAccessor）；HostConnection 对访问器的引用由 weak 定义满足。
function(gfxstream_hal name srcs out soname)
    add_library(${name} SHARED ${srcs})
    set_target_properties(${name} PROPERTIES OUTPUT_NAME ${out} SUFFIX ".so")
    target_include_directories(${name} PRIVATE ${GFS}/guest/egl ${GFS}/guest/GLESv1 ${GFS}/guest/GLESv2)
    target_link_libraries(${name} PRIVATE guest_common)
    # 设备库：NDK sysroot（log/nativewindow）+ 抽取的设备 .so（sync/cutils/drm/protobuf）
    target_link_directories(${name} PRIVATE
        ${DEVLIBS}/system ${DEVLIBS}/vendor)
    target_link_libraries(${name} PRIVATE log nativewindow sync cutils drm protobuf_lite_static)
    target_link_options(${name} PRIVATE "-Wl,-soname,lib${soname}.so")
endfunction()

# 库名与上游一致用 _emulation 后缀（eglDisplay.cpp 按 libGLESv2_emulation 加载，
# 覆盖镜像里原有的 goldfish-pipe 版同名库；ro.hardware.egl=emulation）
gfxstream_hal(egl_gfxstream "${GFS}/guest/egl/egl.cpp;${GFS}/guest/egl/eglDisplay.cpp;${GFS}/guest/egl/ClientAPIExts.cpp" "EGL_emulation" "EGL_emulation")
gfxstream_hal(glesv2_gfxstream "${GFS}/guest/GLESv2/gl2.cpp" "GLESv2_emulation" "GLESv2_emulation")
gfxstream_hal(glesv1_gfxstream "${GFS}/guest/GLESv1/gl.cpp" "GLESv1_CM_emulation" "GLESv1_CM_emulation")
