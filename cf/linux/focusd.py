"""focusd: apply the fg / bg / shrunk tiers on a *live* desktop whenever the focused window changes.

    source scripts/ubuntu/cf_enter.sh                     # shell under /cf (delegated cgroup tree)
    scripts/ubuntu/cf_launch.sh wechat qq wps             # apps start inside cf/<app>
    python3 -m cf.linux.focusd --config configs/ubuntu_cn_desktop.json          # static: only the
                                                                                 # focused app is free
    python3 -m cf.linux.focusd --config ... --policy lru --eta 0.3 --delay 3     # budgeted residents

The experiment runner (cf.linux.runner) drives the switches itself; focusd is the same mechanism
for everyday use and demos: you click around, it throttles whatever is not in front.

How it works
  * the root window's _NET_ACTIVE_WINDOW property (python-xlib PropertyNotify; polling fallback)
    tells us the focused window; its _NET_WM_PID -> /proc/<pid>/cgroup -> cf/<app> leaf
  * the focused leaf (and `never_throttle`) go to tier fg
  * every other populated leaf under cf/ goes to shrunk (policy static) or, with --eta, to the
    tier chosen by a budgeted policy from cf.policies (resident -> bg, evicted -> shrunk)
  * --delay N waits N seconds after a switch before limiting the previous app (quick alt-tab
    round trips cost nothing); a window that becomes focused again cancels its pending limit
  * no process is ever frozen or killed; Ctrl+C / SIGTERM lifts every limit (clear_limits)

Windows whose process is *not* below cf/ (terminal, file manager, the browser you started from the
dock) are ignored: they are neither limited nor counted.  Use cf_launch.sh or cf_adopt.sh.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from typing import Optional

from ..logging_util import as_logger
from ..policies import AppInfo, make_policy
from . import apps as A
from . import x11
from .cgroup import CgroupTree, ThrottleProfile, HOST_LEAF, apply_tier


class FocusWatcher:
    """Yields the active window id whenever it changes (X PropertyNotify, polling fallback)."""

    def __init__(self, poll_s: float = 0.5):
        self.poll_s = poll_s
        self.d = None
        self.atom = None
        try:
            from Xlib import X, display   # noqa: F401
            self.d = display.Display()
            self.root = self.d.screen().root
            self.atom = self.d.intern_atom("_NET_ACTIVE_WINDOW")
            self.root.change_attributes(event_mask=X.PropertyChangeMask)
            self.d.flush()
        except Exception:        # noqa: BLE001  (no python-xlib / no display -> poll)
            self.d = None

    def active(self) -> Optional[int]:
        if self.d is not None:
            try:
                prop = self.root.get_full_property(self.atom, 0)
                if prop and prop.value:
                    wid = int(prop.value[0])
                    return wid or None
            except Exception:    # noqa: BLE001
                pass
        return x11.active_window()

    def wait(self, timeout: float) -> bool:
        """Block up to `timeout` s; True when a focus-change event arrived (or polling tick)."""
        if self.d is None:
            time.sleep(timeout)
            return True
        import select
        from Xlib import X
        r, _, _ = select.select([self.d.fileno()], [], [], timeout)
        changed = False
        while self.d.pending_events():
            e = self.d.next_event()
            if e.type == X.PropertyNotify and e.atom == self.atom:
                changed = True
        return changed or not r


class FocusDaemon:
    def __init__(self, cfg: dict, policy: str = "static", eta: Optional[float] = None, delay_s: float = 0.0,
                 resident_tier: str = "bg", dry_run: bool = False, log=None, jsonl: Optional[str] = None,
                 tree: Optional[CgroupTree] = None):
        self.cfg = cfg
        self.log = as_logger(log)
        self.tree = tree or CgroupTree()
        self.profile = ThrottleProfile.from_cfg(cfg.get("throttle"))
        self.cat = A.with_overrides(cfg.get("app_overrides") or {}, cfg.get("placeholder_apps"))
        self.never = set(cfg.get("never_throttle") or [])
        self.delay_s = delay_s
        self.resident_tier = resident_tier
        self.dry_run = dry_run
        self.eta = eta
        self.policy_name = policy if eta is not None else "static"
        self.pol = None
        self.tiers: dict[str, str] = {}
        self.m_fg: dict[str, int] = {}            # peak memory.current while focused (bytes)
        self.pending: dict[str, float] = {}       # leaf -> time when its limit becomes due
        self.focused: Optional[str] = None
        self.t = 0
        self.current: set[str] = set()
        self.jsonl = open(jsonl, "a") if jsonl else None

    # ------------------------------------------------------------------ cgroup side
    def leaves(self) -> list[str]:
        return [l for l in self.tree.leaves() if l != HOST_LEAF and self.tree.populated(l)]

    def leaf_for_window(self, wid: Optional[int]) -> Optional[str]:
        if not wid:
            return None
        info = x11.window_info(wid)
        pid = info.get("pid")
        if pid:
            leaf = self.tree.leaf_of(pid)
            if leaf and leaf != HOST_LEAF:
                return leaf
        # apps without _NET_WM_PID: match WM_CLASS / title against the catalogue
        for leaf in self.leaves():
            a = self.cat.get(leaf)
            if a and x11.windows_for([], a.wm_class, a.name_rx or None, [info]):
                return leaf
        return None

    def _infos(self) -> dict[str, AppInfo]:
        infos = {}
        for leaf in self.leaves():
            cur = self.tree.stats(leaf).get("memory_current") or 1
            m_fg = self.m_fg.get(leaf) or cur
            high = self.profile.mem_high_bytes(m_fg)
            infos[leaf] = AppInfo(leaf, float(cur), float(cur), min(1.0, high / cur), 60.0, 0.0)
        return infos

    def _set(self, leaf: str, tier: str) -> dict:
        if self.tiers.get(leaf, "fg") == tier:
            return {}
        self.tiers[leaf] = tier
        if self.dry_run:
            return {"tier": tier, "dry_run": True}
        # never focused since we started: take its current footprint as the foreground size
        m_fg = self.m_fg.get(leaf) or self.tree.stats(leaf).get("memory_current") or None
        return apply_tier(self.tree, leaf, tier, self.profile, m_fg)

    def _tier_for(self, leaf: str, resident: bool) -> str:
        if leaf in self.never:
            return "fg"
        return self.resident_tier if resident else "shrunk"

    # ------------------------------------------------------------------ decisions
    def on_focus(self, leaf: Optional[str]) -> None:
        prev = self.focused
        self.focused = leaf
        if leaf:
            self.pending.pop(leaf, None)
            cur = self.tree.stats(leaf).get("memory_current") or 0
            self.m_fg[leaf] = max(self.m_fg.get(leaf, 0), cur)
            r = self._set(leaf, "fg")
            self.t += 1
            if self.pol is not None:
                self.pol.on_request(self.t, leaf)
            self._emit("focus", leaf=leaf, prev=prev, applied=r)
        if prev and prev != leaf and prev not in self.pending:
            self.pending[prev] = time.time() + self.delay_s
        # apps limited by the policy of the previous focus may need re-evaluation now
        for other in self.leaves():
            if other != leaf and other not in self.pending and self.tiers.get(other, "fg") == "fg":
                self.pending[other] = time.time() + self.delay_s

    def flush_pending(self) -> None:
        now = time.time()
        due = [l for l, when in self.pending.items() if when <= now]
        if not due:
            return
        for l in due:
            self.pending.pop(l, None)
        leaves = self.leaves()
        if self.pol is None and self.eta is not None:
            self.pol = make_policy(self.policy_name, leaves)
        if self.pol is not None and self.focused:
            infos = self._infos()
            B = self.eta * sum(self.m_fg.get(l) or infos[l].m for l in leaves)
            S = set(self.pol.choose_resident(self.t, self.focused, infos, B, self.current)) | {self.focused}
            self.current = S
        else:
            S = {self.focused} if self.focused else set()
        applied = {}
        for leaf in leaves:
            if leaf == self.focused:
                continue
            tier = self._tier_for(leaf, leaf in S)
            r = self._set(leaf, tier)
            if r:
                applied[leaf] = {"tier": tier, "reclaim": r.get("reclaim")}
        if applied:
            self._emit("limit", focused=self.focused, applied=applied,
                       mem_mb={l: round((self.tree.stats(l).get("memory_current") or 0) / 2 ** 20) for l in leaves})

    def release_all(self) -> int:
        n = 0
        for leaf in self.tree.leaves():
            if leaf == HOST_LEAF:
                continue
            if not self.dry_run:
                self.tree.clear_limits(leaf)
            self.tiers[leaf] = "fg"
            n += 1
        return n

    # ------------------------------------------------------------------ logging
    def _emit(self, kind: str, **fields) -> None:
        rec = {"ts": round(time.time(), 3), "type": kind, **fields}
        if self.jsonl:
            self.jsonl.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.jsonl.flush()
        if kind == "focus":
            self.log.info(f"focus -> {A.display_name_for(self.cat, fields['leaf'])}"
                          + (f"  (from {A.display_name_for(self.cat, fields['prev'])})" if fields.get("prev") else ""))
        elif kind == "limit":
            parts = [f"{A.display_name_for(self.cat, l)}={v['tier']}" for l, v in fields["applied"].items()]
            self.log.step(f"limit  {', '.join(parts)}  | mem MB {fields['mem_mb']}" + (" [dry-run]" if self.dry_run else ""))

    # ------------------------------------------------------------------ main loop
    def run(self, poll_s: float = 0.5) -> int:
        watcher = FocusWatcher(poll_s)
        self.log.head(f"focusd: policy={self.policy_name}" + (f" eta={self.eta}" if self.eta is not None else "")
                      + f" delay={self.delay_s}s resident->{self.resident_tier} | {self.profile.describe()}"
                      + (" | DRY RUN" if self.dry_run else ""))
        leaves = self.leaves()
        self.log.info(f"apps under {self.tree.path()}: {', '.join(A.display_name_for(self.cat, l) for l in leaves) or '(none yet)'}")
        self.release_all()           # start from a clean slate (limits left by a crashed run)
        stop = {"flag": False}

        def _sig(*_):
            stop["flag"] = True
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
        last_wid = None
        try:
            while not stop["flag"]:
                wid = watcher.active()
                if wid != last_wid:
                    last_wid = wid
                    leaf = self.leaf_for_window(wid)
                    if leaf != self.focused:
                        self.on_focus(leaf)
                self.flush_pending()
                watcher.wait(min(poll_s, 0.5))
        finally:
            n = self.release_all()
            self._emit("release", leaves=n)
            self.log.ok(f"focusd stopped: limits lifted on {n} cgroup(s)")
            if self.jsonl:
                self.jsonl.close()
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="configs/ubuntu_cn_desktop.json",
                    help="experiment config (throttle profile, app_overrides, never_throttle)")
    ap.add_argument("--policy", default="static", help="static (default) or any cf.policies name with --eta")
    ap.add_argument("--eta", type=float, default=None, help="background budget fraction of sum(m_fg); enables the policy")
    ap.add_argument("--delay", type=float, default=2.0, help="grace period before limiting an unfocused app (s)")
    ap.add_argument("--resident-tier", choices=("fg", "bg"), default="bg",
                    help="tier of policy residents: bg = CPU-limited only, fg = untouched")
    ap.add_argument("--poll", type=float, default=0.5)
    ap.add_argument("--log", default=None, help="JSONL event log")
    ap.add_argument("--dry-run", action="store_true", help="log decisions, do not touch cgroups")
    args = ap.parse_args(argv)

    with open(args.config) as f:
        cfg = json.load(f)
    tree = CgroupTree()
    if not tree.available():
        print(f"cgroup tree {tree.path()} not found: sudo scripts/ubuntu/setup_cgroups.sh", file=sys.stderr)
        return 2
    if not tree.writable() and not args.dry_run:
        print(f"{tree.path()} is not writable by uid {os.getuid()}: sudo scripts/ubuntu/setup_cgroups.sh", file=sys.stderr)
        return 2
    if not x11.have_display():
        print("DISPLAY is not set: focusd needs an X11 session", file=sys.stderr)
        return 2
    if x11.tools_missing():
        print(f"missing tools: {x11.tools_missing()} (apt install xdotool x11-utils)", file=sys.stderr)
        return 2
    d = FocusDaemon(cfg, policy=args.policy, eta=args.eta, delay_s=args.delay, resident_tier=args.resident_tier,
                    dry_run=args.dry_run, jsonl=args.log, tree=tree)
    return d.run(args.poll)


if __name__ == "__main__":
    sys.exit(main())
