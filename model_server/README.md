# Model server (placeholder)

The AI models (speech-to-text, LLM, text-to-speech) run on a **separate GPU server**,
not in this repo's containers. The app reaches them over OpenAI-compatible HTTP endpoints:

| Variable | Purpose |
|---|---|
| `STT_BASE_URL` | OpenAI-compatible `/v1/audio/transcriptions` |
| `LLM_BASE_URL` | OpenAI-compatible `/v1/chat/completions` |
| `TTS_BASE_URL` | OpenAI-compatible `/v1/audio/speech` |

Setup instructions for the GPU server will be added in a later phase.
