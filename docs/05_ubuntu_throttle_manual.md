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
| `dwell_s` / `settle_s` / `warmup_dwell_s` | [5, 12] / 2 / 10 | 每步停留 / 切换后等待 / warmup 每应用停留 (秒)。`dwell_s` 给一个数 = 固定; 给 `[lo, hi]` = 每步按 seed 均匀抽样 (同一 seed 下各策略的每一步停留相同, 可直接对比); 每条 `step` 记录带 `dwell_s` |
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
scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 60       # 默认: 4 场景 x (none static lru landlord hybrid) x η 0.6/0.8, ~30 格 ~8 h
NIGHT=1 scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 40   # 过夜档: + 1c:0.3 / 0.5c:0.5 / cpuset / swap128 / memhard 对照, ~50 格 ~10 h
FULL=1  scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 60   # 全量: 5 场景 (含 day) x 6 策略 x η 0.55/0.7/0.85 x 2 seed x 全部变体, ~350 格, 数天 (可中断续跑)
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
| 微信 / QQ / 钉钉 | `draft`: 往焦点输入框键入 `cf-work <id> t<step> 草稿未保存 ` | `clipboard`: ctrl+a ctrl+c 后剪贴板文本的长度 + md5 | 需要先手动点开一个会话让输入框有焦点 (登录时顺手做) |
| WPS | `edit`: 键入同样一行 + 回车 | `clipboard` | 文档不保存; 回来时 ctrl+a 全选再 End 取消选区 |
| 浏览器 / 腾讯视频 / 哔哩哔哩 | `browse`: 滚轮下翻 `scroll` 格再回退 1/4 | `title`: `_NET_WM_NAME` (页面标题) | 像素会随视频变化, 所以只认标题; 页面被重载会换标题 / 回到顶部 |
| 百度网盘 / 小艺 | `none` | `title` | 下载本身在后台进行 (`write_mb` 已经度量) |
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
`"oracle": "screen"` 或 `"title"`。(b) `draft` 文本会越积越长 (每步追加一段), 60 步 ≈ 3 KB, 对输入框无压力; 想清空就在下一次登录时删掉。
(c) 只支持 X11 (XTEST / `get_image`); Wayland 下 `--work` 会全部 `not-active` / `error`。(d) 手机版 `ui` oracle 依赖 `uiautomator dump`,
Waydroid 上要 1–3 s; 列表页的推荐流本身会刷新 → 用 `tolerance` 更大的 `screen` 或 `kind: none`。(e) `work_s` 计入 dwell 的开头, 所以
`dwell_s` 的下限至少要 ≥ 2 × `work_s`。(f) 微信 / QQ 在没有焦点输入框时 `draft` 的按键会被吞掉, 不影响实验, 只是 `clipboard` 读到的是空 → None。

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
| `eta_min` / `eta_feasible` / `n_resident_avg` | 该格 η 的可达下限 (shrunk floor / Σ m_fg) / 本格 η 是否高于它 / 平均留在预算内的后台应用数 | `eta_feasible = False` 的格等价于 static, 不要拿来比较策略 |
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

**预算策略 (lru / landlord / hybrid) 的结果和 static 一模一样, `budget_violations = T`** — η 低于 shrunk floor (§5 "关于 η 与预算的语义"):
看 `eta_min` 列, 用 η ≥ eta_min + 0.05 重跑, 或 `MEM_FRAC=0.3`。

**下载类应用的 `download_mbps` 很低且各格都一样** — 代理在整形流量 (§2.5), 或者下载源本身限速; 实验前 `unset http_proxy https_proxy all_proxy`,
并在 `none` 基线里确认未限流时的速率明显更高。

**WPS 卡在 "新建" 首页转圈 / `edit` 脚本打不进字** — "新建" 页是内嵌网页, 要联网拉 docer 模板; 所以 `apps.py` 里 WPS 的启动命令直接打开
`~/.cache/context_freeze/blank.docx` (启动前自动生成), 不经过那一页。手动 `wps` 启动的会停在首页: 关掉, 用 `scripts/ubuntu/cf_launch.sh wps`。
文件损坏时 `python3 -m cf.linux.blankdoc --reset` 重建 (`run_all.sh ONBOARD=1` 每次都重建)。
`apt` 走代理 `Could not connect to x.x.x.2:7897 Connection refused` — VMware NAT 的宿主是 `.1` 不是网关 `.2` (§2.5); 删掉 `/etc/apt/apt.conf.d/95proxy`。

**`--work`: 所有 step 的 `work.status` 都是 `not-active`** — `xdotool` 发键前核对的是 `_NET_ACTIVE_WINDOW`, 它不是该应用的窗口:
焦点保护 (见 `activate` 失败一条) 或该应用弹了对话框; `--discover` 看当前活动窗口。Wayland 会话下也是这个现象 (只支持 X11)。

**`--work`: `work_state_ok` 全是 None, `work_checks = 0`** — oracle 读不到: `clipboard` 需要 `xclip` (`check_env.sh` 有提示) 且该应用的输入框有焦点
(微信 / QQ 要先点开一个会话); `screen` 需要 `python3-xlib` 且窗口没被最小化 (`minimize_background` 必须是 false); 手机版 `ui` 需要 `uiautomator`
(Waydroid 自带) 和 adb。看 `step.launch.work_check.after.error`。

**`--work`: 浏览器 / 哔哩哔哩的 `work_fail` 偶发** — 页面自己换了标题 (视频自动连播、标签页标题带未读数)。把那个应用的 oracle 改成 `screen`
并放大 `tolerance`, 或 `kind: none` + `oracle: title` 只看窗口还在不在; 真正的上下文丢失通常伴随 `n_cold > 0` 或 `ping` 很长。

**macOS 侧** — 跑矩阵时关闭 Spotlight 索引新建的结果目录 (如果是共享目录), 关闭 Time Machine; UTM 不要开 "Balloon" 内存气球。

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
```

## 12. 脚本与文件清单

| 文件 | 何时用 | 作用 |
|---|---|---|
| `scripts/ubuntu/install_deps.sh [--hwe] [--subs]` | 一次 | apt 依赖 (xdotool, xclip, x11-utils, python3-xlib, dbus-x11, libnotify-bin, numpy, matplotlib …); `--hwe` 6.x 内核 (memory.reclaim); `--subs` 开源替代品 |
| `scripts/ubuntu/check_env.sh` | 随时 | 一页体检: 会话类型 / 工具 (含 xclip) / cgroup v2 委托 / 本 shell 是否在 /cf / memory.reclaim / zram / snap 浏览器; 退出码 0 = 可以跑 |
| `scripts/ubuntu/setup_cgroups.sh [--pid P] [--no-enter]` | 每次开机 (sudo) | 建 `/sys/fs/cgroup/cf`, 开 cpu/cpuset/memory/io 控制器, chown 给当前用户, 把 shell 移进 `cf/host` |
| `scripts/ubuntu/cf_enter.sh` | 每个新终端 (`source`) | 等价于上一条, 显式传当前 shell 的 pid (tmux / VS Code 终端用这个) |
| `scripts/ubuntu/cf_adopt.sh` | runner 内部 (sudo) | 把别的 uid 的进程 (Waydroid 里的 Android 应用) 移进 `/cf/<app>`; sudoers 规则由 `setup_waydroid.sh --install` 写 |
| `scripts/ubuntu/setup_zram.sh [MB] [算法] / --off` | 每次开机 (sudo) | zstd zram swap (默认 4 GB), 让 `memory.high` 有地方放匿名页 |
| `scripts/mac/download_cn_apps.sh [--arch arm64\|amd64] [--only a,b] [--open] [--dry-run] [--refresh] [--keep-foreign] [--scp user@vm] [--install] [--vm-repo P]` (在 Mac 上跑, 虚拟机里也能跑) | 装应用前 | 在宿主机批量下载各应用的 Ubuntu deb 到 `~/Downloads/cf_debs` (解析厂商页面 / 接口 / 签名链接; 无直链的打印页面或开浏览器), 记 SOURCES.txt; 重跑跳过已有的 (含手动存的厂商文件名), 自动删架构不符 / 损坏的包; `--scp --install` 一键拷进虚拟机并安装 |
| `scripts/ubuntu/install_cn_apps.sh [--list] [--check] [DIR]` | 装应用 | 列出 13 个应用的下载页 / 架构 / 包名 / 手机版 fallback; 从目录批量安装 deb (跳过 x86_64-only) |
| `scripts/ubuntu/setup_waydroid.sh [--install] [--status] [--stop]` | 有应用没有桌面版时 | Waydroid 仓库 + binder 模块 + init (一次); 每次登录起嵌套 weston X11 窗口 + 会话 + adb connect |
| `scripts/ubuntu/install_mobile_apps.sh [--list] [--missing] [--from-phone SN pkg…] [--search 名]` | 同上 | 桌面版缺失的那些应用的手机版 APK: 下载并 `adb install`; 小艺从华为手机导出 |
| `scripts/ubuntu/cf_launch.sh app…` | 首次登录 | 把应用启动进各自的 cgroup 并保持运行 (扫码 / 登录 / 关新手引导一次, 之后实验复用) |
| `scripts/ubuntu/cf_onboard.sh [app…] / --check` + `configs/accounts.json` | 每次开机 | 统一账号登录向导: 启动进 cgroup → 按 `accounts.json` 提示登录方式并等主窗口 → 提示准备动作 → 演练一轮 `--work` 脚本 + oracle; 记 `results/onboard/state.json`, `--check` 列表 (§4.6) |
| `python3 run_linux.py CONFIG --probe / --catalog / --discover / --mobile-status / --scenario list / --work-plan / --launch / --onboard [--yes] / --onboard-check / --release` | 诊断 | 环境与每应用可用性 / 目录表 / 桌面上实际的 WM_CLASS 与 cgroup 叶 / Android 状态 / 本配置会用哪些应用 / 每应用的脚本化操作与 oracle / 启动 / 清限制 |
| `python3 run_linux.py CONFIG --policy … --eta … --T … --scenario … [--work] [--resume FILE]` | 单格实验 | 一次实验 → `results/<name>.jsonl` (+ `.log`, `.ckpt`); Ctrl+C 安全, `--resume` 续跑 |
| `cf/linux/work.py` (`WorkDriver`: `X11WorkDriver` + `AdbWorkDriver`) | `--work` 内部 | 每类应用的默认脚本 (打草稿 / 滚动 / 缩放 / 上滑) 与内容 oracle (clipboard / title / screen / statefile / ui), 比较规则; `work_overrides` 按 id 覆盖 |
| `scripts/ubuntu/run_matrix.sh [CONFIG] [T]` + 环境变量 | 批量 | 场景 × 策略 × η × seed × 变体 (PROFILES / CPU_MODES / RESIDENT / SWAP_CAPS / MEM_HARD / MEM_FRAC); 预设 QUICK / NIGHT / FULL; PLACEHOLDERS / WORK (+ WORK_S) / DRY; 跳过已完成的格, 未完成的续跑; 结束后出全部表 / 图 / report.md |
| `scripts/ubuntu/run_all.sh` + `PRESET= OUT= T= ONBOARD= PLACEHOLDERS= WORK= WORK_S= SKIP_SMOKE= ZRAM_MB= EXTRA= MEM_FRAC=` | 一键 | 开机后一条命令: cgroup + zram → check_env → (`ONBOARD=1`: 登录向导 + `--check`, 不全就绪即停) → probe / catalog / mobile (/ work_plan) 快照 → 冒烟 (检查 state_lost / oom / COLD / work_fail = 0) → 矩阵 → 分析; 日志在 `OUT/run_all.log` |
| `python3 -m cf.linux.analyze FILES… [--csv] [--agg] [--per-app] [--pareto] [--plot] [--plots-dir] [--md]` | 分析 | 每格表 / 复本聚合 / 每应用表 / Pareto 点 / 图 / Markdown 报告 |
| `python3 -m cf.linux.focusd --config CONFIG [--policy --eta --delay --log --dry-run]` | 日常使用 | 焦点变化守护进程: 切走就限流, 切回就放开 |
| `python3 -m cf.linux.placeholder_app --app ID --title T [--cpu f --mem MB --io-mbps n --notify-every s --workers k]` | 冒烟 / 占位 | 内置 X11 占位程序: 可控的 CPU / 内存 / 写盘 / 通知负载, 状态写到 `/tmp/cf_placeholder_<id>.json` |
| `configs/ubuntu_cn_desktop.json` | 主配置 | 13 个应用、`substitute_kinds` / `mobile`、限流档位、轨迹、`dwell_s` 范围、`sample_every_s`、`placeholder_fill`、`work` / `work_s` / `work_overrides` |
| `configs/ubuntu_placeholders.json` | 冒烟 | 4 个占位程序的最小配置 (任何 X11 会话可跑, 含 Xvfb) |
| `results/<matrix>/{summary,summary_agg,per_app,pareto}.csv`, `plots/*.png`, `report.md`, `matrix.env`, `*.console.log` | 产物 | 表 / 图 / 报告 / 本次矩阵的参数与机器信息 / 每格控制台输出 |
