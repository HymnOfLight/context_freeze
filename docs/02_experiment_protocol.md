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
   5. dwell_s 后再次采样, 写一行 JSON (type=step)
cleanup   解冻全部, 释放气球, 恢复 lmkd
```

### 冻结 (y_i)
`/sys/fs/cgroup/uid_<uid>/pid_<pid>/cgroup.freeze` 写 1/0 (Android 11+ 的 cgroup v2 freezer, 与系统 cached-app
freezer 相同机制)。找不到时回退 `kill -STOP/-CONT`。**冻结不释放内存** —— 这是文档 §1 强调的区分, runner 分开记录
`frozen` 与 `compressed` 两个集合。

### 回收 / 换出 (z_i, s_i)
按探测结果依次尝试:
1. `memory.reclaim` (cgroup v2 memcg, 内核 ≥ 5.19 / Android 15 模拟器 6.6 内核) — 精确到应用;
2. `/proc/<pid>/reclaim` 写 `anon` / `all` (Android 通用内核, 部分 5.x);
3. tmpfs 气球: 在 `/data/local/tmp/cf_balloon` 上挂 tmpfs, 用 `/dev/urandom` 填不可压缩页, 把 MemAvailable 压到
   `balloon_reserve_mb`, 由内核 LRU 挤出最冷 (已冻结) 应用的页面。全局、不精确, 但任何内核都可用。

`reclaim_mode=anon` 只压匿名页 (文件页留在 DRAM, 模型里作为 `b_keep` 计入压缩后占用);
`reclaim_mode=all` 同时丢弃文件页 (对应 e^b_i, 之后产生 R^file refault)。

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
# A. 基线: 不冻结 / Android 默认 freezer
scripts/prepare_device.sh --system-freezer enabled  && python3 run_experiment.py configs/emulator_baseline_none.json --name android_default
scripts/prepare_device.sh --system-freezer disabled && python3 run_experiment.py configs/emulator_baseline_none.json --name none

# B. 策略 x η (同一随机种子 => 同一 σ)
POLICIES="lru lfu landlord markov hybrid" ETAS="0.15 0.25 0.35 0.5" scripts/run_matrix.sh configs/emulator_base.json 60

# C. 分布漂移: 在 T/2 处打乱应用流行度, 观察 hybrid 的 w 是否回落到 Landlord
python3 run_experiment.py configs/emulator_base.json --policies markov hybrid landlord --eta 0.3 --T 80   # 配置里 trace.drift_at=40

# D. 内存压力: 2 GB 客体重跑 B
scripts/start_emulator.sh cf_api35 2048 && scripts/prepare_device.sh --no-reboot && ...

# E. 离线最优: 把真机轨迹回放进 sim, 用测得的 m_i, a_i, ρ 求 OPT(σ) 下界
python3 -m cf.sim.run_sim --trace replay --trace-path results/matrix_*/landlord_eta0.3.jsonl --k 10 --T 60
```

每个 (策略, η) 至少重复 3 个种子; 报告均值与置信区间, 模拟器上的时延抖动 (宿主调度) 明显大于真机。

## 5. 已知局限

* 模拟器上 "闪存写入" 是宿主文件写入, 没有真机的写放大与磨损; 用 `pswpout` 作为代理量。
* `am start -W` 的 `TotalTime` 是到首帧的时间, 不含应用内部懒加载的 refault 尾部; `pgmajfault`/`refault` 是补充。
* 我们的控制器与 ActivityManager 的 OOM adj / lmkd 并行工作: 被我们压缩的进程仍可能被 lmkd 杀掉 (记为 COLD),
  这是真实系统里也存在的交互, 分析时单独列出 `n_killed`。
* 冻结进程的 Binder 调用方会阻塞; `never_freeze` 用于排除有前台服务 / 音频 / 推送依赖的应用 (E_t 安全集合)。
* 真机 (鸿蒙 PC / root Android) 迁移只需替换 `cf/device.py` 的探测路径, 策略与分析层不变。
