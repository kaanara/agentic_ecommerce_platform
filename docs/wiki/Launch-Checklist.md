# Launch Checklist

Roadmap milestone **L4 - "First 10 sales before autonomy"**: launch one product with agents assisting, and a human
approving almost everything. G4-G6 (evidence-gated autonomy promotion, see [Autonomy](Autonomy)) only start once
real approvals exist from that launch - L4 is the entry gate of Phase 5. This page explains the dashboard checklist
that tracks readiness for that launch and the owner's acknowledgements it depends on.

## Where to find it

**Operations > Launch** (dashboard). It is read-only except for five acknowledgement buttons - everything else is
computed from the real state of the company, the same way `aecom doctor` is (`src/aecom/launch.py`,
`aecom.launch.checks()`). A check that fails to run is shown as "needs a look" instead of breaking the page.

## The checklist

| Check | Ready when | Where to fix |
|---|---|---|
| Dashboard login | `DASHBOARD_AUTH` is on and a password is set | Settings > Security and backups > Security |
| Backups scheduled | `BACKUP_ENABLED` / the in-app schedule is on | Settings > Security and backups > Backups and updates |
| Backup verified recently | the newest **verified** backup (`aecom backup verify`) is under 26 hours old | same |
| Off-device backup target | `BACKUP_TARGET` is set | same |
| Backup encryption | `BACKUP_PASSPHRASE` is set | same |
| Anthropic key + mode | `ANTHROPIC_API_KEY` set and `LLM_MODE=anthropic` | Settings > Money and AI > AI and models |
| Real store ready | `STORE_ADAPTER=lemonsqueezy`, its API key and webhook secret are set, and `aecom store sync` has synced at least one product | Settings > Money and AI > Store and email |
| AgentMail live | `AGENTMAIL_LIVE=true`, `AGENTMAIL_API_KEY` set, an inbox configured | Settings > Money and AI > Store and email |
| Alerts reach the owner | Telegram paired, or `NTFY_URL` set | Settings > Channels and chat > Channels / Alerts and notifications |
| Heartbeat configured | `HEALTHCHECKS_PING_URL` set | Settings > Channels and chat > Alerts and notifications |
| Kill switch and safe mode off | both off right now | Overview |
| `aecom doctor` has no FAIL | no check reports FAIL (a WARN only needs a look) | Health > Doctor |
| Migrations at head | the database is at the latest Alembic revision | run `aecom init` |

### Acknowledged by a human (the app cannot verify these)

Five items can only be judged by the owner. Ticking them calls `aecom.launch.acknowledge(key, by)`, which is
audited (`launch_ack_<key>`) and has **no agent tool** - the same human-only pattern as closing/reopening an
accounting period (`aecom.periods`).

- **Anthropic spend cap** - a spend cap is set at console.anthropic.com.
- **Legal pages** - the legal pages are published (`docs/legal/`: privacy policy, terms, imprint).
- **Human on-call** - a human is reachable for P1/P2 alerts.
- **Webhook URL publicly reachable** - the Lemon Squeezy/AgentMail webhook URLs were set up and tested (see
  [Raspberry Pi](Raspberry-Pi), section 6, Webhooks).
- **Data retention table** - the open `[DECISION]` rows in [GDPR and Retention](GDPR-and-Retention) (H10) have been
  decided for this launch.

## Progress toward the first 10 sales

The same page shows a progress bar toward the ~10 **real** sales (orders from the real store platform, demo/mock
orders never count - see `aecom.trust.real_sales()`) that L4 asks for, plus the net revenue from those sales
(`aecom.monitors.real_net_revenue_eur()`, the same figure the Pi-to-cloud migration reminder uses). This is the same
evidence `aecom.trust.eligibility()` checks before a tool can be promoted a stage - reaching it does not promote
anything by itself, it only makes promotion *possible* once decisions since then are also good (see
[Autonomy](Autonomy)).

## `launch.ready()`

`aecom.launch.ready()` is `True` only once nothing on the checklist is still "to do" ("needs a look" items do not
block it). Nothing reads `ready()` to gate anything automatically; it is for the owner's own judgement before
accepting real traffic.
