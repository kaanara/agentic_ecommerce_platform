# Live Validation

Roadmap "Next" item 1: run every scenario (see [Workflows and Scenarios](Workflows-and-Scenarios)) against the
**real** Claude API instead of the scripted offline replay, record what actually happened and what it actually
cost, and flag drift between the configured `PRICE_*` settings and Anthropic's published prices so they can be
calibrated. Implemented in `aecom.live_validation`; no CLI wiring exists yet (see *Planned CLI* below).

## What it does

- **Refuses** unless `LLM_MODE=anthropic` **and** `ANTHROPIC_API_KEY` is set (`.env` or environment) — it exists to
  spend real money against the real API, so it never silently falls back to scripted agents and never runs
  without a usable key.
- **Never touches the company database.** Each scenario gets its own fresh, throwaway SQLite file under the
  system temp directory (the same technique `aecom.chaos` and `aecom chaos` use — see
  `aecom.chaos._is_disposable_db`). `DATABASE_URL` is switched and the engine reset before each scenario, and the
  originally configured database URL and engine are restored before `run()` returns, even if a scenario crashes.
- **Caps total spend at `max_eur`** (default €2.00), two ways:
  - *Between scenarios*: before starting the next scenario, if spend so far is already at or above the cap, every
    remaining scenario is recorded as `skipped` instead of run.
  - *Within a scenario* (best effort): every agent's daily budget (`aecom.graph.agents.daily_budget`) is wrapped
    for the duration of the run so it never exceeds what is left of the cap, live-recomputed from the current
    scenario's own `AgentRun` rows before each agent call. This narrows — but, being a soft per-agent check, does
    not guarantee — a single scenario's overshoot past the cap; the between-scenario check is the hard guarantee.
- **Reports**, per scenario: status (`completed` / `error` / `skipped`), wall-clock time, number of `AgentRun`
  rows, input/output tokens, cost in EUR (summed from those rows — real cost from real token counts, not an
  estimate), approvals created, and — for scenarios whose result carries a Judge `verdict` (company/selection
  graph state) — whether that verdict's shape still holds up (`approved` is a bool, `score` in `0..10` if
  present). Scenarios without a verdict (e.g. `order-delivery`, `support-refund`) report `verdict: null`.
- **Calibrates `PRICE_*`**: groups the run's `AgentRun` rows by model tier (reversed from `AgentRun.model` via
  `settings.model_for_tier`) and compares the observed average cost per run (computed with the *configured*
  `price_opus`/`price_sonnet`/`price_haiku`) against what the same token counts would cost at
  `aecom.live_validation.REFERENCE_PRICE_USD` — a small table of Anthropic's published per-tier prices kept in
  that module, which a maintainer updates whenever Anthropic changes pricing. A gap over 15% is flagged, and the
  reference price is printed as the suggested `PRICE_*` value. **Nothing is written automatically** — apply a
  suggested value yourself in Settings → "AI and models" (`price_opus`/`price_sonnet`/`price_haiku` are already
  dashboard-editable `(input, output)` pairs) or in `.env`.
- **Masks secrets**: every error message and the full report are passed through `aecom.observability.mask` before
  being written, so API keys and other secret-shaped strings never land on disk.
- **Writes** `data/live_validation/<timestamp>/report.json` and `report.md` — a sibling of `data/backups/`
  (`BACKUP_DIR`), never inside it, so backups never pick these reports up. `.env` is never written to.

## Running it

There is no `aecom live-check` subcommand yet (see *Planned CLI*); call the function directly for now:

```bash
LLM_MODE=anthropic uv run python -c "
from aecom.live_validation import run
import json
print(json.dumps(run(max_eur=2.0, by='your-name'), indent=2))
"
```

Pass `names=["bootstrap", "support-refund"]` to run a subset instead of all 8 scenarios.

### Planned CLI

```
aecom live-check [scenario ...] --max-eur N [--by NAME]   run scenario(s) against the real Claude API, capped
                                                            at €N total spend (default: all scenarios, €2.00)
```

See the "cli.py patch" in the handoff / PR description for the exact `argparse` + dispatch diff; `cli.py` is not
owned by this change and is left for a follow-up.

## What it roughly costs

Each scenario runs 2–4 agents for a handful of steps. At the default Sonnet-tier pricing (`price_sonnet = (3.0,
15.0)` USD per 1M input/output tokens) a typical scenario costs a few cents; the full 8-scenario suite has
historically landed well under €1 total. `--max-eur` exists precisely because that is an estimate, not a
guarantee — Claude's actual tool-use loop length depends on what it decides to do.

## What it needs

- `ANTHROPIC_API_KEY` set to a **capped** key (a key with a hard spend limit configured on the Anthropic side, or
  a dedicated low-limit key used only for this). This module's own `--max-eur` is a software limit on *this run*;
  it is not a substitute for a provider-side cap in case of a bug or a runaway agent loop.
- `LLM_MODE=anthropic` for the duration of the run (the module refuses otherwise).
- Nothing else — it provisions its own throwaway database and restores your configured one when done.
