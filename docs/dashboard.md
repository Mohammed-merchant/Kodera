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
