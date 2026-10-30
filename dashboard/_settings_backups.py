# Govern > Settings > Backups and updates tab (§5c): list, next run, last result, free disk, verify/back up now,
# restore and download.
#
# `render_backups_tab(by)` is imported and called by settings.py (not defined there - see the module docstring for
# the 2-line patch:
#     from _settings_backups import render_backups_tab
#     ...
#     elif name == "Backups and updates":
#         render_backups_tab(by)
# and dropping "Backups and updates" from PLACEHOLDER_TABS).
#
# Heavy I/O (the actual backup/verify) never runs here: "Back up now"/"Verify" only publish `backup.requested` /
# `backup.verify_requested` on the event bus; the scheduler's `jobs.run_pending()` tick runs them (aecom.backup,
# wave 2) and writes the result to `ops_jobs`, which this tab reads back. Restore and download need the dashboard
# password again (customer data); restore also needs the backup name typed in full.

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from _common import step_up
from aecom import backup, jobs
from aecom.bus.events import publish
from aecom.company_settings import set_setting
from aecom.config import get_settings

RESTORE_UNIT = "aecom-restore.service"


def _next_run() -> str:
    """Next scheduled run of the `backup` job, computed from the `backup_schedule` setting (same grammar the
    scheduler uses - see aecom.schedule_spec). Read-only: this tab never touches the scheduler's own jobs."""
    from aecom import schedule_spec

    settings = get_settings()
    if not settings.backup_enabled:
        return "disabled"
    try:
        kind, kwargs = schedule_spec.parse_schedule(settings.backup_schedule)
    except ValueError:
        return f"invalid schedule ({settings.backup_schedule!r})"
    now = datetime.now(UTC)
    try:
        if kind == "interval":
            from apscheduler.triggers.interval import IntervalTrigger

            trigger = IntervalTrigger(**kwargs, start_time=now)
        else:
            from apscheduler.triggers.cron import CronTrigger

            trigger = CronTrigger(**{k: v for k, v in kwargs.items() if v != "*"})
        nxt = trigger.get_next_fire_time(None, now)
        return nxt.strftime("%Y-%m-%d %H:%M UTC") if nxt else "unknown"
    except Exception as exc:  # noqa: BLE001 - a display glitch must never break the Settings page
        return f"unknown ({exc})"


def _free_disk() -> str:
    try:
        d = backup.backup_dir()
        usage = shutil.disk_usage(d if d.exists() else d.parent if d.parent.exists() else "/")
        return f"{usage.free / 1024**3:.1f} GB free of {usage.total / 1024**3:.1f} GB"
    except OSError as exc:
        return f"unknown ({exc})"


def _backups_table() -> pd.DataFrame:
    rows = []
    for p in backup.list_backups():
        try:
            m = backup.read_manifest(p)
        except backup.BackupError:
            rows.append({"backup": p.name, "created": "", "size (MB)": None, "schema": "", "git": "",
                        "verified": "unreadable", "off-device": ""})
            continue
        size = sum(f.get("bytes", 0) for f in m.get("files", {}).values())
        rows.append({"backup": p.name, "created": m.get("created_at", "")[:19].replace("T", " "),
                    "size (MB)": round(size / 1024**2, 1), "schema": m.get("alembic_revision") or "",
                    "git": m.get("git_sha") or "",
                    "verified": "\u2705" if m.get("verified") else "\u2014",
                    "off-device": {True: "\u2705", False: "\u274c"}.get(m.get("copied_off_device"), "\u2014")})
    return pd.DataFrame(rows)


def _last_result(kind: str) -> str:
    rows = jobs.list_jobs(kind, limit=1)
    if not rows:
        return "never run"
    j = rows[0]
    when = str(j["finished_at"] or j["started_at"] or j["created_at"])[:19]
    if j["status"] == "error":
        return f"{when}: error - {str((j['result'] or {}).get('error', ''))[:200]}"
    if kind == "verify":
        ok = (j["result"] or {}).get("ok")
        return f"{when}: {'ok' if ok else 'FAILED'}"
    problems = []
    if (j["result"] or {}).get("copied_off_device") is False:
        problems.append("off-device copy failed")
    if (j["result"] or {}).get("verified") is False:
        problems.append("verify failed")
    return f"{when}: ok" + (f" ({'; '.join(problems)})" if problems else "")


def _request(topic: str, by: str, **payload: object) -> None:
    publish(topic, {"requested_by": by, **payload})
    st.success("Requested. The scheduler runs it within its next tick; refresh this tab to see the result.")


def _restore_commands(name: str) -> str:
    return (f"On the Pi:\n\n```\nsudo systemctl start {RESTORE_UNIT}\n```\n\n"
           f"(or, without the sudo rule: `uv run python -m aecom.backup` after stopping the scheduler/API/dashboard "
           f"and setting `restore_requested={name}` via `aecom doctor`'s ops_settings).")


def _do_restore(name: str, by: str) -> None:
    set_setting("restore_requested", name)
    from aecom.audit import audit

    audit(f"human:{by}" if by else "system", "restore_requested", "ok", {"backup": name})
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", "start", RESTORE_UNIT], capture_output=True, text=True,
                           timeout=10)
        if r.returncode == 0:
            st.success(f"Restore of {name} started ({RESTORE_UNIT}). The dashboard will restart with it.")
            return
    except (OSError, subprocess.SubprocessError):
        pass
    st.warning(f"Could not start {RESTORE_UNIT} automatically (no sudo rule, or not on the Pi). " + _restore_commands(name))


def render_backups_tab(by: str) -> None:
    settings = get_settings()
    st.caption("Backups are created and verified by the scheduler, never by this dashboard process. The schedule, "
              "verify-after-backup and retention settings live in their own fields above/in Schedules; this tab is "
              "status plus the one-off actions.")

    cols = st.columns(4)
    cols[0].metric("Next scheduled run", _next_run())
    cols[1].metric("Last backup", _last_result("backup"))
    cols[2].metric("Last verify", _last_result("verify"))
    cols[3].metric("Free disk", _free_disk())

    c1, c2 = st.columns(2)
    if c1.button("Back up now", icon=":material/backup:"):
        if not by:
            st.warning("Enter your name in the sidebar first.")
        else:
            _request("backup.requested", by)
    if c2.button("Verify the newest backup", icon=":material/verified:"):
        if not by:
            st.warning("Enter your name in the sidebar first.")
        else:
            _request("backup.verify_requested", by)

    st.subheader("Backups")
    table = _backups_table()
    if table.empty:
        st.caption("No backups yet. Click \u201cBack up now\u201d, or run `uv run aecom backup` on the Pi.")
        return
    st.dataframe(table, hide_index=True, width="stretch")

    names = [p.name for p in backup.list_backups()]
    choice = st.selectbox("Backup", names, key="_backups_row_choice")
    r1, r2, r3 = st.columns(3)
    if r1.button("Verify this backup", key="_verify_row"):
        if not by:
            st.warning("Enter your name in the sidebar first.")
        else:
            _request("backup.verify_requested", by, backup=choice)

    st.divider()
    st.subheader("Restore and download (critical)")
    st.caption("Both need your password again (restore replaces the live database; a download holds customer "
              "personal data). Restore: stops the scheduler, API and dashboard; verifies the backup first; takes "
              "a pre-restore safety backup; restores; runs `aecom doctor`; starts the services back up - and also "
              "needs the backup name typed in full.")
    if not step_up("Confirm your password to restore or download a backup.", key="restore_stepup"):
        return

    typed = st.text_input("Type the backup name in full to restore it", key="_restore_typed")
    if r2.button("Restore", key="_restore_btn", type="primary", disabled=(typed != choice)):
        if not by:
            st.warning("Enter your name in the sidebar first.")
        else:
            _do_restore(choice, by)

    if r3.button("Prepare download", key="_download_btn"):
        from aecom.audit import audit

        audit(f"human:{by}" if by else "system", "backup_download", "ok", {"backup": choice})
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(next(p for p in backup.list_backups() if p.name == choice), arcname=choice)
        st.download_button("Download .tar.gz", buf.getvalue(), file_name=f"{choice}.tar.gz", mime="application/gzip",
                           key="_download_dl")

