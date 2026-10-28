# Runbooks

One runbook per monitor in `src/aecom/monitors.py` (thresholds and alert routing: [Monitoring and Alerting](Monitoring-and-Alerting); the process: [Incident Response](Incident-Response)). Monitors run every 5 minutes in `aecom scheduler`. There is one incident per (monitor, subject), and it **auto-resolves on the first monitor run that no longer reports that subject**. To re-check now instead of waiting: `uv run aecom monitors`.

## Always first
```bash
uv run aecom doctor                                            # DB, schema, kill switch, event bus, invariants, approvals, errors, stuck runs, ...
uv run aecom incidents                                         # open + acknowledged (--all includes resolved)
uv run aecom incidents ack <ID> --by <NAME> --note "looking"   # stops P1 repeat alerts; does not resolve
```
Inspect the database with `sqlite3 data/aecom.db` (or the file in `DATABASE_URL`). **Use read-only queries only.**

**Never, for any incident:** edit or delete `ledger_entries` rows (book a correcting entry instead), edit `commerce_license_keys` by hand, delete rows from `ops_events`, `ops_incidents`, `ops_errors` or `ops_audit_log`, or `incidents resolve` something that is still failing (the next monitor run opens a new incident anyway).

---

## undelivered_order (P1)
| | |
|---|---|
| **Means** | An order has been `paid` or `failed` for more than 15 min (subject = order id). `failed` = no licence keys at delivery time. A paying customer has no key. |
| **First checks** | `uv run aecom doctor` (kill switch on?), `uv run aecom events dead`, `sqlite3 data/aecom.db "select id,status,created_at from commerce_orders where status in ('paid','failed')"`; look for a `restock_failed` or `order_dead_letter` incident for the same SKU or order. |
| **Safe actions** | Fix the cause first (kill switch: `uv run aecom kill-switch off`; no keys: restock the SKU). Then `uv run aecom reconcile` (re-publishes `order.paid`, idempotent) or `uv run aecom events retry <EVENT_ID>` for its dead letter. |
| **Do NOT** | Set the order to `delivered` in SQL or send keys by hand: delivery assigns the keys and books the ledger. |
| **Auto-resolves** | When the order leaves `paid`/`failed` (delivered or refunded). |

## unemailed_order (P1)
| | |
|---|---|
| **Means** | An order is `delivered` / `partially_refunded` for more than 15 min but `email_sent` is false: keys are assigned, the access email never went out. |
| **First checks** | `uv run aecom doctor` (credentials, AgentMail live), `uv run aecom events dead`, `sqlite3 data/aecom.db "select ts,component,exc_type,message from ops_errors order by id desc limit 20"`, `... "select * from ops_outbox_emails order by id desc limit 5"`. |
| **Safe actions** | Fix the mail cause (key, outage), then `uv run aecom reconcile`: it requeues the order and `deliver_order` only resends the email. |
| **Do NOT** | Assign new keys, create a new order, or set `email_sent` by hand. |
| **Auto-resolves** | When `email_sent` becomes true. |

## ledger_imbalance (P1)
| | |
|---|---|
| **Means** | Debits ≠ credits, overall or within one `txn_id` (subject = `ledger`; the detail lists transactions in cents). The money records are inconsistent. |
| **First checks** | `uv run aecom doctor` (invariants), `sqlite3 data/aecom.db "select txn_id,sum(debit),sum(credit) from ledger_entries group by txn_id having round(sum(debit)-sum(credit),2)<>0"`, then `ops_errors` / `ops_audit_log` around that time. |
| **Safe actions** | Contain: `uv run aecom agents-pause on` if an agent is booking. Find the bug (it gets a regression test). Fix with a **new correcting entry** that balances the transaction, prepared as a T3 approval. A human decides. |
| **Do NOT** | Update or delete existing ledger rows. The ledger is append-only. |
| **Auto-resolves** | When every transaction and the totals balance again. |

## duplicate_key (P1)
| | |
|---|---|
| **Means** | One licence key value is on two orders, or an order holds more keys than its quantity (subject = `license_keys`; the detail names key ids and order ids, never the key). A sellable secret may have reached the wrong customer. |
| **First checks** | `uv run aecom doctor` (invariants), `sqlite3 data/aecom.db "select id,order_id from commerce_license_keys where id in (<ids from detail>)"`, `ops_audit_log` for those orders. |
| **Safe actions** | Contain: `uv run aecom agents-pause on`. A human decides per customer (replacement key, or a refund through the normal approval path) and revokes the duplicated key with the vendor. |
| **Do NOT** | Paste key values into issues, chat or alerts; move keys between orders in SQL. |
| **Auto-resolves** | When no key value spans two orders and no order holds more than `quantity` keys. |

## order_dead_letter (P1)
| | |
|---|---|
| **Means** | An `order.*` or `inventory.*` event failed 5 attempts and was dead-lettered (subject = event id). Delivery or restock for an order has stopped. |
| **First checks** | `uv run aecom events dead` (topic, attempts, `last_error`), `sqlite3 data/aecom.db "select ts,component,exc_type,message from ops_errors where event_id=<ID>"`. |
| **Safe actions** | Fix the cause, then `uv run aecom events retry <ID>` (re-queues and drains). For `order.paid`, `uv run aecom reconcile` also works. |
| **Do NOT** | Retry again and again without a fix (it dead-letters after 5 more attempts); delete the event row. |
| **Auto-resolves** | When the event is no longer dead (retried). |

## restock_failed (P1)
| | |
|---|---|
| **Means** | An `inventory.restock_failed` event for a SKU in the last 24 h (subject = sku): there is no key source, so retrying can't help. New orders for that SKU end up `failed` (see `undelivered_order`). |
| **First checks** | `uv run aecom doctor`, `sqlite3 data/aecom.db "select ts,payload from ops_events where topic='inventory.restock_failed' order by id desc limit 5"`, stock on Dashboard → Business. |
| **Safe actions** | Add keys from the real vendor or key source with the normal supply tools, then `uv run aecom reconcile` for waiting orders. Consider pausing the product. |
| **Do NOT** | Invent keys or reuse keys that were already sold. |
| **Auto-resolves** | Only after 24 h without a new `restock_failed` for that SKU, so acknowledge it once fixed to stop the repeats. |

## other_dead_letter (P2)
| | |
|---|---|
| **Means** | Any other event (not `order.*` / `inventory.*`) dead-lettered after 5 attempts (subject = event id). Some agent or bookkeeping work did not happen. |
| **First checks** | `uv run aecom events dead`, `ops_errors` for that `event_id`. |
| **Safe actions** | Fix the cause, then `uv run aecom events retry <ID>`. If the event is obsolete, leave it dead and acknowledge the incident with a note. |
| **Do NOT** | Delete the event row. |
| **Auto-resolves** | When the event is retried. |

## event_backlog (P2)
| | |
|---|---|
| **Means** | More than 100 waiting/queued events, or the oldest due event is older than 10 min (subject = `event_bus`). The drain job (every 30 s) is behind or not running. |
| **First checks** | `uv run aecom doctor` (event bus), `systemctl status` / `journalctl` for the scheduler, `ops_errors` where `component='event_bus'`. |
| **Safe actions** | Restart the scheduler; `uv run aecom drain` once by hand. |
| **Do NOT** | Mark events processed in SQL. |
| **Auto-resolves** | When pending ≤ 100 and no due event is older than 10 min. |

## stuck_run (P2)
| | |
|---|---|
| **Means** | An agent run has been `running` for more than 30 min (subject = run id). Usually a crashed process or a hung external call. |
| **First checks** | Dashboard → Agents → Recent runs, `sqlite3 data/aecom.db "select id,agent_id,started_at from ops_agent_runs where status='running'"`, `ops_errors` with that `run_id`, `journalctl`. |
| **Safe actions** | Restart the scheduler; `uv run aecom agents disable <AGENT_ID>` if it keeps happening. |
| **Do NOT** | Re-run the same task blindly: some of its tool calls may have happened (check `ops_tool_calls`). |
| **Auto-resolves** | When the run leaves `running`. |

## errors (P2)
| | |
|---|---|
| **Means** | At least one `ops_errors` row in the last hour (subject = `ops_errors`; the detail has the count). Unexpected exceptions in an agent, the gate, the event bus, the API or the scheduler. |
| **First checks** | `sqlite3 data/aecom.db "select id,ts,component,run_id,event_id,exc_type,message from ops_errors order by id desc limit 20"`, then `"select traceback from ops_errors where id=<ID>"`. |
| **Safe actions** | Handle by component (dead letter, stuck run, external outage). A code bug becomes a GitHub issue with the traceback and a failing test. |
| **Do NOT** | Delete `ops_errors` rows to silence it. |
| **Auto-resolves** | One hour after the last new error. |

## approval_ageing (P2)
| | |
|---|---|
| **Means** | An approval has been `pending` for more than 24 h (subject = approval id). An agent is blocked waiting for you. |
| **First checks** | `uv run aecom approvals` (pending list), Dashboard → Approvals. |
| **Safe actions** | `uv run aecom approvals approve <ID> --by <NAME> --note "..."` or `uv run aecom approvals reject <ID> --by <NAME> --note "..."`. |
| **Do NOT** | Approve without reading just to clear the alert. |
| **Auto-resolves** | When the approval is approved or rejected. |

## kill_switch_long (P2)
| | |
|---|---|
| **Means** | The kill switch has been on for more than 1 h (subject = `kill_switch`; first seen on is stored in setting `kill_switch_since`). Reflexes go through the gate, so **deliveries are stopped too**. |
| **First checks** | `uv run aecom doctor`, `uv run aecom incidents` (why was it turned on?), `ops_audit_log` for who set it. |
| **Safe actions** | Once the cause is fixed: `uv run aecom kill-switch off`, then `uv run aecom reconcile` for waiting orders. To stop agents but keep delivering, use `uv run aecom agents-pause on` instead. |
| **Do NOT** | Use the kill switch as a long-term pause; edit `kill_switch_since`. |
| **Auto-resolves** | When the kill switch is off. |

## monitor_error (P2)
| | |
|---|---|
| **Means** | A monitor itself raised (subject = monitor name). That monitor is blind, and its own incidents are left as they were. |
| **First checks** | `sqlite3 data/aecom.db "select ts,exc_type,message,traceback from ops_errors where component='monitors' order by id desc limit 5"`, `uv run aecom doctor` (schema), `uv run aecom monitors`. |
| **Safe actions** | Fix the DB/schema or the code bug (with a regression test). Until then, check that monitor's condition by hand. |
| **Do NOT** | Resolve that monitor's other incidents by hand: their state is unknown. |
| **Auto-resolves** | On the first run where the monitor completes. |

## awaiting_consent_order (P3)
| | |
|---|---|
| **Means** | An order has been `awaiting_consent` for more than 24 h (subject = order id): the customer paid but never gave the express consent to immediate delivery (withdrawal right), so nothing was delivered. This is held on purpose, not a delivery fault. |
| **First checks** | `sqlite3 data/aecom.db "select id,customer_id,created_at,withdrawal_consent_at from orders where id='<ID>'"`, Dashboard → Orders, and the customer's email thread. |
| **Safe actions** | Ask the customer for consent; once recorded the order is `paid` again and `uv run aecom reconcile` delivers it. If the customer will not consent, refund the order. |
| **Do NOT** | Set the order to `delivered` or fill `withdrawal_consent_at` by hand without the customer's actual consent; hand out a key manually. |
| **Auto-resolves** | When the order leaves `awaiting_consent` (consent recorded, delivered, or refunded). |

## Safe mode is on
| | |
|---|---|
| **Means** | Planned (K7, not built yet): a P1 unacknowledged for 2 h puts the company in safe mode (agents paused, deliveries and support replies continue, no spending). Today the equivalent is agents pause on (`uv run aecom agents-pause on`). |
| **First checks** | `uv run aecom doctor` (shows kill switch and agents pause), `uv run aecom incidents` (the P1 that caused it). |
| **Safe actions** | Work the P1 with its runbook and acknowledge it. Once it is fixed: `uv run aecom agents-pause off`. |
| **Do NOT** | Switch to the kill switch instead (that also stops deliveries); turn agents back on before the P1 is understood. |
| **Auto-resolves** | No: a human turns it off. |
