---
budget_eur_per_day: 3
---
# Sentinel policy
Diagnostic tools are T0; `submit_diagnosis` is T1 and reserved for the Sentinel. Runs at most once per incident, only
for new P1/P2 incidents. Remediation tools are reserved for the Sentinel and gated like any other side effect (denied
under the kill switch, in safe mode and while agents are paused): `rerun_reconciler` and `pause_agents` T1,
`replay_dead_letter` T2 (topics other than order/inventory/email escalate), `resend_delivery_email` T2,
`propose_correcting_entry` T3 (always a human). Can't unpause, leave safe mode, change the kill switch, change
incidents (beyond its note) or edit ledger rows.
