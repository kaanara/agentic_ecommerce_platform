"""K1 error tracking (ops_errors, secret masking, optional Sentry) and batched AI cost booking."""

import asyncio

import pytest
from sqlalchemy import select

from aecom import ledger, observability
from aecom.bus import events
from aecom.bus.events import HandlerFailed, drain, publish
from aecom.company_settings import get_setting
from aecom.db import AgentRun, ErrorRow, session

FAKE_AM = "am_us_FAKE0123456789abcdef"
FAKE_ANT = "sk-ant-api03-FAKE_abc-123"


def errors() -> list[ErrorRow]:
    with session() as s:
        return list(s.scalars(select(ErrorRow).order_by(ErrorRow.id)))


def _raise(msg: str) -> None:
    raise ValueError(msg)


def test_record_error_stores_traceback_and_masks_secrets(monkeypatch):
    monkeypatch.setenv("SOME_VENDOR_TOKEN", "vendor-token-value-42")
    try:
        _raise(f"auth failed key={FAKE_AM} ant={FAKE_ANT} hdr=Bearer abc.def-ghi vendor-token-value-42")
    except ValueError as exc:
        eid = observability.record_error("agent", exc, run_id="run_x",
                                         context={"headers": {"Authorization": f"Bearer {FAKE_AM}"}, "n": 1})
    assert eid is not None
    (row,) = errors()
    assert row.id == eid and row.component == "agent" and row.run_id == "run_x" and row.exc_type == "ValueError"
    assert "Traceback (most recent call last)" in row.traceback and "_raise" in row.traceback
    blob = row.message + row.traceback + str(row.context)
    for secret in (FAKE_AM, FAKE_ANT, "abc.def-ghi", "vendor-token-value-42"):
        assert secret not in blob
    assert "***" in row.message and row.context["n"] == 1 and row.sentry_event_id is None


def test_record_error_never_raises_when_db_write_fails(monkeypatch):
    import aecom.db

    def broken_session():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(aecom.db, "session", broken_session)
    assert observability.record_error("agent", RuntimeError("x")) is None


def test_sentry_is_a_noop_without_dsn(monkeypatch):
    from aecom.secrets import secrets

    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(secrets, "_dotenv", {})
    monkeypatch.setattr(observability, "_sentry_ready", None)
    assert observability._sentry_capture(RuntimeError("x")) is None
    assert observability._sentry_ready is None  # never initialised
    observability.record_error("agent", RuntimeError("x"))
    assert errors()[0].sentry_event_id is None


def test_sub_cent_ai_costs_are_batched_into_the_ledger(fresh_company, monkeypatch):
    from aecom.graph import agents

    monkeypatch.setattr(type(fresh_company), "cost_eur", lambda self, tier, tin, tout: 0.0003)

    async def many() -> None:
        for _ in range(100):
            assert (await agents.run_agent("support", "hello")).status == "ok"

    asyncio.run(many())
    booked = ledger.balances().get("ai_expense", 0.0)
    assert booked == pytest.approx(0.03, abs=0.01) and booked > 0
    with session() as s:
        unbooked = [r.cost_eur for r in s.scalars(select(AgentRun).where(AgentRun.cost_booked.is_(False)))]
    carry = float(get_setting("ai_cost_carry_eur", "0"))
    assert booked + sum(unbooked) + carry == pytest.approx(0.03, abs=1e-9)  # nothing lost to rounding


def test_crashed_agent_run_creates_error_row(monkeypatch):
    from aecom.graph import agents

    def boom(*a, **k):
        raise RuntimeError(f"bad key {FAKE_ANT}")

    monkeypatch.setattr(agents, "get_model", boom)
    result = asyncio.run(agents.run_agent("support", "hello", run_id="run_crash"))
    assert result.status == "error"
    (row,) = errors()
    assert row.component == "agent" and row.run_id == "run_crash" and row.exc_type == "RuntimeError"
    assert "boom" in row.traceback and FAKE_ANT not in row.message + row.traceback


def test_unexpected_handler_exception_creates_error_row_but_handler_failed_does_not(monkeypatch):
    def flaky(payload):
        raise HandlerFailed("supplier offline, retry later")

    def buggy(payload):
        raise KeyError("missing_field")

    monkeypatch.setitem(events._subscribers, "test.flaky", [flaky])
    monkeypatch.setitem(events._subscribers, "test.buggy", [buggy])
    publish("test.flaky", {})
    drain()
    assert errors() == []
    eid = publish("test.buggy", {})
    drain()
    (row,) = errors()
    assert row.component == "event_bus" and row.event_id == eid and row.exc_type == "KeyError"
    assert "buggy" in row.traceback and row.context["handler"] == "buggy"
