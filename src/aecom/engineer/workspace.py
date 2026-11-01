"""The Engineer's own git workspace: a separate clone, confined paths, one branch at a time, and a test runner.

Nothing here trusts the prompt. Every function raises :class:`WorkspaceError` (a plain ``RuntimeError``) with a
short, model-safe reason instead of silently doing something unsafe.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from aecom.company_settings import get_setting, set_setting
from aecom.config import ROOT
from aecom.context import current_run
from aecom.db import now
from aecom.secrets import secrets

GIT_TIMEOUT = 30.0          # seconds for an ordinary git call
NETWORKED_GIT_TIMEOUT = 120.0  # clone / fetch / push: still local-disk-only in tests, just slower
TEST_TIMEOUT_DEFAULT = 300.0
TEST_OUTPUT_CAP = 20_000     # characters of stdout/stderr kept per stream
AUTHOR_NAME = "aecom-engineer"
AUTHOR_EMAIL = "engineer@aecom.local"
_TEST_RESULT_KEY = "engineer_last_test_pass"
OPEN_BRANCH_KEY = "engineer_open_branch"

# Writes are confined to adapters/webhooks/tests/one integrations doc (E3). Prefixes end in "/": a prefix never
# matches a sibling file that merely starts with the same characters (e.g. "src/aecom/store_x.py").
ALLOWED_PREFIXES = ("src/aecom/store/", "src/aecom/integrations/", "src/aecom/channels/", "tests/")
ALLOWED_EXACT = ("src/aecom/api/webhooks.py", "docs/wiki/Integrations.md")

# Always denied, even if a future allowlist entry would otherwise match (checked first, in `denied_path_reason`).
DENIED_PREFIXES = ("migrations/", "scripts/", "deploy/", ".github/", "agents/", "src/aecom/policy/", ".git/")
DENIED_EXACT = ("pyproject.toml", "uv.lock", "src/aecom/db.py", "src/aecom/config.py", "src/aecom/secrets.py",
               "src/aecom/settings_registry.py", "src/aecom/dashboard_auth.py", "src/aecom/net_guard.py")
_ENV_FILE = re.compile(r"(?:^|/)\.env(?:\..*)?$")  # .env, .env.local, .env.whatever - never a path component's suffix


class WorkspaceError(RuntimeError):
    """A workspace / git operation refused or failed. ``str(exc)`` is short and safe to show the model."""


# --------------------------------------------------------------------------------------------------- where / clone

def workspace_root() -> Path:
    """``<ROOT>/data/engineer_workspace``, or ``ENGINEER_WORKSPACE_DIR`` when set (tests point this at tmp_path so
    the real checkout's ``data/`` directory is never touched by a test run)."""
    override = secrets.get("ENGINEER_WORKSPACE_DIR")
    return Path(override).resolve() if override else (ROOT / "data" / "engineer_workspace").resolve()


def _clone_source() -> tuple[str, bool]:
    """(source, local): ``local=True`` clones ``--local`` from this checkout; otherwise ``source`` is the explicit
    ``ENGINEER_REPO_URL`` (a real remote in production, a local bare repo path in tests)."""
    url = secrets.get("ENGINEER_REPO_URL")
    return (url, False) if url else (str(ROOT), True)


def _run(args: list[str], cwd: Path | None = None, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"command timed out after {timeout:.0f}s: {' '.join(args)}") from exc


def _git(ws: Path, *args: str, timeout: float = GIT_TIMEOUT) -> str:
    r = _run(["git", *args], cwd=ws, timeout=timeout)
    if r.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:500]}")
    return r.stdout


def ensure_workspace() -> Path:
    """The Engineer's own clone, cloned on first use. Never the live checkout - always a distinct ``git clone``
    with its own ``.git``, so nothing the Engineer does ever touches the running app's working tree."""
    ws = workspace_root()
    if (ws / ".git").is_dir():
        return ws
    if ws.exists() and any(ws.iterdir()):
        raise WorkspaceError(f"{ws} exists and is not a git repository; move it aside or set ENGINEER_WORKSPACE_DIR")
    ws.parent.mkdir(parents=True, exist_ok=True)
    source, local = _clone_source()
    args = ["git", "clone", *(["--local"] if local else []), source, str(ws)]
    r = _run(args, timeout=NETWORKED_GIT_TIMEOUT)
    if r.returncode != 0:
        raise WorkspaceError(f"could not clone the Engineer workspace from {source!r}: "
                             f"{(r.stderr or r.stdout).strip()[:500]}")
    _git(ws, "config", "user.name", AUTHOR_NAME)
    _git(ws, "config", "user.email", AUTHOR_EMAIL)
    return ws


def default_branch(ws: Path) -> str:
    """The repo's default branch (``origin/HEAD``'s target), falling back to whatever is currently checked out."""
    r = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=ws)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rsplit("/", 1)[-1]
    return current_branch(ws) or "main"


def current_branch(ws: Path) -> str:
    return _git(ws, "branch", "--show-current").strip()


def head_sha(ws: Path) -> str:
    return _git(ws, "rev-parse", "HEAD").strip()


def assert_engineer_branch(branch: str) -> None:
    if branch in ("main", "master") or not branch.startswith("engineer/"):
        raise WorkspaceError(f"refusing to act on branch '{branch}': work happens only on an engineer/<slug> branch")


# --------------------------------------------------------------------------------------------------- one branch at a time

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug_for_run() -> str:
    """One branch per agent run (`current_run`), so repeated tool calls in the same task share a branch."""
    run_id = current_run.get()
    if not run_id:
        raise WorkspaceError("eng_write_file needs a current run id to name its branch")
    slug = _SLUG_RE.sub("-", run_id.lower()).strip("-")
    return slug or "work"


def open_branch() -> str:
    """The branch currently reserved (`ops_settings["engineer_open_branch"]`), or ""."""
    return get_setting(OPEN_BRANCH_KEY) or ""


def mark_branch_open(branch: str) -> None:
    """Record ``branch`` as the one open engineer branch (idempotent: a PR re-opened on the same branch is fine)."""
    set_setting(OPEN_BRANCH_KEY, branch)


def clear_open_branch(by: str) -> None:
    """Human-only (no agent tool calls this): release the one-open-branch slot after a PR is merged or dropped."""
    from aecom.audit import audit

    set_setting(OPEN_BRANCH_KEY, "")
    audit(f"human:{by}", "engineer_open_branch", "cleared", {})


def _reserve_branch(slug: str) -> str:
    branch = f"engineer/{slug}"
    current = open_branch()
    if current and current != branch:
        raise WorkspaceError(f"another engineer branch '{current}' is already open; a human must close it "
                             "(merge or drop it, then clear it) before starting new work")
    if not current:
        set_setting(OPEN_BRANCH_KEY, branch)
    return branch


def ensure_branch(ws: Path) -> str:
    """The branch for the current run: reused if already checked out, else freshly cut from the latest base."""
    branch = _reserve_branch(_slug_for_run())
    if current_branch(ws) == branch:
        return branch
    _git(ws, "fetch", "origin", timeout=NETWORKED_GIT_TIMEOUT)
    base = default_branch(ws)
    exists_locally = _run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=ws).returncode == 0
    if exists_locally:
        _git(ws, "checkout", branch)
    else:
        _git(ws, "checkout", "-B", branch, f"origin/{base}")
    return branch


# --------------------------------------------------------------------------------------------------- path containment

def normalize_path(raw: str) -> str:
    """A clean, repo-relative, forward-slash path, or a `WorkspaceError` - no absolute paths, no ``..``, no NUL."""
    if not raw or not isinstance(raw, str):
        raise WorkspaceError("empty path")
    if "\x00" in raw:
        raise WorkspaceError("path contains a NUL byte")
    posix = raw.replace("\\", "/").strip()
    if posix.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", posix):
        raise WorkspaceError(f"path '{raw}' must be relative to the workspace root")
    parts = [p for p in posix.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise WorkspaceError(f"path '{raw}' must not contain '..'")
    if not parts:
        raise WorkspaceError(f"path '{raw}' is empty")
    return "/".join(parts)


def resolve_in_workspace(ws: Path, raw: str) -> Path:
    """``raw``, confined to ``ws``; raises if any part of the path (the file or a parent folder) is a symlink, or if
    it would resolve outside the workspace. The symlink test runs on the UNRESOLVED path: ``resolve()`` follows
    links first, so testing afterwards could never fire (a link inside an allowed folder pointing at a denied
    file would pass as that denied file's real path)."""
    rel = normalize_path(raw)
    root = ws.resolve()
    probe = root
    for part in Path(rel).parts:
        probe = probe / part
        if probe.is_symlink():
            raise WorkspaceError(f"path '{raw}' goes through a symlink ('{part}'); refusing to follow it")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise WorkspaceError(f"path '{raw}' resolves outside the workspace") from None
    return candidate


def denied_path_reason(rel: str) -> str | None:
    """Why `eng_write_file` (or a PR's diff) must never touch `rel`, or None. Checked before the allowlist, and
    again on the full diff at `eng_open_pr` time, so nothing denied reaches a PR even via an older commit."""
    if _ENV_FILE.search(rel):
        return "secret file (.env*)"
    if rel in DENIED_EXACT or any(rel.startswith(p) for p in DENIED_PREFIXES):
        return "denied path"
    return None


def denied_read_reason(rel: str) -> str | None:
    """Reads are broad (the Engineer needs context from the whole repo) but never secrets or git internals."""
    if _ENV_FILE.search(rel):
        return "secret file (.env*)"
    if rel == ".git" or rel.startswith(".git/"):
        return "git internals"
    return None


def is_write_allowed(rel: str) -> bool:
    if denied_path_reason(rel):
        return False
    return rel in ALLOWED_EXACT or any(rel.startswith(p) for p in ALLOWED_PREFIXES)


# --------------------------------------------------------------------------------------------------- reads

def list_files(ws: Path, prefix: str = "") -> list[str]:
    out = _git(ws, "ls-files")
    files = [line for line in out.splitlines() if line]
    if prefix:
        files = [f for f in files if f == prefix or f.startswith(prefix.rstrip("/") + "/")]
    return sorted(f for f in files if not denied_read_reason(f))


def search(ws: Path, query: str) -> list[str]:
    if not query or not query.strip():
        raise WorkspaceError("empty search query")
    r = _run(["git", "grep", "-n", "-I", "--fixed-strings", "-e", query], cwd=ws)
    if r.returncode not in (0, 1):  # 1 = no matches, not an error
        raise WorkspaceError(f"search failed: {(r.stderr or r.stdout).strip()[:300]}")
    out = []
    for line in r.stdout.splitlines():
        if not line:
            continue
        path = line.split(":", 1)[0]
        if not denied_read_reason(path):
            out.append(line)
    return out


# --------------------------------------------------------------------------------------------------- write + commit

# Code that reaches for the owner's secret files or the process environment has no place in an adapter or a test:
# the nested test run is the same OS user as the app, so this is defence in depth, not a sandbox (see Engineer-Agent.md).
_FORBIDDEN_CONTENT = re.compile(
    r"(?i)(\.env\b|oauth_tokens|\.ssh\b|id_rsa|id_ed25519|os\.environ|os\.getenv|getenv\(|subprocess|"
    r"\bsocket\b|/etc/passwd|aecom\.secrets|\bsecrets\.get\b|data/aecom\.db|data/backups)")


def _forbidden_content(content: str) -> str | None:
    if (m := _FORBIDDEN_CONTENT.search(content)):
        return f"it mentions '{m.group(0)}' (secret files, the environment, processes and raw sockets are off limits)"
    return None


def write_file(ws: Path, rel_raw: str, content: str) -> dict:
    rel = normalize_path(rel_raw)
    if not is_write_allowed(rel):
        reason = denied_path_reason(rel) or "not on the Engineer's write allowlist"
        raise WorkspaceError(f"refusing to write '{rel}': {reason}")
    if reason := _forbidden_content(content):
        raise WorkspaceError(f"refusing to write '{rel}': {reason}")
    target = resolve_in_workspace(ws, rel)  # validate the path before any branch or file is created
    branch = ensure_branch(ws)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(ws, "add", "--", rel)
    unchanged = _run(["git", "diff", "--cached", "--quiet", "--", rel], cwd=ws).returncode == 0
    if unchanged:
        return {"path": rel, "branch": branch, "committed": False, "note": "no change (content identical)"}
    _git(ws, "commit", "--author", f"{AUTHOR_NAME} <{AUTHOR_EMAIL}>", "-m", f"engineer: write {rel}")
    return {"path": rel, "branch": branch, "committed": True, "sha": head_sha(ws)}


# --------------------------------------------------------------------------------------------------- diff

def diff_paths(ws: Path, base: str | None = None) -> list[str]:
    base = base or f"origin/{default_branch(ws)}"
    out = _git(ws, "diff", "--name-only", f"{base}...HEAD")
    return [line for line in out.splitlines() if line]


def diff_stat(ws: Path, base: str | None = None) -> dict:
    base = base or f"origin/{default_branch(ws)}"
    out = _git(ws, "diff", "--numstat", f"{base}...HEAD")
    files = added = removed = 0
    for line in out.splitlines():
        if not line.strip():
            continue
        a, d, _path = line.split("\t", 2)
        files += 1
        added += int(a) if a.isdigit() else 0
        removed += int(d) if d.isdigit() else 0
    return {"files": files, "insertions": added, "deletions": removed}


def diff_text(ws: Path, base: str | None = None, max_chars: int = 20_000) -> str:
    base = base or f"origin/{default_branch(ws)}"
    out = _git(ws, "diff", f"{base}...HEAD")
    return out if len(out) <= max_chars else out[:max_chars] + "\n…[truncated]"


# --------------------------------------------------------------------------------------------------- push (never force, never another ref)

def _with_token(origin_url: str, token: str | None) -> str:
    if token and origin_url.startswith("https://github.com/"):
        return origin_url.replace("https://", f"https://x-access-token:{token}@", 1)
    return origin_url


def _scrub(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


def push_branch_only(ws: Path, branch: str) -> None:
    """Pushes exactly ``<branch>:<branch>`` to origin. No ``--force``, no other refspec is ever constructed, so
    nothing but this one branch can be touched - the refspec string itself never names another ref."""
    assert_engineer_branch(branch)
    token = secrets.get("GITHUB_TOKEN")
    origin = _git(ws, "remote", "get-url", "origin").strip()
    push_target = _with_token(origin, token)
    r = _run(["git", "push", push_target, f"{branch}:{branch}"], cwd=ws, timeout=NETWORKED_GIT_TIMEOUT)
    if r.returncode != 0:
        raise WorkspaceError(f"push failed: {_scrub((r.stderr or r.stdout).strip()[:500], token)}")


_REPO_RE = re.compile(r"github\.com[:/](?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$")


def target_repo(ws: Path) -> str:
    """``ENGINEER_REPO`` if set, else parsed from the origin remote URL."""
    repo = secrets.get("ENGINEER_REPO")
    if repo:
        return repo
    origin = _git(ws, "remote", "get-url", "origin").strip()
    m = _REPO_RE.search(origin)
    if not m:
        raise WorkspaceError(f"cannot determine the GitHub repo from origin '{origin}'; set ENGINEER_REPO")
    return m.group("repo")


# --------------------------------------------------------------------------------------------------- tests

def _env_for_tests(ws: Path) -> dict[str, str]:
    """A minimal, built-from-scratch environment: nothing inherited from the parent process is kept except a
    short allowlist needed to run `uv` at all - no *_KEY/*_TOKEN/*_SECRET/*_DSN, no ANTHROPIC_*, nothing of the
    kind ever reaches the child. A scripted LLM and a throwaway DB keep the nested suite fully offline.

    The child must not share the app's Python environment (``VIRTUAL_ENV`` would load the real installed package,
    so ``aecom.config.ROOT`` would be the live checkout, not this clone) nor the owner's real ``HOME`` (ssh keys,
    credentials): it gets its own project environment inside the workspace and a workspace-local HOME. Only the uv
    download cache is shared, read from the parent's real location."""
    keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SystemRoot")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    (ws / "data").mkdir(parents=True, exist_ok=True)
    home = ws / ".engineer_home"
    home.mkdir(exist_ok=True)
    real_home = Path(os.environ.get("HOME", "~")).expanduser()
    env["UV_CACHE_DIR"] = os.environ.get("UV_CACHE_DIR") or str(real_home / ".cache" / "uv")
    env["HOME"] = str(home)
    env["UV_PROJECT_ENVIRONMENT"] = str(ws / ".venv")
    env["LLM_MODE"] = "scripted"
    env["DATABASE_URL"] = f"sqlite:///{ws / 'data' / 'engineer_test.db'}"
    env["MCP_AUTOLOAD"] = "false"
    return env


def _invoke_pytest(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    """The actual subprocess call - a seam tests replace (a real nested ``uv run`` needs a synced venv)."""
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)


def _redact(text: str) -> str:
    """Test output goes back to the agent: mask anything secret-shaped and the real checkout's path."""
    from aecom.observability import mask

    return mask(text.replace(str(ROOT), "<app>"))


def run_tests(ws: Path, selection: list[str], timeout: float = TEST_TIMEOUT_DEFAULT,
             output_cap: int = TEST_OUTPUT_CAP) -> dict:
    if not selection:
        raise WorkspaceError("selection must name at least one path under tests/")
    for item in selection:
        rel = normalize_path(item.split("::", 1)[0])
        if not (rel == "tests" or rel.startswith("tests/")):
            raise WorkspaceError(f"selection '{item}' is not under tests/")
    cmd = ["uv", "run", "pytest", "-q", *selection]
    env = _env_for_tests(ws)
    try:
        r = _invoke_pytest(cmd, ws, env, timeout)
    except subprocess.TimeoutExpired:
        raise WorkspaceError(f"tests timed out after {timeout:.0f}s") from None
    tree = head_sha(ws)
    passed = r.returncode == 0
    record = {"tree_hash": tree, "passed": passed, "selection": selection, "at": now().isoformat()}
    set_setting(_TEST_RESULT_KEY, json.dumps(record))
    return {"passed": passed, "returncode": r.returncode, "tree_hash": tree, "selection": selection,
           "stdout": _redact((r.stdout or "")[-output_cap:]), "stderr": _redact((r.stderr or "")[-output_cap:])}


def last_test_pass() -> dict | None:
    raw = get_setting(_TEST_RESULT_KEY)
    return json.loads(raw) if raw else None
