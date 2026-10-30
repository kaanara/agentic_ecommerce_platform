"""J14: quarterly decision review - due list, evidence, recording a review, the reminder, and the dashboard tab.

Every test gets a fresh temp DB (tests/conftest.py's `fresh_company` fixture, autouse), seeded by
`aecom.bootstrap.init()`. Decision rows are created directly via `aecom.db.Decision` (not the `record_decision`
tool) so `created_at` can be controlled precisely for age-based due logic.
"""

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from aecom import decision_review
from aecom.db import AuditLog, Decision, DecisionReview, Vendor, now, session
from aecom.tools.catalog import CATALOG

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the partial does `from _common import ...`


def _make_decision(category: str = "strategy", title: str = "Go multi-channel", choice: str = "Shopify",
                   status: str = "accepted", age_days: int = 90, decided_by: str = "ceo") -> str:
    with session() as s:
        d = Decision(category=category, title=title, choice=choice, rationale="because reasons",
                    status=status, decided_by=decided_by, created_at=now() - timedelta(days=age_days))
        s.add(d)
        s.flush()
        return d.id


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


# --------------------------------------------------------------------------- current_quarter
def test_current_quarter_buckets_months_into_quarters():
    assert decision_review.current_quarter(now().replace(month=1, day=1)) == f"{now().year}-Q1"
    assert decision_review.current_quarter(now().replace(month=4, day=1)) == f"{now().year}-Q2"
    assert decision_review.current_quarter(now().replace(month=7, day=1)) == f"{now().year}-Q3"
    assert decision_review.current_quarter(now().replace(month=12, day=31)) == f"{now().year}-Q4"


# --------------------------------------------------------------------------- due()
def test_due_excludes_decisions_younger_than_60_days():
    _make_decision(age_days=10)
    assert decision_review.due() == []


def test_due_includes_accepted_decisions_at_least_60_days_old_oldest_first():
    newer = _make_decision(title="newer", age_days=61)
    older = _make_decision(title="older", age_days=400)
    rows = decision_review.due()
    assert [r["id"] for r in rows] == [older, newer]


def test_due_excludes_proposed_and_superseded():
    _make_decision(status="proposed", age_days=90)
    _make_decision(status="superseded", age_days=90)
    assert decision_review.due() == []


def test_due_excludes_decisions_already_reviewed_this_quarter():
    did = _make_decision(age_days=90)
    decision_review.record_review(did, "delivered", "", "Ada")
    assert decision_review.due() == []


def test_due_still_lists_a_decision_reviewed_in_an_earlier_quarter():
    did = _make_decision(age_days=90)
    with session() as s:
        s.add(DecisionReview(decision_id=did, quarter="2000-Q1", outcome="delivered", reviewed_by="Ada"))
    rows = decision_review.due()
    assert [r["id"] for r in rows] == [did]


# --------------------------------------------------------------------------- evidence()
def test_evidence_unknown_decision_raises():
    with pytest.raises(ValueError):
        decision_review.evidence("adr_missing")


def test_evidence_includes_decision_text_category_and_age():
    did = _make_decision(category="strategy", title="Go multi-channel", choice="Shopify", age_days=100)
    ev = decision_review.evidence(did)
    assert ev["title"] == "Go multi-channel"
    assert ev["category"] == "strategy"
    assert ev["choice"] == "Shopify"
    assert ev["rationale"] == "because reasons"
    assert ev["days_old"] >= 100


def test_evidence_has_no_facts_for_a_non_numeric_category():
    did = _make_decision(category="social", choice="Mastodon", age_days=90)
    ev = decision_review.evidence(did)
    assert ev["facts"] == {}
    assert "social" in ev["facts_note"]


def test_evidence_reports_vendor_status_when_the_choice_matches_a_vendor():
    with session() as s:
        s.add(Vendor(name="KeyForge", category="asset_supplier", monthly_cost_eur=29, status="active"))
    did = _make_decision(category="vendor", choice="KeyForge", age_days=90)
    ev = decision_review.evidence(did)
    assert ev["facts"]["vendor_status"] == "active"
    assert ev["facts"]["monthly_cost_eur"] == 29


def test_evidence_vendor_without_a_matching_record_gets_a_note_not_a_crash():
    did = _make_decision(category="vendor", choice="Nonexistent Co", age_days=90)
    ev = decision_review.evidence(did)
    assert ev["facts"] == {}
    assert "Nonexistent Co" in ev["facts_note"]


def test_evidence_strategy_category_reports_lifetime_pnl_facts():
    did = _make_decision(category="strategy", age_days=90)
    ev = decision_review.evidence(did)
    assert "net_revenue_eur_lifetime" in ev["facts"]
    assert "real_paid_orders_lifetime" in ev["facts"]


# --------------------------------------------------------------------------- record_review()
def test_record_review_rejects_unknown_outcome():
    did = _make_decision(age_days=90)
    with pytest.raises(ValueError):
        decision_review.record_review(did, "amazing", "note", "Ada")


def test_record_review_requires_a_reviewer_name():
    did = _make_decision(age_days=90)
    with pytest.raises(ValueError):
        decision_review.record_review(did, "delivered", "", "")


def test_record_review_requires_a_note_unless_delivered():
    did = _make_decision(age_days=90)
    with pytest.raises(ValueError):
        decision_review.record_review(did, "partly", "", "Ada")
    decision_review.record_review(did, "partly", "only half shipped", "Ada")  # fine with a note


def test_record_review_delivered_does_not_need_a_note():
    did = _make_decision(age_days=90)
    out = decision_review.record_review(did, "delivered", "", "Ada")
    assert out["outcome"] == "delivered"


def test_record_review_unknown_decision_raises():
    with pytest.raises(ValueError):
        decision_review.record_review("adr_missing", "delivered", "", "Ada")


def test_record_review_upserts_the_same_quarter_row():
    did = _make_decision(age_days=90)
    decision_review.record_review(did, "partly", "half shipped", "Ada")
    decision_review.record_review(did, "delivered", "", "Ada")  # correcting the same quarter's verdict
    rows = decision_review.list_reviews(did)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "delivered"


def test_record_review_audits_as_human():
    did = _make_decision(age_days=90)
    decision_review.record_review(did, "delivered", "all good", "Ada")
    with session() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action == "decision_review")))
    assert len(rows) == 1
    assert rows[0].actor == "human:Ada"
    assert rows[0].decision == "delivered"
    assert rows[0].detail["decision_id"] == did


# --------------------------------------------------------------------------- list_reviews()
def test_list_reviews_filters_by_decision_and_orders_recent_first():
    a = _make_decision(title="a", age_days=90)
    b = _make_decision(title="b", age_days=90)
    decision_review.record_review(a, "delivered", "", "Ada")
    decision_review.record_review(b, "not_delivered", "scrapped", "Ada")
    assert {r["decision_id"] for r in decision_review.list_reviews()} == {a, b}
    assert [r["decision_id"] for r in decision_review.list_reviews(a)] == [a]


# --------------------------------------------------------------------------- remind_due_reviews()
def test_remind_due_reviews_sends_nothing_when_none_due(monkeypatch):
    sent = []
    from aecom import notify

    monkeypatch.setattr(notify, "route", lambda event: sent.append(event) or {})
    out = decision_review.remind_due_reviews()
    assert out["sent"] is False
    assert sent == []


def test_remind_due_reviews_sends_once_and_names_the_count(monkeypatch):
    _make_decision(age_days=90)
    _make_decision(age_days=120)
    sent = []
    from aecom import notify

    monkeypatch.setattr(notify, "route", lambda event: sent.append(event) or {})
    first = decision_review.remind_due_reviews()
    assert first["sent"] is True
    assert len(sent) == 1
    assert "2" in sent[0]["text"]

    second = decision_review.remind_due_reviews()  # same quarter: idempotent, no second notification
    assert second["sent"] is False
    assert len(sent) == 1


def test_remind_due_reviews_notify_failure_does_not_raise(monkeypatch):
    _make_decision(age_days=90)
    from aecom import notify

    def _boom(event):
        raise RuntimeError("channel down")

    monkeypatch.setattr(notify, "route", _boom)
    out = decision_review.remind_due_reviews()  # must not raise
    assert out["sent"] is False


# --------------------------------------------------------------------------- no agent tool writes reviews
def test_no_agent_tool_can_record_a_decision_review():
    assert "record_review" not in CATALOG
    assert decision_review.record_review not in (t.fn for t in CATALOG.values())
    assert not [n for n in CATALOG if "decision_review" in n]


# --------------------------------------------------------------------------- dashboard tab (AppTest)
def _tab_named():
    from _acct_decisions import render_decision_review_tab

    render_decision_review_tab("Ada")


def _tab_blank():
    from _acct_decisions import render_decision_review_tab

    render_decision_review_tab("")


def test_tab_with_nothing_due_shows_empty_state():
    at = AppTest.from_function(_tab_named).run()
    assert not at.exception, _messages(at)
    assert any("Nothing due this quarter" in m.value for m in at.markdown)


def test_tab_lists_a_due_decision_with_evidence():
    _make_decision(category="strategy", title="Go multi-channel", age_days=90)
    at = AppTest.from_function(_tab_named).run()
    assert not at.exception, _messages(at)
    markdown = " ".join(m.value for m in at.markdown)
    assert "Go multi-channel" in markdown
    assert at.radio
    assert at.text_area


def test_tab_save_without_note_shows_inline_message_and_does_not_save():
    did = _make_decision(age_days=90)
    at = AppTest.from_function(_tab_named).run()
    at.radio(key=f"_decrev_outcome_{did}").set_value("not_delivered")
    at = at.run()
    captions = " ".join(c.value for c in at.caption)
    assert "A note is required" in captions
    at.button(key=f"_decrev_save_{did}").click().run()
    assert decision_review.list_reviews(did) == []


def test_tab_save_without_reviewer_name_is_blocked():
    did = _make_decision(age_days=90)
    at = AppTest.from_function(_tab_blank).run()
    at.button(key=f"_decrev_save_{did}").click().run()
    assert not at.exception, _messages(at)
    assert any("Enter your name" in w.value for w in at.warning)
    assert decision_review.list_reviews(did) == []


def test_tab_save_records_the_review_and_shows_history():
    did = _make_decision(age_days=90)
    at = AppTest.from_function(_tab_named).run()
    at.radio(key=f"_decrev_outcome_{did}").set_value("delivered")
    at = at.run()
    at.button(key=f"_decrev_save_{did}").click().run()
    assert decision_review.list_reviews(did)[0]["outcome"] == "delivered"
    at = AppTest.from_function(_tab_named).run()
    assert not at.exception, _messages(at)
    assert at.dataframe  # the history table
