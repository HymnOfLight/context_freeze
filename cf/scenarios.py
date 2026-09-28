"""Mainland-China app catalogue and usage scenarios for the switching / freezing experiments.

Two things live here:

* CATALOG - package name -> (display name, category, official download page) for the apps that
  dominate real Chinese phones (微信/QQ/微博/网易云音乐/网易邮箱大师/抖音/小红书/淘宝/支付宝/...).
  The emulator image does not ship them; scripts/install_cn_apps.py installs APKs from a local
  directory or pulls them from a connected phone.

* SCENARIOS - a usage scenario is a small behavioural model of one activity (办公 / 刷社交媒体 /
  通勤 / 购物 / 晚间娱乐): the apps involved with their popularity, a *hub* app the user keeps
  returning to (微信 for most scenarios, 网易云音乐 while commuting, 淘宝 while shopping) and a few
  *flows* - short fixed switching paths such as 邮箱 -> WPS -> 邮箱 (open an attachment and reply)
  or 淘宝 -> 支付宝 -> 淘宝 (pay). gen_scenario() samples a request sequence from that model;
  gen_day() concatenates scenarios (通勤 -> 办公 -> 社交 -> 购物 -> 娱乐), which gives a natural
  distribution drift for the habit-change experiments of the plan.

Popularities and flows are design assumptions, not measured data; they are meant to produce
realistic *structure* (a heavy-tailed hub, A->B->A returns, fixed multi-app paths), which is what
the residency policies react to. Every scenario is automatically restricted to the apps that are
present in the config and installed on the device; apps that are missing are reported, not
silently substituted.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Sequence

# ------------------------------------------------------------------ catalogue
# pkg: (name, category, where to get the APK)
CATALOG: dict[str, tuple[str, str, str]] = {
    # 通讯 / 社交
    "com.tencent.mm":                ("微信", "im",        "https://weixin.qq.com/"),
    "com.tencent.mobileqq":          ("QQ", "im",          "https://im.qq.com/"),
    "com.sina.weibo":                ("微博", "social",    "https://m.weibo.cn/"),
    "com.xingin.xhs":                ("小红书", "social",  "https://www.xiaohongshu.com/"),
    "com.zhihu.android":             ("知乎", "social",    "https://www.zhihu.com/app"),
    # 短视频 / 视频 / 音频
    "com.ss.android.ugc.aweme":      ("抖音", "video",     "https://www.douyin.com/download"),
    "com.smile.gifmaker":            ("快手", "video",     "https://www.kuaishou.com/"),
    "tv.danmaku.bili":               ("哔哩哔哩", "video", "https://app.bilibili.com/"),
    "com.qiyi.video":                ("爱奇艺", "video",   "https://www.iqiyi.com/"),
    "com.tencent.qqlive":            ("腾讯视频", "video", "https://v.qq.com/download.html"),
    "com.netease.cloudmusic":        ("网易云音乐", "music", "https://music.163.com/#/download"),
    "com.tencent.qqmusic":           ("QQ音乐", "music",   "https://y.qq.com/download/"),
    "com.ximalaya.ting.android":     ("喜马拉雅", "audio", "https://www.ximalaya.com/"),
    # 办公
    "com.netease.mail":              ("网易邮箱大师", "mail", "https://mail.163.com/dashi/"),
    "com.alibaba.android.rimet":     ("钉钉", "office",    "https://www.dingtalk.com/download"),
    "com.tencent.wework":            ("企业微信", "office", "https://work.weixin.qq.com/#indexDownload"),
    "com.ss.android.lark":           ("飞书", "office",    "https://www.feishu.cn/download"),
    "cn.wps.moffice_eng":            ("WPS Office", "office", "https://www.wps.cn/"),
    "com.tencent.wemeet.app":        ("腾讯会议", "office", "https://meeting.tencent.com/download/"),
    "com.tencent.docs":              ("腾讯文档", "office", "https://docs.qq.com/"),
    "com.baidu.netdisk":             ("百度网盘", "office", "https://pan.baidu.com/download"),
    # 购物 / 支付 / 生活
    "com.taobao.taobao":             ("淘宝", "shopping",  "https://www.taobao.com/"),
    "com.jingdong.app.mall":         ("京东", "shopping",  "https://app.jd.com/"),
    "com.xunmeng.pinduoduo":         ("拼多多", "shopping", "https://www.pinduoduo.com/"),
    "com.taobao.idlefish":           ("闲鱼", "shopping",  "https://www.goofish.com/"),
    "com.eg.android.AlipayGphone":   ("支付宝", "payment", "https://mobile.alipay.com/index.htm"),
    "com.sankuai.meituan":           ("美团", "life",      "https://www.meituan.com/mobile/download"),
    # 出行 / 资讯
    "com.autonavi.minimap":          ("高德地图", "map",   "https://mobile.amap.com/"),
    "com.baidu.BaiduMap":            ("百度地图", "map",   "https://map.baidu.com/zt/client/index/"),
    "com.ss.android.article.news":   ("今日头条", "news",  "https://www.toutiao.com/"),
}


def display_name(pkg: str) -> str:
    return CATALOG.get(pkg, (pkg.rsplit(".", 1)[-1],))[0]


# ------------------------------------------------------------------ scenarios
@dataclass
class Scenario:
    name: str
    title: str
    apps: dict[str, float]                 # pkg -> relative popularity inside the scenario
    hub: str                               # the app the user keeps coming back to
    flows: list[list[str]] = field(default_factory=list)   # fixed multi-app switching paths
    p_hub: float = 0.30                    # P(next request is the hub | not in a flow, current != hub)
    p_flow: float = 0.25                   # P(start one of the flows)
    stickiness: float = 0.20               # P(return to the app before the current one: A -> B -> A)
    description: str = ""


MM, QQ, WEIBO, XHS, ZHIHU = "com.tencent.mm", "com.tencent.mobileqq", "com.sina.weibo", "com.xingin.xhs", "com.zhihu.android"
DOUYIN, KUAISHOU, BILI, IQIYI, QQLIVE = ("com.ss.android.ugc.aweme", "com.smile.gifmaker", "tv.danmaku.bili",
                                         "com.qiyi.video", "com.tencent.qqlive")
MUSIC163, QQMUSIC, XIMALAYA = "com.netease.cloudmusic", "com.tencent.qqmusic", "com.ximalaya.ting.android"
MAIL163, DINGTALK, WEWORK, LARK, WPS, WEMEET, TDOCS, NETDISK = (
    "com.netease.mail", "com.alibaba.android.rimet", "com.tencent.wework", "com.ss.android.lark",
    "cn.wps.moffice_eng", "com.tencent.wemeet.app", "com.tencent.docs", "com.baidu.netdisk")
TAOBAO, JD, PDD, XIANYU, ALIPAY, MEITUAN = ("com.taobao.taobao", "com.jingdong.app.mall", "com.xunmeng.pinduoduo",
                                            "com.taobao.idlefish", "com.eg.android.AlipayGphone", "com.sankuai.meituan")
AMAP, BAIDUMAP, TOUTIAO = "com.autonavi.minimap", "com.baidu.BaiduMap", "com.ss.android.article.news"

SCENARIOS: dict[str, Scenario] = {
    "office": Scenario(
        "office", "办公",
        apps={MM: 5, DINGTALK: 4, WPS: 3, MAIL163: 3, QQ: 2, WEWORK: 2, WEMEET: 1.5, TDOCS: 1.5, NETDISK: 1, LARK: 1},
        hub=MM,
        flows=[[MAIL163, WPS, MAIL163],            # 看附件 -> 回邮件
               [DINGTALK, WEMEET, DINGTALK],       # 群里点开会议链接
               [MM, TDOCS, MM],                    # 微信里打开共享文档
               [NETDISK, WPS],                     # 网盘里的文件用 WPS 打开
               [DINGTALK, MM, DINGTALK]],          # 工作 IM 与私人 IM 来回
        p_hub=0.30, p_flow=0.30, stickiness=0.20,
        description="以微信/钉钉为中心, 邮件-文档-会议之间的固定路径多, 切换较慢但重复性高"),
    "social": Scenario(
        "social", "刷社交媒体",
        apps={MM: 5, WEIBO: 4, DOUYIN: 4, XHS: 3, BILI: 3, QQ: 2, ZHIHU: 1.5, KUAISHOU: 1, TAOBAO: 0.5},
        hub=MM,
        flows=[[WEIBO, MM, WEIBO],                 # 分享到微信再回来
               [DOUYIN, MM, DOUYIN],
               [XHS, TAOBAO, XHS],                 # 种草 -> 看价格 -> 回来
               [BILI, QQ, BILI]],
        p_hub=0.35, p_flow=0.20, stickiness=0.30,
        description="消息驱动: 频繁被微信打断又切回信息流应用, A->B->A 返回占比最高"),
    "commute": Scenario(
        "commute", "通勤",
        apps={MUSIC163: 5, MM: 4, AMAP: 3, ALIPAY: 2.5, WEIBO: 2, TOUTIAO: 1.5, XIMALAYA: 1, BAIDUMAP: 1},
        hub=MUSIC163,
        flows=[[AMAP, ALIPAY, MUSIC163],           # 看路线 -> 乘车码 -> 回到音乐
               [MM, MUSIC163],
               [WEIBO, MM, WEIBO]],
        p_hub=0.35, p_flow=0.25, stickiness=0.20,
        description="音乐/播客在后台常驻并被反复切回, 地图与支付短暂进入前台"),
    "shopping": Scenario(
        "shopping", "购物",
        apps={TAOBAO: 5, JD: 3, ALIPAY: 3, MM: 3, PDD: 2, XHS: 2, MEITUAN: 1.5, XIANYU: 1},
        hub=TAOBAO,
        flows=[[TAOBAO, JD, TAOBAO],               # 比价
               [TAOBAO, ALIPAY, TAOBAO],           # 付款
               [JD, ALIPAY, JD],
               [MEITUAN, MM, MEITUAN],             # 拼单 / 分享
               [XHS, TAOBAO]],
        p_hub=0.30, p_flow=0.35, stickiness=0.25,
        description="电商应用间比价 + 支付跳转, 单次路径长, 电商应用内存大"),
    "evening": Scenario(
        "evening", "晚间娱乐",
        apps={BILI: 4, DOUYIN: 4, MM: 3, MUSIC163: 2, IQIYI: 2, QQLIVE: 1.5, WEIBO: 2, QQ: 1.5},
        hub=BILI,
        flows=[[BILI, MM, BILI],
               [DOUYIN, WEIBO, DOUYIN],
               [IQIYI, MM, IQIYI]],
        p_hub=0.25, p_flow=0.20, stickiness=0.30,
        description="长视频/短视频为主, 前台驻留时间长, 后台应用多为重量级视频进程"),
}

# a "day": scenarios in order with their share of the T requests
DAY_SCHEDULE: list[tuple[str, float]] = [
    ("commute", 0.15), ("office", 0.40), ("social", 0.15), ("shopping", 0.10), ("evening", 0.20)]


def scenario_from(spec) -> Scenario:
    """Name of a built-in scenario, or an inline dict {apps:{pkg:w}, hub, flows, p_hub, ...}."""
    if isinstance(spec, Scenario):
        return spec
    if isinstance(spec, str):
        if spec not in SCENARIOS:
            raise ValueError(f"unknown scenario {spec!r}; known: {', '.join(SCENARIOS)}")
        return SCENARIOS[spec]
    if isinstance(spec, dict):
        d = dict(spec)
        d.setdefault("name", "custom")
        d.setdefault("title", d["name"])
        if "hub" not in d:
            d["hub"] = max(d["apps"], key=d["apps"].get)
        return Scenario(**d)
    raise TypeError(f"scenario spec must be a name or a dict, got {type(spec).__name__}")


def restrict(sc: Scenario, apps: Sequence[str]) -> tuple[Scenario, list[str]]:
    """Drop apps that are not available (not in the config / not installed). Returns (scenario, dropped)."""
    avail = {a: w for a, w in sc.apps.items() if a in set(apps)}
    dropped = [a for a in sc.apps if a not in avail]
    if len(avail) < 2:
        raise ValueError(f"scenario {sc.name}: only {len(avail)} of its apps are available "
                         f"({', '.join(display_name(a) for a in avail) or 'none'}); need at least 2. "
                         f"Missing: {', '.join(display_name(a) for a in dropped)}")
    flows = []
    for f in sc.flows:
        g = [a for a in f if a in avail]
        g = [a for i, a in enumerate(g) if i == 0 or a != g[i - 1]]   # no consecutive repeats
        if len(g) >= 2:
            flows.append(g)
    hub = sc.hub if sc.hub in avail else max(avail, key=avail.get)
    return Scenario(sc.name, sc.title, avail, hub, flows, sc.p_hub, sc.p_flow, sc.stickiness, sc.description), dropped


def gen_scenario(apps: Sequence[str], T: int, spec, seed: int = 0, rng: random.Random | None = None,
                 start: str | None = None) -> list[str]:
    """Sample T requests from one scenario. `start` is the app currently in the foreground (for
    concatenation), never repeated as the first request."""
    sc, _ = restrict(scenario_from(spec), apps)
    rng = rng or random.Random(seed)
    names = list(sc.apps)
    weights = [sc.apps[a] for a in names]
    seq: list[str] = []
    cur, prev = start, None

    def push(a: str) -> None:
        nonlocal cur, prev
        if a == cur:
            return
        seq.append(a)
        prev, cur = cur, a

    while len(seq) < T:
        r = rng.random()
        if sc.flows and r < sc.p_flow:
            for a in rng.choice(sc.flows):
                push(a)
            continue
        r -= sc.p_flow
        if cur != sc.hub and r < sc.p_hub:
            push(sc.hub)
            continue
        r -= sc.p_hub
        if prev is not None and prev != cur and r < sc.stickiness:
            push(prev)
            continue
        for _ in range(50):
            cand = rng.choices(names, weights=weights)[0]
            if cand != cur:
                push(cand)
                break
    return seq[:T]


def gen_day(apps: Sequence[str], T: int, schedule: Sequence[Sequence] | None = None, seed: int = 0
            ) -> tuple[list[str], list[dict]]:
    """Concatenate scenarios according to `schedule` = [(scenario, share), ...]. Returns the trace
    and the segment table [{scenario, start, end}] (end exclusive) for per-scenario analysis."""
    schedule = [tuple(x) for x in (schedule or DAY_SCHEDULE)]
    tot = sum(float(s) for _, s in schedule)
    rng = random.Random(seed)
    # largest-remainder split of T over the segments so that they sum exactly to T
    raw = [T * float(s) / tot for _, s in schedule]
    lens = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - lens[i], reverse=True)[:T - sum(lens)]:
        lens[i] += 1
    trace: list[str] = []
    segments: list[dict] = []
    for (name, _), n in zip(schedule, lens):
        if n <= 0:
            continue
        start = len(trace)
        trace += gen_scenario(apps, n, name, rng=rng, start=trace[-1] if trace else None)
        segments.append({"scenario": scenario_from(name).name, "start": start, "end": len(trace)})
    return trace, segments


def describe(apps: Sequence[str] | None = None) -> str:
    """Human-readable overview of the scenarios (optionally restricted to `apps`)."""
    lines = []
    for sc in SCENARIOS.values():
        if apps is not None:
            try:
                sc, dropped = restrict(sc, apps)
            except ValueError as e:
                lines.append(f"{sc.name:9s} {sc.title}: UNUSABLE - {e}")
                continue
        else:
            dropped = []
        order = sorted(sc.apps, key=sc.apps.get, reverse=True)
        lines.append(f"{sc.name:9s} {sc.title}: hub={display_name(sc.hub)}  apps="
                     + "/".join(display_name(a) for a in order)
                     + f"  flows={len(sc.flows)}  p_hub={sc.p_hub} p_flow={sc.p_flow} stick={sc.stickiness}"
                     + (f"  (missing: {'/'.join(display_name(a) for a in dropped)})" if dropped else ""))
    lines.append("day       一天: " + " -> ".join(f"{n} {int(s * 100)}%" for n, s in DAY_SCHEDULE))
    return "\n".join(lines)
