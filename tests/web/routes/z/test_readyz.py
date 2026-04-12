from fastapi.testclient import TestClient

from anibridge_metadata.core.config import Settings
from anibridge_metadata.web.app import create_app
from anibridge_metadata.web.dependencies import get_cache


class FakeCache:
    async def ping(self) -> None:
        pass


def test_readyz_endpoint_reports_ready(test_settings: Settings) -> None:
    app = create_app(test_settings)
    app.dependency_overrides[get_cache] = lambda: FakeCache()

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
