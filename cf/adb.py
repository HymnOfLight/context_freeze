"""Thin adb wrapper.

On google_apis / default (AOSP) emulator images `adb root` works and every
`adb shell` command already runs as root.  On images where `adb root` is
refused (Google Play) we fall back to `su 0 <cmd>` if a su binary exists.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from typing import Optional


class AdbError(RuntimeError):
    pass


def find_adb(explicit: Optional[str] = None) -> str:
    candidates = [explicit, os.environ.get("ADB")]
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env)
        if root:
            candidates.append(os.path.join(root, "platform-tools", "adb"))
    candidates.append(os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"))
    candidates.append(os.path.expanduser("~/Android/Sdk/platform-tools/adb"))
    candidates.append(shutil.which("adb"))
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    raise AdbError("adb not found; set $ANDROID_HOME or $ADB")


class Adb:
    def __init__(self, serial: Optional[str] = None, adb_path: Optional[str] = None,
                 verbose: bool = False):
        self.adb = find_adb(adb_path)
        self.serial = serial or os.environ.get("ANDROID_SERIAL")
        self.verbose = verbose
        self._su_prefix = ""

    def _base(self) -> list[str]:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(self, *args: str, check: bool = True, timeout: float = 120) -> str:
        cmd = self._base() + list(args)
        if self.verbose:
            print("+", " ".join(shlex.quote(c) for c in cmd))
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise AdbError(f"{' '.join(cmd)} failed ({p.returncode}): {p.stderr.strip()}")
        return p.stdout

    def shell(self, cmd: str, check: bool = False, timeout: float = 120) -> str:
        """Run a shell command (as root when available). Returns stdout+stderr."""
        full = self._su_prefix + cmd
        p = subprocess.run(self._base() + ["shell", full], capture_output=True, text=True,
                           timeout=timeout)
        if self.verbose:
            print("+ adb shell", full)
        out = p.stdout + (p.stderr if p.stderr else "")
        if check and p.returncode != 0:
            raise AdbError(f"adb shell {full!r} failed ({p.returncode}): {out.strip()}")
        return out

    def ensure_root(self) -> str:
        """Try `adb root`; else try `su 0`. Returns the mechanism used."""
        if self.shell("id -u").strip() == "0":
            return "adbd-root"
        try:
            out = self.run("root", check=False)
            if "cannot run as root" not in out:
                self.run("wait-for-device", timeout=60)
                if self.shell("id -u").strip() == "0":
                    return "adbd-root"
        except (AdbError, subprocess.TimeoutExpired):
            pass
        if self.shell("su 0 id -u").strip() == "0":
            self._su_prefix = "su 0 "
            return "su"
        raise AdbError("cannot obtain root: use a google_apis / default (non-Play) system image")

    def wait_boot(self, timeout: float = 600) -> None:
        import time
        self.run("wait-for-device", timeout=timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.shell("getprop sys.boot_completed").strip() == "1":
                return
            time.sleep(2)
        raise AdbError("device did not finish booting")

    def read_file(self, path: str) -> str:
        return self.shell(f"cat {shlex.quote(path)} 2>/dev/null")

    def write_file(self, path: str, value: str) -> bool:
        out = self.shell(f"echo {shlex.quote(value)} > {shlex.quote(path)} 2>&1 && echo __OK__")
        return "__OK__" in out

    def exists(self, path: str) -> bool:
        return "__YES__" in self.shell(f"[ -e {shlex.quote(path)} ] && echo __YES__")
