"""context_freeze: 多应用冻结 / 换出 / 恢复实验工具集.

模块划分:
- adb, device   : 与 Android 模拟器 / 真机交互 (指标采集、cgroup freezer、memcg 回收、tmpfs 气球)
- trace         : 生成请求序列 sigma = (r_1, ..., r_T)
- policies      : 在线策略 (none / lru / lfu / landlord / markov / hybrid)
- runner        : 在真实设备上执行实验循环, 输出 JSONL 轨迹
- analyze       : 汇总指标 (PSS / ZRAM / swap I/O / refault / 恢复时延 P50-P99 / Pareto)
- sim           : 两层 (DRAM / ZRAM) 合成验证与离线 Bellman 最优 (OPT)
"""

__version__ = "0.1.0"
