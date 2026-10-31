# GSI 适配指南

可以把基础镜像里的 system 分区换成别的 Android 15 GSI（Generic System Image）或厂商系统的社区移植包，
沿用我们的 vendor（图形、输入、vsock 服务都还在）。本文是通用阻塞点清单与实测记录；快速上手命令见
README「运行 GSI」。

> 原则：所有修改只作用于本地自持的镜像副本（实验盘），补丁以脚本形式提供（`scripts/gsi_disk.py`、
> `scripts/coloros_gsi_fixes.py`、`scripts/a15_hyperos_dada_fixes.py`、`scripts/a14_coloros_opd2404_fixes.py`、
> `scripts/a16_coloros_opd2404_fixes.py`、`scripts/a17_coloros_plg120_fixes.py`，以及基础内核的
> `scripts/kernel_fixes.py`），对镜像版本有校验、可重复运行。
>
> **命名规范**：实验盘（`images/gsi/`）按 `{Android 版本}_{系统+版本}_{机型}.img` 命名，版本与机型取自
> 镜像里的 `ro.build.display.id.show`（前三段）与 `ro.build.product`，如 `a17_coloros16.0.0_plg120.img`、
> `a15_hyperos2.0.215.0_dada.img`、`a14_coloros14.1.0_opd2404.img`；版本专属补丁脚本同名去掉版本段
> （Python 模块名不能带点），如 `a17_coloros_plg120_fixes.py` 配 `a17_coloros16.0.0_plg120.img`。注意 A17
> 预览这版：设置里「软件版本」显示 16.0.0.01，但那是 `display.id.show` 钉住的显示值，内部 build 号是
> `PLG120_17.0.0.12(CN01)`——命名跟随用户可见版本，真相在 RESEARCH §29。

## 1. 工具

| 脚本 | 作用 |
|---|---|
| `scripts/gsi_disk.py <基础system.img> <GSI> <输出盘>` | 生成实验盘。GSI 往往比原 system 大得多（super 放不下）：复制基础盘后在末尾追加空间，重写 GPT（主/备份 GPT、CRC、保护性 MBR）与 liblp 的 4 份元数据（块设备大小、分区组上限、system 的唯一 extent 指向新区域），再写入 GSI（支持 sparse，DONT_CARE/零填充直接跳过）。最后自动跑 `vendor_install --gsi` |
| `scripts/gsi_undef_types.py <盘>` | 找出 GSI 的 contexts 文件引用了、但最终策略里没定义的 SELinux 类型（**离线提前跑**，别等开机失败再查） |
| `scripts/kernel_fixes.py [内核文件]` | 基础内核（gzip 的 arm64 Image）指令级补丁：lpm_trie 的 UBSAN 越界分支改跳 -ENOSPC 错误返回（Android 16 的 connectivity 调 BPF 更新 LPM trie 会踩中 trap 模式直接炸内核，§2.7）。对所有 GSI 生效；`avdctl prepare --force` 重建基础镜像后要重跑 |
| `scripts/coloros_gsi_fixes.py` / `a15_hyperos_dada_fixes.py` | 各自的厂商框架适配补丁，见 §3 |
| `scripts/a14_coloros_opd2404_fixes.py` | Android 14 的 ColorOS GSI 跑在我们 Android 15 vendor 上的全部适配（§2.6），在 coloros_gsi_fixes 之后运行 |
| `avdctl create <名字> --system <盘>` | 用实验盘建实例（写时复制层以它为后备，`prepare --force` 不会重建）；`start --verbose-boot` 把内核/init 全量日志记进 `logs/console.log` |

实验盘是基础盘的拷贝：`avdctl prepare --force` 之后要用 `gsi_disk.py` 重新生成（ColorOS/HyperOS 再跑
各自的 fixes），然后 `avdctl edit <名字> --system <盘>` 重新挂上（只重建写时复制层，userdata 保留）。

## 2. 通用阻塞点（按出现顺序）

### 2.1 first stage 与 SELinux

- ramdisk fstab 没有 avb 标志时不需要动 vbmeta；GSI 自带的 `skip_mount_cfg` 会跳过 /product、/system_ext
  （用 GSI 自己的），vendor 与 system_dlkm 照常挂我们的。
- **user 版 GSI 强制 enforcing**（无视 `androidboot.selinux=permissive`）。好在基础 vendor 已经按
  enforcing 配套（见 [architecture.md](architecture.md) §6），GSI 侧还要处理：
  - contexts 文件引用 vendor 策略里没有的类型 → init 设属性上下文时 fatal。用 `gsi_undef_types.py`
    找全，把缺的类型**只补声明**（`gfx/vendor-overlay/gsi/gsi_sepolicy.cil`），不加规则。
  - OPLUS 系 init（`oplus_selinux.cpp`）在调试版（`ro.debuggable=1` 的移植包）会编译 `*_debug` 版策略，
    要求 `/vendor/etc/selinux/vendor_sepolicy_debug.cil` 与 `plat_pub_versioned_debug.cil`——给与正式版
    内容相同的副本即可，缺了会 "Unable to open SELinux policy" → InitFatalReboot 无限重启进 bootloader。

### 2.2 源机型的属性抢先

init 先加载 build.prop、后执行 vendor 的 init 脚本，而 **ro 属性只能设一次**：GSI 里源机型的
`ro.sf.lcd_density`、`ro.opengles.version` 等会抢在 vendor 按内核命令行设的值之前生效——系统按真机
密度排版（状态栏、时钟大一倍）、应用以为有 GLES 3.2。处理：fixes 脚本把 build.prop 里这几行注释掉
（OPLUS 的同名属性在 `/my_product`、由 OPLUS init 在 vendor 之后加载，不受影响；小米的在
`/product/etc/build.prop` 与 `/system/build.prop`，要处理）。

### 2.3 厂商框架对真机的假设

移植包的 system 按真机硬件环境写死了假设，裸 virtio/QEMU 环境满足不了就会崩（往往表现为
system_server 周期性崩溃或开机完成后无限重启）。实测遇到的三类（修法都在 fixes 脚本里，按类定位与
校验，镜像版本变了会拒绝打而不是打错位置）：

- **CPU 簇拓扑**（ColorOS）：OPLUS 框架按真机大小核排序 BitSet 区间，模拟器的 dummy-cpufreq 每个 CPU
  一个 policy、频率相同，排出来的排列会让 `calcAffinityExcludeSuperBigCore` 抛
  IndexOutOfBoundsException → system_server 每分钟崩一次。修法：在 oplus-services.jar 的 dex 里把该方法
  首条指令改成 `return-void`（重算 dex 校验和，删对应 odex/vdex/art）。
- **/data 在 device-mapper 上**（HyperOS）：小米 vold 的 f2fs 空闲维护从 /proc/mounts 找 `/data` 的挂载
  设备名并 `strstr(fsname, "dm")` 直接赋 std::string——裸 virtio 盘 `/dev/block/vdc` 匹配不到返回 NULL
  → SIGSEGV → `reboot(vold-failed)` 每轮开机完成后约 10 秒重启。修法：把 vold 二进制里 strstr 的针
  换成同长度的 `"vd"`，后续 sysfs 路径不存在、打开失败即跳过。
- **蓝牙 Adapter 非空**（HyperOS）：`MiuiNetworkMonitor` 在 system_server 里假定
  `BluetoothAdapter.getDefaultAdapter()` 非空直接 getProfileProxy → NPE → 每 17 秒崩一次，永远到不了
  开机完成。修法：实验盘 vendor 的特性声明不再 `<unavailable-feature>` 蓝牙（蓝牙默认关闭时栈根本
  不启动，前提条件消失）。注意这与官方镜像相反——官方镜像要去掉蓝牙特性声明才能阻止蓝牙栈反复 abort，
  区别在于有没有厂商代码去拿 Adapter。（ColorOS 14 例外：它的蓝牙管理会主动开 BLE，而 A14 蓝牙栈在这里
  打不开 wake_lock 反复 abort，所以那边维持蓝牙不可用，让几个 OPLUS 应用 NPE 后由 AMS 退避重启。）
- **只认高通/联发科**（ColorOS）：OPLUS 代码到处按 `ro.boot.hardware` 前缀、`ro.hardware == "qcom"`、
  `ro.board.platform` 前缀判断平台，`ranchu` 两边都不中，有的地方拿到 null 就崩（Telecom 的 SoC 解耦层、
  日志服务）。`ro.boot.hardware` 不能改（vendor 的 init.ranchu.rc、fstab 靠它），只在会崩的地方把判断
  改成"高通"（源机型是骁龙），dex 指令级补丁。高通的 BoostFramework 也一样：zygote 只在
  `ro.boot.hardware` 以 qcom 开头时才加载 `libqti_performance.so`，其余 perf 调用的异常被吞掉，但 ColorOS 14
  在应用进程启动时会另起线程直接调 JNI，`UnsatisfiedLinkError` 直接杀死 system_server（表现为点开应用整机
  重启框架）。相关类都在启动类路径上，改了启动镜像会作废，所以在 services.jar 的调用方截断。
- **32 位程序**：宿主的骁龙 X Elite（Oryon 核）不支持 AArch32，GSI 里的 32 位可执行文件一律跑不起来。
  ColorOS 14 的 mediaserverwrapper 按 `ro.mediaserver.64b.enable` 决定 exec 64 位还是 32 位的 mediaserver，
  源机型的 vendor 设了这个属性、我们的没有——没有 mediaserver 会连锁拖垮 MediaProvider 与外部存储。

### 2.4 分辨率与密度

厂商界面按源机型的屏幕调（比例、dp 尺寸，有的参数直接按像素写死）。配法：

1. 查源机型的分辨率和 `ro.sf.lcd_density`（GSI Treble 化可能抹掉机型信息，就从 GSI 里残留的
   build.prop、面板配置找，或对照真机资料）；
2. 按宿主屏幕高度选一个**等比缩小**的分辨率（密度同比例缩小，dp 尺寸与真机一致，布局不变；宿主
   屏幕高度有限，原分辨率本来也要缩到一半显示，直接用一半还能明显提速）；
3. `avdctl create/edit --width W --height H --density D`；平板固件（`ro.build.characteristics` 含 tablet）
   给横屏分辨率，并去掉 build.prop 里的 `ro.surface_flinger.primary_display_orientation`（平板面板常是物理
   竖装、SF 固定旋转 90°/270°，我们的虚拟屏本身就是横的，再转整个界面就横躺了）；
4. MIUI 系的应用会按屏幕物理 PPI 再算一遍自己的密度（AutoDensity：PPI × 1.1399 × deviceScale，
  deviceScale = min(1, 短边英寸/2.8)）——virtio-gpu 的 `dpi=` 属性让 EDID 按实例密度报物理尺寸后，
  应用密度与真机比例一致（如 HyperOS 实例 600 宽、系统 260dpi、应用 246dpi，与小米 15 的
  1200px@460ppi 同为约 392dp 宽）。改 `--density` 不影响这个应用密度，想再大用系统设置的字体/显示大小。

### 2.5 其他

- **没有开机向导的 GSI** 不会写 `device_provisioned` / `user_setup_complete`，Android 会一直不响应
  主屏幕键和最近任务（"Not going home because user setup is in progress"）。QEMU 每次开机完成后经
  adbd 自动补成 1（`boot-cmd`，见 [goldfish-devices.md](goldfish-devices.md) §4.2），无需手动处理。
- **超时灭屏很快**（ColorOS/HyperOS 锁屏几秒、桌面一分钟）：工具栏电源键或 `adb shell input keyevent
  WAKEUP` 亮屏，`svc power stayon true`（工具栏「保持唤醒」）常亮。
- GMS 域验证异常（`not the approved domain verification agent`）之类移植包常见噪音，AMS 带退避重启，
  无害。
- 缺大量厂商 HAL 的功能不可用是正常的（相应服务反复退出，只要不占 CPU 就无害）。
- 移植者改过的 `persist.sys.usb.config` 要是 init.usb*.rc 认识的组合（如 `mtp,adb`），写成 `adb,mtp` 时
  adbd 永远不启动。
- ColorOS 的"权限监控"（开发者选项"禁止权限监控"）会收回 adb shell 的 WRITE_SECURE_SETTINGS，开机命令写
  provisioned 标志就失败；它的开关属性会被 OPLUS 应用写回持久化，a14_coloros_opd2404_fixes 直接放开 shell 这一处。

### 2.6 跨大版本：Android 14 的 GSI

我们的 vendor 和内核是 Android 15 的（VINTF 8.0、SELinux 202404、android15-6.6 GKI），Treble 只保证新
system 配旧 vendor，反过来要逐项降级。ColorOS 14 实测需要（全在 `a14_coloros_opd2404_fixes.py`，按出现顺序）：

| 症状 | 原因 | 处理 |
|---|---|---|
| InitFatalReboot，无限重启 | A14 GSI 没有 `mapping/202404.cil` | 以基础镜像的映射为模板，裁掉 A14 没有的类型 |
| HAL 全部 CANNOT LINK | A14 linkerconfig 没有 `ro.vndk.version` 就生成 legacy 配置 | vendor build.prop 补 `ro.vndk.version=34` |
| 所有 HAL 注册被拒 | A14 libvintf 不认 `manifest version="8.0"` | vendor 的 VINTF 改 7.0；vendor APEX（8.0 碎片、minSdk 35）全部禁用 |
| vendor 组件缺符号 | A15 的 NDK 符号（libui 等） | 把 A15 的 libbinder(_ndk)/libui 等装进 vendor，另加直通 mapper 的 shim |
| zygote 反复重启 | 没有 app_process32 | 删掉 zygote_secondary 定义 |
| system_server "duplicate authenticator" | A14 框架把 A15 指纹 HAL 的同一传感器注册两次 | 删掉指纹/人脸 HAL 的 VINTF 声明与 rc（声明文件要删除，不能改名占位） |
| 所有应用 setgroups EINVAL | 厂商组名（`vendor_launcher` 等）解析不到，A14 把 -1 放进附加组 | 写进 `/vendor/etc/group`（bionic 只认带分区前缀的组名：`system_`/`vendor_`…） |
| systemReady 时 SIGABRT | A14 libvintf 把内核版本串里的 android15 换算成越界等级 | 补丁 `gkiAndroidReleaseToLevel`，越界返回 UNSPECIFIED |
| 开机弹"设备内部出现了问题" | 设备矩阵要求 System SDK 35、框架矩阵没有 6.6 内核条目 | system-sdk 改 34；框架矩阵补 6.6 条目 |
| 桌面黑屏、launcher ANR | 高通 perfservice 读不到 `hwservicemanager.ready`，HIDL 无限等待，调它的进程全卡死 | vendor 策略补那条 allow |

外加 ColorOS 自身的几处（§2.3 的平台判断、32 位 mediaserver，§2.5 的 adb 与权限监控）。

## 2.7 跨大版本（向上）：Android 16 的 GSI

A16 system 配 A15 vendor 是真机 OTA 升级的方向，Treble 原生支持（GSI 自带 vendor 策略版本的映射、
libvintf 认旧清单与内核等级），M8 那些降级补丁全都不需要。ColorOS 16（M9）实测只需要：

| 症状 | 原因 | 处理 |
|---|---|---|
| zygote 每 5 秒被杀、system_server 无声死亡 | netd 崩溃（dlopen 被移植包掏空的 oplusex 模块，错误分支 `strlen(dlerror()=NULL)`），netd.rc 的 onrestart 连坐杀 zygote | `liboplusNetd.so` 的 `initNetd` 改立即返回 0（按 .dynsym 定位） |
| 开机即 Kernel panic，还连带 QEMU 崩 | A16 connectivity 从 ConnectivityService 调 `bpf(BPF_MAP_UPDATE_ELEM)` 更新 LPM trie，android15-6.6 GKI 的 `-fsanitize-trap` 边界检查（brk）+ panic-on-oops 直接炸；brk 只能被跳进，NOP 它会掉进后面的非指令数据 | `kernel_fixes.py`：把 3 条越界 `b.hs` 改跳现成的 -ENOSPC 错误路径（等同上游后来的显式校验） |
| 画面横躺（显示变成竖屏尺寸） | C16 移植包把 OPLUS 的 `my_product` 打进了 system 分区，`/my_product/build.prop` 在 vendor 之后加载，420 密度与 SF 270° 转向抢跑（C15 时代的包不含它，只注释 system/build.prop 不够） | `my_product/build.prop` 里同样注释那几行 |
| 没有串口 logcat | A16 平台策略删了 logd→串口 allow（同 A14） | vendor 策略补 4 条 allow（类型走 GSI 自带 mapping） |

另外两条工具教训：dex「首条指令换 return-void」必须把**剩余指令单元全部清成 nop**（A16 的
`calcAffinityExcludeSuperBigCore` 签名变了且首指令 3 个代码单元，只换单元 0 会 VerifyError 杀
system_server）；运行时 grep 与镜像事实矛盾时先怀疑自己的 adb 引号转义（`\|` 会被吃掉输出假阴性），
prop 来源排查直接对镜像裸数据 grep 最快。

## 2.8 跨大版本（向上）：Android 17 预览的 GSI

A17 预览（CinnamonBun，SDK 36.1）沿用 §2.7 的大部分补丁，新增两类坑（M10，全在
`scripts/a17_coloros_plg120_fixes.py` 与 `kernel_fixes.py` K2）：

| 症状 | 原因 | 处理 |
|---|---|---|
| system_server 每 5 秒 `Check failed: new_capacity <= Size()` SIGABRT | A17 libart 的 ClearGrowthLimit 用**写死的 512MB** 调 ClampGrowthLimit；我们环境 -Xmx 被按 192m 写死（来源不明，改属性/RAM 都没用） | libandroid_runtime.so 里把读的属性名换等长哑名 + 默认 16m→1g，-Xmx 恒为 1g |
| 给 services.jar 打 dex 补丁后 `ClassNotFoundException: SystemServer` | **A17 的 zygote 按 jar 校验和静默剔除 SYSTEMSERVERCLASSPATH 上改过的 jar**（即使 zip/dex 校验全对；日志有 ClassLoaderContext size mismatch 线索） | A17 起 framework 路径上的 jar 一律不能打 dex 补丁，改走 native 库（.so 验证可打）或 system_ext 的 jar |
| IPv6 发包时 Kernel panic（trie_lookup_elem） | lpm_trie 的 UBSAN 误报第二处：cgroup BPF 程序查询路径（K1 只修了更新路径） | kernel_fixes.py K2：3 条越界分支改跳"返回 NULL（未命中）" |
| 桌面壁纸纯黑 | 默认壁纸是 OPLUS 视频动态壁纸（GL LUT 着色器 + external OES），该 GL 路径 gfxstream 不支持——引擎起播前就放弃、一帧未画（静态壁纸 A/B 实测正常，同 §26.2 黑壁纸一族） | 临时：设静态壁纸；真修要走 gfxstream 侧 |

上游自己修好的：startProcess 的 perfHint 路径 A17 已砍（不用补）；Telecom 的 SocDecouplingCenter
继续不存在。首次开机含 /data 全量编译约 7 分钟。

## 3. 实测记录

| GSI | 源机型 | 实例参数 | 结果 |
|---|---|---|---|
| AOSP Android 15（官方 aosp_arm64 user 版） | — | 默认 720x1280 | 直接开机到锁屏；user 版 adb 需屏幕授权 |
| ColorOS 15（Mystic GSI 移植包） | 一加 Ace 2V（PHP110，1240x2772@560） | `--width 620 --height 1386 --density 280 --ram 6144 --cores 6` | 跑 coloros_gsi_fixes.py 后 50–125 秒开机到桌面，触摸/设置/滚动正常；两种图形后端都可用 |
| HyperOS 2.0（Mystic GSI 移植包） | 小米 15（dada，1200x2670@520） | `--width 600 --height 1335 --density 260 --ram 6144 --cores 6` | 跑 a15_hyperos_dada_fixes.py 后约 2 分钟开机到桌面；壁纸、状态栏、通知栏/控制中心、触摸、联网、设置正常 |
| ColorOS 14（Mystic GSI 移植包，Android 14） | 一加 Pad Pro（OPD2404，平板，3000x2120@420） | `--width 1500 --height 1060 --density 210 --ram 6144 --cores 6` | 跑 coloros_gsi_fixes.py 与 a14_coloros_opd2404_fixes.py 后首次开机约 95 秒、之后 35–60 秒；锁屏、桌面、设置（平板双栏）、触摸、联网正常；蓝牙不可用，几个 OPLUS 应用会周期性 NPE 重启 |
| ColorOS 16（Mystic GSI 移植包，Android 16） | 一加 Pad Pro（OPD2404，平板，3000x2120@420） | `--width 1500 --height 1060 --density 210 --ram 6144 --cores 6` | 跑 kernel_fixes.py、coloros_gsi_fixes.py 与 a16_coloros_opd2404_fixes.py 后约 46 秒亮屏、1 分钟进桌面；横屏平板布局、应用库/文件管理可打开、联网正常；向上跨版本（A16 system + A15 vendor），详见 §2.7 |
| ColorOS 16 PLG120（Mystic GSI 移植包，Android 17 预览底座，包在 Generic 目录、代号 CinnamonBun；显示版本 16.0.0、内部 build 17.0.0.12） | OPPO Find X9 Pro 卫星通信版（PLG120，手机，约 1272 宽@560） | `--width 636 --height 1390 --density 280 --ram 6144 --cores 6` | 跑 kernel_fixes.py（K1+K2）、coloros_gsi_fixes.py 与 a17_coloros_plg120_fixes.py 后开机到桌面；解锁、设置可打开、联网正常；默认视频壁纸黑（设静态即可，§2.8）；A17 预览的 ART 与 jar 校验有两处新坑，见 §2.8 |

复现（以 HyperOS 为例）：

```sh
py scripts/gsi_disk.py images/base/android-35/system.img <HyperOS system.img> images/gsi/a15_hyperos2.0.215.0_dada.img
py scripts/a15_hyperos_dada_fixes.py images/gsi/a15_hyperos2.0.215.0_dada.img
py tools/avdctl/avdctl.py create a15_dada --system images/gsi/a15_hyperos2.0.215.0_dada.img --ram 6144 --cores 6 --no-firstboot --width 600 --height 1335 --density 260
py tools/avdctl/avdctl.py start a15_dada --wait
```

## 4. 排查现场

- **tombstone** 在 guest `/data/tombstones/`——崩溃循环时趁 adb 在赶紧拉（wipe userdata 会清）；
- **"无限重启但 logcat 看起来正常"**：先看 `logs/console.log` 里 init 的 `shutdown_command` /
  `reboot_on_failure`（vold、netd 等都是 reboot_on_failure 服务）；**QEMU 日志里反复
  `guest boot completed` 就是"每轮都开机完成后立刻重启"的指纹**；
- system_server 周期性崩溃：`logcat` 里数 FATAL EXCEPTION 的周期与线程名，对照 `debuggerd -b <pid>`
  的原生栈；
- **user 版 GSI 上 adb 只是 shell**，读不了 /data/anr、不能 debuggerd 别的进程：`adb bugreport` 由 root 的
  dumpstate 收集，一次拿到全部 ANR trace、tombstone 和 `BINDER TRANSACTIONS`（卡死的进程在等谁的哪个调用）；
- 某个进程卡死时，在 bugreport 的 BINDER TRANSACTIONS 里找 `outgoing transaction ... from <pid>`，对端 pid
  再到进程列表里查名字；
- 更多手段（gdb 定位 init、--verbose-boot、内核符号表）见 [debugging.md](debugging.md)。
