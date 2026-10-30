# Govern > Operations > Connections tab (F2): OAuth connections for ad/marketplace platforms (Etsy, Google Ads,
# Meta). `render_connections_tab(by)` is imported and called by operations.py (not defined there).
#
# Connecting is entirely human-driven: no redirect handler exists yet, so this shows the provider's consent link
# (built with PKCE by `aecom.oauth_tokens.authorize_url`) and a write-only field for the refresh token the human
# gets back after approving access on the provider's page. The token is validated inline (not empty, no
# whitespace, a plausible length) before `oauth_tokens.set_refresh_token` stores it; this module never reads,
# displays or logs the token itself, only `oauth_tokens.status()`'s connected/expires_at/last_refreshed/last_error.

from __future__ import annotations

import uuid

import streamlit as st

from _common import confirm_dialog, empty_state, status_badge
from aecom import oauth_tokens
from aecom.config import get_settings

MIN_TOKEN_LENGTH = 15  # a plausible lower bound for a refresh token; catches an obvious mis-paste, not a real check


def _redirect_uri(provider: str) -> str:
    """No public redirect handler exists yet (see `oauth_tokens.exchange_code`'s docstring); this is the URI a
    future one would register under, kept stable so a consent link generated today still works once it exists."""
    return f"{get_settings().api_base_url}/oauth/callback/{provider}"


def _token_problem(token: str) -> str | None:
    if any(ch.isspace() for ch in token):
        return "it must not contain spaces."
    if len(token) < MIN_TOKEN_LENGTH:
        return f"that looks too short for a refresh token (at least {MIN_TOKEN_LENGTH} characters)."
    return None


def _connect_form(provider: str, by: str) -> None:
    state_key = f"_conn_state_{provider}"
    state = st.session_state.setdefault(state_key, uuid.uuid4().hex)
    try:
        url, _verifier = oauth_tokens.authorize_url(provider, state, _redirect_uri(provider))
    except oauth_tokens.OAuthError as exc:
        st.warning(str(exc))
        return
    st.link_button("Open the consent page", url, icon=":material/open_in_new:")
    st.caption("Sign in there, approve access, then paste the refresh token it gives you back below.")
    token_key = f"_conn_token_{provider}"
    token = st.text_input("Refresh token", key=token_key, type="password").strip()
    problem = _token_problem(token) if token else None
    if problem:
        st.caption(f":material/error: Not saved: {problem}")
    if st.button("Save", key=f"_conn_save_{provider}", disabled=not by or not token or bool(problem),
                icon=":material/save:"):
        try:
            oauth_tokens.set_refresh_token(provider, token, by)
        except oauth_tokens.OAuthError as exc:
            st.error(str(exc))
            return
        st.success(f"{provider} connected.")
        st.session_state.pop(token_key, None)
        st.rerun()


def _do_disconnect(provider: str, by: str) -> None:
    oauth_tokens.disconnect(provider, by)
    st.success(f"{provider} disconnected.")
    st.rerun()


def _card(provider: str, info: dict, by: str) -> None:
    with st.container(border=True):
        top = st.columns([3, 2])
        top[0].markdown(f"### {provider.replace('_', ' ').title()}")
        if info["connected"]:
            status_badge("active", "Connected")
        else:
            status_badge("disabled", "Not connected")
        if info["connected"]:
            if info.get("last_refreshed"):
                st.caption(f"Last refreshed: {info['last_refreshed']}")
            else:
                st.caption("Not refreshed yet.")
            if info.get("last_error"):
                st.warning(info["last_error"], icon=":material/error:")
            if st.button("Disconnect", key=f"_conn_disconnect_{provider}", disabled=not by,
                        icon=":material/link_off:"):
                confirm_dialog(f"Disconnect {provider}?",
                              "The stored refresh token is deleted immediately. Any agent call that needs it "
                              "fails until it is reconnected.", lambda: _do_disconnect(provider, by),
                              confirm_label="Disconnect")
        else:
            with st.expander("Connect", icon=":material/link:"):
                _connect_form(provider, by)


def render_connections_tab(by: str) -> None:
    st.caption("OAuth connections for ad and marketplace platforms. A refresh token never shows here once saved - "
              "only whether the provider is connected, when it last refreshed and its last error, if any.")
    if not by:
        st.warning("Enter your name in the sidebar first.")
        return
    statuses = oauth_tokens.status()
    if not statuses:
        empty_state("link_off", "No OAuth providers configured")
        return
    for provider, info in statuses.items():
        _card(provider, info, by)
