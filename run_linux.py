#!/usr/bin/env python3
"""Ubuntu 22.04 throttling experiments (window loses focus -> CPU / RAM / swap limits, nothing frozen).

    python3 run_linux.py configs/ubuntu_cn_desktop.json --probe        # environment + per-app availability
    python3 run_linux.py configs/ubuntu_cn_desktop.json --discover     # windows on screen: pid / WM_CLASS / cmd
    python3 run_linux.py configs/ubuntu_cn_desktop.json --catalog      # the app catalogue and what is installed
    python3 run_linux.py configs/ubuntu_cn_desktop.json --mobile-status  # Waydroid session / adb / installed APKs
    python3 run_linux.py configs/ubuntu_cn_desktop.json --launch wechat qq   # start apps into their cgroups (log in once)
    python3 run_linux.py configs/ubuntu_cn_desktop.json --scenario list
    python3 run_linux.py configs/ubuntu_cn_desktop.json --scenario office --policy static --T 40
    python3 run_linux.py configs/ubuntu_cn_desktop.json --scenario office --policy landlord --eta 0.3 --T 60
    python3 run_linux.py configs/ubuntu_cn_desktop.json --policies none static landlord --sweep-eta 0.25 0.5 --seeds 1 2
    python3 run_linux.py configs/ubuntu_cn_desktop.json --resume results/landlord_office_eta0.3_s1.jsonl
    python3 run_linux.py configs/ubuntu_cn_desktop.json --release      # lift every limit under /sys/fs/cgroup/cf

Throttle profile overrides: --cpu-mode quota|cpuset|both|none --cpu-cores 1 --cpuset last --mem-frac 0.5
--swap-max-mb 1024 --mem-hard --no-resident-cpu.  Results: <out-dir>/<name>.jsonl (+ .log); summarise with
`python3 -m cf.linux.analyze results/*.jsonl`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from cf.logging_util import Logger
from cf.policies import POLICIES
from cf.runner import Experiment, PreflightError, ResumeError, load_config
from cf.linux import apps as A
from cf.linux.cgroup import CgroupTree
from cf.linux.device import LinuxDesktop
from cf.linux.runner import make_mobile_runtime, run_from_config


def load_m_fg(path: str) -> tuple[dict, dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if '"type": "budget"' in line:
                r = json.loads(line)
                return r["m_fg_kb"], r.get("a_fg_kb", {})
    raise SystemExit(f"{path}: no budget record (run did not reach the end of warmup)")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--policy", choices=sorted(POLICIES))
    ap.add_argument("--policies", nargs="+", choices=sorted(POLICIES))
    ap.add_argument("--eta", type=float)
    ap.add_argument("--sweep-eta", type=float, nargs="+")
    ap.add_argument("--T", type=int)
    ap.add_argument("--scenario", metavar="NAME", help="office | design | download | chat | day | list")
    ap.add_argument("--scenarios", nargs="+", metavar="NAME")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--seeds", type=int, nargs="+")
    ap.add_argument("--m-fg-from", metavar="JSONL", help="reuse an earlier run's warmup m_fg (same budget denominator)")
    ap.add_argument("--cpu-mode", choices=["quota", "cpuset", "both", "none"])
    ap.add_argument("--cpu-cores", type=float, help="cores worth of cpu.max quota per throttled app (default 1)")
    ap.add_argument("--cpuset", help="cpuset for throttled apps: last | first | '2-3'")
    ap.add_argument("--mem-frac", type=float, help="memory.high = mem_frac * m_fg for shrunk apps (default 0.5)")
    ap.add_argument("--swap-max-mb", type=int, help="memory.swap.max per shrunk app")
    ap.add_argument("--mem-hard", action="store_true", help="also set memory.max (OOM-kill risk; off by default)")
    ap.add_argument("--no-resident-cpu", action="store_true", help="apps inside the budget keep full CPU (only shrunk ones limited)")
    ap.add_argument("--no-strict", action="store_true", help="warn instead of abort on preflight problems")
    ap.add_argument("--name"); ap.add_argument("--out-dir")
    ap.add_argument("--resume", nargs="?", const=True, default=False, metavar="JSONL")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--probe", action="store_true"); ap.add_argument("--discover", action="store_true")
    ap.add_argument("--catalog", action="store_true")
    ap.add_argument("--mobile-status", action="store_true", help="Waydroid / adb state and which mobile builds are installed")
    ap.add_argument("--launch", nargs="+", metavar="APP", help="start these apps into their cgroups and exit")
    ap.add_argument("--release", action="store_true", help="clear all limits under /sys/fs/cgroup/cf and exit")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    base = load_config(args.config)
    if args.out_dir:
        base["out_dir"] = args.out_dir
    if args.no_strict:
        base["strict_preflight"] = False
    thr = dict(base.get("throttle") or {})
    for key, val in (("cpu_mode", args.cpu_mode), ("cpu_cores", args.cpu_cores), ("cpuset_cpus", args.cpuset),
                     ("mem_frac", args.mem_frac), ("swap_max_mb", args.swap_max_mb)):
        if val is not None:
            thr[key] = val
    if args.mem_hard:
        thr["mem_hard"] = True
    base["throttle"] = thr
    if args.no_resident_cpu:
        base["cpu_throttle_resident"] = False
    cat = A.with_overrides(base.get("app_overrides"))
    kinds = base.get("substitute_kinds", list(A.DEFAULT_SUBSTITUTE_KINDS))

    def mobile_runtime():
        mb = make_mobile_runtime(base)
        mb.probe()
        return mb

    def resolve_with(mb):
        return A.resolve(base.get("apps", []), cat, installed=lambda a: A.is_installed(a, mobile=mb),
                         substitute_kinds=kinds)

    if args.scenario == "list" or (args.scenarios and "list" in args.scenarios):
        used, subs, missing = resolve_with(mobile_runtime())
        for w, g in subs:
            print(f"substitute: {w} -> {g} ({cat[g].kind})")
        for m in missing:
            print(f"missing: {m}  (chain {' -> '.join(A.substitute_chain(m, cat))}; substitute_kinds={kinds})")
        print(A.describe(used, cat))
        return 0
    if args.catalog:
        mb = mobile_runtime()
        print(mb.describe())
        print(A.catalog_table(cat, mobile=mb))
        print(f"\nsubstitute_kinds = {kinds}  (desktop build first; '-> x' = next fallback in the chain)")
        return 0
    if args.mobile_status:
        mb = mobile_runtime()
        print(json.dumps(mb.caps, indent=1, ensure_ascii=False))
        print(mb.describe())
        if mb.available:
            for a in cat.values():
                if a.is_mobile:
                    pids = mb.host_pids(a.package)
                    print(f"{a.id:22s} {a.package:28s} installed={str(mb.installed(a.package)):5s} "
                          f"host_pids={pids[:6]}")
            print(f"foreground: {mb.foreground_pkg()}")
        return 0
    if args.discover:
        from cf.linux import x11
        print(f"session {x11.session_type() or '?'}  DISPLAY {os.environ.get('DISPLAY', '-')}  wm {x11.wm_name() or '?'}")
        print(x11.discover_table(cat, CgroupTree().leaf_of))
        return 0
    if args.release:
        n = LinuxDesktop(cat=cat).release_all()
        print(f"cleared limits on {n} cgroup(s) under {CgroupTree().base}")
        return 0
    if args.probe:
        dev = LinuxDesktop(cat=cat, notifications=False, mobile=make_mobile_runtime(base))
        caps = dev.probe()
        apps_info = caps.pop("apps")
        print(json.dumps(caps, indent=1, ensure_ascii=False))
        used, subs, missing = resolve_with(dev.mobile)
        print(f"\nrunnable: {used}\nsubstituted: {subs}\nmissing: {missing}")
        for id_ in base.get("apps", []):
            i = apps_info.get(id_, {})
            line = f"{id_:15s} installed={str(i.get('installed')):5s} cmd={i.get('cmd')!r}"
            for alt in A.substitute_chain(id_, cat)[1:]:
                j = apps_info.get(alt, {})
                line += f"\n{'':15s} -> {alt} ({j.get('kind')}) installed={j.get('installed')}"
            print(line)
        dev.close()
        return 0
    if args.launch:
        dev = LinuxDesktop(cat=cat, notifications=False, mobile=make_mobile_runtime(base))
        dev.probe()
        if not dev.tree.inside():
            print("!! this shell is not inside /cf (run `sudo scripts/ubuntu/setup_cgroups.sh` first); "
                  "apps would start outside their cgroups", file=sys.stderr)
        for id_ in args.launch:
            if id_ not in cat:
                print(f"{id_}: unknown app"); continue
            r = dev.launch(id_)
            print(f"{id_:15s} {r.get('launch_state')} status={r.get('status')} pids={dev.pids(id_)} "
                  f"wid={r.get('wid')} ping={r.get('ping_ms')}")
        dev.close()
        return 0
    if args.m_fg_from:
        base["fixed_m_fg_kb"], base["fixed_a_fg_kb"] = load_m_fg(args.m_fg_from)
        print(f"budget denominator fixed from {args.m_fg_from}: "
              f"sum m_fg = {sum(base['fixed_m_fg_kb'].values()) // 1024} MB over {len(base['fixed_m_fg_kb'])} apps")

    if isinstance(args.resume, str):
        path = args.resume if args.resume.endswith(".jsonl") else args.resume + ".jsonl"
        base["out_dir"] = os.path.dirname(path) or "."
        base["name"] = os.path.basename(path)[:-len(".jsonl")]
        ck = Experiment.load_checkpoint(path)
        if ck:
            base["policy"], base["eta"] = ck["cfg"]["policy"], ck["cfg"]["eta"]
        base.setdefault("policy", "?"); base.setdefault("eta", "?")
        jobs = [(base["name"], base)]
    else:
        policies = args.policies or [args.policy or base.get("policy", "landlord")]
        etas = args.sweep_eta or [args.eta if args.eta is not None else base.get("eta", 0.3)]
        seeds = args.seeds or [args.seed]
        scenarios = args.scenarios or [args.scenario]
        jobs = []
        for pol in policies:
            for eta in etas:
                for sc in scenarios:
                    for seed in seeds:
                        cfg = json.loads(json.dumps(base))
                        cfg["policy"], cfg["eta"] = pol, eta
                        if args.T:
                            cfg.setdefault("trace", {})["T"] = args.T
                        if seed is not None:
                            cfg.setdefault("trace", {})["seed"] = seed
                        if sc:
                            tr = cfg.setdefault("trace", {})
                            tr["kind"] = "day" if sc == "day" else "scenario"
                            if sc != "day":
                                tr["scenario"] = sc
                        single = len(policies) * len(etas) * len(seeds) * len(scenarios) == 1
                        tag = f"_{sc}" if sc else ""
                        if args.name and single:
                            cfg["name"] = args.name
                        elif args.name:
                            cfg["name"] = args.name + (tag if args.scenarios else "") + (f"_s{seed}" if args.seeds else "")
                        else:
                            cfg["name"] = f"{pol}{tag}_eta{eta}" + (f"_s{seed}" if args.seeds else "") + \
                                f"_{time.strftime('%Y%m%d-%H%M%S')}"
                        jobs.append((cfg["name"], cfg))

    outputs, failed = [], []
    for i, (name, cfg) in enumerate(jobs):
        path = os.path.join(cfg["out_dir"], name + ".jsonl")
        st = Experiment.jsonl_status(path)
        resume = bool(args.resume)
        log = Logger()
        log.head(f"===== [{i + 1}/{len(jobs)}] policy={cfg['policy']} eta={cfg['eta']} -> {path} =====")
        if st["exists"] and st["finished"]:
            log.ok(f"already finished ({st['steps']} steps) - skipping (delete the file to redo)")
            outputs.append(path)
            continue
        if st["exists"] and not resume and not args.overwrite:
            if Experiment.load_checkpoint(path) is not None:
                log.warn(f"unfinished run found ({st['steps']}/{st['T']} steps) - resuming it. --overwrite restarts.")
                resume = True
            else:
                log.warn("result file exists without a checkpoint - overwriting")
        if resume and Experiment.load_checkpoint(path) is None:
            if st["exists"]:
                log.err(f"{path} has no .ckpt - cannot resume")
                failed.append(path)
                continue
            log.warn("nothing to resume yet - starting a fresh run")
            resume = False
        try:
            outputs.append(run_from_config(cfg, verbose=args.verbose, resume=resume, log=log))
        except KeyboardInterrupt:
            print(f"\nstopped. Continue with:\n  python3 run_linux.py {args.config} --resume {path}")
            return 130
        except ResumeError as e:
            log.err(str(e)); failed.append(path)
        except PreflightError as e:
            log.err(f"{e} - stopping the sweep")
            return 2
        except Exception as e:      # noqa: BLE001
            log.err(f"run failed: {type(e).__name__}: {str(e)[:300]}")
            log.warn(f"continue later with: python3 run_linux.py {args.config} --resume {path}")
            failed.append(path)
            if args.verbose:
                raise
        time.sleep(2)

    print("\nresults:")
    for o in outputs:
        print("  ", o)
    for f in failed:
        print("   FAILED/INCOMPLETE", f)
    if outputs:
        print("summarise with: python3 -m cf.linux.analyze " + " ".join(outputs))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
