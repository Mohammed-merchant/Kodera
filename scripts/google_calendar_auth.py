"""One-time setup for the Google Calendar copy: get a refresh token for the clinic's calendar.

Run on a computer with a browser (standard Python only, nothing to install):

    python scripts/google_calendar_auth.py "C:\\path\\to\\client_secret_....json"

1. Your browser opens Google. Sign in with the clinic's Google account and click Allow.
2. The script prints four lines. Put them in the app's .env on the VPS and restart the app.

Before the first run, in Google Cloud Console (same project as the client):
  - APIs & Services > Library: enable "Google Calendar API".
  - APIs & Services > Credentials > your OAuth client > Authorized redirect URIs:
    add  http://localhost:8765/
  - OAuth consent screen: add your Google account as a test user, or publish the app
    ("In production"). A test-mode app's refresh token stops working after 7 days.
See docs/google_calendar.md.
"""

import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

PORT = 8765
REDIRECT = f"http://localhost:{PORT}/"
SCOPE = "https://www.googleapis.com/auth/calendar.events"  # create and delete events only


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open(sys.argv[1], encoding="utf-8") as f:
        client = json.load(f)
    client = client.get("web") or client.get("installed") or {}
    client_id, client_secret = client.get("client_id"), client.get("client_secret")
    if not client_id or not client_secret:
        sys.exit("That file has no client_id/client_secret. Download the OAuth client JSON again.")

    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
    })  # fmt: skip
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" not in query and "error" not in query:
                self.send_response(404)
                self.end_headers()
                return
            got.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")

        def log_message(self, *args: object) -> None:
            pass

    print("Opening your browser. If it does not open, paste this address into it:\n\n" + url + "\n")
    webbrowser.open(url)
    with http.server.HTTPServer(("localhost", PORT), Handler) as server:
        while not got:
            server.handle_request()

    if "error" in got:
        sys.exit(
            f"Google said: {got['error']}. If it is redirect_uri_mismatch, add {REDIRECT} "
            "to the client's Authorized redirect URIs in Google Cloud Console."
        )
    if got.get("state") != state:
        sys.exit("The answer did not match this request (state mismatch). Run the script again.")

    data = urllib.parse.urlencode({
        "code": got["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
    }).encode()  # fmt: skip
    tokens = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data))
    refresh = tokens.get("refresh_token")
    if not refresh:
        sys.exit(
            "Google sent no refresh token. Remove the app's access at "
            "https://myaccount.google.com/permissions and run the script again."
        )

    # Check that the token can really see the calendar.
    req = urllib.request.Request(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events?maxResults=1",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    urllib.request.urlopen(req).read()
    print(
        "Calendar access works.\n\nPut these four lines in the app's .env on the VPS "
        "(keep them secret), then restart the app:\n"
    )
    print(f"GOOGLE_CLIENT_ID={client_id}")
    print(f"GOOGLE_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={refresh}")
    print("GOOGLE_CALENDAR_ID=primary")


if __name__ == "__main__":
    main()
