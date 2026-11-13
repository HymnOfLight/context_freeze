#!/usr/bin/env bash
# The real programs behind the compute apps of the three-class experiment (manual §6.9).
#
#   scripts/ubuntu/install_compute_apps.sh            # apt: 7-Zip, Z3, OpenSSL, Tesseract (OCR), FFmpeg
#   scripts/ubuntu/install_compute_apps.sh --llama    # + a local large language model: llama.cpp built from
#                                                     #   source (~3-10 min) and a 470 MB GGUF model
#   scripts/ubuntu/install_compute_apps.sh --check    # afterwards: run every job once and print the times
#   scripts/ubuntu/install_compute_apps.sh --llama --check
#
# Run it as the desktop user (it calls sudo for apt only).  Nothing here needs an account.
#
# llama.cpp and the model go to ~/.local/share/context_freeze/llama (CF_DATA_HOME moves it):
#   LLAMA_TAG=v0.9.0                the llama.cpp release that is built (the flags the job uses were tested with it)
#   LLAMA_REPO=https://github.com/ggml-org/llama.cpp.git    a mirror, when github.com is slow (or export
#                                   https_proxy first, manual §2.5)
#   MODEL_URL=<url of a .gguf>      another model; default Qwen2.5-0.5B-Instruct Q4_K_M from ModelScope,
#                                   then hf-mirror.com, then huggingface.co
# A model you already have:  mkdir -p ~/.local/share/context_freeze/llama/models && cp x.gguf there
# (the first *.gguf by name is used), or export CF_LLAMA_MODEL=/path/x.gguf; a llama-completion binary
# you already have: put it on PATH or export CF_LLAMA_BIN=/path/llama-completion.
set -euo pipefail
cd "$(dirname "$0")/../.."
LLAMA=0; CHECK=0
for a in "$@"; do
  case "$a" in
    --llama) LLAMA=1 ;;
    --check) CHECK=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] && { echo "run it as the desktop user, not with sudo (llama.cpp and the model go to your home)" >&2; exit 2; }

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -q
sudo apt-get install -y -q p7zip-full z3 openssl tesseract-ocr ffmpeg python3-pil fonts-dejavu-core

if [ "$LLAMA" = 1 ]; then
  HOME_DIR="${CF_DATA_HOME:-$HOME/.local/share/context_freeze}/llama"
  TAG="${LLAMA_TAG:-v0.9.0}"
  REPO="${LLAMA_REPO:-https://github.com/ggml-org/llama.cpp.git}"
  BIN="$HOME_DIR/llama.cpp/build/bin/llama-completion"
  mkdir -p "$HOME_DIR/models"
  if [ -x "$BIN" ] && [ "$(cat "$HOME_DIR/llama.cpp/.cf_tag" 2>/dev/null)" = "$TAG" ]; then
    echo "== llama.cpp $TAG already built: $BIN"
  else
    sudo apt-get install -y -q build-essential cmake git curl
    rm -rf "$HOME_DIR/llama.cpp"
    echo "== fetching llama.cpp $TAG from $REPO"
    git clone --depth 1 --branch "$TAG" "$REPO" "$HOME_DIR/llama.cpp"
    flags=(-DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF
           -DLLAMA_BUILD_SERVER=OFF -DLLAMA_BUILD_APP=OFF -DBUILD_SHARED_LIBS=OFF)
    build() { # extra cmake flags...
      rm -rf "$HOME_DIR/llama.cpp/build"
      (cd "$HOME_DIR/llama.cpp" && CC=gcc CXX=g++ cmake -B build "${flags[@]}" "$@" > build.log 2>&1 \
         && cmake --build build -j"$(nproc)" --target llama-completion >> build.log 2>&1)
    }
    echo "== building llama-completion (log: $HOME_DIR/llama.cpp/build.log)"
    if ! build; then
      # -mcpu=native is not understood by every compiler / hypervisor combination
      echo "   build failed; once more without CPU-specific flags (GGML_NATIVE=OFF)"
      build -DGGML_NATIVE=OFF || { tail -30 "$HOME_DIR/llama.cpp/build.log" >&2; echo "!! llama.cpp did not build" >&2; exit 1; }
    fi
    [ -x "$BIN" ] || { echo "!! $BIN was not produced (see build.log)" >&2; exit 1; }
    echo "$TAG" > "$HOME_DIR/llama.cpp/.cf_tag"
    echo "== built $BIN"
  fi
  if ls "$HOME_DIR"/models/*.gguf >/dev/null 2>&1 || [ -n "${CF_LLAMA_MODEL:-}" ]; then
    echo "== model present: ${CF_LLAMA_MODEL:-$(ls "$HOME_DIR"/models/*.gguf | head -1)}"
  else
    name=qwen2.5-0.5b-instruct-q4_k_m.gguf
    urls=("https://modelscope.cn/models/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/master/$name"
          "https://hf-mirror.com/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/$name"
          "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/$name")
    if [ -n "${MODEL_URL:-}" ]; then urls=("$MODEL_URL"); name=$(basename "${MODEL_URL%%\?*}"); fi
    ok=0
    for u in "${urls[@]}"; do
      echo "== downloading the model (about 470 MB): $u"
      if curl -fL --retry 3 --connect-timeout 20 -C - -o "$HOME_DIR/models/$name.part" "$u" \
         && [ "$(head -c 4 "$HOME_DIR/models/$name.part")" = GGUF ]; then
        mv "$HOME_DIR/models/$name.part" "$HOME_DIR/models/$name"; ok=1; break
      fi
      echo "   failed or not a GGUF file; next source"
      rm -f "$HOME_DIR/models/$name.part"
    done
    [ "$ok" = 1 ] || { echo "!! no model downloaded: fetch a .gguf yourself into $HOME_DIR/models/" >&2; exit 1; }
  fi
fi

echo
if [ "$CHECK" = 1 ]; then
  python3 -m cf.linux.tool_jobs --check
else
  python3 -m cf.linux.tool_jobs || true
  echo "run every job once:  python3 -m cf.linux.tool_jobs --check"
fi
