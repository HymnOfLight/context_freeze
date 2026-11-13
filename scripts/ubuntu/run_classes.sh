#!/usr/bin/env bash
# Three-class experiment (manual §6.9): one short run per cell that answers, for three kinds of apps,
# "what does limiting the background cost?"
#   1. interactive (wechat / qq / wps / firefox): how long a call / switch takes to answer and whether the
#      app is smooth right afterwards
#   2. compute (sevenzip, z3 - the installed 7-Zip and Z3 doing a fixed job over and over): time per job and
#      jobs per minute, foreground vs background
#   3. download (baidunetdisk, xunlei - each with a download task running): MB/s, network round trip and
#      receive backlog, read every second from the kernel's TCP statistics of the app's sockets
#
# Before the first run:   sudo apt install -y p7zip-full z3
#                         start a big download in 百度网盘 and in 迅雷 (enough for 40 min; a speed limit of a few
#                         MB/s in the app keeps the rate steady and the disk from filling) and leave both running
#
#   scripts/ubuntu/run_classes.sh                        # none + mild + squeeze, 2 rounds, ~10 min per cell
#   CELLS="none squeeze" scripts/ubuntu/run_classes.sh    # subset: none | mild | squeeze  (~20 min)
#   ROUNDS=3 DWELL=20 scripts/ubuntu/run_classes.sh       # more calls per app / longer background stretches
#   SEEDS="1 2" scripts/ubuntu/run_classes.sh             # replicates (the visiting order is fixed; the seed only names the run)
#   ONBOARD=1 scripts/ubuntu/run_classes.sh               # walk the apps that need an account through login / prep first
#   EXTRA="--placeholders" scripts/ubuntu/run_classes.sh  # apps that are not installed run as the bundled stand-in
#   DL_MIN_KBPS=0 scripts/ubuntu/run_classes.sh           # do not insist that the download apps are downloading right now
#   RTT_HOST=223.5.5.5:443 scripts/ubuntu/run_classes.sh  # host the latency probe inside the download apps' cgroups connects to
#                                                         # (default www.baidu.com:443; "" = no probe)
#   STRICT=0 / SWAPPINESS=60 / DRY=1                      # as in run_squeeze.sh
#
# Responsiveness + compute only, with more compute apps (no download apps, nothing to log in to or to start by hand
# for the compute class): 7-Zip, Z3, OpenSSL (encrypt / decrypt / sign), a local large language model (llama.cpp),
# Tesseract OCR, FFmpeg transcoding - 14 steps per round, ~10 min per cell
#   scripts/ubuntu/install_compute_apps.sh --llama --check                 # once: the tools, llama.cpp + a model, one test job each
#   CONFIG=configs/ubuntu_arm64_compute.json OUT=results/arm64_compute scripts/ubuntu/run_classes.sh
#   TOOLCHECK=0                                           # skip the test job of every compute app before the first cell
#
# All three classes at full size - 13 real apps: the 4 interactive ones, the 6 compute apps above and 3 download
# apps: 百度网盘, 迅雷 (logged in, a download running in each, speed limited in the app) and browser_dl (a second
# Firefox that keeps downloading a large file by itself, held at 2 MB/s) - 17 steps per round, ~15 min per cell.
# Setting the three download apps up: manual §6.9 "下载型三个应用怎么配置".
#   CONFIG=configs/ubuntu_arm64_full.json OUT=results/arm64_full scripts/ubuntu/run_classes.sh
#   ROUNDS=6 SEEDS="1 2 3" CONFIG=configs/ubuntu_arm64_full.json OUT=results/arm64_full_night scripts/ubuntu/run_classes.sh   # ~6 h
#   CF_BROWSER_DL_RATE_MBPS=4 / CF_BROWSER_DL_URL=https://...   # browser_dl: speed to hold (0 = unlimited), file to fetch
#   DL_DISK_CHECK=0                                       # do not stop when the downloads would not fit on the disk
#   DL_TRIM=1                                             # delete every file 百度网盘 / 迅雷 finish while the run goes on (cf.linux.dl_trim:
#                                                         # only files it saw them write): the disk holds the files in progress,
#                                                         # not rate x time.  Queue many files of 1 - 3 GB, one task at a time
#                                                         # (python3 -m cf.linux.dl_files lists links)
#   DL_TRIM=punch                                         # also free the written part of the files in progress (they read back
#                                                         # as zeros: try it on one task first, an app may reject the finished file)
#   DL_TRIM_ARGS="--dir /mnt/dl"                          # more flags for cf.linux.dl_trim (a download directory that is hidden
#                                                         # or under a system path, --grace, --min-mb)
#
# Synthetic variant (no tools, accounts or download tasks needed: bundled numeric / zlib / downloader apps and a
# local rate-limited source):
#   CONFIG=configs/ubuntu_arm64_classes_synthetic.json OUT=results/arm64_classes_syn scripts/ubuntu/run_classes.sh
#   DL_URL=https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/22.04/ubuntu-22.04.5-desktop-amd64.iso   # real file instead
#   DL_RATE_MBPS=10                                       # local source: MB/s per connection (default 20)
#
# cells (configs/ubuntu_arm64_classes.json):
#   none     policy none: nothing limited - the baseline every "x" / "%" column refers to
#   mild     static, 1 core cpu.max quota + cpu.weight 20, memory.high = 0.5 x m_fg, one reclaim
#   squeeze  static, the config's profile: all background apps on the last core, cpu.max 10 % of it, cpu.weight 1,
#            memory.high = 0.1 x m_fg, memory.reclaim x3 on the switch-away
# All cells walk the same fixed visiting order (trace kind "pattern"), so even this short run is comparable.
#
# Output: $OUT/*.jsonl (+ .ckpt while running; re-run the same command to resume), summary.csv,
# classes_interactive.csv / classes_compute.csv / classes_download.csv, classes_report.md (the three tables),
# classes_tables.xlsx (Chinese headers), plots/classes.png and plots/<run>_bench.png.
# Run it inside the Xorg session (or with DISPLAY exported, manual §2.4) after setup_cgroups.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
CONFIG="${CONFIG:-configs/ubuntu_arm64_classes.json}"
OUT="${OUT:-results/arm64_classes}"
ROUNDS="${ROUNDS:-2}"
DWELL="${DWELL:-15}"
SEEDS="${SEEDS:-1}"
CELLS="${CELLS:-none mild squeeze}"
DL_URL="${DL_URL:-}"
DL_RATE_MBPS="${DL_RATE_MBPS:-20}"
DL_PORT="${DL_PORT:-18080}"
RTT_HOST="${RTT_HOST-www.baidu.com:443}"
EXTRA="${EXTRA:-}"
DRY="${DRY:-0}"
mkdir -p "$OUT"
LOG="$OUT/run_classes.log"
exec > >(tee -a "$LOG") 2>&1
PAT=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))['trace']['pattern']))" "$CONFIG")
T=$((ROUNDS * PAT))
echo "== run_classes $(date -Is): config=$CONFIG rounds=$ROUNDS (T=$T) dwell=${DWELL}s seeds=$SEEDS cells=$CELLS out=$OUT"

has_app() { python3 -c "import json,sys; sys.exit(0 if sys.argv[2] in json.load(open(sys.argv[1]))['apps'] else 1)" "$CONFIG" "$1"; }
has_downloads() { python3 -c "import json,sys; sys.exit(0 if (json.load(open(sys.argv[1])).get('classes') or {}).get('download') else 1)" "$CONFIG"; }
MINUTES=$(( (T * (DWELL + 6) / 60 + 2) * $(echo $CELLS | wc -w) * $(echo $SEEDS | wc -w) ))
SRV_PID=""
TRIM_PID=""
DL_TRIM="${DL_TRIM:-0}"
old_swappiness=""
cleanup() {
  [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null || true
  [ -n "$TRIM_PID" ] && kill "$TRIM_PID" 2>/dev/null || true
  # the bench apps would otherwise keep computing / downloading after the experiment
  pkill -f "cf.linux.bench_app --app" 2>/dev/null || true
  pkill -f "cf.linux.browser_dl --app" 2>/dev/null || true       # it stops its browser and removes the partial file
  [ -n "$old_swappiness" ] && sudo sysctl -q -w vm.swappiness="$old_swappiness" || true
}

if [ "$DRY" != 1 ]; then
  if [ -z "${DISPLAY:-}" ]; then
    pid=$(pgrep -u "$USER" -x gnome-shell | head -1 || true)
    [ -n "$pid" ] || { echo "!! no DISPLAY and no gnome-shell session" >&2; exit 2; }
    for v in DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE; do
      val=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^$v=//p"); [ -n "$val" ] && export "$v=$val"
    done
  fi
  grep -q '^0::/cf/' /proc/self/cgroup || sudo scripts/ubuntu/setup_cgroups.sh --pid $$
  grep -q '^0::/cf/' /proc/self/cgroup || { echo "!! still outside /cf" >&2; exit 2; }
  if [ "${ZRAM_MB:-4096}" != 0 ] && ! grep -q zram /proc/swaps; then sudo scripts/ubuntu/setup_zram.sh "${ZRAM_MB:-4096}"; fi
  # shellcheck disable=SC2086
  python3 run_linux.py "$CONFIG" --scenario list > "$OUT/apps_resolved.txt" || true
  if grep -q '^missing:' "$OUT/apps_resolved.txt"; then
    echo "!! not installed: $(grep '^missing:' "$OUT/apps_resolved.txt" | tr '\n' ' ')  (EXTRA=\"--placeholders\" runs them as the stand-in)"; fi
  if ! tools=$(python3 -m cf.linux.tool_jobs --missing "$CONFIG"); then
    echo "!! the compute apps drive real programs, and these are not ready (scripts/ubuntu/install_compute_apps.sh [--llama]):" >&2
    echo "$tools" | sed 's/^/   /' >&2
    echo "   (or take the app out of apps / classes.compute / trace.pattern of the config; STRICT=0 runs without it)" >&2
    [ "${STRICT:-1}" = 1 ] && exit 2
  fi
  want=$(python3 -c "import json,sys; print(' '.join(json.load(open(sys.argv[1]))['apps']))" "$CONFIG")
  others=""
  for leaf in /sys/fs/cgroup/cf/*/; do
    id=$(basename "$leaf"); [ "$id" = host ] && continue
    n=$(wc -l < "$leaf/cgroup.procs" 2>/dev/null || echo 0); [ "$n" -gt 0 ] || continue
    case " $want " in *" $id "*) continue;; esac
    others+=" $id($n procs, $(( $(cat "$leaf/memory.current") / 1048576 )) MB)"
  done
  if [ -n "$others" ]; then
    echo "!! running but not part of this experiment:$others"
    echo "   quit them (they take RAM / CPU and distort the job times), or STRICT=0 to run anyway"
    [ "${STRICT:-1}" = 1 ] && exit 2
  fi
  trap cleanup EXIT
  # bench apps left over from an earlier run would keep their old log position and old download source
  pkill -f "cf.linux.bench_app --app" 2>/dev/null && sleep 1 || true
  # one job of every compute app, here and unlimited: a tool that is installed but does not work (a model that
  # does not load, a codec that is missing) would otherwise only show up as bad_jobs after the run
  if [ "${TOOLCHECK:-1}" = 1 ]; then
    echo "  one test job per compute app (TOOLCHECK=0 skips this):"
    if ! python3 -m cf.linux.tool_jobs --check --cycles 1 --config "$CONFIG" | sed 's/^/    /'; then
      echo "!! a compute app does not do its job correctly - see the line marked FAILED / MISSING above" >&2
      [ "${STRICT:-1}" = 1 ] && exit 2
    fi
  fi
  SWAPPINESS="${SWAPPINESS:-200}"
  cur=$(cat /proc/sys/vm/swappiness)
  if [ "$SWAPPINESS" != "$cur" ]; then
    sudo sysctl -q -w vm.swappiness="$SWAPPINESS" && old_swappiness="$cur"
    echo "  vm.swappiness $cur -> $(cat /proc/sys/vm/swappiness) for the run"
  fi
  # bundled downloader only - its source: a real URL, or the bundled rate-limited one on the loopback
  # (it runs in this script's cgroup /cf/host, which is never limited)
  if ! has_app bench_download; then :
  elif [ -z "$DL_URL" ]; then
    DL_URL="http://127.0.0.1:${DL_PORT}/stream"
    if ! python3 -m cf.linux.bench_app --check-url "$DL_URL" >/dev/null 2>&1; then
      python3 -m cf.linux.bench_app --serve "$DL_PORT" --rate-mbps "$DL_RATE_MBPS" >> "$OUT/download_source.log" 2>&1 &
      SRV_PID=$!
      sleep 1
    fi
    echo "  download source: $DL_URL (local, ${DL_RATE_MBPS} MB/s per connection)"
  else
    echo "  download source: $DL_URL"
  fi
  if has_app bench_download; then
    why=$(python3 -m cf.linux.bench_app --check-url "$DL_URL" 2>&1) || { echo "!! download source not reachable: $why" >&2; exit 2; }
    export CF_BENCH_URL="$DL_URL"
  fi
  # the latency probe of the download apps (inside each app's cgroup) connects to this host twice a second
  export CF_BENCH_RTT_HOST="$RTT_HOST"
  if [ -n "$RTT_HOST" ] && has_downloads; then
    echo "  latency probe of the download apps: TCP connect to $RTT_HOST"; fi
  if [ "${ONBOARD:-0}" = 1 ]; then
    python3 -m cf.linux.blankdoc --reset >/dev/null
    CONFIG="$CONFIG" scripts/ubuntu/cf_onboard.sh || true
  fi
  # shellcheck disable=SC2086
  python3 run_linux.py "$CONFIG" --onboard-check $EXTRA | tee "$OUT/onboard.txt" \
    || echo "  note: some apps are not ready - ONBOARD=1 (or scripts/ubuntu/cf_onboard.sh) first"
  # the browser that downloads by itself: start it now, so that it is downloading when the check below looks
  if has_app browser_dl; then
    if python3 -m cf.linux.browser_dl --check | sed 's/^/    browser_dl: /'; then
      if ! pgrep -u "$USER" -f "cf.linux.browser_dl --app browser_dl" >/dev/null; then
        python3 run_linux.py "$CONFIG" --launch browser_dl | sed 's/^/    /' || true
        echo "    waiting 25 s for its download to start"; sleep 25
      fi
    elif echo " $EXTRA " | grep -q -- "--placeholders"; then
      echo "    browser_dl cannot run right now (see above) - continuing because of --placeholders"
    else
      echo "!! browser_dl cannot run (see above; manual §6.9 '下载型三个应用怎么配置')" >&2
      [ "${STRICT:-1}" = 1 ] && exit 2
    fi
  fi
  # real download apps: they must be downloading now - nothing else is measured for them
  rc=0
  if [ "$DL_TRIM" != 0 ]; then
    # finished files are deleted as the run goes on: what the disk has to hold is the files in progress
    python3 -m cf.linux.netstat "$CONFIG" --watch 6 --min-kbps "${DL_MIN_KBPS:-50}" || rc=$?
  else
    python3 -m cf.linux.netstat "$CONFIG" --watch 6 --min-kbps "${DL_MIN_KBPS:-50}" --minutes "$MINUTES" || rc=$?
  fi
  if [ "$rc" = 3 ]; then
    echo "   (DL_TRIM=1 deletes the files the download apps finish during the run; DL_DISK_CHECK=0 runs anyway)"
    [ "${DL_DISK_CHECK:-1}" = 1 ] && [ "${STRICT:-1}" = 1 ] && exit 2
  elif [ "$rc" != 0 ]; then
    echo "   start a big download (enough for $MINUTES min) in each of these apps, leave it running and re-run;"
    echo "   DL_MIN_KBPS=0 or STRICT=0 runs anyway (their download columns will be empty or low)"
    [ "${STRICT:-1}" = 1 ] && exit 2
  fi
  if [ "$DL_TRIM" != 0 ] && has_downloads; then
    trim_flags="${DL_TRIM_ARGS:-}"; [ "$DL_TRIM" = punch ] && trim_flags="--punch $trim_flags"
    echo "  files the download apps are writing now (DL_TRIM=$DL_TRIM: finished ones are deleted during the run$( [ "$DL_TRIM" = punch ] && echo ', the written part of the others is freed'); log: $OUT/dl_trim.log):"
    # shellcheck disable=SC2086
    python3 -m cf.linux.dl_trim "$CONFIG" --list $trim_flags | sed 's/^/    /' || true
    # shellcheck disable=SC2086
    python3 -m cf.linux.dl_trim "$CONFIG" $trim_flags >> "$OUT/dl_trim.log" 2>&1 &
    TRIM_PID=$!
  fi
  free_gb=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc 0-9)
  if has_downloads || has_app bench_download; then
    [ "${free_gb:-99}" -lt 10 ] && echo "!! only ${free_gb} GB free in $HOME - the downloads keep writing during the run"
  else
    [ "${free_gb:-99}" -lt 2 ] && echo "!! only ${free_gb} GB free in $HOME - the result files and the swap file need room"
  fi
fi

M_FG_FROM=""
FAILED=0
run_one() { # name policy seed [extra flags...]
  local name="$1" pol="$2" seed="$3" rc; shift 3
  if [ -f "$OUT/${name}.jsonl" ] && tail -c 4096 "$OUT/${name}.jsonl" | grep -q '"type": "end"'; then
    echo "== $name already finished - skipping"
    [ -z "$M_FG_FROM" ] && M_FG_FROM="$OUT/${name}.jsonl"
    return 0
  fi
  local cmd=(python3 -u run_linux.py "$CONFIG" --policy "$pol" --eta 0.0 --T "$T" --seed "$seed"
             --name "$name" --out-dir "$OUT" --dwell "$DWELL" --work)
  [ -n "$M_FG_FROM" ] && cmd+=(--m-fg-from "$M_FG_FROM")
  # shellcheck disable=SC2206
  [ -n "$EXTRA" ] && cmd+=($EXTRA)
  [ $# -gt 0 ] && cmd+=("$@")
  [ -f "$OUT/${name}.ckpt" ] && cmd+=(--resume)
  if [ "$DRY" = 1 ]; then echo "${cmd[*]}"; return 0; fi
  set +e
  "${cmd[@]}" 2>&1 | tee -a "$OUT/${name}.console.log"
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" = 130 ]; then echo "== interrupted. Resume with:  OUT=$OUT $0"; exit 130
  elif [ "$rc" = 2 ]; then echo "== preflight failed; fix the environment and re-run:  OUT=$OUT $0"; exit 2
  elif [ "$rc" != 0 ]; then echo "!! $name failed (exit $rc); continuing"; FAILED=1; fi
  if [ -z "$M_FG_FROM" ] && grep -q '"type": "budget"' "$OUT/${name}.jsonl" 2>/dev/null; then
    M_FG_FROM="$OUT/${name}.jsonl"; echo "== m_fg (per-app foreground footprint) fixed from $M_FG_FROM"
  fi
}
has() { echo " $1 " | grep -q " $2 "; }

for seed in $SEEDS; do
  has "$CELLS" none    && run_one "none_classes_s${seed}" none "$seed"
  has "$CELLS" mild    && run_one "mild_classes_s${seed}" static "$seed" \
                            --cpu-mode quota --cpu-cores 1 --cpu-weight 20 --mem-frac 0.5 --squeeze 0
  has "$CELLS" squeeze && run_one "squeeze_classes_s${seed}" static "$seed"
done

[ "$DRY" = 1 ] && exit 0
if ! ls "$OUT"/*.jsonl >/dev/null 2>&1; then echo "!! no result files in $OUT"; exit 1; fi
python3 -m cf.linux.analyze "$OUT"/*.jsonl --csv "$OUT/summary.csv" >/dev/null || true
python3 -m cf.linux.classes "$OUT"/*.jsonl --out "$OUT" || true
[ "$FAILED" = 1 ] && echo "== some cells are incomplete; finish them with:  OUT=$OUT $0"
echo "== results in $OUT  (classes_report.md, classes_tables.xlsx, classes_interactive.csv, classes_compute.csv, classes_download.csv, plots/classes.png, plots/<run>_bench.png)"
