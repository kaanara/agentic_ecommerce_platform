"""B/L2: deterministic monitors open and auto-resolve incidents, alert routing (ntfy, email), heartbeat, /health."""

from datetime import timedelta

import httpx
import pytest
from aecom import alerts, db, incidents, monitors
from aecom.api import app as api_app
from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import drain, publish
from aecom.company_settings import set_setting
from aecom.db import AgentRun, Approval, ErrorRow, EventRow, LedgerEntry, LicenseKey, Order, session
from aecom.secrets import secrets
from aecom.store.adapter import get_store
from fastapi.testclient import TestClient
from sqlalchemy import select

SKU = "LIC-FOCUSFLOW-PRO-V1"
LATER = timedelta(minutes=20)  # past the 15-min order grace


@pytest.fixture(autouse=True)
def config(monkeypatch):
    """No alert config from the environment or a developer's .env; tests opt in via `config.set`."""
    values: dict[str, str] = {}
    monkeypatch.setattr(secrets, "get", lambda name: values.get(name))
    return values


@pytest.fixture
def net(monkeypatch):
    """Records every httpx call made by alerts; `net.fail = True` makes them raise."""
    class Net:
        calls: list[tuple] = []
        fail = False

    def call(method):
        def fn(url, **kw):
            Net.calls.append((method, url, kw))
            if Net.fail:
                raise httpx.ConnectError(f"cannot reach {url}")
            return httpx.Response(200, request=httpx.Request(method, url))
        return fn

    Net.calls = []
    monkeypatch.setattr(alerts.httpx, "post", call("POST"))
    monkeypatch.setattr(alerts.httpx, "get", call("GET"))
    return Net


@pytest.fixture
def mail(monkeypatch):
    sent: list[tuple] = []

    class FakeMail:
        def send(self, to, subject, text, inbox=None):
            sent.append((to, subject, text))
            return {"email_id": "eml_x"}

    monkeypatch.setattr(alerts, "AgentMail", FakeMail)
    return sent


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)
    seed_demo()
    orders = [get_store().create_order(f"c{i}@example.com", SKU, "DE")["id"] for i in range(2)]
    drain()
    return orders


def active(monitor):
    return {i["subject"]: i for i in incidents.list_incidents() if i["monitor"] == monitor and i["status"] in incidents.ACTIVE}


def later():
    return db.now() + LATER


def test_clean_demo_has_no_incidents(demo):
    r = monitors.run_monitors(now=later())
    assert r["failing"] == {} and r["monitor_errors"] == [] and incidents.open_counts()["P1"] == 0


# ------------------------------------------------------------------------------------- P1 monitors
def _undelivered(demo):
    with session() as s:
        s.get(Order, demo[0]).status = "paid"
    def fix():
        with session() as s:
            s.get(Order, demo[0]).status = "delivered"
    return demo[0], fix


def _unemailed(demo):
    with session() as s:
        s.get(Order, demo[0]).email_sent = False
    def fix():
        with session() as s:
            s.get(Order, demo[0]).email_sent = True
    return demo[0], fix


def _ledger(demo):
    with session() as s:
        s.add(LedgerEntry(txn_id="txn_bad", account="cash", debit=10.0, credit=0.0))
    def fix():
        with session() as s:
            s.add(LedgerEntry(txn_id="txn_bad", account="revenue", debit=0.0, credit=10.0))
    return "ledger", fix


def _dup_key(demo):
    with session() as s:
        spare = s.scalars(select(LicenseKey).where(LicenseKey.status == "available")).first()
        spare.status, spare.order_id = "assigned", demo[0]
        spare_id = spare.id
    def fix():
        with session() as s:
            k = s.get(LicenseKey, spare_id)
            k.status, k.order_id = "available", None
    return "license_keys", fix


def _dead_order_event(demo):
    eid = publish("order.refund_requested", {"order_id": demo[0]})
    with session() as s:
        e = s.get(EventRow, eid)
        e.dead, e.attempts, e.last_error = True, 5, "boom"
    def fix():
        with session() as s:
            s.get(EventRow, eid).dead, s.get(EventRow, eid).processed = False, True
    return str(eid), fix


def _restock(demo):
    eid = publish("inventory.restock_failed", {"sku": SKU, "reason": "no key source"})
    with session() as s:
        s.get(EventRow, eid).processed = True
    def fix():  # the failure ages out of the 24 h window
        with session() as s:
            s.get(EventRow, eid).ts = db.now() - timedelta(hours=25)
    return SKU, fix


@pytest.mark.parametrize("make,monitor", [
    (_undelivered, "undelivered_order"), (_unemailed, "unemailed_order"), (_ledger, "ledger_imbalance"),
    (_dup_key, "duplicate_key"), (_dead_order_event, "order_dead_letter"), (_restock, "restock_failed"),
])
def test_p1_monitor_opens_and_auto_resolves(demo, make, monitor):
    subject, fix = make(demo)
    monitors.run_monitors(now=later())
    inc = active(monitor)
    assert list(inc) == [subject] and inc[subject]["severity"] == "P1"
    assert incidents.open_counts()["P1"] == 1

    fix()
    monitors.run_monitors(now=later())
    assert active(monitor) == {}
    assert incidents.get_incident(inc[subject]["id"])["status"] == "resolved"


def test_duplicate_key_alert_never_contains_the_key_value(demo, config, net, mail, monkeypatch):
    from aecom.chaos import invariants

    with session() as s:
        k = s.scalars(select(LicenseKey).where(LicenseKey.order_id == demo[0])).first()
        value = k.key
    monkeypatch.setattr(invariants, "no_key_on_two_orders",
                        lambda: [{"check": "no_key_on_two_orders", "detail": f"key {value} is assigned to several orders"}])
    config.update(NTFY_URL="https://ntfy.example/aecom", ALERT_EMAIL="ops@example.com")
    monitors.run_monitors(now=later())
    inc = active("duplicate_key")["license_keys"]
    assert value not in inc["detail"] and value not in inc["subject"]
    assert net.calls and mail
    assert all(value not in repr(c) for c in net.calls) and all(value not in repr(m) for m in mail)


def test_duplicate_key_detail_names_order_not_key(demo):
    _dup_key(demo)
    with session() as s:
        values = s.scalars(select(LicenseKey.key).where(LicenseKey.order_id == demo[0])).all()
    monitors.run_monitors(now=later())
    detail = active("duplicate_key")["license_keys"]["detail"]
    assert demo[0] in detail and not any(v in detail for v in values)


def test_young_undelivered_order_is_not_an_incident(demo):
    with session() as s:
        s.get(Order, demo[0]).status = "paid"
    monitors.run_monitors(now=db.now() + timedelta(minutes=5))
    assert active("undelivered_order") == {}


# ------------------------------------------------------------------------------------- P2 monitors
def test_p2_monitors(demo):
    t = later()
    with session() as s:
        s.add(AgentRun(id="run_stuck", run_id="r", agent_id="ceo", model="m", task="t", started_at=t - timedelta(hours=1)))
        s.add(Approval(id="apr_old", agent_id="ceo", tool_name="x", args={}, risk_tier="high", reason="r",
                       created_at=t - timedelta(hours=25)))
        s.add(ErrorRow(component="agent", exc_type="E", message="m", ts=t - timedelta(minutes=5)))
    eid = publish("vendor.thing", {})
    with session() as s:
        s.get(EventRow, eid).dead = True
    set_setting("kill_switch", "on")

    r = monitors.run_monitors(now=t)
    assert set(r["failing"]) == {"stuck_run", "approval_ageing", "errors", "other_dead_letter"}
    assert list(active("stuck_run")) == ["run_stuck"] and list(active("approval_ageing")) == ["apr_old"]
    assert list(active("other_dead_letter")) == [str(eid)] and active("order_dead_letter") == {}
    assert active("errors")["ops_errors"]["detail"].startswith("1 unexpected")
    assert {i["severity"] for i in incidents.list_incidents()} == {"P2"}

    monitors.run_monitors(now=t + timedelta(minutes=61))  # kill switch on for over an hour
    assert list(active("kill_switch_long")) == ["kill_switch"]
    set_setting("kill_switch", "off")
    monitors.run_monitors(now=t + timedelta(minutes=62))
    assert active("kill_switch_long") == {}


def test_event_backlog(demo):
    publish("vendor.thing", {})  # never drained
    t = db.now()
    monitors.run_monitors(now=t + timedelta(minutes=5))
    assert active("event_backlog") == {}
    monitors.run_monitors(now=t + timedelta(minutes=11))
    assert "oldest due event" in active("event_backlog")["event_bus"]["detail"]


def test_raising_monitor_becomes_monitor_error_and_others_still_run(demo, monkeypatch):
    def broken(now):
        raise RuntimeError("monitor exploded")

    monkeypatch.setitem(monitors.MONITORS, "ledger_imbalance", ("P1", broken))
    with session() as s:
        s.get(Order, demo[0]).status = "paid"
    r = monitors.run_monitors(now=later())
    assert r["monitor_errors"] == ["ledger_imbalance"]
    err = active("monitor_error")["ledger_imbalance"]
    assert err["severity"] == "P2" and "monitor exploded" in err["detail"]
    assert list(active("undelivered_order")) == [demo[0]]  # later monitors ran

    monkeypatch.undo()
    monitors.run_monitors(now=later())
    assert active("monitor_error") == {}


# ------------------------------------------------------------------------------------- alerting
def test_p1_alerts_push_and_email_and_repeats_until_acknowledged(demo, config, net, mail):
    config.update(NTFY_URL="https://ntfy.example/aecom", NTFY_TOKEN="tk_secret_123", ALERT_EMAIL="ops@example.com")
    with session() as s:
        s.get(Order, demo[0]).status = "paid"
    t0 = later()

    r = monitors.run_monitors(now=t0)
    assert len(r["alerted"]) == 1
    (method, url, kw), = net.calls
    assert (method, url) == ("POST", "https://ntfy.example/aecom")
    assert kw["headers"]["Priority"] == "high" and kw["headers"]["Authorization"] == "Bearer tk_secret_123"
    assert kw["headers"]["Title"] == f"[P1] undelivered_order: {demo[0]}"
    assert mail == [("ops@example.com", f"[P1] undelivered_order: {demo[0]}", mail[0][2])]

    monitors.run_monitors(now=t0 + timedelta(minutes=10))
    assert len(net.calls) == 1 and len(mail) == 1  # not yet 30 min
    monitors.run_monitors(now=t0 + timedelta(minutes=31))
    assert len(net.calls) == 2 and len(mail) == 2
    inc = active("undelivered_order")[demo[0]]
    assert inc["alert_count"] == 2

    incidents.acknowledge(inc["id"], "kaan")
    monitors.run_monitors(now=t0 + timedelta(minutes=70))
    assert len(net.calls) == 2 and len(mail) == 2


def test_p2_is_email_only_and_alerts_once(demo, config, net, mail):
    config.update(NTFY_URL="https://ntfy.example/aecom", ALERT_EMAIL="ops@example.com")
    t = later()
    with session() as s:
        s.add(Approval(id="apr_old", agent_id="ceo", tool_name="x", args={}, risk_tier="high", reason="r",
                       created_at=t - timedelta(hours=25)))
    monitors.run_monitors(now=t)
    monitors.run_monitors(now=t + timedelta(hours=2))
    assert net.calls == [] and [m[1] for m in mail] == ["[P2] approval_ageing: apr_old"]


def test_p3_is_not_routed(config, net, mail):
    config.update(NTFY_URL="https://ntfy.example/aecom", ALERT_EMAIL="ops@example.com")
    inc, _ = incidents.raise_incident("drift", "P3", "roas")
    assert alerts.route(inc) == {"push": False, "email": False} and net.calls == [] and mail == []


def test_alert_text_is_masked(config, net, mail):
    config.update(ALERT_EMAIL="ops@example.com")
    inc, _ = incidents.raise_incident("other_dead_letter", "P2", "1", "failed with Bearer abc.def.ghi and sk-ant-xyz")
    alerts.route(inc)
    assert "abc.def.ghi" not in mail[0][2] and "sk-ant-xyz" not in mail[0][2] and "***" in mail[0][2]


def test_everything_is_a_noop_without_config(demo, net, mail):
    with session() as s:
        s.get(Order, demo[0]).status = "paid"
    r = monitors.run_monitors(now=later())
    assert len(r["alerted"]) == 1 and r["heartbeat"] is False  # marked alerted, but nothing configured to send
    assert net.calls == [] and mail == []


def test_heartbeat_pinged_after_the_loop(config, net):
    config.update(HEALTHCHECKS_PING_URL="https://hc-ping.example/uuid-1")
    assert monitors.run_monitors()["heartbeat"] is True
    assert net.calls == [("GET", "https://hc-ping.example/uuid-1", {"timeout": alerts.TIMEOUT})]


def test_network_errors_never_raise_and_are_recorded(config, net, mail):
    config.update(NTFY_URL="https://ntfy.example/secret-topic", HEALTHCHECKS_PING_URL="https://hc-ping.example/uuid-1")
    net.fail = True
    inc, _ = incidents.raise_incident("ledger_imbalance", "P1", "ledger", "x")
    assert alerts.route(inc) == {"push": False, "email": False}
    assert alerts.heartbeat() is False
    with session() as s:
        rows = list(s.scalars(select(ErrorRow).where(ErrorRow.component == "alerts")))
    assert [r.context["channel"] for r in rows] == ["ntfy", "heartbeat"]
    assert all("secret-topic" not in r.message and "uuid-1" not in r.message for r in rows)


# ------------------------------------------------------------------------------------- /health
def test_health_200_when_healthy_and_503_with_open_p1():
    client = TestClient(api_app.app)
    r = client.get("/health", params={"detail": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["db"] is True and body["kill_switch"] is False
    assert body["agents_paused"] is False and body["open_incidents"] == {"P1": 0, "P2": 0, "P3": 0}
    assert set(body["event_bus"]) == {"waiting_for_retry", "queued", "dead_letter"}

    incidents.raise_incident("ledger_imbalance", "P2", "ledger")
    assert client.get("/health", params={"detail": True}).status_code == 200  # P2 is degraded, not down
    p1, _ = incidents.raise_incident("undelivered_order", "P1", "ord_1")
    r = client.get("/health", params={"detail": True})
    assert r.status_code == 503 and r.json()["ok"] is False and r.json()["open_incidents"]["P1"] == 1
    incidents.resolve(p1["id"], "kaan")
    assert client.get("/health", params={"detail": True}).status_code == 200


def test_health_503_when_db_is_down(monkeypatch):
    monkeypatch.setattr(api_app, "_db_ok", lambda: False)
    r = TestClient(api_app.app).get("/health", params={"detail": True})
    assert r.status_code == 503 and r.json()["db"] is False and r.json()["ok"] is False


def test_health_default_body_keeps_its_shape_but_still_turns_503():
    client = TestClient(api_app.app)
    assert client.get("/health").json() == {"ok": True, "kill_switch": False, "agents_paused": False}
    incidents.raise_incident("undelivered_order", "P1", "ord_1")
    r = client.get("/health")
    assert r.status_code == 503 and r.json() == {"ok": False, "kill_switch": False, "agents_paused": False}
