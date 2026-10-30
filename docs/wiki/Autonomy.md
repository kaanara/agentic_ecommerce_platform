# Autonomy

The company starts supervised: anything above small limits waits for a human. **Autonomy is earned per action, with evidence, and lost automatically.** Nothing here can be changed by an agent.

## Stages
Five actions can be promoted. Stage 1 is today's behaviour. A higher stage raises **only the per-call limit**.

| Action | Stage 1 | Stage 2 | Stage 3 (also notifies) |
|---|---|---|---|
| `refund_order` | up to €50 | up to €150 | up to €500 |
| `update_price` | change ≤ 20% | ≤ 30% | ≤ 50% |
| `launch_campaign` (per campaign) | €150 | €300 | €600 |
| `record_expense` | €200 | €400 | €800 |
| `register_vendor` (per month) | €50 | €100 | €200 |

The stage lives in the setting `autonomy_stage:<tool>` and is read by the policy gate on every call (`policy/gate.py`, rules in `policy/policy.yaml` under `stages:`).

### What never moves
Stages cannot change: the **monthly marketing and vendor budgets**, the rule that Support may only email the customer of the current case, everything that needs a human (T3) or is forbidden (T4), the kill switch, agent pause, safe mode, and the ban on sign-ups and logins. A call that is allowed only because of a raised stage is marked `autonomous` in the audit log.

## Promotion: a human, with evidence
```bash
uv run aecom autonomy report                                  # per action: stage, approvals, unchanged rate, eligibility
uv run aecom autonomy promote refund_order --by YOURNAME --reason "why"
```
The dashboard has the same on the **Trust** page. Promotion is checked again on the server and has no override, and the check and the write are **one atomic step** (R19): `trust.promote` re-checks eligibility and then calls `autonomy.set_stage` with the exact stage it just saw, as a guarded compare-and-set, inside one DB transaction. If the stage changed underneath it in between - another promotion, or an automatic demotion - the write is refused outright instead of silently applying a decision based on stale evidence; run the command again to re-check. It needs **all** of:

- at least **10 real sales** (orders from the store platform; demo and simulated orders don't count),
- at least **20 decided approvals** for that action since its last stage change,
- at least **95% approved unchanged** (you did not have to edit the agent's arguments),
- **no rejections and no expired approvals** in that window,
- no demotion of that action in the last **14 days**,
- no open P1 incident, and safe mode and the kill switch off.

Thresholds can be changed in `.env` (`AUTONOMY_MIN_REAL_SALES`, `AUTONOMY_MIN_DECISIONS`, `AUTONOMY_MIN_UNCHANGED_RATE`, `AUTONOMY_COOLDOWN_DAYS`). The evidence is recorded in the audit log with every promotion. One stage at a time.

## Demotion: automatic, immediate, back to stage 1
`monitors.run_monitors` (every 5 minutes) demotes promoted actions when:

| Trigger | Demotes |
|---|---|
| Safe mode on, or the kill switch used in the last 24 h | all promoted actions |
| Any open or acknowledged P1 incident | all promoted actions |
| More than 10% of the last 7 days' orders refunded (at least 10 orders), or one customer with more than 2 refunds in 30 days | `refund_order` |
| One actor denied by the gate 20 or more times in an hour | all promoted actions |
| ROAS below 1.0 after at least €100 of ad spend in 7 days | `launch_campaign` |
| Gross margin below 50% with at least 10 orders in 7 days | `update_price` |

Each demotion **raises its P2 incident (`autonomy_demotion`) first and demotes the tool second** (R19): `raise_incident` is idempotent (it refreshes the already-open incident for the same tool rather than duplicating it), so if the demotion write itself then fails, the incident - the audit trail that something is wrong - is never lost; the next monitor tick (5 minutes later) finds the tool still promoted, reuses the same open incident, and completes the demotion. The incident stays until you acknowledge it. The refund and denial triggers also raise their own alerts (`refund_abuse`, `policy_deny_spike`) even when nothing is promoted. See [Runbooks](Runbooks).

## Stage-3 actions also notify immediately
A stage-3 ("notify") autonomous action - the gate already marks it `autonomous: true`, `autonomy_stage: 3` in the audit log - sends an immediate owner notification (`aecom.notify.route`, `kind: "autonomy"`, masked text naming the tool) in addition to the daily flash and the log (R19). It is sent by `autonomy_guard.notify_stage3_actions`, which runs on the same ~5-minute monitor tick as the demotion checks above (a bookmark in `ops_settings` keeps it idempotent): the tool call itself happens in the policy gate (`policy/gate.py`), which this fix does not touch, so "immediate" means the next tick rather than synchronous with the call - still far sooner than the daily flash.

## Where you see it
- **Trust page** (dashboard) and `aecom autonomy report|log`.
- The **daily flash** and the monthly **governance report** list autonomous actions, demotions and current stages.
- A chaos invariant (`no_unearned_promotion`) fails if a stage is raised without a recorded human promotion, so tampering with the setting is detected.

## Limits to know
- Trust is collected from now on: approvals from before this feature have no "edited" information.
- The Judge's scores are **not** a demotion trigger, and R19 deliberately did not add one. The Judge reviews a whole
  company run (`ceo_direct → foreman_plan → execute → judge`), not a single tool call: its verdict (`submit_verdict`)
  is only persisted as one `ops_tool_calls` row keyed by `run_id`, and several unrelated T2 actions - different
  tools, different agents, different departments - can share that same `run_id` in one run. `ops_agent_runs.status`
  even has an unused `"vetoed"` value that nothing ever sets, confirming a per-action verdict was never wired up.
  Blaming one specific autonomous action for a run-level rejection that may be about a sibling department's output
  would be invented precision, not evidence - so this trigger needs a schema change (recording the Judge's verdict
  per task/tool-call, not per run) before it can be added safely. If/when that exists, the trigger should demote a
  tool to stage 1 with an incident when the Judge vetoed/failed at least `autonomy_judge_fail_demote` (default 2) of
  the last `autonomy_judge_window` (default 10) of its autonomous actions; those two fields need to be added to
  `config.Settings` (so `getattr(get_settings(), "autonomy_judge_fail_demote", 2)` resolves to a real, overridable
  value) and to the "Autonomy" section of `settings_registry.py` - see the R19 report for the exact rows.
