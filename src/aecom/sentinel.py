"""K4: the Sentinel - a read-only Ops agent that diagnoses incidents (the monitors are the safety net, not this).

    diagnose(incident_id)   # run the sentinel agent once for an incident; never raises

The monitors call :func:`diagnose` for new P1/P2 incidents after alerting (``monitors.diagnose_new``). Each incident
is diagnosed at most once: the company setting ``sentinel_diagnosed:<incident id>`` is written before the run, so a
failing or slow run is not repeated every 5 minutes. The agent finishes with ``submit_diagnosis``, which lands here
in :func:`append_diagnosis`: a formatted note on the incident (status untouched) plus an audit entry.
"""

from __future__ import annotations

import logging
from typing import Any

from aecom.audit import audit
from aecom.company_settings import agent_enabled, get_setting, set_setting
from aecom.config import get_settings
from aecom.context import current_run
from aecom.db import Incident, now, session
from aecom.observability import mask, record_error

log = logging.getLogger(__name__)

AGENT = "sentinel"
DIAGNOSED_PREFIX = "sentinel_diagnosed:"  # company setting per incident: ISO time of the (single) diagnosis run
SEVERITIES = ("P1", "P2")  # incidents the monitors ask the Sentinel about


def available() -> bool:
    """The Sentinel can run: enabled, and in scripted LLM mode only when a script for it is loaded."""
    if not agent_enabled(AGENT):
        return False
    if get_settings().llm_mode == "scripted":
        from aecom.llm import Script

        return bool(Script.queues.get(AGENT))
    return True


def already_diagnosed(incident_id: str) -> bool:
    return bool(get_setting(DIAGNOSED_PREFIX + incident_id))


def _task(inc: dict[str, Any]) -> str:
    subject = f" (subject {inc['subject']})" if inc.get("subject") else ""
    return (f"Diagnose incident {inc['id']}: {inc['severity']} {inc['monitor']}{subject}, status {inc['status']}, "
            f"first seen {inc['first_seen']:%Y-%m-%d %H:%M} UTC.\nDetail: {mask(inc.get('detail') or '')}\n\n"
            f"Read the incident, gather evidence with your diagnostic tools, follow the runbook skill "
            f"runbook_{inc['monitor']}, and finish with submit_diagnosis(incident_id=\"{inc['id']}\", ...). "
            "Change nothing yourself.")


def diagnose(incident_id: str) -> Any | None:
    """Run the Sentinel once for the incident. Returns the AgentResult, or None if it did not run. Never raises."""
    try:
        from aecom import incidents

        inc = incidents.get_incident(incident_id)
        if inc is None or already_diagnosed(incident_id) or not agent_enabled(AGENT):
            return None
        set_setting(DIAGNOSED_PREFIX + incident_id, now().isoformat())  # before the run: at most once, even if it fails
        from aecom.graph.agents import run_agent
        from aecom.handlers import run_sync

        result = run_sync(run_agent(AGENT, _task(inc)))
        audit(AGENT, "incident", "diagnosis_run", {"incident_id": incident_id, "status": result.status,
                                                   "diagnosed": result.structured is not None})
        return result
    except Exception as exc:  # the Sentinel must never take the monitors down
        log.exception("sentinel diagnosis of %s failed", incident_id)
        record_error("sentinel", exc, context={"incident_id": incident_id})
        return None


def format_diagnosis(summary: str, root_cause: str, evidence: list[str], next_actions: list[str]) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {x}" for x in items) if items else "- (none)"

    return (f"DIAGNOSIS\nSummary: {summary}\nRoot cause: {root_cause}\nEvidence:\n{bullets(list(evidence or []))}\n"
            f"Next actions:\n{bullets(list(next_actions or []))}")


def append_diagnosis(incident_id: str, summary: str, root_cause: str, evidence: list[str],
                     next_actions: list[str], by: str = AGENT) -> dict[str, Any]:
    """Append a formatted, masked diagnosis to the incident's notes (status unchanged) and audit it."""
    text = mask(format_diagnosis(summary, root_cause, evidence, next_actions))[:8000]
    with session() as s:
        i = s.get(Incident, incident_id)
        if i is None:
            raise ValueError(f"unknown incident {incident_id}")
        note = f"{by}: {text}"
        i.notes = f"{i.notes}\n{note}" if i.notes else note
    audit(by, "incident", "diagnosed", {"incident_id": incident_id, "summary": mask(summary)[:500]},
          current_run.get())
    return {"incident_id": incident_id, "recorded": True, "summary": summary, "root_cause": root_cause,
            "evidence": list(evidence or []), "next_actions": list(next_actions or [])}
