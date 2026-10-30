"""Settings center: every option is in the registry, layers apply in the right order, secrets never leak."""

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from aecom import envfile, settings_registry as reg
from aecom.config import ROOT, Settings, get_settings
from aecom.db import AuditLog, Setting, session
from aecom.secrets import secrets


@pytest.fixture
def envp(tmp_path, monkeypatch):
    """A private .env; also forget overrides applied by earlier tests in this process."""
    d = tmp_path / "envdir"
    d.mkdir()
    p = d / ".env"
    monkeypatch.setattr(envfile, "env_path", lambda: p)
    secrets.reload()
    reg._overridden.clear()
    reg._seen_version = None
    s = get_settings()
    saved = {k: getattr(s, k) for k in Settings.model_fields}  # the singleton outlives each test's database
    yield p
    for k, v in saved.items():
        setattr(s, k, v)
    reg._overridden.clear()
    reg._seen_version = None
    secrets.reload()


def test_every_settings_field_is_in_the_registry():
    assert set(Settings.model_fields) <= set(reg.FIELDS), set(Settings.model_fields) - set(reg.FIELDS)


def test_every_env_example_key_is_in_the_registry():
    keys = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), flags=re.M))
    missing = {k for k in keys if k.lower() not in reg.FIELDS}
    assert not missing, missing


def test_config_fields_exist_on_settings():
    bad = [k for k, f in reg.FIELDS.items() if f.storage in ("config", "readonly") and k not in Settings.model_fields
           and k not in ("dashboard_auth", "dashboard_password_hash")]
    assert not bad, bad


def test_layers_default_env_ui_and_reset(envp, monkeypatch):
    s = get_settings()
    assert s.chat_history_turns == 12 and reg.source("chat_history_turns") == "default"
    monkeypatch.setenv("CHAT_HISTORY_TURNS", "7")
    assert reg.source("chat_history_turns") == ".env"
    assert reg.set_value("chat_history_turns", "30", "kaan") == 30
    assert get_settings() is s and s.chat_history_turns == 30 and reg.source("chat_history_turns") == "ui"
    reg.reset_value("chat_history_turns", "kaan")
    assert s.chat_history_turns == 7 and reg.source("chat_history_turns") == ".env"


def test_change_reaches_another_process_through_config_version(envp):
    s = get_settings()
    before = reg.config_version()
    reg.refresh_if_changed()
    with session() as db:
        db.add(Setting(key="config:chat_history_turns", value=json.dumps(44)))
    reg.bump_version()  # what the other process's save does
    assert reg.config_version() != before
    assert reg.refresh_if_changed() is True
    assert s.chat_history_turns == 44
    assert reg.refresh_if_changed() is False


def test_validation_messages(envp):
    for key, raw in (("chat_history_turns", "1"), ("chat_history_turns", "abc"), ("chat_daily_cap_share", "1.5"),
                     ("schedule_monitors", "every day"), ("quiet_hours_start", "25:00"), ("price_opus", "5"),
                     ("llm_mode", "gpt")):
        with pytest.raises(ValueError):
            reg.set_value(key, raw, "kaan", step_up_ok=True)
    assert reg.coerce(reg.FIELDS["price_opus"], "6, 30") == (6.0, 30.0)
    assert reg.coerce(reg.FIELDS["chat_agents"], "ceo, finance") == ["ceo", "finance"]
    with pytest.raises(ValueError):
        reg.set_value("chat_history_turns", "20", "")  # who changed it is always recorded


def test_critical_settings_need_the_password_again(envp):
    with pytest.raises(reg.StepUpRequired):
        reg.set_value("agentmail_live", "true", "kaan")
    assert reg.set_value("agentmail_live", "true", "kaan", step_up_ok=True) is True
    assert get_settings().agentmail_live is True
    with pytest.raises(reg.StepUpRequired):
        reg.reset_value("agentmail_live", "kaan")
    reg.reset_value("agentmail_live", "kaan", step_up_ok=True)
    assert get_settings().agentmail_live is False


def test_secrets_are_written_to_env_only_and_never_shown(envp):
    envp.write_text("# keep me\nNTFY_URL=old\n")
    assert reg.set_value("ntfy_url", "https://ntfy.sh/topic-123456", "kaan", step_up_ok=True) is True
    assert "# keep me" in envp.read_text() and "NTFY_URL=https://ntfy.sh/topic-123456" in envp.read_text()
    assert reg.value("ntfy_url") is True  # set / not set, never the text
    assert reg.secret_hint("ntfy_url") == {"set": True, "last4": "3456"}
    with session() as db:
        assert "topic-123456" not in json.dumps([r.detail for r in db.scalars(select(AuditLog))])
        assert all("topic-123456" not in r.value for r in db.scalars(select(Setting)))
    assert all("topic-123456" not in json.dumps(h, default=str) for h in reg.history())
    with pytest.raises(reg.StepUpRequired):
        reg.set_value("ntfy_url", "x", "kaan")


def test_managed_and_readonly_keys_are_refused(envp):
    for key in ("dashboard_auth", "dashboard_password_hash", "database_url", "backup_dir"):
        with pytest.raises(ValueError):
            reg.set_value(key, "x", "kaan", step_up_ok=True)


def test_env_storage_and_db_storage(envp):
    reg.set_value("alert_email", "ops@example.com", "kaan")
    assert "ALERT_EMAIL=ops@example.com" in envp.read_text() and secrets.get("ALERT_EMAIL") == "ops@example.com"
    reg.set_value("marketing_budget_month_eur", "250", "kaan")
    assert reg.value("marketing_budget_month_eur") == 250.0


def test_history_records_who_and_what(envp):
    reg.set_value("chat_history_turns", "20", "kaan")
    reg.set_value("chat_history_turns", "25", "kaan")
    h = reg.history(key="chat_history_turns")
    assert [(x["old"], x["new"], x["by"]) for x in h] == [(20, 25, "kaan"), (12, 20, "kaan")]


def test_export_import_skips_secrets_and_critical(envp):
    reg.set_value("chat_history_turns", "20", "kaan")
    reg.set_value("agentmail_live", "true", "kaan", step_up_ok=True)
    reg.set_value("ntfy_url", "https://ntfy.sh/zzzzzzzzzz", "kaan", step_up_ok=True)
    exported = reg.export_settings()
    assert exported["chat_history_turns"] == 20
    assert not {"agentmail_live", "ntfy_url", "anthropic_api_key"} & set(exported)
    result = reg.import_settings({"chat_history_turns": 8, "agentmail_live": True, "ntfy_url": "x", "nope": 1,
                                  "chat_daily_cap_share": 9}, "kaan")
    assert result["applied"] == ["chat_history_turns"]
    assert set(result["skipped"]) == {"agentmail_live", "ntfy_url", "nope"} and len(result["errors"]) == 1


def test_no_agent_code_can_reach_the_settings_center():
    """Agents get tools only from tools/ and graph/; none of it may import the registry, the .env writer or auth setters."""
    src = ROOT / "src" / "aecom"
    offenders = []
    for folder in ("tools", "graph", "mcp_servers", "registry"):
        for f in (src / folder).rglob("*.py"):
            text = f.read_text()
            if re.search(r"settings_registry|\benvfile\b|dashboard_auth\.(set_|save_|turn_|change_|reset_)", text):
                offenders.append(str(f.relative_to(ROOT)))
    assert not offenders, offenders
