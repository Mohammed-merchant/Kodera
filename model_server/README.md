# Model server (the GPU side)

Everything that needs a GPU lives in this folder and is deployed **on the GPU machine only**. The call app (`app/`, deployed with Docker) never loads a model; it just calls these three HTTP services. This folder imports nothing from `app/` and is excluded from the app's Docker build.

| Service | What runs | Port | OpenAI-compatible endpoint | App setting |
|---|---|---|---|---|
| LLM | Qwen GGUF in llama.cpp `llama-server` | 8001 | `/v1/chat/completions` | `LLM_BASE_URL`, `LLM_MODEL` |
| STT | faster-whisper, `stt_server.py` | 8002 | `/v1/audio/transcriptions` | `STT_BASE_URL` |
| TTS | Piper, `tts_server.py` | 8003 | `/v1/audio/speech` | `TTS_BASE_URL` |

All three check `Authorization: Bearer <MODEL_SERVER_API_KEY>` and refuse to start or serve without a key.

## VRAM budget

| GPU | LLM | STT | TTS |
|---|---|---|---|
| 4 GB (RTX 3050) | Qwen2.5-3B-Instruct Q4_K_M, `LLM_CTX=2048` (~2.3 GB) | `small` int8_float16 (~0.6 GB), or `base` on CPU | Piper (CPU, 0 GB) |
| 8 GB (RTX 5060) | Qwen2.5-7B-Instruct Q4_K_M (~4.7 GB) | `small` int8_float16 (~0.6 GB) | Piper (CPU, 0 GB) |

Check with `nvidia-smi` while all three are running. If you run out, lower `LLM_CTX`, use a smaller Qwen, or move Whisper to CPU.

## Setup on the GPU machine

```bash
cd model_server
python -m venv venv && venv\Scripts\activate        # Linux: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                # then edit it: set MODEL_SERVER_API_KEY
```

1. **NVIDIA driver and CUDA**: faster-whisper needs CUDA 12 cuBLAS/cuDNN (`pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`). RTX 50-series (Blackwell) needs CUDA 12.8+ and CTranslate2 4.6+.
2. **LLM**: install [llama.cpp](https://github.com/ggml-org/llama.cpp/releases) (the CUDA build, which provides `llama-server`) and put it on `PATH`. Download a Qwen GGUF into `model_server/models/` (for example `Qwen2.5-3B-Instruct-Q4_K_M.gguf` from the `Qwen/Qwen2.5-3B-Instruct-GGUF` repo on Hugging Face) and set `LLM_GGUF` in `.env`.
3. **Model files**: run `python download_models.py` (Piper voice into `models/piper/`, Qwen GGUF into `models/`, sizes from `.env`). Or `python download_models.py voice` / `llm` for one of them.
4. **Whisper** downloads itself on first start (to the Hugging Face cache).

## Run

```bash
python run_all.py            # LLM + STT + TTS, stopped together with Ctrl+C
python run_all.py stt tts    # only some, e.g. if the LLM runs under Ollama
```

Check from any machine:

```bash
curl http://<gpu-ip>:8002/health
curl http://<gpu-ip>:8003/health
curl http://<gpu-ip>:8001/v1/models -H "Authorization: Bearer <key>"
```

## Connect the app

In the app's `.env` (not this folder's):

```
LLM_BASE_URL=http://<gpu-ip>:8001/v1
LLM_MODEL=qwen                  # = LLM_ALIAS here
LLM_API_KEY=<MODEL_SERVER_API_KEY>
STT_BASE_URL=http://<gpu-ip>:8002/v1
TTS_BASE_URL=http://<gpu-ip>:8003/v1
```

The app currently has an API-key setting only for the LLM. When the voice pipeline is wired in, STT and TTS will use the same key.

Open ports 8001-8003 on the GPU machine's firewall **only to the app server's IP** (or use a VPN such as WireGuard/Tailscale). The traffic is plain HTTP; do not expose these ports to the internet.

## Notes

- `tts_server.py` returns `pcm` (raw 16-bit mono, 24 kHz) by default, which is what Pipecat's OpenAI TTS service asks for. `wav` is also supported. `mp3`/`opus` are not.
- `PRELOAD=true` loads Whisper and the Piper voice at startup so the first phone call is not slow.

## A fresh rented GPU server in one command (E2E Networks T4, any Linux + NVIDIA GPU)

`bootstrap_e2e.sh` sets up a brand-new machine from nothing and starts everything. Use it when
you create a new instance and delete it when you are done (so you only pay while it exists).

```bash
curl -fsSL https://raw.githubusercontent.com/Mohammed-merchant/Kodera/main/model_server/bootstrap_e2e.sh -o bootstrap_e2e.sh
MODEL_SERVER_API_KEY=the-key-from-the-VPS NGROK_AUTHTOKEN=your-ngrok-token bash bootstrap_e2e.sh
```

It installs the system packages, clones **only** `model_server`, builds the Python environment,
downloads llama.cpp (CUDA 12.8), Qwen2.5 7B (4-bit), Whisper `small` and the Piper voice, writes
`.env`, starts the server and opens `ngrok http 80 --url <your address>`. About 10 to 15
minutes on a fresh machine; run it again after a failure and it resumes.

| Command | What it does |
|---|---|
| `bash bootstrap_e2e.sh check` | tests every download link and the GPU, installs nothing |
| `bash bootstrap_e2e.sh status` | processes, GPU, local and public health |
| `bash bootstrap_e2e.sh start` / `stop` | restart or stop the server and tunnel |

Options (environment variables): `LLM_SIZE=7b|3b`, `WHISPER_SIZE=small|base|medium|turbo`,
`LLM_PARALLEL` (calls answered at once, default 2), `NGROK_URL`, `PORT`, `BASE`.

Only one machine can use the ngrok address at a time: stop the tunnel on your laptop first.
A T4 needs CUDA 12 builds (CUDA 13 builds dropped support for that GPU generation).
On Windows laptops use `gpu_server.cmd start|stop|status` instead.
