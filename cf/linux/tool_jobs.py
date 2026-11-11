"""Real programs as the compute apps of the three-class experiment (manual §6.9): the work is done by
the installed tool itself - 7-Zip packing real files, Z3 solving constraint problems, OpenSSL
encrypting, a language model answering a question ... - and cf.linux.bench_app only starts the same
job again and again and writes down how long the tool took (wall time and the tool's own CPU time).

  sevenzip   `7z a -mx=5 -mmt=2` of a folder of real files copied from this system (documentation,
             python sources, binaries; ~4 MB), then `7z x` of the archive (7-Zip verifies the CRCs).
             Two records per cycle: op "compress" and op "extract".
  z3         three SMT-LIB problems with known answers, one `z3 file.smt2` each: 24 queens (integer
             constraints, sat), pigeonhole 9 into 8 (propositional, unsat), factoring a 32-bit
             semiprime (bit-vectors, sat).  One record per cycle; `ok` = every answer was right.
  openssl    password-based file encryption and signing of a 32 MB file: op "encrypt" = `openssl enc
             -aes-256-cbc -pbkdf2 -iter 600000`, op "decrypt" = the reverse piped into `openssl dgst
             -sha256` (ok = the digest of the original), op "sign" = RSA-3072 / SHA-256 signature of
             the file and its verification.
  llama      a local large language model (llama.cpp's `llama-completion` + a GGUF model, by default
             Qwen2.5-0.5B-Instruct Q4_K_M): every job starts the program, loads the model and
             generates exactly 64 tokens answering one fixed question (greedy, one thread).
  tesseract  OCR with Tesseract's LSTM network: one page of English text rendered to an image at
             setup is read back; ok = at least 90 % of the words were recognised.  One thread
             (OMP_THREAD_LIMIT=1: Tesseract's OpenMP build gets slower, not faster, with more).
  ffmpeg     video transcoding: a 4 s 1280x720 clip made at setup is decoded, scaled to 960x540 and
             encoded with libx264 (preset medium, crf 23).

    python3 -m cf.linux.tool_jobs                      # what is installed, what is missing and how to get it
    python3 -m cf.linux.tool_jobs --check              # run one cycle of every installed kind and print the times
    python3 -m cf.linux.tool_jobs --check llama z3     # only these
    python3 -m cf.linux.tool_jobs --check --config CONFIG   # the kinds the compute apps of a config use
    python3 -m cf.linux.tool_jobs --missing CONFIG     # exit 1 when a compute app of the config lacks its tool
"""
from __future__ import annotations

import ctypes
import glob
import hashlib
import os
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

TOOLS = {"sevenzip": ("7z", "7zz", "7za"), "z3": ("z3",), "openssl": ("openssl",), "llama": ("llama-completion",),
         "tesseract": ("tesseract",), "ffmpeg": ("ffmpeg",)}
INSTALL = {"sevenzip": "sudo apt install p7zip-full", "z3": "sudo apt install z3", "openssl": "sudo apt install openssl",
           "llama": "scripts/ubuntu/install_compute_apps.sh --llama", "tesseract": "sudo apt install tesseract-ocr",
           "ffmpeg": "sudo apt install ffmpeg"}
THREADS = {"sevenzip": 2, "llama": 1, "ffmpeg": 2, "tesseract": 1}       # default worker threads of the tool
CORPUS_ROOTS = ("/usr/share/doc", "/usr/lib/python3/dist-packages", "/usr/bin", "/usr/share/locale", "/usr/lib")

try:
    _LIBC = ctypes.CDLL(None, use_errno=True)
except OSError:      # not Linux
    _LIBC = None


def data_home() -> str:
    """Where scripts/ubuntu/install_compute_apps.sh puts what no package provides (llama.cpp, models)."""
    return os.environ.get("CF_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share", "context_freeze")


def find_tool(kind: str) -> Optional[str]:
    """The tool's binary: $CF_<KIND>_BIN, PATH, then (llama) the build made by install_compute_apps.sh."""
    forced = os.environ.get(f"CF_{kind.upper()}_BIN")
    if forced:
        return forced if os.access(forced, os.X_OK) else None
    for name in TOOLS.get(kind, ()):
        p = shutil.which(name)
        if p:
            return p
        if kind == "llama":
            for d in ("llama.cpp/build/bin", "bin"):
                p = os.path.join(data_home(), "llama", d, name)
                if os.access(p, os.X_OK):
                    return p
    return None


def find_model() -> Optional[str]:
    """The GGUF model of the llama kind: $CF_LLAMA_MODEL, else the first *.gguf (by name) in
    <data home>/llama/models."""
    forced = os.environ.get("CF_LLAMA_MODEL")
    if forced:
        return forced if os.path.isfile(forced) else None
    found = sorted(glob.glob(os.path.join(data_home(), "llama", "models", "*.gguf")))
    return found[0] if found else None


def _text_font() -> Optional[str]:
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
              "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return p
    try:
        import matplotlib
        p = os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans.ttf")
        return p if os.path.exists(p) else None
    except Exception:    # noqa: BLE001
        return None


def missing(kind: str, tool: Optional[str] = None, model: Optional[str] = None) -> str:
    """'' when the kind can run, else what is missing (one line)."""
    if kind not in TOOLS:
        return ""
    if not (tool or find_tool(kind)):
        return f"{'/'.join(TOOLS[kind])} not found"
    if kind == "llama" and not (model or find_model()):
        return f"no GGUF model ($CF_LLAMA_MODEL or {os.path.join(data_home(), 'llama', 'models')}/*.gguf)"
    if kind == "tesseract":
        import importlib.util
        if importlib.util.find_spec("PIL") is None:
            return "python3-pil (draws the page that is read) not found: sudo apt install python3-pil"
        if not _text_font():
            return "no DejaVu / Liberation font to draw the page with: sudo apt install fonts-dejavu-core"
    return ""


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
    check: Optional[Callable[[str], bool]] = None     # called with the stdout after a zero exit status
    env: Optional[dict] = None                # added to the environment of the tool


@dataclass
class Job:
    steps: list
    per_op: bool                              # one log record per step (else one per cycle)
    what: str                                 # one line for the window and the log
    cache: list = field(default_factory=list)  # big input files that must be charged to the app's cgroup


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


# ------------------------------------------------------------------ OpenSSL
def _cached(marker: str, key: str, files: list) -> bool:
    try:
        return open(marker).read() == key and all(os.path.getsize(f) > 0 for f in files)
    except OSError:
        return False


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def build_payload(work: str, mb: int = 32) -> str:
    """One file of `mb` MB made of real files of this system (the 7-Zip corpus, repeated); kept."""
    path, marker = os.path.join(work, "payload.bin"), os.path.join(work, ".cf_payload")
    if _cached(marker, str(mb), [path]) and os.path.getsize(path) == mb << 20:
        return path
    corpus = os.path.join(work, "corpus")
    build_corpus(corpus, 4)
    parts = [os.path.join(corpus, f) for f in sorted(os.listdir(corpus)) if not f.startswith(".")]
    left = mb << 20
    with open(path, "wb") as out:
        while left > 0:
            for f in parts:
                data = open(f, "rb").read()[:left]
                out.write(data)
                left -= len(data)
                if left <= 0:
                    break
    with open(marker, "w") as fh:
        fh.write(str(mb))
    return path


OPENSSL_PASS = "pass:context-freeze"


def openssl_steps(tool: str, work: str, mb: int = 32, iters: int = 600000, rsa_bits: int = 3072) -> list[Step]:
    payload = build_payload(work, mb)
    key, pub = os.path.join(work, "key.pem"), os.path.join(work, "pub.pem")
    if not _cached(os.path.join(work, ".cf_key"), str(rsa_bits), [key, pub]):
        quiet = dict(check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        subprocess.run([tool, "genrsa", "-out", key, str(rsa_bits)], **quiet)
        subprocess.run([tool, "rsa", "-in", key, "-pubout", "-out", pub], **quiet)
        with open(os.path.join(work, ".cf_key"), "w") as fh:
            fh.write(str(rsa_bits))
    digest = _sha256(payload)
    enc, sig = os.path.join(work, "payload.enc"), os.path.join(work, "payload.sig")
    q = shlex.quote
    cipher = f"-aes-256-cbc -pbkdf2 -iter {iters} -pass {OPENSSL_PASS}"
    return [Step("encrypt", [tool, "enc", "-e", *cipher.split(), "-salt", "-in", payload, "-out", enc]),
            Step("decrypt", ["sh", "-c", f"{q(tool)} enc -d {cipher} -in {q(enc)} | {q(tool)} dgst -sha256 -r"],
                 check=lambda out: out.split()[:1] == [digest]),
            Step("sign", ["sh", "-c", f"{q(tool)} dgst -sha256 -sign {q(key)} -out {q(sig)} {q(payload)} && "
                                      f"{q(tool)} dgst -sha256 -verify {q(pub)} -signature {q(sig)} {q(payload)}"],
                 expect="Verified OK")]


# ------------------------------------------------------------------ llama.cpp
LLAMA_PROMPT = "用三句话解释: 操作系统为什么要把长时间不用的内存页换出到磁盘?"


def llama_steps(tool: str, model: str, tokens: int = 64, threads: int = 1) -> list[Step]:
    """One chat turn: load the model, read the question, generate exactly `tokens` tokens (greedy;
    --ignore-eos, so an answer that ends early does not make a shorter job).  ok = text came out.

    One thread by default: llama.cpp's worker threads wait for each other in a busy loop, so with
    two threads on a machine whose cores are all taken - or pinned to one core by a cpuset - a 4 s
    job took one to three minutes and up to 60 s of CPU.  That is a property of its thread pool, and
    it would swamp the limits being measured; one thread used the same 5 s of CPU busy or not.

    The text is not compared with an earlier answer: even with one thread and greedy decoding a
    busy machine now and then gave another continuation (one job in six)."""
    return [Step("generate", [tool, "-m", model, "--jinja", "--single-turn", "-p", LLAMA_PROMPT, "-n", str(tokens),
                              "--ignore-eos", "--temp", "0", "--top-k", "1", "--seed", "1", "-t", str(threads), "-c", "1024",
                              "--no-display-prompt", "--simple-io", "--no-warmup"],
                 check=lambda out: len("".join(out.split())) >= 20)]


# ------------------------------------------------------------------ Tesseract
OCR_TEXT = (
    "An operating system decides which programs may use the processor and how much memory each of them keeps. "
    "When memory runs short the kernel writes pages that have not been touched for a while to the swap device "
    "and gives the frames to whoever needs them now. A program that is brought back to the foreground must first "
    "read those pages again, which is why the first seconds after a switch can feel slow. Control groups make this "
    "policy explicit: every application lives in its own group with a limit on processor time and on memory, and "
    "the limits of the application in front are lifted before its window is raised. Background work continues, "
    "only more slowly, and nothing is killed. The experiment in this repository measures what that costs: how long "
    "a compression job, a constraint solver, an encryption pass or a language model takes while it sits behind "
    "another window, compared with the same job in front. Each job is checked, so a wrong answer never counts as "
    "a fast one. The page you are reading is itself part of such a job: it was drawn once and is now recognised "
    "again and again by a neural network that has never seen it before."
)


def build_page(work: str, text: str = OCR_TEXT, size=(1654, 2339), font_px: int = 34, repeat: int = 2) -> tuple[str, list]:
    """Draw `text` (`repeat` times, as paragraphs) on a white A4 page at 200 dpi -> (png, the words on it)."""
    from PIL import Image, ImageDraw, ImageFont
    path, marker = os.path.join(work, "page.png"), os.path.join(work, ".cf_page")
    key = f"{hashlib.sha256(text.encode()).hexdigest()[:16]} {size} {font_px} {repeat}"
    font = ImageFont.truetype(_text_font(), font_px)
    img = Image.new("L", size, 255)
    draw = ImageDraw.Draw(img)
    margin, y, words = 120, 120, []
    for _ in range(repeat):
        line = ""
        for w in text.split():
            if draw.textlength((line + " " + w).strip(), font=font) > size[0] - 2 * margin:
                draw.text((margin, y), line, font=font, fill=0)
                y += int(font_px * 1.45)
                line = w
            else:
                line = (line + " " + w).strip()
            words.append(w)
        draw.text((margin, y), line, font=font, fill=0)
        y += int(font_px * 2.4)
    if not _cached(marker, key, [path]):
        img.save(path)
        with open(marker, "w") as fh:
            fh.write(key)
    return path, words


def _words(s: str) -> list:
    return re.findall(r"[a-z]+", s.lower())


def ocr_ok(out: str, words: list, need: float = 0.9) -> bool:
    got = set(_words(out))
    want = _words(" ".join(words))
    return bool(want) and sum(w in got for w in want) >= need * len(want)


def tesseract_steps(tool: str, work: str, threads: int = 1) -> list[Step]:
    page, words = build_page(work)
    return [Step("ocr", [tool, page, "stdout", "-l", "eng", "--psm", "6"], env={"OMP_THREAD_LIMIT": str(threads)},
                 check=lambda out: ocr_ok(out, words))]


# ------------------------------------------------------------------ FFmpeg
def ffmpeg_steps(tool: str, work: str, threads: int = 2, seconds: int = 4) -> list[Step]:
    src, out, marker = os.path.join(work, "src.mkv"), os.path.join(work, "out.mp4"), os.path.join(work, ".cf_clip")
    key = f"mandelbrot 1280x720 30 {seconds}"
    if not _cached(marker, key, [src]):
        # a zoom into the Mandelbrot set: detail and motion in every frame, and no file to download
        subprocess.run([tool, "-y", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "mandelbrot=size=1280x720:rate=30",
                        "-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", src],
                       check=True, stdin=subprocess.DEVNULL)
        with open(marker, "w") as fh:
            fh.write(key)

    def rm_out() -> None:
        try:
            os.remove(out)
        except OSError:
            pass

    def made(_out: str) -> bool:
        try:
            return os.path.getsize(out) > 20000
        except OSError:
            return False

    return [Step("transcode", [tool, "-y", "-v", "error", "-nostdin", "-threads", str(threads), "-i", src, "-vf", "scale=960:540",
                               "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-x264-params", f"threads={threads}",
                               "-an", out], prepare=rm_out, check=made)]


# ------------------------------------------------------------------ one entry for every kind
def build(kind: str, tool: str, work: str, threads: Optional[int] = None, level: int = 5, corpus_mb: Optional[float] = None,
          model: Optional[str] = None, tokens: int = 64) -> Job:
    """The steps of `kind` with their inputs prepared in `work` (kept between runs)."""
    os.makedirs(work, exist_ok=True)
    threads = threads or THREADS.get(kind, 1)
    if kind == "sevenzip":
        size, n = build_corpus(os.path.join(work, "corpus"), corpus_mb or 4)
        return Job(sevenzip_steps(tool, work, level, threads), True,
                   f"{tool} a -mx={level} -mmt={threads} + x: {n} files, {size / 2 ** 20:.1f} MB")
    if kind == "z3":
        steps = z3_steps(tool, work)
        return Job(steps, False, f"{tool}: " + ", ".join(f"{s.op} ({s.expect})" for s in steps))
    if kind == "openssl":
        mb = int(corpus_mb or 32)
        return Job(openssl_steps(tool, work, mb), True,
                   f"{tool}: AES-256-CBC + PBKDF2 x600000 of {mb} MB, decrypt + SHA-256, RSA-3072 sign + verify")
    if kind == "llama":
        model = model or find_model()
        if not model:
            raise FileNotFoundError(missing("llama", tool))
        return Job(llama_steps(tool, model, tokens, threads), False,
                   f"{os.path.basename(tool)} {os.path.basename(model)} ({os.path.getsize(model) >> 20} MB): "
                   f"{tokens} tokens, {threads} threads", cache=[model])
    if kind == "tesseract":
        steps = tesseract_steps(tool, work, threads)
        return Job(steps, False, f"{tool}: one A4 page at 200 dpi, eng, {threads} thread(s)")
    if kind == "ffmpeg":
        return Job(ffmpeg_steps(tool, work, threads), False,
                   f"{tool}: 4 s 1280x720 -> 960x540 libx264 medium crf 23, {threads} threads")
    raise ValueError(f"unknown tool kind {kind!r}")


def evict(path: str) -> None:
    """Drop `path` from the page cache.  File pages stay charged to the cgroup that read them first:
    a model read by an earlier run (or by `--check` in a shell) would sit outside the app's
    memory.high.  Read again by the app, it is the app's memory."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass


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
        capture = st.expect is not None or st.check is not None
        try:
            self._proc = subprocess.Popen(st.argv, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, cwd=self.cwd,
                                          env={**os.environ, **st.env} if st.env else None, preexec_fn=_child_setup)
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
        text = (out or b"").decode("utf-8", "replace")
        ok = rc == 0 and (st.expect is None or text.strip() == st.expect)
        if ok and st.check is not None:
            try:
                ok = bool(st.check(text))
            except Exception:    # noqa: BLE001 - a check that cannot run is a failed check
                ok = False
        if not ok:
            self.log.put("err", msg=f"{st.op}: exit {rc}" + (f", answered {text.strip()[:30]!r}" if capture else ""))
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


# ------------------------------------------------------------------ command line
class _PrintLog:
    def __init__(self):
        self.records: list = []

    def put(self, k, **kw):
        self.records.append({"k": k, **kw})


def check(kind: str, cycles: int = 1, work: Optional[str] = None) -> tuple[bool, str]:
    """Run `cycles` cycles of `kind` like the app would -> (all ok, one line with the times)."""
    import tempfile
    why = missing(kind)
    if why:
        return False, f"{why}  ->  {INSTALL[kind]}"
    tool = find_tool(kind)
    try:
        job = build(kind, tool, work or os.path.join(tempfile.gettempdir(), f"cf_bench_{kind}.d"))
    except Exception as e:    # noqa: BLE001
        return False, f"setup failed: {type(e).__name__}: {str(e)[:200]}"
    log, stop = _PrintLog(), threading.Event()
    w = ToolWorker(job.steps, log, stop, per_op=True)
    ok = True
    times: dict = {}
    for _ in range(cycles):
        for st in job.steps:
            r = w._run(st)
            ok = ok and bool(r and r[2])
            times.setdefault(st.op, []).append(r)
    line = ", ".join(f"{op} {sorted(x[0] for x in v)[len(v) // 2]:.0f} ms (cpu {sorted(x[1] for x in v)[len(v) // 2]:.0f} ms)"
                     for op, v in times.items())
    errs = "; ".join(r["msg"] for r in log.records if r["k"] == "err")
    return ok, line + (f"  !! {errs}" if errs else "") + f"   [{job.what}]"


def config_kinds(path: str) -> dict:
    """{app id: tool kind} for the apps of a config that drive a real tool."""
    import json
    from . import apps as A
    cfg = json.load(open(path))
    cat = A.with_overrides(cfg.get("app_overrides"), cfg.get("placeholder_apps"))
    out = {}
    for id_ in cfg.get("apps", []):
        m = re.search(r"cf\.linux\.bench_app\b.*--kind (\S+)", cat[id_].cmd or "") if id_ in cat else None
        if m and m.group(1) in TOOLS:
            out[id_] = m.group(1)
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kinds", nargs="*", help=f"default all: {' '.join(TOOLS)}")
    ap.add_argument("--check", action="store_true", help="run the jobs once and print the times")
    ap.add_argument("--cycles", type=int, default=2, help="--check: cycles per kind (the median is printed)")
    ap.add_argument("--missing", metavar="CONFIG", help="list the compute apps of CONFIG whose tool is not installed; exit 1 if any")
    ap.add_argument("--config", metavar="CONFIG", help="only the kinds the compute apps of CONFIG use")
    args = ap.parse_args(argv)
    if args.config and not args.kinds:
        args.kinds = list(dict.fromkeys(config_kinds(args.config).values()))
        if not args.kinds:
            return 0
    if args.missing:
        bad = {a: (k, missing(k)) for a, k in config_kinds(args.missing).items() if missing(k)}
        for a, (k, why) in bad.items():
            print(f"{a}: {why}  ->  {INSTALL[k]}")
        return 1 if bad else 0
    unknown = [k for k in args.kinds if k not in TOOLS]
    if unknown:
        print(f"unknown kind(s) {unknown}; known: {' '.join(TOOLS)}", file=sys.stderr)
        return 2
    rc = 0
    for kind in args.kinds or list(TOOLS):
        why = missing(kind)
        if why:
            print(f"{kind:10s} MISSING  {why}  ->  {INSTALL[kind]}")
            rc = 1
        elif not args.check:
            extra = f"  model {find_model()}" if kind == "llama" else ""
            print(f"{kind:10s} ok       {find_tool(kind)}{extra}")
        else:
            ok, line = check(kind, max(1, args.cycles))
            print(f"{kind:10s} {'ok      ' if ok else 'FAILED  '} {line}", flush=True)
            rc = rc or (0 if ok else 1)
    return rc


if __name__ == "__main__":
    sys.exit(main())
