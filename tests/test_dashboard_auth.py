"""C6: dashboard login (scrypt hash, fail closed, rate limit) and every page gated before data/buttons."""

import ast
import re
from pathlib import Path

import pytest
from aecom import dashboard_auth as auth
from aecom.secrets import secrets

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
PAGES = [DASHBOARD / "Home.py", *sorted((DASHBOARD / "app_pages").glob("*.py"))]


@pytest.fixture
def no_hash(monkeypatch):
    monkeypatch.delenv(auth.ENV_VAR, raising=False)
    monkeypatch.setattr(secrets, "_dotenv", {})


@pytest.fixture
def with_hash(monkeypatch, no_hash):
    monkeypatch.setenv(auth.ENV_VAR, auth.hash_password("correct horse"))


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_hash_format_and_round_trip(with_hash):
    line = auth.hash_password("pw")
    scheme, salt, digest = line.split("$")
    assert scheme == "scrypt" and len(bytes.fromhex(salt)) == 16 and bytes.fromhex(digest)
    assert line != auth.hash_password("pw")  # salted
    assert auth.verify_password("pw", line)
    assert auth.is_configured()
    assert auth.verify_password("correct horse")


def test_wrong_or_malformed_fails(with_hash):
    assert not auth.verify_password("Correct horse")
    assert not auth.verify_password("")
    for bad in ["plain", "scrypt$zz$00", "bcrypt$00$00", "scrypt$00"]:
        assert not auth.verify_password("pw", bad)


def test_unset_hash_fails_closed(no_hash):
    assert not auth.is_configured()
    assert not auth.verify_password("")
    assert not auth.verify_password("anything")
    assert not auth.attempt_login("anything", {})


def test_rate_limit_after_five_failures_and_reset(with_hash):
    clock, state = Clock(), {}
    for _ in range(auth.MAX_FAILURES):
        assert not auth.attempt_login("nope", state, now=clock)
    assert auth.locked_out(state, clock) == pytest.approx(auth.LOCKOUT_SECONDS)
    # even the right password is refused during the lockout
    assert not auth.attempt_login("correct horse", state, now=clock)
    assert not state.get("authenticated")
    clock.t += auth.LOCKOUT_SECONDS - 1
    assert auth.locked_out(state, clock) > 0
    clock.t += 1
    assert auth.locked_out(state, clock) == 0
    assert auth.attempt_login("correct horse", state, now=clock)
    assert state["authenticated"] and state["auth_failures"] == 0


def test_four_failures_do_not_lock(with_hash):
    clock, state = Clock(), {}
    for _ in range(auth.MAX_FAILURES - 1):
        auth.attempt_login("nope", state, now=clock)
    assert auth.locked_out(state, clock) == 0
    assert auth.attempt_login("correct horse", state, now=clock)


def test_cli_prints_hash_not_password(with_hash, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "s3cret-pw")
    auth.main()
    out = capsys.readouterr().out
    assert "s3cret-pw" not in out
    value = re.search(rf"^{auth.ENV_VAR}=(\S+)$", out, re.M).group(1)
    assert auth.verify_password("s3cret-pw", value)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_page_requires_login_first(page):
    lines = page.read_text().splitlines()
    gate = next((i for i, line in enumerate(lines) if line.strip() == "require_login()"), None)
    assert gate is not None, f"{page.name} never calls require_login()"
    first_use = next((i for i, line in enumerate(lines) if re.search(r"\bsql\(|\.button\(|st\.toggle\(", line)), None)
    assert first_use is None or gate < first_use, f"{page.name}: data/button before require_login()"
    # stricter: only imports and st.set_page_config may run before the gate
    for stmt in ast.parse(page.read_text()).body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            name = ast.unparse(stmt.value.func)
            if name == "require_login":
                break
            assert name == "st.set_page_config", f"{page.name}: {name}() runs before require_login()"
        else:
            assert isinstance(stmt, ast.Import | ast.ImportFrom), f"{page.name}: code runs before require_login()"


@pytest.mark.parametrize("value,expected", [("off", True), ("OFF", True), ("false", True), ("0", True), ("no", True),
                                            (None, False), ("", False), ("on", False), ("disabled-ish", False)])
def test_login_is_disabled_only_by_an_explicit_off(monkeypatch, value, expected):
    monkeypatch.setattr(secrets, "get", lambda name: value if name == "DASHBOARD_AUTH" else None)
    assert auth.disabled() is expected


def test_disabled_login_shows_a_warning_and_skips_the_gate():
    src = (Path(__file__).resolve().parents[1] / "dashboard" / "_common.py").read_text()
    gate = src[src.index("def require_login"):]
    assert gate.index("dashboard_auth.disabled()") < gate.index("is_configured()")  # explicit opt-out checked first
    assert "st.warning" in gate[:gate.index("is_configured()")]


# --- login managed from the dashboard (database wins over .env) -------------------------------------------------
def test_database_mode_and_hash_win_over_env(with_hash):
    assert not auth.disabled() and auth.verify_password("correct horse")
    auth.set_password("a much longer secret", "a much longer secret", "kaan", current="correct horse", state={})
    assert auth.verify_password("a much longer secret") and not auth.verify_password("correct horse")
    assert not auth.disabled()


def test_first_password_needs_no_current_one_and_turns_login_on(no_hash, monkeypatch):
    monkeypatch.setenv("DASHBOARD_AUTH", "off")
    assert auth.disabled() and not auth.is_configured()
    auth.set_password("twelve chars ok", "twelve chars ok", "kaan")
    assert not auth.disabled() and auth.is_configured()


@pytest.mark.parametrize("new,repeat,by,msg", [("short", "short", "kaan", "at least"), ("long enough pw", "different pw!!", "kaan", "match"),
                                              ("long enough pw", "long enough pw", " ", "name")])
def test_set_password_validation(no_hash, new, repeat, by, msg):
    with pytest.raises(ValueError, match=msg):
        auth.set_password(new, repeat, by)
    assert not auth.is_configured()


def test_changing_or_disabling_needs_the_current_password_and_is_rate_limited(with_hash):
    state = {}
    with pytest.raises(ValueError, match="current"):
        auth.set_password("a much longer secret", "a much longer secret", "kaan", current="nope", state=state)
    with pytest.raises(ValueError, match="current"):
        auth.turn_login_off("kaan", "nope", state)
    assert not auth.disabled()
    for _ in range(auth.MAX_FAILURES):
        auth.confirm_password("nope", state)
    assert not auth.confirm_password("correct horse", state)  # locked out, even with the right one
    auth.turn_login_off("kaan", "correct horse", {})
    assert auth.disabled()
    auth.turn_login_on("kaan")
    assert not auth.disabled()


def test_turn_on_without_password_is_refused(no_hash):
    with pytest.raises(ValueError, match="password first"):
        auth.turn_login_on("kaan")


def test_shell_reset_needs_no_current_password(with_hash):
    auth.reset_password_local("recovered password!")
    assert auth.verify_password("recovered password!") and not auth.disabled()


def test_changes_are_audited_without_the_password(with_hash):
    from sqlalchemy import select

    from aecom.db import AuditLog, session

    auth.set_password("a much longer secret", "a much longer secret", "kaan", current="correct horse", state={})
    with session() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action == "dashboard_login_password_set")))
    assert rows and rows[0].actor == "human:kaan" and "secret" not in str(rows[0].detail)


def test_idle_timeout():
    state = {}
    assert not auth.idle_expired(state, 12)
    auth.touch(state, now=lambda: 1000.0)
    assert not auth.idle_expired(state, 12, now=lambda: 1000.0 + 11 * 3600)
    assert auth.idle_expired(state, 12, now=lambda: 1000.0 + 13 * 3600)
    assert not auth.idle_expired(state, 0, now=lambda: 1e9)
