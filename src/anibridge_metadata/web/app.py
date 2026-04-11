"""FastAPI application factory."""

import asyncio
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from anibridge_metadata.core.config import Settings, get_settings
from anibridge_metadata.core.db import build_engine, init_db
from anibridge_metadata.services.batch_refresh import BatchRefreshService
from anibridge_metadata.services.providers.registry import ProviderRegistry
from anibridge_metadata.web.routes import router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = build_engine(resolved_settings)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        provider_registry = ProviderRegistry(settings=resolved_settings)

        await provider_registry.start()
        await init_db(engine)

        batch_providers = provider_registry.batchable_providers()
        batch_refresh_service = BatchRefreshService(
            session_factory=session_factory,
            settings=resolved_settings,
            providers=batch_providers,
        )
        await batch_refresh_service.start()
        startup_task = None
        if resolved_settings.batch_refresh.refresh_on_startup:
            startup_task = asyncio.create_task(
                batch_refresh_service.refresh_all(),
                name="batch-refresh-startup",
            )

        app.state.engine = engine
        app.state.provider_registry = provider_registry
        app.state.session_factory = session_factory
        app.state.settings = resolved_settings
        app.state.batch_refresh_service = batch_refresh_service
        # Store the background startup task so it can be cancelled or awaited later.
        app.state.batch_refresh_startup_task = startup_task

        yield

        startup_task = getattr(app.state, "batch_refresh_startup_task", None)
        if startup_task is not None and not startup_task.done():
            startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await startup_task

        await batch_refresh_service.close()
        await provider_registry.close()
        await engine.dispose()

    app = FastAPI(
        lifespan=lifespan,
        title="anibridge-metadata",
        version=version("anibridge-metadata"),
    )
    app.include_router(router)

    return app


app = create_app()
