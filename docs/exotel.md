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

## Switching back to Twilio

Set `CALL_PROVIDER=twilio` in `.env` and redeploy. See the Twilio notes in `README.md`.
