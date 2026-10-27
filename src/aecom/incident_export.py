"""K8/K10: incident -> GitHub issue -> PR description, and postmortem drafts.

- :func:`issue_body` - a masked Markdown issue for an incident: what, timeline, the Sentinel's diagnosis, related
  errors (shortened tracebacks), the run timeline of a referenced run id and a *Reproduce* section with a failing
  test skeleton that uses the chaos injector matching the monitor.
- :func:`export_issue` - **dry run by default**: writes ``reports/issues/<incident id>.md`` and returns the path.
  Only with ``dry_run=False`` *and* ``GITHUB_REPO`` + ``GITHUB_TOKEN`` set does it create the issue via the GitHub
  REST API.
- :func:`pr_body` - the PR description template for the Engineer agent or a human. Nothing here deploys or merges.
- :func:`draft_postmortem` - a ``postmortem`` report (immutable, number-checked) for a resolved P1 incident.

Everything leaving this module goes through :func:`aecom.observability.mask` and :func:`redact` (licence keys,
download tokens, links and e-mail addresses), because an issue may be public.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, or_, select

from aecom import incidents, reports, run_detail
from aecom.audit import audit
from aecom.config import ROOT
from aecom.db import ErrorRow, Incident, LicenseKey, Order, Report, session
from aecom.observability import mask, record_error

ISSUES_DIR = ROOT / "reports" / "issues"
GITHUB_API = "https://api.github.com"
TIMEOUT = 15.0
TRACE_LINES = 12
MAX_ERRORS = 10
MAX_TIMELINE = 50
ERROR_WINDOW = timedelta(hours=1)  # errors this long before first seen (until resolved/last seen) count as related

_INCIDENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_RUN_REF = re.compile(r"\b(?:run|sel)_[0-9a-f]{12}\b")
_ORDER_REF = re.compile(r"\bord_[0-9a-f]{12}\b")
_URL = re.compile(r"(?i)\b(?:https?|ftp)://\S+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
DIAGNOSIS_MARK = "DIAGNOSIS"
_NOTE_START = re.compile(r"[\w.:-]+: ")  # "system: ...", "kaan: ...", "human:kaan: ..."
DEAD_LETTER_MONITORS = ("order_dead_letter", "other_dead_letter")

# monitor -> (chaos injector call, invariant the fix must keep). The injector reproduces the fault class in-process.
REPRO: dict[str, tuple[str | None, str | None]] = {
    "undelivered_order": ("injectors.delivery_failures(1.0, seed=0)", "every_paid_order_delivered"),
    "unemailed_order": ("injectors.email_failures(1.0, seed=0)", "every_delivered_order_emailed"),
    "order_dead_letter": ("injectors.delivery_failures(1.0, seed=0)", "no_dead_letters"),
    "other_dead_letter": (None, "no_dead_letters"),
    "duplicate_key": ("injectors.duplicate_order_paid()", "no_key_on_two_orders"),
    "ledger_imbalance": ("injectors.duplicate_order_paid()", "ledger_balanced"),
    "stuck_run": ("injectors.claude_down()", "no_run_stuck_running"),
    "errors": ("injectors.claude_down()", None),
}


# --- gathering ---------------------------------------------------------------------------------------------

def _load(incident_id: str) -> dict[str, Any]:
    if not _INCIDENT_ID.fullmatch(incident_id or ""):
        raise ValueError(f"invalid incident id {incident_id!r}")
    inc = incidents.get_incident(incident_id)
    if inc is None:
        raise ValueError(f"unknown incident {incident_id}")
    return inc


def _secret_values() -> list[str]:
    """Licence keys and download tokens: sellable secrets that must never reach an issue or a report."""
    with session() as s:
        keys = s.scalars(select(LicenseKey.key)).all()
        tokens = s.scalars(select(Order.download_token).where(Order.download_token.is_not(None))).all()
    return sorted({v for v in [*keys, *tokens] if v and len(v) >= 6}, key=len, reverse=True)


def redact(text: str, secret_values: list[str] | None = None) -> str:
    """mask() plus licence keys, download tokens, links and e-mail addresses."""
    text = mask(text or "")
    for value in secret_values if secret_values is not None else _secret_values():
        text = text.replace(value, "[key redacted]")
    text = _URL.sub("[link redacted]", text)
    return _EMAIL.sub("[email redacted]", text)


def _short_trace(trace: str, lines: int = TRACE_LINES) -> str:
    parts = (trace or "").strip().splitlines()
    return "\n".join(parts if len(parts) <= lines else ["…", *parts[-lines:]])


def diagnosis(inc: dict[str, Any]) -> str:
    """The Sentinel's latest diagnosis from the incident notes ("sentinel: DIAGNOSIS ..."), or ""."""
    notes = inc.get("notes") or ""
    at = notes.rfind(f": {DIAGNOSIS_MARK}")
    if at < 0:
        return ""
    body = notes[at + 2:]
    # the diagnosis runs until the next "<actor>: ..." note (its own lines are "Summary: ...", "- ...", ...)
    lines = body.splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        if _NOTE_START.match(line) and not line.startswith("Summary: "):
            break
        out.append(line)
    return "\n".join(out).strip()


def _root_cause(diag: str) -> str:
    m = re.search(r"^Root cause: (.*)$", diag, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _summary(diag: str) -> str:
    m = re.search(r"^Summary: (.*)$", diag, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _text_of(inc: dict[str, Any]) -> str:
    return " ".join([inc.get("subject") or "", inc.get("detail") or "", inc.get("notes") or ""])


def run_ids(inc: dict[str, Any]) -> list[str]:
    """Run ids (``run_...`` / ``sel_...``) referenced by the incident's subject, detail or notes, in order."""
    return list(dict.fromkeys(_RUN_REF.findall(_text_of(inc))))


def order_ids(inc: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(_ORDER_REF.findall(_text_of(inc))))


def related_errors(inc: dict[str, Any]) -> list[dict[str, Any]]:
    """ops_errors of a referenced run or dead-lettered event, plus errors around the incident; newest first."""
    runs = run_ids(inc)
    event_id = int(inc["subject"]) if inc["monitor"] in DEAD_LETTER_MONITORS and str(inc["subject"]).isdigit() else None
    start = inc["first_seen"] - ERROR_WINDOW
    end = inc.get("resolved_at") or inc.get("last_seen") or inc["first_seen"]
    conds = [ErrorRow.ts.between(start, end)]
    if runs:
        conds.append(ErrorRow.run_id.in_(runs))
    if event_id is not None:
        conds.append(ErrorRow.event_id == event_id)
    with session() as s:
        rows = s.scalars(select(ErrorRow).where(or_(*conds)).order_by(ErrorRow.id.desc()).limit(MAX_ERRORS)).all()
        return [{"id": e.id, "ts": e.ts, "component": e.component, "run_id": e.run_id, "event_id": e.event_id,
                 "exc_type": e.exc_type, "message": (e.message or "")[:500],
                 "traceback": _short_trace((e.traceback or "").replace(f"{ROOT}/", ""))} for e in rows]


# --- issue -------------------------------------------------------------------------------------------------

def _ts(value: Any) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if isinstance(value, datetime) else "-"


def issue_title(incident_id: str) -> str:
    inc = _load(incident_id)
    subject = f": {inc['subject']}" if inc.get("subject") else ""
    return redact(f"[{inc['severity']}] {inc['monitor']}{subject} ({inc['id']})")[:250]


def _test_name(inc: dict[str, Any]) -> str:
    return re.sub(r"\W+", "_", f"test_{inc['monitor']}_{inc['id']}").lower()


def repro_test(inc: dict[str, Any]) -> str:
    """A pytest function (fresh_company fixture) that should fail while the bug exists and pass once fixed."""
    injector, invariant = REPRO.get(inc["monitor"], (None, None))
    names = ["_chaos_run", "_place_orders", *(["injectors"] if injector else []), "settle"]
    imports = [f"from aecom.chaos import {', '.join(names)}"]
    if invariant:
        imports.append(f"from aecom.chaos.invariants import {invariant}")
    body = ["import random", "", *imports, "", "",
            f"def {_test_name(inc)}(fresh_company):",
            f'    """Regression for incident {inc["id"]} ({inc["severity"]} {inc["monitor"]})."""',
            "    with _chaos_run():"]
    if injector:
        body += [f"        with {injector}:",
                 "            _place_orders(random.Random(0), 3)  # or build the incident's state by hand",
                 "            settle()",
                 "        settle()  # fault gone: retries / reconciler must repair what the fault broke"]
    else:
        body += ["        _place_orders(random.Random(0), 3)  # TODO: reproduce the state from the timeline above",
                 "        settle()"]
    if invariant:
        body.append(f"    assert {invariant}() == []")
    else:
        body.append("    assert False, 'TODO: assert the behaviour the incident showed was wrong'")
    return "\n".join(body)


def issue_body(incident_id: str) -> str:
    """Markdown issue for an incident; masked and redacted."""
    inc = _load(incident_id)
    secrets = _secret_values()
    diag = diagnosis(inc)
    errs = related_errors(inc)
    lines = [f"# {inc['severity']} {inc['monitor']}: {inc['subject'] or '(no subject)'}", "",
             f"- **Incident:** `{inc['id']}`", f"- **Severity:** {inc['severity']}",
             f"- **Monitor:** `{inc['monitor']}`", f"- **Subject:** `{inc['subject'] or '-'}`",
             f"- **Status:** {inc['status']}", "", "## Detail", "", inc.get("detail") or "(none)", "",
             "## Timeline", "",
             f"- First seen: {_ts(inc['first_seen'])}", f"- Last seen: {_ts(inc['last_seen'])}",
             f"- Alerts: {inc['alert_count']} (last {_ts(inc['last_alerted_at'])})",
             f"- Acknowledged: {_ts(inc['acknowledged_at'])}"
             + (f" by {inc['acknowledged_by']}" if inc.get("acknowledged_by") else ""),
             f"- Resolved: {_ts(inc['resolved_at'])}", "",
             "## Diagnosis (Sentinel)", "", diag or "No diagnosis recorded yet.", "",
             "## Related errors", ""]
    if not errs:
        lines.append("None recorded in ops_errors.")
    for e in errs:
        where = ", ".join(x for x in [f"run `{e['run_id']}`" if e["run_id"] else "",
                                      f"event {e['event_id']}" if e["event_id"] is not None else ""] if x)
        lines += [f"### error {e['id']} · {e['component']} · {_ts(e['ts'])}" + (f" · {where}" if where else ""),
                  "", f"`{e['exc_type']}: {e['message']}`", "", "```", e["traceback"] or "(no traceback)", "```", ""]
    for rid in run_ids(inc):
        entries = run_detail.timeline(rid)
        lines += ["", f"## Run timeline `{rid}`", ""]
        if not entries:
            lines.append("No records for this run id.")
        lines += [f"- {_ts(x['ts'])} · {x['kind']} · {x['actor']} · {x['summary']}" for x in entries[:MAX_TIMELINE]]
        if len(entries) > MAX_TIMELINE:
            lines.append(f"- … {len(entries) - MAX_TIMELINE} more entries (`aecom` dashboard → Run detail)")
    lines += ["", "## Reproduce", "",
              "Suggested failing test (put it in `tests/`, make it fail first, then fix the code):", "",
              "```python", repro_test(inc), "```", "",
              "Run: `uv run pytest -q tests/ -k " + _test_name(inc) + "` and `uv run aecom chaos all`.", "",
              "---", "Generated by `aecom incidents export`. Secrets, licence keys, links and e-mail addresses are "
              "redacted. The Engineer opens a PR; it never deploys, a human merges."]
    return redact("\n".join(lines), secrets) + "\n"


def _github_create(repo: str, token: str, title: str, body: str, labels: list[str]) -> dict[str, Any]:
    r = httpx.post(f"{GITHUB_API}/repos/{repo}/issues", json={"title": title, "body": body, "labels": labels},
                   headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def export_issue(incident_id: str, dry_run: bool = True) -> Path:
    """Write the issue to ``reports/issues/<id>.md`` and return the path. With ``dry_run=False`` (and GITHUB_REPO +
    GITHUB_TOKEN set) also create the GitHub issue; its URL is added to the incident notes and audited."""
    from aecom.secrets import secrets as env

    inc = _load(incident_id)
    body, title = issue_body(incident_id), issue_title(incident_id)
    ISSUES_DIR.mkdir(parents=True, exist_ok=True)
    path = ISSUES_DIR / f"{inc['id']}.md"
    path.write_text(f"<!-- title: {title} -->\n{body}", encoding="utf-8")
    if dry_run:
        audit("system", "incident", "issue_exported", {"incident_id": inc["id"], "dry_run": True})
        return path
    repo, token = env.get("GITHUB_REPO") or "", env.get("GITHUB_TOKEN") or ""
    if not (repo and token):
        raise ValueError(f"GITHUB_REPO and GITHUB_TOKEN must be set to create the issue; dry run written to {path}")
    if not _REPO.fullmatch(repo):
        raise ValueError("GITHUB_REPO must look like owner/name")
    try:
        issue = _github_create(repo, token, title, body, ["incident", inc["severity"]])
    except httpx.HTTPError as exc:
        record_error("incident_export", exc, context={"incident_id": inc["id"], "repo": repo})
        raise ValueError(f"GitHub issue creation failed ({type(exc).__name__}): {mask(str(exc))[:300]}; "
                         f"dry run written to {path}") from exc
    url = str(issue.get("html_url") or "")
    with session() as s:
        i = s.get(Incident, inc["id"])
        note = f"github: issue {url}"
        i.notes = f"{i.notes}\n{note}" if i.notes else note
    audit("system", "incident", "issue_exported", {"incident_id": inc["id"], "dry_run": False, "url": url})
    return path


def pr_body(incident_id: str) -> str:
    """PR description template for the Engineer agent / a human."""
    inc = _load(incident_id)
    diag = diagnosis(inc)
    injector, invariant = REPRO.get(inc["monitor"], (None, None))
    lines = [f"## Fixes incident `{inc['id']}` ({inc['severity']} {inc['monitor']})", "",
             "### Problem", "", inc.get("detail") or "(see the incident issue)", "",
             "### Root cause", "", _root_cause(diag) or "(no diagnosis yet: state the root cause you found)", "",
             "### Failing test", "", f"`tests/...::{_test_name(inc)}` - fails on `main`, passes with this PR.",
             f"Injector: `{injector}`." if injector else "No chaos injector for this monitor: build the state by hand.",
             f"Invariant: `{invariant}()`." if invariant else "", "",
             "### Fix checklist", "",
             "- [ ] failing test added first and seen failing",
             "- [ ] smallest fix at the responsible layer",
             "- [ ] `uv run pytest -q` passes",
             "- [ ] `uv run aecom chaos all` passes",
             "- [ ] no secrets, keys or customer data in code, tests or logs",
             "- [ ] docs/wiki updated if behaviour changed",
             "- [ ] postmortem regression test linked (P1)", "",
             "### Deployment", "",
             "This PR never deploys; a human reviews and merges, then `scripts/update.sh` deploys."]
    return redact("\n".join(x for x in lines if x is not None)) + "\n"


# --- postmortem (K10) --------------------------------------------------------------------------------------

def _impact(inc: dict[str, Any]) -> dict[str, Any]:
    ids = order_ids(inc)
    if not ids and inc["monitor"] in ("undelivered_order", "unemailed_order") and inc["subject"]:
        ids = [inc["subject"]]
    with session() as s:
        found = s.execute(select(func.count(Order.id), func.sum(Order.amount_eur)).where(Order.id.in_(ids))).one()
    return {"orders_affected": int(found[0] or 0), "amount_eur": round(float(found[1] or 0.0), 2)}


def _figures(text: str) -> list[float]:
    """Every number written in quoted incident text, so the number check accepts figures taken from the record."""
    cleaned = reports._LIST_MARKER.sub(r"\1", reports._DATE.sub(" ", text))
    out: list[float] = []
    for m in reports._NUMBER.finditer(cleaned):
        scale = reports._SCALES[m["scale"].strip().lower()] if m["scale"] else 1.0
        out += [v * scale for v, _ in reports._candidates(m["num"])]
    return sorted(set(out))


def postmortem_text(inc: dict[str, Any], data: dict[str, Any]) -> str:
    diag = diagnosis(inc)
    imp, tl = data["impact"], data["timeline"]
    injector, invariant = REPRO.get(inc["monitor"], (None, None))
    lines = [f"Postmortem draft for incident {inc['id']} ({inc['severity']} {inc['monitor']}).", "",
             "## What happened", "",
             f"Monitor {inc['monitor']} raised the incident for {inc['subject'] or 'the company'}: "
             f"{inc.get('detail') or 'no detail'}.",
             f"First seen {_ts(inc['first_seen'])}, resolved {_ts(inc['resolved_at'])} "
             f"after {tl['duration_minutes']} minutes; alerts sent: {tl['alerts']}."
             + (f" Acknowledged by {inc['acknowledged_by']}." if inc.get("acknowledged_by") else " Never acknowledged."),
             "", "## Impact", "",
             f"Orders affected: {imp['orders_affected']}. Order value affected: €{imp['amount_eur']:.2f}.", "",
             "## Why", "", _root_cause(diag) or _summary(diag) or "No Sentinel diagnosis recorded: a human fills this in.",
             "", "## What changes", "",
             "- To be completed by a human: the code, policy or runbook change that prevents a recurrence.",
             f"- Issue and PR: `aecom incidents export {inc['id']}`.", "",
             "## Regression test", "",
             f"Test {_test_name(inc)}"
             + (f" with the chaos injector {injector.split('(')[0]}" if injector else "")
             + (f", asserting {invariant}." if invariant else ".")]
    return "\n".join(lines)


def draft_postmortem(incident_id: str, created_by: str = "system") -> dict[str, Any]:
    """Store a ``postmortem`` report (period ``all``) for a resolved P1 incident; raises ValueError otherwise.

    Each call stores a new version for that incident. Numbers in the text come from the report data."""
    inc = _load(incident_id)
    if inc["severity"] != "P1":
        raise ValueError(f"postmortems are drafted for P1 incidents only; {inc['id']} is {inc['severity']}")
    if inc["status"] != "resolved" or inc["resolved_at"] is None:
        raise ValueError(f"incident {inc['id']} is {inc['status']}; resolve it before drafting the postmortem")
    secrets = _secret_values()
    inc = {**inc, **{k: redact(inc.get(k) or "", secrets) for k in ("subject", "detail", "notes")}}
    duration = max((inc["resolved_at"] - inc["first_seen"]).total_seconds(), 0.0)
    data: dict[str, Any] = {
        "kind": "postmortem", "period": {"name": "all"},
        "incident": {"id": inc["id"], "monitor": inc["monitor"], "severity": inc["severity"],
                     "subject": inc["subject"], "first_seen": _ts(inc["first_seen"]),
                     "resolved_at": _ts(inc["resolved_at"]), "acknowledged_by": inc.get("acknowledged_by") or ""},
        "timeline": {"duration_minutes": round(duration / 60), "alerts": inc["alert_count"]},
        "impact": _impact(inc),
    }
    data["quoted_figures"] = _figures(" ".join([inc["subject"], inc["detail"], diagnosis(inc)]))
    text = redact(postmortem_text(inc, data), secrets)
    if unknown := reports.number_check(text, data):
        raise ValueError(f"postmortem mentions numbers not in its data: {', '.join(unknown)}")
    with session() as s:
        previous = [r.version for r in s.scalars(select(Report).where(Report.kind == "postmortem"))
                    if (r.data or {}).get("incident", {}).get("id") == inc["id"]]
        r = Report(kind="postmortem", period_start=None, period_end=None, version=max(previous, default=0) + 1,
                   data=data, text=text, sha256=reports._sha(data, text), created_by=created_by)
        s.add(r)
        s.flush()
        out = reports._to_dict(r)
    audit(created_by, "incident", "postmortem_drafted", {"incident_id": inc["id"], "report_id": out["id"]})
    return out
