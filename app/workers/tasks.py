"""ARQ worker: runs after-call jobs. For now it only logs each event."""

import asyncio
from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from app.business.call_flush import flush_orphans
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import create_session_factory
from app.events.consumers import consume_forever

log = structlog.get_logger(__name__)


async def log_call_event(ctx: dict[str, Any], event: dict[str, Any]) -> None:
    """Placeholder after-call job (later: summary, CRM sync, retries)."""
    log.info("call_event_received", **event)


async def save_unsaved_calls(ctx: dict[str, Any]) -> None:
    """Every minute: write calls that ended (or died) without being saved to Postgres."""
    saved = await flush_orphans(ctx["event_redis"], ctx["session_factory"])
    if saved:
        log.info("unsaved_calls_recovered", count=saved)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    redis: Redis = Redis.from_url(settings.redis_url)
    ctx["event_redis"] = redis
    ctx["db_engine"] = create_async_engine(settings.database_url, pool_size=2, max_overflow=2)
    ctx["session_factory"] = create_session_factory(ctx["db_engine"])

    async def handler(event: dict[str, Any]) -> None:
        # Stream event -> ARQ job. ARQ gives retries and a result log.
        await ctx["redis"].enqueue_job("log_call_event", event)

    ctx["consumer_task"] = asyncio.create_task(consume_forever(redis, handler))
    log.info("worker_started")


async def shutdown(ctx: dict[str, Any]) -> None:
    ctx["consumer_task"].cancel()
    await ctx["event_redis"].aclose()
    await ctx["db_engine"].dispose()
    log.info("worker_stopped")


class WorkerSettings:
    functions = [log_call_event]
    cron_jobs = [cron(save_unsaved_calls, second={0}, run_at_startup=True)]  # every minute
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 5
