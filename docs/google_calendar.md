# Google Calendar copy of every booking

Each phone booking is saved in the database first (the dashboard reads it from there), then copied
to the Google Sheet (if set up) and to Google Calendar, in the background. A cancellation deletes
the calendar event. If Google is slow or down, the caller does not wait and nothing is lost: the
app retries 3 times and logs `google_calendar_gave_up` if it still fails.

Off until `GOOGLE_REFRESH_TOKEN` is set.

## One-time setup (about 10 minutes)

1. **Google Cloud Console**, in the project of your OAuth client:
   - *APIs & Services > Library*: enable **Google Calendar API**.
   - *APIs & Services > Credentials*: open the OAuth client and add this
     **Authorized redirect URI**: `http://localhost:8765/` (then Save).
   - *OAuth consent screen*: click **Publish app** ("In production"). In "Testing" mode the
     refresh token stops working after 7 days. (Or add your Google account as a test user, for a
     short test only.)
2. **On your laptop**, run (the path is your downloaded client JSON):

   ```powershell
   python scripts\google_calendar_auth.py "C:\Users\moham\Downloads\client_secret_....json"
   ```

   Sign in with the Google account whose calendar should get the appointments and click
   **Allow** (on an unverified app: *Advanced > Go to ... (unsafe)*, it is your own app).
   The script prints four `GOOGLE_...` lines.
3. **Put the four lines in the app's `.env`** on the VPS and restart the app. From your laptop,
   after adding them to the local `.env`:

   ```powershell
   .\scripts\push_env.ps1 -Key C:\Users\moham\Downloads\ssh.pem -Deploy
   ```

## Check

Book a test appointment by phone. Within a few seconds it is in the calendar as
"Appointment: <name>" at the booked time (one slot long), with the phone number and reason in the
description. In the app logs, `google_calendar_refused` or `google_calendar_auth_failed` explain a
problem; for an auth failure, run step 2 again.

Never commit the client JSON or the `.env` lines: they give access to the calendar.
