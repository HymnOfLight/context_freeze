"""Console + file logger for the on-device runner.

Every line goes to stdout (flushed immediately, so `| tee` and Ctrl+C never lose output)
and, once the result path is known, to `<result>.log` next to the JSONL file.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Callable, Optional

_COLORS = {"INFO": "", "OK": "\033[32m", "WARN": "\033[33m", "ERR": "\033[31m",
           "STEP": "\033[36m", "HEAD": "\033[1m"}
_RESET = "\033[0m"

SHORT_PREFIXES = ("com.google.android.apps.", "com.google.android.", "com.android.", "org.", "com.")


def short_pkg(pkg: str, width: int = 0) -> str:
    """'com.google.android.apps.messaging' -> 'messaging' (for progress lines only)."""
    for pre in SHORT_PREFIXES:
        if pkg.startswith(pre) and len(pkg) > len(pre):
            pkg = pkg[len(pre):]
            break
    return pkg.ljust(width) if width else pkg


def fmt_dur(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "--"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


class Logger:
    """Callable like print(): log("msg") == log.info("msg"). Extra methods: warn/ok/err/head/step."""

    def __init__(self, path: Optional[str] = None, color: Optional[bool] = None,
                 sink: Optional[Callable[[str], None]] = None):
        self.fh = None
        self.sink = sink                 # replaces stdout (tests pass a no-op)
        self.color = sys.stdout.isatty() if color is None else color
        self.warnings: list[str] = []
        self.t0 = time.time()
        if path:
            self.attach(path)

    def attach(self, path: str, append: bool = False) -> None:
        if self.fh:
            self.fh.close()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.fh = open(path, "a" if append else "w", encoding="utf-8")
        if append:
            self.fh.write(f"\n# ---- log reopened {time.strftime('%Y-%m-%d %H:%M:%S')} ----\n")
            self.fh.flush()

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None

    def __call__(self, *parts, level: str = "INFO") -> None:
        msg = " ".join(str(p) for p in parts)
        stamp = time.strftime("%H:%M:%S")
        tag = f"{level:<4s}"
        line = f"{stamp} {tag} {msg}"
        if self.sink is not None:
            self.sink(line)
        else:
            if self.color and _COLORS.get(level):
                out = f"{stamp} {_COLORS[level]}{tag}{_RESET} {msg}"
            else:
                out = line
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
        if self.fh:
            self.fh.write(line + "\n")
            self.fh.flush()

    def info(self, *parts) -> None:
        self(*parts, level="INFO")

    def ok(self, *parts) -> None:
        self(*parts, level="OK")

    def warn(self, *parts) -> None:
        self.warnings.append(" ".join(str(p) for p in parts))
        self(*parts, level="WARN")

    def err(self, *parts) -> None:
        self(*parts, level="ERR")

    def head(self, *parts) -> None:
        self(*parts, level="HEAD")

    def step(self, *parts) -> None:
        self(*parts, level="STEP")


def as_logger(log) -> Logger:
    """Accept a Logger, a plain callable (print-like) or None."""
    if isinstance(log, Logger):
        return log
    if log is None:
        return Logger()
    return Logger(sink=lambda line: log(line))
