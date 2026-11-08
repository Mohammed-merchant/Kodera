"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from app import __version__
from app.api.routes import exotel, health, webhooks
from app.api.ws import exotel_media, media
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.db.session import create_session_factory

settings = get_settings()
setup_logging(settings.log_level)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Clients connect lazily, so startup does not fail if a dependency is down;
    # /ready reports that instead.
    app.state.db_engine = create_async_engine(settings.database_url, pool_size=5, max_overflow=5)
    app.state.session_factory = create_session_factory(app.state.db_engine)
    app.state.redis = Redis.from_url(settings.redis_url)
    log.info("app_started", version=__version__, env=settings.app_env)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.db_engine.dispose()
        log.info("app_stopped")


app = FastAPI(title="AI Calling Agent", version=__version__, lifespan=lifespan)
app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(media.router)
app.include_router(exotel.router)
app.include_router(exotel_media.router)
