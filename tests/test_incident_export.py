"""K8/K10: incident -> issue markdown (dry run by default) -> PR template, and postmortem drafts."""

from __future__ import annotations

import ast
from datetime import timedelta

import httpx
import pytest
from aecom import incident_export, incidents, reports, sentinel
from aecom.bootstrap import seed_demo
from aecom.db import AgentRun, LicenseKey, Order, Product, now, session
from aecom.observability import record_error
from aecom.store.adapter import get_store

SECRET = "zz-very-secret-api-value-42"
KEY = "FOCUS-SECRET-KEY-9Q7X"
RUN = "run_0123456789ab"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("SHOP_API_KEY", SECRET)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(incident_export, "ISSUES_DIR", tmp_path / "reports" / "issues")


def _no_http(*a, **k):
    raise AssertionError("no network in a dry run")


def _delivery_incident(severity: str = "P1") -> tuple[dict, str]:
    seed_demo()
    oid = get_store().create_order("buyer@example.com", "LIC-FOCUSFLOW-PRO-V1", "DE")["id"]
    t = now()
    with session() as s:
        s.add(AgentRun(run_id=RUN, agent_id="ops", model="scripted", task="deliver the order", status="error",
                       started_at=t - timedelta(minutes=5), ended_at=t - timedelta(minutes=4)))
        pid = s.query(Product.id).filter(Product.sku == "LIC-FOCUSFLOW-PRO-V1").scalar()
        s.add(LicenseKey(product_id=pid, key=KEY, status="assigned", order_id=oid))
    try:
        raise RuntimeError(f"store said no; token={SECRET}")
    except RuntimeError as exc:
        record_error("agent", exc, run_id=RUN)
    detail = (f"order {oid} is 'paid' for over 15 min; run {RUN}; key {KEY}; buyer@example.com; "
              f"see https://shop.example.com/admin/{oid}?t={SECRET}")
    inc, _ = incidents.raise_incident("undelivered_order", severity, oid, detail)
    sentinel.append_diagnosis(inc["id"], "Delivery raised in the store adapter",
                              "MockStore.deliver_order raised RuntimeError for this SKU", [f"error in {RUN}"],
                              ["retry the delivery"])
    return incidents.get_incident(inc["id"]), oid


def test_issue_body_masked_with_diagnosis_errors_timeline_and_skeleton():
    inc, oid = _delivery_incident()
    body = incident_export.issue_body(inc["id"])
    for part in ("## Timeline", "## Diagnosis (Sentinel)", "## Related errors", f"## Run timeline `{RUN}`",
                 "## Reproduce", "Severity:** P1", "`undelivered_order`", oid,
                 "Root cause: MockStore.deliver_order raised RuntimeError", "RuntimeError: store said no",
                 "def test_undelivered_order_", "(fresh_company)", "injectors.delivery_failures(1.0, seed=0)",
                 "assert every_paid_order_delivered() == []", "never deploys"):
        assert part in body, part
    for leak in (SECRET, KEY, "buyer@example.com", "https://shop.example.com"):
        assert leak not in body, leak
    assert "***" in body and "[key redacted]" in body and "[link redacted]" in body
    skeleton = body.split("```python\n", 1)[1].split("```", 1)[0]
    ast.parse(skeleton)  # a valid test file to start from


def test_repro_uses_the_monitor_injector():
    inc, _ = incidents.raise_incident("unemailed_order", "P1", "ord_x", "not emailed")
    sk = incident_export.repro_test(inc)
    assert "injectors.email_failures" in sk and "every_delivered_order_emailed" in sk
    inc, _ = incidents.raise_incident("restock_failed", "P1", "SKU-1", "restock failed")
    sk = incident_export.repro_test(inc)
    assert "injectors." not in sk and "TODO" in sk
    ast.parse(sk)


def test_dry_run_writes_the_file_and_never_calls_http(monkeypatch):
    monkeypatch.setattr(httpx, "post", _no_http)
    monkeypatch.setenv("GITHUB_REPO", "acme/shop")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_dry_run_token_value")
    inc, _ = _delivery_incident()
    path = incident_export.export_issue(inc["id"])
    assert path == incident_export.ISSUES_DIR / f"{inc['id']}.md" and path.exists()
    text = path.read_text()
    assert incident_export.issue_body(inc["id"]) in text
    assert SECRET not in text and "ghp_dry_run_token_value" not in text
    assert "github:" not in incidents.get_incident(inc["id"])["notes"]


def test_live_calls_github_with_the_issue_payload(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(201, json={"html_url": "https://github.com/acme/shop/issues/7"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("GITHUB_REPO", "acme/shop")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_live_token_value")
    inc, _ = _delivery_incident()
    path = incident_export.export_issue(inc["id"], dry_run=False)
    assert path.exists() and len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://api.github.com/repos/acme/shop/issues"
    assert call["headers"]["Authorization"] == "Bearer ghp_live_token_value"
    assert call["json"]["title"].startswith("[P1] undelivered_order")
    assert call["json"]["labels"] == ["incident", "P1"]
    assert call["json"]["body"] == incident_export.issue_body(inc["id"])
    assert "ghp_live_token_value" not in call["json"]["body"] and SECRET not in call["json"]["body"]
    assert "github: issue https://github.com/acme/shop/issues/7" in incidents.get_incident(inc["id"])["notes"]


def test_live_without_config_refuses_and_never_calls_http(monkeypatch):
    monkeypatch.setattr(httpx, "post", _no_http)
    inc, _ = _delivery_incident()
    with pytest.raises(ValueError, match="GITHUB_REPO and GITHUB_TOKEN"):
        incident_export.export_issue(inc["id"], dry_run=False)


def test_pr_body_has_root_cause_test_and_human_merge():
    inc, _ = _delivery_incident()
    pr = incident_export.pr_body(inc["id"])
    for part in ("### Problem", "### Root cause", "MockStore.deliver_order raised RuntimeError", "### Failing test",
                 "### Fix checklist", "- [ ] failing test added first", "never deploys; a human"):
        assert part in pr, part
    assert SECRET not in pr and KEY not in pr


def test_postmortem_for_resolved_p1():
    inc, oid = _delivery_incident()
    incidents.resolve(inc["id"], "kaan", "fixed the adapter")
    rep = incident_export.draft_postmortem(inc["id"])
    assert rep["kind"] == "postmortem" and rep["period_start"] is None and rep["version"] == 1
    for part in ("## What happened", "## Impact", "## Why", "## What changes", "## Regression test",
                 "Orders affected: 1.", "MockStore.deliver_order raised RuntimeError"):
        assert part in rep["text"], part
    with session() as s:
        amount = s.get(Order, oid).amount_eur
    assert f"€{amount:.2f}" in rep["text"] and rep["data"]["impact"] == {"orders_affected": 1, "amount_eur": amount}
    assert reports.number_check(rep["text"], rep["data"]) == []
    dump = rep["text"] + str(rep["data"])
    assert SECRET not in dump and KEY not in dump and "buyer@example.com" not in dump
    assert incident_export.draft_postmortem(inc["id"])["version"] == 2


def test_postmortem_refused_for_open_or_p3():
    open_p1, _ = _delivery_incident()
    with pytest.raises(ValueError, match="resolve it"):
        incident_export.draft_postmortem(open_p1["id"])
    p3, _ = incidents.raise_incident("errors", "P3", "ops_errors", "1 error")
    incidents.resolve(p3["id"], "kaan")
    with pytest.raises(ValueError, match="P1 incidents only"):
        incident_export.draft_postmortem(p3["id"])
    assert reports.list_reports("postmortem") == []


def test_cli_export_and_postmortem(monkeypatch, capsys):
    from aecom.cli import main

    monkeypatch.setattr(httpx, "post", _no_http)
    inc, _ = _delivery_incident()
    main(["incidents", "export", inc["id"]])
    assert capsys.readouterr().out.strip() == str(incident_export.ISSUES_DIR / f"{inc['id']}.md")
    with pytest.raises(SystemExit, match="resolve it"):
        main(["incidents", "postmortem", inc["id"]])
    incidents.resolve(inc["id"], "kaan")
    main(["incidents", "postmortem", inc["id"], "--by", "kaan"])
    out = capsys.readouterr().out
    assert '"kind": "postmortem"' in out and SECRET not in out
