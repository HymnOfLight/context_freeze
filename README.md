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
├── scripts/                        # setup_avd / start_emulator / prepare_device / install_fdroid_apps / run_matrix
├── configs/                        # 实验配置 (JSON)
├── cf/                             # Python 包
│   ├── adb.py, device.py, parsers.py   # adb 封装; cgroup freezer / memcg 回收 / zram / tmpfs 气球; /proc 解析
│   ├── trace.py                        # 请求序列 σ 生成 (zipf / markov / drift / replay)
│   ├── policies.py                     # none / lru / lfu / landlord / markov / hybrid / belady
│   ├── runner.py                       # 真机(模拟器)实验循环 -> results/*.jsonl (+ .log, 每步 .ckpt 断点)
│   ├── logging_util.py                 # 带时间戳的控制台 + 文件日志, 进度/ETA
│   ├── analyze.py                      # 指标汇总、CSV、Pareto 图
│   └── sim/                            # Linux 合成验证: 两层 (DRAM/ZRAM) 模型 + 离线 Bellman OPT
├── run_experiment.py               # 真机实验入口
└── tests/                          # 解析器、策略、Bellman(与穷举一致)、runner 端到端 (假设备)
```

## 快速开始 (macOS, Apple Silicon)

```bash
# 0. 依赖
brew install --cask android-commandlinetools      # 或安装 Android Studio 后勾选 "Android SDK Command-line Tools"
python3 -m pip install -r requirements.txt

# 1. 创建可 root 的 arm64 AVD (google_apis, 非 Play 镜像), 客体内存 6 GB
scripts/setup_avd.sh 35 cf_api35 6144

# 2. 启动模拟器并等待开机 (脚本会检查宿主可用内存与是否退化为软件渲染; 内存压力实验可改为 3072)
scripts/start_emulator.sh cf_api35 6144

# 3. 设备准备: adb root, 关闭 Android 自带 cached-apps freezer (基线对照时改为 enabled), 开 1 GB zram
scripts/prepare_device.sh --system-freezer disabled --zram-mb 1024

# 4. (可选) 安装 F-Droid 开源应用扩大 Top-k 集合 (Firefox/VLC/NewPipe/Organic Maps/Wikipedia/AntennaPod)
python3 scripts/install_fdroid_apps.py
scripts/list_launchable.sh                        # 核对 configs/emulator_base.json 里的包名

# 5. 探测设备能力 (freezer / memcg / zram / 各应用 uid 与启动 Activity)
python3 run_experiment.py configs/emulator_base.json --probe

# 6. 单次实验 / 参数扫描 (控制台输出同时写入 results/<name>.log)
python3 run_experiment.py configs/emulator_base.json --policy landlord --eta 0.3 --T 40 --name landlord_eta0.3
POLICIES="none lru landlord hybrid" ETAS="0.2 0.3 0.5" scripts/run_matrix.sh configs/emulator_base.json 40

# 6b. 断点续跑: Ctrl+C / 模拟器崩溃 / adb 超时后, 从最后一个完成的步骤继续 (轨迹、策略状态、冻结/压缩集合全部恢复)
python3 run_experiment.py configs/emulator_base.json --resume results/landlord_eta0.3.jsonl
OUT=results/matrix_20260914-005939 scripts/run_matrix.sh configs/emulator_base.json 40   # 跳过已完成格子, 续跑未完成的

# 7. 汇总
python3 -m cf.analyze results/matrix_*/*.jsonl --csv summary.csv --pareto pareto.csv --plot pareto.png
```

运行时每步打印一行进度, 例如

```
07:15:28 STEP [ 12/40  5m32s ETA  13m10s] gm          WARM  1348 ms <-zram | S= 4 frz=11 zram=10 | M_bg  331/428 MB | frz+1/-0 rcl 1 0.4s
```

(第 12/40 步, 已用 5m32s, 预计剩余 13m10s; Gmail 从 zram 恢复用了 1348 ms; 常驻 4 / 冻结 11 / 压缩 10 个应用; 后台占用 331 MB
对预算 428 MB; 本步冻结 1 个、回收 1 个, 动作耗时 0.4 s)。开始前的 preflight 会对 "系统 freezer 未关"、"客体内存 < 4 GB"、
"软件渲染"、"无 swap" 等已知干扰因素给出 WARN, 结束时打印 HOT/WARM/COLD 计数、时延 P50/P95/P99、后台 PSS 与预算、swap 读写的摘要。

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

* `tests/` (24 项): /proc、`am start -W` 输出解析 (含真实抓取的 HOT / FRONT / TIMEOUT 样本), 策略约束, Bellman DP 与穷举一致,
  在线策略代价 ≥ OPT, runner 端到端 (假设备), 崩溃后断点续跑 (landlord / hybrid / lru 三种策略状态恢复、无重复无缺步、计数器回绕)。
* 在 Linux 主机上用 **Android 15 (API 35) google_apis 系统镜像** 实测通过: `adb root`、cgroup v2 `cgroup.freeze` 冻结/解冻、
  memcg v1 每应用回收 (Clock: PSS 42 MB → 35 kB, SwapPss → 22 MB, zram/pswpout 同步增长)、解冻后按需换回 (majflt/pswpin)、
  tmpfs 气球、完整 `run_experiment.py` 循环与 `cf.analyze` 汇总。该主机无可用 KVM, 模拟器以软件模拟运行, 因而绝对时延无意义;
  Apple Silicon 上的 arm64 镜像用户态布局相同 (同一 Android 15 内核配置), 脚本无需改动。

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
