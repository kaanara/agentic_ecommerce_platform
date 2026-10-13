import pytest

import aecom.handlers  # noqa: F401  (register event subscribers)
from aecom import db
from aecom.config import get_settings


@pytest.fixture(autouse=True)
def fresh_company(tmp_path):
    """Every test gets its own SQLite DB, scripted LLM, no MCP autoload, decisions written to tmp."""
    s = get_settings()
    saved = (s.database_url, s.llm_mode, s.mcp_autoload, s.decisions_dir)
    s.database_url = f"sqlite:///{tmp_path / 'aecom.db'}"
    s.llm_mode, s.mcp_autoload, s.decisions_dir = "scripted", False, tmp_path / "decisions"
    db.reset_engine()
    from aecom.bootstrap import init

    init()
    yield s
    s.database_url, s.llm_mode, s.mcp_autoload, s.decisions_dir = saved
    db.reset_engine()
