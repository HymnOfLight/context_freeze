# Ubuntu 22.04 后台限流实验: 运行与配置手册

本手册对应 `cf/linux/`、`run_linux.py`、`scripts/ubuntu/` 与 `configs/ubuntu_*.json`。
需求相对 Android 版的变化:

| | Android 版 (`run_experiment.py`) | Ubuntu 版 (`run_linux.py`) |
|---|---|---|
| 实验环境 | Android 模拟器 (AVD) | **Ubuntu 22.04 虚拟机**, 跑在同一台 MacBook (Apple M4, 16 GB) 上 |
| 窗口切走后 | 冻结进程 (`cgroup.freeze`) + 压缩换出 (`memory.reclaim` → zram) | **不冻结**: 把它的 cgroup 限到 **1 个逻辑核心** (`cpu.max`), 预算外的应用再加 **`memory.high` = mem_frac × 前台 PSS**、可选 **swap 上限**; 进程继续跑、窗口和状态全部保留 |
| 不能发生的事 | lmkd 杀后台 (COLD 启动) | 任何进程 / 窗口消失、OOM kill (`state_lost` 必须为 0) |
| 应用集合 | 国产手机 App | 下载: 百度网盘、浏览器、腾讯视频、哔哩哔哩; 通知推送: 微信、QQ、钉钉; 计算处理: WPS Office、剪映、中望CAD、悟空图像、小艺 |
| 恢复时延 | `am start -W` | `xdotool windowactivate --sync` (切换时间) + `_NET_WM_PING` 往返 (事件循环重新响应的时间) |

三个档位 (tier), 每一步切换窗口后由策略决定每个后台应用落在哪一档:

| tier | 谁 | cgroup 旋钮 | 效果 |
|---|---|---|---|
| `fg` | 前台应用、`never_throttle` 里的应用 | 全部清除 (`cpu.max max`, `memory.high max`) | 不受限 |
| `bg` | 预算 S_t **之内**的后台应用 (`cpu_throttle_resident=true` 时) | `cpu.max = cpu_cores × 100ms / 100ms`, `cpu.weight 20`, 可选 `cpuset.cpus` | 最多 1 个核, 内存不动 |
| `shrunk` | 预算 S_t **之外**的后台应用 | `bg` 的全部 + `memory.high = max(mem_frac × m_fg, mem_min_mb)` + 可选 `memory.swap.max`; 立即 `memory.reclaim` 回收到位 | 多出的页被换到 zram/swap, 再分配会被内核节流; **默认不设 `memory.max`, 所以不会 OOM kill** |

`static` 策略 = 只有前台应用是 `fg`, 其余全部 `shrunk` (最激进, 不需要 η)。
`none` = 什么都不限 (基线)。`lru / landlord / hybrid / markov / belady` 与 Android 版相同, 用预算 B = η × Σ m_fg 决定哪些后台应用留在 `bg`、哪些降到 `shrunk`。

---

## 0. 从零到第一次实验: 操作顺序速查

§1–§6 是每一步的原理与细节; 这里只给顺序, 照着做一遍, 卡住再翻对应章节。

**第 1 步 在 macOS 上建虚拟机 (UTM)**

1. 装 UTM (https://mac.getutm.app 或 `brew install --cask utm`); 下载 **arm64 Desktop** 镜像 `ubuntu-22.04.x-desktop-arm64.iso`
   (cdimage.ubuntu.com/releases/22.04/release/), 不是 amd64。
2. UTM → *Create a New VM* → **Virtualize** (不要 Emulate) → Linux → 选 ISO → CPU **4 核**, 内存 **8192 MB**, 磁盘 **64 GB**;
   建好后在 VM 设置里关掉 *Display → Retina Mode*, 不开 Balloon。
3. 装系统: "最小安装", 不勾 "安装第三方软件"; 记住密码 (后面全程要 sudo)。
4. 第一次进桌面就做三件事: **登录界面右下角齿轮 → "Ubuntu on Xorg"** 再登录; 设置 → 电源: 不熄屏、不自动锁屏
   (`gsettings set org.gnome.desktop.session idle-delay 0`); 设置 → 显示: 固定分辨率, 之后不再改。
5. 客体工具与仓库 (不要在 UTM 共享目录上直接运行, clone 到本地盘):

```bash
sudo apt update && sudo apt install -y spice-vdagent git
echo $XDG_SESSION_TYPE                                 # 必须是 x11
cd ~ && git clone <仓库地址> context_freeze && cd context_freeze
git checkout cursor/ubuntu-throttle-on-unfocus-c246    # 注意分支名带 cursor/ 前缀
```

**第 2 步 依赖 + HWE 内核 (一次)**

```bash
sudo scripts/ubuntu/install_deps.sh --hwe     # xdotool x11-utils python3-xlib dbus-x11 libnotify-bin numpy ... + 6.x 内核 (memory.reclaim)
sudo reboot                                   # 重新登录仍选 Ubuntu on Xorg
scripts/ubuntu/check_env.sh                   # 一页体检: 会话类型 / 工具 / cgroup v2 / zram / snap 浏览器
```

**第 3 步 每次开机 (每个终端)**

```bash
cd ~/context_freeze
sudo scripts/ubuntu/setup_cgroups.sh          # /sys/fs/cgroup/cf + 控制器 + chown + 把当前 shell 移进 /cf/host
sudo scripts/ubuntu/setup_zram.sh 4096        # 4 GB zstd zram
cat /proc/self/cgroup                         # 应为 0::/cf/host
python3 run_linux.py configs/ubuntu_cn_desktop.json --probe     # cgroup writable=True runner_inside=True
```

其它终端 (含 tmux 每个 pane) 用 `source scripts/ubuntu/cf_enter.sh` 进入 /cf/host, 否则它启动的应用进不了 `/cf/<app>`。

**第 4 步 不装应用先冒烟**

```bash
python3 run_linux.py configs/ubuntu_placeholders.json --policy static --T 12 --out-dir results/smoke
```

结尾应是 `COLD 0`、`state lost events 0`、后台合计 ≈ 1 核; 通过说明虚拟机 / cgroup / X11 都对了。

**第 5 步 装应用: 桌面版优先**

```bash
scripts/ubuntu/install_cn_apps.sh --list      # 每个应用的下载页 / 架构 / 包名 / 手机版 fallback
# 在虚拟机的浏览器里把各家 arm64 deb 下到 ~/Downloads (微信 / QQ / 钉钉 / WPS / 百度网盘 / 腾讯视频 / 哔哩哔哩 / 中望CAD / 悟空图像)
sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads     # 自动跳过 x86_64-only 的包 (Chrome / Edge 在 arm64 装不上)
# Firefox 用 Mozilla APT 仓库的 deb, Chromium 用 ppa:xtradeb/apps 的 deb; 不要用默认 snap (进程逃出 cgroup)
python3 run_linux.py configs/ubuntu_cn_desktop.json --catalog   # 哪些已装, 哪些会退到手机版
```

没有桌面版的 (剪映、小艺) → 同一应用的手机版, 跑在 Waydroid 里 (§4.2):

```bash
sudo scripts/ubuntu/setup_waydroid.sh --install    # 一次: 仓库 + binder 模块 + waydroid init (~1 GB) + sudoers 规则
scripts/ubuntu/setup_waydroid.sh                   # 每次登录: 嵌套 weston 窗口 + 会话 + adb connect
scripts/ubuntu/install_mobile_apps.sh --missing    # 桌面版缺失的那些, 自动下载手机版 APK 装进去
python3 run_linux.py configs/ubuntu_cn_desktop.json --mobile-status
```

然后把 `configs/ubuntu_cn_desktop.json` 的 `"sudo_adopt": false` 改成 `true` (Android 进程属于别的 uid, 移进 cgroup 要 root)。
小艺从华为手机拉: `scripts/ubuntu/install_mobile_apps.sh --from-phone <序列号> com.huawei.vassistant com.huawei.hwid`。

**第 6 步 首次登录、核对窗口、跑实验**

```bash
source scripts/ubuntu/cf_enter.sh
scripts/ubuntu/cf_launch.sh wechat qq dingtalk wps baidunetdisk    # 逐个扫码 / 登录 / 关新手引导; 它们保持运行, 后面直接复用
python3 run_linux.py configs/ubuntu_cn_desktop.json --discover     # WM_CLASS / 命令名与目录不同的, 写进 app_overrides
python3 run_linux.py configs/ubuntu_cn_desktop.json --scenario office --policy static --T 60
scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 60      # 场景 x 策略 x η
python3 -m cf.linux.analyze results/ubuntu_matrix_*/*.jsonl
```

卡住时先看 §10: Wayland 会话、shell 不在 /cf、snap 进程逃出 cgroup、无 swap、Waydroid 的 binder / adb 都有条目。

---

## 1. 在 MacBook 上建 Ubuntu 22.04 虚拟机

| 项 | 建议 | 说明 |
|---|---|---|
| 虚拟机软件 | **UTM** (免费, Apple Virtualization 后端) / Parallels Desktop / VMware Fusion | 三者都用 Hypervisor.framework 原生跑 arm64 客体; 不要开 "模拟 x86_64" 模式 (慢 10 倍以上, 时延数据无意义) |
| 镜像 | Ubuntu 22.04.x **Desktop arm64** (`ubuntu-22.04.x-desktop-arm64.iso`, 来自 cdimage.ubuntu.com) | Server 版 + `ubuntu-desktop` 也可; 24.04 同样能跑 (cgroup/X11 相同), 本手册以 22.04 为准 |
| CPU | **4 核** | 要能看出 "后台只给 1 核" 的效果, 客体至少 4 核; M4 的大核够用 |
| 内存 | **8 GB** (16 GB 主机) | 13 个桌面应用 (微信/QQ/钉钉都是 Electron 或 Qt, 各 300–900 MB) 常驻约 6–8 GB; 6 GB 以下内核自己的回收会盖过我们的控制 (preflight 对 < 6144 MB 给 WARN)。跑矩阵时关掉 macOS 上的 IDE / 浏览器 |
| 磁盘 | ≥ 60 GB | 系统 10 GB + 应用 (微信 1 GB、WPS 1.5 GB、浏览器各 0.5 GB …) + 下载任务的缓存文件 |
| 显示 | 分辨率固定 (如 1920×1200), 关闭 "自动调整" | 窗口几何变化会干扰 `windowactivate` 的计时 |
| 会话类型 | **Xorg** (登录界面右下角齿轮 → "Ubuntu on Xorg") | 默认 Wayland 下 `xdotool` 不能切换原生 Wayland 窗口, preflight 直接拒绝 |
| 客体工具 | UTM: `apt install spice-vdagent`; Parallels: Parallels Tools | 剪贴板 / 共享目录, 方便把 .deb 拷进去 |
| 休眠 / 锁屏 | 设置 → 电源: 不休眠, 不自动锁屏; `gsettings set org.gnome.desktop.session idle-delay 0` | 锁屏后 X 服务器的焦点切换全部失败 |

UTM 的最小步骤: *Create a New VM → Virtualize → Linux → 选 ISO →* CPU 4, 内存 8192 MB, 磁盘 64 GB → 装完后在
UTM 设置里勾 *Display → Retina Mode* 关掉 (DPI 缩放不要变)。装系统时选 "最小安装", 不要勾 "安装第三方软件"。

把本仓库放进虚拟机 (`git clone`, 或 UTM 共享目录 → `cp -r` 到 `~/context_freeze`; **不要直接在共享目录上运行**,
9p/virtiofs 上 `results/` 的频繁小写入很慢)。

## 2. 安装依赖

```bash
cd ~/context_freeze
sudo scripts/ubuntu/install_deps.sh            # xdotool x11-utils python3-xlib dbus-x11 libnotify-bin numpy matplotlib ...
sudo scripts/ubuntu/install_deps.sh --hwe      # 22.04 原始内核 5.15 没有 memory.reclaim; 装 HWE 内核 (6.x) 后重启
sudo scripts/ubuntu/setup_waydroid.sh --install  # 只在有应用没有 Ubuntu 桌面版时需要 (剪映 / 小艺): Android 运行时, 见 §4.2
sudo scripts/ubuntu/install_deps.sh --subs     # (可选, 默认不会用到) 开源替代品 kdenlive / librecad / gimp, 仅当配置里 substitute_kinds 含 "oss"
scripts/ubuntu/check_env.sh                    # 一页式体检: 会话类型 / 工具 / cgroup v2 / zram / snap 浏览器
```

`memory.reclaim` 不是必需的: 没有它时 `shrunk` 应用的内存只在它下次分配时被内核按 `memory.high` 慢慢压下去 (惰性);
有它时 `set_tier` 立刻把多出的部分回收掉, 后续切回时的页入成本才稳定可测。preflight 只给 WARN。

## 3. 每次开机: cgroup 委托 + zram

```bash
sudo scripts/ubuntu/setup_cgroups.sh      # 建 /sys/fs/cgroup/cf, 开 cpu cpuset memory io 控制器, chown 给你, 把 *当前 shell* 移进 /cf/host
sudo scripts/ubuntu/setup_zram.sh 4096    # 4 GB zstd zram, 优先级高于 /swapfile (没有 swap 时 memory.high 只能丢文件页, 见 §10)
```

为什么 shell 必须搬家: cgroup v2 允许普通用户移动进程的条件是 **同时能写目标 `cgroup.procs` 和源/目标最近公共祖先的
`cgroup.procs`**。实验程序从 `/cf/host` 里的 shell 启动应用, 公共祖先是 `/cf` (归你所有), 所以能把子进程放进
`/cf/<app>`; 从普通终端 (`user.slice`) 启动则公共祖先是根, 没有权限。因此:

* **每个想跑实验的终端** 都要先 `sudo scripts/ubuntu/setup_cgroups.sh` 或 `source scripts/ubuntu/cf_enter.sh`
  (后者显式传 `--pid $$`, 在 tmux / VS Code 终端里更可靠);
* 树不持久, 重启后重做;
* 之后不再需要 root: 建叶子、移进程、写 `cpu.max` / `memory.high` / `memory.reclaim` 全是你自己的权限。

验证: `cat /proc/self/cgroup` 应为 `0::/cf/host`; `python3 run_linux.py configs/ubuntu_cn_desktop.json --probe` 里
`cgroup ... writable=True runner_inside=True controllers=['cpuset','cpu','io','memory']`。

## 4. 安装应用

规则 (与需求一致): **每个应用优先用它的 Ubuntu 桌面版**; 确实没有桌面版 (剪映、小艺), 或这个架构拿不到 deb 时,
**用同一个应用的手机版** (Android APK), 跑在同一台 Ubuntu 桌面上的 Waydroid 里 (§4.2), 受同样的 cgroup 限流。
开源 "长得像" 的程序 (kdenlive / librecad / gimp) 和内置占位程序 **默认不会被用上**, 只有配置里 `substitute_kinds`
显式写了 `"oss"` / `"placeholder"` 才允许。

```bash
scripts/ubuntu/install_cn_apps.sh --list       # 每个桌面版: 下载页 / 架构 / deb 包名 / 备注 / 它的手机版 fallback
sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads     # apt install ./*.deb (自动跳过架构不符的包)
python3 run_linux.py configs/ubuntu_cn_desktop.json --catalog   # 整个目录: kind / 是否已装 / 没装时会退到哪一条
scripts/ubuntu/install_mobile_apps.sh --list   # 按配置里的 apps 逐个列: 桌面版状态 / 手机版状态 / 实际会跑哪一个
```

### 4.1 桌面版 (第一优先)

| id | 应用 | 类别 | Ubuntu 桌面版 | arm64 | 没有时 → 手机版 (Waydroid) |
|---|---|---|---|---|---|
| `baidunetdisk` | 百度网盘 | 下载 | deb (pan.baidu.com/download#linux) | 统信/麒麟商店有 arm64 包 | `com.baidu.netdisk` |
| `firefox` | Firefox | 下载 | **Mozilla APT 仓库的 deb** | 是 | – (浏览器总有桌面版; **不要用 22.04 默认 snap**, 见 §10) |
| `chrome` / `edge` | Chrome / Edge | 下载 | deb | **仅 x86_64** | – (arm64 虚拟机用 `chromium`: ppa:xtradeb/apps 的 deb) |
| `tencent_video` | 腾讯视频 | 下载 | deb (v.qq.com/download.html) | 是 | `com.tencent.qqlive` |
| `bilibili` | 哔哩哔哩 | 下载 | deb (github.com/msojocs/bilibili-linux; 统信商店有官方版) | 是 | `tv.danmaku.bili` |
| `wechat` | 微信 | 推送 | 官方 deb (linux.weixin.qq.com) | 是 | `com.tencent.mm` |
| `qq` | QQ | 推送 | 官方 deb (im.qq.com/linuxqq) | 是 | `com.tencent.mobileqq` |
| `dingtalk` | 钉钉 | 推送 | deb (page.dingtalk.com) | 是 | `com.alibaba.android.rimet` |
| `wps` | WPS Office | 计算 | deb (linux.wps.cn) | 是 | `cn.wps.moffice_eng` |
| `jianying` | 剪映 | 计算 | **无** (剪映专业版只有 Windows / macOS) | – | **`com.lemon.lv` (剪映手机版)**; 导出视频 = 后台计算负载 |
| `zwcad` | 中望CAD | 计算 | deb / run (zwsoft.cn, 国产平台版) | 是 | 目录里没预置手机版包名 (中望 CAD 派客云图), `install_mobile_apps.sh --search 中望` 查到后按 §4.3 加 `zwcad_mobile` |
| `photosir` | 悟空图像 | 计算 | deb (photosir.com) | 是 | 同上, `--search 悟空图像` |
| `xiaoyi` | 小艺 | 计算 | **无** (鸿蒙 PC 自带) | – | **`com.huawei.vassistant`**: 华为系统应用, 应用市场无独立 APK, 需从华为手机 `--from-phone` 拉取并同时装 HMS Core (`com.huawei.hwid`); Waydroid 里通常只能跑起界面, 语音 / 云端能力未必可用。还是装不上时, 只有把 `"placeholder"` 加进 `substitute_kinds` 才会退到 `xiaoyi_sim` |

每个桌面版都记在 `cf/linux/apps.py` (`kind="desktop"`), 它的手机版是 `<id>_mobile` (`kind="mobile"`), 由 `substitute` 串成链
`jianying -> jianying_mobile (-> kdenlive)`。`resolve()` 只沿着 `substitute_kinds` 允许的 kind 往下走 (默认 `["mobile"]`)。
运行时逐条打印 `WARN 剪映 (jianying): no Ubuntu desktop build installed - using 剪映(手机版) (jianying_mobile, mobile) instead`,
记录在结果文件 `meta.substitutions` / `meta.kinds` 与汇总列 `substitutions` 里; 场景轨迹里的 id 也随之替换
(`remap_scenario_apps`), 所以 `design` 场景照常以 CAD 为枢纽。两边都没有的应用 WARN 后剔除 (`dropped`)。

### 4.2 手机版 (第二优先): Waydroid

为什么是 Waydroid 而不是 Android 模拟器: Waydroid 是跑在 **宿主内核** 上的 LXC 容器 (LineageOS 用户空间), 里面每个 Android
应用进程在宿主 `/proc` 里都看得见 (cmdline 就是包名 `com.lemon.lv` / `com.lemon.lv:push`), 所以能被移进 `/sys/fs/cgroup/cf/<app>`,
吃到与桌面应用 **完全相同** 的 `cpu.max` / `memory.high` / swap 限制; arm64 APK 在 arm64 虚拟机里原生运行, 没有二次虚拟化。
嵌套虚拟化的模拟器则把进程全藏在客体里, 没法按应用限流。

```bash
sudo scripts/ubuntu/setup_waydroid.sh --install   # 一次: repo.waydro.id + waydroid + linux-modules-extra (binder), waydroid init (下载 ~1 GB 镜像),
                                                  #       单窗口模式, 写 /etc/sudoers.d/cf-adopt (让 runner 能把 Android 进程移进 /cf/<app>)
scripts/ubuntu/setup_waydroid.sh                  # 每次登录: 起一个嵌套 weston (它是 Xorg 里的普通 X11 窗口) -> waydroid session -> adb connect
scripts/ubuntu/install_mobile_apps.sh --missing   # 配置里桌面版缺失的那些应用, 自动下载手机版 APK (应用宝 / 酷安, 自动选 ABI) 并安装
scripts/ubuntu/install_mobile_apps.sh com.lemon.lv                                   # 或指定包名
scripts/ubuntu/install_mobile_apps.sh --from-phone <华为手机 adb 序列号> com.huawei.vassistant com.huawei.hwid   # 小艺
python3 run_linux.py configs/ubuntu_cn_desktop.json --mobile-status                  # 会话 / adb / ABI / 每个手机版是否已装 / 宿主 pid
```

然后像桌面应用一样首次启动一遍 (`run_linux.py ... --launch jianying_mobile`), 在 Android 窗口里接受协议、登录。

手机版在实验里的差别 (都已在 `cf/linux/mobile.py` / `device.py` 里处理, 这里只说明语义):

| | 桌面版 | 手机版 (Waydroid) |
|---|---|---|
| 窗口 | 应用自己的 X11 窗口 | **所有 Android 应用共用一个 X11 窗口** (嵌套 weston, WM_CLASS `weston`)。切到手机版应用 = 激活这个窗口 + 在 Android 里 `am start -W` 它 |
| 切换时延 `total_time_ms` | activate + `_NET_WM_PING` | activate + Android 报告的 **TotalTime** (与本仓库 Android 版同一个数); `launch_state` 用 Android 自己的 HOT / WARM / COLD; 没有 ping |
| 进程 | runner 启动时就放进 `/cf/<app>` | zygote 把它们 fork 进容器自己的 cgroup, 每次启动后和每次采样时 **重新收养** 进 `/cf/<app>`; 这需要 root → 配置 `"sudo_adopt": true` (`setup_waydroid.sh --install` 已写 sudoers 免密)。收养不了的进程照样被测量, 但列在 `stray_pids` 里, 限制碰不到它们 |
| 通知 | D-Bus `Notify` | Android 的 `dumpsys notification` 里该包名出现的不同通知 key 数 (结果里的键 `mobile:<包名>`) |
| 后台负载 | 下载任务 / 推送 / 渲染 | 相同: 百度网盘手机版开一个下载; 剪映手机版导出一段视频; 微信手机版扫码登录后正常收推送 (走微信长连接, 不依赖 GMS) |
| 状态丢失 | 进程 / 窗口消失 | 相同判据; 额外注意 Android 自己的 LMK 也可能杀后台进程 — 那同样记为 `state_lost`, 是手机版的真实代价。可以在 Waydroid 里 `setprop ro.lmk.kill_timeout_ms 0` 等参数或给虚拟机更多内存 |
| 配置 | – | `"mobile": {"backend": "waydroid", "adb_serial": "192.168.240.112:5555", "wm_class": "^weston"}` (都有默认值; IP 以 `waydroid status` 为准) |

x86_64 虚拟机 (非 Apple 芯片) 上的 arm64-only APK 需要 Waydroid 的 libhoudini / libndk 转译镜像 (`waydroid init -f -s VANILLA` 后按
官方文档安装 `waydroid_script`); 本手册的目标机器是 arm64, 不涉及。

### 4.3 在 `app_overrides` 里补一个手机版

目录里没有预置包名的应用 (中望CAD、悟空图像) 或厂商改了包名时:

```json
"app_overrides": {
  "zwcad_mobile": {"name": "中望CAD(手机版)", "category": "compute", "kind": "mobile",
                   "package": "<install_mobile_apps.sh --search 查到的包名>",
                   "match": "^<包名>(:|$)", "wm_class": "^weston", "cmd": ""},
  "zwcad": {"substitute": "zwcad_mobile"}
}
```

### 4.4 允许开源替代 / 占位 (可选)

```json
"substitute_kinds": ["mobile", "oss", "placeholder"]
```

顺序即链上的顺序: 桌面版 → 手机版 → 开源替代 (kdenlive / librecad / gimp, `install_deps.sh --subs`) → 占位程序。不写时只到手机版为止。

### 4.5 首次启动、登录、核对窗口

```bash
source scripts/ubuntu/cf_enter.sh
scripts/ubuntu/cf_launch.sh wechat qq dingtalk wps baidunetdisk    # = run_linux.py --launch: 启动到 /cf/<id> 并退出
#   -> 逐个接受协议、扫码登录、关掉新手引导; 它们保持运行, 后面的实验直接复用 (warmup 对已运行的应用只切窗口)
python3 run_linux.py configs/ubuntu_cn_desktop.json --discover    # 屏幕上的窗口: wid / pid / WM_CLASS / 标题 / cgroup / 命令
```

`--discover` 每行列出 WID / PID / WM_CLASS / 标题 / 进程所在的 cgroup 叶子 (`CGROUP`, 在 `/cf` 之外时为 `-`) / 匹配到的
catalogue id (`APP`) / 命令行。常见修正:

* 命令名 / WM_CLASS 与 catalogue 不同 (厂商改版) → 在 `app_overrides` 里改 `cmd` / `match` / `wm_class` / `name_rx`;
* 进程在 cgroup 外 (`stray`) → runner 默认 `adopt_strays=true` 会把匹配 `match` 的进程移进来; snap / D-Bus 激活的进程移不动时
  见 §10 (`sudo_adopt`);
* 微信等每次启动都要扫码的应用: 不要让 runner 启动它, 先 `cf_launch.sh` 登好。

## 5. 配置文件参考 (`configs/ubuntu_cn_desktop.json`)

| 键 | 默认 | 含义 |
|---|---|---|
| `apps` | 13 个 id | 实验集合 (桌面版 id); 没装的沿 `substitute` 链找 `substitute_kinds` 允许的替代, 都没有则 WARN 并剔除 (少于 2 个时报错) |
| `substitute_kinds` | `["mobile"]` | 桌面版缺失时允许退到哪些 kind: `mobile` (同一应用的手机版, Waydroid) / `oss` (kdenlive 等) / `placeholder` (占位程序) |
| `mobile` | `{}` | Android 运行时: `backend` (`waydroid` / `adb`), `adb_serial` (默认 `waydroid status` 的 IP:5555), `wm_class` (Android 窗口的 WM_CLASS 正则), `wayland_display` |
| `app_overrides` | `{}` | 对 catalogue 条目的字段覆盖, 或新增应用 (键 = 新 id, 至少 `cmd` `match` `wm_class` `category`, 可加 `name` `notify_names` `arch` `kind` `package` `substitute`) |
| `never_throttle` | `[]` | 永远 `fg` 的应用 (音乐播放器、录屏) |
| `policy` / `eta` / `policy_kw` | `static` / 0.3 | 策略与预算系数, 同 Android 版; `static` `none` 忽略 η |
| `throttle.cpu_mode` | `quota` | `quota`: 每个后台应用各自 `cpu.max` = `cpu_cores` 个核的带宽; `cpuset`: 所有后台应用挤在 `cpuset_cpus` 这 (几) 个核上 (`last` = 编号最大的核); `both`; `none` |
| `throttle.cpu_cores` | 1.0 | 可以是 0.5 (半个核) |
| `throttle.cpu_weight` | 20 | 后台与前台争同一核时的权重 (前台 100) |
| `throttle.mem_frac` | 0.5 | `shrunk` 应用 `memory.high` = mem_frac × m_fg (m_fg = warmup 时前台 PSS+Swap) |
| `throttle.mem_min_mb` | 192 | `memory.high` 下限; 低于它 Electron 应用会抖动 (每次切回都要重新读几百 MB) |
| `throttle.mem_hard` | false | true 时再设 `memory.max = mem_hard_factor × memory.high` → **有 OOM kill 风险**, 仅用于对照 |
| `throttle.swap_max_mb` | null | 单个 `shrunk` 应用的 `memory.swap.max`; `memory.high` + 很小的 swap 上限 = 应用几乎不能再分配内存, 只会变慢, 仍不会死 |
| `throttle.io_weight` | null | 后台 `io.weight` (需要 io 控制器, 对下载类应用有用) |
| `throttle.proactive_reclaim` | true | 降档时立即写 `memory.reclaim` (内核 ≥ 5.19) |
| `cpu_throttle_resident` | true | 预算内的后台应用也限 CPU (`bg`); false 则预算内的完全不受限, 策略只决定内存 |
| `trace` | scenario office T=60 | `kind`: `scenario` (`office` `design` `download` `chat`) / `day` (四场景依次经历) / `zipf` / `markov`; 也可内联 `{"apps":{id:权重},"hub":id,"flows":[[...]]}` |
| `dwell_s` / `settle_s` / `warmup_dwell_s` | 8 / 2 / 10 | 每步停留 / 切换后等待 / warmup 每应用停留 (秒) |
| `minimize_background` | false | 切走的窗口是否最小化 (真实用户不会, 默认不做) |
| `notifications` | true | 用 `dbus-monitor` 统计推送类应用的 `Notify` 调用 |
| `adopt_strays` / `sudo_adopt` | true / false | 把 cgroup 外的同名进程移进来; 用户权限移不动时是否 `sudo scripts/ubuntu/cf_adopt.sh` (需 sudoers 免密, 见脚本头)。**有手机版应用时必须 `sudo_adopt: true`** (Android 进程属于其它 uid) |
| `launch_timeout_s` / `ping_timeout_s` | 90 / 15 | 等窗口出现 / 等 `_NET_WM_PING` 回应 |
| `min_ram_mb` | 6144 | 低于此 WARN |

命令行可以覆盖最常改的项: `--policy --eta --T --scenario --seed --cpu-mode --cpu-cores --cpuset --mem-frac
--swap-max-mb --mem-hard --no-resident-cpu --name --out-dir`。

**关于 η 与预算的语义** (与 Android 版的差别): 一个 `shrunk` 应用在预算模型里占 `memory.high` 那么多 (ρ_i = memory.high / m),
所以 B = η × Σ m_fg 必须大于 Σ(后台应用的 memory.high) 才会有应用留在 `bg`。13 个应用、`mem_min_mb=192` 时这个底约 2.4 GB,
η < 0.3 基本等价于 `static`。想比较策略之间的差别用 η ∈ {0.4, 0.5, 0.6} 或把 `mem_min_mb` 降到 96。

## 6. 运行

```bash
source scripts/ubuntu/cf_enter.sh                                      # 每个新终端
python3 run_linux.py configs/ubuntu_cn_desktop.json --probe            # 环境 + 每个应用是否可用 (退出码 0 才往下)

# 6.1 不装任何应用的冒烟测试: 4 个内置占位程序 (下载 / 推送 / 多进程计算 / 小艺), 约 2 分钟
python3 run_linux.py configs/ubuntu_placeholders.json --policy static --T 12 --out-dir results/smoke

# 6.2 单次实验 (控制台同时写到 results/<name>.log)
python3 run_linux.py configs/ubuntu_cn_desktop.json --scenario office --policy static --T 60
python3 run_linux.py configs/ubuntu_cn_desktop.json --scenario office --policy landlord --eta 0.5 --T 60
python3 run_linux.py configs/ubuntu_cn_desktop.json --scenario design --policy static --cpu-cores 0.5 --mem-frac 0.3

# 6.3 矩阵: 场景 x 策略 x η x 种子 (+ 限流档位变体), 同一目录, 同一预算分母 (第一格的 m_fg 复用到所有格)
scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 60
PROFILES="1c:0.5 1c:0.3 0.5c:0.5" POLICIES="none static" scripts/ubuntu/run_matrix.sh     # 只比限流强度
QUICK=1 scripts/ubuntu/run_matrix.sh                                                     # office, none+static, T=15

# 6.4 断点续跑 (Ctrl+C / X 连接断 / 虚拟机挂起后): 轨迹、策略状态、各应用档位全部恢复并重新写回 cgroup
python3 run_linux.py configs/ubuntu_cn_desktop.json --resume results/landlord_office_eta0.5_s1.jsonl
OUT=results/ubuntu_matrix_20260101-120000 scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 60   # 跳过已完成的格

# 6.5 收尾: 清掉 /sys/fs/cgroup/cf 下所有限制 (正常结束会自动做; 异常退出后手动)
python3 run_linux.py configs/ubuntu_cn_desktop.json --release
```

一次实验的流程: preflight → 解析替代 → warmup (逐个拉到前台, 停留 `warmup_dwell_s`, 记 m_fg 与冷启动时延)
→ B = η Σ m_fg → 对轨迹每一步: 把请求的应用设为 `fg` 并激活窗口 (记 activate / ping 时延, HOT / **WARM** (从 `shrunk` 回来)
/ COLD (进程没了) / FRONT (已在前台)) → 等 `settle_s` 采样 → 策略决定 S_t → 其余应用各自落到 `bg` / `shrunk`
→ 停留 `dwell_s` 再采样 → 比较两次采样: 后台各应用拿到的 CPU、被节流的时间、写盘字节、通知条数、PSS / swap、
`memory.events`, 以及 **是否有进程 / 窗口消失或 OOM kill** → 写一条 `step` 记录 + 检查点。

### 6.1 日常使用: `focusd`

实验 runner 自己驱动窗口切换; 想在平时使用虚拟机时也享受 "切走就限流", 开一个守护进程:

```bash
source scripts/ubuntu/cf_enter.sh
scripts/ubuntu/cf_launch.sh wechat qq wps firefox         # 应用必须在 /cf/<id> 里 (或之后用 run_linux.py --launch 启动)
python3 -m cf.linux.focusd --config configs/ubuntu_cn_desktop.json --delay 3            # static: 只有焦点窗口的应用自由
python3 -m cf.linux.focusd --config configs/ubuntu_cn_desktop.json --policy lru --eta 0.5 --delay 3 --log results/focusd.jsonl
python3 -m cf.linux.focusd ... --dry-run                   # 只打印决策不碰 cgroup
```

它监听根窗口的 `_NET_ACTIVE_WINDOW`, 把焦点窗口的 pid 映射到 `/cf/<app>`, 对焦点应用清限制, 对其它应用在 `--delay` 秒
宽限期后按策略降档 (快速 alt-tab 来回不触发); 焦点落在不受管的窗口 (终端、文件管理器) 时所有受管应用都算后台;
Ctrl+C / SIGTERM 时解除全部限制。启动时也会先清一遍残留限制。

## 7. 结果文件与指标

`results/<name>.jsonl` 每行一条记录: `meta` (配置、主机能力、应用类别、`kinds` (desktop / mobile …)、替代关系、限流档位) → `warmup` × N → `budget`
→ `trace` → `step` × T → (`interrupted` / `resume`)* → `end`。`step` 记录保留 Android 版的全部键 (`frozen`≡`throttled`,
`compressed`≡`shrunk`, `was_compressed`≡`was_shrunk`, `M_bg_kb`, `budget_violation`, `after_launch` / `after_dwell` 采样 …),
`cf.analyze` 原样可用; 新增 `tiers`, `bg` (每应用的 Δcpu / Δthrottled / Δwrite / 通知 / cg 内存), `state_lost`, `notifications`。

```bash
python3 -m cf.linux.analyze results/ubuntu_matrix/*.jsonl                       # 表格
python3 -m cf.linux.analyze results/ubuntu_matrix/*.jsonl --csv s.csv --agg agg.csv --pareto p.csv --plot pareto.png
```

| 列 | 含义 | 希望 |
|---|---|---|
| `lat_p50/p95/p99_ms` | 切换总时延 = activate + ping (COLD 再加启动); 手机版应用 = activate + Android `am start -W` 的 TotalTime | 低 |
| `activate_p50_ms` | 窗口管理器完成切换的时间 | 基本恒定 (30–70 ms) |
| `ping_resident_p50_ms` / `ping_shrunk_p50/p95_ms` | 从 `bg` / 从 `shrunk` 回来后应用事件循环回应 `_NET_WM_PING` 的时间 | shrunk 的值就是 "被压缩的代价" |
| `n_hot` / `n_warm` / `n_cold` / `n_front` / `n_timeout` | 切换类型计数 | **`n_cold` 应为 0** (没有重启) |
| `bg_pss_avg_mb` / `budget_mb` / `budget_violations` / `bg_pss_avg_over_sum_fg` | 后台实际占用 vs 预算; 达成的 η | |
| `bg_cores_avg` / `bg_throttled_pct` | 后台应用合计拿到的核数 / 被配额节流的时间占比 | 1 核配额 × k 个后台 ≈ ≤ k |
| `download_mb` / `download_mbps` | 下载类应用在后台写盘的量与速率 | 限流后仍 > 0 (下载在继续, 只是慢) |
| `notif_total` / `notif_per_h` | 推送类应用在后台发出的通知 | 限流后仍 > 0 |
| `compute_cpu_s` | 计算类应用在后台得到的 CPU 秒 | |
| `state_lost_events` / `oom_kills` | 进程 / 窗口消失、OOM kill | **必须为 0** |
| `swap_write_mb` / `swap_read_mb` / `refaults` | zram 换出 / 换入 | 换入 = WARM 切换的代价 |
| `psi_cpu_some_ms` | 全系统 CPU 压力 | 前台卡顿的旁证 |
| `cpu` / `mem_frac` / `cpu_throttle_resident` / `substitutions` | 本格的限流档位与替代关系 (桌面版缺失时用了谁的手机版) | 用来分组 |

## 8. 验证 "不丢状态和上下文"

1. **自动**: 每一步比较 dwell 前后: 存活 ≥ 5 s 的 pid 是否消失、窗口是否消失、`memory.events` 的 `oom_kill` 是否增加
   → `state_lost`; 控制台出现 `STATE LOST:` 并在结尾 WARN。`n_cold` > 0 也说明有应用重启过。
2. **占位程序**: `python3 -m cf.linux.placeholder_app --app sim_compute --workers 3 --mem 400` 的窗口与状态文件
   `/tmp/cf_placeholder_<app>.json` 里有单调递增的 `counter`、`worker_counter`、`bytes_written`、`notifications`。
   跑完一轮后 `counter` 只增不减、`pid` 不变 = 进程没重启; 在 `shrunk` 期间 `counter` 增速下降但不为 0 = 还在干活。
3. **真实应用**: 在 WPS 里打半行字不保存、在浏览器里把页面滚到中间、微信里打开一个聊天窗口输入未发送文字,
   跑完实验回去看: 光标、滚动位置、草稿都应原样 (冻结版本也能做到这点, 限流版本额外保证后台任务继续推进:
   下载进度条在动, 微信消息在实验中照常到达并弹通知)。
4. 对照: `--mem-hard` 打开 `memory.max`, 看 `oom_kills` 变成非 0 时 `state_lost_events` 如何跟着出现 — 这就是我们默认不设硬上限的原因。

## 9. 与 Android 版 / 文档模型的对应

| 文档模型 | Android 版实现 | Ubuntu 版实现 |
|---|---|---|
| m_i (工作集) | PSS + SwapPss (uid 下所有进程) | PSS + SwapPss (`/cf/<app>` 下所有进程, `smaps_rollup`) |
| 压缩后占用 ρ·a_i | zram 压缩比 × anon | `memory.high` (内核把多出的页换到 zram, 剩下的留在内存); ρ_i = memory.high / m |
| 冻结 / 换出 | `cgroup.freeze=1` + `memory.reclaim` | tier `bg` (`cpu.max`) / tier `shrunk` (+`memory.high`, `memory.reclaim`) |
| 恢复 c_resume | `am start -W` 的 TotalTime 与 HOT 的差 | WARM 切换的 (activate + ping) 与 HOT 的差; 手机版应用用 activate + `am start -W` TotalTime; 没有样本时按 (m − memory.high) / 1.2 GB/s 估算 |
| 预算 B | η × Σ m_fg (后台) | 同 |
| 不能发生 | lmkd 杀 → COLD | 进程 / 窗口消失、OOM kill → `state_lost`; `n_cold` |

## 10. 常见问题

**preflight: `Wayland session`** — 注销, 登录界面齿轮选 "Ubuntu on Xorg"。`echo $XDG_SESSION_TYPE` 应为 `x11`。

**`this process is in cgroup '0::/user.slice/...', not under /cf/`** — 这个终端没做 §3; `source scripts/ubuntu/cf_enter.sh`。
tmux 里要在 **每个 pane** 做。

**应用的进程不在它的 cgroup 里 (`stray` / `adopted` / `cannot place pid`)** —
(a) snap 包: snapd 把每个 snap 应用放进自己的 systemd scope, 用户移不动 → 用 deb 版 (Firefox: Mozilla APT 仓库; Chromium:
ppa:xtradeb/apps); (b) 通过 D-Bus 激活或桌面图标启动的应用在 `user.slice` 里 → 用 `cf_launch.sh` 启动, 或配置
`sudo_adopt=true` + sudoers 免密 (见 `scripts/ubuntu/cf_adopt.sh` 头部); (c) 应用自己 fork 出的 daemon 一般会继承 cgroup, 没问题。

**没有 swap: `memory.high can only drop file pages`** — 匿名页没地方去, 内核只能在应用每次分配时让它停下来等
(allocation throttling): 应用更慢、`notify-send` 之类的子进程启动也可能被卡住几秒, 但依旧不会被杀。做 `sudo scripts/ubuntu/setup_zram.sh 4096`。

**`memory.high` 太低, 应用抖动 (ping 几秒, `refaults` 很大)** — 提高 `mem_min_mb` (Electron 应用 ≥ 256) 或 `mem_frac`。
观察 `bg[*].high_events` (memory.events high 计数) 与 `cg_psi_mem_full_us`。

**`thr 0.0s` / `bg_throttled_pct = 0`** — 后台应用本来就用不到 1 个核 (空闲的聊天软件), 配额没有绑定, 是正常现象; 用
`--cpu-cores 0.5` 或在 design 场景 (CAD / 视频剪辑渲染) 里看。占位程序用 `--workers 3` 才能超过 1 核 (GIL)。

**通知计数一直是 0** — `dbus-monitor` 必须和应用在同一个会话总线 (`echo $DBUS_SESSION_BUS_ADDRESS` 非空; 从桌面的终端里跑);
应用的 `app_name` 与 catalogue 的 `notify_names` 不同时在 `app_overrides` 里补; 微信 / QQ 的通知要在应用设置里打开 "系统通知"。

**`activate` 失败 / `not-active`** — 窗口管理器的焦点保护拒绝了切换。GNOME 上确认 `gsettings get org.gnome.mutter
focus-new-windows` 为 `'smart'` (默认; `'strict'` 会拒绝 xdotool 的请求); 确认屏幕没有锁定、没有模态对话框 (首次启动的协议 /
更新提示) 挡在该应用前面 (`--discover` 里会多出一个 `_NET_WM_WINDOW_TYPE_DIALOG` 窗口)。

**x86_64-only 的 deb (Chrome / Edge) 在 arm64 虚拟机里** — `install_cn_apps.sh` 会跳过, `--catalog` 标 `arch` 不符,
`resolve` 剔除。浏览器类用 Firefox deb + Chromium deb 即可。

**内核 5.15 (`memory.reclaim: NO`)** — `sudo scripts/ubuntu/install_deps.sh --hwe` 后重启; 不装也能跑 (惰性回收)。

**手机版: `no Android runtime is reachable`** — `scripts/ubuntu/setup_waydroid.sh` 没跑或 weston 没起来 (`--status`);
`waydroid status` 的 Session 必须 RUNNING, `adb devices` 里要有 `<IP>:5555` (没有就 `waydroid prop set persist.sys.usb.config adb`
后重启会话; 或者不装 adb, runner 会退到 `sudo -n waydroid shell`, 需要 sudoers 免密)。`modprobe binder_linux` 失败 = 内核没有
binder, 装 `linux-modules-extra-$(uname -r)` 或 HWE 内核后重启。

**手机版: `Android processes ... could not be moved`** — 没开 `sudo_adopt`, 或 `/etc/sudoers.d/cf-adopt` 里的仓库路径不对
(`setup_waydroid.sh --install` 按当时的仓库位置写)。此时手机版应用会被测量但不受限 (`stray_pids` 非空)。

**手机版: 切换时 `launch_state` 全是 FRONT / `total_time_ms` 为空** — `am start -W` 认为该 Activity 已在最前 (Waydroid 单窗口里
确实如此时是正常的, 比如连续两步都是同一应用); 若所有手机版应用都这样, 检查 `persist.waydroid.multi_windows` 是否被设成了 true。

**手机版: APK 装不上 (`INSTALL_FAILED_NO_MATCHING_ABIS`)** — 下载器已按 `ro.product.cpu.abilist` 选包; 出现这个说明是 x86_64
虚拟机 + arm64-only 应用, 需要 libhoudini / libndk 转译镜像 (见 §4.2)。

**结果里 `n_cold > 0`** — 看 `results/app_logs/<id>.log` (应用 stdout/stderr) 与对应 step 的 `state_lost`: 是应用自己崩了
(腾讯视频 / 百度网盘的 Electron 渲染进程偶尔会), 还是 `mem_hard` 导致的 OOM; 后者把 `mem_hard` 关掉。

**虚拟机内存不够 (`VM RAM is 4096 MB`)** — 要么加到 8 GB, 要么减少 `apps` (去掉两个浏览器之一与一个视频应用)。

**macOS 侧** — 跑矩阵时关闭 Spotlight 索引新建的结果目录 (如果是共享目录), 关闭 Time Machine; UTM 不要开 "Balloon" 内存气球。

## 11. 测试

```bash
python3 -m pytest -q tests/test_linux_*.py         # 不需要桌面: tier 旋钮值 / 伪 cgroupfs / xprop 解析 / 伪桌面上的完整 runner (静态与预算策略、
                                                   # 无进程被杀、WARM、state_lost 注入、resume 重放档位) / focusd 决策 / 应用目录与 桌面->手机版 替代链 /
                                                   # Notify 解析 / 手机版: waydroid status、dumpsys 解析, 伪 Android 运行时上的 启动-收养-通知 与 preflight
                                                   # 有 Xvfb + xfwm4/openbox + xdotool 时自动加跑一次真实窗口 发现 -> 激活 -> _NET_WM_PING 往返
                                                   # /sys/fs/cgroup/cf 已委托给当前用户时自动加跑一次真实 cgroup 的档位读写
```
