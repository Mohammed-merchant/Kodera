"""Exotel provider helpers: read call details, status values, WebSocket audio.

What is documented (developer.exotel.com, checked Nov 2026):
- Passthru applet: classic form/query fields CallSid, CallFrom, CallTo, Direction
  (incoming/outgoing), CurrentTime. AgentStream Passthru: JSON with CallSid, StreamSid,
  StreamUrl, AccountSid, EventType, From, To, Direction (inbound/outbound).
- Voicebot WebSocket: JSON events connected, start, media, dtmf, mark, stop with snake_case
  fields. Audio is raw 16-bit little-endian mono PCM, 8 kHz by default, base64 encoded.
  Chunks we send must be a multiple of 320 bytes, between 3200 bytes and 100 KB.

What is NOT documented: a webhook signature and the status-callback fields. Status parsing
below therefore accepts several likely field names. Confirm them on the first live call.
"""

import base64
import binascii
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass

_CALL_SID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_STATUS = re.compile(r"^[a-z][a-z-]{1,19}$")

MIN_CHUNK_BYTES = 3200
CHUNK_MULTIPLE = 320
MAX_CHUNK_BYTES = 100_000
MAX_BUFFER_BYTES = 400_000  # drop audio if we fall far behind, never grow without limit

_DIRECTIONS = {
    "incoming": "inbound",
    "inbound": "inbound",
    "outgoing": "outbound",
    "outbound": "outbound",
    "outbound-api": "outbound",
    "outbound-dial": "outbound",
    "outbound dial": "outbound",
}
_DURATION_KEYS = ("Duration", "DialCallDuration", "ConversationDuration", "CallDuration")
_STATUS_KEYS = ("Status", "CallStatus", "DialCallStatus")


@dataclass(frozen=True)
class ExotelCall:
    call_sid: str
    from_number: str
    to_number: str
    direction: str
    account_sid: str


def is_valid_call_sid(value: str) -> bool:
    return bool(_CALL_SID.match(value))


def normalize_direction(raw: str) -> str:
    return _DIRECTIONS.get(raw.strip().lower(), raw.strip().lower())


def parse_call(data: Mapping[str, str]) -> ExotelCall:
    """Read a Passthru request (classic form fields or AgentStream JSON)."""
    call_sid = data.get("CallSid", "")
    if not is_valid_call_sid(call_sid):
        raise ValueError("invalid CallSid")
    return ExotelCall(
        call_sid=call_sid,
        from_number=data.get("From") or data.get("CallFrom", ""),
        to_number=data.get("To") or data.get("CallTo", ""),
        direction=normalize_direction(data.get("Direction", "")),
        account_sid=data.get("AccountSid", ""),
    )


def parse_status(data: Mapping[str, str]) -> str:
    """Return a clean lowercase status such as 'completed' or 'no-answer'."""
    raw = next((data[k] for k in _STATUS_KEYS if data.get(k)), "")
    status = raw.strip().lower().replace("_", "-").replace(" ", "-")
    if not _STATUS.match(status):
        raise ValueError("invalid status")
    return status


def parse_duration(data: Mapping[str, str]) -> int | None:
    raw = next((data[k] for k in _DURATION_KEYS if data.get(k)), "")
    if not raw:
        return None
    if not raw.isdigit() or len(raw) > 7:
        raise ValueError("invalid duration")
    return int(raw)


def parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    """Decode 'Authorization: Basic base64(key:token)'."""
    if not header:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    key, sep, token = decoded.partition(":")
    return (key, token) if sep else None


def verify_basic_auth(header: str | None, api_key: str, api_token: str) -> bool:
    """Exotel opens the WebSocket with Basic auth (API key and token) when the URL has them."""
    if not api_key or not api_token:
        return False
    parsed = parse_basic_auth(header)
    if parsed is None:
        return False
    key_ok = hmac.compare_digest(parsed[0].encode(), api_key.encode())
    token_ok = hmac.compare_digest(parsed[1].encode(), api_token.encode())
    return key_ok and token_ok


def decode_audio(payload: str) -> bytes | None:
    """base64 PCM payload -> bytes, or None if it is not valid base64."""
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None


def next_chunk(buffer: bytearray) -> bytes | None:
    """Take the next audio chunk we may send back: >= 3200 bytes, multiple of 320, <= 100 KB."""
    if len(buffer) > MAX_BUFFER_BYTES:
        del buffer[: len(buffer) - MAX_BUFFER_BYTES]
    if len(buffer) < MIN_CHUNK_BYTES:
        return None
    size = min(len(buffer), MAX_CHUNK_BYTES) // CHUNK_MULTIPLE * CHUNK_MULTIPLE
    chunk = bytes(buffer[:size])
    del buffer[:size]
    return chunk
