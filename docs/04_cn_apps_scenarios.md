# 中国大陆应用集合与使用场景

前几轮实验用的是 google_apis 镜像自带的 Google 应用 (Gmail/Maps/YouTube ...) 加几个 F-Droid 开源应用。它们和国内手机上的真实负载差别很大: 国产应用进程多 (微信常驻 `:push` `:tools` `:appbrand` 等 5–8 个进程)、前台 PSS 大 (250–800 MB)、后台保活积极、切换模式高度以微信为中心。本文档说明如何把实验换到以微信 / QQ / 微博 / 网易云音乐 / 网易邮箱大师 / 抖音 / 小红书 / 淘宝 / 支付宝 / 钉钉 / WPS / 高德 为主的应用集合上, 以及如何用"使用场景" (办公、刷社交媒体、通勤、购物、晚间娱乐) 生成切换路径。

## 1. 应用集合 (`cf/scenarios.py: CATALOG`, `configs/cn_apps.json`)

| 类别 | 应用 (包名) |
|---|---|
| 通讯 / 社交 | 微信 `com.tencent.mm`, QQ `com.tencent.mobileqq`, 微博 `com.sina.weibo`, 小红书 `com.xingin.xhs`, 知乎 `com.zhihu.android` |
| 视频 / 音频 | 抖音 `com.ss.android.ugc.aweme`, 快手 `com.smile.gifmaker`, 哔哩哔哩 `tv.danmaku.bili`, 爱奇艺 `com.qiyi.video`, 腾讯视频 `com.tencent.qqlive`, 网易云音乐 `com.netease.cloudmusic`, QQ音乐 `com.tencent.qqmusic`, 喜马拉雅 `com.ximalaya.ting.android` |
| 办公 | 网易邮箱大师 `com.netease.mail`, 钉钉 `com.alibaba.android.rimet`, 企业微信 `com.tencent.wework`, 飞书 `com.ss.android.lark`, WPS `cn.wps.moffice_eng`, 腾讯会议 `com.tencent.wemeet.app`, 腾讯文档 `com.tencent.docs`, 百度网盘 `com.baidu.netdisk` |
| 购物 / 支付 / 生活 | 淘宝 `com.taobao.taobao`, 京东 `com.jingdong.app.mall`, 拼多多 `com.xunmeng.pinduoduo`, 闲鱼 `com.taobao.idlefish`, 支付宝 `com.eg.android.AlipayGphone`, 美团 `com.sankuai.meituan` |
| 出行 / 资讯 | 高德地图 `com.autonavi.minimap`, 百度地图 `com.baidu.BaiduMap`, 今日头条 `com.ss.android.article.news` |

`configs/cn_apps.json` 默认取其中 14 个 (每个场景至少 4–7 个可用)。**6 GB 客体不要超过 14 个**: 这批应用单个前台 PSS 250–600 MB, 微信 / 淘宝 / 抖音可到 800 MB 以上, 14 个的 Σm̄ᶠᵍ 约 4–6 GB; warmup 要把它们全部启动一遍, 更多就会在 warmup 阶段被 lmkd 杀掉。配置里 `zram_mb` 已改为 2048 (η = 0.25 时后台预算 1–1.5 GB, 压缩集合按 ρ ≈ 0.35 需要 1.5–2 GB zram), `dwell_s` / `warmup_dwell_s` 也加长了, 因为这些应用启动后还要几秒才把首页信息流加载完, 太早采样会低估 m̄ᶠᵍ。

### 1.1 安装

镜像里没有这些应用, 它们也不在 Google Play。`scripts/install_cn_apps.py --download` 从国内应用商店自动下载并安装:

```bash
setopt interactivecomments 2>/dev/null   # zsh: 让行尾 "# 注释" 不被当成参数
cd ~/context_freeze && source scripts/env.sh
export ANDROID_SERIAL=emulator-5554

python3 scripts/install_cn_apps.py --download --config configs/cn_apps.json   # 14 个应用, 约 3.2 GB, 可断点续传
python3 scripts/install_cn_apps.py --list        # 核对: installed 列
scripts/first_run_cn_apps.sh configs/cn_apps.json                # 逐个手工点掉隐私协议 / 登录 (微信 QQ 用备用账号)
python3 run_experiment.py configs/cn_apps.json --probe          # uid / 启动 Activity 都能解析
python3 run_experiment.py configs/cn_apps.json --scenario list  # 每个场景实际会用到的应用
```

下载来源与 ABI 检查:

* **腾讯应用宝** (`upage.html5.qq.com/wechat-apkinfo`, 返回官方包的 url / 版本 / 大小 / md5) → **酷安** (`api.coolapk.com/v6/apk/download`, 302 到同一 CDN 的 64 位包) → 少数官网直链 (哔哩哔哩 `dl.hdslb.com/.../android64/`, 支付宝 `t.alipayobjects.com`)。
* API 31 起 arm64 模拟器镜像是**纯 64 位** (`ro.product.cpu.abilist=arm64-v8a`), 带 armeabi-v7a 原生库的包装不上 (`INSTALL_FAILED_NO_MATCHING_ABIS`)。应用宝给 小红书 / 哔哩哔哩 / 京东 / 支付宝 / 钉钉 的恰好是 32 位包, 所以下载前脚本先用两次 Range 请求读远端 APK 的 ZIP 中央目录, 看 `lib/<abi>/` 与模拟器的 abilist 是否有交集, 不匹配就换下一个来源, 不浪费流量。`--download --dry-run` 只打印每个来源会给什么, 不下载。
* 下载到 `apks/<包名>/<包名>-<版本>.apk`, 写到 `.part` 完成后校验大小 / md5 (应用宝) / ZIP 结构再改名; 中断后重跑同一条命令用 Range 续传; 目录里已有 APK 的应用直接跳过。
* 商店接口不是公开 API, 某天失效时 `--download` 会对该应用打印 `no installable build found`; 这时手工从 `--list` 给出的官网下载 `.apk` / `.xapk` 放进 `apks/`, 不带参数运行即可; 或从自己的手机导出: `--from-phone <serial> --config configs/cn_apps.json` (手机开 USB 调试, `$ADB devices -l` 里状态为 `device`; 用 `pm path` 拉 base.apk + split_*.apk)。
* 不加 `--config` 会处理目录里全部 30 个应用 (约 6 GB, 不建议; AVD `/data` 现在默认 16 GB, 旧 AVD 用 `scripts/resize_data.sh` 加大); 也可以只列包名: `python3 scripts/install_cn_apps.py --download com.tencent.mm com.sina.weibo`。

* 模拟器必须是 **arm64-v8a** 镜像 (M4 上 `scripts/setup_avd.sh` 创建的就是): 国产应用大多只带 arm 原生库, x86_64 镜像上会 `INSTALL_FAILED_NO_MATCHING_ABIS` 或走 ARM 翻译 (极慢且内存行为失真)。
* 安装用 `-g` 一次性授予运行时权限, 减少首次启动的弹窗。
* 少数应用检测到模拟器 / root 会拒绝运行或反复弹风控提示 (银行类、部分游戏、有时是支付宝的安全校验); 这类应用在 `--probe` 阶段仍显示可启动, 但 `am start -W` 会落在提示页上, 需要从 `apps` 里去掉。

### 1.2 首次启动 (必须手工做一遍)

```bash
scripts/first_run_cn_apps.sh configs/cn_apps.json            # 交互: 每个应用处理完按回车 (s 跳过, q 退出)
AUTO=1 DWELL=10 scripts/first_run_cn_apps.sh configs/cn_apps.json   # 不提问: 拉起 -> 停 DWELL 秒 -> force-stop -> 再拉起, 最后打印汇总表
```

`AUTO=1` (stdin 不是终端时自动启用) 只用来核对全部应用能否拉起、第二次启动的 `LaunchState/TotalTime` 是多少; 协议 / 登录仍要在模拟器窗口里点 (可以趁 DWELL 那几秒点, 或先不带 AUTO 跑一遍)。

每个应用第一次启动都有《个人信息保护指引》, 多数还有登录页 / 更新提示。`am start -W` 测的是"第一个 Activity 的首帧", 如果协议没点同意、账号没登录, 之后实验测到的就是协议页和登录页的启动时间, 而不是应用本身。脚本逐个拉起应用, 等你在模拟器窗口里处理完按回车, 然后 `force-stop` 再启动一次, 打印第二次的 `LaunchState/TotalTime` —— 这才是实验会看到的数字, 应该在几百 ms 到 1.5 s 之间并落在主页 Activity。

* 微信 / QQ 需要登录; 用备用账号, 模拟器登录可能触发风控。微博 / 网易云音乐 / 哔哩哔哩 / 抖音 / 淘宝 / 京东可以不登录浏览。
* 弹出"自启动 / 后台运行 / 通知权限"时选拒绝。这些应用后台唤醒频繁, 冻结后它们的 binder 事务会积压, ActivityManager 会以 `Sync transaction while frozen` / `bg anr` 杀掉进程 (runner 会记录原因), 这是国产应用集合上**预期会看到**的现象, 也是与 Google 应用集合最大的行为差异之一。
* 脚本把应用放进 ACTIVE standby bucket 并允许 `RUN_IN_BACKGROUND`, 使 Android 对它们一视同仁, 避免系统自己的分级限制混进对照。
* 拉起方式与 runner (`cf/device.py`) 相同: 先 `cmd package resolve-activity --brief -a MAIN -c LAUNCHER <pkg>` 解析出 `pkg/Activity`, 再 `am start -W -n pkg/Activity`。**不要**手工用 `am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER <pkg>` (只给包名的隐式 intent): 隐式启动要求 intent-filter 带 `android.intent.category.DEFAULT`, 国产应用的启动 Activity 都不带 (Android Studio 模板生成的也不带), 所以应用装得好好的也会报 `Error: Activity not started, unable to resolve Intent { ... pkg=com.tencent.mm }`。解析不到 Activity 时脚本打印 `pm path` / `dumpsys package` 里的 `installed= / enabled= / primaryCpuAbi=` 并退回 `monkey -p <pkg> -c LAUNCHER 1`。

## 2. 使用场景 (`cf/scenarios.py: SCENARIOS`)

一个场景是一小组应用加三种结构:

* **流行度** (`apps: {pkg: 权重}`) —— 场景内的重尾分布;
* **枢纽应用** (`hub`) —— 用户不断切回去的那个: 办公 / 社交里是微信, 通勤时是网易云音乐 (音乐在后台放着, 看完地图、刷完乘车码就切回来), 购物时是淘宝;
* **固定路径** (`flows`) —— 2–3 步的确定性切换链, 例如 网易邮箱 → WPS → 网易邮箱 (看附件、回信), 淘宝 → 支付宝 → 淘宝 (付款), 微博 → 微信 → 微博 (分享)。

采样时每一步以 `p_flow` 的概率走一条固定路径, 否则以 `p_hub` 的概率回枢纽, 否则以 `stickiness` 的概率回到上一个应用 (A → B → A), 否则按流行度抽一个不同于当前前台的应用。同一种子、同一可用应用集合下轨迹是确定的。

| 场景 | 枢纽 | 应用 (按权重) | 固定路径 | 特点 |
|---|---|---|---|---|
| `office` 办公 | 微信 | 微信 钉钉 WPS 网易邮箱 QQ 企业微信 腾讯会议 腾讯文档 网盘 飞书 | 邮箱→WPS→邮箱; 钉钉→会议→钉钉; 微信→腾讯文档→微信; 网盘→WPS; 钉钉→微信→钉钉 | 固定路径多 (p_flow 0.30), 重复性高, 对 Markov / hybrid 有利 |
| `social` 刷社交媒体 | 微信 | 微信 微博 抖音 小红书 哔哩哔哩 QQ 知乎 快手 (淘宝) | 微博→微信→微博; 抖音→微信→抖音; 小红书→淘宝→小红书; 哔哩→QQ→哔哩 | 消息驱动的打断-返回, stickiness 0.30 最高, 信息流应用内存增长快 |
| `commute` 通勤 | 网易云音乐 | 网易云音乐 微信 高德 支付宝 微博 今日头条 喜马拉雅 百度地图 | 高德→支付宝→网易云; 微信→网易云; 微博→微信→微博 | 音频应用后台常驻且被频繁切回 —— 冻结它会中断播放, 是 `never_freeze` 的典型候选 |
| `shopping` 购物 | 淘宝 | 淘宝 京东 支付宝 微信 拼多多 小红书 美团 闲鱼 | 淘宝→京东→淘宝; 淘宝→支付宝→淘宝; 京东→支付宝→京东; 美团→微信→美团; 小红书→淘宝 | 路径最长 (p_flow 0.35), 电商应用前台 PSS 最大, 压缩收益与换入代价都最大 |
| `evening` 晚间娱乐 | 哔哩哔哩 | 哔哩哔哩 抖音 微信 网易云音乐 爱奇艺 腾讯视频 微博 QQ | 哔哩→微信→哔哩; 抖音→微博→抖音; 爱奇艺→微信→爱奇艺 | 重量级视频进程, 切换少而每次驻留久 (可配更长 `dwell_s`) |
| `day` 一天 | — | 上述场景依次: 通勤 15% → 办公 40% → 社交 15% → 购物 10% → 娱乐 20% | — | 段与段之间流行度整体改变 = 自然的分布漂移, 替代 `drift_at`; 分析按段拆分 |

权重和路径是设计假设 (为了产生真实的**结构**: 重尾枢纽、A→B→A 返回、多应用固定链), 不是测量数据; 如果有自己的使用记录, 用 `kind: replay` 回放, 或在配置里内联一个场景对象覆盖。

不在配置 / 未安装的应用会从场景中自动去掉 (路径也随之缩短), 开跑时 WARN 列出被去掉的应用; 一个场景剩下不到 2 个应用则报错。`python3 run_experiment.py configs/cn_apps.json --scenario list` 显示当前配置下每个场景实际会用的应用。

## 3. 运行

```bash
python3 run_experiment.py configs/cn_apps.json --probe                       # 能力 + 每个应用的 uid / 启动 Activity
python3 run_experiment.py configs/cn_apps.json --scenario list

# 单个场景
python3 run_experiment.py configs/cn_apps.json --scenario office --policy landlord --eta 0.3 --T 60
# 一天 (分段分析)
python3 run_experiment.py configs/cn_apps.json --scenario day --policy hybrid --eta 0.3 --T 100
# 初次实验 (<= 10 小时): 五场景 x 4 策略 x η {0.25, 0.5} x 1 种子 = 35 格 (同目录、同预算分母、严格 preflight, 见 docs/02 §8)
SCENARIOS="office social commute shopping evening" POLICIES="none lru landlord hybrid" ETAS="0.25 0.5" SEEDS="1" \
  OUT=results/cn_matrix scripts/run_matrix.sh configs/cn_apps.json 60
# 完整矩阵 (30–40 小时): 同一目录补齐 η=0.35 与种子 2、3; 已完成的 35 格会被跳过
SCENARIOS="office social commute shopping evening" POLICIES="none lru landlord hybrid" ETAS="0.25 0.35 0.5" SEEDS="1 2 3" \
  OUT=results/cn_matrix scripts/run_matrix.sh configs/cn_apps.json 60
```

`bash scripts/bootstrap_cn.sh` 默认就是上面的初次实验, `FULL=1 bash scripts/bootstrap_cn.sh` 是完整矩阵, 见 §5。

结果文件名带场景: `landlord_office_eta0.3_s1.jsonl`。`cf.analyze` 的汇总表多了 `scenario` 列, 聚合按 (策略, 场景, η, 客体 RAM) 分组; 多个场景画在一张 Pareto 图上时每条曲线是 `策略@场景`; `day` 轨迹额外打印按段 (通勤 / 办公 / ...) 的 HOT/WARM/COLD 计数、时延 P50/P95、后台 PSS 与 swap 写入。

## 4. 与 Google 应用集合相比要预期的差异

* **多进程**: 冻结 / 回收按 `pkg` 与 `pkg:child` 匹配的全部进程 (`Device.pids_of_pkg`), 微信一次冻结 5–8 个进程; 保活进程被系统杀掉后应用会拉起新的, 所以 `killed_since_prev` 只在**所有**进程都消失时计数, 部分进程被杀会表现为下一次恢复 WARM 变 COLD-ish 而不计入 kill。
* **后台唤醒 → 冻结期间的 ANR / binder 积压 kill**: 会比 Google 应用多得多, `kill_reasons` 列会看到 `Sync transaction while frozen` 与 `bg anr`。这不是 bug, 而是冻结策略在真实负载上必须付出的代价之一; 对比 `none` 基线 (不冻结) 的 kill 数即可量化。想抑制可对个别应用 `never_freeze`, 或在 `first_run` 时关掉它们的通知与自启动。
* **内存随时间增长**: 信息流应用 (微博 / 抖音 / 小红书) 前台驻留期间持续加载内容, m̄ᶠᵍ 在 warmup 时测一次会偏低; 用 `--m-fg-from` 固定分母只保证运行间可比, 不保证等于真实峰值。
* **网络**: 这些应用启动即拉取信息流, 模拟器要能访问国内服务; 网络慢时 `TotalTime` 里会混入等待首屏数据的时间 (`am start -W` 只等首帧, 通常影响不大, 但启动页广告会把首帧提前、把真正的主页推后)。
* **时长**: 单步 dwell 5 s + settle 2 s + 启动 1–3 s + 回收 1–4 s, 加上 14 个应用的 warmup (每个 10 s dwell) 与 zram 准备, 60 步一格约 12–16 分钟; 五场景 × 4 策略 × 3 η × 3 种子的完整矩阵 150 格约 30–40 小时。所以实验分两级做 (§5): 初次实验 35 格 (≤ 10 小时), 完整矩阵在同一目录补齐。`run_matrix.sh` 开跑前打印格数与预计时长, 并靠断点续跑分多次完成。

## 5. 分级实验计划: 初次实验 ≤ 10 小时, 完整矩阵 30–40 小时

一格 (T=60, 14 个应用) 约 12–16 分钟, 10 小时的预算约 35–45 格。完整矩阵 150 格无法一次跑完, 于是把 η 与种子两个维度分成两级, 第一级是第二级的**严格子集**, 同一输出目录 (`results/cn_matrix`) 续跑即可升级:

| 级别 | 命令 | 场景 | 策略 | η | 种子 | 格数 | 预计时长 |
|---|---|---|---|---|---|---|---|
| 冒烟 | `QUICK=1 bash scripts/bootstrap_cn.sh` | office | none, landlord | 0.3 | 1 | 2 (T=20) | ~15 分钟 |
| **初次实验** (默认) | `bash scripts/bootstrap_cn.sh` | 全部 5 个 | none, lru, landlord, hybrid | 0.25, 0.5 | 1 | 5 × (1 + 3 × 2) = **35** | **7–9.5 小时** |
| 完整矩阵 | `FULL=1 bash scripts/bootstrap_cn.sh` | 全部 5 个 | 同上 | 0.25, 0.35, 0.5 | 1, 2, 3 | 5 × (3 + 3 × 3 × 3) = 150 | 30–40 小时 (续跑只补 115 格) |

初次实验为什么这样裁:

* **保留全部五个场景**: 场景 (枢纽、固定路径、应用体量) 是这套应用集合区别于 Google 应用集合的核心变量, 少一个场景就少一类结论; 五个场景各 7 格, 每个场景内部仍是完整的 策略 × η 对照。
* **保留全部四个策略**: 对照的对象不能少; `none` 每个场景一格作为基线。
* **η 从 3 点减到 2 点 (0.25 / 0.5)**: 取最紧与最松两个预算, Pareto 曲线的两个端点已经给出每条曲线的方向和与 `none` 基线的距离; 中间点 0.35 留给完整矩阵补齐。不用 0.3/0.5 是为了让初次实验的每一格都能被完整矩阵直接复用。
* **种子从 3 个减到 1 个**: 单种子没有误差棒 (`summary_agg.csv` 里 `*_sd` 为 0, `n_runs`=1), 图上是点而不是 mean ± sd; 结论只能到"方向", 显著性要等完整矩阵。**不减 T**: T 决定每格的样本数 (P95 需要足够多的恢复事件), 60 步是下限。
* 初次实验结束后先看 `pareto.png`; 若某个场景 / 策略的差异已经很明显, 完整矩阵可以只对它补种子: `SCENARIOS=shopping SEEDS="2 3" ...` (其它参数不变, 同一 `OUT`)。
