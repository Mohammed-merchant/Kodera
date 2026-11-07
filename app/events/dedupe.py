"""Duplicate protection for carrier webhooks (Twilio retries when we are slow)."""

from redis.asyncio import Redis

DEDUPE_TTL_SECONDS = 24 * 3600


async def first_time(redis: Redis, key: str, ttl: int = DEDUPE_TTL_SECONDS) -> bool:
    """True the first time a key is seen, False for every repeat (Redis SET NX)."""
    return bool(await redis.set(f"dedupe:{key}", "1", nx=True, ex=ttl))
