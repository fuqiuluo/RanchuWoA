"""coloros14_fixes.py — M8 实验：给 ColorOS 14（OPLUS，Android 14/SDK 34）GSI 的实验盘打补丁。

用法: py scripts/coloros14_fixes.py <GSI 实验盘>        （scripts/gsi_disk.py 的产物，盘不能正被 VM 使用）
在 coloros_gsi_fixes.py 之后运行；可重复运行（已处理的项会跳过）。

补丁 1（system 分区，build.prop）：去掉源机型的显示/图形硬件属性，改由模拟器决定（同 hyperos_fixes 的第 3 条，
见 RESEARCH §25；ro 属性只能设一次，init 先加载 /system/build.prop、后执行 vendor 的 init 脚本）：
  - ro.sf.lcd_density=420（源机型的密度）：抢在 vendor 按 androidboot.lcd_density 设的值之前生效，
    实例分辨率（原机一半 1500x1060）配 210 密度会被它顶成 420，界面按整屏真机排版。
  - ro.opengles.version=196610（GLES 3.2，出现两行）：宿主 gfxstream / SwiftShader 只有 3.0，
    应用以为有 3.2 会走到未实现的路径。
  - ro.surface_flinger.primary_display_orientation=ORIENTATION_270：源机型是平板（一加 Pad Pro，
    OPD2404，3000x2120，ro.build.characteristics=tablet），面板物理竖装、自然方向是横屏，SF 固定转
    270°。我们的虚拟屏直接按横屏给（实例 1500x1060），再转就成了竖屏布局（M8 实测：竖屏实例上整个界面
    横躺，WMS 的逻辑屏是 1386x620）。
被去掉的行改成注释（带 RanchuWoA 标记），原值留档。
另把移植者加的 persist.sys.usb.config=adb,mtp 换成 mtp,adb（补丁 1b）：init.usb.rc/init.usb.configfs.rc
只认 adb、mtp,adb 等固定组合，adb,mtp 没有触发器，adbd 永远不启动（adb 一直 offline；ColorOS 15 GSI
写的是 mtp,adb 所以正常）。再加 ro.mediaserver.64b.enable=true：OPLUS 的 media 服务是 mediaserverwrapper，
按这个属性 exec /system/bin/mediaserver64，否则 exec 32 位的 /system/bin/mediaserver——骁龙 X Elite
（Oryon）不支持 AArch32，exec 失败 exit 255，每 5 秒重启一次（源机型的 vendor 设了该属性，我们的没有）。
没有 mediaserver 就没有 media.player / media.resource_manager：MediaProvider 每次等 5 秒，
其 ExternalStorageService 超时 ANR，外部存储（FUSE）起不来，StorageManagerService 一直报
"unmounted due to system locked"，桌面（TouchInteractionService）也跟着 ANR、一直黑屏。

补丁 2（system 分区，SELinux mapping）：生成 /system/etc/selinux/mapping/202404.cil。
  我们的 vendor 是 android-35 镜像的（plat_sepolicy_vers.txt=202404），init 无条件把
  /system/etc/selinux/mapping/202404.cil 传给 secilc（system/core/init/selinux.cpp，不像
  system_ext/product 的映射有存在性检查），Android 14 平台的 GSI 只带 29.0–34.0 的映射，缺这个文件
  secilc 直接 255 → "Unable to open SELinux policy" → InitFatalReboot 无限重启（M8 实测）。
  做法：以基础镜像（A15）自带的 202404.cil 为模板——它声明全部 X_202404 公共类型并把每个关联到
  当前平台的同名类型 X——裁掉目标类型在 A14 平台策略里不存在的条目（A14→A15 新增的 43 个类型，
  X_202404 符号本身由 vendor 的 plat_pub_versioned.cil 声明，不留悬空引用）。

补丁 3（vendor 分区，build.prop）：追加 ro.vndk.version=34。
  A14 的 linkerconfig（modules/environment.cc）：IsLegacyDevice() = 没有 ro.vndk.version 即真——
  A15 起不再需要该属性（我们的 vendor 与 GSI 都不设它），于是在 A14 上 linkerconfig 生成 legacy
  单命名空间 ld.config（dir.legacy=/vendor...）：vendor HAL 全按 default 命名空间解析依赖，摸到
  /system/system_ext/lib64 里标签为 system_file 的 Rust dylib（libbinder_rs.dylib.so 等）被 SELinux
  拒绝 → 链接失败全部 exit(1)，zygote 同理反复重启（M8 实测）。补上后 linkerconfig 恢复 Treble 配置
  （vendor 段 + sphal 命名空间，vendor/lib64 里有这些 dylib 的可读副本）。

补丁 4（vendor 分区，SELinux）：追加 logd 域的串口访问 allow（vendor_sepolicy.cil 与 _debug 两份都加）。
  goldfish-logcat（logd 域）往 /dev/hvc1 写日志：A15 的平台策略自带这些 allow，A14 的没有——
  不加的话 logcat 输出被 enforcing 拦下，logs/logcat.txt 与串口里都没有 Android 日志，排查两眼一抹黑
  （A14 还是 user 版：shell 域连 logcat 二进制都不许 exec）。另补 logcat 连 logd 的 logdr socket。
  同一块里还有：全部 hal_* 域的 binder 设备访问、mapper/woa 的 find、system_server 查可选服务的 find，
  以及 GSI 高通性能服务 vendor.perfservice 读 hwservicemanager.ready——读不到时 HIDL getService
  无限等待，它的 binder 线程全被占住，libgui 的 initAppType（OPLUS 加在 BLASTBufferQueue 构造里）
  调它就卡死：桌面主线程卡住、TouchInteractionService ANR、开机后一直黑屏（M8 实测）。

补丁 5（vendor 分区，VINTF）：etc/vintf 下所有 XML 的 manifest version="8.0" 改成 "7.0"。
  A15 vendor 的 VINTF 清单是 8.0 格式，A14 的 libvintf（@7.0）解析报
  "Unrecognized manifest.version 8.0" → servicemanager/hwservicemanager 的清单查询全失败，
  每个 HAL 注册都被 "must be in VINTF manifest" 拒绝 → keymint/gatekeeper 等直接 abort。
  另把设备兼容性矩阵的 <system-sdk> 35 改成 34（5b）：A14 框架清单只支持 29–34，VintfObject
  判不兼容 → Build.isBuildConsistent() 为假，开机后弹"您的设备内部出现了问题"。

补丁 6（vendor 分区）：A15 的系统库装进 /vendor/lib64（同名遮蔽，vendor 命名空间优先用它们）。
  A15 的 vendor 组件引用 A15 新增的 NDK 符号（libui.so 的 AServiceManager_openDeclaredPassthroughHal、
  wifi 家族的 APersistableBundle_*），A14 系统的 libbinder_ndk 没有：hwcomposer-3/camera provider 链接
  libui.so 就 CANNOT LINK 退出。装入 ColorOS 15 GSI 的 libbinder_ndk/libbinder/libvndksupport/libui
  （OPLUS 版：Google 版 libbinder_ndk 的 vendor 进程会在 /dev/vndbinder 上死等不存在的
  vndservicemanager）和基础镜像的 libapexsupport（A15 起随 system 提供）。libbinder.so 里
  android::openDeclaredPassthroughHal 改名（真实现要 VINTF isDeclared("mapper/woa")，A14 的 libvintf
  对 native 格式 HAL 不通过），libbinder_ndk.so 追加 DT_NEEDED "_ndk.so"——直通 shim，直接按 woa 后缀
  加载 mapper.woa.so（源码 gfx/vendor-overlay/gsi/coloros14/woa_passthrough_shim.c）。

补丁 7（vendor 分区）：禁用 vendor 自带的全部 A15 APEX（.apex → .apex.disabled）。
  widevine 的 VINTF 碎片是 8.0 格式，A14 的 libvintf 解析失败会让整个设备清单作废；其余 minSdk=35，
  A14 的 PackageManager scanApexPackages 直接抛异常杀 system_server。

补丁 8（system 分区）：删掉 zygote_secondary 的全部服务定义（init.zygote64_32.rc 与 system_ext 的
  tango-binfmt.rc）。纯 64 位环境没有 app_process32（宿主 CPU 也不支持 AArch32），它 exec 失败反复退出，
  onrestart 每次都把主 zygote 连同 system_server 杀一轮。

补丁 9（vendor 分区，VINTF）：禁用生物识别 HAL 的声明与 rc（fingerprint-service.ranchu、face-example）。
  这两个服务是 oneshot+disabled 的按需服务，init rc 里没有任何 start 触发器——由 servicemanager 在
  框架查询声明的接口时经 ctl.interface_start 拉起。A15 vendor 的指纹 HAL 枚举传感器后，A14 GSI 的
  BiometricServiceRegistry 会把同一 authenticator 注册两次 → IllegalStateException "Cannot register
  duplicate authenticator" 杀掉 system_server（M8 实测，崩溃前一行正是 FingerprintService: Adding
  AIDL provider: default）。模拟器本就没有指纹/人脸传感器，去掉声明即走"无传感器"路径
  （No sensors found，无害）；rc 一并删除保险，顺带消掉 hal_face_default 注册 IFace/virtual 的
  service_manager add 噪声拒绝。注意：声明文件必须彻底删除而非改名——libvintf 会读 manifest/
  目录下所有文件，无 SELinux 标签的占位文件会让整个清单作废（NULL VINTF MANIFEST!）。

补丁 10（vendor 分区，/etc/group）：定义 OPLUS 的 vendor 组 vendor_launcher / vendor_saures。
  platform.xml 的 oplus.permission.OPLUS_COMPONENT_SAFE（几乎所有特权应用都有）带这两个组，
  真机由 OPLUS vendor 的 config.fs 生成；我们的 vendor 没有，getgrnam 失败 → SystemConfig 把 -1
  记进权限 gid（A14 不过滤，A15 跳过）→ zygote setgroups 撞上内核 make_kgid 的非法值
  （0xFFFFFFFF）→ EINVAL → JNI FatalError，所有应用进程起不来（permissioncontroller 崩溃又让
  PermissionPolicyService 拖死 system_server）。bionic 只认带分区前缀的行（/system/etc/group 要
  system_、/vendor/etc/group 要 vendor_，否则打 "without required prefix" 并忽略），所以必须写在
  vendor 的组表里；标准组名（inet/log/audio…）bionic 内置，不需要组表。早版本往 system 组表塞
  标准组表的做法无效（全被忽略、每次查找刷 55 行错误日志），这里顺带恢复成 GSI 原样。

补丁 11（system 分区，lib64/libvintf.so）：内核版本串里的 android15 不再让 system_server abort。
  AMS.systemReady → Build.isBuildConsistent → VintfObject.verifyBuildAtBoot 取 RuntimeInfo，A14 的
  libvintf 把 GKI 版本串 "6.6.30-android15-8-..." 的 android15 换算成 Level 9，超出它的枚举
  （LAST_PLUS_ONE=9，A15 才有 V）→ CHECK 失败 SIGABRT，system_server 每轮都死在这里（M8 实测）。
  改 gkiAndroidReleaseToLevel 一条指令：越界时返回 UNSPECIFIED（与 Android 12 之前的内核同路），
  parseGkiKernelRelease 随之失败，fetchVersion 退回只解析 6.6.30 的 parseKernelVersion，内核按非 GKI
  处理。verifyBuildAtBoot 的兼容性结论只决定要不要弹“设备内部问题”对话框，不影响开机。
  补丁按 sub/cmp/b.hs 三条指令的字节校验，GSI 版本不同找不到就拒绝打。

补丁 12（system 分区）：OPLUS 框架 dex 的指令级补丁（DEX_PATCHES 表，四处）。
  OPLUS 代码普遍只认高通/联发科两种平台，各处判断写法不一（ro.boot.hardware 以 qcom 开头、ro.hardware
  等于 qcom、ro.board.platform 以 mt 开头……），我们的 ranchu 两边都不中；ro.boot.hardware 又不能改
  （vendor 的 init.ranchu.rc、fstab 都靠它）。只修会崩的地方，取向一律按高通（源机型是骁龙 8 Gen 3）。
  按 (类, 方法, 原型) 找到代码，核对原指令（含 string/method 索引，GSI 版本不同就拒绝打）再替换，当前
  已是新指令则跳过；dex 在 APK/jar 里都是 STORED，原地改字节并重算 dex 头与 zip 的 CRC，文件大小与偏移
  不变（APK 签名块不动，系统分区 APK 开机扫描只取证书不校验摘要），再删掉对应的预编译产物。

  a) system_ext/priv-app/Telecom/Telecom.apk：Telecom 的 SoC 解耦层固定用高通实现。
  OPLUS Telecom（跑在 system_server 进程里）到处调 SocDecouplingCenter.requireSocProduce() 取平台
  实现（QcomSocImpl / MtkSocImpl，两个都编进了 APK、都无条件登记），选哪个看
  OplusTelephonyManager.isQcomPlatform/isMTKPlatform——即 ro.boot.hardware 是否以 qcom/mt 开头。
  我们的是 ranchu（vendor 的 init.ranchu.rc 靠它加载，不能改），两边都不中 → 返回 null →
  checkNotNull 抛 NPE，CallAudioManager 构造时杀掉 system_server（M8 实测，systemReady 之后）。
  只改 Telecom 自己的 SocDecouplingCenter.<clinit>：sIsQcomSoc 恒为真，框架其余地方的平台判断不动。

  b) system_ext/framework/oplus-wifi-service.jar：OplusGtxController.validateExecutor 判空。
  Wi-Fi 省电（GTX）命令执行器同样按 ro.hardware == "qcom" / MTK 二选一创建，我们这里是 null；
  validateExecutor() 打完 "cmd executor is null" 却照样返回 true（C14 的 bug，C15 已改成返回 false），
  开机完成阶段（phase 1000）OplusWifiService → enableVerboseLogging 调到 null 上 → NPE 杀 system_server。
  按 C15 的修法改：判空分支直接 return false（GTX 只是省电优化，没有执行器就不做）。

  c) system_ext/app/Olc/Olc.apk（OPLUS 日志服务）：ro.boot.hardware 不是 qcom、ro.board.platform 也不是
  mt* 时 AlwaysOnConfigHelper 抛 "Unknown platform."，进程约每秒崩一次（2 分钟 226 次）；把它的高通
  判断（混淆后的 c1.b.c）改成恒真。

  d) framework/oplus-services.jar：OplusShellPermissionUtils.revokeShellPermission 恒返回 false。OPLUS 的
  “权限监控”（persist.sys.permission.enable 为真，即开发者选项“禁止权限监控”关着）对 adb shell 收回
  WRITE_SECURE_SETTINGS 等权限，QEMU 开机命令写 device_provisioned/user_setup_complete 就静默失败
  （没有开机向导的 GSI 靠它，§21.5；这个移植包的 adbd 想以 root 运行，user 版 SELinux 策略不给，
  setcon 失败 abort 一次后退回 shell）。该属性是 OPLUS 全局拦截总开关、会被 OPLUS 应用写回持久化，
  改 build.prop 默认值压不住，也不该整体关掉，所以只放开 shell 这一处。

  e) framework/services.jar：ProcessListSocExtImpl.startProcess 直接返回。高通 SoC 扩展在应用进程启动时
  发 perfHint(4225, …, 101)，QPerformance 的 Performance 对 101 另起线程直接调 JNI；zygote 只在
  ro.boot.hardware 以 qcom 开头时预加载 libqti_performance.so，于是 UnsatisfiedLinkError 无人捕获，
  点开设置等应用就杀死 system_server（其余 perf 调用经 BoostFramework 反射，异常被吞掉）。Performance、
  BoostFramework、ZygoteInit 都在启动类路径上，改它们启动镜像就作废，所以截断在 services.jar 的调用方
  （该方法只发这一个提示）。也没让 zygote 加载那个库：它会在每个进程里直连高通 perf HAL，读不到
  hwservicemanager.ready 的域会像 perfservice 一样无限等待。services 的 odex/vdex/art 删掉后 odrefresh
  开机时在 /data 上重新编译。

补丁 14（vendor 分区，etc/permissions/ranchuwoa-features.xml）：保持通用版，蓝牙不可用（不学 HyperOS）。
  com.oplus.exsystemservice、com.oplus.battery、com.heytap.accessory 等假定 BluetoothAdapter 非空，
  蓝牙不可用时它们 NPE 后被 AMS 带退避地重启（ColorOS 15 同样如此，整机仍空闲）。试过 HyperOS 的
  做法（gsi/hyperos-features.xml 保留蓝牙特性）：C14 的 OplusBluetoothManagerServiceExt 会主动开
  BLE，A14 蓝牙栈打不开 /sys/power/wake_lock（wakelock.cc CHECK 失败）每 0.6 秒 abort 一次，更糟。
  这一步只在实验盘上留有那次试验的特性表时把它换回通用版，正常情况下跳过。

补丁 15（system 分区，etc/vintf/compatibility_matrix.{7,8}.xml）：补一条 6.6 内核条目。
  补丁 11 之后内核按非 GKI 处理、内核 FCM 等级取 7，A14 框架矩阵里 7/8 级只有 5.10/5.15/6.1 的条目，
  VintfObject 报 "No kernel entry found for kernel version 6.6" → isBuildConsistent() 为假 → 开机弹
  "您的设备内部出现了问题"（与补丁 5b 同一个对话框，两处都不兼容）。插入不带配置要求的
  <kernel version="6.6.0" level="N"/>（我们的 android15-6.6 GKI 本就满足 A15 更严的要求）。

背景（M8）：ColorOS 15 的同名属性在 /my_product（GSI 不含该分区，不影响）；HyperOS 的在
/product/etc/build.prop 与 /system/build.prop；ColorOS 14 底包只写 /system/build.prop。
"""
import io
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alpine_job import ROOT, run_job                      # noqa: E402
from coloros_gsi_fixes import Dex                         # noqa: E402
from hyperos_fixes import patch_props, replace_cmds       # noqa: E402
from vendor_install import _write_file_cmds               # noqa: E402

# 补丁 1：system 分区里的 build.prop（ext4 内路径）→ 要注释掉的属性
PROP_EDITS = [
    ("system/build.prop", ("ro.sf.lcd_density", "ro.opengles.version",
                           "ro.surface_flinger.primary_display_orientation")),
]
# 补丁 15：要补 6.6 内核条目的框架兼容性矩阵 (路径, 内核 FCM 等级)
FCM_KERNEL = [("system/etc/vintf/compatibility_matrix.7.xml", 7),
              ("system/etc/vintf/compatibility_matrix.8.xml", 8)]
# 补丁 14：vendor 特性表保持通用版（蓝牙不可用，不学 HyperOS 恢复蓝牙）
FEATURES = ROOT / "gfx" / "vendor-overlay" / "etc" / "permissions" / "ranchuwoa-features.xml"
FEATURES_REL = "etc/permissions/ranchuwoa-features.xml"
# 补丁 1b：设属性 (键, 值, 原因)：已有该键（未注释）就改值并把原行留成注释，没有就追加
PROP_SET = {
    "system/build.prop": [
        ("persist.sys.usb.config", "mtp,adb",
         "init.usb*.rc 只有 mtp,adb 的触发器，adb,mtp 不会启动 adbd"),
        ("ro.mediaserver.64b.enable", "true",
         "mediaserverwrapper 据此 exec 64 位的 mediaserver64；/system/bin/mediaserver 是 32 位，宿主 CPU 不支持 AArch32"),
    ],
}


def set_props(data: bytes, items) -> bytes:
    """按 PROP_SET 的条目改 build.prop 内容（幂等：值已对就不动）。"""
    lines = data.decode("utf-8").split("\n")
    for key, value, why in items:
        idx = [i for i, ln in enumerate(lines) if ln.split("=", 1)[0].strip() == key
               and "=" in ln and not ln.lstrip().startswith("#")]
        if any(lines[i].split("=", 1)[1].strip() == value for i in idx):
            continue
        for i in idx:
            lines[i] = f"# RanchuWoA: {why}，原值: {lines[i]}"
        at = idx[-1] + 1 if idx else len(lines) - (1 if lines and lines[-1] == "" else 0)
        if not idx:
            lines.insert(at, f"# RanchuWoA: {why}")
            at += 1
        lines.insert(at, f"{key}={value}")
    return "\n".join(lines).encode("utf-8")

BASE_IMG = ROOT / "images" / "base" / "android-35" / "system.img"   # A15 基础镜像（202404 映射的模板来源）
MAPPING_REL = "system/etc/selinux/mapping/202404.cil"
MAPPING_LABEL = "sepolicy_file"
SHIM = ROOT / "gfx" / "vendor-overlay" / "gsi" / "coloros14" / "_ndk.so"

# 补丁 11：libvintf.so 的 RuntimeInfo::gkiAndroidReleaseToLevel 里 "x19 = release - 6; cmp x19, #9;
# b.hs <CHECK 失败>" 的 b.hs 换成 "csinv x19, x19, xzr, lo"（越界时返回 Level::UNSPECIFIED = -1）
VINTF_REL = "system/lib64/libvintf.so"
VINTF_SIG_OLD = struct.pack("<III", 0xD1001813, 0xF100267F, 0x54000182)
VINTF_SIG_NEW = struct.pack("<III", 0xD1001813, 0xF100267F, 0xDA9F3273)

# 补丁 12/13：OPLUS 框架 dex 的指令级补丁（详见文件头说明）。每项：
#   zip 包在 system 分区里的路径 → [(类, 方法, 参数, 返回, 指令偏移, 原指令, 新指令), ...]，
#   以及包对应的预编译产物（dex 校验和变了就作废，删掉让 ART 直接跑新 dex）。
# 原指令里含 string/method 索引，GSI 版本不同就对不上、拒绝打；当前就是新指令则视为已打过。
DEX_PATCHES = {
    "system/system_ext/priv-app/Telecom/Telecom.apk": (
        [("Lcom/oplus/platform/SocDecouplingCenter;", "<clinit>", "", "V", 7,
          # invoke-static {} FeatureUtilsKt.isQcomPlatform()Z; move-result v0
          (0x0071, 0x0963, 0x0000, 0x000A),
          # const/4 v0, #1; nop ×3
          (0x1012, 0x0000, 0x0000, 0x0000))],
        ["system/system_ext/priv-app/Telecom/oat/arm64/Telecom.odex",
         "system/system_ext/priv-app/Telecom/oat/arm64/Telecom.vdex"]),
    "system/system_ext/framework/oplus-wifi-service.jar": (
        [("Lcom/oplus/server/wifi/power/OplusGtxController;", "validateExecutor", "", "Z", 4,
          # const-string v1, "OplusGtxController"; const-string v0, "cmd executor is null";
          # invoke-static {v1, v0} Log.d —— 之后落到 000b 的 const/4 v1, #1; return v1
          (0x011A, 0x3418, 0x001A, 0x4A3D, 0x2071, 0x0577, 0x0001),
          # const/4 v1, #0; return v1; nop ×5
          (0x0112, 0x010F, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000))],
        ["system/system_ext/framework/oat/arm64/oplus-wifi-service.odex",
         "system/system_ext/framework/oat/arm64/oplus-wifi-service.vdex"]),
    "system/system_ext/app/Olc/Olc.apk": (
        # OPLUS 日志服务的平台判断（混淆后的 c1.b.c = ro.boot.hardware 是否等于 "qcom"）：
        # 两边都不中时 AlwaysOnConfigHelper 抛 "Unknown platform."，进程每秒崩一次
        [("Lc1/b;", "c", "", "Z", 0,
          (0x001A, 0x4C60),                      # const-string v0, "ro.boot.hardware"
          (0x1012, 0x000F))],                    # const/4 v0, #1; return v0
        ["system/system_ext/app/Olc/oat/arm64/Olc.odex",
         "system/system_ext/app/Olc/oat/arm64/Olc.vdex"]),
    "system/framework/oplus-services.jar": (
        # OPLUS 的 adb 权限监控：persist.sys.permission.enable 为真（默认，开发者选项“禁止权限监控”
        # 关着）时对 uid 2000 收回 WRITE_SECURE_SETTINGS 等权限；该属性是全局拦截总开关、会被
        # OPLUS 应用写回持久化，不能靠改默认值，只把 shell 这一处放开
        [("Lcom/android/server/am/OplusShellPermissionUtils;", "revokeShellPermission",
          "Landroid/content/Context;Ljava/lang/String;I", "Z", 1,
          (0x0163, 0x2558),                      # sget-boolean v1, IS_FORUM_VERSION
          (0x000F, 0x0000))],                    # return v0（v0 已由第 0 条置 0）; nop
        []),                                     # 该 jar 没有预编译产物（coloros_gsi_fixes 已删）
    "system/framework/services.jar": (
        # 高通 SoC 扩展在应用进程启动时发 perfHint(4225, 进程名, pid, 101)，QPerformance 的
        # Performance.perfHint 对 101 另起 SendGameLaunchBroadcast 线程直接调 JNI，而 zygote 只在
        # Build.isQcomPlatform()（ro.boot.hardware 以 qcom 开头）时预加载 libqti_performance.so——
        # UnsatisfiedLinkError 无人捕获，点开应用就杀死 system_server。Performance/BoostFramework/
        # ZygoteInit 都在启动类路径上（改了启动镜像作废），所以在 services.jar 的调用方截断：整个方法只是
        # 这一个性能提示
        [("Lcom/android/server/am/ProcessListSocExtImpl;", "startProcess",
          "Lcom/android/server/am/HostingRecord;Landroid/os/Process$ProcessStartResult;"
          "Lcom/android/server/am/ProcessRecord;", "V", 0,
          (0x0162, 0x523D),                      # sget-object v1, mPerfServiceStartHint
          (0x000E, 0x0000))],                    # return-void; nop
        # odrefresh 发现 /system 上的 system_server 产物不全，会在 /data 上重新编译
        ["system/framework/oat/arm64/services.odex",
         "system/framework/oat/arm64/services.vdex",
         "system/framework/oat/arm64/services.art"]),
}


def patch_zip_dex(blob: bytes, patches) -> tuple[bytes | None, list[str]]:
    """在 zip（APK/jar）里原地改 dex 指令：dex 必须是 STORED（不压缩）条目，改完重算 dex 头的
    SHA-1/adler32 与 zip 本地头、中央目录的 CRC32，文件大小与各项偏移不变（APK 签名块原样保留，
    系统分区的 APK 开机扫描只取证书不校验摘要）。返回 (新内容或 None=全部已打过, 说明列表)。"""
    d = bytearray(blob)
    todo = list(patches)
    notes, changed = [], False
    for info in zipfile.ZipFile(io.BytesIO(blob)).infolist():
        if not (info.filename.startswith("classes") and info.filename.endswith(".dex")) or not todo:
            continue
        nlen, xlen = struct.unpack_from("<HH", d, info.header_offset + 26)
        start = info.header_offset + 30 + nlen + xlen
        dex = Dex(bytearray(d[start:start + info.file_size]))
        dex_changed = False
        for p in list(todo):
            cls, name, args, ret, off, old, new = p
            at = dex.find_code(cls, name, args, ret)
            if at is None:
                continue
            todo.remove(p)
            where = f"{info.filename}：{cls[1:-1].rsplit('/', 1)[-1]}.{name}"
            cur = struct.unpack_from(f"<{len(old)}H", dex.d, at + 2 * off)
            if cur == tuple(new):
                notes.append(f"{where} 已打过")
                continue
            if cur != tuple(old):
                sys.exit(f"{where} 第 {off} 个代码单元起的指令与预期不符（GSI 版本不同？）：{cur}")
            if info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 0x8:
                sys.exit(f"{info.filename} 不是无数据描述符的 STORED 条目，不能原地改")
            struct.pack_into(f"<{len(new)}H", dex.d, at + 2 * off, *new)
            notes.append(f"{where} 已改")
            dex_changed = True
        if not dex_changed:
            continue
        dex.fix_header()
        d[start:start + info.file_size] = dex.d
        crc = zlib.crc32(dex.d) & 0xFFFFFFFF
        struct.pack_into("<I", d, info.header_offset + 14, crc)
        pos = struct.unpack_from("<I", d, d.rindex(b"PK\x05\x06") + 16)[0]
        while d[pos:pos + 4] == b"PK\x01\x02":
            n1, n2, n3 = struct.unpack_from("<HHH", d, pos + 28)
            if struct.unpack_from("<I", d, pos + 42)[0] == info.header_offset:
                struct.pack_into("<I", d, pos + 16, crc)
                break
            pos += 46 + n1 + n2 + n3
        else:
            sys.exit(f"中央目录里找不到 {info.filename}")
        changed = True
    if todo:
        sys.exit(f"找不到方法：{[(p[0], p[1]) for p in todo]}")
    return (bytes(d) if changed else None), notes


def patch_ndk_dtneeded(data: bytes) -> bytes:
    """给 libbinder_ndk.so 追加一条 DT_NEEDED，把 shim（文件名 _ndk.so）挂进依赖闭包，补出 A14
    缺的 android::openDeclaredPassthroughHal。bionic 校验 DT_NEEDED 的偏移必须落在 DT_STRSZ
    范围内，字符串没法放别处；.dynstr 里 SONAME（"libbinder_ndk.so"）又被 DT_VERDEF 按名引用，
    不能改写——所以用子串：DT_NEEDED 指向 SONAME 第 9 字节起的 "_ndk.so"（shim 就叫这个名），
    原字符串一个字节都不动。新条目写进原 DT_NULL 槽位，紧随其后的零条目成为新的终止符。"""
    d = bytearray(data)
    e_phoff, e_phentsize, e_phnum = (struct.unpack_from("<Q", d, 0x20)[0],
                                     struct.unpack_from("<H", d, 0x36)[0],
                                     struct.unpack_from("<H", d, 0x38)[0])
    dyn = None
    loads = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        t = struct.unpack_from("<I", d, off)[0]
        v, o = struct.unpack_from("<Q", d, off + 16)[0], struct.unpack_from("<Q", d, off + 8)[0]
        fs = struct.unpack_from("<Q", d, off + 32)[0]
        if t == 1:
            loads.append((v, o, fs))
        elif t == 2:
            dyn = (o, fs)
    strtab = soname = null_off = None
    for i in range(0, dyn[1], 16):
        tag, val = struct.unpack_from("<QQ", d, dyn[0] + i)
        if tag == 0:
            null_off = dyn[0] + i
            break
        if tag == 5:
            strtab = val
        if tag == 14:
            soname = val
    if null_off is None or strtab is None or soname is None:
        sys.exit("libbinder_ndk.so 里找不到 DT_NULL/DT_STRTAB/DT_SONAME")
    so = next(o + (strtab - va) for va, o, s in loads if va <= strtab < va + s) + soname
    if d[so:so + 16] != b"libbinder_ndk.so":
        sys.exit(f"SONAME 字符串异常: {bytes(d[so:so + 16])!r}")
    struct.pack_into("<QQ", d, null_off, 1, soname + 9)   # DT_NEEDED "_ndk.so"（SONAME 子串）
    return bytes(d)

# 只动 system/vendor 分区的作业（结构同 hyperos_fixes.JOB，vendor 段仅写 build.prop）
JOB = r"""
D="$DISK0"
[ -n "$D" ] || { echo "找不到实验盘"; exit 1; }
set -e
# ---- vendor 分区：build.prop ----
if [ -s /payload/vendor.cmds ]; then
losetup -o {voff} /dev/loop0 "$D"
rc=0; e2fsck -fy /dev/loop0 >/tmp/fsck0.log 2>&1 || rc=$?; echo "pre_fsck_rc=$rc"; [ $rc -le 1 ]
debugfs -w -f /payload/vendor.cmds /dev/loop0 2>&1 | grep -v -E '^debugfs [0-9.]+ ' || true
rc=0; e2fsck -fn /dev/loop0 >/tmp/fsck.log 2>&1 || rc=$?; echo "verify_fsck_rc=$rc"; [ $rc -eq 0 ] || { tail -20 /tmp/fsck.log; exit 3; }
debugfs -R 'cat build.prop' /dev/loop0 2>/dev/null | grep 'RanchuWoA' || true
losetup -d /dev/loop0
fi
# ---- system 分区 ----
if [ -s /payload/system.cmds ]; then
losetup -o {soff} /dev/loop0 "$D"
rc=0; e2fsck -fy /dev/loop0 >/tmp/fsck1.log 2>&1 || rc=$?; echo "pre_fsck_rc=$rc"; [ $rc -le 1 ]
debugfs -w -f /payload/system.cmds /dev/loop0 2>&1 | grep -v -E '^debugfs [0-9.]+ ' || true
rc=0; e2fsck -fn /dev/loop0 >/tmp/fsck.log 2>&1 || rc=$?; echo "verify_fsck_rc=$rc"; [ $rc -eq 0 ] || { tail -20 /tmp/fsck.log; exit 3; }
echo "--- build.prop 里被注释掉的属性："
for p in {props}; do debugfs -R "cat $p" /dev/loop0 2>/dev/null | grep 'RanchuWoA' || true; done
for p in {props}; do debugfs -R "ea_get $p security.selinux" /dev/loop0 2>/dev/null; done
losetup -d /dev/loop0
fi
echo COLOROS14_FIXES_OK
"""


def patch_libbinder_symbol(data: bytes) -> bytes:
    """把 C15 系统 libbinder.so 导出的 android::openDeclaredPassthroughHal 符号改名
    （mangled 名末尾 i→j，同长度）：真实现要 VINTF isDeclared("mapper/woa")，A14 的 libvintf
    对 native 格式 HAL 的这个查询不通过；改掉后 libbinder_ndk 的未定义引用落到我们的 _ndk.so
    shim 上（直接按 woa 后缀加载 mapper.woa.so）。"""
    old = b"_ZN7android26openDeclaredPassthroughHalERKNS_8String16ES2_i"
    n = data.count(old)
    if n < 1:
        sys.exit("libbinder.so 里找不到目标符号")
    return data.replace(old, old[:-1] + b"j")


def gen_mapping() -> str | None:
    """按最终策略（平台 + vendor）裁剪 A15 基础镜像的 mapping/202404.cil；盘里已有的内容一致
    则返回 None（跳过），否则重写。"""
    voff_m = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(disk_arg), "vendor"],
                            capture_output=True, text=True, check=True).stdout.split()[0]
    boff = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(BASE_IMG), "system"],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    stage_map = stage / "a15"
    (stage_map).mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(BASE_IMG), boff,
                    "system/etc/selinux/mapping/202404.cil", "-o", str(stage_map)],
                   check=True, capture_output=True)
    tpl = (stage_map / "202404.cil").read_text(encoding="utf-8", errors="replace")
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                    "system/etc/selinux/plat_sepolicy.cil", "-o", str(stage_map)],
                   check=True, capture_output=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff_m,
                    "/etc/selinux/vendor_sepolicy.cil", "-o", str(stage_map)],
                   check=True, capture_output=True)
    # 目标类型的过滤集合要含 vendor 声明的公共类型（hal_* 域都在 vendor 策略里），
    # 只对平台过滤会把它们全裁掉——映射变空属性，vendor 的 allow 就都不生效了
    syms = set()
    for cil in ("plat_sepolicy.cil", "vendor_sepolicy.cil"):
        syms |= set(re.findall(r"\((?:type|typeattribute)\s+([A-Za-z0-9_]+)\)",
                               (stage_map / cil).read_text(encoding="utf-8", errors="replace")))
    # 模板按 3 行一组（typeattributeset X_202404 (X) / expandtypeattribute / typeattribute），
    # 目标 X 不在 A14 平台策略里的整组去掉；X_202404 符号由 vendor 的 ppv 声明，不会悬空。
    lines = tpl.split("\n")
    kept, dropped = [], 0
    i = 0
    while i < len(lines):
        m = re.match(r"\(typeattributeset\s+([\w]+?)_202404\s+\(([\w]+)\)\)", lines[i])
        if m and i + 2 < len(lines) and lines[i + 1].startswith("(expandtypeattribute") \
                and lines[i + 2].startswith("(typeattribute "):
            if m.group(2) in syms:
                kept += lines[i:i + 3]
            else:
                dropped += 1
            i += 3
        else:
            kept.append(lines[i])
            i += 1
    print(f"mapping/202404.cil：{len(kept) // 3} 条映射，裁掉 A15 新增类型 {dropped} 条")
    text = "\n".join(kept)
    cur = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
         "/" + MAPPING_REL, "-o", str(stage / "cur-map")], capture_output=True, text=True)
    cur_map = stage / "cur-map" / "202404.cil"    # ext4tool 对不存在的文件也返回 0（只打 skip）
    if cur.returncode == 0 and cur_map.is_file() and cur_map.read_text(
            encoding="utf-8", errors="replace") == text:
        return None                      # 盘里的已是最新
    return text


def main():
    global disk_arg, soff, stage
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    disk_arg = Path(sys.argv[1])
    if not BASE_IMG.is_file():
        sys.exit(f"缺少基础镜像 {BASE_IMG}（mapping 模板来源）")
    soff = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(disk_arg), "system"],
                          capture_output=True, text=True, check=True).stdout.split()[0]

    stage = ROOT / "images" / ".alpine-job" / "coloros14-payload"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "files").mkdir(parents=True)
    (stage / "labels").mkdir()

    # system：build.prop（ext4tool 按文件名平铺输出，各取到单独目录）
    print("取出 build.prop ...")
    for i, (rel, _) in enumerate(PROP_EDITS):
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                        "/" + rel, "-o", str(stage / f"orig/prop{i}")], check=True, capture_output=True)
    scmds = []
    for i, (rel, keys) in enumerate(PROP_EDITS):
        name = f"prop{i}"
        changed = patch_props(stage / "orig" / name / Path(rel).name, stage / "files" / name, keys)
        if changed:
            print(f"{rel}：注释掉 {len(changed)} 行（{', '.join(changed[:3])}）")
        # 补丁 1b：设属性（在注释之后的结果上做）
        cur = (stage / "files" / name if changed else stage / "orig" / name / Path(rel).name).read_bytes()
        new = set_props(cur, PROP_SET.get(rel, ()))
        if new != cur:
            (stage / "files" / name).write_bytes(new)
            print(f"{rel}：设 {', '.join(f'{k}={v}' for k, v, _ in PROP_SET[rel])}")
        if changed or new != cur:
            scmds += replace_cmds(rel, f"/payload/files/{name}", 0o644, f"/tmp/label_{name}")
        else:
            print(f"{rel}：无需改动")
    shutil.rmtree(stage / "orig")

    # 补丁 2：生成并写入 A14 平台的 mapping/202404.cil（已存在但内容旧也重写：早版本只按平台
    # 符号过滤，把 vendor 声明的 hal_* 公共类型裁掉了，vendor 的 allow 全落空）
    mapping = gen_mapping()
    if mapping is not None:
        (stage / "files" / "202404.cil").write_text(mapping, encoding="utf-8", newline="\n")
        scmds += _write_file_cmds(stage, MAPPING_REL, "/payload/files/202404.cil",
                                  0o644, 0, 0, MAPPING_LABEL)
    else:
        print(f"{MAPPING_REL}：已存在，跳过")

    # 补丁 10 的清理（system 分区）：早版本往 /system/etc/group 写过整张标准组表，bionic 只认
    # system_ 前缀的行，其余全被忽略且每次查找都刷错误日志——只保留 system_ 开头的行（即 GSI 原样）。
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg),
                    soff, "/system/etc/group", "-o", str(stage / "orig-group")],
                   capture_output=True, text=True)
    cur = b"".join(p.read_bytes() for p in (stage / "orig-group").rglob("group"))
    kept = b"".join(ln + b"\n" for ln in cur.splitlines() if ln.startswith(b"system_"))
    if not cur or kept == cur:
        print("system/etc/group：无需改动")
    else:
        (stage / "files" / "sgroup").write_bytes(kept)
        # debugfs 不能走分区根的 etc→/system/etc 符号链接（报 Ext2 inode is not a directory
        # 还会留下指向已删 inode 的半截目录项），必须用真实路径 system/etc/...
        scmds += replace_cmds("system/etc/group", "/payload/files/sgroup", 0o644, "/tmp/label_sgroup")
        print(f"system/etc/group：去掉无 system_ 前缀的行，留 {kept.count(b'\n')} 行")

    # 补丁 3：vendor 的 build.prop 补 ro.vndk.version（A14 的 linkerconfig 没有它就生成 legacy 单命名空间
    # 配置，vendor HAL 与 zygote 的依赖解析全乱，M8 实测全部 exit(1)）
    voff = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(disk_arg), "vendor"],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
                    "/build.prop", "-o", str(stage / "orig-vbp")], check=True, capture_output=True)
    vbp = (stage / "orig-vbp" / "build.prop").read_text(encoding="utf-8", errors="replace")
    vcmds = []
    if "ro.vndk.version" in vbp:
        print("vendor/build.prop：ro.vndk.version 已存在，跳过")
    else:
        new = vbp.rstrip("\n") + "\n# RanchuWoA: A14 的 linkerconfig 需要，否则按 legacy 生成 ld.config\nro.vndk.version=34\n"
        (stage / "files" / "vbp").write_text(new, encoding="utf-8", newline="\n")
        vcmds += replace_cmds("build.prop", "/payload/files/vbp", 0o644, "/tmp/label_vbp")
        print("vendor/build.prop：追加 ro.vndk.version=34")
    # 补丁 10（vendor 分区）：/vendor/etc/group 定义 OPLUS 的 vendor 组（详见文件头说明）。
    # 真机的值来自 OPLUS vendor 的 config.fs，这里取 OEM 区间（2900–2999）里的空闲值——只决定
    # 该权限附带的组身份，模拟器上没有属于这些组的文件，无副作用。
    VENDOR_GROUPS = [b"vendor_launcher::2950:", b"vendor_saures::2951:"]
    r = subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
                        "/etc/group", "-o", str(stage / "orig-vgroup")], capture_output=True, text=True)
    vg = stage / "orig-vgroup" / "group"
    cur = vg.read_bytes() if r.returncode == 0 and vg.is_file() else None
    if cur is not None and all(ln in cur.splitlines() for ln in VENDOR_GROUPS):
        print("vendor/etc/group：OPLUS 组已定义，跳过")
    else:
        names = {ln.split(b":")[0] for ln in VENDOR_GROUPS}
        keep = [ln for ln in (cur or b"").splitlines() if ln.strip() and ln.split(b":")[0] not in names]
        (stage / "files" / "vgroup").write_bytes(b"".join(ln + b"\n" for ln in keep + VENDOR_GROUPS))
        if cur is not None:      # 原文件存在（基础 vendor 里是空文件）：保留它的 SELinux 标签
            vcmds += replace_cmds("etc/group", "/payload/files/vgroup", 0o644, "/tmp/label_vgroup")
        else:
            vcmds += _write_file_cmds(stage, "etc/group", "/payload/files/vgroup",
                                      0o644, 0, 0, "vendor_configs_file")
        print("vendor/etc/group：定义 vendor_launcher/vendor_saures")
    # 补丁 4：logd 域写串口的 allow（goldfish-logcat 的输出，A15 平台策略自带、A14 缺）
    # + 全部 hal_* 域的 vndbinder/hwbinder 访问（AIDL HAL 经 /dev/vndbinder 注册；A15 平台给了，
    #   A14 没有，vendor 策略又只给 cas/camera/drm 等几个域写了显式 allow → keymint/health 等
    #   注册失败直接 abort。haldomain 只是 .te 宏概念、策略里没这个属性，按 vendor 域逐个生成；
    #   注意 allow 的源必须是未版本化的域类型（hal_*_202404 是空属性，规则不生效））
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
                    "/etc/selinux/vendor_sepolicy.cil", "-o", str(stage / "orig-vsp")],
                   check=True, capture_output=True)
    vsp_text = (stage / "orig-vsp" / "vendor_sepolicy.cil").read_text(encoding="utf-8", errors="replace")
    hal_domains = sorted(set(re.findall(r"^\(type (hal_\w+)\)$", vsp_text, re.M))
                         - {d for d in re.findall(r"^\(type (hal_\w+)\)$", vsp_text, re.M)
                            if d.endswith(("_exec", "_socket", "_data_file", "_file", "_prop",
                                           "_service", "_hwservice", "_client"))})
    hal_rules = "".join(
        f"(allow {d} {dev} (chr_file (ioctl read write getattr lock append map open watch watch_reads)))\n"
        for d in hal_domains for dev in ("vndbinder_device_202404", "hwbinder_device_202404"))
    print(f"hal 域 binder 访问规则：{len(hal_domains)} 个域")
    sep_addon = ("; @ranchuwoa-addon: color14-diag\n"
                 "; goldfish-logcat（logd 域）把 Android 日志写到串口 /dev/hvc1：A14 平台策略缺这些\n"
                 ";（曾试过 (permissive domain) 一次收集全部缺口，permissive 只能作用于类型、不能是属性，编译失败）\n"
                 "(allow logd_202404 serial_device_202404 (chr_file (append getattr ioctl open read write)))\n"
                 "(allow logd_202404 device_202404 (dir (open read search)))\n"
                 "(allow logd_202404 logdr_socket_202404 (sock_file (write getattr)))\n"
                 "(allow logd_202404 logd_202404 (unix_stream_socket (connectto)))\n"
                 "; A15 的平台策略给全部 HAL 域 binder 设备访问，A14 没有，按 vendor 域逐个补\n"
                 "; isDeclared(\"mapper/woa\") 的 find 权限（A15 平台策略自带，A14 缺，mapper 直通加载需要）\n"
                 "(allow hal_graphics_composer_default hal_graphics_mapper_service (service_manager (find)))\n"
                 "(allow surfaceflinger hal_graphics_mapper_service (service_manager (find)))\n"
                 "; system_server 探测可选 OPLUS 服务（biometric 注册的 getDeclaredInstances 等）的 find：\n"
                 "; 这些名字没有专属 contexts 条目、落到 default_android_service/hwservice，A15 平台策略\n"
                 "; 给了 find，A14 没有 → BiometricServiceRegistry 里 SecurityException 杀 system_server\n"
                 "(allow system_server_202404 default_android_service (service_manager (find)))\n"
                 "(allow system_server_202404 default_android_hwservice (hwservice_manager (find)))\n"
                 "; GSI system_ext 的高通性能服务 vendor.perfservice：读 hwservicemanager.ready 与查 AIDL perf2\n"
                 "; 是否声明（高通 vendor 策略经 hal_client_domain 给，我们的 vendor 没有）。读不到属性时 HIDL\n"
                 "; getService 无限“再等一秒”，binder 线程全被占住，libgui initAppType（BLASTBufferQueue 构造）\n"
                 "; 调它的进程全卡死——桌面 ANR、一直黑屏（M8 实测）\n"
                 "(allow vendor_perfservice hwservicemanager_prop (file (read open getattr map)))\n"
                 "(allow vendor_perfservice default_android_service (service_manager (find)))\n"
                 + hal_rules)
    scmds_sep = []
    for rel in ("etc/selinux/vendor_sepolicy.cil", "etc/selinux/vendor_sepolicy_debug.cil"):
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
                        "/" + rel, "-o", str(stage / "orig-sep")], check=True, capture_output=True)
        cur = (stage / "orig-sep" / Path(rel).name).read_text(encoding="utf-8", errors="replace")
        if sep_addon in cur:
            print(f"vendor/{rel}：补丁已是最新，跳过")
            continue
        # 标记之后是我们自己的追加块（总在文件末尾），有旧版本就整块替换
        base = cur.split("; @ranchuwoa-addon: color14-diag")[0].split(
            "; @ranchuwoa-addon: coloros14")[0].rstrip("\n")
        (stage / "files" / Path(rel).name).write_text(base + "\n" + sep_addon, encoding="utf-8", newline="\n")
        scmds_sep += replace_cmds(rel, f"/payload/files/{Path(rel).name}", 0o644, f"/tmp/label_{Path(rel).name}")
        print(f"vendor/{rel}：写入 logd 串口 allow")
    vcmds += scmds_sep

    # 补丁 5：VINTF manifest version 8.0 → 7.0（目录递归取出只含 manifest/ 碎片，顶层两个要单独取）
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
                    "/etc/vintf", "/etc/vintf/manifest.xml", "/etc/vintf/compatibility_matrix.xml",
                    "-o", str(stage / "orig-vintf")], check=True, capture_output=True)
    # 5b：设备兼容性矩阵要求的 System SDK 35 → 34（A14 框架清单只支持 29–34，VintfObject
    # 判不兼容 → Build.isBuildConsistent() 为假，开机后弹"您的设备内部出现了问题"）
    sdk35 = re.compile(r"(<system-sdk>\s*<version>)35(</version>)")
    n_vintf = 0
    done_vintf = set()
    for p in sorted((stage / "orig-vintf").rglob("*.xml")):
        text = p.read_text(encoding="utf-8", errors="replace")
        new = sdk35.sub(r"\g<1>34\g<2>", text.replace('version="8.0"', 'version="7.0"')
                        .replace("<version>202404</version>", "<version>34.0</version>"))
        if new == text:
            continue
        part = p.relative_to(stage / "orig-vintf").as_posix()
        if part.startswith("vintf/"):          # 目录提取会多一层以目录名命名的子目录
            part = part[len("vintf/"):]
        rel = "etc/vintf/" + part
        if rel in done_vintf:                  # 顶层文件可能被目录提取和单独提取各取到一份
            continue
        done_vintf.add(rel)
        name = "vintf" + str(n_vintf)
        (stage / "files" / name).write_text(new, encoding="utf-8", newline="\n")
        vcmds += replace_cmds(rel, f"/payload/files/{name}", 0o644, f"/tmp/label_{name}")
        n_vintf += 1
    print(f"etc/vintf：改写 {n_vintf} 个 XML（version 8.0→7.0、system-sdk 35→34）" if n_vintf
          else "etc/vintf：无需改动")

    # 补丁 14：vendor 特性表保持通用版（蓝牙不可用，详见文件头说明）
    r = subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
                        "/" + FEATURES_REL, "-o", str(stage / "orig-feat")], capture_output=True, text=True)
    cur_feat = stage / "orig-feat" / Path(FEATURES_REL).name
    if r.returncode == 0 and cur_feat.is_file() and cur_feat.read_bytes() == FEATURES.read_bytes():
        print(f"vendor/{FEATURES_REL}：通用版，跳过")
    else:
        shutil.copyfile(FEATURES, stage / "files" / "features.xml")
        vcmds += _write_file_cmds(stage, FEATURES_REL, "/payload/files/features.xml",
                                  0o644, 0, 0, "vendor_configs_file")
        print(f"vendor/{FEATURES_REL}：换回通用版（蓝牙不可用）")

    # 补丁 7：禁用 vendor 自带的全部 A15 APEX（改扩展名，apexd 只认 *.apex）。
    # 两类问题：widevine 的 VINTF 碎片是 8.0 格式，A14 的 libvintf 解析失败会让整个设备 manifest
    # 作废（所有 HAL 的 VINTF 查找都失败）；其余（authsecret/cas/neuralnetworks/rebootescrow）
    # minSdk=35，A14 的 PackageManager scanApexPackages 直接抛异常杀 system_server（M8 实测）。
    # 对应 HAL 功能缺失（DRM/NNAPI 等）不影响开机，init 里的相关服务变成 cannot execv 警告。
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
                    "/apex", "-o", str(stage / "orig-vapex")], check=True, capture_output=True)
    vapex = stage / "orig-vapex" / "apex"
    for f in sorted(vapex.glob("*.apex")):
        (stage / "files" / ("apex-" + f.name)).write_bytes(f.read_bytes())
        vcmds += [f"rm apex/{f.name}", f"write /payload/files/apex-{f.name} apex/{f.name}.disabled"]
        print(f"vendor/apex：禁用 {f.name}")

    # 补丁 9：禁用生物识别 HAL 的 VINTF 声明 + rc（详见文件头说明）。声明按文件名识别
    # （android.hardware.biometrics.* / face-example*），第二遍运行时文件已不在、提取列表为空，
    # 自然跳过。注意不能改成 .disabled 占位：libvintf 会尝试读 manifest/ 目录下所有文件，
    # debugfs 写入的占位文件没有 SELinux 标签 → 读取被拒 → "NULL VINTF MANIFEST!" 整个清单作废
    # （M8 实测），必须彻底删除；早版本留下的占位文件也要一并清掉。rc 同理删除（init 只 glob *.rc）。
    for p in sorted((stage / "orig-vintf").rglob("*")):
        if not p.is_file() or not p.name.startswith(("android.hardware.biometrics", "face-example")):
            continue
        if not p.name.endswith((".xml", ".xml.disabled")):
            continue
        part = p.relative_to(stage / "orig-vintf").as_posix()
        if part.startswith("vintf/"):          # 目录提取会多一层以目录名命名的子目录（同补丁 5）
            part = part[len("vintf/"):]
        vcmds.append(f"rm etc/vintf/{part}")
        print(f"etc/vintf：删除生物识别声明 {part}")
    init_ls = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ext4tool.py"), "ls", str(disk_arg), voff, "etc/init"],
        capture_output=True, text=True).stdout
    init_names = {ln.split()[1] for ln in init_ls.splitlines() if len(ln.split()) >= 2}
    for rc in ("android.hardware.biometrics.fingerprint-service.ranchu.rc", "face-example.rc"):
        target = next((n for n in (rc, rc + ".disabled") if n in init_names), None)
        if target is None:
            continue
        vcmds.append(f"rm etc/init/{target}")
        print(f"etc/init/{target}：删除（断掉 ctl.interface_start 的按需拉起）")
    (stage / "vendor.cmds").write_text("".join(c + "\n" for c in vcmds), encoding="utf-8", newline="\n")

    # 补丁 6：A15 的系统库 → /vendor/lib64（vendor 组件链接需要 A15 符号）。
    # libbinder_ndk.so 再做 DT_NEEDED 手术挂上 _ndk.so（补 android::openDeclaredPassthroughHal）
    # libbinder_ndk/libbinder/libvndksupport 要用 OPLUS 版（ColorOS 15 GSI 里的）：Google 版
    # （基础镜像）libbinder_ndk 的 vendor 进程会在 /dev/vndbinder 上死等不存在的
    # vndservicemanager；A15 实测（ColorOS 15 正常开机）vendor HAL 的 binder 全走系统变体：
    # sphal 命名空间链到 system 的 libbinder/libbinder_ndk（fd 指向 binderfs/binder），所以把
    # 这三个系统库放进 /vendor/lib64（同名遮蔽 vendor 变体）。libapexsupport.so 只有基础镜像有。
    GSI15_IMG = ROOT / "images" / "gsi" / "coloros15-system.img"
    boff = subprocess.run([sys.executable, str(ROOT / "scripts/superimg.py"), str(BASE_IMG), "system"],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    (stage / "a15").mkdir(parents=True, exist_ok=True)
    have = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ext4tool.py"), "ls", str(disk_arg), voff,
         "lib64"], capture_output=True, text=True).stdout
    for lib, src in (("libbinder_ndk.so", "gsi15"), ("libbinder.so", "gsi15"),
                     ("libvndksupport.so", "gsi15"), ("libui.so", "gsi15"),
                     ("libapexsupport.so", "base")):
        origin = None
        if src == "gsi15" and GSI15_IMG.is_file():
            subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(GSI15_IMG),
                            "/system/lib64/" + lib, "-o", str(stage / "a15")],
                           check=True, capture_output=True)
            if (stage / "a15" / lib).is_file():
                origin = "ColorOS 15 GSI"
        if origin is None:
            subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(BASE_IMG), boff,
                            "/system/lib64/" + lib, "-o", str(stage / "a15")],
                           check=True, capture_output=True)
            origin = "基础镜像"
        # libbinder.so/libui.so 是替换 vendor 原有的（换成系统变体），其余是新增、装过就跳过
        if lib not in ("libbinder.so", "libui.so") and lib in have:
            print(f"vendor/lib64：{lib} 已存在，跳过")
            continue
        blob = (stage / "a15" / lib).read_bytes()
        if lib == "libbinder.so":
            blob = patch_libbinder_symbol(blob)
        (stage / "files" / lib).write_bytes(blob)
        vcmds += _write_file_cmds(stage, "lib64/" + lib, f"/payload/files/{lib}",
                                  0o644, 0, 0, "same_process_hal_file")
        print(f"vendor/lib64：装入 {origin} 的 {lib}")
    # 6b：_ndk.so（源码 gfx/vendor-overlay/gsi/coloros14/woa_passthrough_shim.c；不能按子串判断，
    # ls 输出里的 "libbinder_ndk.so" 也含 "_ndk.so"——内容一致才跳过）
    shim_cur = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), voff,
         "/lib64/_ndk.so", "-o", str(stage / "cur-shim")], capture_output=True, text=True)
    if shim_cur.returncode == 0 and (stage / "cur-shim" / "_ndk.so").is_file() \
            and (stage / "cur-shim" / "_ndk.so").read_bytes() == SHIM.read_bytes():
        print("vendor/lib64：_ndk.so 已是最新，跳过")
    else:
        (stage / "files" / "_ndk.so").write_bytes(SHIM.read_bytes())
        vcmds += _write_file_cmds(stage, "lib64/_ndk.so", "/payload/files/_ndk.so",
                                  0o644, 0, 0, "same_process_hal_file")
        print("vendor/lib64：装入 _ndk.so")
    # 6c：vendor 的 libbinder_ndk.so 打 DT_NEEDED 补丁（始终从干净的 A15 副本重建，幂等）
    (stage / "files" / "libbinder_ndk.so").write_bytes(
        patch_ndk_dtneeded((stage / "a15" / "libbinder_ndk.so").read_bytes()))
    vcmds += _write_file_cmds(stage, "lib64/libbinder_ndk.so", "/payload/files/libbinder_ndk.so",
                              0o644, 0, 0, "same_process_hal_file")
    print("vendor/lib64/libbinder_ndk.so：追加 DT_NEEDED _ndk.so")
    (stage / "vendor.cmds").write_text("".join(c + "\n" for c in vcmds), encoding="utf-8", newline="\n")

    # 补丁 8：去掉 zygote_secondary 的全部服务定义（arm64-only 的 GSI 没有 app_process32，
    # 它 exec 127 反复退出，onrestart 里的 restart zygote 每次都把主 zygote（连带 system_server）
    # 杀一轮，永远开不了机（M8 实测）。共两处：init.zygote64_32.rc（换成只剩 import 64 位）和
    # system_ext 的 tango-binfmt.rc——OPLUS 给 Tango 转译项目用 override 补回了一份定义。
    # init.rc 里零星 start zygote_secondary 变成 service not found 警告，无害。
    for rel, new_text in (
        ("system/etc/init/hw/init.zygote64_32.rc", "import /system/etc/init/hw/init.zygote64.rc\n"),
        ("system/system_ext/etc/init/tango-binfmt.rc", None),
    ):
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                        "/" + rel, "-o", str(stage / "orig-zrc")], check=True, capture_output=True)
        cur = (stage / "orig-zrc" / Path(rel).name).read_text(encoding="utf-8", errors="replace")
        if not any(ln.startswith("service zygote_secondary") for ln in cur.split("\n")):
            # 只剩 "#service zygote_secondary..." 之类的注释行也算已处理
            print(f"{rel}：已无 zygote_secondary，跳过")
            continue
        if new_text is None:
            lines = cur.split("\n")
            start = next(i for i, ln in enumerate(lines) if ln.startswith("service zygote_secondary"))
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].startswith("#endif")), len(lines))
            lines[start:end + 1 if end < len(lines) else end] = []
            new_text = "\n".join(lines)
        (stage / "files" / Path(rel).name).write_text(new_text, encoding="utf-8", newline="\n")
        scmds += replace_cmds(rel, f"/payload/files/{Path(rel).name}", 0o644, "/tmp/label_zrc")
        print(f"{rel}：去掉 zygote_secondary 服务定义")

    # 补丁 11（system 分区）：libvintf 的 GKI 内核等级换算越界不再 abort（详见文件头说明）
    subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                    "/" + VINTF_REL, "-o", str(stage / "orig-vintf-lib")], check=True, capture_output=True)
    lib = (stage / "orig-vintf-lib" / Path(VINTF_REL).name).read_bytes()
    if lib.count(VINTF_SIG_NEW) == 1:
        print(f"{VINTF_REL}：已打过，跳过")
    elif lib.count(VINTF_SIG_OLD) == 1:
        (stage / "files" / "libvintf.so").write_bytes(lib.replace(VINTF_SIG_OLD, VINTF_SIG_NEW))
        scmds += replace_cmds(VINTF_REL, "/payload/files/libvintf.so", 0o644, "/tmp/label_vintf")
        print(f"{VINTF_REL}：gkiAndroidReleaseToLevel 越界改为返回 UNSPECIFIED")
    else:
        sys.exit(f"{VINTF_REL} 里找不到 gkiAndroidReleaseToLevel 的特征指令（GSI 版本不同？）")

    # 补丁 15（system 分区）：框架兼容性矩阵补一条 6.6 内核条目（详见文件头说明）
    for k, (rel, level) in enumerate(FCM_KERNEL):
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                        "/" + rel, "-o", str(stage / f"orig-fcm{k}")], check=True, capture_output=True)
        text = (stage / f"orig-fcm{k}" / Path(rel).name).read_bytes().decode("utf-8")
        if 'kernel version="6.6.0"' in text:
            print(f"{rel}：已有 6.6 内核条目，跳过")
            continue
        at = text.index("    <kernel version=")
        entry = (f'    <!-- RanchuWoA: 模拟器内核是 android15-6.6 GKI，A14 的矩阵没有 6.6 条目 -->\n'
                 f'    <kernel version="6.6.0" level="{level}"/>\n')
        (stage / "files" / f"fcm{k}").write_bytes((text[:at] + entry + text[at:]).encode("utf-8"))
        scmds += replace_cmds(rel, f"/payload/files/fcm{k}", 0o644, f"/tmp/label_fcm{k}")
        print(f"{rel}：插入 6.6 内核条目（level {level}）")

    # 补丁 12/13（system 分区）：OPLUS 框架 dex 的指令级补丁（详见文件头说明）
    for k, (rel, (patches, stale)) in enumerate(DEX_PATCHES.items()):
        name = Path(rel).name
        subprocess.run([sys.executable, str(ROOT / "scripts/ext4tool.py"), "get", str(disk_arg), soff,
                        "/" + rel, "-o", str(stage / f"orig-dex{k}")], check=True, capture_output=True)
        blob, notes = patch_zip_dex((stage / f"orig-dex{k}" / name).read_bytes(), patches)
        shutil.rmtree(stage / f"orig-dex{k}")
        for n in notes:
            print(f"  {n}")
        if blob is None:
            print(f"{rel}：已打过，跳过")
            continue
        (stage / "files" / f"dex{k}-{name}").write_bytes(blob)
        scmds += replace_cmds(rel, f"/payload/files/dex{k}-{name}", 0o644, f"/tmp/label_dex{k}")
        scmds += [f"rm {p}" for p in stale]               # 预编译产物的 dex 校验和已对不上
        print(f"{rel}：写回，删除对应的预编译产物")

    if not scmds and not vcmds:
        print("已完成（无改动）")
        return
    (stage / "system.cmds").write_text("".join(c + "\n" for c in scmds), encoding="utf-8", newline="\n")

    print("写回 vendor 与 system 分区（Alpine 辅助 VM）...")
    job = (JOB.replace("{voff}", str(voff)).replace("{soff}", str(soff))
           .replace("{props}", " ".join(rel for rel, _ in PROP_EDITS)))
    rc = run_job(job, disks=[disk_arg], payload_dir=stage,
                 log=ROOT / "images" / ".alpine-job" / "coloros14_fixes.log", echo=False)
    print("完成" if rc == 0 else f"失败（rc={rc}），日志 images/.alpine-job/coloros14_fixes.log")
    sys.exit(rc)


if __name__ == "__main__":
    main()
