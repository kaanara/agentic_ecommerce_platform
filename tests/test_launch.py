"""Roadmap L4: launch readiness (aecom.launch). Offline throughout (``fresh_company`` fixture gives every test its
own tmp SQLite DB and ``LLM_MODE=scripted``); nothing here makes a real network or LLM call.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from aecom import backup, launch
from aecom.company_settings import set_flag, set_setting
from aecom.config import get_settings
from aecom.db import AuditLog, ChannelLink, Customer, Order, Product, session
from aecom.secrets import secrets
from aecom.tools.catalog import CATALOG

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the harness page does `from _common import ...` / `from _ops_launch import ...`


def by_id(rows):
    return {c.id: c for c in rows}


# --------------------------------------------------------------------------- never raises on a fresh DB

def test_checks_never_raise_on_a_fresh_db():
    rows = launch.checks()
    ids = {c.id for c in rows}
    assert ids == {
        "dashboard_login", "backup_scheduled", "backup_verified_recent", "backup_offdevice", "backup_passphrase",
        "anthropic_llm", "store_ready", "agentmail_live", "alerts_owner", "alerts_heartbeat", "ack_spend_cap",
        "ack_legal", "ack_oncall", "kill_switch_safe_mode", "doctor_fail", "migrations_head", "ack_webhook",
        "ack_retention",
    }
    assert all(c.status in (launch.OK, launch.TODO, launch.WARN) for c in rows)


def test_a_check_that_raises_is_reported_as_warn_not_todo(monkeypatch):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(launch, "CHECKS", [boom, *launch.CHECKS[1:]])
    rows = launch.checks()
    crashed = [c for c in rows if "kaboom" in c.detail]
    assert len(crashed) == 1 and crashed[0].status == launch.WARN
    assert len(rows) == len(launch.CHECKS)  # every other check still ran


# --------------------------------------------------------------------------- statuses flip with state

def test_dashboard_login_flips_on_and_off():
    assert by_id(launch.checks())["dashboard_login"].status == launch.TODO  # no password set yet
    from aecom import dashboard_auth

    dashboard_auth.set_password("a-password-12345", "a-password-12345", "kaan")
    assert by_id(launch.checks())["dashboard_login"].status == launch.OK


def test_backup_scheduled_flips_with_backup_enabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "backup_enabled", True)
    assert by_id(launch.checks())["backup_scheduled"].status == launch.OK
    monkeypatch.setattr(settings, "backup_enabled", False)
    assert by_id(launch.checks())["backup_scheduled"].status == launch.TODO


def test_backup_verified_recent_flips_with_age(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "bk")
    assert by_id(launch.checks())["backup_verified_recent"].status == launch.TODO

    path = backup.create_backup(now=datetime.now(UTC) - timedelta(hours=1))
    backup.update_manifest(path, verified=True, verified_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    assert by_id(launch.checks())["backup_verified_recent"].status == launch.OK

    path2 = backup.create_backup(now=datetime.now(UTC) - timedelta(hours=30), label="stale")
    backup.update_manifest(path2, verified=True, verified_at=(datetime.now(UTC) - timedelta(hours=30)).isoformat())
    # the still-fresh verification from `path` is newer, so the item stays ok
    assert by_id(launch.checks())["backup_verified_recent"].status == launch.OK


def test_backup_offdevice_and_passphrase_flip(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "backup_target", None)
    monkeypatch.setattr(secrets, "get", lambda name: None)
    rows = by_id(launch.checks())
    assert rows["backup_offdevice"].status == launch.TODO
    assert rows["backup_passphrase"].status == launch.TODO

    monkeypatch.setattr(settings, "backup_target", "user@host:/backups")
    monkeypatch.setattr(secrets, "get", lambda name: "s3cr3t" if name == "BACKUP_PASSPHRASE" else None)
    rows = by_id(launch.checks())
    assert rows["backup_offdevice"].status == launch.OK
    assert rows["backup_passphrase"].status == launch.OK


def test_anthropic_check_needs_both_key_and_mode(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_mode", "scripted")
    monkeypatch.setattr(secrets, "get", lambda name: None)
    assert by_id(launch.checks())["anthropic_llm"].status == launch.TODO

    monkeypatch.setattr(secrets, "get", lambda name: "sk-ant-test" if name == "ANTHROPIC_API_KEY" else None)
    assert by_id(launch.checks())["anthropic_llm"].status == launch.TODO  # key present, mode still scripted

    monkeypatch.setattr(settings, "llm_mode", "anthropic")
    assert by_id(launch.checks())["anthropic_llm"].status == launch.OK


def test_store_ready_needs_adapter_keys_and_a_synced_catalog(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "store_adapter", "mock")
    assert by_id(launch.checks())["store_ready"].status == launch.TODO

    monkeypatch.setattr(settings, "store_adapter", "lemonsqueezy")
    fake = {"LEMONSQUEEZY_API_KEY": "k", "LEMONSQUEEZY_WEBHOOK_SECRET": "s"}
    monkeypatch.setattr(secrets, "get", lambda name: fake.get(name))
    assert by_id(launch.checks())["store_ready"].status == launch.TODO  # no synced products yet

    with session() as s:
        s.add(Product(sku="LIC-1", title="t", price_eur=10.0, external_id="ls-variant-1"))
    assert by_id(launch.checks())["store_ready"].status == launch.OK


def test_agentmail_live_needs_key_and_inbox(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "agentmail_live", False)
    assert by_id(launch.checks())["agentmail_live"].status == launch.TODO

    monkeypatch.setattr(settings, "agentmail_live", True)
    monkeypatch.setattr(secrets, "get", lambda name: "am-key" if name == "AGENTMAIL_API_KEY" else None)
    monkeypatch.setattr(settings, "agentmail_org_inbox", "owner@agentmail.to")
    assert by_id(launch.checks())["agentmail_live"].status == launch.OK


def test_alerts_owner_ok_with_telegram_or_ntfy(monkeypatch):
    monkeypatch.setattr(secrets, "get", lambda name: None)
    assert by_id(launch.checks())["alerts_owner"].status == launch.TODO

    monkeypatch.setattr(secrets, "get", lambda name: "https://ntfy.sh/mytopic" if name == "NTFY_URL" else None)
    assert by_id(launch.checks())["alerts_owner"].status == launch.OK

    monkeypatch.setattr(secrets, "get", lambda name: None)
    with session() as s:
        s.add(ChannelLink(channel="telegram", external_chat_id="123", active=True))
    assert by_id(launch.checks())["alerts_owner"].status == launch.OK


def test_alerts_heartbeat_flips_with_healthchecks_url(monkeypatch):
    monkeypatch.setattr(secrets, "get", lambda name: None)
    assert by_id(launch.checks())["alerts_heartbeat"].status == launch.TODO
    monkeypatch.setattr(secrets, "get", lambda name: "https://hc-ping.com/x" if name == "HEALTHCHECKS_PING_URL" else None)
    assert by_id(launch.checks())["alerts_heartbeat"].status == launch.OK


def test_kill_switch_and_safe_mode_block_readiness():
    assert by_id(launch.checks())["kill_switch_safe_mode"].status == launch.OK
    set_flag("kill_switch", True, "kaan")
    assert by_id(launch.checks())["kill_switch_safe_mode"].status == launch.TODO
    set_flag("kill_switch", False, "kaan")
    set_setting("safe_mode", "on")
    assert by_id(launch.checks())["kill_switch_safe_mode"].status == launch.TODO


def test_doctor_fail_blocks_but_warn_only_needs_a_look():
    row = by_id(launch.checks())["doctor_fail"]
    assert row.status in (launch.OK, launch.WARN)  # a fresh DB has no FAIL (credentials may WARN)

    from aecom.db import EventRow

    with session() as s:
        s.add(EventRow(topic="order.paid", payload={"order_id": 1}, attempts=5, dead=True, last_error="boom"))
    assert by_id(launch.checks())["doctor_fail"].status == launch.TODO


def test_migrations_head_is_ok_right_after_init():
    assert by_id(launch.checks())["migrations_head"].status == launch.OK


# --------------------------------------------------------------------------- acknowledgements: audited, human-only

def test_acknowledge_requires_a_known_key():
    with pytest.raises(ValueError, match="unknown"):
        launch.acknowledge("not-a-real-key", "kaan")


def test_acknowledge_requires_a_human_name():
    with pytest.raises(ValueError, match="human"):
        launch.acknowledge("spend_cap", "")
    with pytest.raises(ValueError, match="human"):
        launch.acknowledge("spend_cap", "   ")


def test_acknowledge_flips_the_check_and_is_audited():
    assert by_id(launch.checks())["ack_spend_cap"].status == launch.TODO
    launch.acknowledge("spend_cap", "kaan")
    assert by_id(launch.checks())["ack_spend_cap"].status == launch.OK
    with session() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "launch_ack_spend_cap")).first()
        assert row is not None and row.actor == "human:kaan" and row.decision == "on"


@pytest.mark.parametrize("key", list(launch.ACK_ITEMS))
def test_every_ack_item_is_acknowledgeable(key):
    assert by_id(launch.checks())[f"ack_{key}"].status == launch.TODO
    launch.acknowledge(key, "kaan")
    assert by_id(launch.checks())[f"ack_{key}"].status == launch.OK


def test_no_agent_tool_can_acknowledge_a_launch_item():
    suspicious_tools = {n for n in CATALOG if "launch" in n.lower()
                        and ("ack" in n.lower() or "acknowledge" in n.lower())}
    assert not suspicious_tools, f"an agent tool could acknowledge a launch item: {suspicious_tools}"

    policy = yaml.safe_load(get_settings().policy_file.read_text())
    rule_names = set((policy.get("rules") or {}).keys())
    suspicious_rules = {n for n in rule_names if "launch" in n.lower()
                        and ("ack" in n.lower() or "acknowledge" in n.lower())}
    assert not suspicious_rules, f"policy.yaml grants a rule for: {suspicious_rules}"


# --------------------------------------------------------------------------- ready()

def test_ready_is_false_while_any_check_is_todo():
    rows = launch.checks()
    assert any(c.status == launch.TODO for c in rows)
    assert launch.ready(rows) is False


def test_ready_is_true_once_nothing_is_todo(monkeypatch):
    from aecom import dashboard_auth

    dashboard_auth.set_password("a-password-12345", "a-password-12345", "kaan")
    settings = get_settings()
    monkeypatch.setattr(settings, "backup_enabled", True)
    monkeypatch.setattr(settings, "backup_target", "user@host:/backups")
    monkeypatch.setattr(settings, "llm_mode", "anthropic")
    monkeypatch.setattr(settings, "store_adapter", "lemonsqueezy")
    monkeypatch.setattr(settings, "agentmail_live", True)
    monkeypatch.setattr(settings, "agentmail_org_inbox", "owner@agentmail.to")
    fake = {"ANTHROPIC_API_KEY": "k", "LEMONSQUEEZY_API_KEY": "k", "LEMONSQUEEZY_WEBHOOK_SECRET": "s",
           "BACKUP_PASSPHRASE": "a-long-enough-passphrase", "AGENTMAIL_API_KEY": "k", "NTFY_URL": "https://ntfy.sh/t",
           "HEALTHCHECKS_PING_URL": "https://hc-ping.com/x"}
    monkeypatch.setattr(secrets, "get", lambda name: fake.get(name))
    with session() as s:
        s.add(Product(sku="LIC-1", title="t", price_eur=10.0, external_id="ls-variant-1"))
    for key in launch.ACK_ITEMS:
        launch.acknowledge(key, "kaan")
    monkeypatch.setattr(get_settings(), "backup_dir", get_settings().backup_dir)  # no-op, keeps real tmp dir below

    import tempfile

    monkeypatch.setattr(settings, "backup_dir", Path(tempfile.mkdtemp()))
    path = backup.create_backup(now=datetime.now(UTC))
    backup.update_manifest(path, verified=True, verified_at=datetime.now(UTC).isoformat())

    rows = launch.checks()
    todo = [c for c in rows if c.status == launch.TODO]
    assert todo == [], todo
    assert launch.ready(rows) is True


# --------------------------------------------------------------------------- progress()

def _real_order(amount=10.0):
    with session() as s:
        cus = s.scalar(select(Customer).where(Customer.email == "t@example.com"))
        if cus is None:
            cus = Customer(email="t@example.com")
            prd = Product(sku="DL-LAUNCH", title="t", price_eur=amount)
            s.add_all([cus, prd])
            s.flush()
        else:
            prd = s.scalar(select(Product).where(Product.sku == "DL-LAUNCH"))
        order = Order(customer_id=cus.id, product_id=prd.id, amount_eur=amount, status="paid",
                     external_id=f"ext-{uuid.uuid4().hex}")
        s.add(order)
        s.flush()
        order_id = order.id
    from aecom import ledger

    ledger.record_sale(order_id, amount, 0.0, 0.0)
    return order_id


def test_progress_starts_at_zero_of_the_threshold():
    from aecom import trust

    p = launch.progress()
    assert p["real_sales"] == 0
    assert p["threshold"] == int(trust.thresholds()["AUTONOMY_MIN_REAL_SALES"])
    assert p["remaining"] == p["threshold"]
    assert p["revenue_eur"] == 0.0
    assert "first real sale" in p["stage_text"]


def test_progress_counts_real_sales_and_revenue():
    _real_order(10.0)
    _real_order(20.0)
    p = launch.progress()
    assert p["real_sales"] == 2
    assert p["revenue_eur"] == 30.0
    assert p["remaining"] == p["threshold"] - 2
    assert f"{p['real_sales']} of {p['threshold']}" in p["stage_text"]


def test_progress_stage_text_changes_once_the_threshold_is_reached(monkeypatch):
    from aecom import trust

    monkeypatch.setattr(trust.secrets, "get", lambda name: "2" if name == "AUTONOMY_MIN_REAL_SALES" else None)
    _real_order(10.0)
    _real_order(10.0)
    p = launch.progress()
    assert p["remaining"] == 0 and p["fraction"] == 1.0
    assert "reached" in p["stage_text"]


# --------------------------------------------------------------------------- the dashboard tab (AppTest)

def _harness(tmp_path) -> Path:
    page = tmp_path / "_launch_harness.py"
    page.write_text(
        "import streamlit as st\n"
        "from _common import require_login, reviewer_name\n"
        "from _ops_launch import render_launch_tab\n"
        "require_login()\n"
        "render_launch_tab(reviewer_name())\n",
        encoding="utf-8",
    )
    return page


def _app(tmp_path) -> AppTest:
    from aecom import dashboard_auth as auth

    set_setting(auth.MODE_KEY, "off")
    at = AppTest.from_file(str(_harness(tmp_path)), default_timeout=60)
    at.session_state["reviewer_name"] = "kaan"
    return at.run()


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def test_launch_tab_renders_without_error(tmp_path):
    at = _app(tmp_path)
    assert not at.exception, _messages(at)
    assert any("real sales" in p.proto.text for p in at.get("progress"))
    assert any("To do" in m.value for m in at.markdown)


def test_launch_tab_acknowledge_button_flips_an_item_and_audits_it(tmp_path):
    at = _app(tmp_path)
    assert not at.exception, _messages(at)
    btn = next(b for b in at.button if b.key == "_launch_ack_spend_cap")
    at = btn.click().run()
    assert not at.exception, _messages(at)
    assert any("Acknowledged" in m.value for m in at.success)
    assert by_id(launch.checks())["ack_spend_cap"].status == launch.OK
    with session() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "launch_ack_spend_cap")).first()
        assert row is not None and row.actor == "human:kaan"
