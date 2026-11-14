#!/usr/bin/env bash
# Kodera GPU model server: set up a BRAND-NEW GPU machine from scratch, then start it.
# Built for an E2E Networks T4 (16 GB) running Ubuntu 22.04, but any Linux + NVIDIA GPU works.
#
# It: installs system packages -> clones ONLY the model_server folder -> makes a Python venv
# with all packages -> downloads llama.cpp, the LLM, Whisper (speech-to-text) and the Piper
# voice -> writes model_server/.env -> starts the server -> opens the ngrok tunnel.
# Run it again any time: finished steps are skipped and interrupted downloads resume.
#
# Fresh machine, one line (copy it from your notes; the repo is public):
#   curl -fsSL https://raw.githubusercontent.com/Mohammed-merchant/Kodera/main/model_server/bootstrap_e2e.sh -o bootstrap_e2e.sh
#   MODEL_SERVER_API_KEY=xxxx NGROK_AUTHTOKEN=yyyy bash bootstrap_e2e.sh
# (anything not given as a variable is asked for; typing hides the secrets)
#
# Other commands:   bash bootstrap_e2e.sh check | status | start | tunnel | stop
#
# Settings (environment variables, all optional):
#   MODEL_SERVER_API_KEY  must equal LLM_API_KEY in the call app's .env on the VPS
#   NGROK_AUTHTOKEN       your ngrok account token (dashboard.ngrok.com/get-started/your-authtoken)
#   NGROK_URL             tunnel address        (default https://compel-dissuade-subheader.ngrok-free.dev)
#   LLM_SIZE=qwen3-8b|7b|3b  4-bit LLM (default qwen3-8b: Hindi/Gujarati + reliable booking; 7b/3b = Qwen2.5)
#   WHISPER_SIZE=small|base|medium|turbo   speech-to-text model (default small)
#   LLM_PARALLEL          calls the LLM answers at once (default 2)
#   SKIP_NGROK=1          set everything up and start the server, but do not open the tunnel yet
#   PORT                  local gateway port, the one ngrok forwards (default 80, 8000 if not root)
#   BASE                  where to put everything (default ~/kodera-gpu)
#   BRANCH, REPO_URL, LLAMA_TAG   which code / llama.cpp build to use

set -Eeuo pipefail
trap 'echo; echo "FAILED near line $LINENO. Read the message above, fix it and run the script again: it resumes." >&2' ERR
START_TIME=$(date +%s)

REPO_URL=${REPO_URL:-https://github.com/Mohammed-merchant/Kodera.git}
BRANCH=${BRANCH:-main}
BASE=${BASE:-$HOME/kodera-gpu}
REPO_DIR=$BASE/Kodera
MS=$REPO_DIR/model_server
NGROK_URL=${NGROK_URL:-https://compel-dissuade-subheader.ngrok-free.dev}
LLM_SIZE=${LLM_SIZE:-qwen3-8b}
WHISPER_SIZE=${WHISPER_SIZE:-turbo}   # turbo: far better Hindi/Gujarati than small
LLM_PARALLEL=${LLM_PARALLEL:-2}
LLAMA_TAG=${LLAMA_TAG:-b12360}
PORT=${PORT:-80}

GH_LLAMA=https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG
LLAMA_TGZ=llama-$LLAMA_TAG-bin-ubuntu-cuda-12.8-x64.tar.gz        # CUDA 12.8 still supports the T4
CUDART_TGZ=cudart-llama-$LLAMA_TAG-bin-ubuntu-cuda-12.8-x64.tar.gz # CUDA runtime libraries
NGROK_TGZ=https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz
HF=https://huggingface.co
PIPER_BASE=$HF/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium
KOKORO_BASE=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; trap - ERR; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; [ "$PORT" -lt 1024 ] && { warn "Not root: using port 8000 instead of $PORT"; PORT=8000; }; fi

# ---- which model files for the chosen sizes ----
case "$LLM_SIZE" in
  qwen3-8b) LLM_REPO=Qwen/Qwen3-8B-GGUF   # understands Hindi/Gujarati and calls tools reliably
      LLM_FILES=(Qwen3-8B-Q4_K_M.gguf)
      LLM_FIRST=${LLM_FILES[0]}; LLM_CTX=${LLM_CTX:-$((4096 * LLM_PARALLEL))} ;;
  7b) LLM_REPO=Qwen/Qwen2.5-7B-Instruct-GGUF
      LLM_FILES=(qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf)
      LLM_FIRST=${LLM_FILES[0]}; LLM_CTX=${LLM_CTX:-$((4096 * LLM_PARALLEL))} ;;
  3b) LLM_REPO=Qwen/Qwen2.5-3B-Instruct-GGUF
      LLM_FILES=(qwen2.5-3b-instruct-q4_k_m.gguf)
      LLM_FIRST=${LLM_FILES[0]}; LLM_CTX=${LLM_CTX:-$((4096 * LLM_PARALLEL))} ;;
  *) die "LLM_SIZE must be qwen3-8b, 7b or 3b" ;;
esac
case "$WHISPER_SIZE" in
  tiny|base|small|medium) W_REPO=Systran/faster-whisper-$WHISPER_SIZE
      W_FILES=(config.json model.bin tokenizer.json vocabulary.txt); W_DIR=$WHISPER_SIZE ;;
  turbo) W_REPO=deepdml/faster-whisper-large-v3-turbo-ct2
      W_FILES=(config.json model.bin preprocessor_config.json tokenizer.json vocabulary.json); W_DIR=turbo ;;
  *) die "WHISPER_SIZE must be tiny, base, small, medium or turbo" ;;
esac

# ---- helpers ----
dl() {  # dl URL DEST : resumable download; a finished file is skipped
  local url="$1" dest="$2"
  [ -f "$dest.ok" ] && return 0
  mkdir -p "$(dirname "$dest")"
  curl -fL --retry 8 --retry-delay 3 --retry-all-errors -C - -o "$dest" "$url"
  touch "$dest.ok"
}

ask() {  # ask VAR "text": keep the variable if set, else read it (hidden) from the terminal
  local var="$1" text="$2"
  if [ -z "${!var:-}" ]; then
    [ -r /dev/tty ] || die "$var is not set (run: $var=... bash $0)"
    read -rsp "$text: " "$var" </dev/tty; echo
  fi
  [ -n "${!var}" ] || die "$var is empty"
  export "${var?}"
}

wait_health() {  # wait_health URL SECONDS [header]
  local url="$1" secs="$2" i
  for ((i = 0; i < secs; i += 5)); do
    if curl -fsS -m 8 -H 'ngrok-skip-browser-warning: 1' "$url" 2>/dev/null | grep -q '"status":"ok"'; then return 0; fi
    sleep 5
  done
  return 1
}

gpu_line() { nvidia-smi --query-gpu=name,temperature.gpu,memory.used,memory.total --format=csv,noheader 2>/dev/null || echo "no GPU found"; }

# ---- check: can this machine do it? (installs nothing) ----
cmd_check() {
  say "Checking this machine and the download sources (nothing is installed)"
  [ "$(uname -s)" = Linux ] && echo "OK  Linux $(uname -m)" || warn "this script is for Linux"
  if have nvidia-smi; then echo "OK  GPU: $(gpu_line)"; nvidia-smi | grep -m1 "CUDA Version" | sed 's/^/    /'; else warn "nvidia-smi not found: is the NVIDIA driver installed?"; fi
  df -h "${BASE%/*}" 2>/dev/null | tail -1 | awk '{print "OK  disk free: " $4 " (needs ~25 GB)"}'
  local urls=("$GH_LLAMA/$LLAMA_TGZ" "$GH_LLAMA/$CUDART_TGZ" "$NGROK_TGZ" "$PIPER_BASE/en_US-lessac-medium.onnx")
  local f; for f in "${LLM_FILES[@]}"; do urls+=("$HF/$LLM_REPO/resolve/main/$f"); done
  for f in "${W_FILES[@]}"; do urls+=("$HF/$W_REPO/resolve/main/$f"); done
  local u code
  for u in "${urls[@]}"; do
    code=$(curl -sIL -m 25 -o /dev/null -w '%{http_code}' "$u" || true)
    case "$code" in 200|206) echo "OK  $code  ${u#https://}" ;; *) echo "BAD $code  ${u#https://}"; BAD=1 ;; esac
  done
  if have git && git ls-remote -q --exit-code "$REPO_URL" "refs/heads/$BRANCH" >/dev/null 2>&1; then
    echo "OK  repo $REPO_URL ($BRANCH)"
  else
    echo "BAD repo $REPO_URL ($BRANCH) not reachable (is git installed?)"; BAD=1
  fi
  [ -z "${BAD:-}" ] || die "some sources are not reachable"
  echo; echo "All good. Run: bash $0"
}

cmd_status() {
  echo "GPU : $(gpu_line)"
  pgrep -f run_all.py >/dev/null && echo "model server : running" || echo "model server : stopped"
  pgrep -x ngrok >/dev/null && echo "ngrok tunnel : running -> $NGROK_URL" || echo "ngrok tunnel : stopped"
  for u in "http://127.0.0.1:$PORT/health" "$NGROK_URL/health"; do
    printf '%-60s' "$u"; curl -fsS -m 8 -H 'ngrok-skip-browser-warning: 1' "$u" 2>/dev/null || printf 'not answering'; echo
  done
}

cmd_stop() {
  pkill -x ngrok 2>/dev/null && echo "stopped ngrok" || true
  pkill -f run_all.py 2>/dev/null && echo "stopped model server" || true
  sleep 2; pkill -f llama-server 2>/dev/null || true
  pkill -f "uvicorn (stt_server|tts_server|gateway):app" 2>/dev/null || true  # children outlive run_all
  sleep 1
  echo "GPU : $(gpu_line)"
}

# ---- the setup steps ----
step_system() {
  say "1/7  System packages"
  have nvidia-smi || die "nvidia-smi not found. Use a GPU image with the NVIDIA driver installed."
  echo "GPU: $(gpu_line)"
  local free_gb; free_gb=$(df -Pk "$(dirname "$BASE")" | awk 'NR==2 {print int($4/1048576)}')
  [ "$free_gb" -ge 25 ] || die "only ${free_gb} GB free disk; need about 25 GB"
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq git curl ca-certificates python3 python3-venv python3-pip libsndfile1 libgomp1 >/dev/null
}

step_code() {
  say "2/7  Code (only the model_server folder)"
  mkdir -p "$BASE"
  if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" pull --ff-only -q || warn "could not update the code, using what is there"
  else
    git clone -q --depth 1 --branch "$BRANCH" --filter=blob:none --sparse "$REPO_URL" "$REPO_DIR"
    git -C "$REPO_DIR" sparse-checkout set model_server
  fi
  [ -f "$MS/run_all.py" ] || die "model_server/run_all.py not found after cloning $REPO_URL ($BRANCH)"
  mkdir -p "$MS/logs" "$MS/models" "$MS/bin"
}

step_python() {
  say "3/7  Python packages"
  [ -x "$MS/.venv/bin/python" ] || python3 -m venv "$MS/.venv"
  "$MS/.venv/bin/pip" install -q -U pip
  "$MS/.venv/bin/pip" install -q -r "$MS/requirements.txt" nvidia-cublas-cu12 nvidia-cudnn-cu12
  # Kokoro voice on the GPU. onnxruntime-gpu 1.22 is the last one built for CUDA 12 (newer wants 13).
  "$MS/.venv/bin/pip" install -q kokoro-onnx "onnxruntime-gpu==1.22.0"
  # Gujarati voice (Meta MMS-TTS) runs on the CPU with the small CPU build of PyTorch.
  "$MS/.venv/bin/pip" install -q torch --index-url https://download.pytorch.org/whl/cpu
  "$MS/.venv/bin/pip" install -q transformers
}

step_llama() {
  LLAMA_DIR=$MS/bin/llama
  [ -x "$LLAMA_DIR/llama-server" ] && return 0
  say "4a/7  llama.cpp ($LLAMA_TAG, CUDA 12.8)"
  local t=$MS/bin/_dl srv
  dl "$GH_LLAMA/$LLAMA_TGZ" "$t/llama.tgz"
  dl "$GH_LLAMA/$CUDART_TGZ" "$t/cudart.tgz"
  rm -rf "$t/x1" "$t/x2"; mkdir -p "$t/x1" "$t/x2" "$LLAMA_DIR"
  tar -xzf "$t/llama.tgz" -C "$t/x1"; tar -xzf "$t/cudart.tgz" -C "$t/x2"
  srv=$(find "$t/x1" -name llama-server -type f | head -1)
  [ -n "$srv" ] || die "llama-server not found inside $LLAMA_TGZ"
  cp -a "$(dirname "$srv")/." "$LLAMA_DIR/"
  find "$t/x2" \( -name '*.so' -o -name '*.so.*' \) -exec cp -a {} "$LLAMA_DIR/" \;
  chmod +x "$LLAMA_DIR/llama-server"; rm -rf "$t/x1" "$t/x2"
}

step_models() {
  say "4b/7  Models (LLM $LLM_SIZE, Whisper $WHISPER_SIZE, Piper voice)"
  local f
  for f in "${LLM_FILES[@]}"; do dl "$HF/$LLM_REPO/resolve/main/$f" "$MS/models/llm/$f"; done
  for f in "${W_FILES[@]}"; do dl "$HF/$W_REPO/resolve/main/$f" "$MS/models/whisper/$W_DIR/$f"; done
  dl "$PIPER_BASE/en_US-lessac-medium.onnx" "$MS/models/piper/en_US-lessac-medium.onnx"
  dl "$PIPER_BASE/en_US-lessac-medium.onnx.json" "$MS/models/piper/en_US-lessac-medium.onnx.json"
  # The natural voice. If these two cannot be downloaded the server falls back to Piper.
  dl "$KOKORO_BASE/kokoro-v1.0.onnx" "$MS/models/kokoro/kokoro-v1.0.onnx" || warn "Kokoro model not downloaded: using Piper"
  dl "$KOKORO_BASE/voices-v1.0.bin" "$MS/models/kokoro/voices-v1.0.bin" || warn "Kokoro voices not downloaded: using Piper"
  for f in config.json model.safetensors vocab.json tokenizer_config.json special_tokens_map.json; do
    dl "$HF/facebook/mms-tts-guj/resolve/main/$f" "$MS/models/mms-guj/$f" || warn "Gujarati voice file $f missing"
  done
}

step_env() {
  say "5/7  Settings (model_server/.env)"
  umask 077
  cat > "$MS/.env" <<EOF
# Written by bootstrap_e2e.sh. Contains a secret: do not share or commit.
MODEL_SERVER_API_KEY=$MODEL_SERVER_API_KEY
BIND_HOST=127.0.0.1
GATEWAY_PORT=$PORT
NGROK_URL=$NGROK_URL
PRELOAD=true
LLAMA_SERVER_BIN=$MS/bin/llama/llama-server
LLM_GGUF=$MS/models/llm/$LLM_FIRST
LLM_THINKING=false
LLM_ALIAS=qwen
LLM_PORT=8001
LLM_CTX=$LLM_CTX
LLM_PARALLEL=$LLM_PARALLEL
LLM_GPU_LAYERS=99
LLM_THREADS=4
LLM_UBATCH=512
STT_PORT=8002
WHISPER_MODEL=$MS/models/whisper/$W_DIR
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=int8_float16
WHISPER_BEAM_SIZE=3
WHISPER_PROMPT=Clinic appointment booking call. Words: appointment, reschedule, cancel, consultation, fees.
WHISPER_LANGUAGES=en,hi,gu
TTS_PORT=8003
PIPER_VOICE=en_US-lessac-medium
PIPER_VOICE_DIR=$MS/models/piper
KOKORO_DIR=$MS/models/kokoro
KOKORO_VOICE=af_heart
KOKORO_HINDI_VOICE=hf_alpha
GPU_MAX_TEMP_C=85
GPU_MAX_MEM_PERCENT=92
EOF
}

step_server() {
  say "6/7  Starting the model server (the first start loads the models, 1-3 minutes)"
  if pgrep -f run_all.py >/dev/null; then echo "already running"; else
    (cd "$MS" && setsid nohup .venv/bin/python -u run_all.py > logs/run_all.out.log 2> logs/run_all.err.log < /dev/null &)
  fi
  local i
  for ((i = 0; i < 900; i += 5)); do
    pgrep -f run_all.py >/dev/null || { tail -n 15 "$MS/logs/run_all.out.log" "$MS/logs/run_all.err.log" >&2; die "the model server stopped. Log lines above."; }
    wait_health "http://127.0.0.1:$PORT/health" 5 && { echo "model server is healthy"; echo "GPU: $(gpu_line)"; return 0; }
  done
  tail -n 15 "$MS/logs/run_all.out.log" >&2; die "model server not ready after 15 minutes"
}

step_ngrok() {
  say "7/7  ngrok tunnel: ngrok http $PORT --url $NGROK_URL"
  if ! have ngrok; then curl -fsSL "$NGROK_TGZ" | $SUDO tar -xz -C /usr/local/bin ngrok; fi
  ngrok config add-authtoken "$NGROK_AUTHTOKEN" >/dev/null
  if pgrep -x ngrok >/dev/null; then echo "ngrok already running"; else
    (cd "$MS" && setsid nohup ngrok http "$PORT" --url "$NGROK_URL" --log=stdout > logs/ngrok.log 2>&1 < /dev/null &)
  fi
  wait_health "$NGROK_URL/health" 60 || { tail -n 12 "$MS/logs/ngrok.log" >&2
    die "the tunnel is not answering. If the log says the endpoint is already online, another machine (your laptop?) is using this ngrok address: stop it there first."; }
  echo "tunnel is up: $NGROK_URL"
}

step_verify() {
  say "Final check: one full turn (speech-to-text, LLM, voice)"
  (cd "$MS" && .venv/bin/python smoke_test.py --base "http://127.0.0.1:$PORT") || warn "the smoke test reported a problem (see above)"
}

cmd_setup() {
  ask MODEL_SERVER_API_KEY "MODEL_SERVER_API_KEY (the same value as LLM_API_KEY on the VPS)"
  [ "${SKIP_NGROK:-}" = 1 ] || ask NGROK_AUTHTOKEN "NGROK_AUTHTOKEN"
  step_system; step_code; step_python
  step_llama & local p1=$!
  step_models & local p2=$!
  wait "$p1" || die "llama.cpp setup failed"
  wait "$p2" || die "model download failed"
  LLAMA_DIR=$MS/bin/llama
  step_env; step_server
  # Check (and warm up) BEFORE the tunnel opens: on a fresh GPU the first LLM request can take
  # about a minute while CUDA prepares its kernels, and a caller must not be the one to wait.
  step_verify
  if [ "${SKIP_NGROK:-}" = 1 ]; then
    say "Done in $(( ($(date +%s) - START_TIME) / 60 )) min. Server is running; the tunnel was skipped."
    echo "Open it later with:  NGROK_AUTHTOKEN=... bash $0 tunnel"
  else
    step_ngrok
    say "Done in $(( ($(date +%s) - START_TIME) / 60 )) min. The GPU server is live at $NGROK_URL"
  fi
  cat <<EOF

  status : bash $0 status         logs : $MS/logs/
  stop   : bash $0 stop           (then delete the instance to stop the charges)
EOF
}

cmd_start() {
  [ -f "$MS/.env" ] || die "not set up yet: run: bash $0"
  NGROK_AUTHTOKEN=${NGROK_AUTHTOKEN:-x}
  PORT=$(grep -E '^GATEWAY_PORT=' "$MS/.env" | cut -d= -f2)
  NGROK_URL=$(grep -E '^NGROK_URL=' "$MS/.env" | cut -d= -f2)
  step_server
  if have ngrok; then
    pgrep -x ngrok >/dev/null || (cd "$MS" && setsid nohup ngrok http "$PORT" --url "$NGROK_URL" --log=stdout > logs/ngrok.log 2>&1 < /dev/null &)
    wait_health "$NGROK_URL/health" 60 && echo "tunnel is up: $NGROK_URL" || warn "tunnel not answering: see $MS/logs/ngrok.log"
  fi
}

case "${1:-setup}" in
  setup) cmd_setup ;;
  start) cmd_start ;;
  tunnel) ask NGROK_AUTHTOKEN "NGROK_AUTHTOKEN"; PORT=$(grep -E '^GATEWAY_PORT=' "$MS/.env" | cut -d= -f2); NGROK_URL=$(grep -E '^NGROK_URL=' "$MS/.env" | cut -d= -f2); step_ngrok ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  check) cmd_check ;;
  -h|--help|help) sed -n '2,32p' "$0" ;;
  *) die "unknown command '$1' (use: setup, start, tunnel, stop, status, check)" ;;
esac
