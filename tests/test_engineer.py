"""E3: the Engineer agent - its own workspace clone, path containment, one branch at a time, tests-first PRs."""

from __future__ import annotations

import asyncio
import inspect
import subprocess
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from aecom import approvals
from aecom.company_settings import set_setting
from aecom.context import current_run
from aecom.db import Approval, AuditLog, session
from aecom.engineer import github_api, workspace
from aecom.engineer.workspace import WorkspaceError
from aecom.persona.loader import agent_ids, load_spec
from aecom.policy.gate import evaluate, gate_builtin
from aecom.registry.tools import allowed_tools, seed_registry
from aecom.tools.catalog import CATALOG

PLACEHOLDER_TEST = "def test_ok():\n    assert True\n"


def _git(*args: str, cwd) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture
def origin_repo(tmp_path):
    """A local bare repo (default branch "main") seeded with one allowlisted file and a placeholder test."""
    bare = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    _git("clone", str(bare), str(seed), cwd=tmp_path)
    (seed / "src" / "aecom" / "store").mkdir(parents=True)
    (seed / "src" / "aecom" / "store" / "__init__.py").write_text("")
    (seed / "tests").mkdir()
    (seed / "tests" / "test_placeholder.py").write_text(PLACEHOLDER_TEST)
    _git("add", "-A", cwd=seed)
    _git("-c", "user.email=seed@example.com", "-c", "user.name=seed", "commit", "-m", "init", cwd=seed)
    _git("push", "origin", "HEAD:main", cwd=seed)
    return bare


@pytest.fixture(autouse=True)
def engineer_env(monkeypatch, tmp_path, origin_repo):
    monkeypatch.setenv("ENGINEER_WORKSPACE_DIR", str(tmp_path / "engineer_workspace"))
    monkeypatch.setenv("ENGINEER_REPO_URL", str(origin_repo))
    monkeypatch.setenv("ENGINEER_REPO", "acme/widgets")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_value_1234567890")
    seed_registry()


def call(tool: str, agent: str = "engineer", run_id: str = "run_test0001", **args) -> str:
    token = current_run.set(run_id)
    try:
        return asyncio.run(gate_builtin(tool, agent).ainvoke(args))
    finally:
        current_run.reset(token)


def pending(tool: str) -> Approval:
    with session() as s:
        (a,) = s.scalars(select(Approval).where(Approval.tool_name == tool, Approval.status == "pending")).all()
        return a


@dataclass
class FakeCompleted:
    returncode: int = 0
    stdout: str = "1 passed"
    stderr: str = ""


def _fake_pytest(passed: bool):
    def fn(cmd, cwd, env, timeout):
        return FakeCompleted(0 if passed else 1, "1 passed" if passed else "1 failed", "" if passed else "boom")
    return fn


def _write_and_test(monkeypatch, run_id: str, path: str, content: str, selection=("tests/test_placeholder.py",),
                    passed: bool = True) -> str:
    monkeypatch.setattr(workspace, "_invoke_pytest", _fake_pytest(passed))
    out = call("eng_write_file", run_id=run_id, path=path, content=content)
    assert '"committed": true' in out, out
    return call("eng_run_tests", run_id=run_id, selection=list(selection))


def _open_pr(monkeypatch, run_id: str, title: str = "Add adapter", body: str = "Implements the thing."):
    captured: dict = {}

    def fake_pinned_post(target, *, json=None, headers=None, timeout=None, data=None):
        captured["target"], captured["json"], captured["headers"] = target, json, headers

        class R:
            def raise_for_status(self):
                return self

            def json(self):
                return {"html_url": "https://github.com/acme/widgets/pull/42"}

        return R()

    monkeypatch.setattr(github_api, "resolve_public", lambda url: url)
    monkeypatch.setattr(github_api, "pinned_post", fake_pinned_post)
    out = call("eng_open_pr", run_id=run_id, title=title, body=body)
    assert out.startswith("ESCALATED to human approval (T3"), out
    a = pending("eng_open_pr")
    result = approvals.approve(a.id, by="kaan")
    return result, captured


# ----------------------------------------------------------------------------- persona / registry
def test_engineer_persona_loads_and_is_registered():
    assert "engineer" in agent_ids()
    spec = load_spec("engineer")
    assert spec.model_tier == "sonnet" and spec.reports_to == "ceo"
    assert spec.soul and spec.procedures
    assert set(spec.tools) <= set(CATALOG)
    summary = seed_registry()
    assert summary["added"] == [] or all(a == "engineer" for a, _ in summary["added"])
    assert "eng_open_pr" in allowed_tools("engineer")
    assert "eng_write_file" in allowed_tools("engineer")


def test_engineer_not_chat_eligible_and_not_dispatched_by_the_supervisor():
    from aecom.chat import chat_agents
    from aecom.graph.supervisor import DEPARTMENTS

    assert "engineer" not in chat_agents()
    assert "engineer" not in DEPARTMENTS


def test_no_tool_merges_deploys_or_restarts_anything():
    eng_tools = [n for n in CATALOG if n.startswith("eng_")]
    assert eng_tools  # sanity: the tools actually registered
    bad_words = ("merge", "deploy", "restart")
    assert not any(w in n.lower() for n in eng_tools for w in bad_words)
    assert not any(w in t.lower() for t in load_spec("engineer").tools for w in bad_words)


# ----------------------------------------------------------------------------- gate tiers (T3 needs approval)
def test_gate_tiers_for_every_engineer_tool():
    expected = {"eng_read_file": "T0", "eng_list_files": "T0", "eng_search": "T0", "eng_diff": "T0",
               "eng_write_file": "T2", "eng_run_tests": "T1", "eng_open_pr": "T3"}
    for name, tier in expected.items():
        v = evaluate("engineer", name, {})
        assert v.tier == tier
        assert v.decision == ("escalate" if tier == "T3" else "allow")
        for other in ("ceo", "foreman", "sentinel", "support", "store_ops"):
            assert evaluate(other, name, {}).decision == "deny"


def test_pr_tool_never_runs_without_a_human_approval():
    with pytest.raises(PermissionError):
        CATALOG["eng_open_pr"].fn(title="x", body="y")


# ----------------------------------------------------------------------------- path allow/deny list
@pytest.mark.parametrize("path,allowed", [
    ("src/aecom/store/stripe.py", True),
    ("src/aecom/integrations/stripe.py", True),
    ("src/aecom/channels/discord.py", True),
    ("src/aecom/api/webhooks.py", True),
    ("tests/test_stripe.py", True),
    ("tests/sub/test_nested.py", True),
    ("docs/wiki/Integrations.md", True),
    ("src/aecom/policy/policy.yaml", False),
    ("agents/engineer/SOUL.md", False),
    ("migrations/0099_x.py", False),
    ("scripts/update.sh", False),
    ("deploy/docker-compose.yml", False),
    (".github/workflows/ci.yml", False),
    ("src/aecom/db.py", False),
    ("src/aecom/config.py", False),
    ("src/aecom/secrets.py", False),
    ("src/aecom/settings_registry.py", False),
    ("src/aecom/dashboard_auth.py", False),
    ("src/aecom/net_guard.py", False),
    ("pyproject.toml", False),
    ("uv.lock", False),
    (".env", False),
    (".env.production", False),
    ("src/aecom/store/.env", False),
    ("src/aecom/store_other.py", False),       # prefix match requires the trailing "/"
    ("src/aecom/api/other.py", False),         # only webhooks.py is allowed under api/
])
def test_write_path_allowlist_and_denylist(path, allowed):
    assert workspace.is_write_allowed(path) == allowed


def test_traversal_and_absolute_paths_are_rejected():
    ws = workspace.ensure_workspace()
    for bad in ("../outside.py", "src/../../outside.py", "/etc/passwd", "src/aecom/store/../../../x.py", ""):
        with pytest.raises(WorkspaceError):
            workspace.resolve_in_workspace(ws, bad)


def test_symlink_escape_is_rejected(tmp_path):
    ws = workspace.ensure_workspace()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    link = ws / "src" / "aecom" / "store" / "escape.py"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside / "secret.txt")
    with pytest.raises(WorkspaceError):
        workspace.resolve_in_workspace(ws, "src/aecom/store/escape.py")


def test_write_via_the_gate_is_refused_for_denied_paths():
    out = call("eng_write_file", path="src/aecom/policy/policy.yaml", content="rules: {}\n")
    assert out.startswith("ERROR") and "denied" in out.lower()


def test_read_denies_env_and_git_internals():
    assert call("eng_read_file", path=".env").startswith("ERROR")
    assert call("eng_read_file", path=".git/config").startswith("ERROR")
    assert "test_ok" in call("eng_read_file", path="tests/test_placeholder.py")


# ----------------------------------------------------------------------------- branch never main
def test_write_file_works_on_an_engineer_branch_never_main():
    out = call("eng_write_file", path="src/aecom/store/new_adapter.py", content="# adapter\n")
    assert '"committed": true' in out
    ws = workspace.ensure_workspace()
    branch = workspace.current_branch(ws)
    assert branch.startswith("engineer/") and branch not in ("main", "master")


def test_assert_engineer_branch_rejects_main_and_master():
    for bad in ("main", "master", "feature/x", ""):
        with pytest.raises(WorkspaceError):
            workspace.assert_engineer_branch(bad)
    workspace.assert_engineer_branch("engineer/ok")  # does not raise


# ----------------------------------------------------------------------------- one open branch at a time
def test_only_one_engineer_branch_may_be_open_at_a_time():
    out1 = call("eng_write_file", run_id="run_aaa", path="tests/test_a.py", content=PLACEHOLDER_TEST)
    assert '"committed": true' in out1
    assert workspace.open_branch() == "engineer/run-aaa"
    out2 = call("eng_write_file", run_id="run_bbb", path="tests/test_b.py", content=PLACEHOLDER_TEST)
    assert out2.startswith("ERROR") and "already open" in out2


def test_clear_open_branch_releases_the_slot_and_is_audited():
    call("eng_write_file", run_id="run_slot1", path="tests/test_slot1.py", content=PLACEHOLDER_TEST)
    assert workspace.open_branch() == "engineer/run-slot1"
    workspace.clear_open_branch("kaan")
    assert workspace.open_branch() == ""
    with session() as s:
        (row,) = s.scalars(select(AuditLog).where(AuditLog.action == "engineer_open_branch")).all()
        assert row.actor == "human:kaan" and row.decision == "cleared"
    out = call("eng_write_file", run_id="run_slot2", path="tests/test_slot2.py", content=PLACEHOLDER_TEST)
    assert '"committed": true' in out


# ----------------------------------------------------------------------------- diff-size cap
def test_diff_over_800_changed_lines_refuses_the_pr(monkeypatch):
    big = "\n".join(f"line {i}" for i in range(900)) + "\n"
    out = _write_and_test(monkeypatch, "run_big1", "src/aecom/store/big_adapter.py", big)
    assert '"passed": true' in out
    result, captured = _open_pr(monkeypatch, "run_big1")
    assert result["status"] == "failed"
    assert "changed lines" in result["result"] and "800" in result["result"]
    assert captured == {}  # the GitHub API was never called


# ----------------------------------------------------------------------------- secret scan
def test_secret_looking_diff_refuses_the_pr(monkeypatch):
    content = 'TOKEN = "ghp_' + "a" * 36 + '"\n'
    out = _write_and_test(monkeypatch, "run_secret1", "src/aecom/integrations/leaky.py", content)
    assert '"passed": true' in out
    result, captured = _open_pr(monkeypatch, "run_secret1")
    assert result["status"] == "failed" and "secret" in result["result"].lower()
    assert captured == {}


# ----------------------------------------------------------------------------- tests-must-pass-first gate
def test_pr_refused_without_ever_running_tests(monkeypatch):
    monkeypatch.setattr(workspace, "_invoke_pytest", _fake_pytest(True))
    out = call("eng_write_file", run_id="run_notest", path="src/aecom/store/x.py", content="# x\n")
    assert '"committed": true' in out
    result, captured = _open_pr(monkeypatch, "run_notest")
    assert result["status"] == "failed" and "eng_run_tests" in result["result"]
    assert captured == {}


def test_pr_refused_if_head_changed_after_the_last_passing_run(monkeypatch):
    out = _write_and_test(monkeypatch, "run_stale1", "src/aecom/store/y.py", "# y\n")
    assert '"passed": true' in out
    call("eng_write_file", run_id="run_stale1", path="src/aecom/store/y.py", content="# y changed\n")
    result, captured = _open_pr(monkeypatch, "run_stale1")
    assert result["status"] == "failed" and "eng_run_tests" in result["result"]
    assert captured == {}


def test_pr_refused_when_the_last_run_failed(monkeypatch):
    out = _write_and_test(monkeypatch, "run_fail1", "src/aecom/store/z.py", "# z\n", passed=False)
    assert '"passed": false' in out
    result, captured = _open_pr(monkeypatch, "run_fail1")
    assert result["status"] == "failed" and "eng_run_tests" in result["result"]
    assert captured == {}


def test_run_tests_rejects_selection_outside_tests_dir():
    ws = workspace.ensure_workspace()
    with pytest.raises(WorkspaceError, match="tests/"):
        workspace.run_tests(ws, ["src/aecom/store/x.py"])


def test_run_tests_accepts_a_single_string_selection(monkeypatch):
    out = _write_and_test(monkeypatch, "run_str1", "tests/test_str1.py", PLACEHOLDER_TEST,
                          selection=("tests/test_str1.py",))
    assert '"passed": true' in out


# ----------------------------------------------------------------------------- daily PR cap
def test_daily_pr_cap_blocks_a_second_pr(monkeypatch):
    set_setting("engineer_pr_daily_cap", "1")
    out = _write_and_test(monkeypatch, "run_cap1", "tests/test_cap1.py", PLACEHOLDER_TEST)
    assert '"passed": true' in out
    result1, captured1 = _open_pr(monkeypatch, "run_cap1")
    assert result1["status"] == "executed" and captured1["json"]
    workspace.clear_open_branch("kaan")  # a human closed the first branch; still capped for today
    out2 = _write_and_test(monkeypatch, "run_cap2", "tests/test_cap2.py", PLACEHOLDER_TEST)
    assert '"passed": true' in out2
    result2, captured2 = _open_pr(monkeypatch, "run_cap2")
    assert result2["status"] == "failed" and "cap" in result2["result"].lower()
    assert captured2 == {}


# ----------------------------------------------------------------------------- push: only the engineer branch
def test_push_implementation_never_forces_or_builds_other_refspecs():
    src = inspect.getsource(workspace.push_branch_only)
    assert '"--force"' not in src and "'--force'" not in src and '"-f"' not in src and "refs/heads/*" not in src


def test_push_refuses_a_non_engineer_branch():
    ws = workspace.ensure_workspace()
    with pytest.raises(WorkspaceError):
        workspace.push_branch_only(ws, "main")


def _bare_refs(bare) -> dict:
    out = subprocess.run(["git", f"--git-dir={bare}", "for-each-ref", "--format=%(refname) %(objectname)"],
                        capture_output=True, text=True, check=True).stdout
    return dict(line.split() for line in out.splitlines() if line)


def test_push_only_touches_the_engineer_branch_main_is_untouched(origin_repo):
    before = _bare_refs(origin_repo)
    call("eng_write_file", run_id="run_push1", path="tests/test_push1.py", content=PLACEHOLDER_TEST)
    ws = workspace.ensure_workspace()
    branch = workspace.current_branch(ws)
    workspace.push_branch_only(ws, branch)
    after = _bare_refs(origin_repo)
    assert after["refs/heads/main"] == before["refs/heads/main"]
    assert after[f"refs/heads/{branch}"] == workspace.head_sha(ws)
    assert set(after) - set(before) == {f"refs/heads/{branch}"}


# ----------------------------------------------------------------------------- PR via the GitHub API
def test_pr_opened_via_the_monkeypatched_github_api_with_the_fixed_disclaimer(monkeypatch):
    out = _write_and_test(monkeypatch, "run_pr1", "src/aecom/store/widget.py", "# widget adapter\n")
    assert '"passed": true' in out
    result, captured = _open_pr(monkeypatch, "run_pr1", title="Add widget adapter",
                                body="Implements the Widget API per the docs I was given.")
    assert result["status"] == "executed"
    assert captured["json"]["title"] == "Add widget adapter"
    assert captured["json"]["body"].strip().endswith(
        "Opened by the Engineer agent. Not deployed. A human must review and merge.")
    assert captured["json"]["base"] == "main"
    assert captured["headers"]["Authorization"] == "Bearer ghp_test_token_value_1234567890"
    assert "https://github.com/acme/widgets/pull/42" in result["result"]


def test_target_repo_prefers_the_engineer_repo_setting():
    ws = workspace.ensure_workspace()
    assert workspace.target_repo(ws) == "acme/widgets"


def test_target_repo_falls_back_to_a_github_origin_url(monkeypatch):
    monkeypatch.delenv("ENGINEER_REPO", raising=False)
    ws = workspace.ensure_workspace()
    with pytest.raises(WorkspaceError):
        workspace.target_repo(ws)  # the local bare-repo origin doesn't look like github.com
    subprocess.run(["git", "remote", "set-url", "origin", "git@github.com:acme/widgets.git"], cwd=ws, check=True)
    assert workspace.target_repo(ws) == "acme/widgets"


# ----------------------------------------------------------------------------- environment scrubbing
def test_eng_run_tests_scrubs_secrets_from_the_child_environment(monkeypatch):
    monkeypatch.setenv("FAKE_SECRET_TOKEN", "super-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    captured_env: dict = {}

    def fake_invoke(cmd, cwd, env, timeout):
        captured_env.update(env)
        return FakeCompleted(0, "1 passed", "")

    monkeypatch.setattr(workspace, "_invoke_pytest", fake_invoke)
    call("eng_write_file", run_id="run_env1", path="tests/test_env1.py", content=PLACEHOLDER_TEST)
    call("eng_run_tests", run_id="run_env1", selection=["tests/test_env1.py"])
    assert "FAKE_SECRET_TOKEN" not in captured_env
    assert "ANTHROPIC_API_KEY" not in captured_env
    assert "GITHUB_TOKEN" not in captured_env
    assert captured_env.get("LLM_MODE") == "scripted"
    assert "sqlite:///" in captured_env.get("DATABASE_URL", "")


# ----------------------------------------------------------------------------- reads
def test_reads_are_fenced_and_scoped_to_the_workspace():
    files = call("eng_list_files")
    assert "tests/test_placeholder.py" in files
    content = call("eng_read_file", path="tests/test_placeholder.py")
    assert content.startswith("```") and "test_ok" in content
    hits = call("eng_search", query="test_ok")
    assert "test_ok" in hits
    assert "no changes yet" in call("eng_diff")
    call("eng_write_file", path="tests/test_diffme.py", content=PLACEHOLDER_TEST)
    assert "test_diffme" in call("eng_diff")


# --------------------------------------------------------------------------- review findings (P0)
def test_a_symlink_to_a_denied_file_inside_the_workspace_is_refused():
    ws = workspace.ensure_workspace()
    (ws / "src" / "aecom").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "aecom" / "db.py").write_text("# real db module\n")
    link = ws / "src" / "aecom" / "store" / "evil_link.py"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(ws / "src" / "aecom" / "db.py")
    with pytest.raises(workspace.WorkspaceError, match="symlink"):
        workspace.write_file(ws, "src/aecom/store/evil_link.py", "overwritten = True\n")
    assert (ws / "src" / "aecom" / "db.py").read_text() == "# real db module\n"


def test_a_symlinked_parent_folder_is_refused():
    ws = workspace.ensure_workspace()
    (ws / "src" / "aecom" / "policy").mkdir(parents=True, exist_ok=True)
    link = ws / "src" / "aecom" / "store" / "sneaky"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(ws / "src" / "aecom" / "policy", target_is_directory=True)
    with pytest.raises(workspace.WorkspaceError, match="symlink"):
        workspace.write_file(ws, "src/aecom/store/sneaky/x.py", "x = 1\n")


@pytest.mark.parametrize("snippet", [
    "open('/home/pi/agentic_ecommerce_platform/.env').read()", "import os\nprint(os.environ)", "x = 'data/oauth_tokens.json'",
    "import subprocess", "p = '~/.ssh/id_rsa'", "from aecom.secrets import secrets",
])
def test_test_code_that_reaches_for_secrets_or_the_environment_is_refused(snippet):
    ws = workspace.ensure_workspace()
    with pytest.raises(workspace.WorkspaceError, match="off limits"):
        workspace.write_file(ws, "tests/test_probe.py", snippet + "\n")


def test_the_test_run_env_has_its_own_python_env_and_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/live/app/.venv")
    monkeypatch.setenv("HOME", "/Users/owner")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-shouldnotleak123456")
    env = workspace._env_for_tests(tmp_path)
    assert "VIRTUAL_ENV" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["HOME"].startswith(str(tmp_path)) and env["UV_PROJECT_ENVIRONMENT"].startswith(str(tmp_path))


def test_test_output_is_redacted_before_it_reaches_the_agent(monkeypatch):
    ws = workspace.ensure_workspace()
    fake = type("R", (), {"returncode": 1, "stdout": f"see {workspace.ROOT}/x sk-ant-abcdefghijklmnopqrstuvwxyz", "stderr": ""})()
    monkeypatch.setattr(workspace, "_invoke_pytest", lambda *a, **k: fake)
    out = workspace.run_tests(ws, ["tests/test_x.py"])
    assert str(workspace.ROOT) not in out["stdout"] and "sk-ant-abcdefghijklmnopqrstuvwxyz" not in out["stdout"]
