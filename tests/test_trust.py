"""Phase 5 / G4: the trust report and evidence-gated promotion."""

import ast
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from aecom import autonomy, cli, flash, incidents, reports, trust
from aecom.audit import audit
from aecom.company_settings import set_setting
from aecom.db import Approval, AuditLog, Customer, Order, Product, now, session

TOOL = "refund_order"


@pytest.fixture
def config(monkeypatch):
    values: dict[str, str] = {}
    monkeypatch.setattr(trust.secrets, "get", lambda name: values.get(name))
    return values


def _orders(n, external=True, status="paid"):
    with session() as s:
        cus = s.scalar(select(Customer).where(Customer.email == "t@example.com"))
        if cus is None:
            cus = Customer(email="t@example.com")
            prd = Product(sku="DL-TRUST", title="t", price_eur=10.0)
            s.add_all([cus, prd])
            s.flush()
        else:
            prd = s.scalar(select(Product).where(Product.sku == "DL-TRUST"))
        for _ in range(n):
            s.add(Order(customer_id=cus.id, product_id=prd.id, amount_eur=10.0, status=status,
                        external_id=f"ext-{uuid.uuid4().hex}" if external else None))


def _approvals(approved=0, edited=0, rejected=0, expired=0, pending=0, tool=TOOL, at=None):
    at = at or now()
    spec = [("approved", False)] * approved + [("approved", True)] * edited + [("rejected", False)] * rejected \
        + [("expired", False)] * expired + [("pending", False)] * pending
    with session() as s:
        for status, ed in spec:
            s.add(Approval(agent_id="support", tool_name=tool, args={}, risk_tier="T2", reason="r", status=status,
                           args_edited=ed, decided_by="kaan" if status in ("approved", "rejected") else None,
                           created_at=at - timedelta(minutes=10),
                           decided_at=at if status in ("approved", "rejected") else None))


def _ready():
    _orders(10)
    _approvals(approved=20)


def test_stats_on_hand_made_approvals():
    _approvals(approved=6, edited=2, rejected=2, expired=1, pending=3)
    st = trust.stats(TOOL)
    assert (st["escalations"], st["decided"], st["approved"], st["rejected"], st["expired"], st["pending"],
            st["edited"]) == (14, 10, 8, 2, 1, 3, 2)
    assert st["unchanged_rate"] == 0.6 and st["reject_rate"] == 0.2
    assert st["median_decision_minutes"] == 10.0 and st["last_decision_at"]
    assert trust.stats("update_price")["unchanged_rate"] is None


def test_stats_autonomous_actions_and_since():
    audit("support", TOOL, "allow", {"autonomous": True, "autonomy_stage": 2})
    audit("support", TOOL, "allow", {"autonomous": False})
    audit("support", "update_price", "allow", {"autonomous": True})
    assert trust.stats(TOOL)["autonomous_actions"] == 1
    assert trust.stats(TOOL, now() + timedelta(hours=1))["autonomous_actions"] == 0


def test_real_sales_counts_only_real_paid_orders():
    _orders(3)
    _orders(2, external=False)
    _orders(1, status="refunded")
    _orders(1, status="delivered")
    assert trust.real_sales() == 4


def test_eligible_when_everything_is_met():
    _ready()
    e = trust.eligibility(TOOL)
    assert e["eligible"] and e["missing"] == [] and (e["stage"], e["next_stage"]) == (1, 2)
    assert e["evidence"]["real_sales"] == 10 and e["evidence"]["decisions"] == 20


def test_each_criterion_blocks_individually():
    _ready()

    def missing():
        return " | ".join(trust.eligibility(TOOL)["missing"])

    # sales
    with session() as s:
        s.delete(s.scalars(select(Order)).first())
    assert "real sales 9 < 10" in missing()
    _orders(1)
    assert trust.eligibility(TOOL)["eligible"]
    # unchanged rate
    _approvals(edited=2)
    assert "approve-unchanged rate 90.9% < 95%" in missing()


def test_decision_count_blocks():
    _orders(10)
    _approvals(approved=19)
    assert "decisions since the last stage change 19 < 20" in trust.eligibility(TOOL)["missing"][0]


def test_rejection_and_expiry_block():
    _ready()
    _approvals(rejected=1)
    assert any("rejection" in m for m in trust.eligibility(TOOL)["missing"])
    _approvals(expired=1, tool="update_price")
    with session() as s:
        s.query(Approval).filter(Approval.status == "rejected").delete()
    assert trust.eligibility(TOOL)["eligible"]
    _approvals(expired=1)
    assert any("expired" in m for m in trust.eligibility(TOOL)["missing"])


def test_open_p1_safe_mode_and_kill_switch_block():
    _ready()
    inc, _ = incidents.raise_incident("heartbeat", "P1", "x")
    assert "an open P1 incident" in trust.eligibility(TOOL)["missing"]
    incidents.raise_incident("other", "P2", "y")
    incidents.resolve(inc["id"], "kaan")
    assert trust.eligibility(TOOL)["eligible"]
    set_setting("safe_mode", "on")
    assert "safe mode is on" in trust.eligibility(TOOL)["missing"]
    set_setting("safe_mode", "off")
    set_setting("kill_switch", "on")
    assert "kill switch is on" in trust.eligibility(TOOL)["missing"]


def test_acknowledged_p1_still_blocks():
    _ready()
    inc, _ = incidents.raise_incident("heartbeat", "P1", "x")
    incidents.acknowledge(inc["id"], "kaan")
    assert not trust.eligibility(TOOL)["eligible"]


def test_not_promotable_and_highest_stage():
    _ready()
    assert "not a promotable action" in trust.eligibility("send_email")["missing"][0]
    set_setting(autonomy.STAGE_KEY.format(tool=TOOL), "3")
    assert "highest defined stage" in trust.eligibility(TOOL)["missing"][0]


def test_only_decisions_since_last_stage_change_count():
    _ready()
    assert trust.promote(TOOL, "kaan", "go") == 2
    e = trust.eligibility(TOOL)
    assert e["stage"] == 2 and not e["eligible"]
    assert e["evidence"]["decisions"] == 0 and "decisions since the last stage change 0 < 20" in e["missing"][0]
    _approvals(approved=20, at=now() + timedelta(minutes=5))
    assert trust.eligibility(TOOL, now() + timedelta(minutes=10))["eligible"]


def test_cooldown_after_demotion(config):
    _ready()
    trust.promote(TOOL, "kaan")
    autonomy.demote(TOOL, "bad outcome")
    _approvals(approved=20, at=now() + timedelta(minutes=1))
    soon = now() + timedelta(days=13)
    assert any("cooldown" in m for m in trust.eligibility(TOOL, soon)["missing"])
    assert trust.eligibility(TOOL, now() + timedelta(days=15))["eligible"]
    config["AUTONOMY_COOLDOWN_DAYS"] = "30"
    assert any("cooldown" in m for m in trust.eligibility(TOOL, now() + timedelta(days=15))["missing"])


def test_thresholds_from_secrets(config):
    _orders(2)
    _approvals(approved=3)
    assert not trust.eligibility(TOOL)["eligible"]
    config.update(AUTONOMY_MIN_REAL_SALES="2", AUTONOMY_MIN_DECISIONS="3", AUTONOMY_MIN_UNCHANGED_RATE="1")
    assert trust.eligibility(TOOL)["eligible"]
    config["AUTONOMY_MIN_DECISIONS"] = "junk"
    assert trust.thresholds()["AUTONOMY_MIN_DECISIONS"] == 20


def test_promote_rechecks_on_the_server_and_cannot_be_forced():
    _orders(9)
    _approvals(approved=20)
    with pytest.raises(ValueError, match="real sales 9 < 10"):
        trust.promote(TOOL, "kaan", "please")
    with pytest.raises(TypeError):
        trust.promote(TOOL, "kaan", force=True)  # type: ignore[call-arg]
    assert autonomy.current_stage(TOOL) == 1
    with pytest.raises(ValueError, match="by"):
        trust.promote(TOOL, " ")
    _orders(1)
    with pytest.raises(ValueError, match="human"):
        trust.promote(TOOL, "system")
    assert autonomy.current_stage(TOOL) == 1


def test_promote_records_evidence_and_raises_by_one():
    _ready()
    assert trust.promote(TOOL, "kaan", "looks good") == 2
    assert autonomy.current_stage(TOOL) == 2
    with session() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "autonomy_stage")).one()
    assert row.actor == "human:kaan" and row.decision == "promoted"
    d = row.detail
    assert (d["tool"], d["from"], d["to"], d["reason"]) == (TOOL, 1, 2, "looks good")
    assert d["evidence"]["real_sales"] == 10 and d["evidence"]["decisions"] == 20 and d["evidence"]["checked_at"]
    with pytest.raises(ValueError):  # new window is empty, so a second promotion is refused
        trust.promote(TOOL, "kaan")


def test_demote():
    _ready()
    trust.promote(TOOL, "kaan")
    with pytest.raises(ValueError):
        trust.demote(TOOL, "")
    with pytest.raises(ValueError, match="not a promotable"):
        trust.demote("send_email", "kaan")
    assert trust.demote(TOOL, "kaan", "too risky") == 1
    with session() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.decision == "demoted")).one()
    assert row.actor == "human:kaan" and row.detail["reason"] == "too risky"


def test_report_has_a_row_per_promotable_tool():
    rows = {r["tool"]: r for r in trust.report()}
    assert set(rows) == {"refund_order", "update_price", "launch_campaign", "record_expense", "register_vendor"}
    assert rows[TOOL]["stats"]["escalations"] == 0 and rows[TOOL]["eligible"] is False


def test_log_lists_changes_and_autonomous_actions():
    _ready()
    trust.promote(TOOL, "kaan", "go")
    audit("support", TOOL, "allow", {"autonomous": True, "autonomy_stage": 2, "args": {"amount_eur": 80}})
    kinds = [(r["kind"], r["tool"]) for r in trust.log(7)]
    assert kinds == [("promoted", TOOL), ("autonomous", TOOL)]
    assert trust.log(7, now() + timedelta(days=8)) == []


# ------------------------------------------------------------------ CLI

def test_cli_report_prints_table(capsys):
    cli.main(["autonomy"])
    out = capsys.readouterr().out
    assert "refund_order" in out and "real sales 0 < 10" in out
    cli.main(["autonomy", "report"])


def test_cli_by_is_required_and_errors_exit_2(capsys):
    for argv in (["autonomy", "promote", TOOL], ["autonomy", "demote", TOOL], ["autonomy", "promote"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2
    assert "--by is required" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        cli.main(["autonomy", "promote", TOOL, "--by", "kaan"])
    assert e.value.code == 2
    assert "real sales" in capsys.readouterr().err
    assert autonomy.current_stage(TOOL) == 1


def test_cli_promote_demote_and_log(capsys):
    _ready()
    cli.main(["autonomy", "promote", TOOL, "--by", "kaan", "--reason", "ok"])
    assert "stage 2" in capsys.readouterr().out and autonomy.current_stage(TOOL) == 2
    cli.main(["autonomy", "log", "--days", "3"])
    assert '"promoted"' in capsys.readouterr().out
    cli.main(["autonomy", "demote", TOOL, "--by", "kaan"])
    assert autonomy.current_stage(TOOL) == 1
    with pytest.raises(SystemExit) as e:
        cli.main(["autonomy", "demote", "send_email", "--by", "kaan"])
    assert e.value.code == 2


# ------------------------------------------------------------------ flash and governance

def test_flash_data_and_body():
    _ready()
    trust.promote(TOOL, "kaan")
    audit("support", TOOL, "allow", {"autonomous": True})
    audit("system", "autonomy_stage", "demoted", {"tool": "update_price", "from": 2, "to": 1})
    data = flash.build_flash()
    assert data["autonomy"] == {"autonomous_actions_24h": 1, "demotions_24h": 1, "promoted": {TOOL: 2}}
    body = flash._body("Commentary.", data, None)
    assert "Autonomy" in body and "Autonomous actions (24h): 1" in body and f"{TOOL} stage 2" in body
    assert "Promoted actions: none" in flash._autonomy_section({"autonomy": {**data["autonomy"], "promoted": {}}})
    assert not reports.number_check(flash._numbers_only(data, None), reports.build_data("daily_flash", "yesterday"))


def test_governance_controls_include_stage_changes():
    _ready()
    trust.promote(TOOL, "kaan", "earned it")
    autonomy.demote(TOOL, "refund went wrong")
    start, end = now() - timedelta(days=1), now() + timedelta(days=1)
    c = reports._governance(start, end)["controls"]
    assert c["autonomy_stage"] == 2 and c["changes"] == 2
    ev = [e for e in c["events"] if e["setting"] == "autonomy_stage"]
    assert [(e["actor"], e["state"], e["tool"], e["reason"]) for e in ev] == [
        ("human:kaan", "promoted", TOOL, "earned it"), ("system", "demoted", TOOL, "refund went wrong")]
    lines = reports._governance_lines(reports._governance(start, end))
    assert any("autonomy stage 2" in line for line in lines)
    assert any("promoted by human:kaan (earned it)" in line for line in lines)


def test_trust_page_requires_login_first():
    page = Path(__file__).resolve().parents[1] / "dashboard" / "app_pages" / "trust.py"
    src = page.read_text()
    for stmt in ast.parse(src).body:
        if isinstance(stmt, ast.Import | ast.ImportFrom):
            continue
        call = stmt.value if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) else None
        name = ast.unparse(call.func) if call else None
        if name == "require_login":
            break
        assert name == "st.set_page_config", f"{name or ast.unparse(stmt)} runs before require_login()"
    else:
        pytest.fail("6_Trust.py never calls require_login()")
    assert "trust.promote" in src and "trust.demote" in src and "Hard caps never move" in src


def test_a_rate_just_below_the_bar_does_not_round_up_into_eligibility(monkeypatch):
    """94.996% displays as 95.00% when rounded to 4 places; eligibility must use the exact ratio."""
    from aecom import trust

    monkeypatch.setattr(trust, "stats", lambda tool, since=None: {
        "decided": 25000, "approved": 24999, "rejected": 0, "expired": 0, "edited": 1250, "pending": 0, "escalations": 25000,
        "unchanged_rate": 0.95, "unchanged_exact": 0.94996, "reject_rate": 0.0, "median_decision_minutes": 1.0,
        "autonomous_actions": 0, "last_decision_at": None})
    e = trust.eligibility("refund_order")
    assert e["eligible"] is False and any("approve-unchanged rate" in m for m in e["missing"])


def test_autonomy_log_never_shows_customer_text(fresh_company):
    from aecom.audit import audit
    from aecom import trust

    audit("support", "refund_order", "allow", {"autonomous": True, "autonomy_stage": 2,
                                               "args": {"order_id": "ord_1", "amount_eur": 120.0, "reason": "mail from jane@example.com"}})
    shown = " ".join(r["detail"] for r in trust.log(days=1))
    assert "jane@example.com" not in shown and "120.0" in shown and "reason" in shown  # amount visible, text reduced to a key
