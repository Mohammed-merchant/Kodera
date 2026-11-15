# Google Sheet copy of bookings

Every booking made on a phone call is saved in Postgres (as before) and **also** copied to a
Google Sheet by a small Apps Script web app. No Google Cloud project is needed.
Cancellations mark the row `Cancelled`. Off when `GOOGLE_SHEET_URL` is empty.

1. Create a new Google Sheet (sheets.new), name it e.g. "Kodera Bookings".
2. Extensions > Apps Script. Delete everything, paste [google_sheet_apps_script.gs](google_sheet_apps_script.gs).
3. Set `SECRET` in the script to the same value as `GOOGLE_SHEET_SECRET` in `.env`. Save.
4. Choose `setup` in the function list > Run > allow access. A "Bookings" tab appears.
5. Deploy > New deployment > type **Web app**: Execute as **Me**, Who has access **Anyone**.
   Deploy, copy the URL ending in `/exec`.
6. Put it in `.env` on the VPS: `GOOGLE_SHEET_URL=https://script.google.com/macros/s/.../exec`
   (and the same `GOOGLE_SHEET_SECRET`), then `bash scripts/deploy.sh`.

After editing the script later: Deploy > Manage deployments > edit > Version: **New version**,
so the same URL runs the new code.
