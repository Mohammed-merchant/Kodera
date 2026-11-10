# Exotel setup

The app can use Exotel (India) or Twilio. `CALL_PROVIDER` in `.env` picks one. Only that
carrier's routes answer; the other carrier's URLs return 404.

## How a call flows

```
Caller -> ExoPhone -> Passthru applet -> Voicebot applet -> wss://DOMAIN/ws/exotel
                          |
                          v
        POST https://DOMAIN/webhooks/exotel/passthru?token=SECRET   (saves the call)
```

There is no TwiML in Exotel. The flow is built in the Exotel dashboard (ExoPhones, then
App Bazaar). Our server only answers the Passthru request and the WebSocket.

## 1. Values for `.env` (on the server)

| Variable | Where to get it |
|---|---|
| `CALL_PROVIDER` | `exotel` |
| `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`, `EXOTEL_ACCOUNT_SID` | Exotel dashboard, Settings, API Settings |
| `EXOTEL_PHONE_NUMBER` | Your ExoPhone, e.g. `+91...` |
| `EXOTEL_WEBHOOK_SECRET` | You make it: `openssl rand -hex 24` |
| `EXOTEL_ALLOWED_IPS` | Optional. Ask Exotel support (hello@exotel.com) for their webhook IPs |

Then run `bash scripts/deploy.sh`.

## 2. Build the call flow in the Exotel dashboard

1. ExoPhones, open your number, choose **Incoming Call Flow**, create a new flow in App Bazaar.
2. Add a **Passthru** applet. URL (HTTPS, method POST):
   `https://industinternational.com/webhooks/exotel/passthru?token=YOUR_EXOTEL_WEBHOOK_SECRET`
   Our server answers HTTP 200 with an empty body, so the flow continues.
3. Add a **Voicebot** applet after it. URL:
   `wss://YOUR_EXOTEL_API_KEY:YOUR_EXOTEL_API_TOKEN@industinternational.com/ws/exotel`
   The key and token in the URL make Exotel send a Basic-auth header. The server refuses any
   WebSocket without it. (Keep the default 8 kHz audio. Do not add `?sample-rate=` yet.)
4. Save the flow and attach it to the ExoPhone.

## 3. Security (what protects each URL)

| URL | Protection |
|---|---|
| `/webhooks/exotel/passthru`, `/status` | Secret `?token=` (constant-time check), optional IP allowlist, optional AccountSid check |
| `wss://.../ws/exotel` | Basic auth (API key and token), hard 120 s limit per call, size limits |

Exotel documents no webhook signature, so the token in the URL is our own convention.
Caddy's access log records the full URL, so the token appears in the server's Caddy logs.
Keep server logs private. Rotate `EXOTEL_WEBHOOK_SECRET` (change `.env` and the Passthru URL)
if it leaks.

## 4. First real call: what to check

Exotel's docs do not agree with each other everywhere, so the first live call confirms these.
After a test call, run:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs app --since 10m | grep -i exotel
```

| Log line | What it tells you |
|---|---|
| `exotel_call_received` | Passthru works. Check the numbers (masked) and `direction` |
| `exotel_stream_started` (shows `media_format`) | The WebSocket and Basic auth work. Check `sample_rate` is 8000 |
| `exotel_stream_ended` (`frames`, `chunks_sent`, `reason`) | Audio arrived and was echoed. `reason` should be `callended` |
| `exotel_ws_rejected` | Wrong or missing Basic auth in the Voicebot URL |
| `exotel_webhook_bad_token` | Wrong token in the Passthru URL |
| `exotel_status_unrecognised` (lists field names) | The status callback uses fields we do not know yet. Send me the log |

You should hear your own voice come back (a short delay is normal).

## 5. Known gaps

- The status-callback fields are not documented by Exotel. The code accepts `Status`,
  `CallStatus` or `DialCallStatus` and `Duration`, `DialCallDuration` or `CallDuration`.
  Until a real callback arrives, a call stays `ringing` in the database.
- Trial accounts: Exotel lists inbound as "Limited" on trial and requires KYC for
  production numbers. Check what your plan allows.
- No outbound calling yet (the Exotel Connect API is not wired up).

## 6. The Exotel number does not reach the server

First run `.venv/Scripts/python scripts/check_exotel.py`. It checks DNS/TLS, the Passthru
URL and the WebSocket Basic auth from outside. If it prints "Server side is fine", the fault
is in the Exotel dashboard. Then place a test call and look at the server log (section 4):

| What you see | Cause and fix |
|---|---|
| Nothing at all in the log | Exotel never called us. The ExoPhone is not attached to the flow, the flow was not saved, or the trial account blocks the call (inbound is "Limited" on trial: call from a number you verified in Exotel; KYC is needed for production numbers). |
| `exotel_webhook_bad_token` | The `?token=` in the Passthru applet differs from `EXOTEL_WEBHOOK_SECRET` in the **server's** `.env`. Copy it exactly, no spaces or quotes. |
| `exotel_webhook_secret_not_configured` (HTTP 503) | `EXOTEL_WEBHOOK_SECRET` is missing from the server's `.env`. Add it, then `bash scripts/deploy.sh`. |
| `exotel_call_received` but no `exotel_stream_started` | The Voicebot applet never opened the WebSocket: wrong URL, or Voicebot/streaming is not enabled on your Exotel account (ask Exotel support to enable it). The URL must be `wss://KEY:TOKEN@domain/ws/exotel`. |
| `exotel_ws_rejected` | Key or token in the Voicebot URL differ from the server's `.env`. |
| `exotel_wrong_account` | `EXOTEL_ACCOUNT_SID` on the server differs from the account that sent the call. |

Other things that must match: the flow uses the same domain as `DOMAIN` / `PUBLIC_BASE_URL`,
the DNS A record points at the server's Elastic IP, and ports 80 and 443 are open in the
AWS security group.

## Voice agent (answering the call)

After the Voicebot applet connects, `app/pipeline/voice_pipeline.py` runs the call:
`caller audio -> Silero VAD -> STT -> LLM -> TTS -> caller`. STT, LLM and TTS run on the GPU
server (`model_server/`), reached over the URLs in `.env` (`LLM_BASE_URL`, `STT_BASE_URL`,
`TTS_BASE_URL`, one shared `LLM_API_KEY`). The agent greets first, and the caller can interrupt.

- If the GPU server is down the call is ended at once (log: `gpu_server_unreachable`).
- The agent's name, greeting, system prompt and limits are in `app/core/config.py` and
  `app/pipeline/prompts/`.
- `VOICE_AGENT_ENABLED=false` goes back to the echo test (caller hears their own voice).
- New log lines: `voice_agent_ready` (at start-up), `gpu_server_unreachable`, `agent_failed`.

## Switching back to Twilio

Set `CALL_PROVIDER=twilio` in `.env` and redeploy. See the Twilio notes in `README.md`.
