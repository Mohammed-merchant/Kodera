import base64

import pytest

from app.providers import exotel as ex


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# --- reading call details -------------------------------------------------------------


def test_parse_classic_passthru_fields() -> None:
    call = ex.parse_call(
        {
            "CallSid": "abc123def456",
            "CallFrom": "+919876543210",
            "CallTo": "+914412345678",
            "Direction": "incoming",
        }
    )
    assert (call.from_number, call.to_number, call.direction) == (
        "+919876543210",
        "+914412345678",
        "inbound",
    )


def test_parse_agentstream_passthru_json_fields() -> None:
    call = ex.parse_call(
        {
            "CallSid": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
            "From": "+919876543210",
            "To": "+918041234567",
            "Direction": "inbound",
            "AccountSid": "acct",
        }
    )
    assert call.direction == "inbound"
    assert call.account_sid == "acct"


@pytest.mark.parametrize("sid", ["", "x", "has space 123", "a" * 65, "bad;sid;123"])
def test_bad_call_sid_is_rejected(sid: str) -> None:
    with pytest.raises(ValueError):
        ex.parse_call({"CallSid": sid})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("incoming", "inbound"),
        ("INBOUND", "inbound"),
        ("outgoing", "outbound"),
        ("outbound-dial", "outbound"),
        ("outbound dial", "outbound"),
        ("", ""),
    ],
)
def test_direction_is_normalised(raw: str, expected: str) -> None:
    assert ex.normalize_direction(raw) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"Status": "completed"}, "completed"),
        ({"CallStatus": "No_Answer"}, "no-answer"),
        ({"DialCallStatus": "busy"}, "busy"),
    ],
)
def test_status_is_normalised(data: dict[str, str], expected: str) -> None:
    assert ex.parse_status(data) == expected


@pytest.mark.parametrize("data", [{}, {"Status": ""}, {"Status": "x; DROP TABLE"}, {"Status": "1"}])
def test_bad_status_is_rejected(data: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        ex.parse_status(data)


def test_duration_parsing() -> None:
    assert ex.parse_duration({"DialCallDuration": "42"}) == 42
    assert ex.parse_duration({"Duration": "7"}) == 7
    assert ex.parse_duration({}) is None
    with pytest.raises(ValueError):
        ex.parse_duration({"Duration": "9; DROP"})
    with pytest.raises(ValueError):
        ex.parse_duration({"Duration": "-5"})


# --- basic auth ----------------------------------------------------------------------


def test_basic_auth_accepts_correct_credentials() -> None:
    assert ex.verify_basic_auth(basic("key", "tok"), "key", "tok") is True


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer abc",
        "Basic",
        "Basic !!!notbase64",
        basic("key", "wrong"),
        basic("x", "tok"),
    ],
)
def test_basic_auth_rejects_everything_else(header: str | None) -> None:
    assert ex.verify_basic_auth(header, "key", "tok") is False


def test_basic_auth_rejects_when_credentials_not_configured() -> None:
    # An unset key/token must never let an empty "Basic :" header through.
    assert ex.verify_basic_auth(basic("", ""), "", "") is False


# --- audio ---------------------------------------------------------------------------


def test_decode_audio() -> None:
    assert ex.decode_audio(base64.b64encode(b"\x01\x02").decode()) == b"\x01\x02"
    assert ex.decode_audio("***not base64***") is None


def test_chunks_wait_until_3200_bytes() -> None:
    buf = bytearray(b"\x00" * 3199)
    assert ex.next_chunk(buf) is None
    buf.extend(b"\x00")
    chunk = ex.next_chunk(buf)
    assert chunk is not None and len(chunk) == 3200
    assert len(buf) == 0


def test_chunks_are_multiples_of_320_and_remainder_is_kept() -> None:
    buf = bytearray(b"\x01" * 3500)
    chunk = ex.next_chunk(buf)
    assert chunk is not None
    assert len(chunk) % 320 == 0 and len(chunk) == 3200
    assert len(buf) == 300  # waits for more audio


def test_chunks_never_exceed_100kb() -> None:
    buf = bytearray(b"\x01" * 250_000)
    sizes = []
    while (chunk := ex.next_chunk(buf)) is not None:
        sizes.append(len(chunk))
    assert all(s <= 100_000 and s % 320 == 0 for s in sizes)


def test_buffer_is_capped() -> None:
    buf = bytearray(b"\x01" * 2_000_000)
    ex.next_chunk(buf)
    assert len(buf) <= ex.MAX_BUFFER_BYTES
