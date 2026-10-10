# context_freeze — 多应用冻结 / 换出 / 恢复实验平台

在 **MacBook (Apple M4, 16 GB)** 上用 Android 模拟器复现《面向多应用冻结、换出与恢复的优化模型及研究综述》
中的实验计划 (§3.5–§3.6): 采集 Top-k 应用的前后台切换轨迹, 在 `B = η·Σ m̄ᶠᵍᵢ` 的后台内存预算下比较
不冻结 / Android 默认 cached-app freezer / LRU / LFU / 仅预测排序 / Landlord 信用 / 预测+信用 (hybrid) /
离线 Bellman 最优 等策略, 输出后台 PSS、ZRAM 逻辑与物理量、累计 swap 写入读取、refault、恢复时延 P50/P95/P99
以及 "内存节省–恢复时延–闪存写入" Pareto 前沿。

```
.
├── docs/01_mac_m4_setup.md         # M4 + 16 GB 环境搭建 (AVD、6 GB 客体内存、root、zram)
├── docs/02_experiment_protocol.md  # 实验流程、指标 <-> 论文符号对照、断点续跑、注意事项
├── docs/03_adb_mechanisms.md       # 技术说明: 冻结 / 换出 / 换入 / 采样在 adb 层面到底做了什么
├── docs/04_cn_apps_scenarios.md    # 中国大陆应用集合 (微信/QQ/微博/网易云音乐/...) 的安装、首次启动与使用场景设计
├── docs/05_ubuntu_throttle_manual.md  # Ubuntu 22.04 虚拟机版 (不冻结, 切走后限 1 核 / memory.high / swap) 的运行与配置手册
├── scripts/                        # setup_avd / start_emulator / resize_data / prepare_device / install_cn_apps / first_run_cn_apps / run_matrix
├── scripts/ubuntu/                 # Ubuntu 版: setup_cgroups (cgroup v2 委托) / cf_enter / setup_zram / install_deps / install_cn_apps / cf_launch / check_env / run_matrix
├── configs/                        # 实验配置 (JSON): emulator_base (Google 应用) / cn_apps (国产应用 + 场景) / ubuntu_cn_desktop, ubuntu_placeholders (Ubuntu 版)
├── cf/                             # Python 包
│   ├── adb.py, device.py, parsers.py   # adb 封装; cgroup freezer / memcg 回收 / zram / tmpfs 气球; /proc 解析
│   ├── trace.py                        # 请求序列 σ 生成 (zipf / markov / drift / replay / scenario / day)
│   ├── scenarios.py                    # 国产应用目录 + 使用场景 (办公 / 刷社交媒体 / 通勤 / 购物 / 晚间娱乐 / 一天)
│   ├── policies.py                     # none / lru / lfu / landlord / markov / hybrid / belady
│   ├── runner.py                       # 真机(模拟器)实验循环 -> results/*.jsonl (+ .log, 每步 .ckpt 断点)
│   ├── logging_util.py                 # 带时间戳的控制台 + 文件日志, 进度/ETA
│   ├── analyze.py                      # 指标汇总、CSV、Pareto 图
│   ├── sim/                            # Linux 合成验证: 两层 (DRAM/ZRAM) 模型 + 离线 Bellman OPT
│   └── linux/                          # Ubuntu 后端: cgroup v2 档位 fg/bg/shrunk, /proc 采样, X11 切换 + _NET_WM_PING, dbus 通知计数,
│                                       #   国产桌面应用目录 (下载/通知推送/计算处理 + 替代品), 占位程序, ThrottleExperiment, focusd, analyze
├── run_experiment.py               # 真机 (Android 模拟器) 实验入口
├── run_linux.py                    # Ubuntu 虚拟机实验入口 (--probe / --discover / --catalog / --launch / --release)
└── tests/                          # 解析器、策略、Bellman(与穷举一致)、runner 端到端 (假设备 / 假桌面)、cgroup 档位、X11 (Xvfb)、focusd
```

## Ubuntu 22.04 版: 切走不冻结, 只限资源

同一台 MacBook 上开一个 **Ubuntu 22.04 arm64 虚拟机** (UTM / Parallels, 4 核 8 GB, Xorg 会话), 应用换成桌面版:
下载 (百度网盘 / Firefox、Chromium 等浏览器 / 腾讯视频 / 哔哩哔哩), 通知推送 (微信 / QQ / 钉钉), 计算处理 (WPS Office /
剪映→kdenlive / 中望CAD→librecad / 悟空图像→gimp / 小艺→内置占位程序; 没有 Linux 版的按箭头替代)。窗口失去前台后
**不冻结**: 它的 cgroup 被限到 **1 个逻辑核心** (`cpu.max`), 预算外的应用再加 `memory.high = 0.5 × 前台 PSS` 与可选
swap 上限; 不设 `memory.max`, 不会 OOM kill, 进程 / 窗口 / 状态全部保留, 后台下载与推送继续 (只是慢)。

```bash
# 在虚拟机里
sudo scripts/ubuntu/install_deps.sh --subs --hwe && sudo reboot          # 工具 + 开源替代品 + HWE 内核 (memory.reclaim)
sudo scripts/ubuntu/setup_cgroups.sh && sudo scripts/ubuntu/setup_zram.sh 4096     # 每次开机 (每个终端): cgroup 委托 + zram
scripts/ubuntu/check_env.sh && python3 run_linux.py configs/ubuntu_cn_desktop.json --probe
python3 run_linux.py configs/ubuntu_placeholders.json --policy static --T 12       # 不装应用的冒烟测试 (4 个占位程序)
scripts/ubuntu/cf_launch.sh wechat qq dingtalk wps && python3 run_linux.py configs/ubuntu_cn_desktop.json --discover
python3 run_linux.py configs/ubuntu_cn_desktop.json --scenario office --policy static --T 60
scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 60          # 场景 x 策略 x η (+ PROFILES="1c:0.5 0.5c:0.3" 限流档位)
python3 -m cf.linux.analyze results/ubuntu_matrix_*/*.jsonl              # + ping / bg_cores / download_mbps / notif_per_h / state_lost 列
python3 -m cf.linux.focusd --config configs/ubuntu_cn_desktop.json       # 日常使用: 焦点一变就按同样规则限流
```

详见 `docs/05_ubuntu_throttle_manual.md` (虚拟机搭建、cgroup 委托原理、各应用的安装来源与架构、配置项、指标、"不丢状态" 的验证方法、常见问题)。

## 快速开始 (macOS, Apple Silicon)

> macOS 默认的 zsh 在交互模式下**不把 `#` 当注释**: 整行连同 `# 说明` 一起粘贴时, 注释会变成命令参数 (例如 `install_cn_apps.py` 会把 `#`、`14`、`个应用,` 当成包名)。
> 先执行一次 `setopt interactivecomments` (加进 `~/.zshrc` 永久生效), 或者粘贴时去掉行尾注释。

```bash
# 0. 依赖
brew install --cask android-commandlinetools      # 或安装 Android Studio 后勾选 "Android SDK Command-line Tools"
python3 -m pip install -r requirements.txt

# 1. 创建可 root 的 arm64 AVD (google_apis, 非 Play 镜像), 客体内存 6 GB, /data 16 GB (第 5 个参数; 已有的 AVD 用 scripts/resize_data.sh 加大)
scripts/setup_avd.sh 35 cf_api35 6144 google_apis 16

# 2. 启动模拟器并等待开机 (脚本会检查宿主可用内存与是否退化为软件渲染; 内存压力实验可改为 3072)
scripts/start_emulator.sh cf_api35 6144

# 3. 设备准备: adb root, 关闭 Android 自带 cached-apps freezer (基线对照时改为 enabled), 开 1 GB zram
scripts/prepare_device.sh --system-freezer disabled --zram-mb 1024

# 4. (可选) 安装 F-Droid 开源应用扩大 Top-k 集合 (Firefox/VLC/NewPipe/Organic Maps/Wikipedia/AntennaPod)
python3 scripts/install_fdroid_apps.py
scripts/list_launchable.sh                        # 核对 configs/emulator_base.json 里的包名

# 4b. 国产应用集合 (微信/QQ/微博/网易云音乐/网易邮箱大师/抖音/小红书/哔哩哔哩/淘宝/京东/支付宝/钉钉/WPS/高德):
#     自动从应用宝 / 酷安下载 64 位 APK 并安装 (约 3.2 GB, 可断点续传), 再手工过一遍首次启动的隐私协议 / 登录
export ANDROID_SERIAL=emulator-5554
python3 scripts/install_cn_apps.py --download --config configs/cn_apps.json
scripts/first_run_cn_apps.sh configs/cn_apps.json          # AUTO=1 不提问, 只核对能否拉起并打印汇总

# 5. 探测设备能力 (freezer / memcg / zram / 各应用 uid 与启动 Activity)
python3 run_experiment.py configs/emulator_base.json --probe

# 6. 单次实验 / 参数扫描 (控制台输出同时写入 results/<name>.log)
python3 run_experiment.py configs/emulator_base.json --policy landlord --eta 0.3 --T 40 --name landlord_eta0.3
#    矩阵 = 策略 x η x 种子, 同一目录、同一客体内存、同一预算分母 (第一格的 warmup m_fg 复用到所有格)
POLICIES="none lru landlord hybrid" ETAS="0.25 0.35 0.5" SEEDS="1 2 3" scripts/run_matrix.sh configs/emulator_base.json 40

# 6c. 国产应用 + 使用场景: 办公 office / 刷社交媒体 social / 通勤 commute / 购物 shopping / 晚间娱乐 evening / 一天 day
python3 run_experiment.py configs/cn_apps.json --scenario list                       # 每个场景在当前配置下的应用与路径
python3 run_experiment.py configs/cn_apps.json --scenario office --policy landlord --eta 0.3 --T 60
python3 run_experiment.py configs/cn_apps.json --scenario day --policy hybrid --eta 0.3 --T 100   # 分段 (场景) 汇总
#    初次实验 (35 格, <= 10 小时): 五场景 x 4 策略 x η {0.25, 0.5} x 1 种子; 完整矩阵 (150 格, 约 30–40 小时) 在同一 OUT 里
#    把 ETAS="0.25 0.35 0.5" SEEDS="1 2 3" 续跑补齐即可 (docs/04 §5); run_matrix.sh 开跑前打印格数与预计时长
SCENARIOS="office social commute shopping evening" POLICIES="none lru landlord hybrid" ETAS="0.25 0.5" SEEDS="1" \
  OUT=results/cn_matrix scripts/run_matrix.sh configs/cn_apps.json 60

# 6b. 断点续跑: Ctrl+C / 模拟器崩溃 / adb 超时后, 从最后一个完成的步骤继续 (轨迹、策略状态、冻结/压缩集合全部恢复)
python3 run_experiment.py configs/emulator_base.json --resume results/landlord_eta0.3.jsonl
OUT=results/matrix_20260914-005939 scripts/run_matrix.sh configs/emulator_base.json 40   # 跳过已完成格子, 续跑未完成的

# 7. 汇总 (只汇总一个矩阵目录; 不同客体内存的矩阵不要用通配符混在一起)
python3 -m cf.analyze results/matrix_<stamp>/*.jsonl --csv summary.csv --agg summary_agg.csv --pareto pareto.csv --plot pareto.png
python3 -m cf.analyze results/matrix_<stamp>/*.jsonl --x eta --plot pareto_eta.png     # 横轴改为实际达到的 η
```

`run_experiment.py` 在开跑前做 **严格 preflight**: 系统 cached-apps freezer 仍在启用 (`settings get global cached_apps_freezer`
不是 `disabled`) 或模拟器在软件渲染 (SwiftShader) 时直接拒绝启动, 因为这两种状态下测出的数字没有意义
(见 docs/02 §8); 明确要测 "Android 默认" 基线时加 `--no-strict`。

运行时每步打印一行进度, 例如

```
07:15:28 STEP [ 12/40  5m32s ETA  13m10s] gm          WARM  1348 ms <-zram | S= 4 frz=11 zram=10 | M_bg  331/428 MB | frz+1/-0 rcl 1 0.4s
```

(第 12/40 步, 已用 5m32s, 预计剩余 13m10s; Gmail 从 zram 恢复用了 1348 ms; 常驻 4 / 冻结 11 / 压缩 10 个应用; 后台占用 331 MB
对预算 428 MB; 本步冻结 1 个、回收 1 个, 动作耗时 0.4 s)。有进程被系统杀掉时该行会带 `killed: calendar[bg anr]` ——
方括号里是 ActivityManager 记录的死亡原因 (`dumpsys activity exit-info`), 汇总表 `kill_reasons` 列统计各原因次数。
结束时打印 HOT/WARM/COLD 计数、时延 P50/P95/P99、后台 PSS 与预算、swap 读写的摘要。

## 国产应用: 一键脚本

```bash
git clone https://github.com/HymnOfLight/context_freeze.git ~/context_freeze 2>/dev/null; cd ~/context_freeze && git pull -q
QUICK=1 bash scripts/bootstrap_cn.sh    # 先做冒烟测试: 1 场景, none + landlord, T=20, 2 格约 15 分钟
bash scripts/bootstrap_cn.sh            # 初次实验: 5 场景 x 4 策略 x η {0.25, 0.5} x 1 种子 = 35 格, 约 7–9.5 小时 (<= 10 小时)
FULL=1 bash scripts/bootstrap_cn.sh     # 完整矩阵: η {0.25, 0.35, 0.5} x 种子 {1, 2, 3} = 150 格, 约 30–40 小时; 同一目录, 只补跑初次实验没有的 115 格
```

`scripts/bootstrap_cn.sh` 按顺序完成: 安装 SDK / Java → 创建 6 GB 客体、16 GB /data 的 AVD → 启动 → 关系统 freezer、开 2 GB zram →
自动下载并安装 14 个应用 (有手机连着时改为从手机导出) → 首次启动手工过协议 (只做一次) → probe → 场景 × 策略 × η × 种子矩阵 → 汇总与 Pareto 图。
每一步都会检查是否已完成, 中断后重新运行同一条命令即可续跑; 只有点隐私协议 / 登录这一步需要人。默认矩阵就是 **初次实验** (≤ 10 小时,
docs/04 §5 说明了为什么这样裁: 保留全部场景与策略, η 取两端、种子取 1 个、不减 T), 开跑前 `run_matrix.sh` 会打印格数和预计时长。手动逐步执行见下。

## 国产应用: 下载、安装并完成配置 (手动逐步)

```bash
setopt interactivecomments 2>/dev/null   # zsh: 让行尾 "# 注释" 不被当成参数
cd ~/context_freeze && source scripts/env.sh
export ANDROID_SERIAL=emulator-5554

python3 scripts/install_cn_apps.py --download --config configs/cn_apps.json   # 自动下载 14 个 64 位 APK (~3.2 GB) 并装进模拟器; /data 不够会先停下
# /data 不够 (旧 AVD 是 8 GB, 装到第 10 个就满): scripts/resize_data.sh cf_api35 16  会清空模拟器 /data 后重建为 16 GB, 再 prepare_device.sh 并重跑上一行
python3 scripts/install_cn_apps.py --list        # 核对: installed 列
scripts/first_run_cn_apps.sh configs/cn_apps.json                # 逐个手工点掉隐私协议 / 登录 (微信 QQ 用备用账号)
python3 run_experiment.py configs/cn_apps.json --probe          # uid / 启动 Activity 都能解析
python3 run_experiment.py configs/cn_apps.json --scenario list  # 每个场景实际会用到的应用
```

`--download` 依次查 腾讯应用宝 → 酷安 → 少数官网直链, 先用 Range 请求读远端 APK 的 ZIP 目录, 只下载含 `lib/arm64-v8a/` 的构建
(API 31+ 的 arm64 模拟器镜像是纯 64 位, 装不了 armeabi-v7a 的包; 应用宝给 小红书 / 哔哩哔哩 / 京东 / 支付宝 / 钉钉 的是 32 位包, 这几个会自动转到酷安)。
下载到 `apks/<包名>/`, 中断后重跑同一条命令续传; 应用宝的包校验 md5。`--download --dry-run` 只列出各来源会给哪个构建, 不下载。
其它两种来源: 手工把 `.apk` / `.xapk` 放进 `apks/` 后不带参数运行; 或从自己的手机导出 (`--from-phone <serial> --config configs/cn_apps.json`,
手机需开 USB 调试并在 `$ADB devices -l` 里显示为 `device`)。拉过 / 下过一次的 APK 会留在 `apks/<包名>/`, 重建 AVD 后不带参数再跑一次即可。

## 合成验证 (无需模拟器)

```bash
python3 -m cf.sim.run_sim --k 8 --T 300 --eta 0.3 0.5 0.7 --trace markov --seed 1
python3 -m cf.sim.run_sim --k 10 --T 200 --trace zipf --drift-at 100 --out results/sim_drift.json
python3 -m pytest -q tests
```

`bellman_opt` 一行是定理 1 的离线最优 OPT(σ) (按子集枚举的动态规划, k ≤ 12), `ratio_to_opt` 即各在线策略与
最优的比值; `eta_rank` 是预测器的加权逆序误差; `eta_floor` 是 "全部压缩也放不下" 的可达下界, 提醒 η=10%
不应预先宣称可达 (文档 §1)。

## 验证状态

* `tests/` (40 项): /proc、`am start -W` 输出解析 (含真实抓取的 HOT / FRONT / TIMEOUT 样本), 策略约束, Bellman DP 与穷举一致,
  在线策略代价 ≥ OPT, runner 端到端 (假设备), 崩溃后断点续跑 (landlord / hybrid / lru 三种策略状态恢复、无重复无缺步、计数器回绕), 严格 preflight、固定预算分母、
  kill 原因采集、多种子 / 多客体内存的汇总聚合, 使用场景生成 (可用应用裁剪、枢纽 / 固定路径结构、一天分段) 与分段汇总。
* 在 Linux 主机上用 **Android 15 (API 35) google_apis 系统镜像** 实测通过: `adb root`、cgroup v2 `cgroup.freeze` 冻结/解冻、
  memcg v1 每应用回收 (Clock: PSS 42 MB → 35 kB, SwapPss → 22 MB, zram/pswpout 同步增长)、解冻后按需换回 (majflt/pswpin)、
  tmpfs 气球、完整 `run_experiment.py` 循环与 `cf.analyze` 汇总。该主机无可用 KVM, 模拟器以软件模拟运行, 因而绝对时延无意义;
  Apple Silicon 上的 arm64 镜像用户态布局相同 (同一 Android 15 内核配置), 脚本无需改动。
* Ubuntu 版 (`tests/test_linux_*.py`, 41 项): 档位旋钮值与伪 cgroupfs 读写、xprop 解析、假桌面上的完整 `ThrottleExperiment`
  (static / 预算策略的分层、无进程被杀、WARM、`state_lost` 注入、断点续跑重放档位)、focusd 决策、应用目录 / 场景 / 替代关系、
  dbus Notify 解析; 有 Xvfb + 窗口管理器时自动加跑真实窗口的 发现 → 激活 → `_NET_WM_PING` 往返, `/sys/fs/cgroup/cf` 已委托时
  自动加跑真实 cgroup 的档位读写。在 Ubuntu 24.04 (cgroup v2, Xvfb + xfwm4, 真实 `/sys/fs/cgroup/cf`) 上用 4 个占位程序跑通
  `run_linux.py` 完整循环: 后台合计 ≈ 1 核、节流时间占比 26%、从 `shrunk` 切回的 ping 0.4–1.8 s、state_lost = 0、OOM = 0。

## 与文档模型的对应

| 文档符号 | 采集/实现位置 |
|---|---|
| r_t, σ | `cf/trace.py`; 真机上由 `am start -W` 依次拉起 |
| a_i(t), b_i(t) (匿名/文件工作集, PSS 分摊) | `/proc/<pid>/smaps_rollup` 的 `Pss_Anon`, `Pss_File`, `SwapPss` (按 uid 汇总所有进程) |
| y_i(t) 冻结 | `/sys/fs/cgroup/uid_*/pid_*/cgroup.freeze` (cgroup v2 freezer), 回退 `SIGSTOP` |
| z_i^a (ZRAM), ρ_i | `SwapPss` + `/sys/block/zram0/mm_stat` (compr/orig) |
| s_i^a 闪存交换 | 可选 `--swapfile-mb` 建立 /data 上的低优先级 swap 文件 |
| M(t) ≤ B_t, B = η Σ m̄ᶠᵍ | warmup 阶段测各应用前台 PSS 作 m̄ᶠᵍᵢ; 预算只计后台应用 |
| W_t, R_t (swap 写/读) | `/proc/vmstat` `pswpout`/`pswpin` × 页大小 |
| refault, major fault | `workingset_refault_anon/file`, `pgmajfault`, `/proc/<pid>/stat` majflt |
| L_t 恢复时延 | `am start -W` 的 `TotalTime` 与 `LaunchState` (HOT/WARM/COLD) |
| PSI | `/proc/pressure/memory` |
| E_t 可冻结安全集合 | `never_freeze` 白名单 (前台/正在播放/推送依赖) |
| 页面回收 (memcg) | `memory.reclaim` (cgroup v2) → memcg v1 每应用 `force_empty` (Android 15 模拟器实测路径) → `/proc/<pid>/reclaim` → tmpfs 气球 (全局压力) 依次回退 |
| T^ML 预算 | 每步 `decision_ms` / `action_ms`, analyze 报告 P50/P99 |

更多细节见 `docs/`。
