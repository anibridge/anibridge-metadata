"""Tests for the SSE batch streaming endpoint."""

from datetime import UTC, datetime

import orjson
from fastapi.testclient import TestClient

from anibridge_metadata.core.config import Settings
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
from anibridge_metadata.services.providers.base import UpstreamNotFoundError
from anibridge_metadata.web.app import create_app
from anibridge_metadata.web.dependencies import get_batch_collector


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
            expires_at=datetime(2026, 4, 11, 6, 0, tzinfo=UTC),
            stale=False,
            source="cache",
        ),
    )


class FakeResolver:
    async def resolve(self, **kwargs) -> MetadataEnvelope:
        descriptor = kwargs["descriptor"]
        if descriptor == "anilist:404":
            raise UpstreamNotFoundError("Not found")
        return _make_envelope(descriptor)

    async def resolve_many_cached(
        self, descriptors: list[str]
    ) -> dict[str, MetadataEnvelope | CacheEntry | None]:
        return {d: None for d in descriptors}


def _create_app(settings: Settings) -> TestClient:
    app = create_app(settings)
    collector = BatchCollector(resolver=FakeResolver())
    app.dependency_overrides[get_batch_collector] = lambda: collector
    return TestClient(app)


def test_batch_sse_streams_results(test_settings: Settings) -> None:
    client = _create_app(test_settings)
    with client:
        response = client.post(
            "/api/metadata/batch/stream",
            json={"descriptors": ["anilist:1", "anilist:2"]},
        )
    assert response.status_code == 200
    lines = [
        line for line in response.text.strip().split("\n") if line.startswith("data:")
    ]
    # Should have at least 2 data events (one per descriptor) + the done event
    assert len(lines) >= 2
    descriptors_seen = set()
    for line in lines:
        payload = orjson.loads(line.removeprefix("data: "))
        if "descriptor" in payload:
            descriptors_seen.add(payload["descriptor"])
    assert "anilist:1" in descriptors_seen
    assert "anilist:2" in descriptors_seen


def test_batch_sse_includes_errors(test_settings: Settings) -> None:
    client = _create_app(test_settings)
    with client:
        response = client.post(
            "/api/metadata/batch/stream",
            json={"descriptors": ["anilist:1", "anilist:404"]},
        )
    assert response.status_code == 200
    events = []
    for line in response.text.strip().split("\n"):
        if line.startswith("data:"):
            events.append(orjson.loads(line.removeprefix("data: ")))
    statuses = {
        e.get("descriptor"): e.get("status") for e in events if "descriptor" in e
    }
    assert statuses.get("anilist:1") == "ok"
    assert statuses.get("anilist:404") == "error"


def test_batch_sse_rejects_empty_list(test_settings: Settings) -> None:
    client = _create_app(test_settings)
    with client:
        response = client.post(
            "/api/metadata/batch/stream",
            json={"descriptors": []},
        )
    assert response.status_code == 422
