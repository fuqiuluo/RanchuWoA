# 参考源码摘录

调研期间从上游项目摘录的源码片段，仅供对照阅读，**不参与本项目编译**。
每个文件以其自身的版权/许可头为准（多数为 Apache-2.0；`kernel_sync.c`、`aosp_goldfish_sync.c` 为 GPL-2.0）。
文件名前缀是本项目调研时的归类习惯：`aosp_*` 摘自 Android 模拟器版 QEMU（android.googlesource.com 上的
platform/external/qemu），`kernel_*` 摘自 Android goldfish 内核，其余按内容命名。

| 文件 | 来源 |
|---|---|
| `adbd_main.cpp`、`adb_transport.cpp`、`adbd_transport_local.cpp` | AOSP adb（platform/packages/modules/adb） |
| `FsCrypt.cpp`、`KeyStorage.cpp`、`MetadataCrypt.cpp`、`VoldNativeService.cpp`、`vold_Keystore.cpp` | AOSP vold（platform/system/vold） |
| `Gralloc4.cpp`、`Gralloc5.cpp`、`GraphicBufferMapper.cpp`、`IMapper*`、`StandardMetadataType.aidl` | AOSP frameworks/native（libui / gralloc） |
| `gralloc5/aidl-Allocator*`、`mapper.minigbm.xml` | minigbm 的 gbm_gralloc（platform/external/minigbm） |
| `hwc3/*` | Android 模拟器的 hwcomposer3 HAL（goldfish/ranchu） |
| `ranchu.c`、`aosp_gas_pci.c`、`aosp_goldfish_sync.c`、`goldfish_pipe.c` | Android 模拟器版 QEMU |
| `kernel_gas.c`、`kernel_goldfish_pipe*.c/h`、`kernel_sync.c` | Android goldfish 内核 |
| `ANDROID-KERNEL.TXT`、`ANDROID-QEMU-PIPE.TXT`、`ANDROID-QEMUD-SERVICES.TXT`、`GOLDFISH-VIRTUAL-HARDWARE.TXT` | Android 模拟器随附文档 |
