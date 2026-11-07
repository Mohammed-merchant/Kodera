"""ARQ worker: runs after-call jobs. For now it only logs each event."""

import asyncio
from typing import Any

import structlog
from arq.connections import RedisSettings
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.events.consumers import consume_forever

log = structlog.get_logger(__name__)


async def log_call_event(ctx: dict[str, Any], event: dict[str, Any]) -> None:
    """Placeholder after-call job (later: summary, CRM sync, retries)."""
    log.info("call_event_received", **event)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    redis: Redis = Redis.from_url(settings.redis_url)
    ctx["event_redis"] = redis

    async def handler(event: dict[str, Any]) -> None:
        # Stream event -> ARQ job. ARQ gives retries and a result log.
        await ctx["redis"].enqueue_job("log_call_event", event)

    ctx["consumer_task"] = asyncio.create_task(consume_forever(redis, handler))
    log.info("worker_started")


async def shutdown(ctx: dict[str, Any]) -> None:
    ctx["consumer_task"].cancel()
    await ctx["event_redis"].aclose()
    log.info("worker_stopped")


class WorkerSettings:
    functions = [log_call_event]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 5
