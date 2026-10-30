# 系统架构与引导链

本文描述 RanchuWoA 的整体架构、机器模型、磁盘布局与 Android 引导链。图形栈细节见
[graphics.md](graphics.md)，自研的 goldfish/virtio 设备协议见 [goldfish-devices.md](goldfish-devices.md)，
GSI 适配见 [gsi-guide.md](gsi-guide.md)，排查手段见 [debugging.md](debugging.md)。

## 1. 总览

```
┌─ Windows on ARM 宿主 ─────────────────────────────────────────────────┐
│  avdctl（实例管理）  SDL 窗口 + 侧边工具栏（Tk，独立进程）             │
│  QEMU 11.1.2（补丁版，WHPX 硬件虚拟化，ARM64 原生）                     │
│   ├─ -M virt,ranchu-devices=on：goldfish pipe / sync / battery        │
│   ├─ virtio：blk×3、net(slirp)、gpu、input(touch+keyboard)、snd、vsock │
│   └─ 可选宿主渲染器：gfxstream → rutabaga_gfx → ANGLE → Direct3D 11    │
└───────────────────────────────────────────────────────────────────────┘
        ▲ virtio-gpu 扫描输出 │ virtio-vsock（qemud 服务、adb）│ goldfish pipe（图形）
┌─ Android 15 guest（官方镜像或 GSI）────────────────────────────────────┐
│  woa gralloc（AIDL IAllocator v2 + stable-C IMapper v5）               │
│  GLES：SwiftShader（guest CPU）或 gfxstream 编码器（宿主 GPU）         │
│  hwc3（client 合成 + DRM 枚举显示器）→ virtio-gpu scanout             │
│  qemud 服务（传感器/GPS/剪贴板/…）→ virtio-vsock → QEMU 内置服务       │
└───────────────────────────────────────────────────────────────────────┘
```

设计原则：**guest 尽量保持原样**。官方镜像的内核、init、surfaceflinger、hwc3、音频 HAL 都原封不动，
替换/补齐的只有三块——vendor 分区里的 gralloc 与 GLES 实现（[graphics.md](graphics.md)）、
QEMU 侧的 goldfish/qemud 设备（[goldfish-devices.md](goldfish-devices.md)）、引导参数（本文）。

## 2. 为什么是 QEMU + WHPX

- Google 官方模拟器（AOSP `platform/external/qemu`）不发布 Windows ARM64 宿主版；WSA 已退役。
- QEMU 11.0（2026-04）合入了 **ARM64 Windows 宿主的 WHPX 加速**：`-accel whpx` + `virt` 板 + arm64 客户机。
  宿主要求 Windows 11 24H2 及以上（WHP ARM64 API 与 ARM64 分区支持）。限制：仅 `virt` 板；SVE/SME 不可用
  （Android 不需要）；无快照/live migration（GIC save/restore 未实现）。
- `-cpu host` 直接透传宿主 CPU（TCG 不支持 `-cpu host`，日志里出现真实 MIDR 即证明 WHPX 生效）。
  TCG 同 ISA 慢约 8 倍，只作无虚拟化环境的兜底。
- QEMU 的 Windows 构建只支持 MinGW 工具链；本项目用 MSYS2 CLANGARM64 环境源码构建（见 README「从源码构建」）。

## 3. 官方镜像构成

基线镜像：`google_apis/arm64-v8a-35`（`https://dl.google.com/android/repository/sys-img/google_apis/`
索引在 `sys-img2-3.xml`，avdctl `prepare` 自动下载）。

| 文件 | 大小 | 说明 |
|---|---|---|
| kernel-ranchu | 14.5MB | gzip 压缩 arm64 Image（GKI 6.6） |
| ramdisk.img | 2.1MB | `-initrd`；**两段串联 cpio**（见 §5） |
| system.img | 3.89GB | GPT 裸盘：`vbmeta`(1MB) + `super`(动态分区，erofs logical) |
| vendor.img | 111MB | 单分区 vendor，goldfish HAL 都在这 |
| encryptionkey.img | 18.9MB | 单分区 metadata（FBE 元数据密钥） |
| VerifiedBootParams.textproto | 356B | AVB 参数（vbmeta size/hash） |
| advancedFeatures.ini / kernel_cmdline.txt | | VirtioInput 等开关；内核命令行仅 `8250.nr_uarts=1` |
| data/ | | 空数据盘模板、local.prop 等（userdata 由首启派生，包内无 userdata.img） |

## 4. 机型：`virt` + ranchu-devices

WHPX 只支持 `virt` 板，ranchu 兼容层做成了 virt 的开关：

```
-M virt,ranchu-devices=on,ranchu-system-disk=sys,ranchu-metadata-disk=meta,ranchu-data-disk=data,gic-version=3
```

- QEMU 运行时动态生成 FDT（内核无内建 ranchu DTB），`ranchu-devices=on` 时注入 Android 固件节点
  （`/firmware/android/fstab` 等）并创建 goldfish 设备。
- 上游 ranchu 的 goldfish_fb/audio/events 在 Android 15 里已 virtio 化（virtio-gpu / virtio-snd /
  virtio-input），不再需要；实际创建的是 pipe、sync、battery 三个。
- 本项目的设备布局（与上游 ranchu 基本一致，两处有意偏离）：

| 设备 | MMIO 基址 | IRQ（SPI） | 备注 |
|---|---|---|---|
| PL011 UART | 0x09000000 | 1 | 串口控制台 |
| goldfish_pipe | 0x0a010000 | 6 | 仅图形管道使用（A15 内核节点名 `goldfish_pipe_dprctd`） |
| goldfish_sync | 0x0a020000 | 81 | 上游是 SPI 7，与 virt 板 PL061 冲突，改 81 |
| goldfish_battery | 0x0a040000 | — | 与官方模拟器一致（vendor sysfs 策略按此路径打标签） |
| virtio-mmio ×32 | 0x0a000000 起，间隔 0x200 | 16–47 | 槽 29/30/31 钉给三块系统盘 |
| RAM | 0x40000000 | — | 0x10000000–0x40000000 预留 PCI |

- goldfish_address_space 是 PCI 设备（vendor 0x607D / device 0xF153，64 位共享内存 BAR），
  寄存器级细节见 [goldfish-devices.md](goldfish-devices.md)。

## 5. 磁盘与引导链

### 5.1 三块盘与槽位钉死

| 盘 | 内容 | virtio-mmio 槽 | guest 盘符 |
|---|---|---|---|
| system.img | vbmeta + super（system/vendor/product/system_ext/system_dlkm 为 logical 分区） | 29 | vda |
| encryptionkey.img | metadata 分区 | 30 | vdb |
| data.img | /data，裸盘 ext4（无 FBE） | 31 | vdc |

两个坑决定了必须显式钉槽位：

1. **QEMU `-device` 顺序与盘符是反的**：`-device` 顺序 [A,B,C] → vda=C, vdb=B, vdc=A（virt 的
   transport 总线分配顺序所致，上游注释自认无法保证顺序）。
2. **fstab 里 metadata 钉死在平台路径**：`/dev/block/platform/a003c00.virtio_mmio/by-name/metadata`
   ——即槽 30。first_stage_mount 只为 `androidboot.boot_devices` 列出的平台设备建 by-name 链接，
   盘不在预期槽位上就挂不上。

因此补丁给 virt 机型加了 `ranchu-system-disk` / `ranchu-metadata-disk` / `ranchu-data-disk` 三个属性
（填 `-drive` 的 id），在 virtio 总线创建后用 `qdev_realize_and_unref` 显式挂到指定槽的 transport 总线。

### 5.2 内核命令行

```
console=ttyAMA0 qemu=1 androidboot.qemu=1 androidboot.hardware=ranchu
androidboot.boot_devices=a003a00.virtio_mmio,a003c00.virtio_mmio
androidboot.hardwareegl=emulation|swiftshader  rootwait init=/init 8250.nr_uarts=1
```

`androidboot.boot_devices` 是**最重要的一个参数**：first_stage_mount 的 DeviceHandler 只为列表里的
平台设备创建 by-name 符号链接，缺了它 super/metadata 挂载失败，init 会走 InitFatalReboot 静默重启进
bootloader（内核日志最后一行 `reboot: Restarting system with command 'bootloader'`，除此之外没有任何
错误输出——这是 stock virt 机引导此镜像"秒退"的根因）。

### 5.3 ramdisk：两段串联 cpio

`ramdisk.img` 是两个 cpio 归档直接串联（外层 lz4-legacy，魔数 `0x184C2102`）：

- 归档 1：`init`(3.7MB) + `system/etc/ramdisk/build.prop`
- 归档 2（GKI 模块段，在 `first_stage_ramdisk/` 下）：`fstab.ranchu` + `lib/modules/`（virtio_blk、
  virtio_mmio、virtio_pci 等 9 个 .ko）

重打包必须保留两段结构（`scripts/repack_cpio.py` 按 newc 格式手工重建；msys cpio 重打包的归档内核不认）。
goldfish 驱动（pipe/battery/sync/address_space 四个 .ko）不在 ramdisk 里，在 vendor.img 中按需加载。

### 5.4 FBE：关闭而非实现

官方镜像的 /data 是 FBE 加密（`fileencryption=aes-256-xts:aes-256-cts, keydirectory=/metadata/vold/...`），
密钥链路需要 keymaster 包装，模拟环境凑不齐。官方模拟器自己的做法也是关 FBE（宿主机重写 fstab），我们同样：

- ramdisk 的 `fstab.ranchu` 里去掉 /data 的 `fileencryption=...keydirectory=...`（重打包实现）；
- super 内 vendor 分区的 fstab 位于 EROFS 未压缩区，用**等长字节补丁**把加密标志串替换为
  `latemount,wait×18,check×4`（`scripts/vendor_install.py`，123 字节 ↔ 123 字节）；
- metadata 分区保留（部分组件仍假设它存在），data.img 由 `avdctl create` 用 e2fsprogs（Alpine 辅助 VM）制作。

### 5.5 AVB

镜像直刷、无 AVB footer（分区名在 GPT 里，vbmeta 参数经 `VerifiedBootParams.textproto` 由命令行传入）。
本项目未启用验证启动，vbmeta 只作占位。

## 6. SELinux

默认 **enforcing**（`avdctl start --selinux permissive` 可临时切）。官方镜像的 vendor 策略按官方模拟器的
设备布局编写，我们的自研组件与布局差异需要在 vendor 策略里补齐（`gfx/vendor-overlay/etc/selinux/`
下的 cil 追加，`vendor_install.py` 幂等写入并删掉 precompiled_sepolicy 让 init 每次现编，约 0.5 秒）：

1. `mapper/woa` 的 service_contexts 条目（IMapper passthrough 的 servicemanager find 权限）；
2. gralloc 缓冲从 **DMA-BUF system heap** 分配（`/dev/dma_heap/system`，常规 Android gralloc 的做法，
   客户端进程无需额外规则；memfd 方案会被打上需要逐进程放行的标签）；
3. goldfish-battery 固定在 0xa040000（vendor sysfs 策略按官方路径 `a040000.goldfish_battery` 打标签，
   挪位置 system_suspend 就读不到唤醒源）;
4. `system_server` 允许 `execmem`（ColorFade 灭屏动画在 system_server 里用 GLES 画，SwiftShader 的
   着色器 JIT 需要；官方镜像自己也有这个问题）。

## 7. vendor 分区改造

自研组件（woa gralloc、两套 GLES、init rc、VINTF 声明、SELinux 补充）都要装进 super 里的 vendor 分区。
vendor 是 EROFS 只读，改造流程（`scripts/vendor_install.py` 一条命令）：

1. `scripts/superimg.py`：改写 liblp 元数据——vendor 是 super 里最后一个分区，后面有空闲空间，直接
   加长 extent、清 READONLY 属性（4 份元数据副本，重算 SHA256），并给整盘加 ext4 所需的额外空间；
2. `scripts/alpine_job.py`：起一个 Alpine 辅助 VM（stock initramfs 后拼一段带 e2fsprogs 的 cpio），
   无人值守执行 resize2fs → `e2fsck -E unshare_blocks`（去掉 erofs 的去重共享块）→ 加日志 →
   debugfs 写入文件/权限/SELinux 标签。

开发循环中也可以在 guest 里 `su 0 mount -o remount,rw /vendor` 直接改（改完 remount,ro 再关机）。

## 8. 生命周期

- **create / wipe**：制作 data.img 后在后台 `-display none` 跑完首次开机（写 device_provisioned 等一次性
  初始化、把「私人 DNS」设为关闭），等 `ranchu: guest boot completed` 再正常关机。用户第一次看到的就是
  干净的二次开机，也省掉首启时间。
- **stop / 关窗口**：QMP `system_powerdown` → ranchu-vsock 设备自己当 adb 客户端连 guest adbd 执行
  `svc power shutdown`（与电源菜单同一流程，广播、AMS/PM 收尾、干净卸载 /data）；超时兜底强制退出。
  `-kernel` 直引没有 ACPI GED，原生 `system_powerdown` 只是按一下电源键（Android 当锁屏）。
- **guest 内重启**（`adb reboot`、电源菜单）：上游 WHPX 的分区 reset 在多 vCPU 下失败
  （其余 vCPU 还在 `WHvRunVirtualProcessor` 里时 `WHvResetPartition` 返回 `WHV_E_INVALID_VP_STATE`），
  表现为第二次开机卡在 `smp: Bringing up secondary CPUs`。补丁改为标记后由 reset 后第一个恢复寄存器的
  vCPU 执行分区 reset，并重设只在创建时配置过的每核寄存器（MIDR/MPIDR、GICR 基址）；重启后约 21–30 秒
  再次开机完成。
- 实例的端口与文件布局见 README「实例文件与端口」。
