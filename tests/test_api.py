"""API tests through TestClient."""

from app.config import settings


def test_health_reports_version_and_uptime(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == settings.app_version
    assert body["uptime_seconds"] >= 0
    assert r.headers["X-Request-ID"]
