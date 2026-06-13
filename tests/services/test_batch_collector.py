"""Tests for scalable batch collection behavior."""

import asyncio
from datetime import UTC, datetime

import pytest

from anibridge_metadata.core.enums import DescriptorProvider, EntityType
from anibridge_metadata.models.metadata import (
    CacheState,
    MetadataEnvelope,
    UnifiedMetadata,
    build_classification,
    build_metadata_id,
    build_titles,
)
from anibridge_metadata.services.batch_collector import BatchCollector
from anibridge_metadata.services.cache import CacheEntry


def _make_envelope(descriptor: str) -> MetadataEnvelope:
    return MetadataEnvelope(
        metadata=UnifiedMetadata(
            kind=EntityType.SHOW,
            id=build_metadata_id(
                descriptor=descriptor,
                provider=DescriptorProvider.ANILIST,
                provider_id=descriptor.split(":")[1],
            ),
            titles=build_titles(display="Test Title"),
            classification=build_classification(),
        ),
        cache=CacheState(
            updated_at=datetime(2026, 4, 11, 0, 0, tzinfo=UTC),
            expires_at=datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
            stale=False,
            source="upstream",
        ),
    )


class SlowResolver:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.resolved: list[str] = []

    async def resolve(self, **kwargs) -> MetadataEnvelope:
        descriptor = kwargs["descriptor"]
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            self.resolved.append(descriptor)
            return _make_envelope(descriptor)
        finally:
            self.active -= 1

    async def resolve_many_cached(
        self, descriptors: list[str]
    ) -> dict[str, MetadataEnvelope | CacheEntry | None]:
        return {descriptor: None for descriptor in descriptors}


@pytest.mark.asyncio
async def test_batch_collector_bounds_upstream_concurrency() -> None:
    resolver = SlowResolver()
    collector = BatchCollector(resolver=resolver)
    descriptors = [f"anilist:{idx}" for idx in range(50)]

    results = [result async for result in collector.stream(descriptors)]

    assert len(results) == len(descriptors)
    assert {result.descriptor for result in results} == set(descriptors)
    assert sorted(resolver.resolved) == sorted(descriptors)
    assert resolver.max_active <= 20
