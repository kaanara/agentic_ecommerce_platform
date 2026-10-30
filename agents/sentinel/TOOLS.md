---
tools: [list_open_incidents, get_incident, recent_errors, run_timeline, event_bus_status, dead_letters, doctor_report,
        list_orders, get_order, get_inventory, list_pending_approvals,
        replay_dead_letter, rerun_reconciler, resend_delivery_email, pause_agents, propose_correcting_entry,
        submit_diagnosis, message_owner]
server_tools: []
---
# Sentinel tools
Read-only diagnostic tools (`aecom.tools.diagnostics`) and a few read-only store tools, plus the terminal
`submit_diagnosis`, which only appends a note to the incident. Runbooks arrive as skills (`runbook_<monitor>`).

Remediation tools (`aecom.tools.remediation`), each checked by the policy gate (kill switch, safe mode, pause):
- `replay_dead_letter(event_id)`: re-queue one dead-lettered event and drain the bus. order.* / inventory.* / email.*
  topics run at once (T2); any other topic is escalated to a human.
- `rerun_reconciler()`: re-publish delivery for paid orders left undelivered or unemailed, then drain (T1).
- `resend_delivery_email(order_id)`: only for a delivered order whose email was never sent; refused otherwise (T2).
- `pause_agents(reason)`: containment; deliveries keep running. Audited with your reason (T1).
- `propose_correcting_entry(lines, memo, ref)`: a NEW balanced ledger entry, `lines = [[account, debit, credit], ...]`.
  Always waits for a human (T3); posted with memo `correction: ...` only when approved. Never edits existing rows.

There is no tool to unpause agents, leave safe mode, change the kill switch, acknowledge or resolve incidents, or
update or delete ledger rows: those stay human. No `request_account_need` either: needs go into the next actions.

`message_owner` reaches the owner outside chat (tier T1, rate-limited, quiet hours apply).
