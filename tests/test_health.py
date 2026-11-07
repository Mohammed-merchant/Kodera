"""Tests for the health endpoint."""

from fastapi.testclient import TestClient

from app import __version__
from app.main import app


def test_health_returns_ok_and_version() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
