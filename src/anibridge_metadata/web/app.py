"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI
from redis.asyncio import Redis

from anibridge_metadata.core.config import Settings, get_settings
from anibridge_metadata.services.batch_collector import BatchCollector
from anibridge_metadata.services.batch_refresh import BatchRefreshService
from anibridge_metadata.services.cache import CacheLayer
from anibridge_metadata.services.providers.registry import ProviderRegistry
from anibridge_metadata.services.resolver import Resolver
from anibridge_metadata.web.routes import router

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Log level: %s", resolved_settings.log_level.upper())

        redis_display = resolved_settings.redis_url.split("@")[-1]
        logger.info("Connecting to Redis at %s", redis_display)
        redis = Redis.from_url(
            resolved_settings.redis_url,
            decode_responses=False,
        )
        cache = CacheLayer(redis=redis, settings=resolved_settings)
        provider_registry = ProviderRegistry(settings=resolved_settings)
        await provider_registry.start()

        enabled = sorted(provider_registry.enabled_providers())
        batchable = sorted(provider_registry.batchable_providers())
        logger.info("Providers enabled: %s", ", ".join(enabled) or "none")
        logger.info("Batchable providers: %s", ", ".join(batchable) or "none")

        resolver = Resolver(
            cache=cache,
            provider_registry=provider_registry,
        )
        batch_collector = BatchCollector(resolver=resolver)
        batch_refresh = BatchRefreshService(
            config=resolved_settings.batch_refresh,
            cache=cache,
            providers=provider_registry.batchable_providers(),
        )
        batch_refresh.start()

        app.state.redis = redis
        app.state.cache = cache
        app.state.provider_registry = provider_registry
        app.state.settings = resolved_settings
        app.state.resolver = resolver
        app.state.batch_collector = batch_collector
        app.state.batch_refresh = batch_refresh

        yield

        logger.info("Shutting down…")
        await batch_refresh.close()
        await provider_registry.close()
        await redis.aclose()
        logger.info("Shutdown complete.")

    app = FastAPI(
        lifespan=lifespan,
        title="anibridge-metadata",
        version=version("anibridge-metadata"),
    )
    app.include_router(router)

    return app


app = create_app()
