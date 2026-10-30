"""R14: GET /download/{order_id}?token=... - public, token-verified, disabled by default, rate limited.

``download_route_enabled`` and ``downloads_dir`` are not (yet) real ``config.Settings`` fields - the lead still has
to add them (see ``aecom.api.webhooks``'s download-route section docstring) - so these tests set them with
``object.__setattr__`` to bypass pydantic's "no such field" guard, exactly what the route itself works around with
``getattr(get_settings(), ..., default)``. Each test resets them in a ``finally`` because ``get_settings()`` is one
process-wide singleton shared with every other test module.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aecom.api import app as api_app
from aecom.api import webhooks
from aecom.bootstrap import seed_demo
from aecom.config import get_settings
from aecom.db import AuditLog, Order, Product, session
from aecom.store import mock
from aecom.store.mock import MockStore

DOWNLOAD_SKU = "TPL-NOTION-FREELANCE-V1"
FILE_BYTES = b"the actual product bytes"


@pytest.fixture(autouse=True)
def demo():
    seed_demo()


@pytest.fixture
def client():
    return TestClient(api_app.app)


@pytest.fixture
def store():
    return MockStore()


@pytest.fixture
def enabled(tmp_path):
    """Enables the route and points it at a throwaway downloads directory; restores both afterwards."""
    settings = get_settings()
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    object.__setattr__(settings, "download_route_enabled", True)
    object.__setattr__(settings, "downloads_dir", downloads_dir)
    try:
        yield downloads_dir
    finally:
        object.__setattr__(settings, "download_route_enabled", False)
        object.__setattr__(settings, "downloads_dir", Path("data") / "downloads")


def _set_asset(sku: str, relative_path: str | None) -> None:
    with session() as s:
        s.scalars(select(Product).where(Product.sku == sku)).one().asset_url = relative_path


def _deliver(store: MockStore, sku: str = DOWNLOAD_SKU) -> tuple[str, str]:
    """Creates and delivers an order, returns (order_id, token)."""
    order = store.create_order("dl@example.com", sku, "DE")
    result = store.deliver_order(order["id"])
    token = result["download_url"].rsplit("/", 1)[1]
    return order["id"], token


def _write_asset(downloads_dir: Path, relative_path: str, content: bytes = FILE_BYTES) -> None:
    path = downloads_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# --------------------------------------------------------------------------- disabled by default
def test_disabled_by_default_is_404(client, store):
    assert getattr(get_settings(), "download_route_enabled", False) is False
    order_id, token = _deliver(store)
    r = client.get(f"/download/{order_id}", params={"token": token})
    assert r.status_code == 404


# --------------------------------------------------------------------------- happy path
def test_valid_token_streams_the_file(client, store, enabled):
    order_id, token = _deliver(store)
    _set_asset(DOWNLOAD_SKU, "notion.zip")
    _write_asset(enabled, "notion.zip")
    r = client.get(f"/download/{order_id}", params={"token": token})
    assert r.status_code == 200
    assert r.content == FILE_BYTES
    assert "attachment" in r.headers["content-disposition"]
    assert f"{DOWNLOAD_SKU}.zip" in r.headers["content-disposition"]  # filename derived from the sku
    with session() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "download_attempt",
                                               AuditLog.decision == "allow")).one()
    assert row.detail["order_id"] == order_id and "token" not in str(row.detail)


def test_no_file_configured_is_404(client, store, enabled):
    """A product with no asset_url (or none on disk) behaves like any other failure: 404, not a 500."""
    order_id, token = _deliver(store)
    r = client.get(f"/download/{order_id}", params={"token": token})
    assert r.status_code == 404


# --------------------------------------------------------------------------- rejections (identical response)
def test_bad_token_is_404(client, store, enabled):
    order_id, token = _deliver(store)
    _set_asset(DOWNLOAD_SKU, "notion.zip")
    _write_asset(enabled, "notion.zip")
    bad = token[:-1] + ("A" if token[-1] != "A" else "B")
    r = client.get(f"/download/{order_id}", params={"token": bad})
    assert r.status_code == 404


def test_unknown_order_is_404(client, enabled):
    r = client.get("/download/ord_does_not_exist", params={"token": "whatever"})
    assert r.status_code == 404


def test_expired_token_is_404(client, store, enabled, monkeypatch):
    order_id, token = _deliver(store)
    _set_asset(DOWNLOAD_SKU, "notion.zip")
    _write_asset(enabled, "notion.zip")
    with session() as s:
        expires_at = s.get(Order, order_id).download_expires_at
    monkeypatch.setattr(mock, "now", lambda: expires_at + timedelta(seconds=1))
    r = client.get(f"/download/{order_id}", params={"token": token})
    assert r.status_code == 404


def test_refunded_order_is_404(client, store, enabled):
    order_id, token = _deliver(store)
    _set_asset(DOWNLOAD_SKU, "notion.zip")
    _write_asset(enabled, "notion.zip")
    store.refund_order(order_id, 29.0, "customer changed their mind")
    r = client.get(f"/download/{order_id}", params={"token": token})
    assert r.status_code == 404


def test_partially_refunded_order_still_works(client, store, enabled):
    """Matches store.verify_download's own contract (test_downloads.py): partial refund keeps access."""
    order_id, token = _deliver(store)
    _set_asset(DOWNLOAD_SKU, "notion.zip")
    _write_asset(enabled, "notion.zip")
    store.refund_order(order_id, 5.0, "partial goodwill")
    r = client.get(f"/download/{order_id}", params={"token": token})
    assert r.status_code == 200


def test_traversal_attempt_is_rejected(client, store, enabled):
    """A product whose asset_url tries to escape the downloads directory never streams anything, even with a
    genuinely valid token."""
    order_id, token = _deliver(store)
    _set_asset(DOWNLOAD_SKU, "../../etc/passwd")
    r = client.get(f"/download/{order_id}", params={"token": token})
    assert r.status_code == 404


def test_absolute_asset_path_is_rejected(client, store, enabled):
    order_id, token = _deliver(store)
    _set_asset(DOWNLOAD_SKU, "/etc/passwd")
    r = client.get(f"/download/{order_id}", params={"token": token})
    assert r.status_code == 404


def test_remote_asset_url_is_not_streamed(client, store, enabled):
    """asset_url can hold a non-local pointer (e.g. the Lemon Squeezy catalog sync writes buy_now_url, a real
    URL) - the route must never try to read that as a local path."""
    order_id, token = _deliver(store)
    _set_asset(DOWNLOAD_SKU, "https://cdn.example/notion.zip")
    r = client.get(f"/download/{order_id}", params={"token": token})
    assert r.status_code == 404


# --------------------------------------------------------------------------- rate limiting
def test_rate_limit_per_ip_and_order(client, store, enabled):
    order_id, token = _deliver(store)
    _set_asset(DOWNLOAD_SKU, "notion.zip")
    _write_asset(enabled, "notion.zip")
    bad = token[:-1] + ("A" if token[-1] != "A" else "B")
    statuses = [client.get(f"/download/{order_id}", params={"token": bad}).status_code for _ in range(10)]
    assert all(s == 404 for s in statuses)  # 10 failed attempts consume the budget...
    r = client.get(f"/download/{order_id}", params={"token": token})  # ...so even a *valid* 11th is refused
    assert r.status_code == 429


def test_rate_limit_is_scoped_per_order(client, store, enabled):
    """A limit hit on one order never blocks a different order from the same IP."""
    order_id, token = _deliver(store)
    _set_asset(DOWNLOAD_SKU, "notion.zip")
    _write_asset(enabled, "notion.zip")
    bad = token[:-1] + ("A" if token[-1] != "A" else "B")
    for _ in range(10):
        client.get(f"/download/{order_id}", params={"token": bad})
    assert client.get(f"/download/{order_id}", params={"token": token}).status_code == 429
    other_id, other_token = _deliver(store)
    assert client.get(f"/download/{other_id}", params={"token": other_token}).status_code == 200


# --------------------------------------------------------------------------- public route, scoped exposure
def test_route_is_public_no_bearer_needed(client, store, enabled):
    order_id, token = _deliver(store)
    _set_asset(DOWNLOAD_SKU, "notion.zip")
    _write_asset(enabled, "notion.zip")
    r = client.get(f"/download/{order_id}", params={"token": token})  # no Authorization header at all
    assert r.status_code == 200


def test_other_routes_still_require_the_bearer_token(client):
    """Adding the public download route must not accidentally widen what else is public (401, or 503 if no
    AECOM_API_TOKEN is configured in this test environment - either way, never served)."""
    assert client.get("/approvals").status_code in (401, 503)
    assert client.get("/status").status_code in (401, 503)


def test_download_router_only_exposes_the_download_route():
    paths = {r.path for r in webhooks.download_router.routes}
    assert paths == {"/download/{order_id}"}
