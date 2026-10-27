---
tools: [list_open_incidents, get_incident, recent_errors, run_timeline, event_bus_status, dead_letters, doctor_report,
        list_orders, get_order, get_inventory, list_pending_approvals, submit_diagnosis]
server_tools: []
---
# Sentinel tools
Read-only diagnostic tools (`aecom.tools.diagnostics`) and a few read-only store tools, plus the terminal
`submit_diagnosis`, which only appends a note to the incident. It has no side-effecting tools at all (not even
`request_account_need`): needs it notices go into the diagnosis' next actions. Runbooks arrive as skills (`runbook_<monitor>`).
