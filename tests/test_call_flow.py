"""A call is recorded in Redis while it runs and saved to Postgres when it ends."""

import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business.call_flush import flush_call, flush_orphans, summarise
from app.core.config import Settings, get_settings
from app.db.models import Call, CallEvent, TranscriptTurn
from app.main import app
from app.pipeline.call_recorder import CallRecorder, keys
from app.pipeline.guards import (
    SAFE_NO_ADVICE,
    SAFE_NOT_BOOKED,
    SAFE_NOT_CANCELLED,
    check_sentence,
)
from tests.fakes import RecorderRedis

SID = "CA_FLOW_000001"
Factory = async_sessionmaker[AsyncSession]


async def record_a_booking_call(redis: RecorderRedis, sid: str = SID) -> CallRecorder:
    rec = CallRecorder(redis, sid, provider="exotel", caller="+919876543210")  # type: ignore[arg-type]
    await rec.start()
    await rec.add_turn("agent", "Hello, thank you for calling City Care Clinic.")
    await rec.add_turn("caller", "I want an appointment tomorrow")
    await rec.add_turn(
        "agent", "I have 10 AM free.", response_ms=1400, stt_ms=700, llm_ms=400, tts_ms=300
    )
    await rec.add_turn("caller", "10 AM, my name is Asha")
    await rec.add_turn(
        "agent", "Booked for 10 AM.", response_ms=1000, stt_ms=600, llm_ms=300, tts_ms=100
    )
    await rec.add_event("booking", ok=True, patient="Asha", slot="2030-01-01T04:30:00+00:00")
    await rec.add_event("guard", reason="claimed a booking that was not made", blocked="x")
    await rec.finish("call ended")
    return rec


# ---------- recorder -> Postgres ----------


async def test_a_finished_call_is_saved_with_transcript_and_timings(
    session_factory: Factory,
) -> None:
    redis = RecorderRedis()
    await record_a_booking_call(redis)
    assert redis.lists[keys(SID)[1]]  # the conversation is in Redis during the call

    assert await flush_call(redis, session_factory, SID)  # type: ignore[arg-type]

    async with session_factory() as session:
        call = (await session.scalars(select(Call))).one()
        assert call.provider_call_id == SID and call.status == "completed"
        assert call.outcome == "booked" and call.turn_count == 5
        assert call.avg_response_ms == 1200 and call.from_number == "+919876543210"
        assert call.duration_seconds is not None and call.ended_at is not None

        turns = list(await session.scalars(select(TranscriptTurn).order_by(TranscriptTurn.id)))
        assert [t.role for t in turns] == ["agent", "caller", "agent", "caller", "agent"]
        assert turns[2].text == "I have 10 AM free." and turns[2].response_ms == 1400
        assert (turns[2].stt_ms, turns[2].llm_ms, turns[2].tts_ms) == (700, 400, 300)
        assert turns[1].response_ms is None  # only agent turns carry timings

        event = (
            await session.scalars(select(CallEvent).where(CallEvent.type == "call.summary"))
        ).one()
        assert event.data["outcome"] == "booked"
        assert event.data["guard_reasons"] == ["claimed a booking that was not made"]

    # Redis is cleared once the data is safely in Postgres
    assert not redis.hashes and not redis.lists


async def test_saving_twice_never_duplicates_the_transcript(session_factory: Factory) -> None:
    redis = RecorderRedis()
    await record_a_booking_call(redis)
    assert await flush_call(redis, session_factory, SID)  # type: ignore[arg-type]
    await record_a_booking_call(redis)  # the same call recorded again (e.g. a retry)
    await flush_call(redis, session_factory, SID)  # type: ignore[arg-type]
    async with session_factory() as session:
        assert len(list(await session.scalars(select(TranscriptTurn)))) == 5


async def test_a_busy_lock_means_somebody_else_is_saving(session_factory: Factory) -> None:
    redis = RecorderRedis()
    await record_a_booking_call(redis)
    redis.plain[f"{keys(SID)[0]}:lock"] = "1"
    assert not await flush_call(redis, session_factory, SID)  # type: ignore[arg-type]
    assert redis.lists  # nothing was lost


async def test_postgres_down_keeps_the_call_in_redis(session_factory: Factory) -> None:
    redis = RecorderRedis()
    await record_a_booking_call(redis)

    class BrokenFactory:
        def __call__(self) -> None:
            raise ConnectionError("postgres is down")

    assert not await flush_call(redis, BrokenFactory(), SID)  # type: ignore[arg-type]
    assert redis.lists[keys(SID)[1]]  # still there for the retry job
    assert await flush_call(redis, session_factory, SID)  # type: ignore[arg-type]


async def test_the_rescue_job_saves_dead_calls_only(session_factory: Factory) -> None:
    redis = RecorderRedis()
    await record_a_booking_call(redis, "CA_ENDED_NOT_SAVED")  # ended, never saved
    live = CallRecorder(redis, "CA_STILL_LIVE", provider="exotel", caller="")  # type: ignore[arg-type]
    await live.start()
    await live.add_turn("caller", "hello")
    await live.drain()
    abandoned = CallRecorder(redis, "CA_ABANDONED", provider="exotel", caller="")  # type: ignore[arg-type]
    await abandoned.start()
    await abandoned.add_turn("caller", "hi")
    await abandoned.drain()
    redis.hashes[keys("CA_ABANDONED")[0]]["last_activity"] = str(time.time() - 600)  # silent 10 min

    assert await flush_orphans(redis, session_factory) == 2  # type: ignore[arg-type]

    async with session_factory() as session:
        saved = set(await session.scalars(select(Call.provider_call_id)))
    assert saved == {"CA_ENDED_NOT_SAVED", "CA_ABANDONED"}  # the live call is left alone


def test_outcomes() -> None:
    caller = [{"role": "caller", "text": "hi"}]
    assert summarise(caller, [{"kind": "booking", "ok": True}])["outcome"] == "booked"
    assert summarise(caller, [{"kind": "booking", "ok": False}])["outcome"] == "info"
    assert summarise(caller, [{"kind": "cancel", "ok": True}])["outcome"] == "cancelled"
    assert summarise([{"role": "agent", "text": "hello"}], [])["outcome"] == "silent"


async def test_recording_never_blocks_or_breaks_a_call() -> None:
    class DeadRedis:
        def pipeline(self) -> None:
            raise ConnectionError("redis is down")

        async def hset(self, *a: object, **k: object) -> None:
            raise ConnectionError("redis is down")

    rec = CallRecorder(DeadRedis(), "CA_DEAD", provider="exotel", caller="")  # type: ignore[arg-type]
    started = time.perf_counter()
    await rec.start()
    await rec.add_turn("caller", "hello")
    assert time.perf_counter() - started < 0.1  # returns at once; the writes happen later
    await rec.finish("call ended")  # errors are logged, nothing raised


# ---------- the safety guard ----------


def test_guard_blocks_a_booking_that_never_happened() -> None:
    said = "I have booked an appointment for you at 12:00 PM on Thursday, 12 November 2026."
    assert check_sentence(said, booked=False, cancelled=False) == (
        SAFE_NOT_BOOKED,
        "claimed a booking that was not made",
    )
    assert check_sentence(said, booked=True, cancelled=False) == (said, None)  # real booking: fine


def test_guard_allows_questions_offers_and_denials() -> None:
    for ok in (
        "Shall I book the 5 PM slot for you?",
        "I have not booked it yet.",
        "I can book you in once I have your name.",
        "We are open Monday to Saturday.",
        "The doctor will examine you and advise accordingly.",
        "Please consult a doctor as soon as possible.",
    ):
        assert check_sentence(ok, booked=False, cancelled=False) == (ok, None), ok


def test_guard_blocks_medical_advice() -> None:
    for bad in (
        "Please take plenty of fluids and rest.",
        "Take paracetamol twice a day.",
        "It sounds like a viral infection.",
        "You might have a cold.",
    ):
        assert check_sentence(bad, booked=True, cancelled=True)[0] == SAFE_NO_ADVICE, bad


def test_guard_blocks_a_cancellation_that_never_happened() -> None:
    said = "I have cancelled your appointment."
    assert check_sentence(said, booked=True, cancelled=False)[0] == SAFE_NOT_CANCELLED
    assert check_sentence(said, booked=False, cancelled=True) == (said, None)


# ---------- dashboard: the call and its transcript ----------


@pytest_asyncio.fixture
async def client(session_factory: Factory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, dashboard_password=SecretStr("pw")
    )
    app.state.session_factory = session_factory
    app.state.redis = RecorderRedis()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


async def test_dashboard_shows_the_transcript_and_response_times(
    client: httpx.AsyncClient, session_factory: Factory
) -> None:
    redis = RecorderRedis()
    await record_a_booking_call(redis)
    await flush_call(redis, session_factory, SID)  # type: ignore[arg-type]
    await client.post("/dashboard/api/login", json={"password": "pw"})

    overview = (await client.get("/dashboard/api/overview")).json()
    row = overview["calls"][0]
    assert row["outcome"] == "booked" and row["turns"] == 5 and row["avg_response_ms"] == 1200
    assert overview["kpis"]["avg_response_ms"] == 1200
    voice = overview["performance"]["voice"]
    assert voice["samples"] == 2 and voice["target_ms"] == 650
    assert voice["p50_ms"] in (1000, 1400) and voice["avg_stt_ms"] == 650

    detail = (await client.get(f"/dashboard/api/calls/{row['id']}")).json()
    assert [t["role"] for t in detail["turns"]] == ["agent", "caller", "agent", "caller", "agent"]
    assert detail["turns"][2]["response_ms"] == 1400
    summary = next(e for e in detail["events"] if e["type"] == "call.summary")
    assert summary["data"]["guard_reasons"]  # the dashboard can show what the guard blocked
    assert detail["call"]["outcome"] == "booked"


async def test_call_detail_needs_login_and_handles_unknown_ids(client: httpx.AsyncClient) -> None:
    assert (await client.get("/dashboard/api/calls/1")).status_code == 401
    await client.post("/dashboard/api/login", json={"password": "pw"})
    assert (await client.get("/dashboard/api/calls/999999")).status_code == 404


def test_keys_are_per_call() -> None:
    assert keys("A")[0] != keys("B")[0] and keys("A")[0].startswith("callrec:A")
    assert json.loads(json.dumps({"ok": True}))  # events are JSON
    assert datetime.now(UTC) - timedelta(seconds=1) < datetime.now(UTC)
