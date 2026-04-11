from fastapi.testclient import TestClient

from anibridge_metadata.core.config import Settings
from anibridge_metadata.web.app import create_app


def test_readyz_endpoint_reports_ready(test_settings: Settings) -> None:
    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
