"""FastAPI dependency providers."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from anibridge_metadata.core.config import Settings


def get_settings(request: Request) -> Settings:
    """Return application settings from app state."""
    return request.app.state.settings


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a database session bound to the current request."""
    async with request.app.state.session_factory() as session:
        yield session
