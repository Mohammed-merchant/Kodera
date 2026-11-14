# Architecture

A **layered modular monolith**. One FastAPI process handles HTTP, carrier webhooks and the live
audio loop. Only after-call work is event-driven.

```
carrier (Telnyx) ──HTTPS webhooks──▶ api/routes ──▶ business ──▶ providers (Call Control API)
        │                                              │
        └──── WSS media stream ──▶ api/ws ──▶ pipeline (LiveKit Agents) ──HTTP──▶ GPU model server
                                                       │                 (STT / LLM / TTS)
                                                       ▼
                                   db (Postgres)   events ──Redis Streams──▶ workers (ARQ)
```

## Layers

Dependencies point downward only. A layer never imports from a layer above it.

| Layer | Path | Responsibility |
|---|---|---|
| core | `app/core` | Settings, JSON logging, signature and API-key checks. Used by every layer. |
| api | `app/api` | HTTP routes (health, webhooks, admin) and the media-stream WebSocket. Thin: parse, check, delegate. |
| business | `app/business` | Call lifecycle, call state machine, US compliance rules, and LLM tools. |
| pipeline | `app/pipeline` | The LiveKit Agents voice loop: VAD → STT → LLM → TTS, with barge-in. Runs inside the app process; `exotel_io.py` plugs the Exotel WebSocket in as the session's audio input and output (no LiveKit server needed). |
| providers | `app/providers` | Carrier adapters behind one small interface: Telnyx now, Twilio later. |
| db | `app/db` | Async SQLAlchemy session, models (calls, transcripts, consent), repositories and Alembic migrations. |
| events | `app/events` | Publishes and consumes after-call events on Redis Streams. |
| workers | `app/workers` | ARQ jobs for after-call work, such as summaries and syncing. |

## Key decisions

- **No SIP server.** Audio arrives over the carrier's bidirectional WebSocket media stream.
- **The live audio loop stays in one process.** Each call's audio stays inside the app process,
  with no queues in the hot path. Uvicorn runs with a single worker.
- **Models are remote.** STT, LLM and TTS are OpenAI-compatible HTTP endpoints on a separate GPU
  server, so the app container stays small.
- **State:** Postgres keeps durable records. Redis holds live call state and the job queue, and is
  configured with `noeviction` so data is never silently dropped.
