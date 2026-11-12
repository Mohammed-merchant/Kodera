# Cal.diy for the clinic's appointments

[Cal.diy](https://github.com/calcom/cal.diy) is open-source scheduling software. With it:

- the **doctor and staff** manage availability and see every booking in Cal.diy's own web app
  (calendar sync, confirmation emails, reschedule and cancel links for patients),
- the **phone agent** reads free slots from Cal.diy and books and cancels through its API, so a
  call and the web booking page can never double-book the same slot,
- the Kodera **dashboard** still shows every booking, wherever it was made.

```
 caller ── Exotel ── Kodera app ──(API v2, key)──► Cal.diy API ──┐
                         │                                       ├─► Postgres "calcom"
                         │◄──(webhook, signed)── Cal.diy web ────┘
                         ▼
        Postgres "appointments" = a copy of every Cal.diy booking, for the dashboard
```

Everything about Cal.diy lives in this folder; the app side is `app/integrations/calcom.py`,
`app/business/booking_backend.py` and `app/business/calcom_sync.py`. The agent works without
Cal.diy (`BOOKING_BACKEND=local`, the default). Switching is one line in `.env`.

## Server size: read this first

Cal.diy is a large Next.js application and it **does not fit on a 2 GB server** next to the call
agent.

| | |
|---|---|
| Building the images | needs ~8 GB, so we build them on GitHub's machines (step 3), never on the server |
| Running web + API | about 1.5 to 2 GB of RAM on top of the Kodera stack (~0.5 GB) |
| Server to use | **4 GB minimum** (AWS `t3.medium`), 8 GB (`t3.large`) is comfortable |
| Disk | about 5 GB free for the two images |

`deploy.sh up` refuses to start on a server with less than 3.5 GB of RAM (override with
`FORCE=1`, only for a short test: low memory can starve the call agent).
To resize on AWS: stop the instance, Actions > Instance settings > Change instance type, start it.
The Elastic IP and all data stay.

## Setup, in order

**1. Resize the server** (above), then check `free -m` shows 3.5 GB or more.

**2. DNS.** Create an A record `cal.YOUR_DOMAIN` pointing at the server's Elastic IP.

**3. Build the images (once, and again to upgrade).** GitHub > the Kodera repository > Actions >
"Build Cal.diy images" > Run workflow. Wait 30 to 60 minutes. It stores
`ghcr.io/YOUR_GITHUB_USER/calcom-diy-web` and `...-api`.
The packages are private by default. Either make them public (GitHub > your profile > Packages >
the package > Settings > Change visibility; Cal.diy is MIT licensed and the images hold no
secrets), or log in on the server: `docker login ghcr.io -u YOUR_USER` with a token that has
`read:packages`.

**4. Install on the server.**
```bash
cd ~/kodera && git pull
bash calcom/deploy.sh init          # creates calcom/.env with fresh secrets
nano calcom/.env                    # CAL_DOMAIN, CAL_PUBLIC_URL, the two image names
bash calcom/deploy.sh up            # database, HTTPS, pull, start (first start: a few minutes)
bash calcom/deploy.sh status        # both containers "healthy", memory looks sane
```

**5. Set up Cal.diy in the browser** (`https://cal.YOUR_DOMAIN`):
1. Create the admin account (the first account becomes the admin).
2. **Availability** > the clinic's hours (for example Mon-Sat 10:00-13:00 and 17:00-20:00) and
   the time zone (Asia/Kolkata).
3. **Event types** > New > "Consultation", **15 minutes**. Note its number: it is in the address
   bar (`/event-types/7` means id 7). Under Advanced > Booking questions set **Email optional**
   and **Phone number required**, so the phone agent can book with the caller's number alone.
   Under Availability pick the schedule from step 2. Add a "Notes" question if it is missing.
4. **Settings > Developer > API keys** > create a key (`cal_...`). Copy it now.
5. **Settings > Developer > Webhooks** > New: subscriber URL
   `https://YOUR_DOMAIN/webhooks/calcom`, triggers *Booking created*, *Booking rescheduled*,
   *Booking cancelled*, and a secret of your choice (`openssl rand -hex 24`).

**6. Tell Kodera about it.** Add to the project `.env` on your PC:
```
BOOKING_BACKEND=calcom
CAL_API_URL=http://calcom-api:80
CAL_API_KEY=cal_...
CAL_EVENT_TYPE_ID=7
CAL_WEBHOOK_SECRET=the secret from step 5.5
CAL_PUBLIC_URL=https://cal.YOUR_DOMAIN
CLINIC_TIMEZONE_NAME=Asia/Kolkata
```
Before pushing, test from your PC (set `CAL_API_URL=https://cal.YOUR_DOMAIN/api` just for this):
```
.venv/Scripts/python calcom/check_cal.py --book-test
```
It lists free slots, books the first one for a test patient and cancels it again. Then push the
`.env` and deploy: `.\scripts\push_env.ps1 -Key C:\path\key.pem -Deploy`.

**7. Try it.** Call the number and book. The booking shows up in Cal.diy (Bookings), in the
Kodera dashboard (chip "Cal.diy") and the doctor's calendar. Make one in Cal.diy's own booking
page too: it appears on the dashboard within seconds through the webhook.

## Day to day

| Task | Command (on the server) |
|---|---|
| Status, memory | `bash calcom/deploy.sh status` |
| Logs | `bash calcom/deploy.sh logs web` or `... api` |
| Upgrade Cal.diy | run the GitHub Action again (new commit or tag), then `bash calcom/deploy.sh update` |
| Backup the database | `bash calcom/deploy.sh backup` (keeps the newest 7 in `calcom/backups/`) |
| Stop it | `bash calcom/deploy.sh down` (data is kept) |
| Go back to the built-in booking | set `BOOKING_BACKEND=local`, push the `.env`, deploy |

Back up before every upgrade. The Cal.diy database is `calcom` in the same Postgres as Kodera's;
include it in whatever backup you take of the server.

## Good to know

- **Free slots and booking are always live from Cal.diy**, so staff changes (blocking a day,
  a holiday, a personal calendar event) apply to the phone agent immediately.
- **If Cal.diy is down**, the agent says the booking system is not working and promises nothing
  (the dashboard's slot-lookup tile turns red). Calls themselves keep working.
- **Phone numbers** are sent to Cal.diy as `+91` plus the 10 digits (`CLINIC_PHONE_COUNTRY_CODE`).
- **Old bookings** made before the switch stay in Kodera's table; the agent can still cancel them.
- **Pinned version**: the workflow builds a fixed Cal.diy commit so upgrades are deliberate.
  If a new Cal.diy changes its API, run `check_cal.py --book-test` before switching the agent.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `deploy.sh up` says not enough RAM | resize the server (step 1) |
| `pull access denied` | make the GHCR packages public or `docker login ghcr.io` (step 3) |
| HTTPS certificate error | the DNS record from step 2 is missing or not propagated yet |
| `calcom-web` restarts or is "unhealthy" | `deploy.sh logs web`: database password mismatch, or out of memory (raise `CAL_WEB_MEMORY`) |
| `check_cal.py`: 401 | wrong or expired API key |
| `check_cal.py`: no free slots | no availability schedule on the event type, or it is hidden |
| `check_cal.py --book-test`: 400 | phone-only booking is off (step 5.3, booking questions) |
| Bookings from Cal.diy do not reach the dashboard | webhook URL, secret or triggers (step 5.5); look for `calcom_webhook` in the app log |
