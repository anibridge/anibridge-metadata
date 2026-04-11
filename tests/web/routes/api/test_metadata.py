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
from anibridge_metadata.web.app import create_app
from anibridge_metadata.web.dependencies import get_cache_service


class FakeCacheService:
    async def get_metadata(self, **kwargs) -> MetadataEnvelope:
        return MetadataEnvelope(
            metadata=UnifiedMetadata(
                kind=EntityType.SHOW,
                id=build_metadata_id(
                    descriptor=kwargs["descriptor"],
                    provider=DescriptorProvider.ANILIST,
                    provider_id="1",
                ),
                titles=build_titles(
                    display="Cowboy Bebop",
                    aliases=["Kauboi Bibappu"],
                ),
                classification=build_classification(genres=["Action", "Sci-Fi"]),
            ),
            cache=CacheState(
                updated_at=datetime(2026, 4, 11, 0, 0, tzinfo=UTC),
                expires_at=datetime(2026, 4, 11, 6, 0, tzinfo=UTC),
                stale=False,
                source="upstream",
            ),
        )


def test_metadata_endpoint_returns_normalized_payload(
    test_settings: Settings,
) -> None:
    app = create_app(test_settings)
    app.dependency_overrides[get_cache_service] = lambda: FakeCacheService()

    with TestClient(app) as client:
        response = client.get("/api/metadata/anilist:1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["kind"] == EntityType.SHOW
    assert payload["metadata"]["id"]["provider"] == DescriptorProvider.ANILIST
    assert payload["metadata"]["id"]["descriptor"] == "anilist:1"
    assert payload["metadata"]["titles"]["display"] == "Cowboy Bebop"
    assert payload["cache"]["source"] == "upstream"


def test_metadata_endpoint_rejects_invalid_descriptor(
    test_settings: Settings,
) -> None:
    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.get("/api/metadata/anilist:1:s2")

    assert response.status_code == 422


def test_metadata_endpoint_rejects_disabled_provider(
    test_settings: Settings,
) -> None:
    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.get("/api/metadata/anilist:1")

    assert response.status_code == 503
    assert "ABM_ANILIST__ENABLED" in response.json()["detail"]
