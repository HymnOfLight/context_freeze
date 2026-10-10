"""Mainland-China desktop applications on Ubuntu 22.04 and the usage scenarios built from them.

Three groups, as in the experiment brief:

    download   百度网盘, browsers (Firefox / Chrome / Chromium / Edge), 腾讯视频, 哔哩哔哩
               -> background progress = bytes written to disk (/proc/<pid>/io write_bytes)
    push       微信, QQ, 钉钉
               -> background progress = desktop notifications delivered while throttled
    compute    WPS Office, 剪映, 中望CAD, 悟空图像 (PhotoSir), 小艺
               -> background progress = CPU time obtained (cpu.stat usage_usec) vs throttled time

Every entry records how the app is launched (`cmd`), how its processes are recognised
(`match`, regex on cmdline / exe) and how its window is recognised (`wm_class`, regex on
WM_CLASS; `name_rx` on the title as a fallback).  Those three fields are what you may have to
adjust after `python3 run_linux.py <config> --discover` on your machine, because vendor
packages change paths between releases - override them per app in the config's `app_overrides`.

Apps without a Linux build (剪映, 小艺) are kept in the catalogue with `linux=False` and a
`substitute`: a real open-source program of the same kind (kdenlive for video editing) or the
bundled placeholder (`python3 -m cf.linux.placeholder_app`), a small X11 program with a
configurable CPU / memory workload and a visible progress counter.  Substitutions are logged,
never silent.
"""
from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, asdict
from typing import Optional, Sequence

from ..scenarios import EXTRA_NAMES, Scenario

PLACEHOLDER_CMD = "python3 -m cf.linux.placeholder_app --app {id} --title '{name}'"


@dataclass
class LinuxApp:
    id: str
    name: str
    category: str                      # download | push | compute
    cmd: str                           # shell command (run from the repo root, in the user's session)
    match: str                         # regex: cmdline / exe / comm of the app's processes
    wm_class: str                      # regex on WM_CLASS (instance or class)
    name_rx: str = ""                  # regex on the window title (fallback)
    notify_names: tuple[str, ...] = () # app_name values the app passes to org.freedesktop.Notifications
    package: str = ""                  # dpkg package (availability check: dpkg -s)
    exe: str = ""                      # absolute path of the main binary (availability check)
    arch: tuple[str, ...] = ("x86_64", "aarch64")
    install: str = ""                  # where to get it
    linux: bool = True                 # has a native Linux build
    substitute: Optional[str] = None   # catalogue id used when this one is not installed
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _p(id_: str, name: str, category: str = "compute", **kw) -> LinuxApp:
    """Placeholder entry (bundled X11 program)."""
    return LinuxApp(id_, name, category, cmd=PLACEHOLDER_CMD.format(id=id_, name=name),
                    match=rf"placeholder_app.*--app {id_}\b", wm_class=rf"^cf-{id_}$",
                    install="bundled: python3 -m cf.linux.placeholder_app (needs python3-xlib)", **kw)


CATALOG: dict[str, LinuxApp] = {a.id: a for a in [
    # ------------------------------------------------------------- download
    LinuxApp("baidunetdisk", "百度网盘", "download",
             cmd="baidunetdisk", match=r"/opt/baidunetdisk/|baidunetdisk", wm_class=r"baidunetdisk",
             package="baidunetdisk", exe="/opt/baidunetdisk/baidunetdisk",
             install="https://pan.baidu.com/download#linux  (deb; x86_64 公开页, arm64 版见统信/麒麟应用商店或联系百度获取)",
             note="登录后把一个大文件的下载任务挂着, 实验期间它就是后台下载负载"),
    LinuxApp("firefox", "Firefox", "download",
             cmd="firefox", match=r"(^|/)firefox(-esr)?(\s|$)|/usr/lib/firefox", wm_class=r"^(firefox|Navigator)",
             package="firefox", exe="/usr/bin/firefox",
             install="apt (Mozilla APT 仓库 deb 版, 不要用 22.04 默认的 snap: snap 进程会被 snapd 放进自己的 systemd scope, 逃出我们的 cgroup)",
             note="deb 版: https://support.mozilla.org/kb/install-firefox-linux#w_install-firefox-deb-package-for-debian-based-distributions"),
    LinuxApp("chrome", "Google Chrome", "download",
             cmd="google-chrome-stable", match=r"/opt/google/chrome/chrome", wm_class=r"google-chrome|Google-chrome",
             package="google-chrome-stable", exe="/opt/google/chrome/chrome", arch=("x86_64",),
             install="https://www.google.com/chrome/  (deb, 仅 x86_64)"),
    LinuxApp("chromium", "Chromium", "download",
             cmd="chromium", match=r"/usr/lib/chromium/chromium|chromium-browser", wm_class=r"^chromium",
             package="chromium", exe="/usr/bin/chromium",
             install="deb 版: add-apt-repository ppa:xtradeb/apps && apt install chromium (snap 版同样不可用)"),
    LinuxApp("edge", "Microsoft Edge", "download",
             cmd="microsoft-edge-stable", match=r"/opt/microsoft/msedge/", wm_class=r"microsoft-edge",
             package="microsoft-edge-stable", exe="/opt/microsoft/msedge/msedge", arch=("x86_64",),
             install="https://www.microsoft.com/edge/download?platform=linux  (deb, 仅 x86_64)"),
    LinuxApp("tencent_video", "腾讯视频", "download",
             cmd="tencent-video", match=r"tencent.?video|qqlive|com\.tencent\.video", wm_class=r"tencent.?video|qqlive",
             name_rx=r"腾讯视频", package="com.tencent.video", exe="",
             install="https://v.qq.com/download.html  (Linux 版 deb, x86_64 / arm64; 命令名与包名以安装后 --discover 看到的为准)",
             note="打开一个剧集并开启 '缓存/下载' 任务, 作为后台下载负载; cmd / wm_class 请按 --discover 结果在 app_overrides 里改"),
    LinuxApp("bilibili", "哔哩哔哩", "download",
             cmd="bilibili", match=r"io\.github\.msojocs\.bilibili|/opt/apps/.*bilibili|bilibili", wm_class=r"bilibili",
             name_rx=r"哔哩哔哩|bilibili", package="io.github.msojocs.bilibili",
             exe="/opt/apps/io.github.msojocs.bilibili/files/bin/bilibili",
             install="https://github.com/msojocs/bilibili-linux/releases  (deb, amd64 / arm64; 统信应用商店有官方客户端)",
             note="登录后开一个视频缓存任务"),
    # ----------------------------------------------------------------- push
    LinuxApp("wechat", "微信", "push",
             cmd="wechat", match=r"/opt/wechat/|(^|/)wechat(\s|$)|WeChat", wm_class=r"^(wechat|WeChat)",
             name_rx=r"微信|WeChat", notify_names=("微信", "wechat", "WeChat"),
             package="wechat", exe="/opt/wechat/wechat",
             install="https://linux.weixin.qq.com/  (官方 deb, x86_64 / arm64 / loongarch64)",
             note="每次启动需手机扫码确认; 用 scripts/ubuntu/cf_launch.sh wechat 启动并登录后再跑实验 (runner 对已运行的应用只切窗口不重启)"),
    LinuxApp("qq", "QQ", "push",
             cmd="qq", match=r"/opt/QQ/|(^|/)qq(\s|$)", wm_class=r"^(qq|QQ)$",
             name_rx=r"^QQ", notify_names=("QQ", "qq"),
             package="linuxqq", exe="/opt/QQ/qq",
             install="https://im.qq.com/linuxqq/  (官方 deb, x86_64 / arm64 / loongarch64; Electron)"),
    LinuxApp("dingtalk", "钉钉", "push",
             cmd="dingtalk", match=r"com\.alibabainc\.dingtalk|/opt/apps/.*dingtalk|dingtalk", wm_class=r"dingtalk",
             name_rx=r"钉钉|DingTalk", notify_names=("钉钉", "DingTalk", "dingtalk"),
             package="com.alibabainc.dingtalk", exe="/opt/apps/com.alibabainc.dingtalk/files/Elevator.sh",
             install="https://page.dingtalk.com/wow/dingtalk/act/download  (Linux 版 deb, x86_64 / arm64)"),
    # -------------------------------------------------------------- compute
    LinuxApp("wps", "WPS Office", "compute",
             cmd="wps", match=r"/opt/kingsoft/wps-office/|wpsoffice|(^|/)(wps|et|wpp|wpspdf)(\s|$)",
             wm_class=r"^(wps|Wps|et|wpp|wpspdf|wpsoffice)", package="wps-office", exe="/usr/bin/wps",
             install="https://linux.wps.cn/  (deb, x86_64 / arm64)",
             note="打开一个几十页带图的文档并让它做拼写检查 / 重新分页, 作为后台计算负载"),
    LinuxApp("jianying", "剪映", "compute",
             cmd="", match=r"jianying|capcut", wm_class=r"jianying|capcut", linux=False, substitute="kdenlive",
             install="无 Linux 版 (剪映专业版只有 Windows / macOS). 默认替代: kdenlive (apt install kdenlive, 同类视频剪辑负载); 也可用 Wine/deepin-wine 运行 Windows 版, 自行在 app_overrides 里给出 cmd/match/wm_class"),
    LinuxApp("zwcad", "中望CAD", "compute",
             cmd="zwcad", match=r"(?i)zwcad", wm_class=r"(?i)zwcad", name_rx=r"中望|ZWCAD",
             package="zwcad", exe="",
             install="https://www.zwsoft.cn/product/zwcad-linux  (Linux 版 deb / run, x86_64 / arm64 国产平台)",
             substitute="librecad", note="没有安装时用 librecad (apt) 代替"),
    LinuxApp("photosir", "悟空图像 PhotoSir", "compute",
             cmd="photosir", match=r"(?i)photosir|wukong", wm_class=r"(?i)photosir", name_rx=r"悟空|PhotoSir",
             package="photosir", exe="",
             install="https://www.photosir.com/  (Linux 版 deb, x86_64 / arm64)",
             substitute="gimp", note="没有安装时用 gimp (apt) 代替"),
    LinuxApp("xiaoyi", "小艺", "compute",
             cmd="", match=r"xiaoyi|celia", wm_class=r"xiaoyi|celia", linux=False, substitute="xiaoyi_sim",
             install="华为小艺没有 Linux 桌面版 (鸿蒙 PC 自带). 默认替代: 内置占位程序 xiaoyi_sim (周期性 CPU 计算 + 固定内存, 显示进度计数, 可验证状态连续性)"),
    # ----------------------------------------------- open-source substitutes
    LinuxApp("kdenlive", "Kdenlive (剪映替代)", "compute",
             cmd="kdenlive", match=r"(^|/)kdenlive(\s|$)", wm_class=r"^kdenlive", package="kdenlive",
             exe="/usr/bin/kdenlive", install="apt install kdenlive"),
    LinuxApp("librecad", "LibreCAD (中望CAD替代)", "compute",
             cmd="librecad", match=r"(^|/)librecad(\s|$)", wm_class=r"^librecad", package="librecad",
             exe="/usr/bin/librecad", install="apt install librecad"),
    LinuxApp("gimp", "GIMP (悟空图像替代)", "compute",
             cmd="gimp", match=r"(^|/)gimp(-[\d.]+)?(\s|$)", wm_class=r"^gimp", package="gimp",
             exe="/usr/bin/gimp", install="apt install gimp"),
    _p("xiaoyi_sim", "小艺(占位)", "compute"),
    _p("sim_download", "下载占位", "download"),
    _p("sim_push", "推送占位", "push"),
    _p("sim_compute", "计算占位", "compute"),
]}

CATEGORIES = {"download": "下载", "push": "通知推送", "compute": "计算处理"}

EXTRA_NAMES.update({a.id: a.name for a in CATALOG.values()})


def display_name_for(cat: Optional[dict[str, LinuxApp]], id_: str) -> str:
    """Human name for a catalogue id (or cgroup leaf name); unknown ids are returned as-is."""
    a = (cat or CATALOG).get(id_)
    return a.name if a else EXTRA_NAMES.get(id_, id_)


def app(id_: str) -> LinuxApp:
    if id_ not in CATALOG:
        raise KeyError(f"unknown app {id_!r}; known: {', '.join(CATALOG)}")
    return CATALOG[id_]


def with_overrides(overrides: Optional[dict]) -> dict[str, LinuxApp]:
    """Catalogue with per-app field overrides from the config (`app_overrides`), plus new apps
    defined there with at least cmd / match / wm_class."""
    cat = {k: LinuxApp(**v.to_dict()) for k, v in CATALOG.items()}
    for id_, ov in (overrides or {}).items():
        if id_ in cat:
            d = cat[id_].to_dict()
            d.update({k: v for k, v in ov.items() if k in d})
            if "notify_names" in ov:
                d["notify_names"] = tuple(ov["notify_names"])
            if "arch" in ov:
                d["arch"] = tuple(ov["arch"])
            cat[id_] = LinuxApp(**d)
        else:
            d = {"id": id_, "name": ov.get("name", id_), "category": ov.get("category", "compute"),
                 "cmd": ov.get("cmd", id_), "match": ov.get("match", id_), "wm_class": ov.get("wm_class", id_)}
            for k in ("name_rx", "package", "exe", "install", "note", "substitute"):
                if k in ov:
                    d[k] = ov[k]
            if "notify_names" in ov:
                d["notify_names"] = tuple(ov["notify_names"])
            if "arch" in ov:
                d["arch"] = tuple(ov["arch"])
            cat[id_] = LinuxApp(**d)
            EXTRA_NAMES[id_] = d["name"]
    return cat


def _first_token(cmd: str) -> str:
    return cmd.split()[0] if cmd.strip() else ""


def is_installed(a: LinuxApp, arch: Optional[str] = None) -> bool:
    arch = arch or platform.machine()
    if not a.linux or not a.cmd:
        return False
    if arch not in a.arch and "any" not in a.arch:
        return False
    tok = _first_token(a.cmd)
    if tok == "python3":                       # placeholder: needs python3-xlib
        try:
            import Xlib  # noqa: F401
            return True
        except ImportError:
            return False
    if a.exe and os.path.exists(a.exe):
        return True
    if shutil.which(tok):
        return True
    if a.package and os.path.exists(f"/var/lib/dpkg/info/{a.package}.list"):
        return True
    return False


def resolve(ids: Sequence[str], cat: Optional[dict[str, LinuxApp]] = None, installed=None,
            allow_substitutes: bool = True) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """Map requested ids to runnable ids. Returns (used, substituted [(wanted, used)], missing)."""
    cat = cat or CATALOG
    installed = installed or is_installed
    used: list[str] = []
    subs: list[tuple[str, str]] = []
    missing: list[str] = []
    for id_ in ids:
        if id_ not in cat:
            missing.append(id_)
            continue
        a = cat[id_]
        if installed(a):
            if id_ not in used:
                used.append(id_)
            continue
        cur, seen = a.substitute if allow_substitutes else None, {id_}
        while cur and cur in cat and cur not in seen:
            seen.add(cur)
            if installed(cat[cur]):
                if cur not in used:
                    used.append(cur)
                subs.append((id_, cur))
                break
            cur = cat[cur].substitute
        else:
            missing.append(id_)
    return used, subs, missing


def category_of(id_: str, cat: Optional[dict[str, LinuxApp]] = None) -> str:
    cat = cat or CATALOG
    return cat[id_].category if id_ in cat else "compute"


# ----------------------------------------------------------------- scenarios
WECHAT, QQ, DINGTALK = "wechat", "qq", "dingtalk"
NETDISK, FIREFOX, CHROME, CHROMIUM, EDGE, TVIDEO, BILI = ("baidunetdisk", "firefox", "chrome", "chromium",
                                                          "edge", "tencent_video", "bilibili")
WPS, JIANYING, ZWCAD, PHOTOSIR, XIAOYI = "wps", "jianying", "zwcad", "photosir", "xiaoyi"

SCENARIOS: dict[str, Scenario] = {
    "office": Scenario(
        "office", "办公",
        apps={WECHAT: 5, WPS: 5, DINGTALK: 4, FIREFOX: 3, NETDISK: 2, QQ: 1, CHROME: 1, XIAOYI: 1},
        hub=WECHAT,
        flows=[[NETDISK, WPS, NETDISK],          # 网盘里的文件用 WPS 打开再回去
               [DINGTALK, WPS, DINGTALK],        # 钉钉收到文档 -> 编辑 -> 回复
               [WECHAT, FIREFOX, WECHAT],        # 微信里的链接
               [WPS, XIAOYI, WPS]],              # 问一下助手再回文档
        p_hub=0.30, p_flow=0.30, stickiness=0.20,
        description="微信/钉钉为枢纽, WPS 长时间前台, 网盘在后台下载; 固定路径多"),
    "design": Scenario(
        "design", "设计/计算",
        apps={ZWCAD: 5, PHOTOSIR: 4, JIANYING: 3, WECHAT: 3, FIREFOX: 2, WPS: 1, NETDISK: 1},
        hub=ZWCAD,
        flows=[[PHOTOSIR, WECHAT, PHOTOSIR],     # 发图给同事再回来改
               [ZWCAD, FIREFOX, ZWCAD],          # 查规范
               [JIANYING, WECHAT, JIANYING],
               [NETDISK, PHOTOSIR]],
        p_hub=0.30, p_flow=0.25, stickiness=0.25,
        description="重计算应用 (CAD / 图像 / 视频剪辑) 在后台继续渲染导出; 限 1 核后进度变慢但不中断"),
    "download": Scenario(
        "download", "下载",
        apps={WECHAT: 4, NETDISK: 4, BILI: 3, TVIDEO: 3, FIREFOX: 3, WPS: 2, QQ: 1},
        hub=WECHAT,
        flows=[[BILI, WECHAT, BILI],
               [TVIDEO, FIREFOX, TVIDEO],
               [NETDISK, WPS, NETDISK],
               [FIREFOX, NETDISK]],              # 浏览器里的链接转存网盘
        p_hub=0.25, p_flow=0.25, stickiness=0.30,
        description="三个下载客户端 + 浏览器在后台持续写盘; 观察限 CPU / 内存后下载吞吐 (write_bytes) 的变化"),
    "chat": Scenario(
        "chat", "消息",
        apps={WECHAT: 5, QQ: 4, DINGTALK: 4, FIREFOX: 2, BILI: 2, WPS: 1, XIAOYI: 1},
        hub=WECHAT,
        flows=[[QQ, WECHAT, QQ],
               [DINGTALK, WECHAT, DINGTALK],
               [WECHAT, BILI, WECHAT]],
        p_hub=0.35, p_flow=0.25, stickiness=0.30,
        description="三个 IM 在后台收推送; 限制后通知是否仍按时到达 (notifications) 是核心观测量"),
}

DAY_SCHEDULE: list[tuple[str, float]] = [("chat", 0.15), ("office", 0.40), ("download", 0.15), ("design", 0.30)]


def scenario_dict(name: str) -> dict:
    if name not in SCENARIOS:
        raise ValueError(f"unknown Linux scenario {name!r}; known: {', '.join(SCENARIOS)}")
    return asdict(SCENARIOS[name])


def resolve_trace_cfg(trace: dict) -> dict:
    """Replace Linux scenario names by inline scenario dicts so cf.trace / cf.scenarios (which only
    know the Android catalogue by name) build the right trace."""
    tr = dict(trace)
    kind = tr.get("kind", "zipf")
    if kind == "scenario":
        sc = tr.get("scenario", "office")
        if isinstance(sc, str):
            tr["scenario"] = scenario_dict(sc)
    elif kind == "day":
        sched = tr.get("schedule") or DAY_SCHEDULE
        tr["schedule"] = [[scenario_dict(n) if isinstance(n, str) else n, float(s)] for n, s in sched]
    return tr


def remap_scenario_apps(sc_dict: dict, mapping: dict[str, str]) -> dict:
    """Apply substitutions (jianying -> kdenlive) inside a scenario dict; drop apps that vanished."""
    apps = {}
    for a, w in sc_dict["apps"].items():
        b = mapping.get(a, a)
        if b is not None:
            apps[b] = max(w, apps.get(b, 0))
    flows = []
    for f in sc_dict.get("flows", []):
        g = [mapping.get(a, a) for a in f]
        g = [a for a in g if a is not None]
        g = [a for i, a in enumerate(g) if i == 0 or a != g[i - 1]]
        if len(g) >= 2:
            flows.append(g)
    hub = mapping.get(sc_dict["hub"], sc_dict["hub"])
    if hub is None or hub not in apps:
        hub = max(apps, key=apps.get) if apps else sc_dict["hub"]
    return {**sc_dict, "apps": apps, "flows": flows, "hub": hub}


def describe(apps: Optional[Sequence[str]] = None, cat: Optional[dict[str, LinuxApp]] = None) -> str:
    from ..scenarios import display_name, restrict
    cat = cat or CATALOG
    lines = []
    for sc in SCENARIOS.values():
        if apps is not None:
            try:
                sc2, dropped = restrict(sc, apps)
            except ValueError as e:
                lines.append(f"{sc.name:9s} {sc.title}: UNUSABLE - {e}")
                continue
        else:
            sc2, dropped = sc, []
        order = sorted(sc2.apps, key=sc2.apps.get, reverse=True)
        lines.append(f"{sc2.name:9s} {sc2.title}: hub={display_name(sc2.hub)}  apps="
                     + "/".join(display_name(a) for a in order)
                     + f"  flows={len(sc2.flows)}  p_hub={sc2.p_hub} p_flow={sc2.p_flow} stick={sc2.stickiness}"
                     + (f"  (missing: {'/'.join(display_name(a) for a in dropped)})" if dropped else ""))
    lines.append("day       一天: " + " -> ".join(f"{n} {int(s * 100)}%" for n, s in DAY_SCHEDULE))
    return "\n".join(lines)


def catalog_table(cat: Optional[dict[str, LinuxApp]] = None, arch: Optional[str] = None) -> str:
    cat = cat or CATALOG
    rows = [("id", "name", "category", "installed", "cmd", "install / note")]
    for a in cat.values():
        inst = "yes" if is_installed(a, arch) else ("no (no linux build)" if not a.linux else "no")
        if a.substitute and inst != "yes":
            inst += f" -> {a.substitute}"
        rows.append((a.id, a.name, a.category, inst, a.cmd or "-", (a.install or a.note)[:90]))
    widths = [max(len(str(r[i])) for r in rows) for i in range(5)]
    return "\n".join("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r[:5])) + "  " + r[5] for r in rows)
