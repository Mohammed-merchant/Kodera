# Staff dashboard

`https://YOUR_DOMAIN/dashboard` shows, in one page:

- appointments (upcoming, past, cancelled) and whether the AI phone agent booked them
- recent calls (numbers masked) and which ones ended in a booking
- how the databases perform: Postgres and Redis response time, the booking slot lookup the
  caller waits for, the time of every dashboard query, table sizes, cache hit ratio

It is read-only and refreshes every 15 seconds.

## Turn it on

1. Put a password in the server's `.env`: `DASHBOARD_PASSWORD=...` (empty = dashboard off, `/dashboard` answers 404).
2. `bash scripts/deploy.sh`
3. Open `/dashboard` and sign in. The session lasts 12 hours (`DASHBOARD_SESSION_HOURS`).

## Security

- The page itself holds no data. Data comes from `/dashboard/api/overview`, which needs the
  signed session cookie (HttpOnly, SameSite=Strict, Secure in production).
- Five wrong passwords from one IP block that IP for 15 minutes.
- Patient names and phone numbers are visible to anyone who has the password. Share it only with clinic staff,
  and change it (then redeploy) when someone leaves. Changing it signs everyone out.
- Text from callers (names, reasons) is always shown as plain text, never as HTML.

## Reading the performance panel

| Tile | Good | Look into it when |
|---|---|---|
| Postgres response | under 10 ms | over 50 ms: the database or the disk is slow |
| Booking slot lookup | under 40 ms | over 120 ms: callers wait in silence while booking |
| Redis response | under 5 ms | over 25 ms |
| Postgres cache hits | 95% or more | under 85%: the database needs more memory |

## Where call data lives

While a call is running, its transcript, bookings and timings are kept in **Redis** (fast,
never blocks the caller). When the call ends, everything is written to **Postgres** in one
step (`calls`, `transcript_turns`, `call_events`) and Redis is cleared. If that save fails
(Postgres briefly down, server restart), the data stays in Redis and a background job in the
worker retries every minute, so a finished call is not lost.

Appointments are the exception: they are written to Postgres **immediately** when the agent
books, because the booking must succeed or fail while the caller is still on the line, and
the database guarantees two patients can never get the same slot.

Click any call in the dashboard to see the conversation, how long the caller waited for each
answer (and where that time went: speech-to-text, LLM, voice), and what the agent did.

## Safety guard

A small language model sometimes claims things that did not happen ("I have booked your
appointment" without ever calling the booking tool) or gives medical advice. Every sentence
is checked before it is spoken. A false booking/cancellation claim, or medical advice, is
replaced with a safe sentence, and the dashboard shows what was blocked on that call.
