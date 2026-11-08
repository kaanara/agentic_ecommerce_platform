import tempfile
from pathlib import Path

import pytest

import aecom.handlers  # noqa: F401  (register event subscribers)
from aecom import db, envfile
from aecom.config import Settings, get_settings
from aecom.secrets import secrets

# Tests never read the developer's .env. With STORE_ADAPTER=whop and a real WHOP_API_KEY there, tests created
# purchasable products on the live store and sent mail through the real AgentMail inbox. Secrets resolve against a
# .env that does not exist (tests that need one point envfile.env_path at a tmp file), and the cached Settings is
# reset to code defaults plus the real process environment.
_NO_ENV = Path(tempfile.mkdtemp(prefix="aecom-tests-")) / ".env"
envfile.env_path = lambda: _NO_ENV
secrets.reload()
Settings.model_config["env_file"] = None  # also for Settings() built later (settings registry "reset to default")
_clean = Settings()
_settings = get_settings()
for _name in Settings.model_fields:
    setattr(_settings, _name, getattr(_clean, _name))


@pytest.fixture(autouse=True)
def fresh_company(tmp_path):
    """Every test gets its own SQLite DB, scripted LLM, no MCP autoload, decisions written to tmp."""
    s = get_settings()
    saved = (s.database_url, s.llm_mode, s.mcp_autoload, s.decisions_dir, s.backup_dir)
    s.database_url = f"sqlite:///{tmp_path / 'aecom.db'}"
    s.llm_mode, s.mcp_autoload, s.decisions_dir = "scripted", False, tmp_path / "decisions"
    s.backup_dir = tmp_path / "backups"  # never list or write the real data/backups
    db.reset_engine()
    from aecom.bootstrap import init

    init()
    yield s
    s.database_url, s.llm_mode, s.mcp_autoload, s.decisions_dir, s.backup_dir = saved
    db.reset_engine()
