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

1. 装 UTM (https://mac.getutm.app 或 `brew install --cask utm`)。**Ubuntu 22.04 没有官方的 arm64 Desktop ISO**
   (cdimage 上只有 amd64 桌面版和 arm64 Server 版), 两条路选一:
   * **方案 A (本手册默认)**: `ubuntu-22.04.5-live-server-arm64.iso`
     (https://cdimage.ubuntu.com/releases/22.04/release/ubuntu-22.04.5-live-server-arm64.iso,
     sha256 `eafec62c…7505`, 用 `shasum -a 256` 核对), 装完再 `sudo apt install ubuntu-desktop-minimal`。
   * **方案 B**: `ubuntu-24.04.x-desktop-arm64.iso` (https://cdimage.ubuntu.com/noble/daily-live/current/ 或
     releases/24.04/release/) 直接是桌面版; cgroup v2 / Xorg / 本仓库脚本全部一样, 只是 24.04 的默认内核已是 6.8, 不用 `--hwe`。
2. UTM → *Create a New VM* → **Virtualize** (不要 Emulate) → Linux → 选 ISO → CPU **4 核**, 内存 **8192 MB**, 磁盘 **64 GB**;
   建好后在 VM 设置里关掉 *Display → Retina Mode*, 不开 Balloon。
3. 装系统 (方案 A 是文本安装器): 不勾任何 snap, 勾 **Install OpenSSH server**; 记住密码 (后面全程要 sudo)。
   分区页选默认的 "Use an entire disk" + LVM 时, 安装器 **只给根卷分配一半磁盘** (40 GB 盘 → 20 GB 的 `ubuntu-lv`), 其余留在卷组里不用;
   在这一页把 `ubuntu-lv` 的 Size 改成最大值, 或者装完后扩 (§10 "磁盘只用了一半")。
   安装器显示完成后 **先在 UTM 里把 ISO 弹出 (CD/DVD → Clear) 再 reboot**, 否则重启又进安装器或黑屏光标闪动。
4. 方案 A 装桌面 (**先** 装 Mozilla 仓库的 Firefox deb, 否则 `ubuntu-desktop-minimal` 依赖的 firefox 过渡包会去装 snap, 在
   snap 商店连不上时 dpkg 直接报错, 见 §10):

```bash
sudo install -d -m 0755 /etc/apt/keyrings
wget -qO- https://packages.mozilla.org/apt/repo-signing-key.gpg | sudo tee /etc/apt/keyrings/packages.mozilla.org.asc >/dev/null
gpg -n -q --import --import-options import-show /etc/apt/keyrings/packages.mozilla.org.asc | grep -q 35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3 && echo key-ok
echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" | sudo tee /etc/apt/sources.list.d/mozilla.list
printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' | sudo tee /etc/apt/preferences.d/mozilla
sudo apt update && sudo apt install -y firefox
sudo apt install -y ubuntu-desktop-minimal      # ~1.5 GB; 之后 sudo systemctl set-default graphical.target && sudo reboot
```

5. 第一次进桌面就做三件事: **登录界面右下角齿轮 → "Ubuntu on Xorg"** 再登录; 设置 → 电源: 不熄屏、不自动锁屏
   (`gsettings set org.gnome.desktop.session idle-delay 0`); 设置 → 显示: 固定分辨率, 之后不再改。
   想全程只用 ssh 操作 (推荐: 复制粘贴方便, 不受 UTM 窗口影响) 见 §2.4; 需要走 macOS 上的代理 (如 Clash 7897 端口) 见 §2.5。
6. 客体工具与仓库 (不要在 UTM 共享目录上直接运行, clone 到本地盘):

```bash
sudo apt update && sudo apt install -y spice-vdagent git
echo $XDG_SESSION_TYPE                                 # 必须是 x11
cd ~ && git clone https://github.com/HymnOfLight/context_freeze.git && cd context_freeze
git checkout cursor/ubuntu-throttle-on-unfocus-c246    # 注意分支名带 cursor/ 前缀; `git remote -v` 应指向 context_freeze
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
# 一键: 在 Mac 上 (宿主机) 把各家 deb 下到 ~/Downloads/cf_debs, 拷进虚拟机并在里面安装 (微信 / QQ / 钉钉 / WPS / 哔哩哔哩;
# x86 虚拟机再加百度网盘 / Chrome / Edge). 腾讯视频 / 中望CAD / 悟空图像 没有稳定直链, --open 直接开浏览器, 手动存进同一目录,
# 再跑一遍同一条命令即可: 已下载的 (包括手动存的厂商文件名) 不会重下, 架构不符 / 损坏的包会被自动删掉
PROXY=http://127.0.0.1:7897 scripts/mac/download_cn_apps.sh --open --scp ubuntu@<虚拟机IP> --install
#   --arch amd64 给 x86 虚拟机; --dry-run 只看链接; --refresh 强制更新; --vm-repo PATH 仓库不在虚拟机 ~/context_freeze 时指定
# 在虚拟机里跑同一个脚本也行 (架构跟虚拟机, 省掉 --scp):  scripts/mac/download_cn_apps.sh --open --install
# 分步:
scripts/ubuntu/install_cn_apps.sh --list      # 每个应用的下载页 / 架构 / 包名 / 手机版 fallback
sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs     # 自动跳过 x86_64-only 的包 (Chrome / Edge 在 arm64 装不上)
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

**第 6 步 统一账号登录、核对窗口、跑实验**

```bash
cp configs/accounts.example.json configs/accounts.json             # 填上实验专用手机号 (一个号注册全部应用; git 忽略此文件)
source scripts/ubuntu/cf_enter.sh
scripts/ubuntu/cf_onboard.sh                                       # 逐个: 启动进 cgroup -> 提示登录方式并等主窗口出现 -> 提示准备动作 -> 演练一轮脚本操作
scripts/ubuntu/cf_onboard.sh --check                               # 哪些已登录 / 就绪 (results/onboard/state.json); 没就绪的 cf_onboard.sh <id> 重做
python3 run_linux.py configs/ubuntu_cn_desktop.json --discover     # WM_CLASS / 命令名与目录不同的, 写进 app_overrides
python3 run_linux.py configs/ubuntu_cn_desktop.json --scenario office --policy static --T 60
ONBOARD=1 WORK=1 PRESET=full scripts/ubuntu/run_all.sh            # 全量: 体检 → 登录向导 (就是上面两条 cf_onboard) → 快照 → 冒烟 → 全矩阵 + 脚本操作 → report.md (tmux 里跑)
PRESET=night scripts/ubuntu/run_all.sh                             # 已登录好时的过夜档: 体检 → 快照 → 冒烟 → 过夜矩阵 → report.md
```

`ONBOARD=1` 时 `run_all.sh` 先在终端里跑一遍 `cf_onboard.sh` (没 `accounts.json` 会从模板复制一份), 然后 `--check` 不全就绪就停下 (返回 2),
不会带着没登录的应用去跑十几个小时的矩阵; 确认要带着跑就 `ONBOARD=0`。

账号怎么注册、每个应用在 Linux 上怎么登录、登录后要停在什么界面, 见 §4.6; **一个账号都不想注册** 先用 `configs/ubuntu_no_login.json` (§4.7: 微信 / QQ / 钉钉 / 百度网盘 用占位顶替, 其余真跑)。应用还没装齐也能先把数据跑出来: `PLACEHOLDERS=1 PRESET=night scripts/ubuntu/run_all.sh` (没装的用占位程序顶替, id 不变; 装齐后同一命令重跑)。
卡住时先看 §10: Wayland 会话、shell 不在 /cf、snap 进程逃出 cgroup、无 swap、Waydroid 的 binder / adb 都有条目。

---

## 1. 在 MacBook 上建 Ubuntu 22.04 虚拟机

| 项 | 建议 | 说明 |
|---|---|---|
| 虚拟机软件 | **UTM** (免费, Apple Virtualization 后端) / Parallels Desktop / VMware Fusion | 三者都用 Hypervisor.framework 原生跑 arm64 客体; 不要开 "模拟 x86_64" 模式 (慢 10 倍以上, 时延数据无意义) |
| 镜像 | `ubuntu-22.04.5-live-server-arm64.iso` + `apt install ubuntu-desktop-minimal` (§0 第 1、4 步) | 22.04 **没有** 官方 arm64 桌面 ISO; 或直接用 `ubuntu-24.04.x-desktop-arm64.iso` (cgroup/X11 相同, 内核已是 6.8) |
| CPU | **4 核** | 要能看出 "后台只给 1 核" 的效果, 客体至少 4 核; M4 的大核够用 |
| 内存 | **8 GB** (16 GB 主机) | 13 个桌面应用 (微信/QQ/钉钉都是 Electron 或 Qt, 各 300–900 MB) 常驻约 6–8 GB; 6 GB 以下内核自己的回收会盖过我们的控制 (preflight 对 < 6144 MB 给 WARN)。跑矩阵时关掉 macOS 上的 IDE / 浏览器 |
| 磁盘 | ≥ 60 GB | 系统 10 GB + 应用 (微信 1 GB、WPS 1.5 GB、浏览器各 0.5 GB …) + 下载任务的缓存文件 |
| 显示 | 分辨率固定 (如 1920×1200), 关闭 "自动调整" | 窗口几何变化会干扰 `windowactivate` 的计时 |
| 会话类型 | **Xorg** (登录界面右下角齿轮 → "Ubuntu on Xorg") | 默认 Wayland 下 `xdotool` 不能切换原生 Wayland 窗口, preflight 直接拒绝 |
| 客体工具 | UTM: `apt install spice-vdagent`; Parallels: Parallels Tools | 剪贴板 / 共享目录, 方便把 .deb 拷进去 |
| 休眠 / 锁屏 | 设置 → 电源: 不休眠, 不自动锁屏; `gsettings set org.gnome.desktop.session idle-delay 0` | 锁屏后 X 服务器的焦点切换全部失败 |

UTM 的最小步骤: *Create a New VM → Virtualize → Linux → 选 ISO →* CPU 4, 内存 8192 MB, 磁盘 64 GB → 装完后在
UTM 设置里勾 *Display → Retina Mode* 关掉 (DPI 缩放不要变)。Server 安装器里勾 OpenSSH server, 不勾 snap;
安装完成后先弹出 ISO 再重启 (重启黑屏 + 光标闪动 = 还挂着 ISO 或显示输出停在串口, 强制关机 → Clear CD/DVD → 再开)。
实际内存少于 8 GB (如 UTM 只给到 5.9 GB) 也能跑, preflight 对 < `min_ram_mb` (默认 6144) 只给 WARN, 但同时常驻的应用数要减少。

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

### 2.4 只用 ssh 操作虚拟机

全部实验都可以在 ssh 里完成, 虚拟机窗口只负责显示桌面 (不用装剪贴板共享)。要点是 ssh 会话要借到桌面会话的三个环境变量:

```bash
# 一次: 让桌面自动登录到 Xorg (这样重启后不用碰 UTM 窗口)
sudo sed -i 's/^#\?WaylandEnable=.*/WaylandEnable=false/' /etc/gdm3/custom.conf
sudo sed -i "s/^#\?\s*AutomaticLoginEnable.*/AutomaticLoginEnable=true/; s/^#\?\s*AutomaticLogin\s*=.*/AutomaticLogin=$USER/" /etc/gdm3/custom.conf
grep -E 'Wayland|Automatic' /etc/gdm3/custom.conf
# 之后每个 ssh 会话 (写进 ~/.bashrc 更省事):
pid=$(pgrep -u $USER -x gnome-shell | head -1)
for v in DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE; do export "$v=$(tr '\0' '\n' </proc/$pid/environ | sed -n "s/^$v=//p")"; done
echo $DISPLAY $XDG_SESSION_TYPE          # :1 x11 (或 :0)
xdotool getactivewindow getwindowname    # 能打印出桌面上当前窗口名 = 成功
```

`scripts/ubuntu/run_all.sh` 在没有 `DISPLAY` 时会自动做这一步。从 Mac 连: `ssh user@<VM IP>` (UTM 共享网络下 IP 在
`ip -4 a` 里, 形如 192.168.64.x); 用 tmux 跑长实验 (`tmux new -s cf`), ssh 断了实验不受影响。

### 2.5 走 macOS 上的代理 (Clash 等, 端口 7897)

虚拟机 → 宿主的地址按虚拟机软件不同: UTM 共享网络是网关 (`ip route | awk '/default/{print $3}'`, 通常 192.168.64.1);
**VMware NAT 的网关 `x.x.x.2` 是 NAT 路由器, 不是宿主** — 宿主是同网段的 `.1` (例: 网关 192.168.248.2 → 宿主 192.168.248.1),
写成 `.2` 会得到 `Connection refused`; VirtualBox NAT 固定 `10.0.2.2`。Clash 要勾 **Allow LAN**。

```bash
GW=$(ip route | awk '/default/{print $3}')                 # UTM: 直接用; VMware: GW=${GW%.*}.1; VirtualBox: GW=10.0.2.2
# apt 单独配置 (sudo 默认不带环境变量)
printf 'Acquire::http::Proxy "http://%s:7897";\nAcquire::https::Proxy "http://%s:7897";\n' $GW $GW | sudo tee /etc/apt/apt.conf.d/95proxy
# 当前 shell / 全局 (重新登录生效)
echo "http_proxy=http://$GW:7897
https_proxy=http://$GW:7897
all_proxy=socks5://$GW:7897
no_proxy=localhost,127.0.0.1,192.168.0.0/16,.cn,.aliyun.com,.baidu.com,.qq.com,.weixin.qq.com,.bilibili.com,.hdslb.com,.dingtalk.com" | sudo tee -a /etc/environment
echo 'Defaults env_keep += "http_proxy https_proxy all_proxy no_proxy"' | sudo tee /etc/sudoers.d/proxy
curl -sI https://github.com | head -1          # HTTP/2 200
```

**跑下载类实验前把代理关掉** (`unset http_proxy https_proxy all_proxy` 或注释 /etc/environment 里的行再重新登录):
代理会把下载应用的流量整形成 Clash 的速率, `download_mbps` 量的就不再是 "后台被限到 1 核后还能下多快"。
`run_all.sh` 发现代理变量时会提示。`snap` 不读这些变量 (这也是 §0 第 4 步先装 Mozilla deb 的原因之一)。

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
scripts/mac/download_cn_apps.sh --open --scp ubuntu@<vm-ip> --install    # 一键: Mac 上批量下载 deb -> 拷进虚拟机 -> 安装 (见下; 可加 --arch / --only / --refresh)
scripts/ubuntu/install_cn_apps.sh --list       # 每个桌面版: 下载页 / 架构 / deb 包名 / 备注 / 它的手机版 fallback
sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs     # apt install ./*.deb (自动跳过架构不符的包)
python3 run_linux.py configs/ubuntu_cn_desktop.json --catalog   # 整个目录: kind / 是否已装 / 没装时会退到哪一条
python3 run_linux.py configs/ubuntu_cn_desktop.json --menu      # 桌面菜单里每个应用认领到的 .desktop (非 deb 安装) + 没人认领的条目
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

**arm64 虚拟机 (UTM / Apple Silicon) 能装的真机应用**比上表少: Chrome / Edge 没有 arm64 公开版, 剪映 / 小艺没有 Linux 版。
为了让 arm64 上也有足够多的真实应用, 目录里另外收了两组应用。第一组 4 个有**官方 arm64 deb** (在浏览器里手动下, 选 ARM64 + deb):

| id | 应用 | 类别 | 下载 | 登录 |
|---|---|---|---|---|
| `feishu` | 飞书 | 推送 | https://www.feishu.cn/download → Linux 客户端 → deb → ARM64 (`Feishu-linux_arm64-*.deb`) | 手机号 + 验证码, 首次建一个团队 |
| `wecom` | 企业微信 | 推送 | https://work.weixin.qq.com/ → 下载 → Linux 桌面端 → ARM deb | 手机企业微信 / 微信扫码, 首次建一个企业 |
| `wemeet` | 腾讯会议 | 推送 | https://meeting.tencent.com/download/ → Linux → ARM64 (`TencentMeeting_*_arm64_*.deb`) | 手机号 + **密码** (先在手机上设; Linux 版扫码 / 验证码常失败); 只做常驻 / 通知负载, `work` 为 none |
| `tencent_docs` | 腾讯文档 | 计算 | https://docs.qq.com/desktop/ → Linux → ARM deb (`TencentDocs-arm64.deb`) | 微信 / QQ 扫码; 新建文档停在正文 |

第二组来自**星火应用商店 arm64 仓库** (https://mirrors.sdu.edu.cn/spark-store/arm64-apm/ 的 `store/` 子目录, 或装好 Spark Store 后在商店里搜;
`.otohime` 后缀的是星火用 Wine / 容器重新打包的 Android 或 Windows 版, 其它是统信 UOS 商店的原生 Linux 版):

| id | 应用 | 类别 | 包名 (dpkg) | 登录 / 准备 (`--onboard` 会逐条提示) |
|---|---|---|---|---|
| `baidunetdisk` | 百度网盘 | 下载 | `com.baidu.baidunetdisk` (UOS 4.17.x arm64) | 手机百度网盘扫码; 挂一个大文件下载 |
| `xunlei` | 迅雷 | 下载 | `com.xunlei.download` | 手机号 + 验证码 / 扫码 (不登录不能建任务); 建一个大文件 http 下载任务停在任务列表 |
| `iqiyi` | 爱奇艺 | 下载 | `com.iqiyi.otohime` | 可不登录; 开一个免费剧集播放 |
| `douyin` | 抖音 | 下载 | `com.douyin.otohime` | 可不登录; 停在推荐流, 鼠标放视频上 |
| `netease_music` | 网易云音乐 | 下载 | `netease.cloud.music.otohime` | 必须登录 (手机号 + 验证码 / 扫码); 放一个歌单后停在歌单页 |
| `shimo` | 石墨文档 | 计算 | `shimo` | 手机号 + 验证码 / 微信扫码; 新建文档, 光标在正文 |

同一仓库里还有夸克网盘 (`quark-cloud-drive`)、中国移动云盘 (`com.cmic.mcloud`)、金山文档 (`cn.kdocs.kdesktop`)、腾讯元宝、华为云会议、
360 浏览器 (`browser360-cn-stable`)、ToDesk, 没有收进目录; 要用时按 §4.3 的方式在 `app_overrides` 里补条目。

这些包装在 `/opt/apps/<包名>/files/` 下, **什么都不放进 PATH**: 启动器只认包里的 `.desktop` 文件。程序会自动识别: `is_installed` 看
`/var/lib/dpkg/info/<包名>.list` (目录条目的 `package` 或 `alt_packages` 任一命中), `launch_cmd` 在 `cmd` 不在 PATH 上时依次改用 `exe` 和该包
`.desktop` 的 `Exec` 行 (去掉 `%U` 之类的占位, 保留 `cmd` 里的参数)。包名对不上 (商店版本改名, 如 `cn.xunlei.thunder`) 时还会在已装的 dpkg 包里找
名字成分与该应用进程正则匹配、且带 `.desktop` 的包 (`thunderbird` 不会被当成迅雷)。`install_cn_apps.sh --check` / `--catalog` 的 cmd 列显示
`thunder -> /opt/apps/com.xunlei.download/files/start.sh  [com.xunlei.download]` 就是识别成功; 窗口的 WM_CLASS 跟目录猜的不一样时
(`--discover` 看), 写 `app_overrides.<id>.wm_class`。装好的全部应用集合有现成配置 `configs/ubuntu_arm64.json` (§5)。

**不是 deb 装的也认** (flatpak / snap / AppImage / 商店自带的安卓应用包装器 - `dpkg -l` 里没有, 但应用网格里有图标): 最后一道兜底是**桌面菜单
本身** - 扫 `/usr/share/applications`、`~/.local/share/applications`、flatpak / snap 的 exports 目录和 `/opt/apps/*/entries/applications`
里所有的 `.desktop`, 按 `StartupWMClass` ↔ `wm_class`、标题 (`Name` / `Name[zh_CN]`) ↔ `name_rx` 或 id、文件名 ↔ 包名 / 进程正则、`Exec` ↔
进程正则 (都按整词 / 整段匹配, `Thunderbird` 不是 `thunder`) 认领一个条目, 它的 `Exec` 就是启动命令。`python3 run_linux.py CONFIG --menu`
列出每个应用认领到的条目 (`matched by` 列) 和**没人认领的其余条目**; 配置里的应用在 `--check` 仍是 `no` 时, 就在这份 "unclaimed" 列表里找它,
把 `Exec` 写进 `app_overrides.<id>.cmd` (`match` / `wm_class` 启动后用 `--discover` 核对)。注意这类启动器进程名未必含应用名 (例如安卓
包装器), 进程识别靠的是启动时进的 cgroup, 所以**一定要让 `cf_onboard.sh` / `cf_launch.sh` 启动它**, 不要点图标。

**批量安装** (手动下好的 deb 全放一个目录, 文件名随意):

```bash
sudo scripts/ubuntu/install_cn_apps.sh --firefox ~/Downloads/cf_debs   # Firefox deb 版 + xclip, 然后目录里每个 deb: 架构不对跳过 / 同版本已装跳过 / apt install
scripts/ubuntu/install_cn_apps.sh --check                               # 哪些被认作已安装
```

**在 Mac 上下载 (`scripts/mac/download_cn_apps.sh`)**: 厂商基本不给稳定直链, 脚本按应用各用一招——微信: 固定的 universal 地址;
QQ: im.qq.com 的 `linuxConfig.js`; 钉钉: `dingtalk.com/win/d/qd=linux_<arch>` 的跳转; WPS: amd64 用 linux.wps.cn 页面并按页面里的
`downLoad()` 给 CDN 链接签名 (`?t=&k=md5(key+path+t)`), arm64 从 365.wps.cn 的脚本包里找; 百度网盘: `pan.baidu.com/disk/cmsdata` 接口
(仅 amd64); 哔哩哔哩: GitHub releases API; Chrome / Edge: 官方直链 / 仓库目录 (仅 amd64)。腾讯视频 (页面纯脚本渲染, 无稳定直链)、
中望CAD / 悟空图像 (要注册) 打印页面地址, `--open` 直接开浏览器, 手动存成 `~/Downloads/cf_debs/<id>_<arch>.deb`; 也可以
`URL_tencent_video=... ` 强制指定任一应用的地址。只依赖 macOS 自带的 bash 3.2 + curl; 下载可续传, 校验 deb 头, 每个包的
来源 / 大小 / sha256 记在 `SOURCES.txt`。**重跑只补缺的**: 目录里已有的包 (脚本存的 `<id>_<arch>.deb`, 或手动存的厂商文件名如
`QQ_3.2.19_arm64.deb` / `wps-office_12.1_amd64.deb`, 按文件名 + deb 里的 Architecture 字段识别) 不再下载、也不再提示手动下载;
`--refresh` 才强制更新。下载前会**自动删掉目标架构装不上的文件**: 另一种架构的 deb (arm64 虚拟机里的 x86_64 包, 反之亦然)、`*.bad`
(服务器返回的网页) 和根本不是 deb 的文件 (`--keep-foreign` 保留; 断点的 `.part` 不动, 下次续传); 用 `--out DIR` 给两种架构各一个目录就互不干扰。
**注意**: qqdl.gtimg.cn / wpscdn.cn 等 CDN 拒绝境外 IP (403), 走全局代理时把这些域名放 DIRECT, 或者先 `unset https_proxy` 再跑;
GitHub 那一个反过来需要代理 (`PROXY=http://127.0.0.1:7897`, Clash 规则模式两者兼顾)。
`--scp user@vm` 下载完直接拷进虚拟机 (有 rsync 就只传新文件), 再加 `--install` 就 ssh 进去跑 `sudo install_cn_apps.sh` (仓库默认在虚拟机的
`~/context_freeze`, 不是就 `--vm-repo PATH`); 在虚拟机里直接跑脚本时 `--install` 装本地目录。用 UTM / Parallels 共享目录也行。

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

两个独立的占位开关 (与 `substitute_kinds` 无关, 都保留应用自己的 id, 场景 / hub / flows 不变):

* `placeholder_fill: true` (= `--placeholders`): **没装的**应用用内置占位程序顶替;
* `placeholder_apps: ["wechat", …]` (= `--placeholder-apps wechat,qq`): **装了也用占位**——给那些没有账号就没有内容的应用 (见 §4.7)。

### 4.7 一个账号都不注册, 先跑数据 (`configs/ubuntu_no_login.json`)

不想用个人社交账号、实验专用号又还没办时, 用这份配置: 与 `ubuntu_cn_desktop.json` 同样的 13 个应用 / 场景 / 限流参数, 只是
`placeholder_apps = [wechat, qq, dingtalk, baidunetdisk]` (装了也不启动厂商程序, 不会弹扫码框) 加 `placeholder_fill = true`
(没装的同样顶替), `work = true`。真正跑厂商程序的只有**不需要登录**的: WPS (关掉登录弹窗)、哔哩哔哩 (不登录可播放)、Firefox / Chrome
(开一个长网页)、中望CAD / 悟空图像 (装了的话)。占位程序不是空壳: 推送类每 15 s 发一条**真实的桌面通知** (通知计数 / 延迟指标照常),
下载类持续写盘, 计算类 3 个 worker 烧 CPU, 都有状态文件 (`statefile` oracle: pid 不变、计数不回退)。

```bash
source scripts/ubuntu/cf_enter.sh
scripts/ubuntu/cf_onboard.sh --yes                                   # 不需要账号, 但 WPS / B站 / 浏览器的 "准备动作" 做一次 (见 §4.6 表第三列)
CONFIG=configs/ubuntu_no_login.json scripts/ubuntu/cf_onboard.sh    #   (交互版: 逐个提示; 占位程序自动通过)
python3 run_linux.py configs/ubuntu_no_login.json --scenario office --policy static --T 30     # 单格试跑
CONFIG=configs/ubuntu_no_login.json PRESET=night WORK=1 scripts/ubuntu/run_all.sh              # 过夜矩阵 (tmux)
```

结果里每格的 `n_placeholders` (analyze / report.md) 记录有几个占位; 论文里把这些格与 "全部真实应用" 的格分开写。之后有了实验账号,
把 id 从 `placeholder_apps` 里删掉、`cf_onboard.sh` 登录, 其它不变; 同一 seed 下场景序列一致, 新旧结果可以逐格对照。
限制: 占位程序没有厂商程序的真实内存曲线 (Electron 应用 300–900 MB vs 占位 120–500 MB) 和网络行为, 所以 **页入成本 / `download_mbps` 这两类指标只在真实应用的格里有意义**, 时延 / CPU 限流 / 状态保留 / 通知到达的机制结论不受影响。

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
* 微信等每次启动都要扫码的应用: 不要让 runner 启动它, 先 `cf_launch.sh` / `cf_onboard.sh` 登好。

### 4.6 统一账号: 注册、登录、模拟用户操作 (`scripts/ubuntu/cf_onboard.sh`)

**一个实验专用手机号, 所有应用用它注册。** 厂商的 Linux 客户端一律不能注册, 注册在手机 App 里做一次 (微信 / QQ 必须在手机上注册并保持登录,
因为它们的 Linux 版只能用这部手机扫码登录; 钉钉 / 百度网盘 / B 站 / WPS 用手机号 + 短信验证码即可; 中望 CAD 在官网注册并申请 Linux 试用授权)。
把号码和各应用的账号写进 `configs/accounts.json` (从 `accounts.example.json` 复制, git 忽略), `cf_onboard.sh` 逐个应用提示该用哪个账号。

| 应用 | Linux 版登录方式 | 登录后停在哪 (给 `--work` 脚本用) |
|---|---|---|
| 微信 | 手机微信扫码 + 手机上确认; 没有密码登录, **每次冷启动都要重扫** | 点开 "文件传输助手", 输入框点一下 (`draft` 往这里打字, 不发送) |
| QQ | 手机 QQ 扫码 (或 QQ 号 + 密码) | 点开 "我的电脑" / 任一会话, 输入框点一下 |
| 钉钉 | 手机号 + 验证码 / 手机钉钉扫码; 首次建一个组织 "cf-bench" | 点开一个会话, 输入框点一下, 关掉引导 |
| WPS | 可不登录 (关掉弹窗) | 启动命令直接打开 `~/.cache/context_freeze/blank.docx` (自动生成, 不经过联网的 "新建" 首页); 关掉弹窗, 在正文点一下让光标在文档里 (`edit` 打字 + 回车, 不保存) |
| 百度网盘 | 手机百度网盘扫码 / 手机号验证码 | 挂一个大文件下载任务 (后台下载负载), 停在传输列表 |
| 迅雷 | 手机号 + 验证码 / 手机迅雷扫码 (不登录不能建任务) | 新建一个大文件 http 下载任务 (如 Ubuntu ISO), 停在任务列表 (`browse` 滚动列表) |
| 爱奇艺 / 抖音 | 可不登录; 手机扫码 | 爱奇艺: 开一个免费剧集播放 (`browse` 滚动剧集页); 抖音: 停在推荐流, 鼠标放在视频区 (`view` 每次滚一格 = 切下一条) |
| 网易云音乐 | **必须登录**: 手机号 + 验证码 / 手机扫码 | 播放一个歌单让它一直放, 停在歌单页 (`browse` 滚动歌曲列表); 后台被限后歌是否断是观测点 |
| 石墨文档 / 腾讯文档 | 手机号 + 验证码 / 微信扫码 | 新建一个文档, 在正文点一下 (`edit` 打字 + 回车, 云端自动保存) |
| 腾讯会议 / 飞书 / 企业微信 | 手机号 + 密码 (会议) / 验证码 (飞书) / 扫码 (企微) | 会议停在主界面不进会 (`none`, 只做常驻); 飞书 / 企微点开一个会话, 输入框点一下 (`draft`) |
| 哔哩哔哩 / 腾讯视频 | 可不登录; 扫码 / 验证码 | 首页推荐流 / 剧集页 (+ 一个缓存任务) (`browse` 滚动) |
| Chrome / Edge / Firefox | 不需要账号 (不要登录浏览器同步) | 一个能向下滚动的长网页 |
| 中望 CAD / 悟空图像 | 中望账号 + 试用授权 / 可不登录 | 打开一张图纸 / 图片, 鼠标停在画布 (`view` 滚轮缩放) |
| 手机版 (Waydroid) | 手机号验证码; 微信 / QQ 手机版可扫码 | 首页 / 列表页; 聊天类点开会话并点输入框 |

`cf_onboard.sh [id…]` (= `run_linux.py CONFIG --onboard`) 对每个应用做四件事, 结果写 `results/onboard/state.json`:

1. **启动进 cgroup** (`dev.launch`, 已在跑的只切前台);
2. **等登录**: 打印登录方式和账号, 轮询到 "主窗口" 出现为止——登录 / 扫码框都很小 (微信 ≈ 300×400, QQ ≈ 330×450, 钉钉 ≈ 420×600),
   主窗口 ≥ 700×480 (`onboard_overrides` 里按 id 改 `main`); 手机版看前台包名。也可以回车确认 / `s` 跳过; `--onboard-timeout` 默认 600 s;
3. **准备动作**: 打印上表第三列, 回车确认;
4. **演练** (`rehearse`): 把窗口切到前台, 做一轮 `--work` 的脚本操作 (`work_s` 秒), 读两次 oracle, 两次一致且有值才算 `ready`。
   这一步把 "输入框没焦点 / 剪贴板读不到 / 被弹窗挡住" 之类的问题在白天暴露出来, 而不是夜里矩阵跑完后看到一列 `work_state_ok = None`。

```bash
scripts/ubuntu/cf_onboard.sh                      # 配置里的全部应用 (已装的桌面版 / 手机版)
scripts/ubuntu/cf_onboard.sh wechat qq            # 只重做这两个
scripts/ubuntu/cf_onboard.sh --check              # 表: installed / running / main_window / onboarded / ready; 有未就绪的返回 1
python3 run_linux.py CONFIG --onboard --yes       # 非交互 (ssh 脚本里): 不等回车, 只按窗口判断
```

runner 的 preflight 会对 "从未 onboard" / "上次未就绪" 的应用给 warning (不阻止); `run_all.sh` 在快照阶段把 `--check` 的表存成
`OUT/onboard.txt`。重启虚拟机后应用都要重新登录 (微信每次冷启动都要扫码), 所以 **每次开机跑一遍 `cf_onboard.sh`**, 已登录的应用几秒就过。
Wayland 会话里演练一步会跳过 (xdotool 不可用), 登录检测照常。

**`--check` 表最后一列 `why` 告诉你每个未就绪应用卡在哪一步**, 按它处理:

| `why` | 意思 | 怎么办 |
|---|---|---|
| `not running (TIMEOUT)` | 启动后没出窗口, 进程也不在了 (崩溃 / 启动命令不对) | 看 `results/app_logs/<id>.log`; `command not found` 说明启动名不在 PATH (现在会自动改用 `exe` 路径), 其他报错按日志; 手动能起来就用 `--discover` 核对 `cmd` 写进 `app_overrides` |
| `running outside /cf (desktop icon / autostart)` | 应用在跑, 但进程在 `user.slice` 里 (从桌面图标点开的, 或钉钉这类开机自启动的), 普通用户不能把它移进 `/cf/<id>`, 限流对它无效 | 交互式 `cf_onboard.sh <id>` 会在启动时发现并提议 "退出它并由脚本重新启动" (回车即可); 钉钉还要关掉自启动 (`rm ~/.config/autostart/*dingtalk*`); 或配置 `sudo_adopt: true` |
| `login timeout / skipped` | 等了 `--onboard-timeout` (默认 600 s) 没见到 ≥ `main` 的主窗口 | 登录完成后重跑 `cf_onboard.sh <id>`; 主窗口本来就小的在 `onboard_overrides` 里改 `main` |
| `N non-normal (dialog / dock ...)` 出现在 `main_window` 列 | 找到的窗口都不是普通顶层窗口 (对话框 / 托盘). 旧版本把微信 / WPS / 中望的 Qt 无边框主窗口 (`_KDE_NET_WM_WINDOW_TYPE_OVERRIDE, _NET_WM_WINDOW_TYPE_NORMAL`) 也误判成这种, 现在按类型列表判断 | 更新代码; 仍出现就 `--discover` 看该窗口的 `_NET_WM_WINDOW_TYPE` |
| `no main window now (...)` | 记录是就绪的, 但现在没有一个 ≥ `main` 的普通窗口; `main_window` 列写着实际找到的最大窗口 (`largest 640x400 < 700x500`) 或 `no window matched` | 窗口被最小化 / 关掉了就点开; 尺寸偏小就放大窗口或改 `main`; `no window matched` = WM_CLASS / 标题都对不上, `--discover` 看真实的 WM_CLASS 写进 `app_overrides.wm_class` |
| `work not-active (focus was on 0x... 'xxx' [class])` / `oracle clipboard: not-active (...)` | 脚本发键前核对 `_NET_ACTIVE_WINDOW`: 焦点在**别的程序**的窗口上 (括号里是谁: 弹窗 / 对话框 / 桌面) - 切前台失败 (GNOME 焦点保护 / 模态对话框挡住) 或当时记的是已经消失的登录框。该应用自己的其他窗口 (QQ / 钉钉弹出的独立会话窗、WPS 重建的文档窗) 不算, 照常输入。发现焦点被抢时脚本会**夺回一次** (再 `activate` 该应用) 再输入 / 读 oracle, 日志记 `refocused`; 只有夺不回来才算 `not-active` | 关掉括号里那个窗口 (操作者的终端: 回车后别再点它), 重跑 `cf_onboard.sh <id>`; 还不行看 §10 "activate 失败" |
| `prep skipped` | 准备那一步回答了 `s` - 常常不是故意的: 上一问 (如 "退出并重新启动?") 回车后脚本忙了 10-20 s, 这期间敲的键被下一问吃掉了 | 重跑 `cf_onboard.sh <id>`, 等提示出现再敲键 (现在每个提示前会丢弃提前敲的键) |
| `oracle clipboard: xclip: ...` | 没装 xclip | `sudo apt install xclip` |
| `oracle clipboard read nothing` / `clipboard-unchanged` | ctrl+a / ctrl+c 后剪贴板没有文本 (2 s 内应用没有交出选区): 输入框没焦点, 回到了登录页, 或该应用复制出来的不是文本 | 在输入框 / 正文里点一下再重跑; WPS 确认光标在 blank.docx 正文而不是 "新建" 页; 聊天软件确认还在登录状态 |
| `oracle ... unstable` | 两次读到的内容不同 (有东西在自动变: 动画标题、输入法候选) | 换 oracle (`work_overrides`: `"oracle": "title"` / `"screen"`) |

## 5. 配置文件参考 (`configs/ubuntu_cn_desktop.json`)

现成的配置: `ubuntu_cn_desktop.json` (手册默认的 13 个应用, x86 / 齐全时用), `ubuntu_arm64.json` (arm64 虚拟机实际装好的 13 个:
微信 / QQ / 钉钉 / 腾讯会议 / 百度网盘 / 迅雷 / 爱奇艺 / 哔哩哔哩 / 网易云音乐 / Firefox / WPS / 中望CAD / 石墨文档, 全部真跑 + `work`; 抖音的
arm64 包在 ACE 容器里缺 `libc_secshared.so` 启动即退, 由哔哩哔哩顶替), `ubuntu_no_login.json` (§4.7), `ubuntu_placeholders.json` (冒烟)。

| 键 | 默认 | 含义 |
|---|---|---|
| `apps` | 13 个 id | 实验集合 (桌面版 id); 没装的沿 `substitute` 链找 `substitute_kinds` 允许的替代, 都没有则 WARN 并剔除 (少于 2 个时报错) |
| `substitute_kinds` | `["mobile"]` | 桌面版缺失时允许退到哪些 kind: `mobile` (同一应用的手机版, Waydroid) / `oss` (kdenlive 等) / `placeholder` (占位程序) |
| `mobile` | `{}` | Android 运行时: `backend` (`waydroid` / `adb`), `adb_serial` (默认 `waydroid status` 的 IP:5555), `wm_class` (Android 窗口的 WM_CLASS 正则), `wayland_display` |
| `app_overrides` | `{}` | 对 catalogue 条目的字段覆盖, 或新增应用 (键 = 新 id, 至少 `cmd` `match` `wm_class` `category`, 可加 `name` `notify_names` `arch` `kind` `package` `substitute`) |
| `never_throttle` | `[]` | 永远 `fg` 的应用 (音乐播放器、录屏) |
| `policy` / `eta` / `policy_kw` | `static` / 0.3 | 策略与预算系数, 同 Android 版; `static` `none` 忽略 η |
| `throttle.cpu_mode` | `quota` | `quota`: 每个后台应用各自 `cpu.max` = `cpu_cores` 个核的带宽; `cpuset`: 所有后台应用挤在 `cpuset_cpus` 这 (几) 个核上 (`last` = 编号最大的核); `both`; `none`。**旧数据注意**: 2026-11-11 之前的版本在把应用提回前台时清不掉 `cpuset.cpus` (程序写的是空串: 零字节的写入根本到不了内核; 而且即使到了, 内核也拒绝把有进程的 cgroup 的 `cpuset.cpus` 清空, 报 ENOSPC —— 现在改成把整棵树的全部核显式写回去, 写不进去的限制会在日志里 WARN), 用过 `cpuset` / `both` 档 (§6.8 的 squeeze、§6.9 的 squeeze 格、矩阵的 cpuset 对照) 以后, 被压过的应用**回到前台也仍钉在那一个核上**, 同一次开机里之后跑的格 (包括 `none`) 也带着这个限制; 这些格的前台响应 / 前台任务时延偏慢, 需要重跑。旧结果文件可以直接查: `python3 -m cf.linux.classes 旧结果/*.jsonl --no-plots` 的开头会标出 "前台应用仍被 cpuset 钉在核 N 上" 的格。现在每次运行开始时也会先解除全部遗留限制 |
| `throttle.cpu_cores` | 1.0 | 可以是 0.5 (半个核) |
| `throttle.cpu_weight` | 20 | 后台与前台争同一核时的权重 (前台 100) |
| `throttle.mem_frac` | 0.5 | `shrunk` 应用 `memory.high` = mem_frac × m_fg (m_fg = warmup 时前台 PSS+Swap) |
| `throttle.mem_min_mb` | 192 | `memory.high` 下限; 低于它 Electron 应用会抖动 (每次切回都要重新读几百 MB) |
| `throttle.thrash_high_per_s` / `thrash_backoff` | 5.0 / 1.5 | 抖动保护: 某个 `shrunk` 应用在一次停留里 `memory.events high` 超过每秒这么多次 (内核在反复回收它, 后台工作集装不进 `memory.high`), 就把它的 `memory.high` 乘 `thrash_backoff`, 最高到 m_fg; 记到 `step.actions.backoff`, 汇总列 `mem_high_backoffs`。0 关闭 (纯静态上限, 用于对照) |
| `throttle.mem_hard` | false | true 时再设 `memory.max = mem_hard_factor × memory.high` → **有 OOM kill 风险**, 仅用于对照 |
| `throttle.swap_max_mb` | null | 单个 `shrunk` 应用的 `memory.swap.max`; `memory.high` + 很小的 swap 上限 = 应用几乎不能再分配内存, 只会变慢, 仍不会死 |
| `throttle.io_weight` | null | 后台 `io.weight` (需要 io 控制器, 对下载类应用有用) |
| `throttle.proactive_reclaim` | true | 降档时立即写 `memory.reclaim` (内核 ≥ 5.19) |
| `throttle.squeeze_rounds` / `squeeze_every_s` | 0 / 0 | **实验性 "挤压"** (§6.8): 降到 `shrunk` 时连写 `squeeze_rounds` 次 `memory.reclaim = memory.current` (把整个 cgroup 能换出的都换到 zram、能丢的页缓存都丢掉, 不只是压到 `memory.high`), 停留期间每 `squeeze_every_s` 秒对所有 `shrunk` 应用再挤一次 (记到 `timeline[].squeeze`); 0 关闭。CLI `--squeeze N --squeeze-every S`。与 `thrash_high_per_s=0` 搭配时内核会一直回收它们 → swap 写入大, 后台应用很慢 |
| `throttle.cpu_weight` | 20 | 后台应用的 `cpu.weight` (1–10000, 前台 100); `cpu_mode both` + `cpu_cores 0.1` + `cpu_weight 1` = 所有后台应用挤在最后一个核上、每个只有该核 10 % 的带宽、前台要用时几乎抢不到 (CLI `--cpu-weight`) |
| `cpu_throttle_resident` | true | 预算内的后台应用也限 CPU (`bg`); false 则预算内的完全不受限, 策略只决定内存 |
| `trace` | scenario office T=60 | `kind`: `scenario` (`office` 办公 / `design` 设计 / `download` 下载 / `chat` 消息 / `media` 娱乐: 抖音 / 爱奇艺 / 网易云 / B站) / `day` (五场景按 15/35/15/20/15 % 依次经历) / `zipf` / `markov`; 也可内联 `{"apps":{id:权重},"hub":id,"flows":[[...]]}`。场景里本机没装 (不在 `apps`) 的应用自动剔除, 只要剩 ≥ 2 个就能跑 |
| `dwell_s` / `settle_s` / `warmup_dwell_s` | [5, 12] / 2 / 10 | 每步停留 / 切换后等待 / warmup 每应用停留 (秒)。`dwell_s` 给一个数 = 固定; 给 `[lo, hi]` = 每步按 seed 均匀抽样 (同一 seed 下各策略的每一步停留相同, 可直接对比); 每条 `step` 记录带 `dwell_s` |
| `per_process` / `quiet_after_s` | false / 30 | **按进程统计** (§6.8): true (或 `--per-process`) 时 warmup / 每步 `after_launch` / `after_dwell` 的每个应用带 `procs` (每个进程的 pid / 名字 / PSS / 匿名 / 文件 / SwapPss / RSS), timeline 采样带 `procs: {pid: [pss_kb, swap_pss_kb, name]}`; `quiet_after_s`: 一次停留里从第几秒起算 "后台稳定静默期" (前台的脚本操作已做完, 后台应用只是待着), analyze 的 `quiet_*` 列只用这一段的采样 |
| `sample_every_s` | 2.0 | > 0 时停留期间每隔这么多秒做一次轻量采样 (PSI cpu/mem/io、zram、MemAvailable、swap in/out、每应用 PSS / swap / cpu / 写盘 / 节流时间 / tier) → `step.timeline`, 画时间序列 (`--plots-dir` 的 `*_timeline.png`); 0 关闭 |
| `placeholder_fill` | false | true (或 `--placeholders`): 没装上的应用用内置占位程序顶替, **id 不变**, 场景 / 轨迹不变 (`meta.placeholders` 列出); 在真实应用装齐之前就能跑完整矩阵, 之后同一命令重跑即用真实应用 |
| `placeholder_apps` | `[]` | 这些 id 即使装了也用占位程序顶替 (没有账号的应用; §4.7); CLI `--placeholder-apps a,b` 追加 |
| `work` / `work_s` | false / 3.0 | true (或 `--work`): 每步前台停留的开头在前台应用上做 `work_s` 秒 **脚本化操作** (打草稿 / 滚动 / 上滑), 切走前记一个内容 oracle, 回来时重读比较 → `launch.work_state_ok`, 不一致计入 `state_lost` 与 `work_fail` (§6.6) |
| `work_overrides` | `{}` | 按应用 id 覆盖脚本: `{"wechat": {"kind": "none", "oracle": "screen"}}`; 字段 `kind` (`edit` `draft` `browse` `view` `swipe` `none`) / `oracle` (`clipboard` `title` `screen` `statefile` `ui` `none`) / `text` / `scroll` / `swipes` / `tolerance` (screen 的汉明距离容差, 默认 12/256 位) |
| `onboard_overrides` | `{}` | 按 id 覆盖 §4.6 的登录知识: `{"wechat": {"main": [600, 400], "needs": "…", "prep": "…", "method": "qr"}}` |
| `minimize_background` | false | 切走的窗口是否最小化 (真实用户不会, 默认不做) |
| `notifications` | true | 用 `dbus-monitor` 统计推送类应用的 `Notify` 调用 |
| `adopt_strays` / `sudo_adopt` | true / false | 把 cgroup 外的同名进程移进来; 用户权限移不动时是否 `sudo scripts/ubuntu/cf_adopt.sh` (需 sudoers 免密, 见脚本头)。**有手机版应用时必须 `sudo_adopt: true`** (Android 进程属于其它 uid) |
| `launch_timeout_s` / `ping_timeout_s` | 90 / 15 | 等窗口出现 / 等 `_NET_WM_PING` 回应 |
| `min_ram_mb` | 6144 | 低于此 WARN |

命令行可以覆盖最常改的项: `--policy --eta --T --scenario --seed --cpu-mode --cpu-cores --cpuset --mem-frac
--swap-max-mb --mem-hard --no-resident-cpu --dwell S [S] --sample-every S --placeholders --work --work-s S --name --out-dir`;
`--work-plan` 打印每个应用会用的脚本 / oracle 后退出。

**关于 η 与预算的语义 (重要, 与 Android 版的差别)**: 一个 `shrunk` 应用在预算模型里仍占 `memory.high` = max(mem_frac × m_fg, mem_min_mb)
(ρ_i = memory.high / m), 所以后台总占用有一个 **下限** floor = Σ(其它应用的 memory.high) ≈ mem_frac × (1 − 1/n) × Σ m_fg。
B = η × Σ m_fg 低于 floor 时任何预算策略都留不下一个应用, 和 `static` 完全一样 (每步 `budget_violation`)。
`mem_frac = 0.5` 时 η_min ≈ 0.5: **比较策略请用 η ≥ 0.6** (矩阵默认 0.6 / 0.8); 想看更低的 η 就同时把 `mem_frac` 调小
(`MEM_FRAC=0.3 ETAS="0.4 0.6" scripts/ubuntu/run_matrix.sh`)。runner 在 `budget` 记录里给出 `shrunk_floor_kb` / `eta_min`, 不可达时 WARN;
`cf.linux.analyze` 的 `eta_min` / `eta_feasible` / `n_resident_avg` 列和 report.md 的提示会把这种格标出来。
`policy: none` 是真正的基线: 任何应用都不受限 (全部 `fg`)。

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
scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 60       # 默认: 5 场景 (office download chat design media) x (none static lru landlord hybrid) x η 0.6/0.8, ~40 格 ~10 h
NIGHT=1 scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 40   # 过夜档: η 0.7/0.85 + 1c:0.3 / 0.5c:0.5 / cpuset 对照, ~50 格 ~7-10 h (swap128 / memhard 默认关, SWAP_CAPS=128 MEM_HARD=1 打开)
FULL=1  scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 60   # 全量: 6 场景 (含 day) x 6 策略 x η 0.55/0.7/0.85 x 2 seed x 全部变体, ~350 格, 数天 (可中断续跑)
PROFILES="1c:0.5 1c:0.3 0.5c:0.5" POLICIES="none static" scripts/ubuntu/run_matrix.sh     # 只比限流强度
CPU_MODES="quota cpuset" RESIDENT="1 0" SWAP_CAPS="512 128" MEM_HARD=1 scripts/ubuntu/run_matrix.sh   # 单独点名变体
MEM_FRAC=0.3 ETAS="0.4 0.6" scripts/ubuntu/run_matrix.sh                                 # 第二遍: 更小的 memory.high (文件名加 _mf0.3)
PLACEHOLDERS=1 NIGHT=1 scripts/ubuntu/run_matrix.sh                                      # 没装上的应用用占位程序顶替, 先把矩阵跑通
WORK=1 WORK_S=3 NIGHT=1 scripts/ubuntu/run_matrix.sh                                     # 每格加 --work: 脚本化操作 + 回来时的内容核对 (work_state_ok)
DRY=1 FULL=1 scripts/ubuntu/run_matrix.sh                                                # 只列出会跑哪些格 / 命令
QUICK=1 scripts/ubuntu/run_matrix.sh                                                     # office, none+static, T=15
# 结束时自动: summary.csv / summary_agg.csv / per_app.csv / pareto.csv / plots/*.png / report.md (都在 OUT 目录)

# 6.3b 一条命令从开机到 report.md (每步幂等, 可中断重跑): 建 cgroup 树 + zram → check_env → probe/catalog 快照 → 冒烟 → 矩阵 → 分析
scripts/ubuntu/run_all.sh                           # 默认矩阵 -> results/ubuntu_all/{env,probe,catalog}.txt + matrix/
PRESET=night scripts/ubuntu/run_all.sh              # 过夜档 (推荐在 tmux 里跑: tmux new -s cf)
PRESET=full PLACEHOLDERS=1 scripts/ubuntu/run_all.sh
WORK=1 PRESET=night scripts/ubuntu/run_all.sh       # 同上 + 脚本化操作; 冒烟阶段把 work_fail = 0 也当作不变量, OUT/work_plan.txt 记下每个应用的脚本
ONBOARD=1 WORK=1 PRESET=full scripts/ubuntu/run_all.sh   # 真实应用全量: 先跑登录向导 (§4.6), --check 不全就绪则停 (rc 2), 全就绪再冒烟 + 全矩阵
PRESET=quick scripts/ubuntu/run_all.sh              # 只做冒烟
SKIP_SMOKE=1 OUT=results/run2 T=40 scripts/ubuntu/run_all.sh

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

### 6.6 用脚本代替人工点击: `--work` 与 `work_state_ok`

`state_check` 只看进程 / 窗口 / OOM; 用户关心的是 **内容**: 没发出去的草稿、滚到一半的页面、CAD 里的视图。`--work` 把 §8 第 3 条
的手工检查自动化 (`cf/linux/work.py`):

1. 每步把应用切到前台、`settle_s` 之后, 先在它身上做 `work_s` 秒脚本化操作 (占用停留时间的开头, 不超过 dwell 的 60%; warmup 时也做一轮);
2. 停留结束、切走 **之前** 读一次 oracle (一个能代表当前可见状态的小摘要) 存起来;
3. 这个应用下次回到前台 (从 `bg` 或 `shrunk` 回来, `settle_s` 之后) 立刻再读一次, 与存的比较 → `launch.work_state_ok` = True / False / None
   (None = 没有可比对象: 首次、COLD 重启、oracle 读不到); False 同时写 `state_lost[app].work_state = {oracle, before, after}`。

| 应用 | 默认脚本 (`kind`) | 默认 oracle | 说明 |
|---|---|---|---|
| 微信 / QQ / 钉钉 | `draft`: ctrl+a 后键入 `cf-work <id> t<step> 草稿未保存 ` (**替换**上一步的草稿, 不追加) | `clipboard`: ctrl+a ctrl+c 后剪贴板文本的长度 + md5 | 需要先手动点开一个会话让输入框有焦点 (登录时顺手做) |
| WPS | `edit`: ctrl+a 后键入同样一行 + 回车 (替换正文) | `clipboard` | 文档不保存; 回来时 ctrl+a 全选再 End 取消选区 |
| 浏览器 / 腾讯视频 / 哔哩哔哩 | `browse`: 滚轮下翻 `scroll` 格再回退 1/4 | `title`: `_NET_WM_NAME` (页面标题) | 像素会随视频变化, 所以只认标题; 页面被重载会换标题 / 回到顶部 |
| 百度网盘 / 小艺 / 腾讯会议 | `none` | `title` | 下载本身在后台进行 (`write_mb` 已经度量); 会议只做常驻 |
| 迅雷 / 爱奇艺 / 网易云音乐 | `browse`: 滚动任务列表 / 剧集页 / 歌单 (`scroll` 4 / 6 / 6) | `title` | 下载 / 播放在后台继续; 标题 = 任务数 / 剧名 / 歌名, 被重载才会变 |
| 抖音 | `view`: 滚轮 1 格 (= 切到下一条视频) | `title` | 每次 work 都翻一条, 后台被限到 1 核后回来是否卡顿看 `activate_ms` / `ping_ms` |
| 石墨文档 / 腾讯文档 / 飞书 / 企业微信 | 同 WPS (`edit`) / 同微信 (`draft`) | `clipboard` | 在线文档云端自动保存, 回来时内容应完整 |
| 中望CAD / 悟空图像 / kdenlive / librecad / gimp | `view`: 滚轮缩放 `scroll` 格 | `screen`: 窗口像素 16×16 均值哈希 (python-xlib `get_image`, 只取客户区), 汉明距离 ≤ `tolerance` 算相同 | 对光标闪烁、时钟数字不敏感, 对视图重置 / 重新打开敏感 |
| 占位程序 | `none` | `statefile`: `/tmp/cf_placeholder_<id>.json` 的 `pid` 不变且 `counter` 不回退 | 冒烟配置用 |
| 手机版 (Waydroid) | 下载 / 视频类 `swipe` (上滑 `swipes` 次, 短视频翻页); 推送类 `draft` (`input text`); 计算类 `view` (滑动) | `ui`: 最前 Activity + uiautomator dump 里全部文字 (数字抹掉) 的 md5; 也可选 `screen` (`screencap`) | 冷启动回到 Splash / 首页 → Activity 不同 → False |

脚本只通过 XTEST (`xdotool type / key / click`) 发给 **当前活动窗口**, 发之前核对 `_NET_ACTIVE_WINDOW` 就是该应用 (否则记 `not-active`,
不输入); 手机版核对 `foreground_pkg`。失败不会中断实验 (`step.work.status` 记录 `ok / skipped / not-active / error: …`)。

```bash
python3 run_linux.py configs/ubuntu_cn_desktop.json --work-plan            # 每个应用会做什么 / 用什么 oracle (含未安装的)
python3 run_linux.py configs/ubuntu_placeholders.json --policy lru --eta 0.6 --T 12 --work --work-s 1   # 占位程序上试一遍
python3 run_linux.py configs/ubuntu_cn_desktop.json --scenario office --policy static --T 60 --work      # 真实应用
```

数据: `step.work` (kind / status / 耗时), `step.launch.work_state_ok` + `work_check {before, after}`, `warmup.work`; 汇总列
`work_checks` (回到前台时做过比较的次数) / `work_fail` (不一致的次数, **应为 0**), 每应用表同名两列; report.md 的不变量一行里有它们;
`run_all.sh` 的冒烟检查把 `work_fail = 0` 和 `state_lost / oom / COLD = 0` 一起当作继续跑矩阵的前提。

注意事项: (a) `clipboard` oracle 会覆盖系统剪贴板, 实验期间不要指望剪贴板里的东西; 不想碰剪贴板就 `work_overrides` 改成
`"oracle": "screen"` 或 `"title"`。(b) `draft` / `edit` 每步先 ctrl+a 再键入, 文本只有一段 (~28 字节), 不会越积越长: 早期版本是追加的, 两晚后 QQ 的草稿到了 22 KB, Electron 输入框慢到 ctrl+c 端出来的还是**上一步**的文本 (剪贴板过期 → 连续假 `work_fail`), 最后 QQ 崩掉回到登录页。读 oracle 前现在会先在剪贴板放一个哨兵, 等应用真的交出选区再读 (最多 2 s); 到时还是哨兵 = 没有可复制的内容 (空输入框 / 登录页) → 记为空摘要, 与之前的草稿不一致 → `work_fail`, 这才是 "会话丢了" 的正确判定。
(c) 只支持 X11 (XTEST / `get_image`); Wayland 下 `--work` 会全部 `not-active` / `error`。(d) 手机版 `ui` oracle 依赖 `uiautomator dump`,
Waydroid 上要 1–3 s; 列表页的推荐流本身会刷新 → 用 `tolerance` 更大的 `screen` 或 `kind: none`。(e) `work_s` 计入 dwell 的开头, 所以
`dwell_s` 的下限至少要 ≥ 2 × `work_s`。(f) 微信 / QQ 在没有焦点输入框时 `draft` 的按键会被吞掉, 不影响实验, 只是 `clipboard` 读到的是空摘要 (`0:…`, 带 `clipboard-unchanged` 注记)。

### 6.8 实验性: 按进程统计前 / 后台占用 + 激进挤压 (`scripts/ubuntu/run_squeeze.sh`)

另一种统计口径: 不看 "后台总 PSS 占 Σm_fg 的比例", 而是**每个进程**切到后台前 (自己那一步停留结束时的 PSS, 没被请求过的用 warmup 样本)
与切到后台后 (后台停留结束时) 各占多少、被挤掉了多少, 以及后台**稳定静默期间每分钟换出多少** (磁盘 / zram 写入速率)。后台用最激进的档位:
`cpuset` 最后一个核 + 该核 10 % 的 `cpu.max` + `cpu.weight 1`; **目标是后台只留活跃时的 10 %**: `memory.high = 0.1 × m_fg` (≥ 32 MB), 切走时连写 3 次
`memory.reclaim = memory.current` (带 `swappiness=200`, 优先换匿名页; 内核 < 6.9 不认这个参数时自动退回普通写法), 停留期间每 10 s 再挤一次,
脚本把全局 `vm.swappiness` 设为 200 (结束时恢复); 关掉抖动保护。没有 `memory.max`, 所以仍不会 OOM kill —— 10 % 装不下时进程只是被节流 (分配内存时睡眠), 不会死。只跑 office 一个场景、5 个应用 (微信 / QQ / 钉钉 / WPS / Firefox), 固定 150 s 的长停留
(前 3 s 脚本操作, 30 s 后算静默期, 每 10 s 采一次), T=12 → 一格约 32 min, 三格约 1.6 h, **不是全量矩阵**:

```bash
scripts/ubuntu/run_squeeze.sh                      # none 基线 + squeeze 激进 + mild 对照 (夜间跑的 1 核 / 0.5 档位), 结果在 results/arm64_squeeze
CELLS="none squeeze" scripts/ubuntu/run_squeeze.sh  # 只跑两格;  T=20 DWELL=240 更长的静默窗;  SEEDS="1 2" 复本;  ONBOARD=1 先过登录向导;  DRY=1 只打印命令
```

配置 `configs/ubuntu_arm64_squeeze.json`; 输出除常规文件外多 `squeeze_tables.xlsx` (四个工作表: 统计表 / 各格对照 / 每格汇总 / 每进程, 中文表头, 可直接进论文; `--xlsx`, 需要 openpyxl; 首次运行的那份存在 `docs/results/arm64_squeeze_2026-11-04.xlsx`)、`squeeze_apps.csv` + `plots/squeeze_apps.png` (每个应用一组柱: 活跃足迹 m_fg vs 后台驻留、驻留占 m_fg 的 %、后台拿到的单核 CPU %、后台每分钟回收 / 页回的量、切回延迟 P50/P95; `--squeeze-apps`) 和 `per_process.csv`, `report.md` 多 "Per app (squeeze)" 与 "Per process" 两节。新指标 (都只看 `per_process` 的跑法, `--all-cols` 可见):

| 列 | 含义 |
|---|---|
| `fg_pss_avg_mb` | 前台应用在自己那步停留结束时的 PSS (各步平均) |
| `bg_pss_quiet_avg_mb` | 停留结束时其它应用的 PSS 之和 (各步平均): 挤完后后台实际还驻留多少 |
| `bg_squeeze_pct` | 1 − 后台应用现在的 PSS / 它们各自上一次在前台时的 PSS → 被挤掉的比例; `none` 应接近 0。注意参照本身会变: 被挤过的应用切回前台只页入它碰到的部分 (前台 PSS 也减半), 所以跨格比较用下一列 |
| `bg_pct_of_m_fg` | 后台应用的驻留 PSS / 它们的 m_fg (warmup 时 PSS+Swap, 整个 sweep 固定) → "后台只剩活跃时的 x %" 的那个数; 目标 10 % 看这一列 |
| `quiet_swap_out_mb_per_min` | 静默期内全系统 `pswpout` 折算的每分钟换出量 (zram 写入速率) |
| `quiet_bg_reclaim_mb_per_min` | 静默期内后台 cgroup `memory.stat pgsteal` 增量: 内核每分钟从它们回收了多少页 (匿名页换出 + 文件页丢弃) |
| `quiet_bg_swap_growth_mb_per_min` | 静默期内后台 cgroup `memory.swap.current` 的增量: 稳定状态下还在持续换出 = 后台应用还在醒着碰内存 |
| `quiet_zram_growth_mb_per_min` / `zram_phys_avg_mb` | 静默期 zram 压缩存储的增长 / 水位: 被挤出去的页仍以压缩形式占着内存, **真正省下的 = PSS 减少 − zram 增长** (zstd 对 Electron 堆通常 3–4 : 1) |
| `quiet_s` | 本格累计的静默秒数 (分母) |
| per_process.csv: `pss_fg_mb` / `pss_bg_mb` / `pss_bg_quiet_mb` / `swap_bg_quiet_mb` / `squeeze_pct` / `swap_growth_mb_per_min` | 每个 (应用, 进程名) 的: 前台 PSS / 刚切走时 / 后台停留结束时 / 那时在 swap 里的量 / 被挤掉的比例 / 静默期 SwapPss 每分钟增长; 同名多进程 (Electron 的 renderer / utility) 合并, `n_pids` 给个数 |

每步记录里 `bg.<app>.reclaim_mb` / `swap_delta_mb` 是这一步停留里从该 cgroup 回收 / 换出的量; `actions.limits.<app>.squeeze` 是切走时那次挤压的前后 `memory.current`。
`report.md` 的 Per process 一节下面会列出后台仍高于前台 10 % 的进程 (`squeeze_pct` < 90, 前台 ≥ 20 MB): 剩下的就是 `memory.reclaim` 拿不走的 —— 钉住的页 (GPU / 共享缓冲、mlock、内核侧)、应用在后台一直碰的工作集; 看它的 `swap_growth_mb_per_min` 是否一直 > 0 就知道是 "拿不走" 还是 "拿走又被碰回来"。

**换出 / 换入随时间的曲线** `plots/<run>_swapflow.png` (每个有 per-process 数据的 run 一张; 每个应用一行; 有中文字体就是中文标注, 没有则自动退回英文; 字体怎么选、图里出现方框怎么办见 §10 "图里的字是方框"): 横轴是**从该应用被切到后台起的秒数**, 左列是每个采样间隔内从它的 cgroup **换出**的量 (`Δpgsteal × 4 KB`, 红) 和它**换回**的量 (`Δmajflt × 4 KB`, 蓝; 都折算成每 `sample_every_s` 秒的量, 缺样本的间隔断开不画), 右列是两者自切走起的累计量 (灰虚线 = 该 cgroup `memory.swap.current` 比切走时多了多少)。一次 sweep 里同一个应用会被切走好几次, 每次 (一个 "episode") 都淡色叠在同一时间轴上, 粗线是各次的均值; 这轮里一次都没被切走过的应用 (如 QQ) 画它从实验开始起的那一段, 标题会注明。黄色底是**暂态**: 从切走到每间隔换出量回落到后半段中位数 2 倍以内的那段, 旁边标出切走瞬间一次性挤出的 MB; 右下角标出最长那次后半段的斜率 (每分钟换出 / 换回 MB 和换回占换出的 %) 并给一句判断: ≥ 80 % "换出 ≈ 换回, 稳态, 内存只是在来回搬"; 20–80 % "换出 > 换回, 稳态, 但一部分被丢掉的页 (文件页经 readahead) 不经缺页就回来了"; < 20 % "几乎不再换回, 后台安静"。两条累计线**平行** = 到了稳态。要看清从暂态到稳态的收敛, 用 `CELLS="none squeeze1" SAMPLE=2` 跑: `squeeze1` 与 `squeeze` 相同但 `squeeze_every_s 0` (只在切走时挤一次, 之后只靠 `memory.high` 顶着), 采样 2 s; 默认的 `squeeze` 格每 10 s 再挤一轮, 曲线是锯齿 (每轮挤出 ~150 MB, 10 s 内又页回), 采样 10 s 也太粗。

**跑之前 (不用重启虚拟机, 不用关这 5 个应用):**

1. 这 5 个应用 (微信 / QQ / 钉钉 / WPS / Firefox) **保持登录并开着**, 它们已在 `/cf/<id>` 里的话 runner 直接复用 (不在的话会重新启动, 需要重新登录); `python3 run_linux.py configs/ubuntu_arm64_squeeze.json --onboard-check` 五个都 ready 即可。WPS 里是 onboarding 的空白文档 / Firefox 固定几个轻页面, 两格之间不要改 (Firefox 的 PSS 随标签页变)。
2. **其它应用全部退出** (腾讯会议 / 网盘 / 迅雷 / 爱奇艺 / 哔哩哔哩 / 网易云 / 中望 / 石墨): 它们不在本配置里, runner 不会碰它们, 但会占内存和 CPU (爱奇艺后台播放), 还会写 swap —— `quiet_swap_out_mb_per_min` 是全系统的 `pswpout`, 会被污染。脚本启动时检查 `/cf/*` 里还有进程的叶子, 发现不在实验里的就停下 (`STRICT=0` 可以硬跑)。退出前若它们还带着上一轮的限制, 先 `python3 run_linux.py configs/ubuntu_arm64.json --release`。
3. **zram**: 上一轮矩阵跑完 zram 里常常还压着 1–2 GB; 退出其它应用后看 `cat /proc/swaps`, 用量 > 一半就重置: `sudo scripts/ubuntu/setup_zram.sh --off && sudo scripts/ubuntu/setup_zram.sh 4096` (swapoff 要把压着的页读回内存, 所以先退应用)。5 个应用被挤到底时 zram 要装下它们几乎全部的匿名页 (压缩后约 1–1.5 GB), 4 GB 够; 要跑 `SEEDS="1 2"` 或 `DWELL=240` 也不需要加大。
4. 之前的矩阵 (tmux 里的 `run_all.sh`) **不能同时在跑**; 本脚本自己的 `OUT=results/arm64_squeeze` 与矩阵互不影响, 中断后同一命令续跑。
5. 跑的 1.5 h 里**不要碰虚拟机**: 剪贴板会被 oracle 覆盖, 点别的窗口会抢焦点让 `work` 失败; Mac 侧 `caffeinate -dims` 防休眠, 之前花屏重启过的话按 §10 关掉 UTM 的 GPU 加速。
6. 会看到什么: 激进格里后台应用被切回时要几秒 (`lat_p95` / `ping_p95` 几千 ms 是正常的), 微信 / 钉钉可能短暂显示 "连接中"; `swap_write_mb` 每格几 GB。**要盯的是** `state_lost` / `work_fail` / `n_cold` 必须仍为 0 —— 如果 QQ / 钉钉在 10 % + 10 % 单核下心跳超时掉线、回到登录页, 那就是这个档位的真实代价, 会以 `work_fail` (剪贴板只剩哨兵 → 空摘要) 和 `processes_gone` 记下来, 不要重跑掩盖它; 想找到不掉线的最低档位, 下一轮再把 `mem_frac` 抬到 0.15 / 0.2 或 `cpu_cores` 抬到 0.2 对比。
7. 不能靠 `memory.max` 再逼: 它能把驻留压死在 10 %, 但 10 % 装不下时内核只能 OOM kill —— 丢状态, 与实验的不变量冲突; `memory.high` + 反复 `memory.reclaim` 已经是不杀进程前提下最激进的组合。
预期: 激进档下后台应用很慢 (切回时 `lat_p95` / `ping_p95` 明显变大, 可能几秒), `swap_write_mb` 大, 但 `state_lost` / `work_fail` 仍应为 0 (没有 `memory.max`, 不会被杀); 如果 `quiet_bg_swap_growth_mb_per_min` 长期 > 0, 说明这个应用在后台一直在碰工作集, 静态上限只是在反复换入换出 (对比 `mild` 格的数字)。

### 6.9 三类应用实验: 呼叫与切换 / 计算型 / 下载型 (`scripts/ubuntu/run_classes.sh`)

一次短跑 (每格约 10 分钟) 同时回答三个问题 —— 把后台应用的资源压下去, 三类应用各付出什么代价。三类都用**真实应用**:

| 类别 | 应用 | 比什么 | 指标 |
|---|---|---|---|
| ① 呼叫 / 切换 (交互型) | 微信、QQ、WPS、Firefox (已登录) | 不限制时被呼叫多久响应 vs 被压制过以后多久响应; 切回后是否顺滑 | **响应** = 窗口激活 + 事件循环第一次应答 (P50 / P95 / 最大); **顺滑** = 切回后 1 s 内连续 5 次 `_NET_WM_PING` 的应答 (P95 / 最大, 超过 100 ms 记一次卡顿) + **输入回读** (全选 + 复制到剪贴板拿到文本的时间, 微信这类不应答 ping 的应用靠它); 内容核对 `work_fail` |
| ② 计算型 | `sevenzip` **7-Zip** (系统里装的 `7z`: 把约 4 MB 真实文件以默认级别 `-mx=5`、两线程压成 `.7z`, 再解压; 压缩、解压各算一个任务); `z3` **Z3 约束求解器** (系统里装的 `z3`: 每个任务解三个 SMT-LIB 问题 —— 24 皇后 / 9 鸽 8 笼 / 32 位半素数分解, 单线程, 约 1 s) | 同一个任务在前台做 vs 在后台做 | **单任务时延** P50 / P95, **每分钟任务数**, 后台 / 前台倍数, 后台做完 100 个任务要多少秒, 任务内 CPU 占比 (低 = 在等配额 / 等缺页) |
| ③ 下载型 | **百度网盘**、**迅雷** (已登录, 各带一个正在进行的大文件下载任务) | 在前台下 vs 在后台下 | **下载速度** MB/s (均值 / 最慢 10 %), **连接的往返时延** (内核 srtt), **内核收包积压** (数据已到、应用还没收走), 写盘速度 (旁证), 以及放在应用 cgroup 里的探针量到的**外网建连时延**和**处理延迟** (包到了以后应用等多久才轮到 CPU) |

**计算型怎么量** (`cf/linux/bench_app.py --kind sevenzip|z3|openssl|llama|tesseract|ffmpeg` + `cf/linux/tool_jobs.py`, 应用目录里 kind = `bench`; 后四种见下面 "只做响应 + 计算型"): 一个 X11 窗口 (runner 能把它切前台 / 压后台) + 一个循环 —— 每个任务都是**启动一次真实的 `7z` / `z3` 进程**, 它作为窗口进程的子进程跑在该应用的 cgroup 里, 所以 `cpu.max` / `cpuset` / `memory.high` 原样作用在真实程序上。每次运行的墙钟时间和程序自己的 CPU 时间 (`RUSAGE_CHILDREN`) 写进 `/tmp/cf_bench_<id>.jsonl`, runner 每步把新增的记录抄进 step 记录 (`step["bench"]`)。"算得慢" 和 "算错" 分得开: 7z 的退出码 (解压时校验 CRC)、z3 的输出必须是预先知道的 `sat` / `unsat` / `sat`, 否则记 `bad_jobs`。
- 7-Zip 的输入是从本机 `/usr/share/doc`、python 标准库、`/usr/bin` 等目录按路径顺序拷出来的约 4 MB 真实文件 (文本 / 源码 / 可执行文件各约三分之一, 放在 `/tmp/cf_bench_sevenzip.d/corpus`, 建一次后复用), 所以**同一台机器的三格压的是同一批文件**; 换机器文件不同, 绝对时间不可跨机器比。压缩这 4 MB 时 7z 自己要用约 65 MB 内存 (LZMA 的字典 + 哈希表; 解压只要约 10 MB), 这正是压缩对 `memory.high` 敏感的原因。
- 三个 Z3 问题由 `tool_jobs.py` 生成 (`/tmp/cf_bench_z3.d/*.smt2`), 分别是整数算术 (sat)、纯命题且对归结很难 (unsat)、位向量乘法 (sat), 合起来在开发用的 x86 云机上约 0.9 s (z3 4.8.12)。
- 不需要账号。没装工具时脚本会停下并逐个列出缺什么、怎么装 (`python3 -m cf.linux.tool_jobs --missing <配置>`; 7-Zip / Z3 `install_deps.sh` 已包含, 其余见 `scripts/ubuntu/install_compute_apps.sh`)。第一格开始之前脚本还会让每个计算应用**试做一个任务** (`python3 -m cf.linux.tool_jobs --check --config <配置>`, 约半分钟; `TOOLCHECK=0` 跳过): 装上了但跑不对的工具 (模型载不进来、缺编码器) 在这里就报 `FAILED`, 不用等跑完才从 `bad_jobs` 里发现。
- **计算应用的 m_fg 取它的 cgroup 的内存峰值** (`memory.peak`, 内核 ≥ 5.19), 不是预热结束那一刻的 PSS: 工具每个任务启动一次, 一次采样正好落在两个任务之间还是任务中间全凭运气 (7-Zip: 15 MB 或 85 MB; 大模型: 几十 MB 或整个模型), `memory.high = 比例 × m_fg` 就跟着乱跳。峰值是这个任务真正需要的量 (匿名内存 + 它读写的文件的页缓存, 正是 `memory.high` 管的那个数)。交互应用仍用 PSS。

**下载型怎么量**: 百度网盘 / 迅雷是闭源应用, 没法在里面记速度, 所以从内核和 cgroup 两头量, 应用本身不做任何改动:
- **下载速度 / 往返时延 / 收包积压** (`cf/linux/netstat.py`): runner 每秒执行一次 `ss -tin --cgroup -p`, 取出属于该应用的全部 TCP 连接 (按连接所属的 cgroup `/cf/<app>`, 或持有它的进程在该 cgroup 里 —— 应用在被收编进 cgroup 之前建的连接靠后一条), 回环连接 (应用自己的进程间通信) 不算。**速度** = 这一秒各连接 `bytes_received` 增量之和, 是应用真正从网络收到的字节; **往返时延** = 这一秒在收数据的连接的内核平滑 RTT (`rtt:`), 按字节加权取中位 —— 它是 "应用发出的请求多久得到对端应答" (网络路径 + 服务器应答时间), 只在应用发数据时更新; **收包积压** = 各连接 Recv-Q 之和 (数据已经到了内核、应用还没 `read` 走)。
- **外网建连时延 / 处理延迟** (`cf/linux/net_probe.py`): runner 在每个下载应用的 cgroup 下面建一个子 cgroup `/cf/<app>/cfprobe` (不开控制器, 所以应用的 CPU / 内存限制原样罩着它, 而应用自己的 `cgroup.procs` 里没有它), 放进去一个约 4 MB 的小探针进程, 每 0.5 s 做两件事: 记下自己比预定时刻**晚了多久**才被调度到 (= 处理延迟: 一个发给这个应用的包到了以后, 应用的线程要等这么久才轮到 CPU), 再向 `RTT_HOST` (默认 `www.baidu.com:443`) 发起一次 TCP 建连并计时 (= 一次 SYN / SYN-ACK 往返)。应用实际感受到的网络时延 ≈ 往返时延 + 处理延迟。`RTT_HOST=""` 不放探针。
- **写盘** = 同一时段该应用 cgroup 的磁盘写入量 / 时长 (runner 本来就记), 是独立于网络统计的旁证: 两者应大致相等; 迅雷走 UDP 的 P2P 流量 `ss -t` 看不到, 只会出现在这一列。
- **无流量的秒** = 速度低于 10 KB/s 的采样占比。高说明下载任务中途下完 / 暂停 / 被服务器掐了, 那一格的速度不可比。

**三格** (同一份配置 `configs/ubuntu_arm64_classes.json`, 走完全相同的访问顺序):

| 格 | 后台应用的限制 |
|---|---|
| `none` | 不限制 —— 基准, 表里 "相对不限制" 的列都以它为分母 |
| `mild` | 1 核 `cpu.max` 配额 + `cpu.weight` 20, `memory.high` = 0.5 × m_fg, 回收一次 (夜间矩阵的档位) |
| `squeeze` | 全部后台应用钉在最后一个核, `cpu.max` = 该核的 10 %, `cpu.weight` 1, `memory.high` = 0.1 × m_fg (不低于 32 MB), 切走时 `memory.reclaim` × 3 (§6.8 的激进档, 但不做周期性再回收) |

**访问顺序** (trace kind `pattern`, 不是随机轨迹, 所以短跑也可比): 每轮 12 步 `微信 → 7-Zip → WPS → 迅雷 → Firefox → Z3 → QQ → 百度网盘 → 微信 → WPS → Firefox → QQ`, 默认 2 轮 = 24 步, 每步停留 15 s。每个交互应用每轮被呼叫 2 次 (两次之间在后台待 1–2 分钟); 每个计算 / 下载应用每轮在前台 1 步、在后台 11 步 —— 前台 / 后台两种位置都有样本。

**跑之前**:

1. `sudo apt install -y p7zip-full z3` (或重跑 `sudo scripts/ubuntu/install_deps.sh`)。
2. 百度网盘、迅雷已登录, **各自开始一个大文件的下载并让它一直下着**: 量要够整个实验 (三格约 35 分钟, 例如限速 2 MB/s 就要 4–5 GB 以上的任务, 多排几个文件也行); 应用设置里有限速选项的话建议限到 2–5 MB/s —— 速度稳定, 三格才可比, 磁盘也不会被写满 (`df -h ~` 先看一眼, 脚本在剩余不足 10 GB 时会提醒)。脚本开跑前会用 `python3 -m cf.linux.netstat CONFIG --watch 6` 看 6 秒: 哪个下载应用没在跑、或者收流量不到 50 KB/s, 就停下来提示 (`DL_MIN_KBPS=0` 或 `STRICT=0` 跳过)。这条命令也可以随时手动跑, 确认采样看得到应用的连接。
3. 其余同 §6.8 的第 1–5 条 —— 交互应用已登录且 `python3 run_linux.py configs/ubuntu_arm64_classes.json --onboard-check` 全部 ready (或 `ONBOARD=1`); 退出实验之外的应用 (脚本会检查 `/cf` 下还有谁在跑, `STRICT=0` 可跳过); zram 用量过半先重置; 不能和别的矩阵同时跑; 跑的时候不要碰虚拟机。7-Zip / Z3 的窗口和探针由 runner 自己启动, 脚本结束时一起停掉; 百度网盘 / 迅雷不会被关, 下载任务在格与格之间继续。没装某个应用时可以 `EXTRA="--placeholders"` 先用占位程序顶替着把流程跑通 (占位的下载应用没有下载数据)。

```bash
scripts/ubuntu/run_classes.sh                        # none + mild + squeeze, 约 30 分钟; 结果在 results/arm64_classes
CELLS="none squeeze" scripts/ubuntu/run_classes.sh    # 只要 "不限制 vs 压制" 两格, 约 20 分钟
ROUNDS=3 DWELL=20 scripts/ubuntu/run_classes.sh       # 每个应用多呼叫几次 / 后台段更长 (时间按比例增加)
SEEDS="1 2" scripts/ubuntu/run_classes.sh             # 重复一遍 (真实下载的速度会随网络波动, 有余力时建议)
ONBOARD=1 scripts/ubuntu/run_classes.sh               # 先过一遍需要账号的应用的登录向导
RTT_HOST=223.5.5.5:443 scripts/ubuntu/run_classes.sh  # 探针建连的目标 (默认 www.baidu.com:443; 空串 = 不放探针)
python3 -m cf.linux.netstat configs/ubuntu_arm64_classes.json --watch 10   # 只看下载应用此刻的连接数 / 速度 / 往返时延
python3 -m cf.linux.classes results/arm64_classes/*.jsonl --out results/arm64_classes   # 只重新出表 / 出图, 不重跑
```

**上机时间**: 预热 8 个应用约 1.5 分钟 + 24 步 × (停留 15 s + 切换 / 探测 / 内容核对约 5 s) ≈ **10 分钟一格**; 三格约 30 分钟, 两格约 20 分钟。中断后同一条命令续跑, 已完成的格跳过。

**输出** (`$OUT/`): `classes_report.md` (三张表, 直接可读; 整列为空的列不显示), `classes_tables.xlsx` (三个 sheet + 说明, 中文表头), `classes_interactive.csv` / `classes_compute.csv` / `classes_download.csv` (完整的列; 计算型每个 (应用, 操作) 一行, 如 `sevenzip:compress` / `sevenzip:extract`), `plots/classes.png` (每类一行, 没有数据的类不画 —— 配置里去掉了下载型就只有两行: 响应时间 / 顺滑度, 单任务时延 / 每分钟任务数, 下载速度 / 网络时延与处理延迟), `plots/<run>_bench.png` (每个计算 / 下载应用随时间的单任务时延 / 下载速度 / 时延, 绿色底 = 它在前台), 以及通常的 `summary.csv` 和每格的 jsonl / log。运行时每步的日志行末尾也有 `| bench sevenzip 27 jobs compress 465 ms / extract 79 ms, z3 16 jobs 897 ms, xunlei 2.0 MB/s rtt 31.0 ms wake 0.1 ms`。

**怎么读**:

- ① 里 "响应" 是在限制**已经解除之后**量的 (runner 先把被请求的应用提回 fg 档再激活窗口), 所以它回答的是 "被压过的应用醒过来要多久" —— 被换出的页要缺页换回, 正卡在 `memory.high` 惩罚性休眠里的线程要等休眠结束。`n_limited` 是其中有几次是从受限的后台切回的; 第 0 步 (预热后的第一次切换, 那时还没有任何应用被限制) 和冷启动不计入。
- ② 里前台列是这个任务不受限时的速度; 后台列除以前台列就是限制的代价。前台列在各格**不一定相同**: `none` 格里前台任务要和同样不受限的后台计算 / 下载应用抢核, 压制的格里后台被赶到一个核的 10 % 上, 前台反而更快 (开发用的 4 核 x86 云机上 7-Zip 压缩前台 P50: none 779 ms, squeeze 439 ms; 核多的机器差别小) —— 这是压制后台给前台带来的好处, 也是表里另有 "后台相对不限制" 一列的原因 (和 none 格的同一任务比, 不受前台列变化的影响)。如果压制格的前台列反而**明显变慢**, 先看报告开头有没有 "前台应用仍被 cpuset 钉在核 N 上" 的提示和日志里的 `was not applied`。`squeeze` 下 CPU 配额是单核的 10 %: **Z3** 是纯 CPU 任务 (工作集约 40 MB), 后台时延的理论值就是前台的约 10 倍; **7-Zip 解压**只要约 10 MB 内存, 也是约 10 倍; **7-Zip 压缩**要约 65 MB, 超过 `memory.high` (32 MB) 以后每次分配都要先把别的页换出去, 时延会远高于 10 倍 —— 多出来的部分就是内存限制的代价, `任务内 CPU 占比` 掉到个位数说明时间几乎都花在等配额和等换页上。P95 远大于 P50 是 "偶尔被饿住" 的任务 (前台应用恰好也在用最后那个核, `cpu.weight 1` 的后台拿不到时间片)。压缩任务在 `squeeze` 下一个可能要几十秒, 一个后台段 (约 4 分钟) 只能做完几个, 样本少时看 `bg_jobs` 列, 需要更多样本就 `ROUNDS=3`。
- ③ 里**往返时延** (连接本身) 和**外网建连时延**的网络部分在各格应基本不变 —— cgroup 限制不改变链路; 变的是**下载速度** (收包、解密、校验、写盘都要 CPU, 页缓存也记在 `memory.high` 里)、**收包积压** (应用来不及收, 数据堆在内核里, 积压顶到接收窗口以后对端就被 TCP 流控压慢) 和**处理延迟** (`cpu.max` 10 ms / 100 ms 时, 一个包最多要等约 90 ms 应用才被调度到; 探针自己的页被换出时还要加上缺页的时间)。下载限速 (例如 2 MB/s) 低于应用在 10 % 核下的处理能力时, 后台速度可以不掉, 这时代价只体现在处理延迟和积压上 —— 这本身就是一个结论。
- **先看报告开头每格下面有没有 "注意"**, 以及日志里的 WARN —— 它们说的是这一格的数据能不能用:
  - "**X 的内存限制在这一格没有生效**": X 被压到后台时仍留着远多于 `memory.high` 的匿名内存, 而它的 cgroup 只记着几 MB。cgroup v2 里内存记在**分配它的时候进程所在的 cgroup** 上, 进程后来被移进 `/cf/<app>` 时账不跟着走, 所以 `memory.high` / `memory.reclaim` 够不着这些页。典型原因是应用启动时真正干活的进程离开了它的 cgroup (snap 包、交给桌面环境启动的包装脚本; 日志 `N matching process(es) outside its cgroup` / `had left its cgroup while it started`), 事后才被收养回来。这一格对 X 只有 CPU 限制, 它的响应时间不能写成 "内存被压过以后" 的结果; 处理办法见 §10 "应用的进程不在它的 cgroup 里"。
  - 日志 "**N app(s) were started by this warmup**": 应用是这次运行冷启动的, `m_fg` 是它启动后 `warmup_dwell_s` 秒时的占用, 通常远小于稳定后的占用 (一次实测: 微信冷启动后 6 s 是 252 MB, 用了几分钟后约 590 MB), 于是 `memory.high = 比例 × m_fg` 比设想的紧得多 (那次 `mild` 格的 "0.5 × m_fg" 实际只有稳定占用的约 21 %), 后面几格沿用第一格的 `m_fg` 也一样。**先把交互应用启动好** (`python3 run_linux.py <配置> --launch wechat qq wps firefox`, 或 `ONBOARD=1`), 用一两分钟、让它们稳定下来再跑; 这样它们也一定是在各自的 cgroup 里启动的。
- 判读用的不变量仍然是: `state_lost` 为空、`work_fail` = 0、`n_cold` = 0 (被压制只是慢, 不能丢状态); 计算型的 `bad_jobs` (程序报错或答案不对) 应为 0; 下载型的 `无流量的秒` 应接近 0。

**局限** (写报告时要交代):

- 下载速度只统计 TCP。迅雷的 P2P / uTP 走 UDP 的部分不在 "下载速度" 里, 看 "写盘" 列; 想让两列对得上, 选 HTTP / 网盘类的下载源。一个连接在两次采样之间收完数据并关闭时, 最后不到 1 s 的字节会漏掉 (长连接下载影响很小)。
- "往返时延" 是内核对**应用发出的数据**的估计 (srtt), 下载连接上应用只偶尔发请求, 所以它更新得慢, 且包含服务器的应答时间; 网络本身的时延以探针的 "外网建连时延" 为准 (目标是固定主机, 不是下载服务器)。
- 探针占用应用 cgroup 约 4 MB 内存和可忽略的 CPU; `memory.high` 只有 32 MB 的小应用会因此略紧一点, 三格都一样。
- 真实下载的速度随网络和服务器波动 (百度网盘非会员本身限速), 前台 / 后台的差别小于格内波动时不要下结论: 看 `速度 P10`、多跑一个 `SEEDS`, 或用下面的合成版做对照。

**只做响应 + 计算型, 计算应用更多** (`configs/ubuntu_arm64_compute.json`): 不含下载型 (没有要手工开始的下载任务, 计算应用不需要任何账号), 交互应用和三格的限制与上面完全相同, 计算型扩成六个真实程序:

| id | 程序 | 一个任务 | 表里的行 | 线程 | 怎么判对 | 安装 |
|---|---|---|---|---|---|---|
| `sevenzip` | 7-Zip | 压缩约 4 MB 真实文件, 再解压 | 压缩 / 解压 | 2 | 7z 退出码 (CRC) | `p7zip-full` |
| `z3` | Z3 SMT 求解器 | 三个约束问题 (24 皇后 / 鸽笼 / 半素数分解) | 1 行 | 1 | 答案是预先知道的 sat / unsat / sat | `z3` |
| `openssl` | OpenSSL (**加密解密**) | 一个 32 MB 文件: 口令加密 `enc -aes-256-cbc -pbkdf2 -iter 600000` → 解密并算 SHA-256 → RSA-3072 签名并验签 | 加密 / 解密 / 签名验签 | 1 | 解密结果的 SHA-256 等于原文件; `Verified OK` | 系统自带 |
| `llama` | llama.cpp (**本地大模型 / AI**) | 启动 `llama-completion`、载入 GGUF 模型 (默认 Qwen2.5-0.5B-Instruct Q4_K_M, 约 470 MB)、对同一个问题贪心生成恰好 64 个 token | 1 行 | 1 | 有文字输出 (≥ 20 个字符) | `install_compute_apps.sh --llama` |
| `tesseract` | Tesseract OCR (**AI 文字识别**, LSTM 网络) | 识别一页 A4 英文 (200 dpi, 约 400 个词, 启动时画好) | 1 行 | 1 | 至少认对 90 % 的词 | `tesseract-ocr` |
| `ffmpeg` | FFmpeg (视频转码) | 4 s 的 1280×720 视频解码 → 缩到 960×540 → libx264 (preset medium, crf 23) | 1 行 | 2 | 退出码 + 输出文件 | `ffmpeg` |

```bash
scripts/ubuntu/install_compute_apps.sh                    # apt: p7zip-full z3 openssl tesseract-ocr ffmpeg (+ python3-pil, fonts-dejavu-core)
scripts/ubuntu/install_compute_apps.sh --llama --check    # 再加 llama.cpp (源码编译 llama-completion, 约 3–10 分钟) 和模型 (约 470 MB), 最后每个任务试做两遍
python3 -m cf.linux.tool_jobs                             # 六个工具各自: ok + 路径, 或 MISSING + 缺什么 + 怎么装
python3 -m cf.linux.tool_jobs --check [llama z3 …]        # 试做 (默认每个 2 遍, 打印每个操作的墙钟 / CPU 时间)

CONFIG=configs/ubuntu_arm64_compute.json OUT=results/arm64_compute scripts/ubuntu/run_classes.sh                          # 三格, 每格约 10 分钟
ROUNDS=8 SEEDS="1 2 3" CONFIG=configs/ubuntu_arm64_compute.json OUT=results/arm64_compute_night scripts/ubuntu/run_classes.sh   # 过夜: 9 格, 每格约 35 分钟
```

- **llama.cpp 和模型放在哪**: `~/.local/share/context_freeze/llama/` (`llama.cpp/build/bin/llama-completion` 和 `models/*.gguf`, 按文件名取第一个; `CF_DATA_HOME` 可改根目录)。安装脚本用**普通用户**跑 (只有 apt 那一步调 sudo); 编译的版本固定为 `LLAMA_TAG=v0.9.0` (任务用的命令行参数是按这个版本核对过的), 源码来自 GitHub (慢就先 `export https_proxy=…`, §2.5, 或 `LLAMA_REPO=<镜像>`); 模型依次试 ModelScope → hf-mirror.com → huggingface.co, 也可以 `MODEL_URL=<任意 .gguf 的地址>` 或自己把一个 `.gguf` 拷进 `models/`。已有现成的可执行文件 / 模型: `export CF_LLAMA_BIN=/path/llama-completion CF_LLAMA_MODEL=/path/x.gguf`。换更大的模型 (例如 Qwen2.5-1.5B, 约 1 GB) 任务更慢、内存压力更大, 注意虚拟机的内存。
- **线程数的取舍**: 六个应用合计 8 个计算线程 (2 + 1 + 1 + 1 + 1 + 2), 正好是 8 核虚拟机的核数 —— `none` 格里后台的计算应用彼此不怎么抢核, 基准才干净; 核更少的机器上 `none` 格本身就是过载的, 各应用的基准时间会按比例变长。大模型**故意只用 1 个线程**: llama.cpp 的工作线程互相等待时是忙等的, 两个线程在核不够用 (或被 cpuset 钉到一个核上) 时一个 4 s 的任务要 1–3 分钟、白烧 30–60 s 的 CPU —— 这是它线程池的性质, 会盖过要量的限制本身; 1 个线程时不管机器忙不忙 CPU 时间都是约 5 s。Tesseract 必须 1 个线程 (`OMP_THREAD_LIMIT=1`, 任务里已设): 它的 OpenMP 构建线程一多反而慢上百倍。要改线程数: 配置的 `app_overrides` 里给该应用的 `cmd` 末尾加 `--threads N`。
- **大模型的内存是页缓存**: 470 MB 的模型文件被 mmap 进来, 算在**第一次读它的 cgroup** 头上。应用启动时先把模型从页缓存里丢掉 (`posix_fadvise(DONTNEED)`), 让它在应用自己的 cgroup 里重新读入 —— 否则之前在别处 (试做、上一次运行) 读过的模型不归这个应用, `memory.high` 管不到。它的 m_fg (cgroup 峰值) 因此约为 800 MB (模型 + 约 300 MB 匿名内存), `mild` 的 `memory.high` 约 400 MB, `squeeze` 约 80 MB, 都**小于模型本身**: 后台的每个任务都要把模型从磁盘重新读一遍, 时延主要花在缺页和回收上。这正是 "把本地 AI 推理压到后台" 的代价, 但也意味着 `squeeze` 下它一个任务可能要几分钟, 一格里只有个位数的样本 —— 想要更多样本就加 `ROUNDS` / `DWELL`。
- **访问顺序**: 每轮 14 步 `微信 → 7-Zip → WPS → Z3 → Firefox → OpenSSL → QQ → 大模型 → 微信 → 文字识别 → WPS → 视频转码 → Firefox → QQ`; 每个交互应用每轮被呼叫 2 次, 每个计算应用每轮在前台 1 步、在后台 13 步。预热时每个应用停 10 s (大模型要把模型读进来并做完第一个任务, 峰值才是对的)。
- **只想跑其中几个**: 从配置的 `apps`、`classes.compute`、`trace.pattern` 三处删掉不要的 id (没装工具的应用脚本会点名; `STRICT=0` 时 runner 自己把它丢掉继续跑)。
- 任务的输入都是启动时在 `/tmp/cf_bench_<id>.d/` 里现做并保留的: 32 MB 的待加密文件 (由 7-Zip 那批真实文件拼成)、RSA 密钥、画好的那页文字、一段 Mandelbrot 缩放的视频 (每帧都有细节和运动, 不用下载素材)。同一台机器的各格用的是同一份输入; 换机器绝对时间不可比。

**三类全量: 响应 + 计算 + 下载, 13 个真实应用** (`configs/ubuntu_arm64_full.json`): 交互 4 个 (微信 / QQ / WPS / Firefox) + 上面的 6 个计算应用 + 3 个下载应用 —— **百度网盘**、**迅雷** (登录后各带一个下载任务) 和 **`browser_dl` 浏览器下载** (第二个 Firefox, 自己开始、自己续上下载, 不用账号)。三格的限制同上; 每轮 17 步 `微信 → 7-Zip → WPS → 迅雷 → Firefox → Z3 → QQ → 百度网盘 → 微信 → OpenSSL → WPS → 大模型 → Firefox → 浏览器下载 → QQ → 文字识别 → 视频转码`, 默认 2 轮, **约 15 分钟一格**。

```bash
CONFIG=configs/ubuntu_arm64_full.json OUT=results/arm64_full scripts/ubuntu/run_classes.sh                                    # 三格, 约 45 分钟
ROUNDS=6 SEEDS="1 2 3" CONFIG=configs/ubuntu_arm64_full.json OUT=results/arm64_full_night scripts/ubuntu/run_classes.sh        # 过夜: 9 格 x 约 40 分钟 = 约 6 小时
python3 -m cf.linux.netstat configs/ubuntu_arm64_full.json --watch 10 --minutes 360    # 三个下载应用此刻的速度, 以及按这个速度 6 小时要写多少盘
```

**下载型三个应用怎么配置** (一次配好, 以后每次跑之前只要确认任务还在下):

*磁盘先算账.* 百度网盘和迅雷是真的把文件写到盘上: **要写的量 (GB) ≈ 速度 (MB/s) × 小时数 × 3.5**。6 小时: 0.5 MB/s → 约 11 GB, 1 MB/s → 约 21 GB, 2 MB/s → 约 42 GB。浏览器下载只占一个文件 (4.8 GB, 下完即删)。先 `df -h ~` 看剩多少, 再决定两个应用各限速多少; 60 GB 的盘 (系统 + 应用占掉二十来 GB) 建议两个应用**各限到 0.5–1 MB/s**。任务量也按这个数准备: 实验期间任务下完了, 那个应用后面的格就没有数据 (报告里 "无流量的秒" 会很高)。脚本开跑前会看 6 秒三个应用的实际速度, 按本次运行的总时长算出要写多少, 放不下 (留 5 GB 余量) 就停下并说明 (`DL_DISK_CHECK=0` 跳过)。**不想留这么多盘**: 用下面的 "边下边删" (`DL_TRIM=1`), 盘上只放正在下的文件; 要下的文件用下面的 `dl_files` 现查。

*启动方式.* 实验里的应用都要**用 runner 启动** (这样进程才在各自的 cgroup `/cf/<id>` 里), 不要点桌面图标:

```bash
python3 run_linux.py configs/ubuntu_arm64_full.json --launch baidunetdisk xunlei      # 启动到 /cf/baidunetdisk、/cf/xunlei 后退出; 然后在窗口里登录、建任务
python3 run_linux.py configs/ubuntu_arm64_full.json --launch wechat qq wps firefox    # 交互应用同理 (登录, 用一两分钟)
```

1. **百度网盘** (`baidunetdisk`; arm64 是星火应用商店 / 统信商店的官方构建 `com.baidu.baidunetdisk`, §4.4):
   - 登录: 手机百度网盘扫码 (或手机号 + 验证码)。
   - 准备要下的文件: 网盘里要有总量够大的文件 (按上面的公式; 一个大文件或几个文件都行)。自己没有就把别人分享的大文件 "保存到我的网盘", 或者从别的电脑先传几个大文件上去。
   - 设置 → 传输: 下载目录放在空间够的盘上 (默认在 `$HOME` 下); **同时下载任务数设为 1** (文件一个接一个下, 速度才平稳); 有 "下载限速" 一项就按上面算好的数填 (非会员本身被限在几百 KB/s 到 1 MB/s 左右, 没有这一项也可以不管)。
   - 选中文件 → 下载, 停在 "传输列表 / 正在下载" 页, 窗口不要关也不要最小化到托盘。
2. **迅雷** (`xunlei`; 星火应用商店的 `com.xunlei.download`, §4.4):
   - 登录: 手机号 + 验证码或手机迅雷扫码 (不登录不能新建任务)。
   - 设置里: **同时下载任务数 1**; **下载限速**按算好的数填 (各版本菜单名字略有不同, 在 "设置中心 → 下载设置 / 速度设置" 里找); 下载目录放在空间够的盘上。
   - 新建任务, 粘贴 **HTTP(S) 链接** (不要用磁力 / BT: 那部分流量走 UDP, `下载速度` 一列量不到, 只会出现在 "写盘" 列)。可以直接用镜像站的系统镜像, 一次把几个链接都粘进去排队, 例如 (共约 12.7 GB, 1 MB/s 能下 3.5 小时; 不够就到 `https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/` 的各版本目录里再拷几个 `.iso` 的链接):
     ```
     https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/20.04/ubuntu-20.04.6-desktop-amd64.iso
     https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/22.04/ubuntu-22.04.5-live-server-amd64.iso
     https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/20.04/ubuntu-20.04.6-live-server-amd64.iso
     https://mirrors.bfsu.edu.cn/ubuntu-releases/22.04/ubuntu-22.04.5-desktop-amd64.iso
     ```
   - 停在任务列表页。
3. **浏览器下载** (`browser_dl`, `cf/linux/browser_dl.py`) —— 不用登录, 不用手工开始, 只要求系统里的 Firefox 是 **deb 版**:
   ```bash
   readlink -f "$(command -v firefox)"          # 应是 /usr/lib/firefox/firefox; 指向 /snap/... 或 /usr/bin/snap 就是 snap 版
   sudo scripts/ubuntu/install_cn_apps.sh --firefox     # 是 snap 版时: 换成 Mozilla APT 仓库的 deb (§0 第 4 步的同一套命令)
   python3 -m cf.linux.browser_dl --check       # 三行: 用哪个 Firefox、哪个下载源答应了、限速多少
   ```
   它做的事: 用**独立的 profile** (`~/.local/share/context_freeze/browser_dl/profile`) 和**独立的窗口类** (`cf-browser_dl`) 再开一个 Firefox —— 和交互型的那个 `firefox` 是两个应用、两个 cgroup, 互不影响, 你自己的书签 / 登录状态不会被动到。窗口里是本机的一个说明页, 页面每 2 秒问一次 "有没有要下的文件", 有就让一个隐藏的 frame 去打开那个链接 —— 和你点一个下载链接完全一样, 文件由 **Firefox 自己的下载管理器**收、解密、写盘 (存到 `~/.local/share/context_freeze/browser_dl/downloads`)。文件下完就删掉重下, 所以跑多久都只占一个文件的空间; 卡住 5 分钟没进度、或者某个源给了链接却没开始下, 就换下一个源。
   - **下载源**: 默认是几个国内镜像站上的同一个 Ubuntu 镜像 (4.8 GB; 清华 → 北外 → 南大 → 华为云 → 浙大 → releases.ubuntu.com, 用第一个答应的)。换成别的文件: `export CF_BROWSER_DL_URL="https://…/big.iso"` (可以空格分隔给几个)。
   - **限速**: Firefox 自己没有限速功能, 而不限速的话一晚上会从镜像站拉走几百 GB, 速度也不稳。所以默认让它的 HTTPS 连接经过同一进程里的一个小中继 (HTTP CONNECT 隧道, 不解密, 只是按 **2 MB/s** 把字节转交给浏览器; 中继在应用自己的 cgroup 里, 受同样的限制)。改速度 `export CF_BROWSER_DL_RATE_MBPS=4`; `=0` 是不经中继、有多快下多快 (这时测到的是浏览器自己的连接, 对 CPU 限制最敏感, 但流量很大, 只建议短跑时用)。两个变量都要在**启动它之前**设好 (`run_classes.sh` 发现它没在跑会自己启动它)。
   - 经中继时表里这个应用的 "往返时延 / 收包积压" 是中继那条外网连接的: 限速靠 "收得慢" 实现, 所以积压一直接近接收窗口, 各格之间只比**下载速度**和**处理延迟**; 不限速 (`=0`) 时这两列才是浏览器自己的连接。
   - 预期: 2 MB/s 的下载占不满 squeeze 格给后台应用的 10 % 核。在一台 x86 云主机上试跑 (1 轮, none + squeeze 两格, 其余两个下载应用是占位) 时它在 squeeze 格的后台速度仍是 2.0 MB/s (不限制的 100 %), 变的只有处理延迟 (P95 2.8 → 37 ms)。想看 CPU 限制把下载压慢到什么程度, 把限速提高到限制够得着的量级 (`CF_BROWSER_DL_RATE_MBPS=8` 或更高, 流量和镜像站的负担也相应变大) 或设 0。
   - `run_classes.sh` 结束时会把它关掉 (否则它会一直下); 单独启动 / 停止: `python3 run_linux.py configs/ubuntu_arm64_full.json --launch browser_dl`, `pkill -f "cf.linux.browser_dl --app"`。日志在 `results/app_logs/browser_dl.log` (每次开始 / 下完 / 换源各一行)。
4. **确认三个都在下**, 并看磁盘够不够:
   ```bash
   python3 -m cf.linux.netstat configs/ubuntu_arm64_full.json --watch 10 --minutes 360
   # app              running  sockets     MB/s   rtt ms
   # baidunetdisk     yes            3    0.512     28.0
   # xunlei           yes            2    1.004     12.5
   # browser_dl       yes            5    1.998      7.2
   # at these speeds, in the 360 min of the run: baidunetdisk writes about 10.8 GB, xunlei writes about 21.2 GB, browser_dl re-uses one file (4.8 GB); 41 GB free in /home/u
   ```
   `running` 是 `NO` = 应用不在它的 cgroup 里 (是点图标启动的: 退出后用上面的 `--launch` 重开); `sockets` 是 0 或速度是 0 = 任务没在下 (暂停了 / 下完了 / 走的是 P2P)。`browser_dl` 没在跑时这条命令看不到它, 先 `--launch browser_dl` 等半分钟。

**下载用的测试文件** (`python3 -m cf.linux.dl_files`): 迅雷和百度网盘都需要 "够下一整晚" 的文件。这个命令读清华 / 北外镜像站的目录页 (发行版的版本号会变, 目录不变), 把里面 0.7–3.3 GB 的安装镜像逐个问一遍 (HEAD: 文件在、多大), 列出此刻确实能下的链接:

```bash
python3 -m cf.linux.dl_files                                   # 全部 (写这一节时 15 个文件, 共 33 GB), 小的在前
python3 -m cf.linux.dl_files --rate-mbps 1 --minutes 360       # 1 MB/s 下 6 小时需要的量 (多给 10 %)
python3 -m cf.linux.dl_files --gb 21 --plain                   # 只要链接, 一行一个 (整段粘进迅雷)
python3 -m cf.linux.dl_files --mirror https://mirrors.bfsu.edu.cn --skip 8 --gb 12 --plain    # 换镜像站、从第 8 个文件开始: 给第二个应用不一样的文件
#  0.74 GB  https://mirrors.tuna.tsinghua.edu.cn/debian-cd/current/amd64/iso-cd/debian-13.7.0-amd64-netinst.iso
#  1.23 GB  https://mirrors.tuna.tsinghua.edu.cn/archlinux/iso/latest/archlinux-2026.11.01-x86_64.iso
#  1.39 GB  https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/20.04/ubuntu-20.04.6-live-server-amd64.iso
#  1.99 GB  https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/22.04/ubuntu-22.04.5-live-server-amd64.iso
#  2.72 GB  https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/26.04/ubuntu-26.04-live-server-amd64.iso
#  ...
# 12 links, 23.7 GB in total (15 different files, 33.2 GB); largest 3.08 GB; asked for 23.2 GB
```

要的总量超过全部不同文件之和时, 列表从头再来一遍, 链接后面加 `?n=2`、`?n=3` …: 镜像站不看查询串, 给的是同一个文件; 对下载软件来说是另一个链接。`--max-gb` 决定单个文件最大多大 (边下边删时, 盘上同时最多就是这么大的一个文件)。

- **放进迅雷**: 新建任务 → 把 `--plain` 的输出整段粘进去 (一行一个链接, 迅雷会识别成多个任务) → 立即下载。设置里 "同时下载的最大任务数" 是 1 时, 它们一个接一个下。同一个文件名第二次出现 (`?n=2` 那几条) 时迅雷若提示任务已存在 / 文件重名, 选 "重新下载" 或 "重命名"; 实在加不进去就等前面的下完 (边下边删会把文件删掉) 再加一遍。
- **放进百度网盘 (离线下载)**: 不用自己上传 —— 让百度的服务器去镜像站取。网页版 `pan.baidu.com` 或客户端里找 **离线下载** (新版在左侧 / "更多" 菜单里; 旧版在 "上传" 按钮旁边) → **新建链接任务** → 粘贴**一条** http(s) 链接 → 选保存到网盘里的一个新文件夹 (例如 `cf_test`) → 确定; 一条一条加完。服务器取完后 (镜像站的常见文件多半是 "秒离", 立刻完成), 在虚拟机的客户端里选中 `cf_test` 整个文件夹 → 下载。离线下载对普通账号有每天的任务数 / 单文件大小之类的限额, 各时期不同; 加不进去时的替代办法: 在 Mac 上把这些链接下下来再用网页版上传, 或者把别人分享的大文件 "保存到我的网盘"。同一个文件想多下几遍: 在网页版里把它复制几份 (复制不占流量, 占网盘容量)。
- 两个应用最好用**不同的文件** (上面的 `--skip` / `--mirror`): 迅雷直接从镜像站下, 百度网盘从百度的服务器下, 互不相干, 但文件不同可以避免你在下载目录里分不清是谁的。

(离线下载和迅雷的 "重复链接" 两处是按客户端的通常行为写的, 没有在真实客户端上验证; 链接本身每次由命令现查。)

**磁盘不够大: 边下边删 (`DL_TRIM`)**。百度网盘和迅雷下完的文件, 实验并不需要 —— 需要的只是 "应用一直在收数据、写盘"。`DL_TRIM=1` 时 `run_classes.sh` 在实验期间开一个清理进程 (`cf/linux/dl_trim.py`, 跑在应用的 cgroup 之外, 它的 CPU 不记在被测应用头上), 每 15 秒看一眼这两个应用的进程打开着哪些文件:

- 只跟踪**亲眼见到该应用正在写**的文件 (≥ 64 MB; 不在隐藏目录 / 系统目录里 —— 那是配置、缓存、数据库)。按 inode 跟踪, 所以下载结束时客户端把 `x.iso.xltd` / `x.iso.baiduyun.p.downloading` 改名成 `x.iso` 也跟得住。
- 一个被跟踪的文件, 该应用的所有进程都已经 30 秒没有打开它, 名字又不再是 "下载中" 的临时名 (`.xltd` `.td` `.downloading` `.part` `.crdownload` `.tmp` …, 旁边也没有 `x.iso.td.cfg` / `x.iso.aria2` 这类进度文件) → 下完了, **删掉**。暂停的任务 (文件关了, 名字还是临时名) 不动。
- **下载目录里你自己的文件不会被碰**: 没被它看到 "正在被该应用写" 的文件 (实验开始前就下完的、别的程序放进去的) 永远不删。

这样盘上只需要放得下**正在下的那几个文件**, 而不是 "速度 × 时间": 两个应用各自 "同时下载任务数 1"、单个文件 ≤ 3.3 GB 时, 一共不到 7 GB (加上浏览器下载的那一个文件); 总下载量不变, 只是不留在盘上。所以任务要**多而小** (上面 `dl_files` 列的就是), 不要一个几十 GB 的大文件。开了 `DL_TRIM` 后开跑前的磁盘检查不再按 "速度 × 时间" 算。

```bash
python3 -m cf.linux.dl_trim configs/ubuntu_arm64_full.json --list       # 现在两个应用各在写哪个文件 (= 它认到的下载文件; 没有就是没在下, 或下载目录是隐藏目录: --dir)
python3 -m cf.linux.dl_trim configs/ubuntu_arm64_full.json --dry-run    # 先空跑看几分钟: 只报告 "would be removed", 什么都不删 (Ctrl+C 结束)
DL_TRIM=1 ROUNDS=6 SEEDS="1 2 3" CONFIG=configs/ubuntu_arm64_full.json OUT=results/arm64_full_night scripts/ubuntu/run_classes.sh
tail -f results/arm64_full_night/dl_trim.log                            # 每删一个文件一行, 每 10 分钟一行合计和剩余空间
```

`DL_TRIM=punch` 更进一步: **正在下的文件**已经写下去的部分也每 15 秒还给文件系统 (`fallocate` 打洞: 文件长度不变, 客户端照常在它的偏移上接着写, 盘上只留最近十几秒收到的数据), 这时连 "一个文件" 的空间都不需要, 浏览器下载的 4.8 GB 也一并省掉。代价: 写过的部分再读出来是全零 —— 客户端如果在任务结束时校验文件, 会判它损坏 (报错、重下, 或者照样算完成, 取决于客户端)。**长跑之前先用一个任务试 10 分钟**, 看任务结束时客户端怎么反应:

```bash
python3 -m cf.linux.dl_trim configs/ubuntu_arm64_full.json --apps xunlei --punch --for 600      # 只对迅雷, 10 分钟; 同时 watch -n5 du -sh ~/下载目录
```

验证过的和没验证的: 在云主机上用 aria2 当下载客户端放进 `/cf/xunlei` (三个文件顺序下载), `DL_TRIM=1` 时每个文件下完约 6 秒 (当时 grace 设的 6 秒) 被删、目录里别的文件原样留着; `punch` 时盘上占用多数时候是十几 MB (aria2 给下一个文件预分配空间的那一下除外); 真实的 Firefox (`browser_dl`) 在 `punch` 下照常下完、接着下一个, 下载目录一直不超过 24 MB。**百度网盘和迅雷的真实客户端没有试过** —— 它们的临时文件名、下完后是否还要读文件 (校验 / 做种)、文件被删后任务列表怎么显示, 都以你虚拟机上 `--list` / `--dry-run` 看到的为准; 临时名不在上面的清单里时, 清理进程会把 "暂停后关闭的未完成文件" 当成下完的删掉 (任务会从头再下), 这时把后缀告诉我或加进 `TEMP_SUFFIXES`。

对测量的影响: 下载速度、网络时延、"写盘" 列都不变 (数据照收照写)。删文件 / 打洞会顺带丢掉这些文件的页缓存, 应用 cgroup 里记着的文件缓存少一些 —— 对 `memory.high` 来说这部分本来就是最先被回收的, 预期影响很小, 但没有单独量过; 要严格对比的两次运行请用同一个 `DL_TRIM` 设置。清理进程只管**它运行期间**见到的文件: 中断后重跑, 中间那段时间下完的文件会留在盘上, 手工删一下。

**下载型只用其中一两个应用 (`DL_APPS`)**: 三个下载应用不必都配。`DL_APPS` 把下载型缩到指定的应用, 其余的从应用表、三类表和访问顺序里一起拿掉 (`cf/linux/subset.py`; 缩减后的配置写到 `$OUT/config.json`, 限制、停留时间等都不变):

```bash
DL_APPS="xunlei" CONFIG=configs/ubuntu_arm64_full.json OUT=results/arm64_full_xunlei scripts/ubuntu/run_classes.sh        # 下载型只有迅雷: 11 个应用, 每轮 15 步, 约 13 分钟一格
DL_APPS="xunlei browser_dl" …      # 迅雷 + 浏览器下载        DL_APPS="browser_dl" …      # 只有浏览器下载 (不用账号)        DL_APPS="none" …      # 不要下载型
```

被拿掉的应用如果还开着 (比如百度网盘), 脚本会说 "running but not part of this experiment" 并停下: 先把它退出。同一个 `OUT` 续跑时要带同样的 `DL_APPS`。**百度网盘没有限速选项**时两条路: 不管它 (普通账号的速度本来就被服务器限着; 配 `DL_TRIM=1` 不占盘), 或者用 `DL_APPS` 不测它。

**迅雷任务加不进去 / 一直 "正在寻找资源" / 速度是 0** —— 按这个顺序查 (每一步都是 "有问题就先修这一步"):

```bash
df -h ~                                              # ① Avail 是 0: 盘满了, 迅雷建不了文件 (设置页里 "剩余空间 0 MB") -> 先腾空间 / 扩文件系统
python3 -m cf.linux.netstat configs/ubuntu_arm64_full.json --apps xunlei --watch 10
#   running NO                 ② 迅雷不在 /cf/xunlei 里 (点图标启动的): 退出, 用 run_linux.py … --launch xunlei 重开
#   "xunlei is limited right now (cpu.max 10 % of a core, memory.high 32 MB)"
#                              ③ 上一次实验被强行中断, 限制还留在它身上 (只剩 10 % 的一个核, 迅雷连资源都找不动):
python3 run_linux.py configs/ubuntu_arm64_full.json --release            #    解除全部限制 (run_classes.sh 正常 / 异常结束时都会自己做这一步)
#   sockets 0                  ④ 它一条 TCP 连接都没建起来: 网络问题, 见下
#   sockets > 0, MB/s 0        ⑤ 连上了但没有数据: 多半是源站不给 / 任务在排队, 换链接
curl -L -o /dev/null -m 20 -w '%{http_code} %{speed_download} B/s\n' "<任务的链接>"      # ④ 虚拟机自己能不能下这个文件: 应是 200 和一个不小的速度
```

- curl 也下不动: 是虚拟机的网络 (DNS / 网关 / 只有走代理才能出去)。**迅雷不读 `http_proxy` 之类的环境变量**, 虚拟机必须能直连外网。
- curl 能下、迅雷找不到资源: 是客户端这一侧。Linux 版迅雷加任务后先去问迅雷自己的资源服务器, 再连原始地址; 校园网 / 公司网常把迅雷的服务器或 P2P 端口封掉, 这时任务就停在 "寻找资源"。可以试: (a) 同一个文件换成 **http://** 链接、换一个站 —— `python3 -m cf.linux.dl_files --mirror https://repo.huaweicloud.com --http --plain` (华为云镜像, 明文 http; 只列出确实不会被跳回 https 的文件); (b) 先只加**一个**任务看它能不能开始, 不要一次粘十几条; (c) 换一个你确知迅雷下得动的文件 (内容是什么不影响实验); (d) 确认已登录。
- 都不行: 下载型改用 `DL_APPS="browser_dl"` (浏览器下载不依赖任何客户端的服务器), 迅雷以后再说。

(以上是按现象列的排查顺序; 我们没有在真实的迅雷客户端上复现过 "寻找资源", 哪一步命中请以你机器上这几条命令的输出为准。)

**两个 Firefox 是怎么分开的**: 进程正则分不开它们 (内容进程的命令行里没有 profile), 所以 (a) 窗口按类名区分 —— 交互型的 `firefox` 只认类名 `firefox` (每个 Firefox 窗口的实例名都是 `Navigator`, 不能用它), 下载用的那个启动时带 `--class cf-browser_dl`; (b) 进程按 cgroup 和父子关系区分 —— 已经在别的应用的 cgroup 里的进程不会被 "收养" 走, 带 `--class cf-…` 的进程及其全部子进程也永远不算 `firefox` 的 (目录条目的 `not_under`)。

**合成版** (`configs/ubuntu_arm64_classes_synthetic.json`): 计算型和下载型换成仓库自带的可控程序 —— `bench_numeric` (LU 分解求解 + FFT + n 体积分, 一个工作进程)、`bench_compress` (zlib -6 压 8 MB 再解压校验, 两个工作进程)、`bench_download` (从本机限速源做 HTTP Range 分片下载, 每片 sha256 后写盘, 自带建连探测)。不用装工具、不用账号、不用准备下载任务, 下载速度上限固定 (默认 20 MB/s), 可以作为真实应用结果的对照或在外网不稳时使用:

```bash
CONFIG=configs/ubuntu_arm64_classes_synthetic.json OUT=results/arm64_classes_syn scripts/ubuntu/run_classes.sh   # 约 9 分钟一格
DL_RATE_MBPS=10 RTT_HOST=223.5.5.5:443 CONFIG=configs/ubuntu_arm64_classes_synthetic.json scripts/ubuntu/run_classes.sh
DL_URL=https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/22.04/ubuntu-22.04.5-desktop-amd64.iso CONFIG=configs/ubuntu_arm64_classes_synthetic.json scripts/ubuntu/run_classes.sh
```

本机限速源 (`python3 -m cf.linux.bench_app --serve 18080 --rate-mbps 20`) 由脚本启动, 跑在脚本自己的 `/cf/host` 里, 不受限制; 这时 "往返时延" 是 `bench_download` 自己向下载源 TCP 建连的耗时 (回环 ≈ 0.1 ms), 处理延迟由它自己的探测线程量。给 `DL_URL=` 则下载一个真实的大文件 (要支持 Range)。下载的数据循环写进 `/tmp` 下一个 256 MB 的临时文件 (退出时删除)。

### 6.7 日常使用: `focusd`

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
`cf.analyze` 原样可用; 新增 `tiers`, `bg` (每应用的 Δcpu / Δthrottled / Δwrite / 通知 / cg 内存), `state_lost`, `notifications`,
`dwell_s`, `timeline`, 以及 `--work` 时的 `work` 与 `launch.work_state_ok` / `launch.work_check`。

```bash
python3 -m cf.linux.analyze results/ubuntu_matrix/*.jsonl                       # 表格
python3 -m cf.linux.analyze results/ubuntu_matrix/*.jsonl --csv s.csv --agg agg.csv --pareto p.csv --plot pareto.png
python3 -m cf.linux.analyze results/ubuntu_matrix/*.jsonl --per-app              # 每个应用一行 (打印; 给文件名则写 CSV)
python3 -m cf.linux.analyze results/ubuntu_matrix/*.jsonl --plots-dir plots --md report.md   # 图 + Markdown 报告
```

`--plots-dir` 生成: 每格一张 `<name>.png` (后台 PSS vs 预算 / 每步切换时延按来源 fg·bg·shrunk·cold 着色 / 后台核数 + 节流 % /
swap in·out + 下载 MB + 通知), `sample_every_s > 0` 时再加 `<name>_timeline.png` (停留期间的每应用 cgroup 内存堆叠、后台核数、PSI、SwapFree),
以及跨格的 `latency_cdf.png` (每策略: 从 bg 回来 vs 从 shrunk 回来的时延 CDF) 和 `bg_tradeoff.png` (相对 `none` 基线的后台内存节省 % vs P95 时延)。
`--md` 把不变量 (state_lost / oom / n_cold 合计)、每格表、复本聚合 (mean ± sd)、day 轨迹分段、每应用表、不可达 η 提示和图链接写成一份 report.md。
`--per-app` 的列: `n_req n_cold lat_bg_p50_ms lat_shrunk_p50_ms ping_shrunk_p50_ms m_fg_mb pss_bg_avg_mb pss_shrunk_avg_mb
mem_bg_avg_mb mem_shrunk_avg_mb t_fg_s t_bg_s t_shrunk_s cores_bg_avg throttled_pct write_mb notifs cpu_s majflt state_lost work_checks work_fail`
(`pss_*` = 进程映射的 PSS+SwapPss; `mem_*` = cgroup `memory.current`, 含应用写出的页缓存, 下载类应用的 `mem_bg` 会明显大于 PSS,
被 `memory.high` 压到 shrunk 时这些缓存页先被丢掉) — 用来回答 "微信在 shrunk 里待了多久、
切回来平均多慢、这期间推了几条通知"、"百度网盘在后台 1 核下写了多少 MB" 这类按应用的问题。

**`mem_saving_pct` / `achieved_eta` (summary_agg, pareto.png) 的分母是 Σ m_fg = warmup 时各应用前台的 PSS+SwapPss, 分子 `bg_pss_avg_mb`
只算驻留的 PSS。** 内存紧张的机器上 (8 GB 跑 13 个应用) 内核本来就把一半冷页压进 zram, 所以 `none` 基线也会显示 ~50% "节省" —
这一列只能看策略之间的差, 不能当绝对节省; 比内存请看 `bg_pss_avg_mb` 的绝对值、`bg_tradeoff.png` (相对 `none` 基线的 %)、
`zram_phys_avg_mb` 和 `swap_write_mb`。一格 7 分钟写了几个 GB swap 而 `bg_pss_avg_mb` 不比 `none` 低, 就是 `memory.high` 在空转
(见 §10 "`memory.high` 太低")。

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
| `state_lost_events` / `oom_kills` | 进程 / 窗口消失、OOM kill、脚本化操作的内容丢失 | **必须为 0** |
| `work_checks` / `work_fail` | `--work` 时: 应用回到前台时做过内容核对的次数 / 不一致 (`work_state_ok = False`) 的次数 (§6.6) | `work_fail` **必须为 0**; `work_checks` 接近 T − n_cold − 首次访问数 |
| `swap_write_mb` / `swap_read_mb` / `refaults` | zram 换出 / 换入 | 换入 = WARM 切换的代价 |
| `psi_cpu_some_ms` | 全系统 CPU 压力 | 前台卡顿的旁证 |
| `cpu` / `mem_frac` / `cpu_throttle_resident` / `substitutions` | 本格的限流档位与替代关系 (桌面版缺失时用了谁的手机版) | 用来分组 |
| `eta_min` / `eta_feasible` / `n_resident_avg` | 该格 η 的可达下限 (shrunk floor / Σ m_fg) / 本格 η 是否高于它 / 平均留在预算内的后台应用数 | `eta_feasible = False` 的格等价于 static, 不要拿来比较策略; η 只比 `eta_min` 高一点 (如 0.6 vs 0.51) 时 `n_resident_avg ≈ 1`, 也几乎等于 static |
| `mem_high_backoffs` | 抖动保护抬高 `memory.high` 的次数 (哪些应用见 `--all-cols` 的 `mem_high_backoff_apps`) | 0 = 配置的 `mem_frac` / `mem_min_mb` 对所有应用都成立; > 0 说明这些应用的后台工作集比上限大, 静态上限只会制造 swap 流量 |
| `n_mobile` / `n_placeholders` | 本格里有几个应用跑的是手机版 / 占位程序 | 占位格只用于把流程跑通, 不进论文 |

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
5. **第 3 条的自动版**: `--work` (§6.6) 让 runner 自己打草稿 / 滚页面 / 上滑, 切走前记下内容摘要, 回来时核对 → `work_state_ok`;
   跑完看 `work_fail = 0`, 以及 `work_checks` 覆盖了多少次从 `shrunk` 回来的切换 (`per_app.csv` 按应用看)。

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
runner 在每次冷启动之后**立刻**再找一遍并把跑出去的进程移回来 (日志 `had left its cgroup while it started`), 这样 `m_fg`
和 CPU 限制是对的; 但它们在移回来之前分配的内存仍记在原来的 cgroup 上, `memory.high` 管不到 (三类实验的报告会写
"内存限制在这一格没有生效", §6.9)。要让内存限制也生效, 只能让进程**从一开始**就在 cgroup 里: 先查它为什么跑出去 ——
`snap list firefox`、`readlink -f "$(command -v firefox)"`、`cat /proc/<pid>/cgroup` (在 `snap.firefox...scope` 或
`app-gnome-...scope` 里就是 (a) / (b)), 换成 deb 版后重跑。

**没有 swap: `memory.high can only drop file pages`** — 匿名页没地方去, 内核只能在应用每次分配时让它停下来等
(allocation throttling): 应用更慢、`notify-send` 之类的子进程启动也可能被卡住几秒, 但依旧不会被杀。做 `sudo scripts/ubuntu/setup_zram.sh 4096`。

**`memory.high` 太低, 应用抖动 (ping 几秒, `refaults` 很大)** — 提高 `mem_min_mb` (Electron 应用 ≥ 256) 或 `mem_frac`。
观察 `bg[*].high_events` (memory.events high 计数) 与 `cg_psi_mem_full_us`。典型例子: 爱奇艺在后台继续播放 / 缓冲, 192 MB 的
`memory.high` 下一格 7 分钟 19,600 次 high 事件、116 万次 major fault、6 GB swap 写入, 而 PSS 一点没省下来 (页被换出又马上换回)。
默认开着的抖动保护 (`thrash_high_per_s` = 5) 会在一次停留里发现这种情况后把该应用的 `memory.high` ×1.5 (最高到 m_fg) 并 WARN,
`summary.csv` 的 `mem_high_backoffs` > 0 就是它动过手; 要做纯静态上限的对照把它设成 0。

**`swap_max_mb` 很小 (如 128) 时前台也卡** — 该应用换不出去, `memory.high` 的回收只能反复丢文件页: 夜间对照里 swap128 变体每格内存 PSI
180~270 s、ping P95 到 559 ms, 而 none 基线不到 1 s。这个变体只说明 "swap 上限不能和 memory.high 一起压得太死", 不要拿它比策略。

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

**装桌面时 `firefox_1:1snap1-0ubuntu2_arm64.deb ... dpkg returned an error code (1)`** — Ubuntu 的 firefox deb 只是个过渡包,
post-inst 里跑 `snap install firefox`, snap 商店连不上 (代理 / 国内网络) 就整个 `ubuntu-desktop-minimal` 失败。先装 Mozilla APT 仓库的
真 deb 并 pin (§0 第 4 步), 再 `sudo apt -f install && sudo apt install ubuntu-desktop-minimal`; 已经卡住的话
`sudo dpkg --remove --force-remove-reinstreq firefox` 后重做。

**`NO_PUBKEY C0BA5CE6DC6315A3` / `packages.mozilla.org ... is not signed`** — 密钥文件没下全 (常见于代理把下载截断): 重新
`wget -qO- https://packages.mozilla.org/apt/repo-signing-key.gpg | sudo tee /etc/apt/keyrings/packages.mozilla.org.asc`,
`gpg -n -q --import --import-options import-show` 看到指纹 `35BA A0B3 3E9E B396 F59C A838 C0BA 5CE6 DC63 15A3` 才算对。

**安装完成点 reboot 后黑屏、光标闪动** — ISO 还挂着 (UTM: CD/DVD → Clear 后再开机), 或者 Server 版默认把输出留在串口:
在 UTM 的 Serial 窗口能看到登录提示就说明系统好着, 装完 `ubuntu-desktop-minimal` 并 `systemctl set-default graphical.target` 即可。

**`git checkout cursor/...: pathspec did not match` / `couldn't find remote ref`** — 先 `git remote -v`: 如果 origin 不是
`HymnOfLight/context_freeze.git` 就是 clone 错了仓库, 重新 clone; 是对的话 `git fetch origin cursor/ubuntu-throttle-on-unfocus-c246`
(分支名带 `cursor/` 前缀)。

**磁盘只用了一半就报满 (`df -h /` 20 GB, 但虚拟机给了 40 GB)** — live-server 安装器的默认 LVM 布局只把卷组的一半分给根卷 `ubuntu-lv`,
剩下的留着 "以后用"。`sudo vgs` 看到 `VFree` 还有十几 GB 就是这种情况, 在线扩到全部 (ext4 不用卸载, 几秒钟):

```bash
sudo lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv && sudo resize2fs /dev/ubuntu-vg/ubuntu-lv && df -h /
```

不是 LVM (方案 B 的桌面 ISO 默认是普通分区) 而是事后在 UTM / Parallels 里把虚拟磁盘调大了的, 分区本身没跟着长:
`sudo growpart /dev/vda 2 && sudo resize2fs /dev/vda2` (`lsblk` 看根分区实际是哪个设备 / 编号; 需要 `cloud-guest-utils` 包)。
扩完再把下载残留清掉: `sudo apt clean`, `rm ~/Downloads/cf_debs/*.part`。

**`check_env.sh`: `FAIL /sys/fs/cgroup/cf missing`** — 每次开机都要 `sudo scripts/ubuntu/setup_cgroups.sh` (§3); `run_all.sh` 会自己做。
`warn only disk swap` 用 `sudo scripts/ubuntu/setup_zram.sh 4096` 解决 (`run_all.sh` 默认也会做)。

**`zramctl: /dev/zram0: failed to reset: Device or resource busy`** — zram 的 reset / disksize 接口在有人打开着这个块设备时返回 EBUSY。
刚 `modprobe` 或刚 `swapoff` 后的一两秒是 udev / udisks 在探测它 (脚本现在会 `udevadm settle` 并重试 10 次, 一般自己就过了);
重试后仍然报就是真有人占着: `fuser -v /dev/zram0`、`findmnt -S /dev/zram0`、`systemctl list-units --all '*zram*' 'dev-zram*.swap'` —
装过 `zram-tools` (`zramswap.service`) / `systemd-zram-generator` / `zram-config` 的机器开机就会自己占住 zram0, 这种情况直接用它们建好的 zram
(`ZRAM_MB=0 scripts/ubuntu/run_all.sh`, 只要 `/proc/swaps` 里有 zram 就行), 或者 `systemctl disable --now zramswap` 后重跑脚本。
`--off` 时 `swapoff` 失败 (空闲内存装不下换出的页) 会跳过该设备并提示, 先关掉几个应用再 `--off`。

**预算策略 (lru / landlord / hybrid) 的结果和 static 一模一样, `budget_violations = T`** — η 低于 shrunk floor (§5 "关于 η 与预算的语义"):
看 `eta_min` 列, 用 η ≥ eta_min + 0.05 重跑, 或 `MEM_FRAC=0.3`。

**下载类应用的 `download_mbps` 很低且各格都一样** — 代理在整形流量 (§2.5), 或者下载源本身限速; 实验前 `unset http_proxy https_proxy all_proxy`,
并在 `none` 基线里确认未限流时的速率明显更高。

**WPS 卡在 "新建" 首页转圈 / `edit` 脚本打不进字** — "新建" 页是内嵌网页, 要联网拉 docer 模板; 所以 `apps.py` 里 WPS 的启动命令直接打开
`~/.cache/context_freeze/blank.docx` (启动前自动生成), 不经过那一页。手动 `wps` 启动的会停在首页: 关掉, 用 `scripts/ubuntu/cf_launch.sh wps`。
文件损坏时 `python3 -m cf.linux.blankdoc --reset` 重建 (`run_all.sh ONBOARD=1` 每次都重建 - 但 WPS 正开着它时**不动它**: WPS 监视着这个文件,
被覆盖会弹出模态的 "文件已修改, 是否重新加载" 对话框抢走焦点, 演练就成了 `not-active`; 想要干净文档先退出 WPS 再 reset)。
`apt` 走代理 `Could not connect to x.x.x.2:7897 Connection refused` — VMware NAT 的宿主是 `.1` 不是网关 `.2` (§2.5); 删掉 `/etc/apt/apt.conf.d/95proxy`。

**`--work`: 所有 step 的 `work.status` 都是 `not-active`** — `xdotool` 发键前核对的是 `_NET_ACTIVE_WINDOW`, 它不是该应用的窗口:
焦点保护 (见 `activate` 失败一条) 或该应用弹了对话框; `--discover` 看当前活动窗口。Wayland 会话下也是这个现象 (只支持 X11)。

**`--work`: `work_state_ok` 全是 None, `work_checks = 0`** — oracle 读不到: `clipboard` 需要 `xclip` (`check_env.sh` 有提示) 且该应用的输入框有焦点
(微信 / QQ 要先点开一个会话); `screen` 需要 `python3-xlib` 且窗口没被最小化 (`minimize_background` 必须是 false); 手机版 `ui` 需要 `uiautomator`
(Waydroid 自带) 和 adb。看 `step.launch.work_check.after.error`。

**`--work`: 浏览器 / 哔哩哔哩的 `work_fail` 偶发** — 页面自己换了标题 (视频自动连播、标签页标题带未读数)。把那个应用的 oracle 改成 `screen`
并放大 `tolerance`, 或 `kind: none` + `oracle: title` 只看窗口还在不在; 真正的上下文丢失通常伴随 `n_cold > 0` 或 `ping` 很长。

**图里的字是方框 (数字、字母、图例全是 □)** — 系统里只有 `Droid Sans Fallback` 这一种中文字体 (最小安装的 Ubuntu 常见),
它只有汉字、没有拉丁字母和数字; 旧版出图代码把它排在字体列表最前面, 而 matplotlib 对一个通用字体族只用第一个匹配的字体,
不会逐字回退, 于是所有 ASCII 字符都成了方框 (日志里是一串 `Glyph 100 (d) missing from font(s) Droid Sans Fallback`)。
现在的规则 (`cf/linux/plots.py`): 逐个检查字体**实际有哪些字**, 优先用汉字和拉丁字符都有的 (Noto Sans CJK / 思源黑体 /
文泉驿 / PingFang / 微软雅黑 …, 以及任何不在名单里但字全的字体); 只有 "只有汉字" 的字体时, 字母数字用 matplotlib 自带的
DejaVu Sans、汉字用它 (逐字回退, 需要 matplotlib ≥ 3.6; 更旧的版本退回英文标注); 一个中文字体都没有就是英文标注。
装完字体不用清缓存 (出图时会把 matplotlib 还不认识的字体文件登记进来)。**已经跑完的结果不用重跑, 重画即可**:

```bash
git pull
sudo apt install -y fonts-wqy-microhei                 # 可选: 字形更好看 (install_deps.sh 现在会装); 或 fonts-noto-cjk
python3 -m cf.linux.classes results/arm64_classes_ci/*.jsonl --out results/arm64_classes_ci      # §6.9 的表和图
python3 -m cf.linux.analyze results/arm64_night/*.jsonl --plots-dir results/arm64_night/plots     # 其它实验的图同理 (§7)
CF_PLOT_FONT="Noto Sans CJK JP" python3 -m cf.linux.classes ...                                   # 指定用哪个字体 (fc-list :lang=zh family)
```

**macOS 侧** — 跑矩阵时关闭 Spotlight 索引新建的结果目录 (如果是共享目录), 关闭 Time Machine; UTM 不要开 "Balloon" 内存气球。

**微信 / 钉钉 / 中望 每次切到都报 `ping-timeout`, 每步多等 15 s** — 它们的 Qt 无边框窗口在 `WM_PROTOCOLS` 里声明了 `_NET_WM_PING`
却从不回复。runner 在同一应用连续 2 次 activate 成功但 ping 无回应后把它记为 "不支持 ping" (日志 `never answers _NET_WM_PING`),
之后不再等待, `ping_ms` 为空、`total_time_ms` 只含 activate; 这是每次运行自动学习的, 不用配置。想一开始就少等, 配置里把 `ping_timeout_s` 调到 3。

## 11. 测试

```bash
python3 -m pytest -q tests/test_linux_*.py         # 不需要桌面: tier 旋钮值 / 伪 cgroupfs / xprop 解析 / 伪桌面上的完整 runner (静态与预算策略、
                                                   # 无进程被杀、WARM、state_lost 注入、resume 重放档位) / focusd 决策 / 应用目录与 桌面->手机版 替代链 /
                                                   # Notify 解析 / 手机版: waydroid status、dumpsys 解析, 伪 Android 运行时上的 启动-收养-通知 与 preflight
                                                   # --work: 每类应用的默认脚本 / oracle、均值哈希与比较规则、伪 Android 上的上滑 + ui oracle、
                                                   # 伪桌面 runner 上的 work_state_ok / work_fail / state_lost 注入 / 检查点里的 oracle
                                                   # 有 Xvfb + xfwm4/openbox + xdotool 时自动加跑一次真实窗口 发现 -> 激活 -> _NET_WM_PING 往返,
                                                   # 以及占位窗口上的 title / statefile / screen oracle; 再有 mousepad + xclip 时跑一次 edit -> clipboard 往返
                                                   # /sys/fs/cgroup/cf 已委托给当前用户时自动加跑一次真实 cgroup 的档位读写
                                                   # 三类应用实验 (§6.9): 基准任务自校验 / 任务日志增量读取 / 本机下载源 + 下载 + 建连探测 /
                                                   # pattern 轨迹 / 伪桌面上的 bench 记录、顺滑探测、前后台区间与三张表、CLI 出表出图
                                                   # 真实应用 (tests/test_linux_realapps.py): ss 输出解析 / 连接计数器 -> 每应用速率 / 真实 ss 量一次本机下载 /
                                                   # cgroup 内探针 / 语料构建 / 装了 7z、z3 时真跑一遍并核对答案 / 按操作分行的计算表、套接字来源的下载表
                                                   # 更多计算应用: OpenSSL 加密 / 解密 (密文被改坏必须判错) / 签名, 画一页字让 Tesseract 认 (白纸必须判错),
                                                   # FFmpeg 转码, llama.cpp 的查找规则与任务 (装了才真跑), 计算应用的 m_fg 取 cgroup 峰值,
                                                   # tool_jobs --check / --missing, configs/ubuntu_arm64_compute.json 的结构
                                                   # 浏览器下载 (tests/test_linux_browser_dl.py): snap 版 Firefox 判为不可用 / 选第一个答应的下载源 / CONNECT 中继
                                                   # 把两条隧道合计限在给定速度 / 说明页把链接交给浏览器 / 下完删掉重下、卡住换源、盘满不下 / 两个 Firefox
                                                   # 互不收养 / 磁盘预算 / configs/ubuntu_arm64_full.json 的结构; 装了 deb 版 Firefox 且有 X 时真下一次本机文件
                                                   # 边下边删 (tests/test_linux_dl_trim.py): 一个真实的 "客户端" 进程写文件 / 改名 / 关闭 -> 下完的被删、
                                                   # 同目录别的文件和只读打开的文件不动、临时名和带进度文件的不动、宽限时间、dry-run; 打洞后文件长度不变、
                                                   # 盘上占用归零、客户端接着写; 镜像目录页解析、按大小筛选去重、不够时加 ?n=K 重复、只留真给明文 http 的;
                                                   # 下载型缩到一个应用后的配置 (访问顺序相邻重复合并) 和 DL_APPS 的 DRY 运行; 应用身上残留的限制能被指出来
                                                   # 冷启动后立刻收养跑出 cgroup 的进程 / "m_fg 是冷启动后量的" 与 "内存限制没有生效" 的提示 / 图只画有数据的类
                                                   # 出图字体 (tests/test_linux_plot_fonts.py): 字全的字体优先 / 只有汉字的字体配 DejaVu 逐字回退 /
                                                   # 装了 Droid Sans Fallback 时真画一张中英混排 + 对数轴的图, 不许有缺字告警
```

## 12. 脚本与文件清单

| 文件 | 何时用 | 作用 |
|---|---|---|
| `scripts/ubuntu/install_deps.sh [--hwe] [--subs]` | 一次 | apt 依赖 (xdotool, xclip, x11-utils, python3-xlib, dbus-x11, libnotify-bin, numpy, matplotlib, iproute2, §6.9 的 p7zip-full / z3 …); `--hwe` 6.x 内核 (memory.reclaim); `--subs` 开源替代品 |
| `scripts/ubuntu/check_env.sh` | 随时 | 一页体检: 会话类型 / 工具 (含 xclip) / cgroup v2 委托 / 本 shell 是否在 /cf / memory.reclaim / zram / snap 浏览器; 退出码 0 = 可以跑 |
| `scripts/ubuntu/setup_cgroups.sh [--pid P] [--no-enter]` | 每次开机 (sudo) | 建 `/sys/fs/cgroup/cf`, 开 cpu/cpuset/memory/io 控制器, chown 给当前用户, 把 shell 移进 `cf/host` |
| `scripts/ubuntu/cf_enter.sh` | 每个新终端 (`source`) | 等价于上一条, 显式传当前 shell 的 pid (tmux / VS Code 终端用这个) |
| `scripts/ubuntu/cf_adopt.sh` | runner 内部 (sudo) | 把别的 uid 的进程 (Waydroid 里的 Android 应用) 移进 `/cf/<app>`; sudoers 规则由 `setup_waydroid.sh --install` 写 |
| `scripts/ubuntu/setup_zram.sh [MB] [算法] / --off` | 每次开机 (sudo) | zstd zram swap (默认 4 GB), 让 `memory.high` 有地方放匿名页 |
| `scripts/mac/download_cn_apps.sh [--arch arm64\|amd64] [--only a,b] [--open] [--dry-run] [--refresh] [--keep-foreign] [--scp user@vm] [--install] [--vm-repo P]` (在 Mac 上跑, 虚拟机里也能跑) | 装应用前 | 在宿主机批量下载各应用的 Ubuntu deb 到 `~/Downloads/cf_debs` (解析厂商页面 / 接口 / 签名链接; 无直链的打印页面或开浏览器), 记 SOURCES.txt; 重跑跳过已有的 (含手动存的厂商文件名), 自动删架构不符 / 损坏的包; `--scp --install` 一键拷进虚拟机并安装 |
| `scripts/ubuntu/install_cn_apps.sh [--list] [--check] [--firefox] [DIR]` | 装应用 | 列出全部桌面应用的下载页 / 架构 / 包名 / 手机版 fallback (本机没有的架构标 `!!`); `--firefox` 装 Mozilla deb 版 + xclip; 从目录批量安装 deb: 另一架构 / 同版本已装的跳过, 结尾列 installed / skipped / FAILED |
| `scripts/ubuntu/setup_waydroid.sh [--install] [--status] [--stop]` | 有应用没有桌面版时 | Waydroid 仓库 + binder 模块 + init (一次); 每次登录起嵌套 weston X11 窗口 + 会话 + adb connect |
| `scripts/ubuntu/install_mobile_apps.sh [--list] [--missing] [--from-phone SN pkg…] [--search 名]` | 同上 | 桌面版缺失的那些应用的手机版 APK: 下载并 `adb install`; 小艺从华为手机导出 |
| `scripts/ubuntu/cf_launch.sh app…` | 首次登录 | 把应用启动进各自的 cgroup 并保持运行 (扫码 / 登录 / 关新手引导一次, 之后实验复用) |
| `scripts/ubuntu/cf_onboard.sh [app…] / --check` + `configs/accounts.json` | 每次开机 | 统一账号登录向导: 启动进 cgroup → 按 `accounts.json` 提示登录方式并等主窗口 → 提示准备动作 → 演练一轮 `--work` 脚本 + oracle; 记 `results/onboard/state.json`, `--check` 列表 (§4.6) |
| `python3 run_linux.py CONFIG --probe / --catalog / --menu / --discover / --mobile-status / --scenario list / --work-plan / --launch / --onboard [--yes] / --onboard-check / --release` | 诊断 | 环境与每应用可用性 / 目录表 / 桌面菜单条目 (非 deb 安装也认) / 桌面上实际的 WM_CLASS 与 cgroup 叶 / Android 状态 / 本配置会用哪些应用 / 每应用的脚本化操作与 oracle / 启动 / 清限制 |
| `python3 run_linux.py CONFIG --policy … --eta … --T … --scenario … [--work] [--resume FILE]` | 单格实验 | 一次实验 → `results/<name>.jsonl` (+ `.log`, `.ckpt`); Ctrl+C 安全, `--resume` 续跑 |
| `cf/linux/work.py` (`WorkDriver`: `X11WorkDriver` + `AdbWorkDriver`) | `--work` 内部 | 每类应用的默认脚本 (打草稿 / 滚动 / 缩放 / 上滑) 与内容 oracle (clipboard / title / screen / statefile / ui), 比较规则; `work_overrides` 按 id 覆盖 |
| `scripts/ubuntu/run_matrix.sh [CONFIG] [T]` + 环境变量 | 批量 | 场景 × 策略 × η × seed × 变体 (PROFILES / CPU_MODES / RESIDENT / SWAP_CAPS / MEM_HARD / MEM_FRAC); 预设 QUICK / NIGHT / FULL; PLACEHOLDERS / WORK (+ WORK_S) / DRY; 跳过已完成的格, 未完成的续跑; 结束后出全部表 / 图 / report.md |
| `scripts/ubuntu/run_squeeze.sh` + `CELLS= T= DWELL= SEEDS= SAMPLE= ONBOARD= SWAPPINESS= STRICT= DRY= OUT=` | 实验性 | §6.8: office 5 应用, none / squeeze (激进: cpuset+10 % 带宽+weight 1, memory.high 10 %, memory.reclaim 整个 cgroup ×3 并周期重复) / mild 三格 (+ 可选 `squeeze1`: 不周期重挤, 配 `SAMPLE=2` 看换出 / 换入收敛曲线), 长停留, 按进程统计 + 静默期换出速率; 出 `squeeze_apps.csv` / `plots/squeeze_apps.png` / `plots/<run>_swapflow.png` / `per_process.csv` |
| `scripts/ubuntu/install_compute_apps.sh [--llama] [--check]` + `LLAMA_TAG= LLAMA_REPO= MODEL_URL= CF_DATA_HOME=` | 一次 | §6.9 计算应用背后的真实程序: apt 装 7-Zip / Z3 / OpenSSL / Tesseract / FFmpeg; `--llama` 源码编译 llama.cpp 的 `llama-completion` 并下载一个 GGUF 模型到 `~/.local/share/context_freeze/llama`; `--check` 每个任务试做一遍 |
| `python3 -m cf.linux.tool_jobs [KIND…] [--check [--cycles n]] [--config CONFIG]` / `--missing CONFIG` | §6.9 检查 | 计算应用的工具装没装、缺什么、怎么装; `--check` 试做并打印每个操作的墙钟 / CPU 时间; `--missing` 列出某个配置里工具没就绪的计算应用 (有则退出码 1) |
| `python3 -m cf.linux.browser_dl --app ID [--url URL …] [--rate-mbps n] [--dir D] [--min-free-gb n]` / `--check` + `CF_BROWSER_DL_URL= CF_BROWSER_DL_RATE_MBPS= CF_BROWSER=` | 由 runner 启动 | §6.9 下载应用 `browser_dl`: 第二个 Firefox (独立 profile / 窗口类), 用浏览器自己的下载管理器反复下载同一个大文件, 下完即删; 默认经进程内的 CONNECT 中继限速 2 MB/s (0 = 不限速); `--check` 报告用哪个 Firefox、哪个源、限速多少 |
| `python3 -m cf.linux.dl_trim CONFIG [--list] [--dry-run] [--punch] [--apps …] [--every s] [--grace s] [--min-mb n] [--dir D] [--for s]` | `run_classes.sh` 在 `DL_TRIM=1 / punch` 时启动 | §6.9 边下边删: 下载应用下完的文件 (只限亲眼见到它在写的) 随下随删; `--punch` 连正在下的文件已写的部分也还给文件系统; `--list` 看应用此刻在写哪些文件, `--dry-run` 只报告不删 |
| `python3 -m cf.linux.subset CONFIG [--download APP… \| none] [--drop APP…] --out FILE` | `run_classes.sh` 在 `DL_APPS=…` 时调用 | 去掉若干应用后的配置 (应用表 / 三类表 / 访问顺序一起改), 例如下载型只留迅雷 |
| `python3 -m cf.linux.dl_files [--gb n \| --rate-mbps r --minutes M] [--min-gb a --max-gb b] [--mirror URL] [--http] [--skip N] [--plain]` | §6.9 准备下载任务 | 从清华 / 北外镜像站的目录页现查一批 0.7–3.3 GB、此刻能下的安装镜像链接 (够数为止, 不够就加 `?n=K` 重复), 用来粘进迅雷 / 百度网盘离线下载 |
| `python3 -m cf.linux.netstat CONFIG [--watch s] [--min-kbps n] [--minutes M]` | §6.9 检查 | 真实下载应用此刻的连接数 / 速度 / 往返时延; `--min-kbps` 有应用没在下时退出码 1; `--minutes` 按看到的速度算这么多分钟要写多少盘, 放不下退出码 3 |
| `scripts/ubuntu/run_classes.sh` + `CONFIG= CELLS= ROUNDS= DWELL= SEEDS= RTT_HOST= DL_MIN_KBPS= DL_APPS= DL_DISK_CHECK= DL_TRIM= DL_TRIM_ARGS= ONBOARD= EXTRA= SWAPPINESS= STRICT= TOOLCHECK= DRY= OUT=` (合成版另有 `DL_URL= DL_RATE_MBPS=`) | 三类应用 | §6.9: 交互 4 个 (呼叫 / 切换响应 + 顺滑度) + 计算 2 个 (真实的 7-Zip / Z3: 前台 vs 后台的单任务时延、每分钟任务数) + 下载 2 个 (百度网盘 / 迅雷: 下载速度、往返时延、收包积压、处理延迟), none / mild / squeeze 三格走同一个固定访问顺序, 约 10 分钟一格; 出 `classes_report.md` / `classes_tables.xlsx` / `classes_*.csv` / `plots/classes.png` / `plots/<run>_bench.png`; `CONFIG=configs/ubuntu_arm64_classes_synthetic.json` 是自带基准程序的合成版 |
| `python3 -m cf.linux.classes FILES… --out DIR [--no-plots]` | 分析 | §6.9 的三张表 (CSV / Markdown / Excel) 和图; 不重跑实验 |
| `python3 -m cf.linux.bench_app --app ID --kind sevenzip\|z3\|openssl\|llama\|tesseract\|ffmpeg\|numeric\|compress\|download [--corpus-mb n --level n --threads n --model F --tokens n --workers k --scale f --url U --rtt-host H:P]` / `--serve PORT --rate-mbps n` / `--check-url U` | §6.9 内部 | 可度量的计算 / 下载应用: X11 窗口 + 循环做固定任务 (`sevenzip` / `z3` 每个任务启动一次真实程序), 每个任务的时延写到 `/tmp/cf_bench_<id>.jsonl`; `--serve` 是合成版的本机限速下载源 |
| `python3 -m cf.linux.netstat CONFIG [--apps ID…] [--watch S] [--min-kbps K]` | §6.9 检查 | 看 S 秒配置里下载应用的 TCP 连接: 是否在跑、连接数、MB/s、往返时延 (内核 `ss` 统计); 低于 K 时退出码 1 |
| `scripts/ubuntu/run_all.sh` + `PRESET= OUT= T= ONBOARD= PLACEHOLDERS= WORK= WORK_S= SKIP_SMOKE= ZRAM_MB= EXTRA= MEM_FRAC=` | 一键 | 开机后一条命令: cgroup + zram → check_env → (`ONBOARD=1`: 登录向导 + `--check`, 不全就绪即停) → probe / catalog / mobile (/ work_plan) 快照 → 冒烟 (检查 state_lost / oom / COLD / work_fail = 0) → 矩阵 → 分析; 日志在 `OUT/run_all.log` |
| `python3 -m cf.linux.analyze FILES… [--csv] [--agg] [--per-app] [--per-proc] [--squeeze-apps] [--xlsx] [--pareto] [--plot] [--plots-dir] [--md]` | 分析 | 每格表 / 复本聚合 / 每应用表 / 每进程表 / 每应用 squeeze 五项表 / Excel 工作簿 (`per_process` 的跑法) / Pareto 点 / 图 / Markdown 报告 |
| `python3 -m cf.linux.focusd --config CONFIG [--policy --eta --delay --log --dry-run]` | 日常使用 | 焦点变化守护进程: 切走就限流, 切回就放开 |
| `python3 -m cf.linux.placeholder_app --app ID --title T [--cpu f --mem MB --io-mbps n --notify-every s --workers k]` | 冒烟 / 占位 | 内置 X11 占位程序: 可控的 CPU / 内存 / 写盘 / 通知负载, 状态写到 `/tmp/cf_placeholder_<id>.json` |
| `configs/ubuntu_cn_desktop.json` | 主配置 | 13 个应用、`substitute_kinds` / `mobile`、限流档位、轨迹、`dwell_s` 范围、`sample_every_s`、`placeholder_fill`、`work` / `work_s` / `work_overrides` |
| `configs/ubuntu_arm64.json` | arm64 真机集合 | UTM 虚拟机实际装好的 13 个应用 (含星火商店的 迅雷 / 爱奇艺 / 哔哩哔哩 / 网易云音乐 / 石墨文档 和 UOS 百度网盘), `work=true`, 五场景 (含 `media`), `sudo_adopt=true` (apm 容器 helper 属别的 uid) |
| `configs/ubuntu_arm64_squeeze.json` | 实验性 (§6.8) | office 5 应用、激进档位 (`cpu_mode both` 0.1 核 weight 1, `mem_frac 0.1`, `squeeze_rounds 3` / `squeeze_every_s 30`, 无抖动保护)、`dwell_s 150`、`per_process`、`quiet_after_s 30` |
| `configs/ubuntu_placeholders.json` | 冒烟 | 4 个占位程序的最小配置 (任何 X11 会话可跑, 含 Xvfb) |
| `results/<matrix>/{summary,summary_agg,per_app,pareto}.csv`, `plots/*.png`, `report.md`, `matrix.env`, `*.console.log` | 产物 | 表 / 图 / 报告 / 本次矩阵的参数与机器信息 / 每格控制台输出 |
