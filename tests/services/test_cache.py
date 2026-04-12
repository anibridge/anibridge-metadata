"""Tests for the Redis-backed cache layer and resolver."""

import asyncio

import pytest
from pydantic import BaseModel

from anibridge_metadata.core.config import Settings
from anibridge_metadata.core.descriptors import MetadataDescriptor
from anibridge_metadata.core.enums import DescriptorProvider, EntityType, ImageType
from anibridge_metadata.models.metadata import (
    MetadataImageModel,
    UnifiedMetadata,
    build_classification,
    build_metadata_id,
    build_titles,
)
from anibridge_metadata.services.cache import CacheLayer
from anibridge_metadata.services.providers.base import UpstreamNotFoundError
from anibridge_metadata.services.resolver import Resolver


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


class FakeRedis:
    """Minimal in-memory Redis mock for cache tests."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._ttls: dict[str, int] = {}

    async def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    async def set(self, key: str, value: bytes, *, ex: int | None = None) -> None:
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._store.pop(key, None)
            self._ttls.pop(key, None)

    async def ttl(self, key: str) -> int:
        return self._ttls.get(key, -1)

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


@pytest.fixture
def settings() -> Settings:
    return Settings(
        redis_url="redis://localhost:6379/15",
        cache_ttl_seconds=3600,
    )


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def cache(fake_redis: FakeRedis, settings: Settings) -> CacheLayer:
    return CacheLayer(redis=fake_redis, settings=settings)  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_resolver_fetches_and_caches(
    cache: CacheLayer, settings: Settings
) -> None:
    adapter = FakeAdapter()
    resolver = Resolver(cache=cache, provider_registry=FakeRegistry(adapter))

    first = await resolver.resolve(descriptor="anilist:1")
    second = await resolver.resolve(descriptor="anilist:1")

    assert first.cache.source == "upstream"
    assert second.cache.source == "cache"
    assert adapter.calls == ["anilist:1"]


@pytest.mark.asyncio
async def test_resolver_resolves_scoped_descriptor_to_parent(
    cache: CacheLayer, settings: Settings
) -> None:
    adapter = FakeAdapter()
    resolver = Resolver(cache=cache, provider_registry=FakeRegistry(adapter))

    envelope = await resolver.resolve(descriptor="tmdb_show:44:s1")

    assert envelope.metadata.kind == EntityType.SHOW
    assert envelope.metadata.id.descriptor == "tmdb_show:44"
    assert adapter.calls == ["tmdb_show:44"]


@pytest.mark.asyncio
async def test_resolver_deduplicates_concurrent_requests(
    cache: CacheLayer, settings: Settings
) -> None:
    adapter = FakeAdapter()
    resolver = Resolver(cache=cache, provider_registry=FakeRegistry(adapter))

    results = await asyncio.gather(
        resolver.resolve(descriptor="anilist:1"),
        resolver.resolve(descriptor="anilist:1"),
    )

    assert len(results) == 2
    # Only one upstream call despite two concurrent requests
    assert adapter.calls == ["anilist:1"]


class NotFoundAdapter:
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
async def test_404_is_cached(cache: CacheLayer, settings: Settings) -> None:
    adapter = NotFoundAdapter()
    resolver = Resolver(cache=cache, provider_registry=NotFoundRegistry(adapter))

    with pytest.raises(UpstreamNotFoundError):
        await resolver.resolve(descriptor="anilist:999")

    # Second call should raise from cache without contacting the provider.
    with pytest.raises(UpstreamNotFoundError, match="Cached 404"):
        await resolver.resolve(descriptor="anilist:999")

    assert adapter.calls == ["anilist:999"]


@pytest.mark.asyncio
async def test_force_refresh_bypasses_cache(
    cache: CacheLayer, settings: Settings
) -> None:
    adapter = FakeAdapter()
    resolver = Resolver(cache=cache, provider_registry=FakeRegistry(adapter))

    first = await resolver.resolve(descriptor="anilist:1")
    second = await resolver.resolve(descriptor="anilist:1", force_refresh=True)

    assert first.cache.source == "upstream"
    assert second.cache.source == "upstream"
    assert adapter.calls == ["anilist:1", "anilist:1"]


@pytest.mark.asyncio
async def test_cache_layer_ping(cache: CacheLayer) -> None:
    await cache.ping()


@pytest.mark.asyncio
async def test_cache_layer_put_and_get(cache: CacheLayer) -> None:
    metadata = UnifiedMetadata(
        kind=EntityType.SHOW,
        id=build_metadata_id(
            descriptor="anilist:1",
            provider=DescriptorProvider.ANILIST,
            provider_id="1",
        ),
        titles=build_titles(display="Test"),
        classification=build_classification(),
    )
    await cache.put("anilist:1", metadata)
    entry = await cache.get("anilist:1")

    assert entry is not None
    assert entry.normalized is not None
    assert entry.normalized.titles.display == "Test"
    assert entry.is_fresh
    assert not entry.not_found
