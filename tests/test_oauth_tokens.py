"""F2: OAuth token store (`aecom.oauth_tokens`) and its dashboard tab (`dashboard/_ops_connections.py`).

Offline only: `oauth_tokens.resolve_public`/`pinned_post` are monkeypatched to a `FakeHTTP` that never touches a
socket (same pattern as `test_secret_ping.py`'s `FakeHTTP` for `aecom.integrations.ping`; real SSRF behaviour of
`net_guard` itself is covered by test_security.py). `OAUTH_TOKEN_FILE` points at a path under `tmp_path` for every
test (autouse), so nothing here ever touches the real `data/oauth_tokens.json`, `data/aecom.db` or a real backup
directory - `fresh_company` (tests/conftest.py, autouse) already points the database at a temp file too.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from aecom import oauth_tokens
from aecom.config import get_settings
from aecom.db import AuditLog, ErrorRow, session

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the partial does `from _common import ...`

PROVIDER_ENV = {
    "ETSY_CLIENT_ID": "etsy-client-id",
    "GOOGLE_ADS_CLIENT_ID": "gads-client-id",
    "GOOGLE_ADS_CLIENT_SECRET": "gads-client-secret",
    "META_APP_ID": "meta-app-id",
    "META_APP_SECRET": "meta-app-secret",
}


@pytest.fixture(autouse=True)
def token_file(tmp_path, monkeypatch):
    """Every test gets its own OAuth token file and provider client credentials; never the real one."""
    monkeypatch.setenv("OAUTH_TOKEN_FILE", str(tmp_path / "oauth_tokens.json"))
    for key, value in PROVIDER_ENV.items():
        monkeypatch.setenv(key, value)
    return tmp_path / "oauth_tokens.json"


class FakeHTTP:
    """Records every `pinned_post` call; `response` (or `exc`) decides what the next call gets back. `delay`
    simulates the token endpoint taking a moment, to widen the race window in the concurrency test."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.response = httpx.Response(200, json={"access_token": "AT-1", "expires_in": 3600,
                                                   "refresh_token": "RT-rotated-1"})
        self.exc: BaseException | None = None
        self.delay = 0.0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(oauth_tokens, "resolve_public", lambda url: url)  # identity: target is the bare url
        monkeypatch.setattr(oauth_tokens, "pinned_post", self._pinned_post)

    def _pinned_post(self, target, *, data=None, json=None, headers=None, timeout=15) -> httpx.Response:
        if self.delay:
            time.sleep(self.delay)
        self.calls.append({"url": target, "data": data, "headers": headers})
        if self.exc is not None:
            raise self.exc
        self.response.request = httpx.Request("POST", target)
        return self.response


@pytest.fixture
def http(monkeypatch) -> FakeHTTP:
    fake = FakeHTTP()
    fake.install(monkeypatch)
    return fake


def _audit_rows() -> list[AuditLog]:
    with session() as s:
        return list(s.scalars(select(AuditLog).where(AuditLog.action == "oauth_tokens")))


# --------------------------------------------------------------------------- connect / disconnect / status

def test_set_refresh_token_stores_it_and_audits_without_the_value():
    oauth_tokens.set_refresh_token("etsy", "db-must-never-see-this-token-xyz", "ada")
    info = oauth_tokens.status()["etsy"]
    assert info["connected"] is True
    assert info["connected_by"] == "ada"
    rows = _audit_rows()
    assert any(r.decision == "connected" for r in rows)
    assert all("db-must-never-see-this-token-xyz" not in json.dumps(r.detail) for r in rows)


@pytest.mark.parametrize("token", ["", "   ", "has a space", "a\ttab", "a\nnewline"])
def test_set_refresh_token_rejects_empty_or_whitespace(token):
    with pytest.raises(oauth_tokens.OAuthError):
        oauth_tokens.set_refresh_token("etsy", token, "ada")
    assert oauth_tokens.status()["etsy"]["connected"] is False


def test_unknown_provider_is_rejected_everywhere():
    with pytest.raises(oauth_tokens.OAuthError):
        oauth_tokens.set_refresh_token("shopify", "some-plausible-token-value", "ada")
    with pytest.raises(oauth_tokens.OAuthError):
        oauth_tokens.access_token("shopify")
    with pytest.raises(oauth_tokens.OAuthError):
        oauth_tokens.authorize_url("shopify", "state", "https://example.com/cb")


def test_access_token_requires_a_connection_first():
    with pytest.raises(oauth_tokens.OAuthError):
        oauth_tokens.access_token("etsy")


def test_disconnect_removes_the_record_and_audits():
    oauth_tokens.set_refresh_token("etsy", "to-be-disconnected-token-value", "ada")
    oauth_tokens.disconnect("etsy", "ada")
    assert oauth_tokens.status()["etsy"]["connected"] is False
    with pytest.raises(oauth_tokens.OAuthError):
        oauth_tokens.access_token("etsy")
    assert any(r.decision == "disconnected" for r in _audit_rows())


# --------------------------------------------------------------------------- refresh: only when needed, rotation

def test_refresh_happens_only_when_needed(http):
    oauth_tokens.set_refresh_token("etsy", "initial-refresh-token-value", "ada")
    assert oauth_tokens.access_token("etsy") == "AT-1"
    assert len(http.calls) == 1
    # a second call within the token's lifetime must not hit the network again
    assert oauth_tokens.access_token("etsy") == "AT-1"
    assert len(http.calls) == 1


def test_refresh_happens_again_once_within_the_expiry_skew(http):
    oauth_tokens.set_refresh_token("etsy", "initial-refresh-token-value", "ada")
    oauth_tokens.access_token("etsy")
    assert len(http.calls) == 1
    path = oauth_tokens.token_path()
    data = json.loads(path.read_text())
    data["etsy"]["expires_at"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()  # inside the 60s skew
    path.write_text(json.dumps(data))
    http.response = httpx.Response(200, json={"access_token": "AT-2", "expires_in": 3600,
                                               "refresh_token": "RT-rotated-2"})
    assert oauth_tokens.access_token("etsy") == "AT-2"
    assert len(http.calls) == 2


def test_rotated_refresh_token_is_saved_before_the_call_returns(http):
    oauth_tokens.set_refresh_token("etsy", "initial-refresh-token-value", "ada")
    token = oauth_tokens.access_token("etsy")
    assert token == "AT-1"
    stored = json.loads(oauth_tokens.token_path().read_text())
    assert stored["etsy"]["refresh_token"] == "RT-rotated-1"  # the provider rotated it; the new one is on disk
    assert stored["etsy"]["access_token"] == "AT-1"


def test_refresh_sends_client_credentials_and_grant_type(http):
    oauth_tokens.set_refresh_token("google_ads", "gads-refresh-token-value", "ada")
    oauth_tokens.access_token("google_ads")
    sent = http.calls[0]["data"]
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "gads-refresh-token-value"
    assert sent["client_id"] == "gads-client-id"
    assert sent["client_secret"] == "gads-client-secret"


# --------------------------------------------------------------------------- storage discipline

def test_file_mode_is_0600_and_not_group_or_world_readable():
    oauth_tokens.set_refresh_token("etsy", "a-plausible-refresh-token-value", "ada")
    mode = stat.S_IMODE(os.stat(oauth_tokens.token_path()).st_mode)
    assert mode == 0o600
    assert not mode & stat.S_IRWXG
    assert not mode & stat.S_IRWXO


def test_nothing_is_ever_written_under_the_backup_directory(tmp_path, monkeypatch):
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(get_settings(), "backup_dir", backup_root)
    oauth_tokens.set_refresh_token("etsy", "must-not-land-in-a-backup-token", "ada")
    assert not backup_root.exists() or not any(backup_root.rglob("*"))
    monkeypatch.setenv("OAUTH_TOKEN_FILE", str(backup_root / "oauth_tokens.json"))
    with pytest.raises(oauth_tokens.OAuthError, match="backup"):
        oauth_tokens.set_refresh_token("meta", "another-token-value", "ada")
    assert not backup_root.exists()


def test_nothing_is_ever_written_into_the_database():
    """The only DB writes this module makes are `audit()` rows (provider name, actor, decision) - never a table of
    tokens, and the token value itself never reaches one (see also the audit assertion in `test_set_refresh_token_
    stores_it_and_audits_without_the_value`)."""
    oauth_tokens.set_refresh_token("etsy", "db-must-never-store-this-value", "ada")
    with session() as s:
        all_detail = json.dumps([r.detail for r in s.scalars(select(AuditLog)).all()])
    assert "db-must-never-store-this-value" not in all_detail


# --------------------------------------------------------------------------- failure path

def test_refresh_failure_masks_the_token_and_records_a_clean_ops_error(http):
    secret = "super-secret-refresh-token-999"
    oauth_tokens.set_refresh_token("etsy", secret, "ada")
    http.exc = httpx.ConnectError(f"connection refused while sending refresh_token={secret}")
    with pytest.raises(oauth_tokens.OAuthError) as excinfo:
        oauth_tokens.access_token("etsy")
    assert secret not in str(excinfo.value)
    assert "***" in str(excinfo.value)
    with session() as s:
        rows = s.scalars(select(ErrorRow)).all()
    assert rows
    assert all(secret not in (r.message or "") for r in rows)
    assert all(secret not in (r.traceback or "") for r in rows)
    info = oauth_tokens.status()["etsy"]
    assert info["last_error"] and secret not in info["last_error"]
    assert info["connected"] is True  # the refresh token itself is kept; only the attempt failed


# --------------------------------------------------------------------------- concurrency: single refresh in flight

def test_concurrent_access_token_calls_share_a_single_refresh(http):
    oauth_tokens.set_refresh_token("etsy", "shared-refresh-token-value", "ada")
    http.delay = 0.05  # widen the race window so a buggy implementation would double-refresh
    n = 8
    barrier = threading.Barrier(n)
    results: list[str] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            token = oauth_tokens.access_token("etsy")
        except BaseException as exc:  # noqa: BLE001 - captured so the assertion below fails the test, not a worker
            with results_lock:
                errors.append(exc)
            return
        with results_lock:
            results.append(token)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert len(http.calls) == 1
    assert results == ["AT-1"] * n


# --------------------------------------------------------------------------- PKCE consent flow

def test_pkce_pair_challenge_matches_the_s256_transform_of_the_verifier():
    import base64
    import hashlib

    verifier, challenge = oauth_tokens.new_pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier and "=" not in challenge
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_pkce_pairs_are_not_reused():
    pairs = {oauth_tokens.new_pkce_pair() for _ in range(20)}
    assert len(pairs) == 20


def test_authorize_url_builds_a_pkce_consent_link():
    from urllib.parse import parse_qs, urlsplit

    url, verifier = oauth_tokens.authorize_url("etsy", "state-xyz", "https://example.com/callback/etsy")
    parts = urlsplit(url)
    params = parse_qs(parts.query)
    assert parts.scheme == "https" and parts.netloc == "www.etsy.com"
    assert params["client_id"] == ["etsy-client-id"]
    assert params["redirect_uri"] == ["https://example.com/callback/etsy"]
    assert params["state"] == ["state-xyz"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]  # present, non-empty
    assert params["scope"][0].split() == list(oauth_tokens.PROVIDERS["etsy"].scopes)
    assert len(verifier) >= 43


def test_authorize_url_requires_the_client_id(monkeypatch):
    monkeypatch.delenv("ETSY_CLIENT_ID", raising=False)
    with pytest.raises(oauth_tokens.OAuthError):
        oauth_tokens.authorize_url("etsy", "state", "https://example.com/cb")


def test_exchange_code_stores_the_resulting_tokens(http):
    http.response = httpx.Response(200, json={"access_token": "AT-exchanged", "refresh_token": "RT-exchanged",
                                              "expires_in": 1800})
    verifier = "v" * 64
    oauth_tokens.exchange_code("etsy", "auth-code-123", verifier, "https://example.com/cb/etsy", by="ada")
    info = oauth_tokens.status()["etsy"]
    assert info["connected"] is True and info["connected_by"] == "ada"
    stored = json.loads(oauth_tokens.token_path().read_text())
    assert stored["etsy"]["refresh_token"] == "RT-exchanged"
    assert stored["etsy"]["access_token"] == "AT-exchanged"
    assert len(http.calls) == 1
    sent = http.calls[0]["data"]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "auth-code-123"
    assert sent["code_verifier"] == verifier


def test_exchange_code_rejects_a_too_short_verifier(http):
    with pytest.raises(oauth_tokens.OAuthError):
        oauth_tokens.exchange_code("etsy", "auth-code-123", "too-short", "https://example.com/cb/etsy")
    assert not http.calls  # rejected before any network call


def test_exchange_code_requires_a_refresh_token_in_the_response(http):
    http.response = httpx.Response(200, json={"access_token": "AT-only"})
    with pytest.raises(oauth_tokens.OAuthError):
        oauth_tokens.exchange_code("etsy", "auth-code-123", "v" * 64, "https://example.com/cb/etsy")


# --------------------------------------------------------------------------- status() never leaks a token

def test_status_never_includes_token_values_or_extra_keys(http):
    oauth_tokens.set_refresh_token("etsy", "status-must-not-show-this-token", "ada")
    oauth_tokens.access_token("etsy")
    dump = json.dumps(oauth_tokens.status())
    assert "status-must-not-show-this-token" not in dump
    assert "AT-1" not in dump  # the access token itself is also never surfaced
    assert "refresh_token" not in dump and "access_token" not in dump
    assert set(oauth_tokens.status()["etsy"]) == {"connected", "expires_at", "last_refreshed", "last_error",
                                                  "connected_at", "connected_by"}


def test_status_lists_every_configured_provider_even_when_disconnected():
    assert set(oauth_tokens.status()) == set(oauth_tokens.PROVIDERS)
    assert all(info["connected"] is False for info in oauth_tokens.status().values())


# --------------------------------------------------------------------------- dashboard tab (AppTest)

def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def _conn_tab_named() -> None:
    from _ops_connections import render_connections_tab

    render_connections_tab("Ada")


def _conn_tab_blank() -> None:
    from _ops_connections import render_connections_tab

    render_connections_tab("")


def test_connections_tab_missing_name_blocks_and_shows_nothing():
    at = AppTest.from_function(_conn_tab_blank).run()
    assert not at.exception, _messages(at)
    assert any("Enter your name" in w.value for w in at.warning)
    assert not at.button


def test_connections_tab_lists_every_provider_as_not_connected():
    at = AppTest.from_function(_conn_tab_named).run()
    assert not at.exception, _messages(at)
    markdown = " ".join(m.value for m in at.markdown)
    assert "Etsy" in markdown and "Google Ads" in markdown and "Meta" in markdown
    assert at.text_input(key="_conn_token_etsy")
    assert at.button(key="_conn_save_etsy").disabled  # nothing typed yet


def test_connections_tab_save_button_stays_disabled_for_an_implausible_token():
    at = AppTest.from_function(_conn_tab_named).run()
    at.text_input(key="_conn_token_etsy").set_value("short")
    at = at.run()
    assert not at.exception, _messages(at)
    assert at.button(key="_conn_save_etsy").disabled
    assert any("Not saved" in c.value for c in at.caption)


def test_connections_tab_save_button_stays_disabled_for_a_token_with_spaces():
    at = AppTest.from_function(_conn_tab_named).run()
    at.text_input(key="_conn_token_etsy").set_value("this has spaces in it")
    at = at.run()
    assert not at.exception, _messages(at)
    assert at.button(key="_conn_save_etsy").disabled


def test_connections_tab_save_flow_connects_with_a_valid_token():
    at = AppTest.from_function(_conn_tab_named).run()
    at.text_input(key="_conn_token_etsy").set_value("a-perfectly-plausible-refresh-token")
    at = at.run()
    assert not at.button(key="_conn_save_etsy").disabled
    at.button(key="_conn_save_etsy").click().run()
    assert not at.exception, _messages(at)
    # the success toast is transient (immediately followed by st.rerun(), see _ops_registry.py's revoke test for
    # the same behaviour); check the functional outcome instead
    assert oauth_tokens.status()["etsy"]["connected"] is True


def test_connections_tab_never_renders_a_token_value():
    oauth_tokens.set_refresh_token("etsy", "must-never-appear-on-the-page", "ada")
    at = AppTest.from_function(_conn_tab_named).run()
    assert not at.exception, _messages(at)
    full_text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "must-never-appear-on-the-page" not in full_text


def test_connections_tab_disconnect_flow(monkeypatch):
    import _ops_connections

    # st.dialog cannot be driven across AppTest reruns (same limitation noted in test_dashboard_ops_review.py);
    # call on_confirm straight away to exercise the real disconnect path.
    monkeypatch.setattr(_ops_connections, "confirm_dialog", lambda title, body, on_confirm, **kw: on_confirm())
    oauth_tokens.set_refresh_token("etsy", "already-connected-token-value", "ada")
    at = AppTest.from_function(_conn_tab_named).run()
    assert at.button(key="_conn_disconnect_etsy")
    at.button(key="_conn_disconnect_etsy").click().run()
    assert not at.exception, _messages(at)
    assert oauth_tokens.status()["etsy"]["connected"] is False
