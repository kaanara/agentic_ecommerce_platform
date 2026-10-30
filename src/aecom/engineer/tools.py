"""The Engineer's tools (roadmap E3), registered in ``aecom.tools.catalog.CATALOG`` by importing this module from
the bottom of ``aecom.tools.catalog`` (mirroring how ``aecom.tools.__init__`` registers the Sentinel's
``aecom.tools.diagnostics`` / ``aecom.tools.remediation`` tools).

Tiers (``policy.yaml``, "engineer" rows): reads (``eng_read_file``, ``eng_list_files``, ``eng_search``, ``eng_diff``)
are T0; ``eng_write_file`` is T2 (its own allow/deny path list is the real control, not the gate's numeric limits);
``eng_run_tests`` is T1; ``eng_open_pr`` is T3, so the gate always parks it for a human - its body below only ever
runs once approved (the same pattern as ``aecom.tools.remediation.propose_correcting_entry``), and even then refuses
an oversized diff, a denied path, a secret-looking string, or a HEAD that tests have not already passed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from aecom.audit import audit
from aecom.context import current_run
from aecom.db import Approval, session
from aecom.engineer import github_api, workspace
from aecom.engineer.workspace import WorkspaceError
from aecom.tools.catalog import tool

AGENT = "engineer"
MAX_LIST_FILES = 300
MAX_SEARCH_HITS = 100


def _fence(text: str) -> str:
    """A plain markdown code fence for readability - unlike ``aecom.tools.catalog.fence``, this never marks the
    run tainted: these reads come from the Engineer's own trusted git workspace, not external/untrusted content."""
    return f"```\n{text}\n```"


def _being_approved(tool_name: str, args: dict[str, Any]) -> bool:
    """True only while a human's approval of exactly this call is executing (status 'approved', result not yet
    written - set by ``aecom.approvals.approve`` before it executes the call). Mirrors
    ``aecom.tools.remediation._being_approved``: an agent can never fake this, since it can't create such a row."""
    with session() as s:
        rows = s.scalars(select(Approval).where(Approval.tool_name == tool_name, Approval.status == "approved",
                                                Approval.result.is_(None))).all()
        return any(dict(a.args or {}) == args for a in rows)


@tool("engineering")
def eng_read_file(path: str) -> str:
    """Read one file from the Engineer's own workspace clone (never the live checkout). Fenced for readability."""
    ws = workspace.ensure_workspace()
    reason = workspace.denied_read_reason(workspace.normalize_path(path))
    if reason:
        raise WorkspaceError(f"refusing to read '{path}': {reason}")
    target = workspace.resolve_in_workspace(ws, path)
    if not target.is_file():
        raise WorkspaceError(f"'{path}' does not exist in the workspace")
    return _fence(target.read_text(encoding="utf-8", errors="replace"))


@tool("engineering")
def eng_list_files(prefix: str = "") -> list[str]:
    """List tracked files in the Engineer's workspace, optionally under `prefix` (e.g. "src/aecom/store")."""
    ws = workspace.ensure_workspace()
    rel = workspace.normalize_path(prefix) if prefix else ""
    return workspace.list_files(ws, rel)[:MAX_LIST_FILES]


@tool("engineering")
def eng_search(query: str) -> str:
    """Search tracked file contents in the workspace for a literal `query` (not a regex). Fenced results."""
    ws = workspace.ensure_workspace()
    hits = workspace.search(ws, query)
    if not hits:
        return "no matches"
    return _fence("\n".join(hits[:MAX_SEARCH_HITS]))


@tool("engineering")
def eng_diff() -> str:
    """Unified diff of the Engineer's current branch vs. the base branch. Fenced."""
    ws = workspace.ensure_workspace()
    text = workspace.diff_text(ws)
    return _fence(text) if text.strip() else "no changes yet"


@tool("engineering", side_effect=True)
def eng_write_file(path: str, content: str) -> dict:
    """Write one file in the Engineer's own workspace, on its branch `engineer/<slug>` (never on main), committing
    it there (author `aecom-engineer`). Allowed only under src/aecom/store|integrations|channels/, tests/,
    src/aecom/api/webhooks.py and docs/wiki/Integrations.md; secrets (.env*), migrations/, scripts/, deploy/,
    .github/, agents/, src/aecom/policy/ and this app's own db/config/secrets/settings_registry/dashboard_auth/
    net_guard modules are always denied, even if a future change to the allowlist would otherwise match them."""
    ws = workspace.ensure_workspace()
    result = workspace.write_file(ws, path, content)
    audit(AGENT, "eng_write_file", "written" if result["committed"] else "noop",
         {"path": result["path"], "branch": result["branch"]}, current_run.get())
    return result


@tool("engineering", side_effect=True)
def eng_run_tests(selection: list[str] | str) -> dict:
    """Run `uv run pytest -q <selection>` inside the Engineer's workspace: LLM_MODE=scripted, a throwaway sqlite
    DATABASE_URL, a timeout and capped output, and an environment scrubbed of every inherited secret. `selection`
    must name one or more paths under tests/. A pass is remembered against the exact commit tested, so `eng_open_pr`
    can require that HEAD has not changed since."""
    ws = workspace.ensure_workspace()
    paths = [selection] if isinstance(selection, str) else list(selection)
    result = workspace.run_tests(ws, paths)
    audit(AGENT, "eng_run_tests", "passed" if result["passed"] else "failed",
         {"selection": paths, "returncode": result["returncode"], "tree_hash": result["tree_hash"]},
         current_run.get())
    return result


@tool("engineering", side_effect=True)
def eng_open_pr(title: str, body: str) -> dict:
    """Open a PR from the Engineer's branch. Tier T3: the policy gate always parks this for a human first; this
    body runs only once approved. Refuses a diff over 800 changed lines, a diff touching a denied path, anything
    that looks like a secret, a HEAD that `eng_run_tests` has not already passed, more than one open engineer
    branch, or the daily PR cap (`engineer_pr_daily_cap`, default 3). Pushes only `engineer/<slug>` (never force,
    never main/master) and opens the PR through the GitHub API; the body always ends with the fixed disclaimer
    that a human must review and merge - the Engineer has no tool that merges, deploys or restarts anything."""
    args = {"title": title, "body": body}
    if not _being_approved("eng_open_pr", args):
        raise PermissionError("a PR is opened only once a human approves it")
    ws = workspace.ensure_workspace()
    branch = workspace.current_branch(ws)
    workspace.assert_engineer_branch(branch)
    changed = workspace.diff_paths(ws)
    if not changed:
        raise WorkspaceError("nothing to open a PR for: no committed changes on this branch yet")
    for p in changed:
        reason = workspace.denied_path_reason(p)
        if reason:
            raise WorkspaceError(f"diff touches a denied path '{p}': {reason}")
    stat = workspace.diff_stat(ws)
    total = stat["insertions"] + stat["deletions"]
    if total > github_api.MAX_DIFF_LINES:
        raise WorkspaceError(f"diff is {total} changed lines, over the {github_api.MAX_DIFF_LINES}-line cap; "
                             "split the change into a smaller PR")
    hit = github_api.secret_like(workspace.diff_text(ws, max_chars=200_000))
    if hit:
        raise WorkspaceError(f"diff looks like it contains a secret ({hit}); refusing to open a PR")
    head = workspace.head_sha(ws)
    tested = workspace.last_test_pass()
    if tested is None or tested.get("tree_hash") != head or not tested.get("passed"):
        raise WorkspaceError("eng_run_tests must pass on the current HEAD of this workspace before opening a PR; "
                             "run it again after your latest change")
    current_open = workspace.open_branch()
    if current_open and current_open != branch:
        raise WorkspaceError(f"another engineer branch '{current_open}' is already open; a human must close it "
                             "first")
    cap = github_api.daily_cap()
    used = github_api.prs_opened_today()
    if used >= cap:
        raise WorkspaceError(f"daily PR cap ({cap}) reached ({used} opened today); ask a human to raise "
                             "engineer_pr_daily_cap or try again tomorrow")
    base = workspace.default_branch(ws)
    workspace.push_branch_only(ws, branch)
    repo = workspace.target_repo(ws)
    pr = github_api.create_pull_request(repo, title, body, head=branch, base=base)
    github_api.record_pr_opened(branch)
    audit(AGENT, "eng_open_pr", "opened", {"branch": branch, "url": pr.get("html_url"), "tested_tree": head},
         current_run.get())
    return {"url": pr.get("html_url"), "branch": branch, "title": title, "changed_files": stat["files"],
           "changed_lines": total}
