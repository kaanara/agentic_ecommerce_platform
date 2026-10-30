---
tools: [eng_read_file, eng_list_files, eng_search, eng_diff, eng_write_file, eng_run_tests, eng_open_pr,
        read_company_doc, request_account_need]
server_tools: []
---
# Engineer tools
Its own tools (`aecom.engineer.tools`), read-only except `eng_write_file`/`eng_run_tests`/`eng_open_pr`, plus two
generic read-only/low-risk tools every non-Sentinel agent has: `read_company_doc` (context) and
`request_account_need` (E4 - if a change needs a credential or account, it asks the CEO for it rather than
inventing one or touching secrets itself).

- `eng_read_file(path)`, `eng_list_files(prefix)`, `eng_search(query)`, `eng_diff()`: read-only (T0), confined to
  the Engineer's own workspace clone (`<ROOT>/data/engineer_workspace/`, never the live checkout); results are
  fenced for readability.
- `eng_write_file(path, content)` (T2): writes in the workspace on its branch `engineer/<slug>` (never `main`),
  committing immediately (author `aecom-engineer`). Allowed only under `src/aecom/store/**`,
  `src/aecom/integrations/**`, `src/aecom/channels/**`, `src/aecom/api/webhooks.py`, `tests/**` and
  `docs/wiki/Integrations.md`. Always denied, whatever the allowlist says: `.env*`, `migrations/`, `scripts/`,
  `deploy/`, `.github/`, `agents/`, `src/aecom/policy/`, this app's own
  `src/aecom/{db,config,secrets,settings_registry,dashboard_auth,net_guard}.py`, `pyproject.toml`, `uv.lock` and the
  `.git` directory. Every path is resolved and confined to the workspace (symlinks and `..` rejected).
- `eng_run_tests(selection)` (T1): runs `uv run pytest -q <selection>` inside the workspace - `selection` must be
  path(s) under `tests/`. `LLM_MODE=scripted`, a throwaway `DATABASE_URL`, a timeout, capped output, and an
  environment built from scratch (not inherited) so no secret in the parent process ever reaches the child.
  Remembers the pass against the exact commit tested.
- `eng_open_pr(title, body)` (T3 - always a human first): once approved, refuses a diff over 800 changed lines, a
  diff touching a denied path, anything that looks like a secret, a HEAD `eng_run_tests` has not already passed, a
  second concurrent open branch, or the daily PR cap (`engineer_pr_daily_cap`, default 3). Pushes only
  `engineer/<slug>` (never force, never `main`/`master`) and opens the PR via the GitHub API (`aecom.net_guard`);
  the body always ends with "Opened by the Engineer agent. Not deployed. A human must review and merge."

There is no tool to merge, deploy, restart anything, edit the live checkout, or read `.env`: those stay human
(`scripts/update.sh`, run by a human or CI, after review).
