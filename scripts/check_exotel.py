"""Check that the server side of the Exotel setup works, from any machine.

    .venv/Scripts/python scripts/check_exotel.py [https://your-domain]

Reads EXOTEL_API_KEY / EXOTEL_API_TOKEN / PUBLIC_BASE_URL from .env. It saves no call data:
the Passthru check sends no token (expects 403) and the WebSocket check only opens and
closes the connection. If everything here is OK but real calls fail, the problem is in the
Exotel dashboard (call flow, applet URLs, number attachment, account/trial limits).
"""

import asyncio
import base64
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import websockets


def read_env() -> dict[str, str]:
    path = Path(__file__).resolve().parents[1] / ".env"
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def http_status(url: str, method: str = "GET", data: bytes | None = None) -> int:
    req = urllib.request.Request(url, data=data, method=method)
    try:
        return urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()).status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        print(f"      cannot connect: {type(exc).__name__}: {exc}")
        return 0


async def ws_check(url: str, auth: str | None) -> str:
    headers = {"Authorization": auth} if auth else {}
    try:
        async with websockets.connect(url, additional_headers=headers, open_timeout=10):
            return "accepted"
    except websockets.exceptions.InvalidStatus as exc:
        return f"rejected (HTTP {exc.response.status_code})"
    except Exception as exc:
        return f"failed ({type(exc).__name__}: {exc})"


def report(ok: bool, text: str, hint: str = "") -> bool:
    print(f"[{'OK' if ok else 'FAIL'}] {text}")
    if not ok and hint:
        print(f"       -> {hint}")
    return ok


def main() -> int:
    env = read_env()
    base = (sys.argv[1] if len(sys.argv) > 1 else env.get("PUBLIC_BASE_URL", "")).rstrip("/")
    if not base.startswith("https://"):
        print("Give the public HTTPS URL as an argument or set PUBLIC_BASE_URL in .env")
        return 2
    ok = True

    s = http_status(f"{base}/health")
    ok &= report(s == 200, f"{base}/health -> {s}",
                 "DNS, firewall (80/443), Caddy or the app container is down")

    s = http_status(f"{base}/webhooks/exotel/passthru", "POST", b"CallSid=probe")
    meaning = {403: "app answers, provider is exotel, secret is set (token correctly required)",
               404: "CALL_PROVIDER on the server is not 'exotel'",
               503: "EXOTEL_WEBHOOK_SECRET is missing in the server's .env"}
    ok &= report(s == 403, f"Passthru without token -> {s}: {meaning.get(s, 'unexpected')}",
                 meaning.get(s, "check Caddy/app logs"))

    ws_url = "wss://" + base.removeprefix("https://") + "/ws/exotel"
    key, token = env.get("EXOTEL_API_KEY", ""), env.get("EXOTEL_API_TOKEN", "")
    r = asyncio.run(ws_check(ws_url, None))
    ok &= report(r.startswith("rejected"), f"WebSocket without auth -> {r} (must be rejected)",
                 "the WebSocket is open to anyone")
    if key and token:
        auth = "Basic " + base64.b64encode(f"{key}:{token}".encode()).decode()
        r = asyncio.run(ws_check(ws_url, auth))
        ok &= report(r == "accepted", f"WebSocket with Basic auth -> {r}",
                     "EXOTEL_API_KEY/TOKEN on the server differ from this .env, "
                     "or CALL_PROVIDER is not exotel")
        odd = sorted(set(re.findall(r"[^A-Za-z0-9._~-]", key + token)))
        ok &= report(not odd, "key/token are safe inside a wss:// URL",
                     f"characters {odd} must be percent-encoded in the Voicebot URL")
    else:
        ok &= report(False, "EXOTEL_API_KEY / EXOTEL_API_TOKEN not found in local .env")

    print("\nServer side is fine." if ok else "\nFix the FAIL lines above first.")
    if ok:
        print("If calls still fail, the problem is in the Exotel dashboard: see docs/exotel.md "
              "section 6.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
