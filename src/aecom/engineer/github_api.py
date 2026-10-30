"""GitHub API access for the Engineer's PRs, plus the diff-size cap, secret scan and daily-cap bookkeeping that
gate ``eng_open_pr`` (``aecom.engineer.tools``).

The PR is created through :func:`aecom.net_guard.resolve_public` / :func:`aecom.net_guard.pinned_post`, the same
SSRF-safe path every outbound fetch in this app uses - even though the GitHub API host is fixed, an attacker who
could somehow influence what ``api.github.com`` resolves to still only ever reaches a public, validated address.
"""

from __future__ import annotations

import re
from typing import Any

from aecom.company_settings import get_setting, set_setting
from aecom.db import now
from aecom.engineer import workspace
from aecom.engineer.workspace import WorkspaceError
from aecom.net_guard import UnsafeURL, pinned_post, resolve_public
from aecom.observability import mask
from aecom.secrets import secrets

GITHUB_API = "https://api.github.com"
MAX_DIFF_LINES = 800
DEFAULT_DAILY_CAP = 3
DAILY_CAP_SETTING = "engineer_pr_daily_cap"
DISCLAIMER = "Opened by the Engineer agent. Not deployed. A human must review and merge."

# Beyond `aecom.observability.mask` (Bearer tokens, *_KEY/*_TOKEN/*_SECRET/*_DSN env values, am_us_/sk-ant- keys):
# a few more key/token shapes a generated adapter might accidentally hard-code.
_SECRET_PATTERNS = [
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),               # GitHub personal access token
    re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}\b"),         # other GitHub token prefixes
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                   # AWS access key id
    re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+=]{12,}[\"']?"),
]


def secret_like(text: str) -> str | None:
    """A short reason if ``text`` looks like it carries a secret, else None.

    Reuses ``observability.mask``'s known shapes (anything that pattern would redact already disqualifies a diff)
    plus a few key/token shapes that are common in hand-written integration code but not in this app's own secrets.
    """
    if mask(text) != text:
        return "matches a known secret shape (observability.mask)"
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            return "looks like an embedded API key, token or private key"
    return None


def daily_cap() -> int:
    raw = get_setting(DAILY_CAP_SETTING)
    if raw is not None:
        try:
            return max(int(raw), 0)
        except ValueError:
            pass
    from aecom.policy.gate import load_policy

    return int(load_policy()["company_settings_defaults"].get(DAILY_CAP_SETTING, DEFAULT_DAILY_CAP))


def _today() -> str:
    return now().date().isoformat()


def prs_opened_today() -> int:
    return int(get_setting(f"engineer_pr_count:{_today()}") or 0)


def record_pr_opened(branch: str) -> None:
    set_setting(f"engineer_pr_count:{_today()}", str(prs_opened_today() + 1))
    workspace.mark_branch_open(branch)


def create_pull_request(repo: str, title: str, body: str, head: str, base: str) -> dict[str, Any]:
    """Opens the PR via the GitHub REST API. The disclaimer is always appended, even if the caller's body already
    has one, so it can never be omitted by an approval that edited the body."""
    token = secrets.get("GITHUB_TOKEN")
    if not token:
        raise WorkspaceError("GITHUB_TOKEN is not set; cannot open a PR")
    full_body = f"{body.rstrip()}\n\n---\n{DISCLAIMER}\n"
    url = f"{GITHUB_API}/repos/{repo}/pulls"
    try:
        target = resolve_public(url)
        r = pinned_post(target, json={"title": title, "body": full_body, "head": head, "base": base},
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                 "X-GitHub-Api-Version": "2022-11-28"}, timeout=20)
        r.raise_for_status()
    except UnsafeURL as exc:
        raise WorkspaceError(f"refused: {exc}") from None
    except Exception as exc:  # httpx.HTTPError and friends
        raise WorkspaceError(f"GitHub PR creation failed: {mask(str(exc))[:300]}") from exc
    return r.json()
