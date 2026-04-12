from datetime import UTC, datetime

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
from anibridge_metadata.services.providers.base import UpstreamNotFoundError
from anibridge_metadata.web.app import create_app
from anibridge_metadata.web.dependencies import get_cache_service


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


class FakeCacheService:
    async def get_metadata(self, **kwargs) -> MetadataEnvelope:
        descriptor = kwargs["descriptor"]
        if descriptor == "anilist:404":
            raise UpstreamNotFoundError("Not found")
        return _make_envelope(descriptor)


def _create_app(settings: Settings) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_cache_service] = lambda: FakeCacheService()
    return TestClient(app)


def test_batch_returns_multiple_results(test_settings: Settings) -> None:
    client = _create_app(test_settings)
    with client:
        response = client.post(
            "/api/metadata/batch",
            json={"descriptors": ["anilist:1", "anilist:2"]},
        )
    assert response.status_code == 200
    body = response.json()
    assert "anilist:1" in body["results"]
    assert "anilist:2" in body["results"]
    assert body["errors"] == {}


def test_batch_returns_errors_for_failed_descriptors(
    test_settings: Settings,
) -> None:
    client = _create_app(test_settings)
    with client:
        response = client.post(
            "/api/metadata/batch",
            json={"descriptors": ["anilist:1", "anilist:404"]},
        )
    assert response.status_code == 200
    body = response.json()
    assert "anilist:1" in body["results"]
    assert "anilist:404" in body["errors"]
    assert body["errors"]["anilist:404"]["status_code"] == 404


def test_batch_deduplicates_descriptors(test_settings: Settings) -> None:
    client = _create_app(test_settings)
    with client:
        response = client.post(
            "/api/metadata/batch",
            json={"descriptors": ["anilist:1", "anilist:1"]},
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    assert "anilist:1" in body["results"]


def test_batch_rejects_empty_list(test_settings: Settings) -> None:
    client = _create_app(test_settings)
    with client:
        response = client.post(
            "/api/metadata/batch",
            json={"descriptors": []},
        )
    assert response.status_code == 422


def test_batch_rejects_oversized_list(test_settings: Settings) -> None:
    client = _create_app(test_settings)
    with client:
        response = client.post(
            "/api/metadata/batch",
            json={"descriptors": [f"anilist:{i}" for i in range(51)]},
        )
    assert response.status_code == 422
