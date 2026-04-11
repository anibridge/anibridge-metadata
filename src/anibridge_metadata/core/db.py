"""Database bootstrap helpers."""

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from anibridge_metadata.core.config import Settings


class Base(AsyncAttrs, DeclarativeBase):
    """Base class for SQLAlchemy ORM models."""


def build_engine(settings: Settings) -> AsyncEngine:
    """Create an async SQLAlchemy engine for the configured database."""
    engine_kwargs: dict[str, object] = {"echo": settings.sql_echo}

    url = make_url(settings.database_url)

    if url.drivername.startswith("sqlite+aiosqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}

        if url.database and url.database != ":memory:":
            db_path = Path(url.database)
            db_path.parent.mkdir(parents=True, exist_ok=True)

        if settings.database_url.endswith(":memory:"):
            engine_kwargs["poolclass"] = StaticPool

    return create_async_engine(settings.database_url, **engine_kwargs)


def build_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """Build an async session factory for the configured database."""
    engine = build_engine(settings)
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped database session."""
    async with session_factory() as session:
        yield session


async def init_db(engine: AsyncEngine) -> None:
    """Create all database tables if they do not exist yet."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def ping_db(session: AsyncSession) -> None:
    """Execute a lightweight query against the configured database."""
    await session.execute(text("SELECT 1"))
