"""A config with fewer apps: the same experiment without some of its apps (an app that cannot be
set up on this machine, a download class reduced to the one client that works).

    python3 -m cf.linux.subset CONFIG --download xunlei --out OUT.json     # download class = 迅雷 only
    python3 -m cf.linux.subset CONFIG --download none --out OUT.json       # no download apps at all
    python3 -m cf.linux.subset CONFIG --drop qq baidunetdisk --out OUT.json
    python3 -m cf.linux.subset CONFIG --classes download --download baidunetdisk browser_dl --out OUT.json
                                                                           # the download class alone, two of its apps

The apps go out of `apps`, of every class in `classes` and of the visiting order `trace.pattern`
(two visits to the same app that end up next to each other become one).  Everything else - limits,
dwell, per-app overrides - is unchanged.  The path written is printed (run_classes.sh: DL_APPS=...,
CLASSES=...).

With --classes the apps of the other classes go; the ones that stay take turns in front.  At least
two apps have to stay: an app is measured in the background only while another one is in front.
"""
from __future__ import annotations

import copy
import json
from typing import Optional, Sequence


def subset(cfg: dict, drop: Sequence[str] = (), download: Optional[Sequence[str]] = None,
           only_classes: Optional[Sequence[str]] = None) -> tuple[dict, list[str]]:
    """(the reduced config, the apps that were removed).  `download`: keep only these apps of
    classes.download ([] = none of them).  `only_classes`: keep only the apps of these classes.
    ValueError for a name the config does not have, and when fewer than two apps would be left."""
    out = copy.deepcopy(cfg)
    apps = list(out.get("apps") or [])
    classes = out.get("classes") or {}
    gone = []
    for a in drop:
        if a not in apps:
            raise ValueError(f"{a} is not an app of this config ({', '.join(apps)})")
        gone.append(a)
    if download is not None:
        have = list(classes.get("download") or [])
        for a in download:
            if a not in have:
                raise ValueError(f"{a} is not a download app of this config ({', '.join(have) or 'it has none'})")
        gone += [a for a in have if a not in download and a not in gone]
    if only_classes is not None:
        for c in only_classes:
            if not isinstance(classes.get(c), list):
                names = ", ".join(k for k, v in classes.items() if isinstance(v, list))
                raise ValueError(f"{c} is not a class of this config ({names or 'it has none'})")
        keep = {a for c in only_classes for a in classes[c]}
        gone += [a for a in apps if a not in keep and a not in gone]
    if not gone:
        return out, []
    out["apps"] = [a for a in apps if a not in gone]
    for k, v in classes.items():
        if isinstance(v, list):
            classes[k] = [a for a in v if a not in gone]
    tr = out.get("trace") or {}
    if isinstance(tr.get("pattern"), list):
        pat = []
        for a in tr["pattern"]:
            if a not in gone and (not pat or pat[-1] != a):
                pat.append(a)
        while len(pat) > 1 and pat[0] == pat[-1]:           # the order repeats: last and first are neighbours too
            pat.pop()
        if not pat:
            raise ValueError("nothing left in trace.pattern")
        if len(pat) < 2:
            raise ValueError(f"only {pat[0]} is left: it would be in front for the whole run and never in the "
                             "background - keep a second app")
        if "T" in tr and tr["pattern"]:
            tr["T"] = max(1, round(tr["T"] / len(tr["pattern"]))) * len(pat)
        tr["pattern"] = pat
    out["_comment_subset"] = f"derived by cf.linux.subset: without {', '.join(gone)}"
    return out, gone


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--download", nargs="+", metavar="APP", help="keep only these download apps ('none': drop them all)")
    ap.add_argument("--classes", nargs="+", metavar="CLASS",
                    help="keep only the apps of these classes (interactive / compute / download)")
    ap.add_argument("--drop", nargs="+", default=[], metavar="APP", help="apps to take out")
    ap.add_argument("--out", required=True, help="where to write the reduced config")
    args = ap.parse_args(argv)
    cfg = json.load(open(args.config))
    keep = None if args.download is None else [a for a in args.download if a != "none"]
    try:
        out, gone = subset(cfg, args.drop, keep, args.classes)
    except ValueError as e:
        print(f"!! {e}", file=sys.stderr)
        return 2
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  apps taken out of {args.config}: {', '.join(gone) or 'none'}; {len(out['apps'])} apps, "
          f"{len((out.get('trace') or {}).get('pattern') or [])} steps per round -> {args.out}", file=sys.stderr)
    print(args.out)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
