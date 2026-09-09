# 在 MacBook (Apple M4, 16 GB) 上搭建实验环境

## 1. 硬件/内存分配

| 项 | 建议 | 说明 |
|---|---|---|
| 客体 (Android) RAM | 3072 MB 基线, 2048 MB 高压 | 16 GB 主机: macOS 自身 + 模拟器进程约需 5–6 GB, 客体 3 GB 时仍有余量; 2 GB 客体更容易触发回收 |
| 客体 CPU | 4 核 | `-cores 4`; M4 大核充足 |
| 磁盘 | ≥ 15 GB | 系统镜像 ~1.5 GB, AVD userdata 8 GB, APK 缓存 |
| 镜像 ABI | `arm64-v8a` | Apple Silicon 通过 Hypervisor.framework 原生运行 arm64 镜像; x86 镜像走翻译, 极慢且不代表真机 |
| 镜像类型 | `google_apis` 或 `default` | 两者都允许 `adb root`; **`google_apis_playstore` 不允许 root**, 无法操作 cgroup/zram |
| API 级别 | 35 (Android 15) 推荐, 34 也可 | Android 15 模拟器内核 6.6, cgroup v2 上有 `memory.reclaim`, freezer 位于 `/sys/fs/cgroup/uid_*/pid_*` |

> Android 模拟器不是真机: 没有真正的闪存写入放大, 没有厂商 lmkd 调优, GPU 走 Metal 转译。它适合验证算法
> 与控制回路, 得出的绝对时延不能替代 root 真机数据 (文档 §3.6 "Android 真机验证" 阶段)。

## 2. 安装 SDK

方式 A (Android Studio): 安装后在 *Settings → Languages & Frameworks → Android SDK → SDK Tools* 勾选
**Android SDK Command-line Tools**、**Android Emulator**、**Android SDK Platform-Tools**。SDK 位于
`~/Library/Android/sdk`。

方式 B (Homebrew, 无 IDE):

```bash
brew install --cask android-commandlinetools temurin   # 需要 JDK 17+
export ANDROID_HOME=~/Library/Android/sdk
```

`scripts/env.sh` 会自动探测两种布局, 也可以显式 `export ANDROID_HOME=...`。

## 3. 创建并启动 AVD

```bash
scripts/setup_avd.sh 35 cf_api35 3072          # API, AVD 名, RAM(MB); 第四个参数可选 default 换 AOSP 镜像
scripts/start_emulator.sh cf_api35 3072        # 等待 sys.boot_completed=1
```

`setup_avd.sh` 写入 `~/.android/avd/cf_api35.avd/config.ini`: `hw.ramSize`, `hw.cpu.ncore=4`,
`disk.dataPartition.size=8G`, `fastboot.forceColdBoot=yes` (每次冷启动, 避免快照把上次实验的内存状态带进来)。

`start_emulator.sh` 使用 `-no-snapshot -no-boot-anim -no-audio -gpu auto`; 若要无窗口跑批量实验加 `-no-window`。
注意 `-memory` 会覆盖 config.ini 里的 RAM, 因此不同压力等级可只改这个参数。

## 4. 设备准备

```bash
scripts/prepare_device.sh --system-freezer disabled --zram-mb 1024 [--swapfile-mb 512]
```

* `adb root` — 失败说明用了 Play 镜像。
* `cached_apps_freezer disabled` — 关闭 Android 自带的 cached-app freezer, 否则系统会和我们的控制器争抢
  `cgroup.freeze`。跑 **"Android 默认" 基线** 时改为 `--system-freezer enabled` 并使用 `policy=none` 配置。
  该设置需要重启, 脚本会自动重启并等待。
* zram — API 35 (Android 15) google_apis 镜像默认已启用 zram (约 RAM 的 75%, lz4, 优先级 -2), 脚本会打印
  `already active`; 旧镜像没有 swap 时脚本在 `/dev/block/zram0` 上建立压缩交换区并 `swapon`。
  若内核没有 zram, 用 `--swapfile-mb` 在 `/data` 上建交换文件 (相当于 "闪存" 层)。
  两者同时开启时 zram 优先级更高, 冷页先进 zram、更冷的溢出到文件 —— 对应文档的 DRAM / ZRAM / 闪存三层。
* 关闭动画、保持亮屏、解锁 — 让 `am start -W` 的 `TotalTime` 更稳定。

### 在 Android 15 (API 35, 内核 6.6) google_apis 镜像上实测到的内核布局

| 项 | 实测 | 对工具的影响 |
|---|---|---|
| cgroup v2 `/sys/fs/cgroup` | 挂载但 `cgroup.controllers` 为空, 只有 freezer 语义; 每个应用进程在 `uid_<uid>/pid_<pid>/` | 冻结用 `cgroup.freeze` (已验证 `cgroup.events` 中 `frozen 1/0`) |
| memcg | v1 在 `/dev/memcg`, `ro.config.per_app_memcg` 未开启, 所有应用都在根 memcg | 没有 v2 `memory.reclaim`; 工具为每个应用建 `/dev/memcg/cf/uid_<uid>`, 置 `memory.move_charge_at_immigrate=3` 后把进程迁入, 再写 `memory.force_empty` 做 **按应用回收** (实测 Clock: PSS 42 MB → 35 kB, SwapPss 0 → 22 MB) |
| `/proc/<pid>/reclaim` | 不存在 | 无法只回收匿名页; force_empty 同时丢弃文件页 |
| zram | 默认开启 `/dev/block/zram0` (lz4) | `prepare_device.sh --zram-mb` 只在未开启时创建 |
| Settings | 与 system_server 共用 uid 1000 | runner 自动加入 `never_freeze`, 且所有操作只按 **进程名** 匹配, 绝不按 uid 整组冻结 |

真机 (per_app_memcg=true) 上应用自带 `/dev/memcg/apps/uid_X/pid_Y`, 工具直接对该组 `force_empty`; 若是 cgroup v2
memcg 则走 `memory.reclaim`。

## 5. 应用集合

API 35 google_apis 镜像自带 (`scripts/list_launchable.sh` 实测): Settings, Chrome, Messages, Phone, Contacts,
Calendar, Clock, Gmail, Maps, YouTube, YouTube Music, Photos, Docs, Files, Google, Camera。
Messages / Calendar / Gmail 等首次打开会弹登录或 "What's New" 引导页, 此时 `am start -W` 返回
`LaunchState: UNKNOWN (0)` (工具记为 `FRONT`, 不计时延); **实验前请手动把每个应用打开一次并跳过引导**,
或把这类应用从列表中去掉。若还想加入更 "重" 的应用, 可安装 F-Droid 开源应用:

```bash
python3 scripts/install_fdroid_apps.py          # Firefox(fennec), VLC, NewPipe, Organic Maps, Wikipedia, AntennaPod
scripts/list_launchable.sh                      # 打印所有带 LAUNCHER 入口的包名
python3 run_experiment.py configs/emulator_base.json --probe   # 检查 uid / Activity 解析
```

`configs/*.json` 里不存在的包会被 runner 自动跳过 (并打印 WARN), 因此可以直接编辑 `apps` 列表。
k=10 左右即可, Bellman 离线最优 (模拟器轨迹回放到 `cf.sim`) 支持 k ≤ 12。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| `adb root` 提示 `cannot run as root in production builds` | 换 `google_apis` / `default` 镜像 |
| `--probe` 显示 `freezer=sigstop` | 镜像没有把应用放进 cgroup v2 freezer 层级 (Android 10 及以下); 会退回 SIGSTOP, 语义相近但 Binder 调用方可能阻塞 |
| `reclaim_methods` 只剩 `balloon` | 既没有 memcg (v1 `/dev/memcg` 或 v2 `memory.reclaim`) 也没有 `/proc/<pid>/reclaim`; runner 会用 tmpfs 气球制造全局压力, 由内核 LRU 把被冻结 (最冷) 的应用页面挤入 zram |
| `LaunchState: TIMEOUT` | `am start -W` 内部等待首帧超时 (模拟器极慢时出现, 例如无 KVM 的 x86 软件模拟); 记录 `WaitTime` 作为时延下界。M4 上原生 arm64 镜像不会出现 |
| 应用 `LaunchState: COLD` 频繁 | lmkd 在杀后台进程; 可提高客体 RAM, 或在配置里设置 `"stop_lmkd": true` (仅实验用, 由内核 OOM killer 兜底) |
| `am start -W` 无 `TotalTime` | 该 Activity 已在前台 (记为 `FRONT`), trace 生成器默认不允许连续重复请求 |
| 主机内存告急 | 关闭 IDE/浏览器, 或客体降到 2048 MB; 不要在同一台机器上同时开两个模拟器 |
