# Govern > Operations > Vendor provisioning tab (E5/E6/E7): vendors waiting for a human, with their numbered
# checklist (`aecom provisioning list|done|snooze|confirm`).
#
# `render_provisioning_tab(by)` is imported and called by operations.py (not defined there). Only
# `aecom.provisioning`'s public functions touch `Vendor` rows here (list_provisioning/tick_step/snooze/confirm);
# the credential itself never reaches this module, only its env var name and the already-masked check error.

from __future__ import annotations

import streamlit as st

from _common import empty_state
from aecom import provisioning

SNOOZE_MIN_DAYS = 1
SNOOZE_MAX_DAYS = 14
SNOOZE_DEFAULT_DAYS = 3


def _tick(vendor: str, step_no: int, by: str) -> None:
    try:
        provisioning.tick_step(vendor, step_no, by)
    except (ValueError, KeyError) as exc:
        st.error(str(exc))
        return
    st.rerun()


def _checklist(v: dict, by: str) -> None:
    for step in v["checklist"]:
        row = st.columns([5, 2])
        label = f"{step['no']}. {step['step']}"
        if step["done"]:
            row[0].markdown(f"~~{label}~~")
            row[1].caption("done")
        else:
            row[0].markdown(label)
            if row[1].button("Mark done", key=f"_prov_done_{v['vendor']}_{step['no']}", disabled=not by,
                             icon=":material/check:"):
                _tick(v["vendor"], step["no"], by)


def _snooze(v: dict, by: str) -> None:
    row = st.columns([2, 1])
    days = row[0].number_input("Snooze reminders for (days)", min_value=SNOOZE_MIN_DAYS, max_value=SNOOZE_MAX_DAYS,
                               value=SNOOZE_DEFAULT_DAYS, key=f"_prov_snooze_days_{v['vendor']}")
    if row[1].button("Snooze", key=f"_prov_snooze_{v['vendor']}", disabled=not by, icon=":material/snooze:"):
        try:
            provisioning.snooze(v["vendor"], int(days), by=by)
        except (ValueError, KeyError) as exc:
            st.error(str(exc))
            return
        st.success(f"Reminders for {v['vendor']} snoozed for {int(days)} days.")
        st.rerun()


def _confirm(v: dict, by: str) -> None:
    st.caption(f"The credential checked out but {v['vendor']} needs your confirmation before it goes live.")
    if st.button("Confirm it works", key=f"_prov_confirm_{v['vendor']}", type="primary", disabled=not by,
                icon=":material/verified:"):
        try:
            provisioning.confirm(v["vendor"], by)
        except (ValueError, KeyError) as exc:
            st.error(str(exc))
            return
        st.success(f"{v['vendor']} is active. The Foreman integrates it next.")
        st.rerun()


def _status_line(v: dict) -> None:
    if v["waiting_for_your_confirmation"]:
        st.info("Waiting for your confirmation.", icon=":material/pending_actions:")
    elif v["last_check_error"]:
        st.warning(v["last_check_error"], icon=":material/error:")
    else:
        st.caption(f"Waiting for the credential in `{v['credential_env'] or '(no env var set)'}`.")


def _card(v: dict, by: str) -> None:
    with st.container(border=True):
        top = st.columns([3, 1, 1])
        top[0].markdown(f"### {v['vendor']}")
        top[1].caption(v["category"])
        top[2].caption(f"\u20ac{v['monthly_cost_eur']:.2f}/mo")
        badge_cols = st.columns([1, 3])
        badge_cols[0].badge(f"activation: {v['activation']}", color="blue")
        if v.get("activation_reason"):
            badge_cols[1].caption(v["activation_reason"])
        _status_line(v)
        st.markdown("**Checklist**")
        _checklist(v, by)
        _snooze(v, by)
        if v["waiting_for_your_confirmation"]:
            _confirm(v, by)


def render_provisioning_tab(by: str) -> None:
    st.caption("Vendors a human approved that are still waiting on a credential, a checklist step, or your "
              "confirmation that they work. Credentials themselves never show here, only their env var name.")
    if not by:
        st.warning("Enter your name in the sidebar first.")
        return
    vendors = provisioning.list_provisioning()
    if not vendors:
        empty_state("rocket_launch", "Nothing waiting for provisioning",
                    "Approved vendor requests appear here until they are active.")
        return
    for v in vendors:
        _card(v, by)
