"""CLI commands: chaos (throwaway DB), agents-pause, reconcile."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select

from aecom import cli
from aecom.bootstrap import seed_demo
from aecom.company_settings import agents_paused_on, get_setting
from aecom.db import EventRow, Order, now, session
from aecom.store.adapter import get_store


def _order_count() -> int:
    with session() as s:
        return s.scalar(select(func.count()).select_from(Order))


def test_chaos_runs_on_throwaway_db_and_leaves_configured_db_alone(fresh_company, capsys):
    url = fresh_company.database_url
    try:
        cli.main(["chaos", "duplicate-paid", "--seed", "3"])
    except SystemExit as e:
        assert e.code in (0, None)
    out = capsys.readouterr().out
    assert "duplicate-paid (seed 3): 0 violation(s)" in out
    assert fresh_company.database_url == url
    assert _order_count() == 0  # the scenario's orders went to the throwaway DB


def test_chaos_exits_1_on_violation(fresh_company, capsys, monkeypatch):
    import aecom.chaos

    url = fresh_company.database_url
    monkeypatch.setattr(aecom.chaos, "run_scenario", lambda n, seed: {
        "scenario": n, "seed": seed, "stats": {},
        "violations": [{"invariant": "ledger_balanced", "detail": "off by 1"}]})
    with pytest.raises(SystemExit) as e:
        cli.main(["chaos", "delivery-crash"])
    assert e.value.code == 1
    assert "delivery-crash (seed 0): 1 violation(s) - ledger_balanced: off by 1" in capsys.readouterr().out
    assert fresh_company.database_url == url  # restored even on failure


def test_chaos_unknown_scenario(fresh_company):
    with pytest.raises(SystemExit) as e:
        cli.main(["chaos", "nope"])
    assert "unknown chaos scenario" in str(e.value.code)


def test_agents_pause_toggles_setting(fresh_company, capsys):
    cli.main(["agents-pause", "on"])
    assert agents_paused_on() and get_setting("agents_paused") == "on"
    cli.main(["agents-pause", "off"])
    assert not agents_paused_on()
    assert "agents paused off" in capsys.readouterr().out


def test_reconcile_prints_counts_and_drains(fresh_company, capsys):
    seed_demo()
    order_id = get_store().create_order("late@example.com", "LIC-FOCUSFLOW-PRO-V1", "DE")["id"]
    with session() as s:  # the order.paid event was lost and the order is an hour old
        s.execute(delete(EventRow))
        s.get(Order, order_id).created_at = now() - timedelta(hours=1)
    capsys.readouterr()
    cli.main(["reconcile", "--older-than-minutes", "30"])
    out = capsys.readouterr().out
    counts = json.loads(out[:out.index("}") + 1])
    assert counts == {"checked": 1, "requeued": 1, "already_pending": 0}
    assert "attempted" in out
    with session() as s:
        assert s.get(Order, order_id).status == "delivered"
