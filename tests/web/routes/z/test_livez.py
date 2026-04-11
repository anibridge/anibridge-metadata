from fastapi.testclient import TestClient

from anibridge_metadata.core.config import Settings
from anibridge_metadata.web.app import create_app


def test_livez_endpoint_reports_alive(test_settings: Settings) -> None:
    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.get("/livez")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
