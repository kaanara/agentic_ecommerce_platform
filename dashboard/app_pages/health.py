# Govern > Health: open incidents, monitor activity, dead letters, a one-click doctor report, backup status and the
# deployed revision.
from collections import defaultdict
from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from _common import empty_state, kpi_row, mask_df, page_header, require_login, reviewer_name, sql
from aecom import backup, doctor, incidents, jobs
from aecom.bus.events import dead_letters, retry
from aecom.bus.events import status as bus_status

require_login()
page_header("Health", "monitor_heart",
           "Open incidents, what the monitors have seen, the event bus, a doctor report, backups and the deployed "
           "revision.")

SEVERITY_COLOR = {"P1": "red", "P2": "orange", "P3": "blue"}
tab_incidents, tab_monitors, tab_doctor, tab_backup = st.tabs(
    ["Incidents", "Monitors & dead letters", "Doctor", "Backup & deploy"])


# --------------------------------------------------------------------------------------------------- incidents
def _ack(incident_id: str, by: str, note: str) -> None:
    try:
        incidents.acknowledge(incident_id, by, note)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.rerun()


def _resolve(incident_id: str, by: str, note: str) -> None:
    try:
        incidents.resolve(incident_id, by, note)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.rerun()


def _incident_card(i: dict, reviewer: str) -> None:
    with st.container(border=True):
        top = st.columns([3, 1, 2])
        top[0].markdown(f"### {i['monitor']} · {i['subject'] or '—'}")
        top[1].badge(i["severity"], color=SEVERITY_COLOR.get(i["severity"], "gray"))
        top[2].caption(f"first seen {i['first_seen']:%Y-%m-%d %H:%M} · last seen {i['last_seen']:%Y-%m-%d %H:%M} · "
                      f"{i['alert_count']} alert(s)")
        st.markdown(i["detail"] or "_no detail recorded_")
        if i["notes"]:
            with st.expander("Notes"):
                st.text(i["notes"])
        note = st.text_input("Note", key=f"note_{i['id']}")
        row = st.container(horizontal=True)
        if i["status"] == "open" and row.button("Acknowledge", key=f"ack_{i['id']}", disabled=not reviewer):
            _ack(i["id"], reviewer, note)
        if row.button("Resolve", key=f"res_{i['id']}", type="primary", disabled=not reviewer):
            _resolve(i["id"], reviewer, note)


with tab_incidents:
    reviewer = reviewer_name()
    if not reviewer:
        st.info("Set your name in the sidebar to acknowledge or resolve an incident.")
    active = [i for i in incidents.list_incidents() if i["status"] in incidents.ACTIVE]
    counts = incidents.open_counts()
    kpi_row([("P1", counts["P1"]), ("P2", counts["P2"]), ("P3", counts["P3"]), ("Open total", sum(counts.values()))])
    if not active:
        empty_state("task_alt", "No open incidents", "The monitors run every 5 minutes (`aecom monitors`).")
    else:
        for i in active:
            _incident_card(i, reviewer)

    with st.expander("Recently resolved"):
        resolved = incidents.list_incidents(status="resolved")[:20]
        if not resolved:
            st.caption("Nothing resolved yet.")
        else:
            st.dataframe(mask_df(pd.DataFrame(resolved)[["monitor", "severity", "subject", "first_seen",
                                                          "resolved_at", "notes"]], ["notes"]),
                         hide_index=True, width="stretch")


# --------------------------------------------------------------------------------------------- monitors & dead letters
def _monitor_rows() -> list[dict]:
    """One row per monitor that has ever raised an incident: its current status and when it last fired - the only
    durable record of a monitor's activity (monitor runs themselves are not logged)."""
    by_monitor: dict[str, list[dict]] = defaultdict(list)
    for i in incidents.list_incidents():
        by_monitor[i["monitor"]].append(i)
    rows = []
    for name, lst in sorted(by_monitor.items()):
        active = [i for i in lst if i["status"] in incidents.ACTIVE]
        worst = min((i["severity"] for i in active), key=incidents.SEVERITIES.index, default=None)
        rows.append({"monitor": name, "status": worst or "ok", "open_now": len(active),
                    "last_activity": max(i["last_seen"] for i in lst)})
    return rows


with tab_monitors:
    st.subheader("Monitor activity")
    st.caption("Each monitor runs every 5 minutes (the scheduler's `monitors` job) and opens or clears an incident "
              "per problem it sees; this table is built from that incident history.")
    rows = _monitor_rows()
    if not rows:
        empty_state("monitor_heart", "No monitor has raised an incident yet")
    else:
        df = pd.DataFrame(rows)
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"status": st.column_config.TextColumn("status")})

    st.subheader("Scheduler heartbeat")
    last_event = sql("SELECT MAX(ts) AS last_event FROM ops_events")["last_event"][0]
    bs = bus_status()
    kpi_row([("Last event processed", str(last_event) if last_event else "never"), ("Queued", bs["queued"]),
            ("Waiting for retry", bs["waiting_for_retry"]), ("Dead letter", bs["dead_letter"])])
    st.caption("The event bus is drained every 30 s; a stale `Last event processed` time means the scheduler "
              "process itself may be down.")

    st.subheader("Dead letters")
    letters = dead_letters()
    if not letters:
        empty_state("task_alt", "No dead-lettered events")
    else:
        st.dataframe(mask_df(pd.DataFrame(letters), ["last_error"]), hide_index=True, width="stretch")
        ids = [row["id"] for row in letters]
        choice = st.multiselect("Retry which event(s)", ids, default=ids)
        if st.button("Retry selected", disabled=not choice, icon=":material/restart_alt:"):
            n = retry(choice)
            st.success(f"Re-queued {n} event(s); the scheduler drains them on its next tick.")
            st.rerun()


# --------------------------------------------------------------------------------------------------------- doctor
with tab_doctor:
    st.caption("Runs every check in-process (database, schema, kill switch, event bus, invariants, approvals, "
              "errors, stuck runs, credentials, catalog sync, disk and backup) - read-only and fast enough to run "
              "on demand, so it does not need to go through a background job.")
    if st.button("Run doctor checks", type="primary", icon=":material/monitor_heart:"):
        st.session_state["_doctor_report"] = doctor.report()
    report = st.session_state.get("_doctor_report")
    if report is None:
        empty_state("monitor_heart", "No report yet", "Click \u201cRun doctor checks\u201d for a full status report.")
    else:
        s = report["summary"]
        kpi_row([("ok", s["ok"]), ("warn", s["warn"]), ("fail", s["fail"]), ("version", report["version"])])
        st.caption(f"Generated {report['generated_at']}")
        for c in report["checks"]:
            cols = st.columns([1, 2, 5])
            cols[0].badge(c["status"], color={"ok": "green", "warn": "orange", "fail": "red"}.get(c["status"], "gray"))
            cols[1].markdown(f"**{c['name']}**")
            cols[2].caption(c["detail"])


# ------------------------------------------------------------------------------------------------- backup & deploy
with tab_backup:
    st.subheader("Backup status")
    backups = backup.list_backups()
    if not backups:
        empty_state("backup", "No backups yet", "Create one from Settings > Backups and updates.")
    else:
        newest = backups[0]
        try:
            manifest = backup.read_manifest(newest)
        except backup.BackupError as exc:
            st.error(str(exc))
            manifest = {}
        age = datetime.now(UTC) - backup.created_at(newest)
        kpi_row([("Last backup", newest.name), ("Age", f"{age.total_seconds() / 3600:.1f} h"),
                ("Verified", "yes" if manifest.get("verified") else "no"), ("Kept", len(backups))])
        if manifest.get("verified"):
            st.caption(f"Verified at {manifest.get('verified_at', '—')}.")
        else:
            st.caption("Not verified yet - verify it from Settings > Backups and updates.")
        st.page_link("app_pages/settings.py", label="Manage backups", icon=":material/backup:")

    st.subheader("Deployment")
    kpi_row([("Deployed revision", doctor.version())])
    last = (jobs.list_jobs(kind="update", limit=1) or [None])[0]
    if last is None:
        st.caption("No `scripts/update.sh` run has been recorded yet. Run `scripts/update.sh --check` on the machine "
                   "for a dry run listing any commits not yet deployed.")
    else:
        res = last["result"] or {}
        kpi_row([("Last update", str(res.get("status", last["status"]))), ("Stopped at", res.get("stage") or "-"),
                 ("From", res.get("from") or "-"), ("To", res.get("to") or "-")])
        st.caption(f"Recorded {last['finished_at']:%Y-%m-%d %H:%M} UTC.")
