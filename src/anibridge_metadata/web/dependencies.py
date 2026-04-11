"""FastAPI dependency providers."""

from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from anibridge_metadata.core.config import Settings
from anibridge_metadata.services.cache import CacheService
from anibridge_metadata.services.providers.registry import ProviderRegistry


def get_settings(request: Request) -> Settings:
    """Return application settings from app state."""
    return request.app.state.settings


def get_provider_registry(request: Request) -> ProviderRegistry:
    """Return the provider registry from app state."""
    return request.app.state.provider_registry


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a database session bound to the current request."""
    async with request.app.state.session_factory() as session:
        yield session


def get_cache_service(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> CacheService:
    """Build the cache service for the current request."""
    return CacheService(
        session=session,
        settings=request.app.state.settings,
        provider_registry=request.app.state.provider_registry,
    )
