# 实验流程与指标

本文对应文档 §2 (问题定义)、§3.1 (分层控制)、§3.4 (鲁棒信用)、§3.5–§3.6 (实验计划与指标)。

## 1. 一次实验做了什么 (`cf/runner.py`)

```
prepare   adb root, 探测 freezer/memcg/zram, 建 zram, 关动画, 校验应用列表
warmup    对每个应用 i: force-stop -> am start -W (冷启动 L_cold_i) -> 停留 warmup_dwell_s -> 采样
          m̄ᶠᵍᵢ = Pss + SwapPss,  aᵢ = Pss_Anon + SwapPss      ->  B = η Σᵢ m̄ᶠᵍᵢ
for t in 1..T:
   1. 解冻 r_t (若被我们冻结), am start -W r_t            -> L_t, LaunchState(HOT/WARM/COLD)
   2. settle_s 后采样 (一次 adb 往返: meminfo, vmstat, PSI, zram mm_stat, swaps, 每个进程 smaps_rollup/stat/oom_score_adj/cgroup.freeze)
   3. 由采样构造 AppInfo(mᵢ, aᵢ, ρ̂, Ĉresumeᵢ, b_keep) -> policy.choose_resident -> S_t  (r_t ∈ S_t, never_freeze ⊆ S_t)
   4. 执行:  i ∉ S_t: freeze + reclaim(anon);   i ∈ S_t \ {r_t}: 按 freeze_resident 决定是否只冻结不回收
   5. dwell_s 后再次采样, 写一行 JSON (type=step), 把控制器状态写入 <name>.ckpt (断点)
cleanup   解冻全部, 释放气球, 恢复 lmkd, 写 type=end, 删除 .ckpt, 打印摘要
```

每一步的 adb 命令、内核行为与验证方法见 `docs/03_adb_mechanisms.md`。

### 冻结 (y_i)
`/sys/fs/cgroup/uid_<uid>/pid_<pid>/cgroup.freeze` 写 1/0 (Android 11+ 的 cgroup v2 freezer, 与系统 cached-app
freezer 相同机制)。找不到时回退 `kill -STOP/-CONT`。**冻结不释放内存** —— 这是文档 §1 强调的区分, runner 分开记录
`frozen` 与 `compressed` 两个集合。

### 回收 / 换出 (z_i, s_i)
按探测结果依次尝试 (只作用于该应用自己的进程, 按进程名匹配):
1. `memory.reclaim` (cgroup v2 memcg 且 memory 控制器已启用) — 精确到应用, 可带 `swappiness=`;
2. memcg v1 `memory.force_empty` — Android 15 模拟器实测走这一条: 应用都在根 memcg, 工具建
   `/dev/memcg/cf/uid_<uid>`、置 `move_charge_at_immigrate=3`、迁入进程后 force_empty, 匿名页进 zram、文件页丢弃
   (真机 per_app_memcg=true 时直接用应用自带的 `apps/uid_X/pid_Y` 组);
3. `/proc/<pid>/reclaim` 写 `anon` / `all` (部分 Android 通用内核);
4. tmpfs 气球: 在 `/data/local/tmp/cf_balloon` 上挂 tmpfs, 用 `/dev/urandom` 填不可压缩页, 把 MemAvailable 压到
   `balloon_reserve_mb`, 由内核 LRU 挤出最冷 (已冻结) 应用的页面。全局、不精确, 但任何内核都可用。

`reclaim_mode=anon` 只在 `/proc/<pid>/reclaim` 可用时真正生效 (文件页留在 DRAM, 模型里作为 `b_keep` 计入压缩后占用);
memcg 路径总是同时丢弃文件页 (对应 e^b_i, 之后产生 R^file refault, 由 `refault_file` 报告)。
每步 JSON 的 `actions.reclaim` 记录每个应用实际使用的机制, 汇总表 `reclaim_methods` 列会列出。

### 预算与 η 扫描
预算只计 **后台** 应用: `M_bg(t) = Σ_{i≠r_t} (Pss_i + ρ̂·SwapPss_i)`; `budget_violation` 记录 M_bg > B。
若全部压缩后仍 > B (η 太小), 策略只保留 r_t, 其余全部压缩 —— 此时应报告 "不可达" 而非硬凑 10%。
`cf.sim.run_sim` 的 `eta_floor` 给出合成实例的可达下界。

## 2. 策略 (`cf/policies.py`)

| 名称 | 文档对应 | 说明 |
|---|---|---|
| `none` | 不冻结 | 什么都不做, 只观测。配合 `--system-freezer enabled` 即 "Android 默认 cached-app freezer" 基线 |
| `lru` / `lfu` | LRU/LFU | 按最近使用 / 衰减频率排序, 从最低优先级开始压缩直到满足预算 (按需换页, 不预取) |
| `markov` | 仅按预测排序 | 一阶 Markov 链 (Laplace 平滑) 精确计算 p̂ᵢ(t,H) = Pr[H 步内访问 i], 按 p̂ᵢ·Ĉresumeᵢ 排序; `prefetch=true` 时主动把高概率应用换回 |
| `landlord` | §3.4 鲁棒信用 | 信用 qᵢ ∈ [0, cᵢ], cᵢ = 一次压缩–恢复周期代价; 需要释放时 δ = min qᵢ/sizeᵢ, sizeᵢ = 压缩可释放的 DRAM (mᵢ − ρaᵢ − b_keep) |
| `hybrid` | 预测 + 信用 (§3.5 第 3 步) | Landlord 信用只允许被预测 **抬高** (仍 ≤ cᵢ, 保持最坏情况保证); 权重 w 随观测到的加权逆序误差 η_rank 衰减, 分布漂移时自动退回纯 Landlord |
| `belady` | 离线启发 | 最远未来使用优先 (sim 用, 需完整 σ) |
| `bellman_opt` | 定理 1 | `cf/sim/bellman.py`: 子集状态 DP, 与穷举一致 (tests) |

真机 runner 里的 Ĉresumeᵢ 由该应用已观测的 "压缩态恢复 − 常驻态恢复" 平均时延给出, 没有观测前用
`60 ms + aᵢ / 1.2 GB·s⁻¹` 先验。

## 3. 指标 (`cf/analyze.py`)

| 输出列 | 文档指标 | 来源 |
|---|---|---|
| `bg_pss_avg_mb`, `bg_pss_peak_mb` | 平均及峰值后台 PSS | Σ_{i≠r_t} Pss (dwell 后采样) |
| `zram_logical_avg_mb`, `zram_phys_avg_mb` | ZRAM 逻辑量 / 物理量 | Σ SwapPss; `mm_stat.mem_used_total` |
| `swap_write_mb`, `swap_read_mb` | 累计换出/换入 (W_cum, R^swap) | Δ`pswpout`, Δ`pswpin` × 页大小 |
| `refault_anon`, `refault_file`, `pgmajfault` | refault 与 major fault | Δ`workingset_refault_*`, Δ`pgmajfault` |
| `lat_p50/95/99_ms`, `lat_compressed_p50_ms`, `lat_resident_p50_ms` | 恢复时延 P50/P95/P99 (按是否从 ZRAM 恢复拆分) | `am start -W` TotalTime |
| `n_hot/n_warm/n_cold`, `n_killed` | 冷启动 (被杀) 次数 | LaunchState, pid 消失 |
| `psi_full_ms`, `psi_some_ms` | 前台卡顿代理 | Δ`/proc/pressure/memory` total |
| `budget_violations` | M(t) ≤ B_t 违约 | 见上 |
| `decision_p99_ms`, `action_p95_ms` | T^ML 在线路径开销 (§3.5 第 2 步 δ^ML) | 策略决策 / 执行动作耗时 |
| `bg_pss_avg_over_sum_fg` | 实际达到的 η | bg_pss_avg / Σ m̄ᶠᵍ |

`--pareto` 输出 (policy, η) → (内存节省 %, P95 时延, 累计写入) 三元组, `--plot` 绘制 "内存节省–恢复时延–闪存写入" 前沿。

## 4. 建议的实验矩阵

```bash
# A. 基线: Android 默认 freezer (需 --no-strict, 因为系统 freezer 是开着的) / 不冻结 (矩阵里的 none)
scripts/prepare_device.sh --system-freezer enabled  && NO_STRICT=1 POLICIES=none SEEDS="1 2 3" OUT=results/android_default scripts/run_matrix.sh
scripts/prepare_device.sh --system-freezer disabled     # 之后的矩阵 B 自带 none 基线

# B. 策略 x η x 种子 (同一种子 => 同一 σ; 3 个种子给出均值与方差)
POLICIES="none lru lfu landlord markov hybrid" ETAS="0.15 0.25 0.35 0.5" SEEDS="1 2 3" scripts/run_matrix.sh configs/emulator_base.json 60

# C. 分布漂移: 在 T/2 处打乱应用流行度, 观察 hybrid 的 w 是否回落到 Landlord
python3 run_experiment.py configs/emulator_base.json --policies markov hybrid landlord --eta 0.3 --T 80   # 配置里 trace.drift_at=40

# D. 内存压力: 2 GB 客体重跑 B
scripts/start_emulator.sh cf_api35 2048 && scripts/prepare_device.sh --no-reboot && ...

# E. 离线最优: 把真机轨迹回放进 sim, 用测得的 m_i, a_i, ρ 求 OPT(σ) 下界
python3 -m cf.sim.run_sim --trace replay --trace-path results/matrix_*/landlord_eta0.3.jsonl --k 10 --T 60
```

每个 (策略, η) 至少重复 3 个种子; 报告均值与置信区间, 模拟器上的时延抖动 (宿主调度) 明显大于真机。
`run_matrix.sh` 把一个矩阵的全部输出放在一个目录里, 结束后只对该目录做 `cf.analyze`; 第一格 warmup 测得的
m̄ᶠᵍ 通过 `--m-fg-from` 复用到其余所有格, 使同一 η 在每次运行里都是同样多 MB 的预算 (否则 Σm̄ᶠᵍ 在运行间 ±7% 浮动,
Pareto 图的横坐标随之漂移)。

## 6. 断点续跑 (checkpoint / resume)

一次 40 步 × 16 应用的实验在模拟器上要跑几十分钟, 矩阵要跑数小时; Ctrl+C、模拟器崩溃、adb 超时、宿主休眠都不应让已跑的步骤作废。

**机制** (`cf/runner.py`):

* 每完成一步, 把完整控制器状态 pickle 到 `<out_dir>/<name>.ckpt` (原子替换): 策略对象 (Landlord 信用、LRU 时间戳、Markov 转移计数、
  hybrid 的 w 与误差窗口)、当前常驻 / 冻结 / 压缩集合、恢复时延历史 (Ĉresume 估计用)、上一步的 pid 集合 (用于统计被杀)、
  warmup 得到的 m̄ᶠᵍ / a / L_cold、预算 B、完整轨迹 σ 与已完成步数。JSONL 仍是唯一的数据文件, `.ckpt` 只是状态快照。
* 任何异常 (`KeyboardInterrupt`、`AdbError`、超时、设备离线) 都会: 写一条 `{"type": "interrupted", "after_step": t}`,
  尽力解冻所有应用, **保留** `.ckpt`, 并在控制台打印续跑命令。
* 续跑 (`--resume`): 以追加模式重开 JSONL 与 `.log`, 重新探测设备、重建 zram / 动画设置 (模拟器可能已重启),
  **跳过 warmup** (沿用 checkpoint 里的 m̄ᶠᵍ, 保证预算不变), 先全部解冻再按 checkpoint 把 `compressed` 集合重新回收、
  `frozen` 集合重新冻结 (进程已不在的记为将要 COLD), 写一条 `{"type": "resume", "from_step": t+1}`, 从 t+1 步继续。
  轨迹来自 checkpoint 而不是重新生成, 因此续跑前后是同一个 σ。
* 正常结束写 `{"type": "end"}` 并删除 `.ckpt`; `run_experiment.py` 遇到已 `end` 的同名结果直接跳过 (不会覆盖), 遇到有 `.ckpt` 的
  同名结果自动续跑, `--overwrite` 强制重来。
* `cf.analyze` 对续跑文件: 同一 t 的重复 step (崩溃时写了半行) 以后者为准; 内核计数器 (`pswpout` 等) 按相邻步差值累加,
  模拟器重启导致的计数器回绕不会算出负数; 汇总表新增 `resumes` (续跑次数) 与 `finished` 两列, 论文里可据此标注。

**用法**:

```bash
# 单次实验: 用结果文件路径续跑 (策略/η/T/名称全部来自 checkpoint)
python3 run_experiment.py configs/emulator_base.json --resume results/landlord_eta0.3.jsonl
# 或重复原命令并加 --resume
python3 run_experiment.py configs/emulator_base.json --policy landlord --eta 0.3 --T 40 --name landlord_eta0.3 --resume

# 矩阵: 指定原来的输出目录即可, 已完成 (policy, η) 跳过, 未完成的续跑, 剩下的照常
OUT=results/matrix_20260914-005939 scripts/run_matrix.sh configs/emulator_base.json 40
scripts/run_matrix.sh --resume results/matrix_20260914-005939
```

续跑会在汇总里留下痕迹 (`resumes ≥ 1`), 中断期间系统状态 (页缓存、zram 内容、模拟器性能) 与连续运行不完全相同; 严谨起见,
Pareto 图上的最终数据点建议来自未中断或只中断一次的运行, 中断多次的运行用于调试。

## 7. 控制台与日志

* 所有输出带时间戳与级别 (`INFO / OK / WARN / ERR / STEP / HEAD`), 同时写入 `<out_dir>/<name>.log`
  (`run_matrix.sh` 另存原始控制台到 `<name>.console.log`), 不再依赖 `tee` 与 Python 缓冲 (已 `line_buffering`, 脚本内 `PYTHONUNBUFFERED=1`)。
* 每步一行: `[t/T 已用 ETA] 应用 状态 时延 <-来源(hot/frz/zram) | S/frz/zram 计数 | M_bg/B | 本步动作与耗时 | killed`; 有进程被杀的步用 WARN 级别。
* 开始前 preflight 对已知干扰因素告警: 系统 cached-apps freezer 未关、客体 RAM < 4 GB、无 swap、只剩气球回收、软件渲染 (SwiftShader/llvmpipe)、
  warmup 时应用未存活或落在引导页 (FRONT/TIMEOUT)。
* 结束时打印摘要 (launch 状态计数、时延分位、后台 PSS 与预算、swap 读写、refault、回收路径、警告条数) 并给出 `cf.analyze` 命令。

## 8. 干扰因素与工具的防护 (来自 9/13、9/16 两轮矩阵的教训)

| 干扰 | 表现 | 工具现在怎么做 |
|---|---|---|
| Android 自带 cached-apps freezer / compaction 仍在启用 (`cached_apps_freezer` 为 `null` = 默认开) | `none` 什么都没做却有 200 MB SwapPss; 控制器记录的 frozen 与内核状态有 ~9% 不一致 | `prepare_device.sh --system-freezer disabled` 同时关 `use_freezer` 与 `use_compaction` (native_boot 命名空间, 持久化), 重启后用 `dumpsys activity settings` 核对; runner **严格 preflight**: 未关则拒绝开跑 (`--no-strict` 仅用于 Android 默认基线) |
| 模拟器退到软件渲染 (SwiftShader) | 宿主可用内存 < 5 GB 时 `-gpu auto` 静默切换; HOT 启动也要 ~1 s, 所有时延被渲染主导 | `start_emulator.sh` 默认 `-gpu host` (Metal), 不再让模拟器自选; 开机后检查日志与 `dumpsys SurfaceFlinger`; runner preflight 检测到 SwiftShader/llvmpipe 即拒绝开跑 |
| 后台进程被系统杀掉 (与内存无关: 6 GB 客体、4 GB 空闲时仍每轮 10–13 次, 与 3 GB 时同步同 App) | 之后的恢复变成 COLD (+1–1.6 s), 主导时延尾部 | 每次检测到进程消失即查 `dumpsys activity exit-info <pkg>`, 把 reason / description (如 `bg anr`、`Sync transaction while frozen`、`too many cached`) 写进 step 记录与进度行, 汇总表 `kill_reasons` 列; 对策按原因定: FREEZER → 系统 freezer 未关; ANR → 把该应用加入 `never_freeze` 或不在冻结期间与其交互; `too many cached` → `device_config put activity_manager max_cached_processes 64` |
| Σm̄ᶠᵍ 每次运行重测, ±7% 浮动 | 同一 η 预算不同, Pareto 横坐标漂移 | `--m-fg-from <jsonl>` / 矩阵自动复用第一格; `budget` 记录带 `m_fg_source` |
| 不同客体内存的矩阵被 `results/matrix_*/*.jsonl` 通配到一张图 | 同一 (策略, η) 两个点, 折线来回折返 | 汇总按 (策略, η, 客体 RAM) 分组, 不同 RAM 用不同标记且**从不连线**; `run_matrix.sh` 只汇总本目录 |
| `none` 按 η 画折线 | 8 个纯噪声样本被一条无关变量连起来 | `none` 画成水平参考带 (均值 ± sd), 不画折线 |
| 单种子, 噪声 (P95 ±400 ms) > 策略差异 | 策略排名不可复现 | `--seeds 1 2 3`; 图上点 = 种子均值, 误差棒 = ±1 sd; `summary_agg.csv` 给 mean/sd/n |

### 读 Pareto 图

`pareto.png` 左图: 横轴后台内存节省 (%), 纵轴恢复时延 P95; 右图纵轴累计 swap 写入。每个点是一个 (策略, η) 在所有种子上的均值,
误差棒 ±1 sd; 同一策略按 η 连线; 灰色水平带是 `none` 的均值 ± sd。`pareto_eta.png` 把横轴换成实际达到的 η (= 后台 PSS / Σm̄ᶠᵍ,
左大右小), 便于和文档里的 η 目标直接对照。两条策略曲线只有在误差棒不重叠时才谈得上谁优。

## 9. 已知局限

* 模拟器上 "闪存写入" 是宿主文件写入, 没有真机的写放大与磨损; 用 `pswpout` 作为代理量。
* `am start -W` 的 `TotalTime` 是到首帧的时间, 不含应用内部懒加载的 refault 尾部; `pgmajfault`/`refault` 是补充。
* 我们的控制器与 ActivityManager 的 OOM adj / lmkd 并行工作: 被我们压缩的进程仍可能被 lmkd 杀掉 (记为 COLD),
  这是真实系统里也存在的交互, 分析时单独列出 `n_killed`。
* 冻结进程的 Binder 调用方会阻塞; `never_freeze` 用于排除有前台服务 / 音频 / 推送依赖的应用 (E_t 安全集合)。
* 真机 (鸿蒙 PC / root Android) 迁移只需替换 `cf/device.py` 的探测路径, 策略与分析层不变。
