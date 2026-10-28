"""One account, every app: guided first login and a rehearsal of the scripted user actions.

The experiment reuses running, logged-in apps (the runner never restarts them), so each app has
to be brought to a usable state once per boot: started into its cgroup, logged in, and left where
the `--work` script can act on it (a chat with an input box, a blank document, a feed).  None of
the vendors' Linux clients can register an account - registration happens once in the phone app
with the one experiment phone number - and WeChat / QQ only log in by scanning a QR code with that
phone, so the login itself stays manual.  This module makes it a checklist instead of folklore:

* `configs/accounts.json` (copied from accounts.example.json, git-ignored) holds the shared identity
  (phone number, nickname) and per-app notes; `--onboard` prints what each app needs from it.
* `LOGIN` describes every catalogue app: how it logs in on Linux, how a logged-in state is
  recognised (the login dialog is a small window; the main window is large: `main=(w, h)`), and
  how to leave it for the work script (`prep`).
* `onboard()` walks the selected apps: launch, wait for the main window (or Enter), ask for the
  prep step, then *rehearse* - one round of the work script followed by two oracle reads - so a
  mis-focused input box or an unreadable clipboard shows up here, not as `work_state_ok = None`
  at 3 a.m.  The outcome is stored in `results/onboard/state.json`; `check()` prints it and the
  runner's preflight warns when a configured app was never onboarded or was not ready.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import sys
import time
from typing import Callable, Optional

from . import x11
from .apps import LinuxApp, display_name_for

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ACCOUNTS_FILE = os.path.join(REPO_ROOT, "configs", "accounts.json")
ACCOUNTS_EXAMPLE = os.path.join(REPO_ROOT, "configs", "accounts.example.json")
STATE_FILE = os.path.join(REPO_ROOT, "results", "onboard", "state.json")
MAIN_WINDOW = (700, 480)        # anything smaller is taken for a login / QR dialog

# ------------------------------------------------------------------ per-app login knowledge
# method:  qr = scan with the phone app | phone = number + SMS code | password | account = vendor
#          account (web registration) | optional = works without logging in | none = no account
# main:    minimum (width, height) of the logged-in main window; the login dialogs of WeChat (QR,
#          ~300x400), QQ (~330x450), DingTalk (~420x600) and Baidu (~440x500) are all smaller.
LOGIN: dict[str, dict] = {
    "wechat": {"method": "qr", "main": (700, 500),
               "needs": "手机微信扫码, 再在手机上点 '确认登录' (Linux 版没有密码登录; 每次冷启动都要重新扫)",
               "prep": "点开 '文件传输助手' 会话, 鼠标在输入框里点一下 (draft 脚本往这个输入框打字, 不发送)"},
    "qq": {"method": "qr", "main": (700, 500),
           "needs": "手机 QQ 扫码确认 (或 QQ 号 + 密码; 账号用实验手机号注册)",
           "prep": "点开 '我的电脑' 或任一会话, 在输入框里点一下"},
    "dingtalk": {"method": "phone", "main": (700, 500),
                 "needs": "实验手机号 + 短信验证码 (或手机钉钉扫码); 首次会让你建 / 加入一个组织, 建一个 'cf-bench' 即可",
                 "prep": "点开一个会话 (和自己的 '我' 也行), 在输入框里点一下, 关掉新手引导"},
    "feishu": {"method": "phone", "main": (800, 500),
               "needs": "实验手机号 + 短信验证码 (或手机飞书扫码); 首次会让你创建 / 加入一个团队, 建一个 'cf-bench' 即可",
               "prep": "点开一个会话 (和自己的 '我的文档' 也行), 在输入框里点一下, 关掉新手引导"},
    "wecom": {"method": "qr", "main": (700, 500),
              "needs": "手机企业微信扫码 (或微信扫码); 账号在手机上用实验手机号注册并创建一个企业",
              "prep": "点开一个会话 (或 '文件助手'), 在输入框里点一下"},
    "wemeet": {"method": "password", "main": (700, 450),
               "needs": "手机号 + 密码 (先在手机腾讯会议里给账号设密码; Linux 版的扫码 / 验证码登录常失败)",
               "prep": "停在登录后的主界面, 不进会议 (只做常驻 / 通知负载)"},
    "tencent_docs": {"method": "qr", "main": (800, 500),
                     "needs": "微信 / QQ 扫码 (同一套账号)",
                     "prep": "新建一个在线文档, 在正文里点一下让光标在文档里 (edit 脚本往这里打字并回车)"},
    "wps": {"method": "optional", "main": (700, 500),
            "needs": "可以不登录 (关掉登录弹窗); 要同步 / 去水印才用实验手机号登录",
            "prep": "启动命令已直接打开 ~/.cache/context_freeze/blank.docx; 关掉弹窗, 在正文里点一下让光标在文档里 "
                    "(edit 脚本往这里打字并回车, 不保存; 若停在 '新建' 首页说明是手动启动的, 关掉后 cf_launch.sh wps)"},
    "baidunetdisk": {"method": "qr", "main": (700, 500),
                     "needs": "手机百度网盘扫码, 或实验手机号 + 验证码 (同一个百度账号)",
                     "prep": "挂一个大文件的下载任务 (这就是它的后台下载负载), 停在传输列表"},
    "xunlei": {"method": "phone", "main": (700, 450),
               "needs": "实验手机号 + 验证码 (或手机迅雷扫码); 迅雷 Linux 版不登录不能建任务",
               "prep": "新建一个大文件的下载任务 (如 Ubuntu ISO 的 http 链接), 停在任务列表 (browse 脚本滚动它)"},
    "iqiyi": {"method": "optional", "main": (800, 500),
              "needs": "可以不登录; 登录用手机爱奇艺扫码 / 实验手机号验证码",
              "prep": "打开一个免费剧集并开始播放 (或停在首页推荐流), 鼠标在页面上 (browse 脚本滚动)"},
    "douyin": {"method": "optional", "main": (800, 500),
               "needs": "可以不登录 (看推荐流); 登录用手机抖音扫码",
               "prep": "停在推荐流, 鼠标放在视频区域 (view 脚本用滚轮切下一条)"},
    "netease_music": {"method": "phone", "main": (800, 500),
                      "needs": "必须登录: 实验手机号 + 验证码 (或手机网易云扫码)",
                      "prep": "播放一个歌单 (让它一直放), 停在歌单页 (browse 脚本滚动歌曲列表)"},
    "shimo": {"method": "phone", "main": (800, 500),
              "needs": "实验手机号 + 验证码 (或微信扫码)",
              "prep": "新建一个文档, 在正文里点一下让光标在文档里 (edit 脚本往这里打字并回车, 云端自动保存)"},
    "bilibili": {"method": "optional", "main": (800, 500),
                 "needs": "可以不登录; 登录用手机 B 站扫码 / 实验手机号验证码",
                 "prep": "停在首页推荐流 (browse 脚本上下滚动它); 想要下载负载就开一个缓存任务"},
    "tencent_video": {"method": "optional", "main": (800, 500),
                      "needs": "可以不登录; 登录用微信 / QQ 扫码 (同一套账号)",
                      "prep": "打开一个剧集页并开始一个缓存任务, 停在该页"},
    "chrome": {"method": "none", "main": (700, 480), "needs": "不需要账号 (不要登录 Google 同步)",
               "prep": "打开一个长网页 (新闻 / 文档), 页面能向下滚动"},
    "edge": {"method": "none", "main": (700, 480), "needs": "不需要账号", "prep": "打开一个长网页"},
    "firefox": {"method": "none", "main": (700, 480), "needs": "不需要账号", "prep": "打开一个长网页"},
    "chromium": {"method": "none", "main": (700, 480), "needs": "不需要账号", "prep": "打开一个长网页"},
    "zwcad": {"method": "account", "main": (800, 500),
              "needs": "中望账号 (官网用实验手机号注册, 申请 Linux 试用授权), 启动后登录 / 激活",
              "prep": "打开或新建一张图纸, 画几条线, 鼠标停在绘图区 (view 脚本用滚轮缩放)"},
    "photosir": {"method": "optional", "main": (800, 500),
                 "needs": "可以不登录; 登录用实验手机号",
                 "prep": "打开一张图片 (任意 jpg), 鼠标停在画布上"},
    "kdenlive": {"method": "none", "main": (800, 500), "needs": "不需要账号", "prep": "新建工程, 时间线上放一段素材"},
    "librecad": {"method": "none", "main": (800, 500), "needs": "不需要账号", "prep": "新建图纸, 画几条线"},
    "gimp": {"method": "none", "main": (800, 500), "needs": "不需要账号", "prep": "打开一张图片"},
}
_MOBILE_LOGIN = {"method": "phone", "main": (0, 0),
                 "needs": "手机版: 实验手机号 + 验证码 (微信 / QQ 手机版可用已登录的手机扫码); 首次启动的权限弹窗全部允许",
                 "prep": "停在首页 / 列表页 (swipe / view 脚本滑动它); 聊天类点开一个会话并点一下输入框"}
_GENERIC_LOGIN = {"method": "optional", "main": MAIN_WINDOW, "needs": "按应用自己的提示登录 (尽量用实验手机号)",
                  "prep": "停在一个有内容可滚动 / 可输入的页面"}


def login_spec(a: LinuxApp, overrides: Optional[dict] = None) -> dict:
    """Login method / main-window size / prep hint for an app (config `onboard_overrides` win)."""
    if a.kind == "placeholder":
        spec = {"method": "none", "main": (0, 0), "needs": "占位程序, 无账号", "prep": "无需准备"}
    elif a.is_mobile:
        spec = dict(_MOBILE_LOGIN)
    else:
        base = a.id[:-len("_mobile")] if a.id.endswith("_mobile") else a.id
        spec = dict(LOGIN.get(base) or _GENERIC_LOGIN)
    spec.update((overrides or {}).get(a.id) or {})
    spec["main"] = tuple(spec.get("main") or (0, 0))
    return spec


# ------------------------------------------------------------------ accounts file
def load_accounts(path: str = ACCOUNTS_FILE) -> dict:
    """The shared identity; an empty dict when the file is missing (the prompts then say so)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def account_line(acc: dict, app_id: str) -> str:
    """One line telling the operator which identity to use for this app."""
    if not acc:
        return f"(没有 {os.path.relpath(ACCOUNTS_FILE, REPO_ROOT)}: cp configs/accounts.example.json configs/accounts.json 并填上实验手机号)"
    per = (acc.get("apps") or {}).get(app_id) or {}
    bits = []
    if per.get("account"):
        bits.append(f"账号 {per['account']}")
    if acc.get("phone"):
        bits.append(f"手机号 {acc['phone']}")
    if acc.get("nickname"):
        bits.append(f"昵称 {acc['nickname']}")
    if per.get("note"):
        bits.append(per["note"])
    return " / ".join(bits) if bits else "(accounts.json 里没填手机号)"


# ------------------------------------------------------------------ state file
def load_state(path: str = STATE_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: str = STATE_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def not_ready(apps: list[str], state: Optional[dict] = None) -> tuple[list[str], list[str]]:
    """(never onboarded, onboarded but not ready) among `apps` - for the runner's preflight."""
    st = load_state() if state is None else state
    never = [a for a in apps if a not in st]
    bad = [a for a in apps if a in st and not st[a].get("ready")]
    return never, bad


# ------------------------------------------------------------------ detection
def main_window(dev, app_id: str, spec: dict, infos: Optional[list[dict]] = None,
                geom: Optional[Callable[[int], Optional[tuple[int, int]]]] = None) -> Optional[dict]:
    """The app's logged-in main window: a normal window at least `main` big.  Mobile builds:
    the shared Android display when the app's package is in the foreground."""
    a = dev.cat[app_id]
    if a.is_mobile:
        mb = dev.mobile
        if mb is None or not mb.available or (a.package and mb.foreground_pkg() != a.package):
            return None
        return dev.window(app_id, infos)
    min_w, min_h = spec["main"]
    best = None
    for w in candidate_windows(dev, app_id, infos, geom):
        g = w["geometry"]
        if g[0] >= min_w and g[1] >= min_h:
            if best is None or g[0] * g[1] > best["geometry"][0] * best["geometry"][1]:
                best = w
    return best


def candidate_windows(dev, app_id: str, infos: Optional[list[dict]] = None,
                      geom: Optional[Callable[[int], Optional[tuple[int, int]]]] = None) -> list[dict]:
    """Every normal-type window of a desktop app that still exists, with its geometry, largest
    first - `main_window` picks from these; `check` shows them when none is big enough."""
    a = dev.cat[app_id]
    geom = geom or x11.geometry
    hits = x11.windows_for(dev.pids(app_id), a.wm_class, a.name_rx or None, infos)
    out = []
    for w in hits:
        if w.get("type", "") not in ("", "_NET_WM_WINDOW_TYPE_NORMAL"):
            continue
        g = geom(w["wid"])
        if g:
            out.append(dict(w, geometry=list(g)))
    return sorted(out, key=lambda w: -(w["geometry"][0] * w["geometry"][1]))


def seen_windows(dev, app_id: str, spec: dict) -> str:
    """Short diagnosis for the `check` table when no main window qualifies: what *was* found."""
    a = dev.cat[app_id]
    if a.is_mobile:
        return "-"
    pids = dev.pids(app_id)
    all_hits = x11.windows_for(pids, a.wm_class, a.name_rx or None)
    if not all_hits:
        return "no window matched" if pids else "-"
    cands = candidate_windows(dev, app_id, all_hits)
    if not cands:
        return f"{len(all_hits)} non-normal ({(all_hits[0].get('type') or '?').replace('_NET_WM_WINDOW_TYPE_', '').lower()})"
    g = cands[0]["geometry"]
    return f"largest {g[0]}x{g[1]} < {spec['main'][0]}x{spec['main'][1]}"


# ------------------------------------------------------------------ interaction
def _tty() -> bool:
    return sys.stdin.isatty()


def wait_for(cond: Callable[[], Optional[dict]], timeout: float, prompt: str, interactive: bool,
             poll: float = 1.0, out=print) -> tuple[Optional[dict], str]:
    """Poll `cond` until it returns something, Enter is pressed ('confirmed'), 's' is typed
    ('skipped') or `timeout` passes.  Returns (cond result, how)."""
    hit = cond()
    if hit:
        return hit, "detected"
    if interactive:
        out(f"    {prompt}  [回车 = 已完成, s = 跳过, 自动检测中...]")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if interactive:
            r, _, _ = select.select([sys.stdin], [], [], poll)
            if r:
                line = sys.stdin.readline().strip().lower()
                if line in ("s", "skip", "n"):
                    return None, "skipped"
                return cond(), "confirmed"
        else:
            time.sleep(poll)
        hit = cond()
        if hit:
            return hit, "detected"
    return None, "timeout"


def ask(prompt: str, interactive: bool, out=print) -> bool:
    """Enter = done, s = skip.  Non-interactive runs answer 'done' without waiting."""
    if not interactive:
        return True
    out(f"    {prompt}  [回车 = 已完成, s = 跳过]")
    line = sys.stdin.readline().strip().lower()
    return line not in ("s", "skip", "n")


# ------------------------------------------------------------------ rehearsal
def rehearse(work, dev, app_id: str, wid: Optional[int], seconds: float, pause: float = 1.0) -> dict:
    """One round of the app's work script, then two oracle reads `pause` apart.  ready = the
    script ran (or is `none`), the oracle produced a value and the two reads agree."""
    a = dev.cat[app_id]
    spec = work.spec(a)
    rec: dict = {"kind": spec.get("kind", "none"), "oracle": spec.get("oracle", "none"), "ready": False}
    if not a.is_mobile and wid:
        x11.activate(wid, timeout=10)
        time.sleep(0.3)
    w = work.work(app_id, wid, seconds, 0)
    rec["work_status"] = w.get("status")
    o1 = work.oracle(app_id, wid)
    time.sleep(pause)
    o2 = work.oracle(app_id, wid)
    rec["oracle_value"] = (o1 or {}).get("value")
    rec["oracle_error"] = (o1 or {}).get("error") or (o2 or {}).get("error")
    same = work.compare(o1, o2, app_id)
    rec["stable"] = same
    worked = rec["work_status"] in ("ok", "skipped")
    if rec["oracle"] == "none":
        rec["ready"] = worked
    else:
        rec["ready"] = bool(worked and same)
    return rec


# ------------------------------------------------------------------ the walk
def onboard(dev, apps: list[str], work=None, cfg: Optional[dict] = None, interactive: Optional[bool] = None,
            timeout: float = 600.0, work_s: float = 3.0, out=print, state_path: str = STATE_FILE) -> dict:
    """Launch, log in, prepare and rehearse every app in `apps`; returns the updated state."""
    cfg = cfg or {}
    interactive = _tty() if interactive is None else interactive
    acc = load_accounts()
    overrides = cfg.get("onboard_overrides") or {}
    state = load_state(state_path)
    if acc:
        out(f"== 统一账号: 手机号 {acc.get('phone') or '?'}  昵称 {acc.get('nickname') or '?'}  "
            f"({os.path.relpath(ACCOUNTS_FILE, REPO_ROOT)})")
    else:
        out(f"== 没有 configs/accounts.json - 先 cp configs/accounts.example.json configs/accounts.json 填上实验手机号 "
            f"(本轮仍可继续, 提示里不会带账号)")
    if not interactive:
        out("== 非交互模式: 不等待回车, 只按窗口自动判断登录状态")
    for app_id in apps:
        a = dev.cat[app_id]
        name = display_name_for(dev.cat, app_id)
        spec = login_spec(a, overrides)
        rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "method": spec["method"], "ready": False,
               "logged_in": None, "login_how": None, "prep": None, "rehearsal": None, "launch": None}
        out(f"\n-- {name} ({app_id})  登录方式: {spec['method']}")
        out(f"    账号: {account_line(acc, app_id)}")
        out(f"    需要: {spec['needs']}")
        if not dev.available(app_id):
            rec["launch"] = "not-installed"
            out("    !! 未安装 - 跳过 (手机版 / 替代由 substitute_kinds 决定)")
            state[app_id] = rec
            save_state(state, state_path)
            continue
        try:
            r = dev.launch(app_id)
        except Exception as e:   # noqa: BLE001 - e.g. no writable cgroup tree; keep walking
            rec["launch"] = f"error: {type(e).__name__}: {e}"[:160]
            out(f"    !! 启动失败: {rec['launch']}")
            state[app_id] = rec
            save_state(state, state_path)
            continue
        rec["launch"] = r.get("launch_state")
        out(f"    启动: {r.get('launch_state')} status={r.get('status')} wid={r.get('wid')}")
        if r.get("status") not in ("ok", "not-active") and not r.get("wid"):
            out(f"    !! 没有窗口 ({r.get('status')}); 看 results/app_logs/{app_id}.log, 或 --discover 核对 cmd / wm_class")
            state[app_id] = rec
            save_state(state, state_path)
            continue
        win, how = wait_for(lambda: main_window(dev, app_id, spec), timeout,
                            f"请在 {name} 窗口里登录 ({spec['needs'].split(';')[0]})", interactive, out=out)
        rec["login_how"] = how
        rec["logged_in"] = bool(win) or how == "confirmed"
        if win:
            rec["wid"], rec["geometry"], rec["title"] = win["wid"], win.get("geometry"), win.get("name")
            out(f"    已登录: 主窗口 {win.get('geometry') or ''} '{(win.get('name') or '')[:40]}' ({how})")
        elif how == "confirmed":
            out("    按回车确认已登录 (没检测到大于登录框的主窗口; 若主窗口确实很小, 在 onboard_overrides 里改 main)")
        else:
            out(f"    !! 未登录 ({how}) - 之后可以只对它重跑: --onboard {app_id}")
            state[app_id] = rec
            save_state(state, state_path)
            continue
        # the window the launch found is usually the login / QR dialog, gone after the login:
        # when no main window was detected, re-pick whatever window the app has *now*
        wid = (win or {}).get("wid") or (dev.window(app_id) or {}).get("wid") or r.get("wid")
        rec["wid"] = rec.get("wid") or wid
        rec["prep"] = ask(f"准备: {spec['prep']}", interactive, out=out)
        if work is not None and rec["prep"]:
            out("    演练: 做一轮脚本操作, 读两次状态摘要 ...")
            try:
                rh = rehearse(work, dev, app_id, wid, work_s)
            except Exception as e:   # noqa: BLE001 - never abort the walk
                rh = {"ready": False, "oracle_error": f"{type(e).__name__}: {e}"[:120]}
            rec["rehearsal"] = rh
            rec["ready"] = bool(rh.get("ready"))
            if rh.get("ready"):
                out(f"    ok  {rh.get('kind')} -> {rh.get('oracle')} 可读且稳定  (值 {str(rh.get('oracle_value'))[:40]})")
            else:
                out(f"    !! 未就绪: work={rh.get('work_status')} oracle={rh.get('oracle')} "
                    f"value={str(rh.get('oracle_value'))[:30]} stable={rh.get('stable')} "
                    f"{('err=' + str(rh.get('oracle_error'))) if rh.get('oracle_error') else ''}")
                out(f"       常见原因: 输入框没有焦点 (再点一下) / 剪贴板为空 / 窗口被弹窗挡住; 修好后重跑 --onboard {app_id}")
        elif work is None:
            rec["ready"] = bool(rec["logged_in"]) and bool(rec["prep"])
        state[app_id] = rec
        save_state(state, state_path)
    out("")
    out(summary(apps, state))
    out(f"记录: {os.path.relpath(state_path, REPO_ROOT)}; 未就绪的单独重跑 --onboard <id>, 全部就绪后跑实验 (--work 可用)")
    return state


def summary(apps: list[str], state: Optional[dict] = None, cat: Optional[dict] = None) -> str:
    st = load_state() if state is None else state
    rows = ["app             logged_in  ready  method    rehearsal"]
    for a in apps:
        r = st.get(a)
        if r is None:
            rows.append(f"{a:15s} -          -      -         (never onboarded: run --onboard {a})")
            continue
        rh = r.get("rehearsal") or {}
        detail = (f"{rh.get('kind', '-')} -> {rh.get('oracle', '-')} work={rh.get('work_status', '-')} "
                  f"stable={rh.get('stable')}" if rh else (r.get("launch") or "-"))
        if rh and rh.get("oracle_error"):
            detail += f" err={str(rh['oracle_error'])[:40]}"
        if r.get("login_how") and r.get("login_how") != "detected":
            detail += f" login={r['login_how']}"
        rows.append(f"{a:15s} {str(r.get('logged_in')):10s} {str(r.get('ready')):6s} {str(r.get('method')):9s} {detail}"
                    f"  @{(r.get('at') or '')[:16]}")
    return "\n".join(rows)


def check(dev, apps: list[str], cfg: Optional[dict] = None, out=print) -> int:
    """Non-interactive status: live main-window detection + the recorded onboarding.  Exit code 1
    when any installed app is not ready."""
    overrides = (cfg or {}).get("onboard_overrides") or {}
    st = load_state()
    bad = 0
    out("app             installed  running  main_window                 onboarded  ready  why")
    hints = []
    for a in apps:
        ap = dev.cat[a]
        inst = dev.available(a)
        spec = login_spec(ap, overrides)
        pids = dev.pids(a)
        win = main_window(dev, a, spec) if inst and (pids or ap.is_mobile) else None
        rec = st.get(a) or {}
        ready = bool(rec.get("ready")) and (win is not None or not inst)
        why = ""
        if inst and not ready:
            bad += 1
            why = _why_not_ready(rec, pids, win, ap)
            if why.startswith("not running") or why.startswith("launch"):
                hints.append(f"results/app_logs/{a}.log")
        shown = str(win.get("geometry")) if win else (seen_windows(dev, a, spec) if inst and pids else "-")
        out(f"{a:15s} {str(inst):10s} {str(bool(pids)):8s} {shown:27s} {str(bool(rec)):10s} {str(ready):6s} {why}")
    if bad:
        out(f"\n{bad} app(s) not ready: scripts/ubuntu/cf_onboard.sh <id…>  (or run_linux.py CONFIG --onboard <ids>)"
            + (f"; crashed / not started: see {' '.join(hints)}" if hints else "")
            + "; windows not recognised: run_linux.py CONFIG --discover (then app_overrides / onboard_overrides.main)")
    return 1 if bad else 0


def _why_not_ready(rec: dict, pids: list[int], win: Optional[dict], ap: LinuxApp) -> str:
    """One phrase per failure mode, for the `check` table."""
    if not rec:
        return "never onboarded"
    if not pids and not ap.is_mobile:
        return f"not running ({rec.get('launch') or 'launch?'})"
    if rec.get("launch") == "TIMEOUT" or (rec.get("launch") or "").startswith("error"):
        return f"launch {rec.get('launch')}"
    if rec.get("logged_in") is False or rec.get("login_how") in ("timeout", "skipped"):
        return f"login {rec.get('login_how')}"
    if win is None:
        return "no main window now (main= too big? see --discover)"
    rh = rec.get("rehearsal") or {}
    if rec.get("prep") is False:
        return "prep skipped"
    if rh and rh.get("work_status") not in ("ok", "skipped"):
        return f"work {rh.get('work_status')}"
    if rh and rh.get("oracle_error"):
        return f"oracle {rh.get('oracle')}: {rh.get('oracle_error')}"
    if rh and rh.get("stable") is None:
        return f"oracle {rh.get('oracle')} read nothing"
    if rh and rh.get("stable") is False:
        return f"oracle {rh.get('oracle')} unstable"
    return "not ready"


def accounts_template_exists() -> bool:
    return os.path.exists(ACCOUNTS_EXAMPLE)


def ensure_accounts_file(out=print) -> None:
    """Create configs/accounts.json from the template when it is missing (first --onboard)."""
    if not os.path.exists(ACCOUNTS_FILE) and accounts_template_exists():
        shutil.copy(ACCOUNTS_EXAMPLE, ACCOUNTS_FILE)
        out(f"created {os.path.relpath(ACCOUNTS_FILE, REPO_ROOT)} from the template - fill in the experiment phone number")
