"""FastAPI application factory."""

from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from anibridge_metadata.core.config import Settings, get_settings
from anibridge_metadata.core.db import build_engine, init_db
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

        app.state.engine = engine
        app.state.provider_registry = provider_registry
        app.state.session_factory = session_factory
        app.state.settings = resolved_settings

        yield

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
