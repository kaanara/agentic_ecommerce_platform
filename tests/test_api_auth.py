"""HTTP API bearer-token auth (fail closed).

Operational note: require_token looks the token up per request, but `aecom.secrets` reads the process environment
and `.env` once at startup - changing or rotating AECOM_API_TOKEN therefore needs a restart of `aecom api`.
The tests below swap the lookup itself, which is why they can change the token without a restart.
"""

import re

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


# --------------------------------------------------------------------------- route discovery
PUBLIC = {"/health", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
BEARER_401 = "Invalid or missing bearer token"
UNCONFIGURED_503 = "API token not configured"


def _routes(webhooks: bool) -> list[tuple[str, str]]:
    """(method, path) of every route served by the app - discovered, never hardcoded.

    Read from the generated OpenAPI schema: FastAPI >= 0.14x keeps included routers as lazy `_IncludedRouter`
    entries in `app.routes`, so the schema is the stable public view of all effective routes (a route registered
    with include_in_schema=False would be invisible here - don't do that for API endpoints)."""
    out = []
    for path, ops in api_app.app.openapi()["paths"].items():
        if path in PUBLIC or path.startswith("/webhooks/") != webhooks:
            continue
        out.extend((m.upper(), path) for m in sorted(ops) if m.upper() != "HEAD")
    return out


PROTECTED = _routes(webhooks=False)
WEBHOOKS = _routes(webhooks=True)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_route_discovery_sees_the_api():
    assert ("GET", "/approvals") in PROTECTED and ("POST", "/kill-switch") in PROTECTED
    assert len(PROTECTED) >= 10 and len(WEBHOOKS) >= 2


@pytest.mark.parametrize(("method", "path"), PROTECTED, ids=[f"{m} {p}" for m, p in PROTECTED])
def test_every_non_public_route_requires_the_token(client, method, path):
    """Fails if a route is added to `app` directly instead of the protected `api` router."""
    r = client.request(method, _concrete(path))
    assert r.status_code == 401 and r.json()["detail"] == BEARER_401, (method, path, r.status_code)
    client.use_token(None)
    r = client.request(method, _concrete(path), headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 503 and r.json()["detail"] == UNCONFIGURED_503, (method, path, r.status_code)


@pytest.mark.parametrize(("method", "path"), WEBHOOKS, ids=[f"{m} {p}" for m, p in WEBHOOKS])
def test_webhooks_are_not_behind_the_bearer_token(client, method, path):
    """Providers cannot send our bearer token: webhooks answer from their own signature checks (401/503)."""
    for token in (TOKEN, None):
        client.use_token(token)
        r = client.request(method, path, content=b"{}")
        allowed = (401, 503, 404) if "whatsapp" in path else (401, 503)  # WhatsApp is 404 while disabled
        assert r.status_code in allowed, (path, r.status_code)
        assert r.json()["detail"] not in (BEARER_401, UNCONFIGURED_503), (path, r.json())
