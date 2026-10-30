"""UI-requested jobs for the Operations tab's CLI-only actions: a company run, store catalog sync, reconciliation,
monitors, and the AgentMail poll (`aecom run`, `aecom store sync`, `aecom reconcile`, `aecom monitors run`,
`aecom agentmail poll` - see cli.py). Each handler below is the exact same code path as its CLI command, so the
same policy gate and per-agent budgets apply; the dashboard only ever queues an ``ops_jobs`` row via ``request()``
and the scheduler's ``jobs.run_pending()`` tick (every 30 s) runs the handler, out of the Streamlit process,
mirroring how ``aecom.backup`` registers its own job kinds.
"""

from __future__ import annotations

from typing import Any

from aecom import jobs

MAX_DIRECTIVE_LEN = 2000


def run_company_job(params: dict[str, Any]) -> dict[str, Any]:
    """``jobs`` handler for kind ``run``: one company run from a directive, same as ``aecom run "<directive>"``
    (``aecom.graph.supervisor.run_company``). Uses ``run_sync`` (not a bare ``asyncio.run``) so this still works if
    the scheduler process is ever driven from a thread that already has a running event loop."""
    from aecom.graph.supervisor import run_company
    from aecom.handlers import run_sync

    directive = str(params.get("directive", "")).strip()
    if not directive:
        raise ValueError("directive is required")
    st = run_sync(run_company(directive))
    return {"run_id": st["run_id"], "plan": st.get("plan"), "verdict": st.get("verdict"), "summary": st.get("summary"),
            "pending_approvals": sorted(set(st.get("escalations", [])))}


def run_sync_store_job(_params: dict[str, Any]) -> dict[str, Any]:
    """``jobs`` handler for kind ``sync_store``: same as ``aecom store sync``."""
    from aecom.store.adapter import get_store

    store = get_store()
    if not hasattr(store, "sync_catalog"):
        raise RuntimeError(f"store adapter {store.name!r} has no catalog to sync (only STORE_ADAPTER=lemonsqueezy does)")
    return store.sync_catalog()


def run_reconcile_job(params: dict[str, Any]) -> dict[str, Any]:
    """``jobs`` handler for kind ``reconcile``: same as ``aecom reconcile [--older-than-minutes N]``, then drains the
    re-published events (mirroring the CLI's own ``_print_drain`` so stuck orders move the same tick)."""
    from datetime import timedelta

    from aecom.bus.events import drain
    from aecom.reconciler import reconcile

    minutes = int(params.get("older_than_minutes") or 15)
    result = dict(reconcile(timedelta(minutes=minutes)))
    result["events_drained"] = drain()
    return result


def run_monitors_job(_params: dict[str, Any]) -> dict[str, Any]:
    """``jobs`` handler for kind ``monitors``: same as ``aecom monitors run``."""
    from aecom.monitors import run_monitors

    return run_monitors()


def run_agentmail_poll_job(_params: dict[str, Any]) -> dict[str, Any]:
    """``jobs`` handler for kind ``agentmail_poll``: same as ``aecom agentmail poll``. ``poll_inbox`` itself is a
    no-op returning 0 when ``AGENTMAIL_API_KEY`` is not set (no network call), same as the scheduler's own
    ``poll_email`` tick."""
    from aecom.bus.events import drain
    from aecom.integrations.agentmail import poll_inbox

    n = poll_inbox()
    return {"new_messages": n, "events_drained": drain()}


def request(kind: str, by: str, **params: Any) -> str:
    """Queue one of this module's job kinds for the dashboard. The one piece of dashboard-specific validation (an
    empty or very long directive) lives here, before the job ever reaches ``run_pending``; the row itself is created
    with the existing ``jobs.enqueue``, which also audits the request (actor ``human:<by>``)."""
    if kind == "run":
        directive = str(params.get("directive", "")).strip()
        if not directive or len(directive) > MAX_DIRECTIVE_LEN:
            raise ValueError(f"directive must be 1-{MAX_DIRECTIVE_LEN} characters")
        params = {"directive": directive}
    return jobs.enqueue(kind, by, params)


def _register() -> None:
    """Idempotent: importing this module twice (tests, scheduler reload) must not raise or double-register."""
    jobs.register("run", run_company_job)
    jobs.register("sync_store", run_sync_store_job)
    jobs.register("reconcile", run_reconcile_job)
    jobs.register("monitors", run_monitors_job)
    jobs.register("agentmail_poll", run_agentmail_poll_job)


_register()
