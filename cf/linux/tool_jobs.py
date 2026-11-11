"""Real programs as the compute apps of the three-class experiment (manual §6.9): the work is done by
the installed tool itself - 7-Zip packing and unpacking real files, Z3 solving constraint problems -
and cf.linux.bench_app only starts the same job again and again and writes down how long the tool
took (wall time and the tool's own CPU time).

  sevenzip  `7z a -mx=5 -mmt=2` of a folder of real files copied from this system (documentation,
            python sources, binaries; ~4 MB), then `7z x` of the archive (7-Zip verifies the CRCs).
            Two records per cycle: op "compress" and op "extract".
  z3        three SMT-LIB problems with known answers, one `z3 file.smt2` each: 24 queens (integer
            constraints, sat), pigeonhole 9 into 8 (propositional, unsat), factoring a 32-bit
            semiprime (bit-vectors, sat).  One record per cycle; `ok` = every answer was right.
"""
from __future__ import annotations

import ctypes
import os
import resource
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

TOOLS = {"sevenzip": ("7z", "7zz", "7za"), "z3": ("z3",)}
INSTALL = {"sevenzip": "sudo apt install p7zip-full", "z3": "sudo apt install z3"}
CORPUS_ROOTS = ("/usr/share/doc", "/usr/lib/python3/dist-packages", "/usr/bin", "/usr/share/locale", "/usr/lib")

try:
    _LIBC = ctypes.CDLL(None, use_errno=True)
except OSError:      # not Linux
    _LIBC = None


def find_tool(kind: str) -> Optional[str]:
    for name in TOOLS.get(kind, ()):
        p = shutil.which(name)
        if p:
            return p
    return None


def _child_setup() -> None:
    """Runs in the tool's process before exec: die with the app (PR_SET_PDEATHSIG)."""
    if _LIBC is not None:
        _LIBC.prctl(1, signal.SIGKILL)


# ------------------------------------------------------------------ 7-Zip
def build_corpus(dst: str, mb: float = 4, roots=CORPUS_ROOTS) -> tuple[int, int]:
    """Copy real files of this system (2 kB .. 4 MB each, in path order, about a third of the size
    from each of the first roots that have enough) into `dst` until they add up to `mb`; kept
    between runs.  Returns (bytes, files)."""
    marker = os.path.join(dst, ".cf_corpus")
    want = int(mb * 2 ** 20)
    try:
        size, n, asked = (int(x) for x in open(marker).read().split())
        if asked == want and n == len(os.listdir(dst)) - 1:
            return size, n
    except (OSError, ValueError):
        pass
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst)
    total = n = 0
    taken: set[str] = set()
    for share in (3, 1):                        # a third from each root first, then whatever is still missing
        for root in roots:
            if total >= want:
                break
            quota = min(want, total + want // share + 1)
            for d, dirs, files in os.walk(root):
                dirs.sort()
                for f in sorted(files):
                    p = os.path.join(d, f)
                    try:
                        if p in taken or os.path.islink(p) or not os.path.isfile(p):
                            continue
                        sz = os.path.getsize(p)
                        if not 2048 <= sz <= 4 * 2 ** 20 or total + sz > want + 2 ** 20:
                            continue
                        shutil.copyfile(p, os.path.join(dst, f"{n:05d}_{f[:40]}"))
                    except OSError:
                        continue
                    taken.add(p)
                    n += 1
                    total += sz
                    if total >= quota:
                        break
                if total >= quota:
                    break
    if total < want // 2:                       # a minimal system: fill up with text-like data
        from .bench_app import make_corpus
        pad = make_corpus(max(1, (want - total) >> 20))
        with open(os.path.join(dst, f"{n:05d}_filler.txt"), "wb") as fh:
            fh.write(pad)
        n += 1
        total += len(pad)
    with open(marker, "w") as fh:
        fh.write(f"{total} {n} {want}")
    return total, n


@dataclass
class Step:
    op: str
    argv: list
    expect: Optional[str] = None              # required stdout (stripped); None = exit status only
    prepare: Optional[Callable[[], None]] = None


def sevenzip_steps(tool: str, work: str, level: int = 5, threads: int = 2) -> list[Step]:
    corpus, archive, out = os.path.join(work, "corpus"), os.path.join(work, "a.7z"), os.path.join(work, "out")
    quiet = ["-bd", "-bso0", "-bsp0", "-y"]

    def rm_archive() -> None:
        try:
            os.remove(archive)
        except OSError:
            pass

    return [Step("compress", [tool, "a", *quiet, f"-mx={level}", f"-mmt={threads}", archive, corpus], prepare=rm_archive),
            Step("extract", [tool, "x", *quiet, f"-o{out}", archive], prepare=lambda: shutil.rmtree(out, ignore_errors=True))]


# ------------------------------------------------------------------ Z3
def smt_queens(n: int = 24) -> str:
    """n queens on an n x n board, one integer column per row: satisfiable for n >= 4."""
    out = ["(set-logic QF_LIA)"]
    for i in range(n):
        out += [f"(declare-const q{i} Int)", f"(assert (and (>= q{i} 0) (< q{i} {n})))"]
    out.append("(assert (distinct " + " ".join(f"q{i}" for i in range(n)) + "))")
    for i in range(n):
        for j in range(i + 1, n):
            out += [f"(assert (not (= (- q{i} q{j}) {j - i})))", f"(assert (not (= (- q{j} q{i}) {j - i})))"]
    return "\n".join(out + ["(check-sat)", ""])


def smt_pigeonhole(n: int = 8) -> str:
    """n + 1 pigeons into n holes, no two in one hole: unsatisfiable, and hard for resolution."""
    out = ["(set-logic QF_UF)"]
    for p in range(n + 1):
        out += [f"(declare-const p{p}_{h} Bool)" for h in range(n)]
        out.append("(assert (or " + " ".join(f"p{p}_{h}" for h in range(n)) + "))")
    for h in range(n):
        for a in range(n + 1):
            out += [f"(assert (or (not p{a}_{h}) (not p{b}_{h})))" for b in range(a + 1, n + 1)]
    return "\n".join(out + ["(check-sat)", ""])


def smt_factor(p: int = 65519, q: int = 65521, bits: int = 16) -> str:
    """Find 1 < x <= y < 2^bits with x * y = p * q (2 * bits wide bit-vectors): satisfiable."""
    w = 2 * bits
    return "\n".join(["(set-logic QF_BV)", f"(declare-const x (_ BitVec {w}))", f"(declare-const y (_ BitVec {w}))",
                      f"(assert (bvult x (_ bv{1 << bits} {w})))", f"(assert (bvult y (_ bv{1 << bits} {w})))",
                      f"(assert (bvugt x (_ bv1 {w})))", "(assert (bvuge y x))",
                      f"(assert (= (bvmul x y) (_ bv{p * q} {w})))", "(check-sat)", ""])


Z3_PROBLEMS = (("queens24", smt_queens, "sat"), ("pigeonhole8", smt_pigeonhole, "unsat"), ("factor32", smt_factor, "sat"))


def z3_steps(tool: str, work: str) -> list[Step]:
    steps = []
    for name, gen, answer in Z3_PROBLEMS:
        path = os.path.join(work, name + ".smt2")
        with open(path, "w") as fh:
            fh.write(gen())
        steps.append(Step(name, [tool, path], expect=answer))
    return steps


# ------------------------------------------------------------------ the worker
class ToolWorker(threading.Thread):
    """Runs the steps in a loop, each one a run of the real tool.  `per_op`: one log record per step
    ({"k": "job", "op": step.op, ...}), else one per cycle.  `cpu` is the CPU time the tool's
    processes used (user + system, all threads), so it can exceed `ms` for a multi-threaded tool."""

    def __init__(self, steps: list[Step], log, stop, per_op: bool = True, cwd: Optional[str] = None):
        super().__init__(daemon=True)
        self.steps, self.log, self.stop_ev, self.per_op, self.cwd = steps, log, stop, per_op, cwd
        self.done = 0
        self.bad = 0
        self.last_ms = 0.0
        self.last: dict[str, float] = {}
        self._proc: Optional[subprocess.Popen] = None

    def _run(self, st: Step) -> Optional[tuple[float, float, bool]]:
        """(wall ms, cpu ms, ok) of one run; None when the app is shutting down."""
        if st.prepare:
            st.prepare()
        r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        t0 = time.perf_counter()
        try:
            self._proc = subprocess.Popen(st.argv, stdout=subprocess.PIPE if st.expect is not None else subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, cwd=self.cwd,
                                          preexec_fn=_child_setup)
            out, _ = self._proc.communicate()
            rc = self._proc.returncode
        except OSError as e:
            self.log.put("err", msg=f"{st.argv[0]}: {type(e).__name__}: {e}"[:120])
            return (time.perf_counter() - t0) * 1000, 0.0, False
        finally:
            self._proc = None
        ms = (time.perf_counter() - t0) * 1000
        r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (r1.ru_utime + r1.ru_stime - r0.ru_utime - r0.ru_stime) * 1000
        if self.stop_ev.is_set():
            return None
        ok = rc == 0 and (st.expect is None or (out or b"").decode("utf-8", "replace").strip() == st.expect)
        if not ok:
            self.log.put("err", msg=f"{st.op}: exit {rc}"
                         + (f", answered {(out or b'').decode('utf-8', 'replace').strip()[:30]!r}" if st.expect else ""))
        return ms, cpu, ok

    def run(self) -> None:
        while not self.stop_ev.is_set():
            tot_ms = tot_cpu = 0.0
            all_ok = True
            for st in self.steps:
                r = self._run(st)
                if r is None:
                    return
                ms, cpu, ok = r
                self.last[st.op] = ms
                if self.per_op:
                    self.log.put("job", op=st.op, ms=round(ms, 2), cpu=round(cpu, 2), w=0, ok=ok)
                tot_ms, tot_cpu, all_ok = tot_ms + ms, tot_cpu + cpu, all_ok and ok
            if not self.per_op:
                self.log.put("job", ms=round(tot_ms, 2), cpu=round(tot_cpu, 2), w=0, ok=all_ok)
            self.done += 1
            self.bad += 0 if all_ok else 1
            self.last_ms = tot_ms
            if not all_ok:
                self.stop_ev.wait(1.0)          # a broken tool must not spin

    def kill(self) -> None:
        p = self._proc
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass
