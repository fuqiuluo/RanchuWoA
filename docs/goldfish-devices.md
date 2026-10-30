# 自研 goldfish / virtio 设备

官方 Android 模拟器的 guest HAL 依赖一组 QEMU 侧设备与服务。Android 15 镜像里这些依赖分成了两条通道：

- **goldfish pipe**（`/dev/goldfish_pipe_dprctd`）：只剩图形管道（`pipe:opengles`、`pipe:GLProcessPipe`、
  `pipe:refcount`），由 QEMU 的 gfxstream 宿主渲染器应答（见 [graphics.md](graphics.md)）；
- **virtio-vsock**：其余全部 qemud 服务（传感器、开机属性、剪贴板、GPS、adb、心跳…），Android 15 的
  guest 库默认走 vsock（宿主 CID 2、端口 5000），不再走 pipe。

QEMU 侧实现都在 `qemu-ranchu/hw/android/`（补丁里有 `RanchuWoA` 注释标记）。上游规范文档摘录在
`docs/reference/`（GOLDFISH-VIRTUAL-HARDWARE.TXT、ANDROID-QEMU-PIPE.TXT 等）。

## 1. goldfish_pipe（`hw/android/goldfish-pipe.c`）

上游协议（v2），guest 驱动是 vendor.img 里的 goldfish_pipe.ko：

- 打开 `/dev/goldfish_pipe` 后写零结尾的服务名（如 `pipe:opengles\0`），之后该 fd 为双向字节流；
- 寄存器：REG_CHANNEL / ADDRESS / SIZE / CMD / STATUS / WAKES；
- 命令：`PIPE_CMD_{OPEN, CLOSE, POLL, READ, WRITE, WAKE_ON_*, DMA_MAPHOST}`；
- v2 协议带批量 command buffer 与 signalled-pipe 缓冲（guest 把命令写进共享页，敲 CMD 批量提交）。

本项目实现：设备本身 + gfxstream 渲染器接入（`pipe:opengles` 等由 virtio-gpu/rutabaga 侧应答，
本设备只做转发）。诊断时可加 `debug=on` 属性打印每次服务请求。

## 2. goldfish-address-space（`hw/android/goldfish-address-space.c`）

gralloc/编解码器零拷贝用的共享内存窗口，PCI 设备 **607d:f153**：

- BAR0 = 控制寄存器（0x1000），BAR1 = 64 位共享内存（属性 `area-size`，AOSP 默认 16GB）；
- 寄存器偏移：`0x00 COMMAND`（1=ALLOC / 2=DEALLOC / 3=GEN_HANDLE / 4=DESTROY / 5=TELL_PING）、
  `0x04 STATUS`、`0x08 GUEST_PAGE_SIZE`、`0x0C/0x10 SIZE`、`0x14/0x18 OFFSET`、`0x28 HANDLE`、
  `0x2C/0x30 PHYS_START`（= BAR1 的总线地址）；
- 内核侧驱动 `goldfish_address_space.c` 对 PCI ID 有三个硬性要求，缺一个 probe 就 `-ENODEV` 或不分配资源：
  1. class 必须 `PCI_CLASS_OTHERS`（`PCI_CLASS_MEMORY_RAM` 会让内核不分配 BAR 资源）；
  2. revision 必须 1；
  3. BAR1 必须 **64 位 + prefetchable**（内核用 `memremap(MEMREMAP_WB)` 映射，要求可缓存）。

诊断技巧：设备侧在 probe 序列的最后几步寄存器写入（GUEST_PAGE_SIZE、PHYS_START_LOW/HIGH）处
`error_report` 打日志，出现即 guest 全链路（probe → misc → /dev 节点 → gralloc 使用）通。

## 3. goldfish-sync（`hw/android/goldfish-sync.c`）

GrallocSync fence 用，sysbus 设备 @0xa020000，兼容串 `google,goldfish-sync`：

- 寄存器：`0x00 BATCH_COMMAND`（写 0 降 IRQ）、`0x04 BATCH_GUESTCOMMAND`（触发）、
  `0x08/0x0C + 0x10/0x14` 批缓冲 GPA、`0x18 INIT`；
- guest command 结构 `{host_command, glsync_handle, thread_handle, guest_timeline_handle}`，
  命令 0–5（READY / CREATE_TIMELINE / CREATE_FENCE / TIMELINE_INC / DESTROY / TRIGGER_HOST_WAIT）；
- 无渲染器时做记账实现（timeline 计数，fence 立即 signaling），gralloc 的 GrallocSync 不需要真实 GPU 同步；
- **中断号必须避开 SPI 7**：virt 板的 PL061 GPIO 控制器默认占用 SPI 7，与上游 ranchu 的取值冲突，
  本项目用 SPI 81。gralloc 配置批命令缓冲的表现是 MMIO 写 0x8/0xC（批缓冲 GPA）。

## 4. ranchu-vsock（`hw/android/ranchu-vsock.c`，`-device ranchu-vsock-device`）

Windows 没有 vhost-vsock，本项目实现了纯用户态 virtio-vsock（规范 5.10：STREAM、信用流控），
并在端口 5000 上内置 qemud 服务。guest 的 `libqemupipe` 对 vsock 通道的服务发现与 pipe 相同
（连接后先发服务名）。

### 4.1 内置服务（端口 5000）

| 服务 | 行为 |
|---|---|
| `pipe:qemud:sensors` | 静态读数（竖屏直立、300 lux…），消除传感器 HAL 崩溃循环 |
| `pipe:qemud:boot-properties` | 读 `boot-props="k=v;..."` 属性，开机时注入 guest（如 qemu.timezone → vendor.qemu.timezone） |
| `pipe:QemuMiscPipe` | 心跳/开机完成通知；bootcomplete 时 QEMU 日志打 `ranchu: guest boot completed` |
| `pipe:clipboard` | `[u32 长度][UTF-8]` 帧，与 Windows 剪贴板双向同步（message-only 窗口 + AddClipboardFormatListener）。不实现的话 guest 会以每秒数百次重连 |
| `pipe:qemud:gps` | NMEA 流，见 §4.3 |
| `pipe:qemud:camera` | 空相机列表（`"%08x"` 长度前缀 + `ok:<NUL>`），camera provider 不再空转 |
| `pipe:qemud:fingerprintlisten` | HAL 发 `0006listen` 后保持连接不回，不空转 |
| 未知服务 | **保持连接、丢弃数据**（日志记一次），宁可阻塞调用方也不让它空转。见 §4.4 |

### 4.2 宿主连接管理

- `forward=5555:5555`：宿主 127.0.0.1:5555 ↔ guest vsock:5555（adbd 设 `service.adb.tcp.port` 时同时监听
  vsock）。bootcomplete 后 3 秒及每次心跳，向 adb server 发 `host:reconnect-offline` + `host:emulator:5555`
  自动登记 transport（smart-socket 协议：4 位 hex 长度前缀），`adb devices` 直接出现 emulator-5554。
  实测 push 100+ MB/s。
- `boot-cmd=<命令>`：每次开机完成后 4 秒，用 §4.5 的宿主 adb 客户端在 guest 里执行（avdctl 用来补
  `device_provisioned=1` 等没有开机向导的 GSI 需要的标志；adbd 要求认证时打警告放弃）。
- 宿主发起的 vsock 连接 5 秒等不到 guest RESPONSE 就 RST 并关宿主 socket（guest 正在关机时发出的
  REQUEST 可能没人回）。

### 4.3 GPS（`pipe:qemud:gps`）

GNSS HAL 在有应用请求定位时才连接，自己按 `$` 开头、换行结尾切分 NMEA，不需要 qemud 帧：

- `$GPRMC,hhmmss,A,ddmm.mmmm,N,dddmm.mmmm,E,速度节,航向,ddmmyy,磁偏角,E*CS`——sscanf 必须 13 个字段
  齐全且有效位为 A，才产生定位；
- `$GPGGA,hhmmss,纬,N,经,E,1,卫星数,HDOP,海拔,M,...`——海拔与卫星列表；
- 设备每秒先发 GGA 后发 RMC，校验和 = `$` 与 `*` 之间字节的异或；
- 位置是 QOM 属性 `gps`（`纬度;经度[;海拔]`），运行中可 `qom-set` 热改，`avdctl geo` 封装并记入实例配置。
  默认 37.4220,-122.0841（与官方模拟器相同）。

### 4.4 HAL 空转与 EOF 语义

对不认识的服务回 RST 会造成 **vCPU 空转**：guest 内核收到 vsock RST 只设 `peer_shutdown`，poll 报
`EPOLLIN|EPOLLRDHUP` 而没有 `EPOLLHUP`，read 返回 0；而 goldfish HAL 普遍不处理 EOF（camera 的
`qemu_pipe_read_fully` 在 read 返回 0 时死循环；fingerprint/GNSS 的 epoll 只认 `EPOLLERR|EPOLRHUP`）。
所以策略是保持连接、丢弃数据。

### 4.5 powerdown：QEMU 自己当 adb 客户端

`-kernel` 直引没有 ACPI GED，QMP `system_powerdown` 只能模拟按电源键（Android 当锁屏）。正常关机
（关窗口、`avdctl stop`）的实现：

1. ranchu-vsock 收到 bootcomplete 后置只读 QOM 属性 `poweroff-ready`；
2. powerdown 请求时向 guest vsock:5555 发起连接，ADB 协议握手（`CNXN(0x01000001, 256K, "host::")`，
  头部校验和为负载字节和），收到 adbd 的 CNXN 后发 `OPEN(1, 0, "shell:svc power shutdown || reboot -p")`；
3. `svc power shutdown`（shell 用户有权限）走框架 ShutdownThread，与电源菜单"关机"相同；失败退回
  init 层 `reboot -p`；
4. virt 的 powerdown 处理在 ranchu 模式且 `poweroff-ready` 时不再按电源键；SDL 关窗先走此流程，
  标题显示"正在关机…"，20 秒兜底强制退出，1 秒内重复请求忽略。

### 4.6 Windows socket 关闭的坑

QEMU 11 在 Windows 上把 `socket()`/`accept()` 返回的 fd 包了一层（`close()` 会被宏替换识别 socket），
设备代码里对这类 fd 必须用 `close()` 而不是 `closesocket()`（后者不认包装后的小整数，静默失败 →
句柄泄漏、对端收不到断开，adb server 会一直认为旧 transport 活着）。另外 `close()` 后 socket 可能停在
CLOSE_WAIT 不发 FIN（底层对象被其他引用撑着），关之前先 `shutdown(SD_BOTH)` 保证协议层立即断开。

## 5. 输入（`hw/input/virtio-input-hid.c` 补丁）

`virtio-touchscreen-pci`：BTN_TOUCH + ABS_X/Y + INPUT_PROP_DIRECT，Android 认作内置单点触摸屏
（virtio-tablet 报 BTN_LEFT 会被 EventHub 忽略）。键位：

- 左键 = 触摸（未按下不报坐标），右键 = BACK，中键 = HOME；
- `INPUT_BUTTON_EXTRA`（鼠标 X2）→ KEY_APPSELECT（最近任务），`INPUT_BUTTON_SIDE`（X1）→ KEY_BACK；
  （QEMU 的 qcode 表没有 KEY_APPSELECT，所以在触摸屏设备上加了这两个映射，侧边工具栏也走这条路）；
- SDL 层 Ctrl+Alt+P = 电源键（按住 100ms 再松开；瞬时按松 OEM 可能当抖动忽略）。

配套 `virtio-keyboard-pci` 供文本输入。鼠标绝对坐标模式不 grab、不隐藏光标，标题用 `-name`，
DPI 感知，初始窗口按桌面可用区域等比缩小。

## 6. 其他设备

- **virtio-snd**（`-device virtio-sound-pci,audiodev=snd0`）：guest 内核 `CONFIG_SND_VIRTIO` 模块自动加载，
  官方镜像的 goldfish 音频 HAL（tinyalsa）开 card0/device0；Windows 后端 dsound/sdl/wav。
  `streams=1` 只建放音流（virtio-snd 在 realize 时就 prepare 每路流，录音流会打开宿主录音设备），
  `create --mic` 才用 2 路。
- **goldfish-battery** @0xa040000：位置与官方模拟器一致，vendor 的 sysfs SELinux 标签按此路径匹配
  （见 [architecture.md](architecture.md) §6）。
- **virtio-net**（slirp user 模式）：DHCP/DNS 自带；vendor 特性声明加 `android.hardware.ethernet` 让
  EthernetService 接管 eth0，并删掉把 eth0 配成受限网络的 goldfish connectivity overlay。
- **virtconsole**：第二个口给 `/dev/hvc1` → 宿主持续抓 logcat（`androidboot.logcat=*:D`）。
