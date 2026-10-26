"""HTTP API bearer-token auth (fail closed)."""

import pytest
from fastapi.testclient import TestClient

from aecom.api import app as api_app

TOKEN = "test-token-123"


@pytest.fixture
def client(monkeypatch):
    """TestClient without the startup hook (fresh_company already initialised the DB)."""
    def use_token(value):
        monkeypatch.setattr(api_app.secrets, "get", lambda name: value if name == "AECOM_API_TOKEN" else None)

    use_token(TOKEN)
    c = TestClient(api_app.app)
    c.use_token = use_token
    return c


def test_missing_or_wrong_token_is_401(client):
    assert client.get("/approvals").status_code == 401
    assert client.get("/approvals", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/approvals", headers={"Authorization": TOKEN}).status_code == 401
    assert client.post("/kill-switch", params={"on": True}).status_code == 401


def test_right_token_is_accepted(client):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/approvals", headers=auth).status_code == 200
    r = client.post("/kill-switch", params={"on": False}, headers=auth)
    assert r.status_code == 200 and r.json() == {"kill_switch": False}


def test_unconfigured_token_fails_closed(client):
    client.use_token(None)
    r = client.get("/approvals", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503 and r.json()["detail"] == "API token not configured"


def test_health_is_public(client):
    assert client.get("/health").status_code == 200
    client.use_token(None)
    assert client.get("/health").status_code == 200
