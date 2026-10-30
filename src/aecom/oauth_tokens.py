"""F2: OAuth token store for ad/marketplace platforms (Etsy, Google Ads, Meta) that hand out a refresh token once
and expect it to be exchanged for a short-lived access token as needed.

Why not `.env` or the database:
- `.env` is parsed once into `SecretsProvider` at process start (`aecom.secrets`); a refresh can rotate the refresh
  token (and always rotates the access token), and nothing re-reads `.env` mid-process. It is also rewritten
  wholesale by the dashboard Settings page's safe writer (`aecom.envfile`), which is for human-edited config, not a
  token this module itself updates several times an hour.
- The database is backed up (`aecom.backup`) and those backups are handed to a human/off-device target; a backup
  must never carry live credentials. `backup.create_backup` only ever copies the two named database files, so
  anything stored here is automatically excluded, but to make that robust against mistakes `_write_store` below
  still refuses to write under `get_settings().backup_dir`.

So tokens live in their own small JSON file, `<ROOT>/data/oauth_tokens.json` by default (path overridable by the
`OAUTH_TOKEN_FILE` env var, for tests), written with the same discipline as `envfile.set_values`: a temp file in
the same directory, `os.chmod(0o600)`, then `os.replace` (atomic on the same filesystem). One file, one record per
provider: `refresh_token`, `access_token`, `expires_at`, `last_refreshed`, `last_error`, `connected_at`,
`connected_by`. Nothing here ever reaches a log, an audit row or `observability.record_error` in the clear -
`_token_request` masks the request's secret values first, then runs `observability.mask` as a second line of
defense, matching `aecom.integrations.ping`'s pattern.

Providers are a small, easy-to-extend table (`PROVIDERS`): name, authorize/token URLs, scopes, and the env var
names of the client id/secret, read through `aecom.secrets` at call time (never cached). `access_token(provider)`
refreshes through the provider's token endpoint via `aecom.net_guard.pinned_post` (the same SSRF-guarded, IP-pinned
POST `channels.whatsapp` uses) whenever the stored access token is missing or within `EXPIRY_SKEW_S` of expiry, and
tolerates providers that rotate the refresh token by saving the new one before returning the access token. A
per-provider `threading.Lock` makes concurrent callers within one process share a single in-flight refresh instead
of each firing their own request.

`authorize_url`/`exchange_code` build the human consent step with PKCE (RFC 7636, S256) for a future redirect
handler; today's actual connect flow is simpler and entirely human-driven: the dashboard's Connections tab shows
the consent link, the human signs in on the provider's page and pastes the refresh token it hands back, and
`set_refresh_token` stores it (audited, the token itself never logged).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from aecom.audit import audit
from aecom.config import ROOT, get_settings
from aecom.net_guard import pinned_post, resolve_public
from aecom.observability import mask, record_error
from aecom.secrets import secrets as secret_store

log = logging.getLogger(__name__)

EXPIRY_SKEW_S = 60          # refresh this many seconds before actual expiry, not exactly at it
DEFAULT_EXPIRES_IN_S = 3600  # assumed lifetime when a token response omits `expires_in` (rare, but seen in the wild)
MIN_VERIFIER_LENGTH = 43     # RFC 7636 4.1: code_verifier is 43-128 unreserved characters


class OAuthError(Exception):
    """Any OAuth token-store failure. `str(exc)` is already masked and safe to show in the dashboard or logs."""


@dataclass(frozen=True)
class Provider:
    name: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id_env: str
    client_secret_env: str | None = None  # some providers (Etsy's public PKCE clients) have no client secret


# Add a provider here only: `access_token`/`authorize_url`/`exchange_code`/`status` all drive off this table.
PROVIDERS: dict[str, Provider] = {
    "etsy": Provider(
        name="etsy",
        authorize_url="https://www.etsy.com/oauth/connect",
        token_url="https://api.etsy.com/v3/public/oauth/token",
        scopes=("listings_r", "listings_w", "transactions_r"),
        client_id_env="ETSY_CLIENT_ID",
        client_secret_env=None,  # Etsy's v3 OAuth is PKCE-only for its public API clients
    ),
    "google_ads": Provider(
        name="google_ads",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=("https://www.googleapis.com/auth/adwords",),
        client_id_env="GOOGLE_ADS_CLIENT_ID",
        client_secret_env="GOOGLE_ADS_CLIENT_SECRET",
    ),
    "meta": Provider(
        name="meta",
        authorize_url="https://www.facebook.com/v21.0/dialog/oauth",
        token_url="https://graph.facebook.com/v21.0/oauth/access_token",
        scopes=("ads_management", "business_management"),
        client_id_env="META_APP_ID",
        client_secret_env="META_APP_SECRET",
    ),
}


def _require_provider(provider: str) -> Provider:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise OAuthError(f"unknown OAuth provider: {provider!r}") from None


# --------------------------------------------------------------------------- storage (see module docstring)

def token_path() -> Path:
    override = os.environ.get("OAUTH_TOKEN_FILE")
    return Path(override) if override else ROOT / "data" / "oauth_tokens.json"


def _read_store() -> dict[str, dict[str, Any]]:
    path = token_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("oauth token file at %s is unreadable; treating it as empty", path)
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(data: dict[str, dict[str, Any]]) -> None:
    path = token_path()
    backup_root = Path(get_settings().backup_dir).resolve()
    resolved = path.resolve()
    if resolved == backup_root or backup_root in resolved.parents:
        raise OAuthError("refusing to write OAuth tokens inside the backup directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".oauth_tokens.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# One lock guards the brief read-modify-write of the whole file (cheap, held only around actual I/O); one lock per
# provider guards the "decide whether to refresh, then refresh" critical section, so two threads needing the same
# provider's access token share a single in-flight HTTP call while different providers refresh independently.
_file_lock = threading.Lock()
_provider_locks: dict[str, threading.Lock] = {}
_provider_locks_guard = threading.Lock()


def _provider_lock(provider: str) -> threading.Lock:
    with _provider_locks_guard:
        return _provider_locks.setdefault(provider, threading.Lock())


def _load_record(provider: str) -> dict[str, Any]:
    with _file_lock:
        return dict(_read_store().get(provider, {}))


def _save_record(provider: str, record: dict[str, Any]) -> None:
    with _file_lock:
        store = _read_store()
        store[provider] = record
        _write_store(store)


def _delete_record(provider: str) -> None:
    with _file_lock:
        store = _read_store()
        store.pop(provider, None)
        _write_store(store)


# --------------------------------------------------------------------------- time helpers

def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _still_valid(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    return expiry - timedelta(seconds=EXPIRY_SKEW_S) > _now()


# --------------------------------------------------------------------------- the token endpoint call

def _scrub(text: str, *secret_values: str | None) -> str:
    """Like `aecom.integrations.ping._scrub`: every given secret value replaced first, then `mask()` as a second
    line of defense for anything it recognises by shape or env-var naming convention."""
    for value in secret_values:
        if value:
            text = text.replace(value, "***")
    return mask(text)


def _token_request(p: Provider, data: dict[str, str], *secret_values: str | None) -> dict[str, Any]:
    """POST `data` (form-encoded) to `p.token_url` through `net_guard`'s SSRF-guarded, IP-pinned POST. Raises
    `OAuthError` (already masked) on any non-2xx response or network failure, and records a masked ops error -
    the token/code/verifier/secret values in `secret_values` never reach either."""
    try:
        target = resolve_public(p.token_url)
        response = pinned_post(target, data=data, headers={"Accept": "application/json"})
        response.raise_for_status()
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - a provider outage/rejection is routine; it must never leak a token
        detail = _scrub(f"{type(exc).__name__}: {exc}", *secret_values)
        log.warning("oauth_tokens.%s token request failed: %s", p.name, detail)
        record_error(f"oauth_tokens.{p.name}", RuntimeError(detail), context={"provider": p.name})
        raise OAuthError(f"{p.name}: token request failed ({detail})") from None
    if not isinstance(body, dict):
        raise OAuthError(f"{p.name}: token endpoint returned a non-object response")
    return body


def _refresh(p: Provider, record: dict[str, Any]) -> str:
    """Actually call the token endpoint and persist the result. Caller holds `_provider_lock(p.name)`."""
    client_id = secret_store.get(p.client_id_env)
    if not client_id:
        raise OAuthError(f"{p.client_id_env} is not set")
    client_secret = secret_store.get(p.client_secret_env) if p.client_secret_env else None
    refresh_token = record.get("refresh_token")
    if not refresh_token:
        raise OAuthError(f"{p.name} is not connected")
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    if client_secret:
        data["client_secret"] = client_secret
    try:
        body = _token_request(p, data, refresh_token, client_secret)
        access_token = body.get("access_token")
        if not access_token:
            raise OAuthError(f"{p.name}: token response had no access_token")
    except OAuthError as exc:
        _save_record(p.name, {**record, "last_error": str(exc)})
        raise
    expires_in = float(body.get("expires_in") or DEFAULT_EXPIRES_IN_S)
    # Tolerate rotation: some providers (Google included) issue a new refresh token on every refresh and invalidate
    # the old one. The new one is saved before this function returns the access token to the caller.
    updated = {**record, "refresh_token": body.get("refresh_token") or refresh_token, "access_token": access_token,
              "expires_at": _iso(_now() + timedelta(seconds=expires_in)), "last_refreshed": _iso(_now()),
              "last_error": None}
    _save_record(p.name, updated)
    return access_token


def access_token(provider: str) -> str:
    """A currently-valid access token for `provider`, refreshing through its token endpoint if the stored one is
    missing or within `EXPIRY_SKEW_S` of expiry. Raises `OAuthError` if the provider is not connected or the
    refresh fails. At most one refresh per provider is in flight per process; other callers block on the lock and
    then see its already-refreshed result instead of each making their own request."""
    p = _require_provider(provider)
    with _provider_lock(p.name):
        record = _load_record(p.name)
        if not record.get("refresh_token"):
            raise OAuthError(f"{p.name} is not connected")
        cached = record.get("access_token")
        if cached and _still_valid(record.get("expires_at")):
            return cached
        return _refresh(p, record)


def set_refresh_token(provider: str, token: str, by: str) -> None:
    """Store a refresh token the human pasted in after consenting on the provider's page. Audited as `by`; the
    token value itself is never part of the audit detail (only the provider name)."""
    p = _require_provider(provider)
    token = (token or "").strip()
    if not token:
        raise OAuthError("the refresh token must not be empty")
    if any(ch.isspace() for ch in token):
        raise OAuthError("the refresh token must not contain whitespace")
    with _provider_lock(p.name):
        record = _load_record(p.name)
        record.update(refresh_token=token, access_token=None, expires_at=None, last_error=None, last_refreshed=None,
                     connected_at=record.get("connected_at") or _iso(_now()), connected_by=by)
        _save_record(p.name, record)
    audit(f"human:{by}", "oauth_tokens", "connected", {"provider": p.name})


def disconnect(provider: str, by: str) -> None:
    """Delete the stored tokens for `provider`. Audited as `by`."""
    p = _require_provider(provider)
    with _provider_lock(p.name):
        _delete_record(p.name)
    audit(f"human:{by}", "oauth_tokens", "disconnected", {"provider": p.name})


def status() -> dict[str, dict[str, Any]]:
    """Per provider: `connected`, `expires_at`, `last_refreshed`, `last_error`, `connected_at`, `connected_by`.
    Never the token itself - this is the only thing the dashboard's Connections tab reads."""
    with _file_lock:
        store = _read_store()
    return {name: {"connected": bool(store.get(name, {}).get("refresh_token")),
                  "expires_at": store.get(name, {}).get("expires_at"),
                  "last_refreshed": store.get(name, {}).get("last_refreshed"),
                  "last_error": store.get(name, {}).get("last_error"),
                  "connected_at": store.get(name, {}).get("connected_at"),
                  "connected_by": store.get(name, {}).get("connected_by")}
           for name in PROVIDERS}


# --------------------------------------------------------------------------- PKCE consent flow (RFC 7636)

def new_pkce_pair() -> tuple[str, str]:
    """A fresh `(code_verifier, code_challenge)` pair: `code_verifier` is a random 128-character URL-safe string,
    `code_challenge` its S256 transform (base64url, no padding, of its SHA-256 digest) - the only method every
    provider in `PROVIDERS` supports."""
    verifier = base64.urlsafe_b64encode(os.urandom(96)).rstrip(b"=").decode("ascii")[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(provider: str, state: str, redirect_uri: str) -> tuple[str, str]:
    """`(url, code_verifier)` for the provider's consent page. The caller (the dashboard's Connections tab, or a
    future redirect handler) must hold on to `code_verifier` to pass to `exchange_code` once the provider redirects
    back with a `code`."""
    p = _require_provider(provider)
    client_id = secret_store.get(p.client_id_env)
    if not client_id:
        raise OAuthError(f"{p.client_id_env} is not set")
    verifier, challenge = new_pkce_pair()
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
             "scope": " ".join(p.scopes), "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256"}
    return f"{p.authorize_url}?{urlencode(params)}", verifier


def exchange_code(provider: str, code: str, verifier: str, redirect_uri: str, by: str = "oauth-redirect") -> None:
    """Exchange an authorization `code` (from the provider's redirect) plus its PKCE `verifier` for tokens, and
    store them - the counterpart `authorize_url` sets up. Not wired to a route yet (no public redirect handler
    exists), but usable and tested offline so one can be added later without touching this module."""
    p = _require_provider(provider)
    if len(verifier) < MIN_VERIFIER_LENGTH:
        raise OAuthError("code_verifier is too short to be a valid PKCE verifier")
    client_id = secret_store.get(p.client_id_env)
    if not client_id:
        raise OAuthError(f"{p.client_id_env} is not set")
    client_secret = secret_store.get(p.client_secret_env) if p.client_secret_env else None
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "client_id": client_id,
           "code_verifier": verifier}
    if client_secret:
        data["client_secret"] = client_secret
    body = _token_request(p, data, code, verifier, client_secret)
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        raise OAuthError(f"{p.name}: token response had no refresh_token")
    access_tok = body.get("access_token")
    expires_in = float(body.get("expires_in") or DEFAULT_EXPIRES_IN_S)
    with _provider_lock(p.name):
        record = _load_record(p.name)
        record.update(refresh_token=refresh_token, access_token=access_tok,
                     expires_at=_iso(_now() + timedelta(seconds=expires_in)) if access_tok else None,
                     last_refreshed=_iso(_now()) if access_tok else record.get("last_refreshed"), last_error=None,
                     connected_at=record.get("connected_at") or _iso(_now()), connected_by=by)
        _save_record(p.name, record)
    audit(f"human:{by}" if by != "oauth-redirect" else "system", "oauth_tokens", "connected", {"provider": p.name})
