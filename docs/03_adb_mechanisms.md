# 技术说明: 用 adb 在 Android 上实现 "冻结 / 换出 / 换入" 的全部机制

本文回答一个问题: 文档 (《面向多应用冻结、换出与恢复的优化模型及研究综述》) 里的决策变量
**y_i(t) 冻结**、**z_i 换出到 ZRAM**、**s_i 换出到闪存**、**换入 / 恢复 (L_t)** 以及约束 **M(t) ≤ B_t**,
在一台 root 的 Android 设备 (这里是 Android 15 模拟器) 上, 到底是哪几条 `adb shell` 命令、
碰的是内核的哪几个文件、内核随后做了什么、又如何用 `/proc` `/sys` 验证它确实发生了。

所有命令都以 `adb shell` 为前缀 (`adb root` 之后 shell 即为 root, 无需 `su`)。
对应代码: `cf/adb.py` (命令封装), `cf/device.py` (机制), `cf/runner.py` (控制回路), `cf/parsers.py` (解析)。

```
                 ┌──────────────── 每一步 t 的 adb 命令时序 (cf/runner.py: Experiment.step) ────────────────┐
  r_t 被请求 ──► echo 0 > .../pid_*/cgroup.freeze      (解冻 r_t, 若被我们冻着)                            │
              ─► am start -W -n <pkg>/<Activity>        (换入 = 让进程自己去缺页; 读出 TotalTime/LaunchState)│
              ─► [一次 shell 脚本] meminfo vmstat psi zram/mm_stat swaps + 每进程 smaps_rollup/stat/oom/frz │
              ─► policy.choose_resident(...) -> S_t     (主机侧, 无 adb)                                    │
              ─► i ∉ S_t:  echo 1 > cgroup.freeze ; 建/进 memcg ; echo 0 > memory.force_empty   (冻结+换出)  │
                 i ∈ S_t\{r_t}: 仅 cgroup.freeze=1 (freeze_resident) —— 冻结不释放内存                        │
              ─► sleep dwell_s ; 再采样一次 ; 写一行 JSONL ; 写 checkpoint                                  │
              └───────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. 前提: 拿到 root 与看清内核布局

| 目的 | 命令 | 期望 / 说明 |
|---|---|---|
| root | `adb root && adb shell id -u` | `0`。`google_apis` / `default` 镜像可以; **Play 镜像会拒绝** |
| 内核 | `uname -r` | Android 15 模拟器: `6.6.x-android15` |
| cgroup v2 挂载点 | `cat /sys/fs/cgroup/cgroup.controllers` | 模拟器上为 **空** —— v2 层级只提供 freezer 语义, 没有 `memory` 控制器 |
| freezer 层级 | `ls /sys/fs/cgroup \| grep uid_` | `uid_10123/ uid_1000/ ...`; 每个应用进程在 `uid_<uid>/pid_<pid>/` |
| memcg v1 | `ls /dev/memcg; cat /dev/memcg/memory.limit_in_bytes` | Android 把 memcg **v1** 单独挂在 `/dev/memcg` |
| 应用是否各有 memcg | `getprop ro.config.per_app_memcg; cat /proc/<app pid>/cgroup \| grep memory` | 模拟器: 未设置, 应用都在根 memcg (`:memory:/`); 真机常见 `/apps/uid_X/pid_Y` |
| 每进程回收接口 | `ls /proc/self/reclaim` | 模拟器内核 **没有** (部分厂商内核有) |
| zram | `ls /sys/block/zram0; cat /proc/swaps` | Android 15 镜像默认已启用 `/dev/block/zram0` (lz4, 约 RAM 的 75%) |
| Android 自带 freezer | `settings get global cached_apps_freezer` | 想让**我们的**控制器独占 `cgroup.freeze` 必须是 `disabled` (见 §3.3) |

`cf/device.py: Device.probe()` 做的就是这张表, 结果写进 JSONL 的 `meta.caps`, `run_experiment.py --probe` 直接打印。
后面每种机制都按 probe 结果选路径 (`reclaim_methods` 顺序: `memcg_v2.memory.reclaim` → `memcg_v1.force_empty` → `proc_reclaim` → `balloon`)。

## 2. 应用身份: 包名 → uid → 进程集合

内核只认 pid / cgroup, 文档的 "应用 i" 要先落到具体进程:

```bash
cmd package list packages -U com.google.android.deskclock      # package:com.google.android.deskclock uid:10112
ps -A -o PID,UID,NAME | awk '$2==10112'                        # 该 uid 的所有进程
cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER com.google.android.deskclock
#   -> com.google.android.deskclock/com.android.deskclock.DeskClock   (am start 用的组件名)
```

**只按进程名匹配, 不按 uid 整组操作** (`Device.pids_of_pkg`): 进程名 == 包名, 或 `包名:子进程` (如 `com.android.chrome:sandboxed_process0`)。
原因是 **共享 uid**: Settings 与 `system_server` 同为 uid 1000, 按 uid 冻结会把整个系统冻住。
`Device.is_isolated_uid(uid) = uid ≥ 10000` 判断是不是普通应用 uid; 不是的包 runner 自动放进 `never_freeze` (文档的安全集合 E_t), 只观测不动作。

## 3. 冻结 y_i(t) — cgroup v2 freezer

### 3.1 命令

```bash
# 冻结 (对该应用的每一个进程各写一次; 路径来自 /proc/<pid>/cgroup 的 "0::/uid_X/pid_Y")
echo 1 > /sys/fs/cgroup/uid_10112/pid_4321/cgroup.freeze
# 解冻
echo 0 > /sys/fs/cgroup/uid_10112/pid_4321/cgroup.freeze
```

`Device.set_frozen(pkg, True/False)` 把一个应用所有进程的写操作拼成一条 shell (`echo 1 > A/cgroup.freeze ; echo 1 > B/cgroup.freeze`),
一次 adb 往返。**从不写 `uid_X/cgroup.freeze`** (那会连同该 uid 下不属于此应用的进程一起冻结)。

### 3.2 内核里发生了什么

cgroup v2 freezer (Linux ≥ 5.2) 给 cgroup 内每个任务发送一个内核内部信号, 任务在返回用户态的路径上进入 `TASK_FROZEN` 并停在
"冰箱" 里: 不再被调度、不再消耗 CPU, **但地址空间原样保留** —— RSS/PSS 一个字节都不变。这正是文档 §1 反复强调的
"冻结 ≠ 释放内存": 冻结只是把应用从 CPU 上摘下来, 让它随后的页面在 LRU 上变 "冷", 真正的内存收益必须靠 §4 的换出。
它与 Android 自带 cached-apps freezer 用的是**同一个文件**, 因此语义 (含对 Binder 的影响) 与系统行为一致。

与 `kill -STOP` 的区别: SIGSTOP 是用户可见的信号 (ptrace / `waitpid` 能观察到, 进程状态 `T`), 而 cgroup freezer 对进程透明, 状态显示为
`D`/`S` 但不会被唤醒; 没有 freezer 层级的旧镜像才回退到 `kill -STOP/-CONT` (`caps.freezer == "sigstop"`)。

### 3.3 验证

```bash
cat /sys/fs/cgroup/uid_10112/pid_4321/cgroup.events          # populated 1 / frozen 1
cat /proc/4321/smaps_rollup | grep -E '^(Pss|SwapPss):'       # 冻结前后不变 —— 证明冻结不释放内存
top -n1 -p 4321                                              # CPU 0%
```

采样脚本 (§6) 为每个进程读 `cgroup.freeze`, 写进 JSONL 每步 `after_dwell.apps.<pkg>.frozen`, analyze/tests 用它对账。

**必须先关掉 Android 自己的 freezer**, 否则 `ActivityManager` 的 `CachedAppOptimizer` 会在应用进入 cached 状态后几秒内也去写
同一个 `cgroup.freeze` (并且它还会通过 `BINDER_FREEZE` ioctl 冻结 binder), 我们解冻它又冻上、我们冻上它又解冻 —— 上一轮日志里
`frozen` 与决策不一致就是这个原因。关法与验证:

```bash
settings put global cached_apps_freezer disabled           # enabled | disabled | device_default
device_config put activity_manager_native_boot use_freezer false   # Android 11+ 读的是这个 namespace
adb reboot                                                 # 该设置在 system_server 启动时读取
dumpsys activity settings | grep -i use_freezer            # 期望 use_freezer=false
```

`scripts/prepare_device.sh --system-freezer disabled` 封装了以上步骤并在重启后打印验证结果; runner 的 preflight 检查到
`cached_apps_freezer != disabled` 会给出 WARN。跑 "Android 默认" 基线时反过来用 `enabled` + `policy=none`。

### 3.4 副作用: Binder

被冻结的进程不能响应 Binder 事务。Android 自己的 freezer 先 `BINDER_FREEZE` 让**发起方立刻得到错误**而不是挂起; 我们只写
`cgroup.freeze`, 因此若有别的进程同步调用被冻结的应用, 调用方会阻塞直到解冻。实验里把有前台服务 / 音频 / 推送依赖的包放进
`never_freeze`, 并在冻结后 `dwell_s` 内不与其交互, 已足够。Android 14+ 另有调试命令 `am freeze [--sticky] <pid>` / `am unfreeze <pid>`, 走系统的 `CachedAppOptimizer`
(会先 `BINDER_FREEZE`), 但它依赖系统 freezer 处于启用状态, 与 §3.3 "关闭系统 freezer 让控制器独占" 相冲突, 工具因此不用它。

## 4. 换出 z_i (→ ZRAM) 与 s_i (→ 闪存) — 让内核回收指定应用的页

冻结之后, 应用的页仍在 DRAM。要得到文档里的 z_i^a (匿名页压缩到 ZRAM) 和 e_i^b (文件页丢弃) 必须让内核**对这个应用单独**跑一遍
LRU 回收。Linux 没有 "把 pid 123 换出" 的系统调用, 可用的手段有四种, 工具按 probe 结果依次回退。

### 4.1 准备 ZRAM (压缩层)

```bash
cat /proc/swaps                                   # 已有 /dev/block/zram0 就什么都不用做 (Android 15 镜像默认开启)
# 没有时:
echo 1 > /sys/block/zram0/reset
echo lz4 > /sys/block/zram0/comp_algorithm        # 可选; cat 该文件看候选 [lz4] lzo zstd
echo 1024M > /sys/block/zram0/disksize            # 逻辑容量 (未压缩)
mkswap /dev/block/zram0
swapon -p 32767 /dev/block/zram0                  # 最高优先级 -> 内核先往 zram 换
cat /sys/block/zram0/mm_stat
#  orig_data_size compr_data_size mem_used_total mem_limit mem_used_max same_pages pages_compacted huge_pages
```

`mm_stat` 前三列就是文档的 ZRAM 逻辑量、压缩量、物理占用; `ρ̂ = mem_used_total / orig_data_size` 是 runner 每步估计的压缩比
(`Experiment._zram_ratio`, 记为 `rho_est`), 预算里换出页的物理占用按 `ρ̂ · SwapPss` 计。

### 4.2 路径 A: cgroup v2 memcg `memory.reclaim` (真机 / 有 memory 控制器的内核)

```bash
echo 4G > /sys/fs/cgroup/uid_10112/pid_4321/memory.reclaim        # 尽量回收这个 cgroup 的页, 直到无可回收
# 6.9+ 内核可加参数偏向匿名页: echo "4G swappiness=200" > memory.reclaim
```

要求 `cgroup.controllers` 含 `memory`, 模拟器不满足 (§1), 所以这条在模拟器上被跳过。

### 4.3 路径 B: memcg v1 `memory.force_empty` — **Android 15 模拟器实际走的路径**

问题: 应用都在根 memcg, 对根 `force_empty` 会把整机所有页回收掉。解法是为该应用**临时造一个 memcg, 把进程连同已计费的页一起迁进去**, 再只对这个组回收:

```bash
G=/dev/memcg/cf/uid_10112
mkdir -p $G
echo 3 > $G/memory.move_charge_at_immigrate    # bit0: 匿名页随任务迁移计费; bit1: 文件页也迁移 -> 3 = 两者
echo 4321 > $G/cgroup.procs                    # 进程 (及其线程) 迁入; 其已映射的页的 charge 一起搬过来
echo 4399 > $G/cgroup.procs                    #   ...该应用的其他进程
cat $G/memory.usage_in_bytes                   # 迁入后 ≈ 该应用 RSS (例: 44 MB)
echo 0 > $G/memory.force_empty                 # 只回收这个组: 匿名页 -> swap(zram), 干净文件页 -> 直接丢
cat $G/memory.usage_in_bytes                   # 例: 35 kB
```

内核动作: `force_empty` 反复调用 `try_to_free_mem_cgroup_pages()` 直到该 memcg 用量降到 0 或无可回收。匿名页走 `add_to_swap →
swap_writepage → zram`, `/proc/vmstat` 的 `pswpout` 增加; 干净文件页 (代码、资源 mmap) 直接释放, 脏页回写后释放。因此**文件页也被丢掉**
—— 文档模型里的 e_i^b, 之后重新访问会产生 file refault (`workingset_refault_file`)。这也是为什么配置项 `reclaim_mode=anon` 在此路径上
无法生效, runner 把 `b_keep` 设为 0 (`Experiment._infos`)。

实测 (Android 15 模拟器, Clock): `Pss 42 MB → 35 kB, SwapPss 0 → 22 MB`, zram `orig_data_size` 与 `pswpout` 同步增长。
`Device.reclaim()` 把 mkdir / move_charge / 迁 pid / force_empty 拼成两条 shell, 并把 `usage_in_bytes` 前后差写进 `last_reclaim_bytes`。

注意点:
* `memory.move_charge_at_immigrate` 与 `memory.force_empty` 在 6.x 内核标记为 deprecated (写入时 dmesg 有一行警告), 但 6.6 仍可用;
  真机若 `per_app_memcg=true`, 应用自带 `/dev/memcg/apps/uid_X/pid_Y`, 直接对该组 `force_empty`, 不需要迁移。
* 迁移只搬 **该任务 mm 已映射** 的页; 共享库页若被别的进程也映射且已计费给别人, 不会重复计费 —— PSS 分摊的语义与之一致。
* 组一直保留 (下次同一应用直接复用), 实验结束不必删除; `rmdir` 需要组内无进程。

### 4.4 路径 C: `/proc/<pid>/reclaim` (Android 通用内核补丁, 部分真机)

```bash
echo anon > /proc/4321/reclaim     # 只换出匿名页 (文件页留下 -> 文档的 b_keep)
echo file > /proc/4321/reclaim     # 只丢文件页
echo all  > /proc/4321/reclaim
```

这是唯一能区分匿名 / 文件页的接口, 也是 `reclaim_mode=anon` 真正生效的路径; 模拟器内核没有它。

### 4.5 路径 D: tmpfs 气球 (任何内核都可用的兜底, 全局、不精确)

```bash
mkdir -p /data/local/tmp/cf_balloon
mount -t tmpfs -o size=2048m tmpfs /data/local/tmp/cf_balloon
head -c 67108864 /dev/urandom > /data/local/tmp/cf_balloon/blk_0     # 64 MB 不可压缩页, 重复直到 MemAvailable 降到 reserve
rm -f /data/local/tmp/cf_balloon/blk_*                              # 放气
```

原理: tmpfs 页是不可换出的 shmem (且 urandom 内容不可压缩), 制造全局内存压力, 由内核 LRU 自行挑最冷的页换出 —— 被我们冻结的应用
恰好最冷, 所以大体上 "被冻结者先被换出", 但无法保证精确到应用, 且 lmkd 会先于内核 OOM 介入杀进程。仅当前三条都不可用时使用
(`reclaim_methods == ['balloon']`), runner 每步按 `MemAvailable - balloon_reserve_mb` 调气球大小。

### 4.6 闪存层 s_i: 交换文件 / zram writeback

```bash
dd if=/dev/zero of=/data/local/tmp/cf_swapfile bs=1M count=512
chmod 600 /data/local/tmp/cf_swapfile && mkswap /data/local/tmp/cf_swapfile
swapon -p 100 /data/local/tmp/cf_swapfile        # 优先级低于 zram(32767): zram 满了才写文件
```

这给出文档的第三层 (DRAM / ZRAM / 闪存): 冷页先进 zram, zram 满后溢出到 /data 上的文件, 累计写入用 `pswpout` 之差 (× 页大小)
近似 W_t。模拟器上文件写入落在宿主磁盘, 没有真机的写放大, 只能当代理量。
真机更接近产品行为的是 zram writeback (`echo /dev/block/by-name/... > /sys/block/zram0/backing_dev; echo idle > /sys/block/zram0/writeback`),
需要 `CONFIG_ZRAM_WRITEBACK`, 模拟器内核未开启。

### 4.7 为什么先冻结再回收

1. 冻结后进程不再运行, 回收出去的页不会被它下一毫秒又访问回来 (避免立刻 refault 造成的 "换出即换入");
2. 不运行的进程页面在 LRU 上不再被 "访问位" 提升, 内核回收时不会误判为热页而跳过;
3. 与文档的时间顺序一致: y_i(t)=1 是 z_i(t)>0 的前提。
runner 的 `_apply()` 对 i ∉ S_t 先 `set_frozen(True)` 再 `reclaim()`; 对 i ∈ S_t∖{r_t} 只冻结 (`freeze_resident=true`), 用来分离
"冻结" 与 "换出" 两种成本。

## 5. 换入 / 恢复 — L_t 的测量

Linux 同样没有 "把 pid 123 的页换回来" 的接口, 换入永远是**按需缺页**: 进程被解冻并执行, 触碰到已换出的页 → major fault →
`swap_readpage` 从 zram 解压 (或从交换文件读) → 继续执行。所以 "换入" 在 adb 层就是**解冻 + 把 Activity 拉到前台**:

```bash
echo 0 > /sys/fs/cgroup/uid_10112/pid_4321/cgroup.freeze
am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER \
   -n com.google.android.deskclock/com.android.deskclock.DeskClock
# Status: ok
# LaunchState: HOT            <- HOT: 进程与 Activity 都在 | WARM: 进程在, Activity 重建 | COLD: 进程被杀, 冷启动
# Activity: com.google.android.deskclock/com.android.deskclock.DeskClock
# TotalTime: 412              <- 从 startActivity 到首帧 (ms): 文档的 L_t
# WaitTime: 430
```

`-W` 让 `am` 阻塞到 Activity 首帧完成 (`ActivityTaskManager` 的 `WaitResult`), `TotalTime` 即恢复时延。要点:

* **LaunchState 是 ActivityManager 的视角, 不知道页在不在 DRAM**: 一个进程被我们压缩到 zram 后再拉起, 它仍是 `HOT`/`WARM`,
  但 `TotalTime` 里包含了几十到几百次 major fault 的解压时间。runner 因此另存 `was_compressed` (拉起前是否在 `compressed` 集合),
  analyze 按它拆出 `lat_compressed_p50_ms` 与 `lat_resident_p50_ms`, 二者之差就是策略里 Ĉresume_i 的估计来源。
* `COLD` 意味着进程在两次请求之间**被 lmkd 杀了** (或崩溃): 我们的控制器从不 kill, 因此 `n_cold`/`n_killed` 度量的是 lmkd 干扰;
  上一轮 3 GB 客体的日志中 COLD 占多数, 这就是把模拟器内存提到 6 GB 的原因。
* `LaunchState: UNKNOWN (0)` + `TotalTime: 0` / "brought to the front" 且无 TotalTime: Activity 已在前台, 没有发生恢复, 记为 `FRONT`
  不计时延 (warmup 后 runner 先按 HOME 就是为了避免第一步出现 FRONT)。
* `Status: timeout` / `UNKNOWN (-1)`: `am` 等首帧超时 (极慢的软件模拟器), 记为 `TIMEOUT`, `WaitTime` 作为下界。

**换入的代价在内核计数器里的位置** (与 L_t 互补, 采样脚本每步读):

| 现象 | 计数器 |
|---|---|
| 从 zram / 交换区读回的页数 | `/proc/vmstat` `pswpin` |
| 需要 I/O 或解压的缺页 | `/proc/vmstat` `pgmajfault`; 每进程 `/proc/<pid>/stat` 第 12 字段 `majflt` |
| 刚被回收又被访问 (回收决策错误的直接证据) | `workingset_refault_anon` / `workingset_refault_file` |
| 应用被压缩后仍在 swap 里的量 | `/proc/<pid>/smaps_rollup` `SwapPss` (换入后趋近 0) |

**主动预取 (换入但不切前台)**: 没有内核接口。可选的近似是 "拉到前台再立刻 `input keyevent KEYCODE_HOME`", 会污染时延统计, 工具默认不做;
`markov` 策略的 `prefetch=true` 只是把高概率应用**提前放回常驻集合** (不再对它回收), 页是否回来仍取决于它自己是否运行。

## 6. 采样: 一次 adb 往返读全所有指标

adb 每次 `shell` 有几十毫秒到 (慢模拟器上) 数秒的固定开销, 所以 `Device.sample()` 把所有读取拼成一段 shell 脚本, 用 `##TAG` 分隔, 主机侧
`_parse_sample()` 一次解析:

```bash
echo '##MEMINFO'; cat /proc/meminfo                     # MemTotal / MemAvailable / SwapFree
echo '##VMSTAT';  cat /proc/vmstat                      # pswpin pswpout pgmajfault workingset_refault_{anon,file}
echo '##PSI';     cat /proc/pressure/memory             # some/full avg10.. total(us): 前台卡顿代理
echo '##ZRAM';    cat /sys/block/zram0/mm_stat          # 逻辑/压缩/物理占用 -> ρ̂
echo '##SWAPS';   cat /proc/swaps                       # 各 swap 设备 used
echo '##PS';      ps -A -o PID,UID,NAME                 # pid -> 进程名 (匹配包名 / 包名:子进程)
ps -A -o PID,UID,NAME | while read pid uid name; do
  case "$name" in com.android.chrome|com.android.chrome:*|com.google.android.gm|com.google.android.gm:*)
    echo "##PID $pid $uid"; cat /proc/$pid/smaps_rollup   # Rss Pss Pss_Anon Pss_File Pss_Shmem SwapPss
    echo '##STAT'; cat /proc/$pid/stat                    # 第 12 字段 majflt
    echo '##OOM';  cat /proc/$pid/oom_score_adj           # lmkd 视角的优先级 (前台 0, cached 900+)
    echo '##FRZ';  cg=$(grep -m1 '^0::' /proc/$pid/cgroup | cut -d: -f3); cat /sys/fs/cgroup$cg/cgroup.freeze;;
  esac; done
```

与文档符号的对应 (按应用把该应用所有进程相加):

| 文档 | 采样值 |
|---|---|
| m_i(t) 应用占用 | `Pss + SwapPss` (前台 warmup 时的值即 m̄ᶠᵍᵢ) |
| a_i(t) 匿名工作集 | `Pss_Anon + SwapPss` |
| b_i(t) 文件工作集 | `Pss_File` |
| z_i^a 在 ZRAM 中的量 (逻辑) | `SwapPss`; 物理占用 ≈ ρ̂·SwapPss |
| M(t) 后台占用 (预算约束左侧) | Σ_{i≠r_t} (Pss_i + ρ̂·SwapPss_i) —— runner 的 `M_bg_kb` |
| W_t / R_t 交换写 / 读 | Δ`pswpout` / Δ`pswpin` × 页大小 |
| refault | Δ`workingset_refault_anon`, Δ`workingset_refault_file` |
| L_t | `am start -W` 的 `TotalTime` |
| 卡顿 | Δ`/proc/pressure/memory` `full total` |

为什么用 `smaps_rollup` 而不是 `smaps` / `dumpsys meminfo`: 前者是内核直接汇总 (一次 page-table walk, 无每 VMA 输出), 比 `smaps`
少 1–2 个数量级的输出, 比 `dumpsys meminfo` 少一次 Binder 到应用进程的往返 —— 对**被冻结的**进程 `dumpsys meminfo` 会挂住, `smaps_rollup` 不会。

## 7. 预算 B 与控制回路

```
warmup:  对每个 i: am force-stop i ; am start -W i ; sleep ; 采样 -> m̄ᶠᵍᵢ = Pss+SwapPss (前台)   ;  B = η · Σᵢ m̄ᶠᵍᵢ
step t:  §5 换入 r_t -> 采样 -> AppInfo(mᵢ, aᵢ, ρ̂, Ĉresumeᵢ, b_keep) -> policy -> S_t
         对 i ∉ S_t 且 i ∉ never_freeze:  §3 冻结 -> §4 回收        (compressed ∪= {i})
         对 i ∈ S_t ∖ {r_t}:               §3 冻结 (freeze_resident) (compressed −= {i}; 页仍在 zram, 由它自己按需换回)
         sleep dwell_s -> 采样 -> M_bg 与 B 比较 -> budget_violation
```

* 预算只计**后台**应用 (前台应用无论如何都在 DRAM), 与文档 "后台内存为前台基线的 η" 一致。
* 若 Σ 全部压缩后仍 > B (η 太小), 策略只保留 r_t 其余全压, `budget_violation` 记 true —— 这是 "η 不可达" 的诚实记录, 不是 bug;
  `cf.sim.run_sim` 的 `eta_floor` 给出可达下界。
* 每步 `decision_ms` (策略计算, 纯主机侧) 与 `action_ms` (§3+§4 的 adb 往返) 就是文档 T^ML 在线路径开销; 后者在模拟器上以 adb 延迟为主。

## 8. 一个完整的手工演示 (Clock, Android 15 模拟器)

```bash
adb root; adb shell
PKG=com.google.android.deskclock
UID=$(cmd package list packages -U $PKG | sed 's/.*uid://')
am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER \
   -n $(cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER $PKG | tail -1)
input keyevent KEYCODE_HOME
PID=$(pidof $PKG)
grep -E '^(Pss|Pss_Anon|SwapPss):' /proc/$PID/smaps_rollup       # 例: Pss 42 MB, SwapPss 0

# 冻结: 内存不变
echo 1 > /sys/fs/cgroup/uid_$UID/pid_$PID/cgroup.freeze
cat /sys/fs/cgroup/uid_$UID/pid_$PID/cgroup.events               # frozen 1
grep -E '^(Pss|SwapPss):' /proc/$PID/smaps_rollup                # 不变

# 换出: PSS 归零, SwapPss 与 zram 增长
G=/dev/memcg/cf/uid_$UID; mkdir -p $G
echo 3 > $G/memory.move_charge_at_immigrate; echo $PID > $G/cgroup.procs
cat /sys/block/zram0/mm_stat | awk '{print $1, $3}'; grep pswpout /proc/vmstat
echo 0 > $G/memory.force_empty
grep -E '^(Pss|SwapPss):' /proc/$PID/smaps_rollup                # 例: Pss 35 kB, SwapPss 22 MB
cat /sys/block/zram0/mm_stat | awk '{print $1, $3}'; grep pswpout /proc/vmstat   # 都增长了

# 换入: 解冻 + 拉起, 观察 majflt / pswpin
awk '{print "majflt", $12}' /proc/$PID/stat; grep pswpin /proc/vmstat
echo 0 > /sys/fs/cgroup/uid_$UID/pid_$PID/cgroup.freeze
am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -n $PKG/com.android.deskclock.DeskClock
awk '{print "majflt", $12}' /proc/$PID/stat; grep pswpin /proc/vmstat            # 都增长; TotalTime 比冻结前的 HOT 大
```

## 9. 已知陷阱与工具的处理

| 陷阱 | 现象 | 工具处理 |
|---|---|---|
| 共享 uid (Settings=1000) | 按 uid 冻结把 system_server 冻住, 设备假死 | 只按进程名操作; 非隔离 uid 自动进 `never_freeze` |
| Android cached-apps freezer 未真正关闭 | `frozen` 集合与决策不一致, 解冻后又被冻 | `prepare_device.sh` 写 settings + device_config 并重启后用 `dumpsys activity settings` 验证; runner preflight WARN |
| lmkd 杀被压缩的进程 | 大量 `COLD`, `n_killed` 高, 策略无从比较 | 客体 RAM 提到 6 GB (默认); 极端时 `"stop_lmkd": true` (内核 OOM 兜底) |
| 宿主内存不足 → 模拟器退到软件渲染 | 所有 `TotalTime` 翻数倍, 与内存无关 | `start_emulator.sh` 检查宿主可用内存与日志中的 "Software GL"; runner 读 `dumpsys SurfaceFlinger` 的 GLES 行并 WARN |
| memcg 回收连文件页一起丢 | 恢复时 file refault | 模型中 `b_keep=0`, analyze 单列 `refault_file` |
| `am start -W` 的 FRONT / TIMEOUT | 无 TotalTime 或伪 0 | 解析为 `FRONT`/`TIMEOUT`, 不进入时延统计, warmup 后 HOME |
| 冻结进程的 Binder 调用方阻塞 | 其他应用/系统服务卡住 | `never_freeze` (有前台服务 / 音频 / 推送依赖的包); 冻结期间不与其交互 |
| 实验中途 Ctrl+C / 模拟器崩溃 | 之前的数据白跑 | 每步写 `.ckpt`, `--resume` 续跑 (见 docs/02 §6) |

## 10. 迁移到真机 / 其他内核

* 有 `memory` 控制器的 cgroup v2: 自动改走 `memory.reclaim` (§4.2), 可精确到应用且可用 `swappiness=` 偏向匿名页 (6.9+)。
* `per_app_memcg=true` 的真机: 直接对 `/dev/memcg/apps/uid_X/pid_Y` `force_empty`, 不再迁移进程。
* 有 `/proc/<pid>/reclaim` 的内核: `reclaim_mode=anon` 生效, 文件页留在 DRAM (b_keep>0)。
* zram writeback 可用的真机: 用 `backing_dev` + `echo idle > writeback` 替代交换文件, 更接近产品的 "闪存层"。
以上都只影响 `cf/device.py` 的探测与执行, 策略 / 采样 / 分析层不变。
