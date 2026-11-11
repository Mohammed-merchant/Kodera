"""Numbers for the clinic dashboard: bookings, calls and how fast the databases answer.

Everything here is read-only. Each query is timed, so the dashboard can show how the
database performs on the same queries the booking agent depends on.
"""

import statistics
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business import appointments as appts
from app.core.config import Settings
from app.core.privacy import mask_number
from app.db.models import Appointment, Call

UPCOMING_LIMIT = 100
PAST_LIMIT = 50
CANCELLED_LIMIT = 30
CALLS_LIMIT = 25
PING_RUNS = 5


class Timings:
    """Collects how long each named step took, in milliseconds."""

    def __init__(self) -> None:
        self.ms: dict[str, float] = {}

    @asynccontextmanager
    async def measure(self, name: str) -> AsyncIterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.ms[name] = round((time.perf_counter() - start) * 1000, 1)


def _aware(dt: datetime) -> datetime:
    return appts.as_aware(dt)


def _labels(dt: datetime, sched: appts.Schedule) -> dict[str, str]:
    local = _aware(dt).astimezone(sched.tz)
    hour12 = local.hour % 12 or 12
    clock = f"{hour12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    return {
        "iso": _aware(dt).isoformat(),
        "date": f"{local:%a} {local.day} {local:%b}",
        "time": clock,
    }


def _call_status(call: Call, now: datetime) -> str:
    """A call still "ringing" long after it started never got its final status from the carrier."""
    if call.status == "ringing" and now - _aware(call.started_at) > timedelta(minutes=15):
        return "no status"
    return call.status


def _appointment_row(a: Appointment, sched: appts.Schedule) -> dict[str, Any]:
    return {
        "id": a.id,
        "patient": a.patient_name,
        "phone": a.phone,
        "reason": a.reason or "",
        "status": a.status,
        "by_phone_agent": bool(a.booked_via_call),
        **_labels(a.slot_start, sched),
    }


async def _ping_db(factory: async_sessionmaker[AsyncSession]) -> float:
    """Median time of a trivial query, which is mostly connection + network cost."""
    runs = []
    for _ in range(PING_RUNS):
        start = time.perf_counter()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        runs.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(runs), 1)


async def _postgres_details(session: AsyncSession) -> dict[str, Any]:
    """Postgres-only numbers (the tests run on SQLite, which has none of this)."""
    version = (await session.execute(text("SHOW server_version"))).scalar_one()
    size = (await session.execute(text("SELECT pg_database_size(current_database())"))).scalar_one()
    connections = (
        await session.execute(
            text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        )
    ).scalar_one()
    hit = (
        await session.execute(
            text(
                "SELECT blks_hit, blks_read FROM pg_stat_database "
                "WHERE datname = current_database()"
            )
        )
    ).one()
    total = (hit.blks_hit or 0) + (hit.blks_read or 0)
    tables = (
        await session.execute(
            text(
                "SELECT relname, n_live_tup, pg_total_relation_size(relid) AS bytes "
                "FROM pg_stat_user_tables ORDER BY relname"
            )
        )
    ).all()
    return {
        "version": str(version).split(" ")[0],
        "size_bytes": int(size),
        "connections": int(connections),
        "cache_hit_percent": round(100 * (hit.blks_hit or 0) / total, 1) if total else None,
        "tables": [
            {"name": t.relname, "rows": int(t.n_live_tup or 0), "bytes": int(t.bytes or 0)}
            for t in tables
        ],
    }


async def _redis_details(redis: Redis) -> dict[str, Any]:
    start = time.perf_counter()
    await redis.ping()
    ping_ms = round((time.perf_counter() - start) * 1000, 1)
    memory = await redis.info("memory")
    server = await redis.info("server")
    return {
        "ping_ms": ping_ms,
        "version": server.get("redis_version"),
        "memory": memory.get("used_memory_human"),
        "keys": await redis.dbsize(),
    }


def _dialect(factory: async_sessionmaker[AsyncSession]) -> str:
    bind = factory.kw.get("bind")
    return str(getattr(getattr(bind, "dialect", None), "name", "unknown"))


async def build_overview(
    factory: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings
) -> dict[str, Any]:
    dialect = _dialect(factory)
    sched = appts.schedule_from_settings(settings)
    now = datetime.now(UTC)
    local_today = sched.now().date()
    day_start = datetime.combine(local_today, dtime.min, tzinfo=sched.tz).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    week_ago = now - timedelta(days=7)
    timings = Timings()
    result: dict[str, Any] = {"generated_at": now.isoformat(), "clinic": settings.clinic_name}

    async with factory() as session:

        async def count(name: str, stmt: Any) -> int:
            async with timings.measure(name):
                return int((await session.execute(stmt)).scalar_one())

        booked = Appointment.status == "booked"
        kpis: dict[str, int | None] = {
            "appointments_today": await count(
                "count appointments today",
                select(func.count())
                .select_from(Appointment)
                .where(
                    booked, Appointment.slot_start >= day_start, Appointment.slot_start < day_end
                ),
            ),
            "appointments_upcoming": await count(
                "count upcoming appointments",
                select(func.count())
                .select_from(Appointment)
                .where(booked, Appointment.slot_start >= now),
            ),
            "calls_today": await count(
                "count calls today",
                select(func.count()).select_from(Call).where(Call.started_at >= day_start),
            ),
            "calls_total": await count("count all calls", select(func.count()).select_from(Call)),
            "calls_7d": await count(
                "count calls 7d",
                select(func.count()).select_from(Call).where(Call.started_at >= week_ago),
            ),
            "bookings_7d": await count(
                "count bookings 7d",
                select(func.count())
                .select_from(Appointment)
                .where(
                    Appointment.created_at >= week_ago, Appointment.booked_via_call.is_not(None)
                ),
            ),
        }
        async with timings.measure("average call length"):
            avg = (
                await session.execute(
                    select(func.avg(Call.duration_seconds)).where(
                        Call.duration_seconds.is_not(None)
                    )
                )
            ).scalar_one()
        kpis["avg_call_seconds"] = round(float(avg)) if avg is not None else None
        calls_7d, bookings_7d = kpis["calls_7d"] or 0, kpis["bookings_7d"] or 0
        kpis["booking_rate_percent"] = round(100 * bookings_7d / calls_7d) if calls_7d else None
        result["kpis"] = kpis

        async with timings.measure("list upcoming appointments"):
            upcoming = (
                await session.scalars(
                    select(Appointment)
                    .where(booked, Appointment.slot_start >= now)
                    .order_by(Appointment.slot_start)
                    .limit(UPCOMING_LIMIT)
                )
            ).all()
        async with timings.measure("list past appointments"):
            past = (
                await session.scalars(
                    select(Appointment)
                    .where(booked, Appointment.slot_start < now)
                    .order_by(Appointment.slot_start.desc())
                    .limit(PAST_LIMIT)
                )
            ).all()
        async with timings.measure("list cancelled appointments"):
            cancelled = (
                await session.scalars(
                    select(Appointment)
                    .where(Appointment.status == "cancelled")
                    .order_by(Appointment.updated_at.desc())
                    .limit(CANCELLED_LIMIT)
                )
            ).all()
        result["appointments"] = {
            "upcoming": [_appointment_row(a, sched) for a in upcoming],
            "past": [_appointment_row(a, sched) for a in past],
            "cancelled": [_appointment_row(a, sched) for a in cancelled],
        }

        async with timings.measure("list recent calls"):
            calls = (
                await session.scalars(
                    select(Call).order_by(Call.started_at.desc()).limit(CALLS_LIMIT)
                )
            ).all()
        async with timings.measure("find calls that booked"):
            booked_calls = set(
                (
                    await session.scalars(
                        select(Appointment.booked_via_call).where(
                            Appointment.booked_via_call.in_([c.provider_call_id for c in calls])
                        )
                    )
                ).all()
            )
        result["calls"] = [
            {
                "from": mask_number(c.from_number or ""),
                "status": _call_status(c, now),
                "duration_seconds": c.duration_seconds,
                "booked": c.provider_call_id in booked_calls,
                **_labels(c.started_at, sched),
            }
            for c in calls
        ]

        postgres: dict[str, Any] = {}
        if dialect == "postgresql":
            async with timings.measure("postgres statistics"):
                postgres = await _postgres_details(session)

    # The booking agent runs this exact lookup while the caller waits.
    day = sched.now().date()
    for _ in range(14):
        if day.weekday() in sched.open_days:
            break
        day += timedelta(days=1)
    lookups = []
    for _ in range(3):
        start = time.perf_counter()
        await appts.free_slots(factory, sched, day)
        lookups.append((time.perf_counter() - start) * 1000)

    redis_info: dict[str, Any] | None
    try:
        redis_info = await _redis_details(redis)
    except Exception:
        redis_info = None

    result["performance"] = {
        "postgres_ping_ms": await _ping_db(factory),
        "slot_lookup_ms": round(statistics.median(lookups), 1),
        "query_ms": timings.ms,
        "postgres": postgres or None,
        "redis": redis_info,
    }
    return result
