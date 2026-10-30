# 排查手册

面向两类问题：**guest 起不来**（引导链）与**画面不对**（图形栈），外加日常开发的坑。GSI 特有的
排查见 [gsi-guide.md](gsi-guide.md) §4。

## 1. 日志与端口

| 位置 | 内容 |
|---|---|
| `images/avds/<名字>/logs/qemu.log` | QEMU 输出：gfxstream 宿主日志、设备日志；开机完成时打 `ranchu: guest boot completed`；gfxstream 崩溃记录搜 `RanchuWoA-VEH` |
| `images/avds/<名字>/logs/console.log` | 串口控制台全部输出；`start --verbose-boot` 时是内核+init 全量日志 |
| `images/avds/<名字>/logs/logcat.txt` | 持续写入的 logcat（virtconsole /dev/hvc1） |
| `images/avds/<名字>/logs/toolbar.log` | 侧边工具栏的错误输出 |

第 i 个实例的端口（只监听 127.0.0.1）：adb `5555+2i`（序列号 emulator-5554+2i）、串口控制台 25400+10i、
HMP 25401+10i、QMP 25402+10i、工具栏专用 QMP 25403+10i。

- 串口控制台是 guest 的 shell（`su 0` 提权）：`py scripts/vserial.py`。QEMU 的 Windows TCP 串口不适合
  反复重连，尽量保持一条连接。
- 截屏：`py scripts/hmp.py --port <HMP端口> --shot out.png`（走 QEMU，不依赖 guest）；
  输入自动化：`py scripts/qmp_input.py`（tap/swipe/key/text，走真实输入层）。

## 2. guest 起不来

按能看到的日志从早到晚排查：

1. **`--verbose-boot` 看 console.log**：加 `earlycon` 后内核日志从第一条就有。卡在
   `smp: Bringing up secondary CPUs` = WHPX 分区 reset 问题（本项目已修，见
   [architecture.md](architecture.md) §8；出现说明跑了未打补丁的 QEMU）。
2. **init 静默重启**：最后一行 `reboot: Restarting system with command 'bootloader'` 且无其他错误 =
   first_stage_mount 失败走 InitFatalReboot（`ro.boot.init_fatal_reboot_target` 默认 bootloader），
   致命日志在 kmsg 打开前就丢了。最常见根因是 `androidboot.boot_devices` 缺失/盘不在预期槽位
   （[architecture.md](architecture.md) §5）。
3. **"无限重启但 logcat 看起来正常"**：console.log 里找 init 的 `shutdown_command`/`reboot_on_failure`
   （vold、netd 是 reboot_on_failure 服务）；qemu.log 反复 `guest boot completed` = 每轮开机完成后
   立刻重启的指纹。
4. **SELinux fatal**："Unable to open SELinux policy"（缺 *_debug 策略副本）、属性上下文 invalid_context
   （contexts 引用未定义类型，GSI 场景用 `scripts/gsi_undef_types.py`）。

### 用 gdb 定位 init 的致命路径（不依赖任何 guest 日志）

init 静默退出时最有效的手段：

1. `vmlinux-to-elf` 从 kernel-ranchu 提取符号表（KASLR 下内核命令线加 `nokaslr`）；
2. TCG + `-s -S` 起QEMU（WHPX 不支持 gdbstub），gdb 断点 `__arm64_sys_reboot`；
3. 命中后沿栈回溯解码 pt_regs，得到用户态调用链与信号帧（init 的致命消息经 SIGABRT 处理器拼在栈上，
   可直接读出字符串，例如 "Failed to create devices required for first stage mount"）。

原生进程崩溃：`debuggerd -b <pid>` 打全部线程栈（SurfaceFlinger 卡在哪、HAL 在等哪个 pipe 一目了然）。

## 3. 画面不对

1. **先分后端**：`avdctl edit <名字> --gpu swiftshader` 换 CPU 渲染对照——问题消失 = 宿主渲染链
   （gfxstream/ANGLE）的事；仍在 = gralloc/hwc3/virtio-gpu 或 guest 侧的事。
2. **qemu.log**：`RanchuWoA-VEH` = gfxstream 宿主崩溃（带故障地址、读写方向、x0–x30、栈上返回地址的
   模块+偏移）；`window handle ... not found` / `Failed to find ColorBuffer` = 宿主对象被提前清理
   （puid 生命周期问题）；成串 "请求服务: pipe:opengles" 正常（图形通道）。
3. **符号化**：宿主 DLL 用 `llvm-symbolizer --obj=hostgfx/libgfxstream_backend-0.dll 0x180<RVA>`；
   ANGLE 内部的崩溃换一份带 PDB 的 ANGLE 复现（放 `hostgfx/angle/`，Electron 的 `*-pdb.zip` 可用
   HTTP Range 只取需要的 PDB）。
4. **闪烁/黑帧**：QMP screendump/HMP 截屏看不到窗口实际呈现的问题，用 `scripts/window_cap.py
   "<名字> - RanchuWoA" <秒> <目录> --adb <序列号> --cmd "<adb shell 操作>"` 抓真实窗口（GDI 截客户区，
   统计单帧闪烁与黑帧），滚动回跳用 `scripts/flicker_cap.py`。
5. **应用画不出来**：logcat 搜 `GFXSTREAM`——guest 编码器在程序链接失败/着色器编译失败时把宿主 info
   log 和着色器源码开头打出来（tag `GFXSTREAM`，`RanchuWoA:` 前缀）；`adb shell setprop
   debug.gfxstream.gltrace <进程名子串>` 后重启该进程，看 `RW-trace` 的 GL 调用序列；着色器单独试编
   用 `debug/guest/test_shaders.c`。
6. **gralloc**：`adb shell su 0 setprop log.tag.woa-allocator D` 后看 logcat 的分配日志。
7. **guest 探针**（NDK clang 编译，push 到 /data/local/tmp）：`debug/guest/test_egl.c`（EGL 全链路）、
   `test_ahb_cpu.c`（CPU⇄GPU 一致性）、`test_dmabuf_mmap.c`（dma-buf 带偏移 mmap，需 root）。
8. **帧率/掉帧**：`dumpsys gfxinfo <包名>`；`dumpsys display` 的 activeMode/renderFrameRate 对比预期
   （历史上 EDID 75Hz 曾把渲染帧率锁在 37.5）。
9. **"黑屏"多半是灭屏**：`dumpsys power` 的 mWakefulness；`adb shell input keyevent WAKEUP` 亮屏。

## 4. adb / 网络

- `adb devices` 出现 `offline`：`adb reconnect offline`，或 `adb kill-server` 后等半分钟（模拟器会
  重新登记 transport）。
- guest 网络（slirp）不通时先确认 boot_completed 达成——connectivity 服务没跑到时 netd 的策略路由
  会让一切直连 socket ENETUNREACH；开机完成后 EthernetService 自动 DHCP，不需要手工配置。
- Git Bash 里 adb/pull 的 guest 路径会被 MSYS 改写：`export MSYS_NO_PATHCONV=1`。

## 5. 开发环境的坑

- **改源文件后必须 `touch`**：Edit 类工具不改 mtime，ninja 会说 "no work to do"。
- **构建/安装前停掉所有 VM**：QEMU exe、libgfxstream_backend DLL 被占用会 Permission denied。
- **MSYS2 构建**：`USERPROFILE` 环境变量要存在（Python 3.14 在 MSYS2 shell 里 `Path.home()` 会抛）；
  PATH 前 + `/c/msys64/clangarm64/bin`。脚本已处理。
- **运行中的 guest 换 /vendor 里的 .so**：remount rw 后先写成临时名、`chcon u:object_r:same_process_hal_file:s0`、
  再 mv 覆盖——原地 cp 会让所有映射着旧库的进程崩掉（SF、SystemUI 一起重启）。GL 驱动是 zygote
  预加载的，换完要 `stop; start` 才对应用生效。这些改动落在实例的 system.qcow2 里，`avdctl edit
  --system` 会丢弃。
- **bash heredoc / python -c 里别写反斜杠转义**（`\n`、行尾续行）：会被吃掉；批量替换写成脚本文件再跑。
- **gdb 附着**不要用 `timeout gdb`（会连 QEMU 一起杀）；**退出码** 139 = SIGSEGV（Windows c0000005），
  0x...235 = CloseHandle 受保护句柄（可恢复，不用追）。
- guest 重启进 bootloader 时 QEMU 会无限重启（没加 -no-reboot），用 `avdctl stop --force`。
- 基础镜像只能 `avdctl prepare --force` 重建；GSI 实验盘要 `gsi_disk.py` 重新生成再
  `avdctl edit <名字> --system <盘>`。
