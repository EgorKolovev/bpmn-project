import os

import pytest
from starlette.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_backend.db"
os.environ["ML_SERVICE_URL"] = "http://localhost:8001"

from app.main import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


class TestHealthEndpoint:
    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
