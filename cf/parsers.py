"""Pure parsers for /proc, /sys and `am` outputs (unit-testable without a device)."""
from __future__ import annotations

import re
from typing import Optional

_KV_KB = re.compile(r"^([A-Za-z_()0-9]+):\s+(\d+)(?:\s+kB)?", re.M)


def parse_kv_kb(text: str) -> dict[str, int]:
    """Parse `Key:  123 kB` style files (/proc/meminfo, smaps_rollup). Values in kB."""
    return {k: int(v) for k, v in _KV_KB.findall(text)}


def parse_vmstat(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            out[parts[0]] = int(parts[1])
    return out


def parse_psi(text: str) -> dict[str, float]:
    """/proc/pressure/memory ->
    {some_avg10, some_avg60, some_avg300, some_total, full_avg10, ...}"""
    out: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        for kv in parts[1:]:
            k, _, v = kv.partition("=")
            out[f"{parts[0]}_{k}"] = float(v)
    return out


def parse_zram_mm_stat(text: str) -> dict[str, int]:
    """/sys/block/zram0/mm_stat:
    orig_data_size compr_data_size mem_used_total mem_limit mem_used_max
    same_pages pages_compacted [huge_pages [huge_pages_since]]"""
    parts = text.split()
    keys = ["orig_data_size", "compr_data_size", "mem_used_total", "mem_limit",
            "mem_used_max", "same_pages", "pages_compacted", "huge_pages", "huge_pages_since"]
    return {k: int(v) for k, v in zip(keys, parts)}


def parse_swaps(text: str) -> list[dict]:
    """/proc/swaps -> [{filename, type, size_kb, used_kb, priority}]"""
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            rows.append({"filename": parts[0], "type": parts[1], "size_kb": int(parts[2]),
                         "used_kb": int(parts[3]), "priority": int(parts[4])})
    return rows


def parse_proc_stat_majflt(text: str) -> Optional[int]:
    """/proc/<pid>/stat: field 12 (majflt) - comm may contain spaces, split after ')'."""
    try:
        rest = text[text.rindex(")") + 2:].split()
        return int(rest[9])  # fields after comm: state(3) ... minflt(10) cminflt(11) majflt(12)
    except (ValueError, IndexError):
        return None


_AM_INT = re.compile(r"^(ThisTime|TotalTime|WaitTime):\s+(\d+)", re.M)


def parse_am_start(text: str) -> dict:
    """Parse `am start -W` output."""
    res: dict = {"status": None, "launch_state": None, "this_time_ms": None,
                 "total_time_ms": None, "wait_time_ms": None, "raw": text.strip()}
    m = re.search(r"^Status:\s+(\w+)", text, re.M)
    if m:
        res["status"] = m.group(1)
    m = re.search(r"^LaunchState:\s+(\w+)", text, re.M)
    if m:
        res["launch_state"] = m.group(1)
    for k, v in _AM_INT.findall(text):
        res[{"ThisTime": "this_time_ms", "TotalTime": "total_time_ms",
             "WaitTime": "wait_time_ms"}[k]] = int(v)
    state = res["launch_state"] or ""
    if res["status"] == "timeout" or state.startswith("UNKNOWN (-1)"):
        # `am start -W` gave up waiting for the first frame: WaitTime is a lower bound
        res["launch_state"] = "TIMEOUT"
        res["total_time_ms"] = res["total_time_ms"] or res["wait_time_ms"]
    elif "currently running top-most instance" in text or state.startswith("UNKNOWN") \
            or (res["total_time_ms"] == 0):
        # the activity was already in the foreground -> nothing resumed, no latency sample
        res["launch_state"] = "FRONT"
        res["total_time_ms"] = None
        res["this_time_ms"] = None
    # note: "Warning: Activity not started, its current task has been brought to the front"
    # accompanies a normal HOT resume and carries a valid TotalTime -> keep it
    if "Error" in text or "Exception" in text:
        res["status"] = res["status"] or "error"
    return res


def parse_resolve_activity(text: str) -> Optional[str]:
    """`cmd package resolve-activity --brief ...` -> 'pkg/cls' or None."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if "/" in line and " " not in line:
            return line
    return None


def parse_pid_list(text: str) -> list[int]:
    return [int(x) for x in text.split() if x.isdigit()]


def parse_uid_from_dumpsys(text: str) -> Optional[int]:
    m = re.search(r"userId=(\d+)", text)
    return int(m.group(1)) if m else None
