"""Linux app catalogue (下载 / 通知推送 / 计算处理), substitutes for apps without a Linux build,
scenario consistency, config overrides and the dbus-monitor Notify parser."""
import pytest

from cf.linux import apps as A
from cf.linux.notify import NotifyCounter, parse_monitor_text
from cf.scenarios import display_name

REQUIRED = {
    "download": {"baidunetdisk", "firefox", "chrome", "tencent_video", "bilibili"},
    "push": {"wechat", "qq", "dingtalk"},
    "compute": {"wps", "jianying", "zwcad", "photosir", "xiaoyi"},
}


def test_catalogue_covers_the_requested_apps_by_category():
    for cat, ids in REQUIRED.items():
        for id_ in ids:
            assert id_ in A.CATALOG, id_
            assert A.CATALOG[id_].category == cat, id_
    assert set(A.CATEGORIES) == {"download", "push", "compute"}


def test_requested_apps_are_desktop_entries_with_the_mobile_build_as_first_fallback():
    """Brief: the Ubuntu desktop build of each app; only when none exists, the same app's mobile
    version.  Open-source look-alikes / the placeholder come after that and are opt-in."""
    for cat_, ids in REQUIRED.items():
        for id_ in ids:
            a = A.CATALOG[id_]
            assert a.kind == "desktop", id_
            chain = A.substitute_chain(id_)[1:]
            if chain:                                        # first fallback is the app's own mobile build
                first = A.CATALOG[chain[0]]
                assert first.kind == "mobile" and first.category == a.category, id_
                assert first.package and first.match == rf"^{first.package.replace('.', chr(92) + '.')}(:|$)"
                assert first.id == f"{id_}_mobile"
            if not a.linux:                                  # no desktop build at all -> must have a mobile entry
                assert chain and A.CATALOG[chain[0]].kind == "mobile", id_
    assert A.substitute_chain("jianying") == ["jianying", "jianying_mobile", "kdenlive"]
    assert A.substitute_chain("xiaoyi") == ["xiaoyi", "xiaoyi_mobile", "xiaoyi_sim"]
    assert A.substitute_chain("wechat") == ["wechat", "wechat_mobile"]
    assert A.CATALOG["wechat_mobile"].package == "com.tencent.mm"
    assert A.CATALOG["jianying_mobile"].package == "com.lemon.lv"
    assert {A.CATALOG[x].kind for x in ("kdenlive", "librecad", "gimp")} == {"oss"}
    # placeholder entries are bundled X11 programs with a quoted title
    ph = A.CATALOG["xiaoyi_sim"]
    assert ph.kind == "placeholder"
    assert ph.cmd.startswith("python3 -m cf.linux.placeholder_app") and "--title '小艺(占位)'" in ph.cmd
    assert ph.wm_class == "^cf-xiaoyi_sim$"


class _FakeMobile:
    wm_class = A.MOBILE_WM_CLASS

    def __init__(self, pkgs):
        self.pkgs = set(pkgs)

    def installed(self, pkg):
        return pkg in self.pkgs


def test_is_installed_for_mobile_entries_needs_a_runtime():
    a = A.CATALOG["wechat_mobile"]
    assert A.is_installed(a) is False                        # no Android runtime -> never "installed"
    assert A.is_installed(a, mobile=_FakeMobile([])) is False
    assert A.is_installed(a, mobile=_FakeMobile(["com.tencent.mm"])) is True
    # desktop entries ignore the runtime
    assert A.is_installed(A.CATALOG["jianying"], mobile=_FakeMobile(["com.lemon.lv"])) is False


def test_launch_cmd_falls_back_to_exe_when_the_launcher_is_not_on_path(tmp_path, monkeypatch):
    """DingTalk / bilibili debs install /opt/apps/.../files/... and a .desktop file but nothing on PATH:
    is_installed() says yes through `exe`, so the launch has to start `exe` too."""
    exe = tmp_path / "Elevator.sh"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(A.shutil, "which", lambda tok: None)
    a = A.LinuxApp("dingtalk", "钉钉", "push", "dingtalk", "x", "x", exe=str(exe))
    assert A.launch_cmd(a) == str(exe)
    assert A.launch_cmd(A.LinuxApp("b", "b", "push", "bilibili --no-sandbox", "x", "x", exe=str(exe))) == f"{exe} --no-sandbox"
    assert A.launch_cmd(A.LinuxApp("w", "w", "push", "wps ~/x.docx", "x", "x", exe=str(tmp_path / "missing"))) == "wps ~/x.docx"
    assert A.launch_cmd(A.LinuxApp("p", "p", "push", "python3 -m cf.linux.placeholder", "x", "x", exe=str(exe))).startswith("python3")
    monkeypatch.setattr(A.shutil, "which", lambda tok: "/usr/bin/" + tok)
    assert A.launch_cmd(a) == "dingtalk"                                   # on PATH: untouched


def _fake_deb(tmp_path, pkg, desktop_files):
    """A dpkg `.list` for `pkg` plus the .desktop files it names (dict path-under-tmp -> text)."""
    info = tmp_path / "dpkg"
    info.mkdir(parents=True, exist_ok=True)
    paths = []
    for rel, text in desktop_files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        paths.append(str(p))
    (info / f"{pkg}.list").write_text("\n".join([f"/opt/apps/{pkg}", f"/opt/apps/{pkg}/files/bin/x", *paths]) + "\n")
    return str(info)


def test_launch_cmd_falls_back_to_the_desktop_exec_of_the_installed_package(tmp_path, monkeypatch):
    """星火 / UOS debs (`com.xunlei.download`, `com.iqiyi.otohime` ...) install under /opt/apps/<pkg>/
    and only ship a .desktop file: the app is recognised through `alt_packages` and started with
    the menu entry's Exec line (field codes stripped, cmd arguments kept)."""
    monkeypatch.setattr(A.shutil, "which", lambda tok: None)
    info = _fake_deb(tmp_path, "com.xunlei.download", {
        "opt/apps/com.xunlei.download/entries/applications/com.xunlei.download.desktop":
            "[Desktop Entry]\nName=迅雷\nExec=/opt/apps/com.xunlei.download/files/start.sh %U\n"
            "StartupWMClass=Thunder\nIcon=x\n\n[Desktop Action New]\nExec=/bin/false\n",
        "opt/apps/com.xunlei.download/files/hidden.desktop":
            "[Desktop Entry]\nName=helper\nNoDisplay=true\nExec=/bin/true\n",
    })
    a = A.LinuxApp("xunlei", "迅雷", "download", "thunder", "x", "x", package="xunlei",
                   alt_packages=("com.xunlei.download",))
    assert A.installed_package(a, info) == "com.xunlei.download"
    d = A.desktop_entry(a, info)
    assert d["exec"] == "/opt/apps/com.xunlei.download/files/start.sh"      # %U dropped, action group ignored
    assert d["name"] == "迅雷" and d["startupwmclass"] == "Thunder" and d["package"] == "com.xunlei.download"
    assert A.launch_cmd(a, info) == "/opt/apps/com.xunlei.download/files/start.sh"
    b = A.LinuxApp("xunlei", "迅雷", "download", "thunder --no-sandbox", "x", "x", alt_packages=("com.xunlei.download",))
    assert A.launch_cmd(b, info) == "/opt/apps/com.xunlei.download/files/start.sh --no-sandbox"
    # nothing installed -> cmd is left alone (the launch fails with 127 and is logged as such)
    assert A.installed_package(A.LinuxApp("q", "q", "download", "quark", "x", "x", package="quark"), info) is None
    assert A.launch_cmd(A.LinuxApp("q", "q", "download", "quark", "x", "x", package="quark"), info) == "quark"
    # a package whose only .desktop is NoDisplay has no usable launcher
    info2 = _fake_deb(tmp_path / "t2", "shimo", {"opt/apps/shimo/x.desktop": "[Desktop Entry]\nNoDisplay=true\nExec=/bin/true\n"})
    assert A.desktop_entry(A.LinuxApp("shimo", "x", "compute", "shimo", "x", "x", package="shimo"), info2) is None


def test_parse_desktop_entry_handles_comments_quotes_and_missing_group():
    d = A.parse_desktop_entry("# comment\n[Desktop Entry]\nExec=\"/opt/a b/run\" --flag %f %u\nName=A\nName[zh_CN]=甲\n")
    assert d["exec"] == '"/opt/a b/run" --flag' and d["name"] == "A"
    assert A.parse_desktop_entry("Exec=/bin/true\n") == {}                 # no [Desktop Entry] group


def test_catalog_marks_the_arm64_additions_and_their_packages():
    cat = A.CATALOG
    for i in ("xunlei", "iqiyi", "douyin", "netease_music", "shimo"):
        assert i in cat and "aarch64" in cat[i].arch and cat[i].package
    assert "com.baidu.baidunetdisk" in cat["baidunetdisk"].alt_packages
    assert cat["xunlei"].alt_packages[0] == "xunlei"


def test_resolve_prefers_desktop_then_mobile_and_only_opts_into_oss_or_placeholder():
    desktop = {"wechat", "wps"}
    mobile = {"com.lemon.lv", "com.tencent.mm"}            # 剪映 and 微信 APKs present
    mb = _FakeMobile(mobile)
    inst = lambda a: A.is_installed(a, mobile=mb) if a.is_mobile else a.id in desktop or a.id in {"kdenlive", "xiaoyi_sim"}  # noqa: E731
    used, subs, missing = A.resolve(["wechat", "wps", "jianying", "xiaoyi", "zwcad"], installed=inst)
    assert used == ["wechat", "wps", "jianying_mobile"]       # desktop wins over the installed mobile build
    assert subs == [("jianying", "jianying_mobile")]
    assert missing == ["xiaoyi", "zwcad"]                    # xiaoyi_sim is a placeholder: not allowed by default
    used, subs, missing = A.resolve(["jianying", "xiaoyi"], installed=inst, substitute_kinds=["mobile", "placeholder"])
    assert used == ["jianying_mobile", "xiaoyi_sim"] and ("xiaoyi", "xiaoyi_sim") in subs and missing == []
    inst_no_apk = lambda a: a.id in {"kdenlive"}  # noqa: E731
    assert A.resolve(["jianying"], installed=inst_no_apk)[2] == ["jianying"]
    used, subs, _ = A.resolve(["jianying"], installed=inst_no_apk, substitute_kinds=["mobile", "oss"])
    assert used == ["kdenlive"] and subs == [("jianying", "kdenlive")]
    # a mobile id may be requested directly
    assert A.resolve(["wechat_mobile"], installed=inst)[0] == ["wechat_mobile"]


def test_display_names_are_chinese_and_known_to_scenarios():
    assert display_name("wechat") == "微信" and display_name("wps") == "WPS Office"
    assert display_name("xiaoyi_sim") == "小艺(占位)"
    assert display_name("com.tencent.mm") != "wechat"      # the Android catalogue still works


def test_resolve_substitutes_and_drops_missing():
    installed = {"wechat", "qq", "wps", "kdenlive", "jianying_mobile", "firefox"}
    used, subs, missing = A.resolve(["wechat", "qq", "wps", "jianying", "zwcad", "photosir", "firefox", "nope"],
                                    installed=lambda a: a.id in installed)
    assert used == ["wechat", "qq", "wps", "jianying_mobile", "firefox"]
    assert subs == [("jianying", "jianying_mobile")]
    assert missing == ["zwcad", "photosir", "nope"]   # no desktop build, no mobile entry -> dropped
    used2, subs2, _ = A.resolve(["jianying"], installed=lambda a: a.id == "jianying_mobile", allow_substitutes=False)
    assert used2 == [] and subs2 == []
    # with_overrides can add a mobile entry for an app that has none (zwcad -> zwcad_mobile)
    cat = A.with_overrides({"zwcad_mobile": {"name": "中望CAD(手机版)", "category": "compute", "kind": "mobile",
                                             "package": "com.zwsoft.zwcad", "match": r"^com\.zwsoft\.zwcad(:|$)",
                                             "wm_class": A.MOBILE_WM_CLASS, "cmd": ""},
                            "zwcad": {"substitute": "zwcad_mobile"}})
    assert cat["zwcad_mobile"].is_mobile and A.substitute_chain("zwcad", cat) == ["zwcad", "zwcad_mobile"]
    used3, subs3, _ = A.resolve(["zwcad"], cat, installed=lambda a: a.id == "zwcad_mobile")
    assert used3 == ["zwcad_mobile"] and subs3 == [("zwcad", "zwcad_mobile")]


def test_with_overrides_changes_fields_and_adds_apps():
    cat = A.with_overrides({
        "tencent_video": {"cmd": "/opt/tencent/txvideo", "notify_names": ["腾讯视频"]},
        "my_tool": {"name": "自研工具", "category": "compute", "cmd": "mytool --gui", "match": "mytool",
                    "wm_class": "^mytool$", "arch": ["any"]},
    })
    assert cat["tencent_video"].cmd == "/opt/tencent/txvideo" and cat["tencent_video"].notify_names == ("腾讯视频",)
    assert cat["tencent_video"].category == "download"               # untouched fields keep catalogue values
    assert cat["my_tool"].category == "compute" and cat["my_tool"].arch == ("any",)
    assert display_name("my_tool") == "自研工具"
    assert A.display_name_for(cat, "my_tool") == "自研工具" and A.display_name_for(cat, "zzz") == "zzz"
    assert A.CATALOG["tencent_video"].cmd != "/opt/tencent/txvideo"  # the global catalogue is not mutated


def test_scenarios_only_reference_catalogue_apps_and_cover_all_categories():
    for name, sc in A.SCENARIOS.items():
        ids = set(sc.apps)
        assert ids <= set(A.CATALOG), f"{name}: {ids - set(A.CATALOG)}"
        assert sc.hub in ids
        for flow in sc.flows:
            assert set(flow) <= ids
        assert sum(sc.apps.values()) > 0
    all_apps = set().union(*(set(sc.apps) for sc in A.SCENARIOS.values()))
    assert {A.CATALOG[a].category for a in all_apps} == {"download", "push", "compute"}
    assert abs(sum(s for _, s in A.DAY_SCHEDULE) - 1.0) < 1e-9
    assert {n for n, _ in A.DAY_SCHEDULE} <= set(A.SCENARIOS)


def test_resolve_trace_cfg_inlines_scenarios_and_remap_applies_substitutes():
    tr = A.resolve_trace_cfg({"kind": "scenario", "scenario": "office", "T": 10})
    assert isinstance(tr["scenario"], dict) and tr["scenario"]["name"] == "office"
    day = A.resolve_trace_cfg({"kind": "day", "T": 10})
    assert all(isinstance(sc, dict) for sc, _ in day["schedule"])
    assert A.resolve_trace_cfg({"kind": "zipf", "T": 5}) == {"kind": "zipf", "T": 5}
    with pytest.raises(ValueError):
        A.resolve_trace_cfg({"kind": "scenario", "scenario": "no_such"})

    design = A.scenario_dict("design")
    mapped = A.remap_scenario_apps(design, {"jianying": "kdenlive", "zwcad": "librecad", "photosir": None})
    assert "kdenlive" in mapped["apps"] and "jianying" not in mapped["apps"]
    assert "photosir" not in mapped["apps"]
    assert mapped["hub"] == "librecad"
    for flow in mapped["flows"]:
        assert len(flow) >= 2 and "photosir" not in flow and all(flow[i] != flow[i + 1] for i in range(len(flow) - 1))


def test_scenario_trace_generation_through_cf_trace():
    from cf.trace import make_trace_meta
    tr = A.resolve_trace_cfg({"kind": "scenario", "scenario": "chat", "T": 40, "seed": 1})
    apps = list(A.scenario_dict("chat")["apps"])
    trace, meta = make_trace_meta(apps, tr)
    assert len(trace) == 40 and set(trace) <= set(apps)
    assert all(trace[i] != trace[i + 1] for i in range(len(trace) - 1))


def test_catalog_table_and_describe_render():
    txt = A.catalog_table(arch="aarch64")
    assert "微信" in txt and "剪映" in txt
    assert "kdenlive" in txt and "no (no android runtime)" in txt
    txt2 = A.catalog_table(arch="aarch64", mobile=_FakeMobile(["com.lemon.lv"]))
    row = next(l for l in txt2.splitlines() if l.startswith("jianying_mobile"))
    assert "yes" in row and "com.lemon.lv" in row
    d = A.describe(["wechat", "jianying"])
    assert "微信" in d and "剪映" in d


MONITOR = """\
method call time=1700000000.123 sender=:1.88 -> destination=:1.20 serial=42 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify
   string "微信"
   uint32 0
   string ""
   string "张三"
   string "晚上吃什么?"
   array [
   ]
   array [
   ]
   int32 -1
method call time=1700000001.5 sender=:1.90 -> destination=:1.20 serial=7 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify
   string "DingTalk"
   uint32 0
   string "dingtalk"
   string "项目群: 新消息 (3)"
   string "..."
method call time=1700000002.0 sender=:1.91 -> destination=:1.20 serial=8 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=GetCapabilities
signal time=1700000002.5 sender=:1.20 -> destination=(null destination) serial=99 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=NotificationClosed
   uint32 5
   uint32 1
"""


def test_notify_parser_counts_only_notify_calls():
    evs = parse_monitor_text(MONITOR)
    assert evs == [("微信", "张三"), ("DingTalk", "项目群: 新消息 (3)")]
    before = {}
    after = {"微信": 3, "DingTalk": 1}
    d = NotifyCounter.delta(before, after)
    assert d == {"微信": 3, "DingTalk": 1}
    assert NotifyCounter.count_for(d, ["微信", "wechat"]) == 3
    assert NotifyCounter.count_for(d, ["钉钉", "dingtalk"]) == 1       # case-insensitive substring
    assert NotifyCounter.count_for(d, ["qq"]) == 0
    assert NotifyCounter.delta({"微信": 5}, {"微信": 5}) == {}
