from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)


def test_root_endpoint():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert "version" in data


def test_health_returns_200():
    response = client.get("/health")
    assert response.status_code == 200


def test_health_response_structure():
    response = client.get("/health")
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "TRINITY CORE"
    assert "version" in data
    assert "environment" in data
    assert "timestamp" in data


def test_health_environment_is_development():
    response = client.get("/health")
    data = response.json()
    assert data["environment"] == "development"
