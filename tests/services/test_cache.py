import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from anibridge_metadata.core.config import Settings
from anibridge_metadata.core.db import build_engine, init_db
from anibridge_metadata.core.descriptors import MetadataDescriptor, parse_descriptor
from anibridge_metadata.core.enums import DescriptorProvider, EntityType, ImageType
from anibridge_metadata.models.database import MetadataRecord
from anibridge_metadata.models.metadata import (
    MetadataImageModel,
    UnifiedMetadata,
    build_classification,
    build_metadata_id,
    build_titles,
    record_to_envelope,
)
from anibridge_metadata.services.cache import CacheService
from anibridge_metadata.services.providers.base import UpstreamNotFoundError
from anibridge_metadata.services.revalidator import BackgroundRevalidator


class FakePayload(BaseModel):
    entity_type: str
    id: str
    title: str


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_raw(self, *, descriptor: MetadataDescriptor) -> FakePayload:
        self.calls.append(descriptor.key)
        return FakePayload(
            entity_type=descriptor.requested_entity_type.value,
            id=descriptor.provider_id,
            title="Cowboy Bebop",
        )

    async def normalize(
        self,
        *,
        descriptor: MetadataDescriptor,
        payload: FakePayload,
    ) -> UnifiedMetadata:
        return UnifiedMetadata(
            kind=descriptor.requested_entity_type,
            id=build_metadata_id(
                descriptor=descriptor.key,
                provider=descriptor.provider,
                provider_id=descriptor.provider_id,
            ),
            titles=build_titles(display=payload.title, aliases=["Kauboi Bibappu"]),
            units=26,
            classification=build_classification(genres=["Action"]),
            images=[
                MetadataImageModel(
                    kind=ImageType.POSTER, url="https://example.com/poster.jpg"
                )
            ],
        )


class FakeRegistry:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter = adapter

    def get(self, provider: DescriptorProvider) -> FakeAdapter:
        assert provider in {DescriptorProvider.ANILIST, DescriptorProvider.TMDB_SHOW}
        return self.adapter


@pytest.fixture
async def cache_dependencies() -> AsyncIterator[tuple[Settings, async_sessionmaker]]:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        cache_ttl_seconds=3600,
    )
    engine = build_engine(settings)
    await init_db(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield settings, session_factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_cache_service_uses_cached_record(
    cache_dependencies: tuple[Settings, async_sessionmaker],
) -> None:
    settings, session_factory = cache_dependencies
    adapter = FakeAdapter()

    async with session_factory() as session:
        service = CacheService(
            session=session,
            settings=settings,
            provider_registry=FakeRegistry(adapter),
        )

        first = await service.get_metadata(descriptor="anilist:1")
        second = await service.get_metadata(descriptor="anilist:1")

    assert first.cache.source == "upstream"
    assert second.cache.source == "cache"
    assert adapter.calls == ["anilist:1"]


@pytest.mark.asyncio
async def test_cache_service_refreshes_stale_record(
    cache_dependencies: tuple[Settings, async_sessionmaker],
) -> None:
    settings, session_factory = cache_dependencies
    adapter = FakeAdapter()

    async with session_factory() as session:
        service = CacheService(
            session=session,
            settings=settings,
            provider_registry=FakeRegistry(adapter),
        )

        await service.get_metadata(descriptor="anilist:1")

        result = await session.execute(select(MetadataRecord))
        record = result.scalar_one()
        record.updated_at = datetime.now(UTC) - timedelta(
            seconds=settings.cache_ttl_seconds + 1
        )
        await session.commit()

        refreshed = await service.get_metadata(descriptor="anilist:1")

    assert refreshed.cache.source == "upstream"
    assert adapter.calls == ["anilist:1", "anilist:1"]


@pytest.mark.asyncio
async def test_cache_service_resolves_scoped_descriptor_to_parent(
    cache_dependencies: tuple[Settings, async_sessionmaker],
) -> None:
    settings, session_factory = cache_dependencies
    adapter = FakeAdapter()

    async with session_factory() as session:
        service = CacheService(
            session=session,
            settings=settings,
            provider_registry=FakeRegistry(adapter),
        )

        envelope = await service.get_metadata(descriptor="tmdb_show:44:s1")
        result = await session.execute(
            select(MetadataRecord).order_by(MetadataRecord.descriptor)
        )
        descriptors = [record.descriptor for record in result.scalars().all()]

    assert envelope.metadata.kind == EntityType.SHOW
    assert envelope.metadata.id.descriptor == "tmdb_show:44"
    assert descriptors == ["tmdb_show:44"]
    assert adapter.calls == ["tmdb_show:44"]


def test_record_to_envelope_normalizes_naive_datetimes() -> None:
    metadata = UnifiedMetadata(
        kind=EntityType.SHOW,
        id=build_metadata_id(
            descriptor="anilist:1",
            provider=DescriptorProvider.ANILIST,
            provider_id="1",
        ),
        titles=build_titles(display="Cowboy Bebop"),
        classification=build_classification(),
    )
    record = MetadataRecord(
        descriptor="anilist:1",
        normalized_payload=metadata.model_dump(mode="json"),
        updated_at=datetime(2026, 4, 11, 0, 0),
    )

    envelope = record_to_envelope(record, source="cache", cache_ttl_seconds=21600)

    assert envelope.cache.updated_at.tzinfo == UTC
    assert envelope.cache.expires_at.tzinfo == UTC


class SlowAdapter:
    """Adapter that takes a configurable delay before returning data."""

    def __init__(self, delay: float = 10.0, title: str = "Fresh Title") -> None:
        self.delay = delay
        self.title = title
        self.calls: list[str] = []

    async def fetch_raw(self, *, descriptor: MetadataDescriptor) -> FakePayload:
        self.calls.append(descriptor.key)
        await asyncio.sleep(self.delay)
        return FakePayload(
            entity_type=descriptor.requested_entity_type.value,
            id=descriptor.provider_id,
            title=self.title,
        )

    async def normalize(
        self,
        *,
        descriptor: MetadataDescriptor,
        payload: FakePayload,
    ) -> UnifiedMetadata:
        return UnifiedMetadata(
            kind=descriptor.requested_entity_type,
            id=build_metadata_id(
                descriptor=descriptor.key,
                provider=descriptor.provider,
                provider_id=descriptor.provider_id,
            ),
            titles=build_titles(display=payload.title),
            classification=build_classification(),
        )


class SlowRegistry:
    def __init__(self, adapter: SlowAdapter) -> None:
        self.adapter = adapter

    def get(self, provider: DescriptorProvider) -> SlowAdapter:
        return self.adapter


async def _seed_stale_record(
    session_factory: async_sessionmaker,
    settings: Settings,
    descriptor: str = "anilist:1",
    title: str = "Stale Title",
) -> None:
    """Insert a record that is already past its cache TTL."""
    metadata = UnifiedMetadata(
        kind=EntityType.SHOW,
        id=build_metadata_id(
            descriptor=descriptor,
            provider=DescriptorProvider.ANILIST,
            provider_id=descriptor.split(":")[1],
        ),
        titles=build_titles(display=title),
        classification=build_classification(),
    )
    async with session_factory() as session:
        record = MetadataRecord(
            descriptor=descriptor,
            normalized_payload=metadata.model_dump(mode="json"),
        )
        session.add(record)
        await session.flush()
        record.updated_at = datetime.now(UTC) - timedelta(
            seconds=settings.cache_ttl_seconds + 1
        )
        await session.commit()


@pytest.mark.asyncio
async def test_stale_while_revalidate_returns_stale_on_timeout(
    cache_dependencies: tuple[Settings, async_sessionmaker],
) -> None:
    """When upstream is slow, stale data is returned within the timeout."""
    settings, session_factory = cache_dependencies
    settings.stale_timeout_seconds = 0.1  # very short so the test is fast
    adapter = SlowAdapter(delay=5.0, title="Fresh Title")
    registry = SlowRegistry(adapter)

    revalidator = BackgroundRevalidator(
        session_factory=session_factory,
        settings=settings,
        provider_registry=registry,
    )

    await _seed_stale_record(session_factory, settings)

    async with session_factory() as session:
        service = CacheService(
            session=session,
            settings=settings,
            provider_registry=registry,
            revalidator=revalidator,
        )
        envelope = await service.get_metadata(descriptor="anilist:1")

    assert envelope.cache.source == "stale-cache"
    assert envelope.metadata.titles.display == "Stale Title"

    await revalidator.close()


@pytest.mark.asyncio
async def test_stale_while_revalidate_returns_fresh_when_fast(
    cache_dependencies: tuple[Settings, async_sessionmaker],
) -> None:
    """When upstream replies quickly, fresh data is returned."""
    settings, session_factory = cache_dependencies
    settings.stale_timeout_seconds = 5.0
    adapter = SlowAdapter(delay=0.0, title="Fresh Title")
    registry = SlowRegistry(adapter)

    revalidator = BackgroundRevalidator(
        session_factory=session_factory,
        settings=settings,
        provider_registry=registry,
    )

    await _seed_stale_record(session_factory, settings)

    async with session_factory() as session:
        service = CacheService(
            session=session,
            settings=settings,
            provider_registry=registry,
            revalidator=revalidator,
        )
        envelope = await service.get_metadata(descriptor="anilist:1")

    assert envelope.cache.source == "upstream"
    assert envelope.metadata.titles.display == "Fresh Title"

    await revalidator.close()


@pytest.mark.asyncio
async def test_stale_while_revalidate_deduplicates_tasks(
    cache_dependencies: tuple[Settings, async_sessionmaker],
) -> None:
    """Concurrent requests for the same descriptor share one background task."""
    settings, session_factory = cache_dependencies
    settings.stale_timeout_seconds = 0.05
    adapter = SlowAdapter(delay=5.0)
    registry = SlowRegistry(adapter)

    revalidator = BackgroundRevalidator(
        session_factory=session_factory,
        settings=settings,
        provider_registry=registry,
    )

    await _seed_stale_record(session_factory, settings)

    descriptor = parse_descriptor("anilist:1")
    task_a = revalidator.schedule(descriptor)
    task_b = revalidator.schedule(descriptor)

    assert task_a is task_b

    await revalidator.close()


@pytest.mark.asyncio
async def test_force_refresh_bypasses_stale_while_revalidate(
    cache_dependencies: tuple[Settings, async_sessionmaker],
) -> None:
    """force_refresh=True should do a blocking fetch, not stale-while-revalidate."""
    settings, session_factory = cache_dependencies
    adapter = FakeAdapter()
    registry = FakeRegistry(adapter)

    revalidator = BackgroundRevalidator(
        session_factory=session_factory,
        settings=settings,
        provider_registry=registry,
    )

    await _seed_stale_record(session_factory, settings, title="Stale Title")

    async with session_factory() as session:
        service = CacheService(
            session=session,
            settings=settings,
            provider_registry=registry,
            revalidator=revalidator,
        )
        envelope = await service.get_metadata(
            descriptor="anilist:1", force_refresh=True
        )

    assert envelope.cache.source == "upstream"
    # The blocking refresh should have been called via the request session.
    assert adapter.calls == ["anilist:1"]

    await revalidator.close()


class NotFoundAdapter:
    """Adapter that always raises UpstreamNotFoundError."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_raw(self, *, descriptor: MetadataDescriptor):
        self.calls.append(descriptor.key)
        raise UpstreamNotFoundError(f"Not found: {descriptor.key}")

    async def normalize(self, *, descriptor: MetadataDescriptor, payload):
        raise AssertionError("normalize should not be called for 404s")


class NotFoundRegistry:
    def __init__(self, adapter: NotFoundAdapter) -> None:
        self.adapter = adapter

    def get(self, provider: DescriptorProvider) -> NotFoundAdapter:
        return self.adapter


@pytest.mark.asyncio
async def test_404_is_cached_and_served_from_cache(
    cache_dependencies: tuple[Settings, async_sessionmaker],
) -> None:
    """A 404 should be cached; the second call should not hit the provider."""
    settings, session_factory = cache_dependencies
    adapter = NotFoundAdapter()

    async with session_factory() as session:
        service = CacheService(
            session=session,
            settings=settings,
            provider_registry=NotFoundRegistry(adapter),
        )

        with pytest.raises(UpstreamNotFoundError):
            await service.get_metadata(descriptor="anilist:999")

        # Second call should raise from cache without contacting the provider.
        with pytest.raises(UpstreamNotFoundError, match="Cached 404"):
            await service.get_metadata(descriptor="anilist:999")

    # Provider was only called once.
    assert adapter.calls == ["anilist:999"]


@pytest.mark.asyncio
async def test_stale_404_retries_upstream(
    cache_dependencies: tuple[Settings, async_sessionmaker],
) -> None:
    """When a cached 404 becomes stale, the next request retries the provider."""
    settings, session_factory = cache_dependencies
    adapter = NotFoundAdapter()

    async with session_factory() as session:
        service = CacheService(
            session=session,
            settings=settings,
            provider_registry=NotFoundRegistry(adapter),
        )

        with pytest.raises(UpstreamNotFoundError):
            await service.get_metadata(descriptor="anilist:999")

        # Expire the record.
        result = await session.execute(select(MetadataRecord))
        record = result.scalar_one()
        record.updated_at = datetime.now(UTC) - timedelta(
            seconds=settings.cache_ttl_seconds + 1
        )
        await session.commit()

        with pytest.raises(UpstreamNotFoundError):
            await service.get_metadata(descriptor="anilist:999")

    # Provider was called twice: once initially, once after expiry.
    assert adapter.calls == ["anilist:999", "anilist:999"]


@pytest.mark.asyncio
async def test_force_refresh_bypasses_cached_404(
    cache_dependencies: tuple[Settings, async_sessionmaker],
) -> None:
    """force_refresh=True should retry the provider even when 404 is cached."""
    settings, session_factory = cache_dependencies
    adapter = NotFoundAdapter()

    async with session_factory() as session:
        service = CacheService(
            session=session,
            settings=settings,
            provider_registry=NotFoundRegistry(adapter),
        )

        with pytest.raises(UpstreamNotFoundError):
            await service.get_metadata(descriptor="anilist:999")

        with pytest.raises(UpstreamNotFoundError):
            await service.get_metadata(descriptor="anilist:999", force_refresh=True)

    # Provider was called twice: initial + force_refresh.
    assert adapter.calls == ["anilist:999", "anilist:999"]
