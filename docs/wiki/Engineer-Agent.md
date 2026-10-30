# Engineer Agent

> Roadmap item **E3**. The highest-risk agent in the company, by design: it is the only one that writes code. It
> writes adapters and webhooks on a branch and opens a pull request; a human reviews and merges; **it never
> deploys**. Containment is enforced in code (`aecom.engineer.workspace`, `aecom.engineer.github_api`), not only in
> the prompt, so a jailbroken or confused model still cannot reach the live checkout, read a secret, push anywhere
> but its own branch, or skip tests before a PR.

## Identity
`agents/engineer/` (persona Forge, sonnet tier, reports to the CEO), same five-file shape as every other agent
([Agents](Agents)). It is **not chat-eligible** (`config.chat_agents` does not list it) and **not dispatched by the
Foreman** (`graph/supervisor.DEPARTMENTS` does not list it): a human starts its work directly, the same way any
agent can be run from the CLI/dashboard, rather than it being reachable through chat or a company directive.

## Its own workspace
Every tool works inside `<ROOT>/data/engineer_workspace/` (override: `ENGINEER_WORKSPACE_DIR`, used by tests) -
**never the live checkout**. The first call clones it:

- `git clone --local <ROOT>` by default (a fast, local, separate clone of this checkout), or
- `git clone <ENGINEER_REPO_URL>` when that is set (a real remote in production; a local bare repo in tests).

Every path a tool touches is resolved and must stay inside that clone after following symlinks and `..`
(`aecom.engineer.workspace.resolve_in_workspace`) - a traversal or symlink-escape attempt is refused, not silently
contained.

## One branch at a time
All writes happen on `engineer/<slug>` (the slug comes from the current run id), never on `main`/`master`. Only one
such branch may be open at once: `ops_settings["engineer_open_branch"]` is set when the first file is written and
checked before starting a new one; a human releases it with `aecom.engineer.workspace.clear_open_branch(by)` after
a PR is merged or dropped (CLI command: see the patch snippet below - not yet wired into `aecom.cli`, an owned file
this change does not touch).

## Tools (`aecom.engineer.tools`, policy.yaml "E3" rows)
| Tool | Tier | What |
|---|---|---|
| `eng_read_file(path)` | T0 | Read a file from the workspace (fenced). Broad - secrets (`.env*`) and `.git/` internals excepted. |
| `eng_list_files(prefix)` | T0 | List tracked files, optionally under `prefix`. |
| `eng_search(query)` | T0 | `git grep` a literal string across tracked files (fenced). |
| `eng_diff()` | T0 | Unified diff of the current branch vs. the base branch (fenced). |
| `eng_write_file(path, content)` | T2 | Write + commit (author `aecom-engineer`) on the branch. Allow/deny list below. |
| `eng_run_tests(selection)` | T1 | `uv run pytest -q <selection>` inside the workspace, scrubbed environment. |
| `eng_open_pr(title, body)` | T3 | Always a human first; its own refusals run only once approved. |

### Write allow/deny list (`eng_write_file`)
**Allowed:** `src/aecom/store/**`, `src/aecom/integrations/**`, `src/aecom/channels/**`,
`src/aecom/api/webhooks.py`, `tests/**`, `docs/wiki/Integrations.md`.

**Always denied**, even if a future allowlist change would otherwise match: `.env*`, `migrations/`, `scripts/`,
`deploy/`, `.github/`, `agents/`, `src/aecom/policy/`, this app's own
`src/aecom/{db,config,secrets,settings_registry,dashboard_auth,net_guard}.py`, `pyproject.toml`, `uv.lock`, the
`.git` directory.

### `eng_run_tests`: an isolated child
`selection` must name path(s) under `tests/`. The child process gets an environment **built from scratch**, not
inherited - only `PATH`/`HOME`/`LANG`/`UV_CACHE_DIR`/`VIRTUAL_ENV`/`TMPDIR` are kept, plus `LLM_MODE=scripted` and a
throwaway sqlite `DATABASE_URL` under the workspace. No secret in the parent process (`ANTHROPIC_API_KEY`,
`GITHUB_TOKEN`, anything `*_KEY`/`*_TOKEN`/`*_SECRET`/`*_DSN`) is ever visible to it. A timeout and an output cap
apply; a pass is remembered against the exact commit tested (`tree_hash`), so `eng_open_pr` can require that HEAD
has not moved since.

### `eng_open_pr`: always a human first, then its own gate
Tier T3, so the policy gate always parks the call as an `Approval` - the function body below only ever runs once a
human approves it (the same pattern as `aecom.tools.remediation.propose_correcting_entry`: it checks that an
`Approval` for this exact call is being executed, and refuses with `PermissionError` otherwise - an agent cannot
fake that). Once approved, it refuses to proceed if:

- the diff is over **800 changed lines** (split the change),
- the diff touches a **denied path**,
- the diff **looks like it contains a secret** - `aecom.observability.mask`'s known shapes (env-named
  `*_KEY`/`*_TOKEN`/`*_SECRET`/`*_DSN` values, `Bearer ...`, Anthropic/AgentMail key shapes) plus a few more key/token
  shapes (`aecom.engineer.github_api._SECRET_PATTERNS`: GitHub tokens, AWS access key ids, PEM private key headers,
  `key/secret/token/password: "..."` literals),
- `eng_run_tests` has not already **passed on this exact HEAD**,
- another engineer branch is already open, or
- the **daily PR cap** (`engineer_pr_daily_cap`, default 3, in `ops_settings`) is reached.

It then pushes **only** `engineer/<slug>` to `origin` - `<branch>:<branch>`, never `--force`, never any other
refspec, so nothing else on the remote can be touched - and opens the PR through the GitHub REST API via
`aecom.net_guard` (the same SSRF-safe path every outbound fetch in this app uses). The repo is `ENGINEER_REPO`
(`owner/name`) if set, else parsed from the `origin` remote URL. The PR body always ends with:

> Opened by the Engineer agent. Not deployed. A human must review and merge.

## What it cannot do
There is no tool that merges a PR, deploys, restarts a service, edits the live checkout, or reads `.env`. Those stay
human: review and merge the PR on GitHub, then the existing deploy path (`scripts/update.sh`, run by a human or CI).

## Configuration (secrets, via `aecom.secrets`, never in a prompt)
| Variable | Meaning |
|---|---|
| `ENGINEER_REPO_URL` | Clone source. Unset = `git clone --local` of this checkout. |
| `ENGINEER_REPO` | `owner/name` for the GitHub API. Unset = parsed from the `origin` remote URL. |
| `ENGINEER_WORKSPACE_DIR` | Overrides `<ROOT>/data/engineer_workspace/` (tests point this at `tmp_path`). |
| `GITHUB_TOKEN` | Used both to push (as `x-access-token`, for an `https://github.com/...` origin) and to call the API. |

`ops_settings`: `engineer_pr_daily_cap` (int, default 3), `engineer_open_branch` (the one open branch, or empty),
`engineer_pr_count:<date>`, `engineer_last_test_pass` (the last `eng_run_tests` result, keyed by `tree_hash`).

## Suggested follow-ups (not applied by this change - outside its owned files)
- `aecom.cli`: a human-only `engineer clear-branch --by NAME` command for `aecom.engineer.workspace.clear_open_branch`.
- `aecom.settings_registry`: a `storage="db"` field for `engineer_pr_daily_cap` so it is editable on the Settings page.
- `docs/wiki/Roadmap.md`: mark **E3** done.

See also: [Agents](Agents), [Policy and Approvals](Policy-and-Approvals), [Incident Response](Incident-Response)
(K8: the issue → PR hand-off this agent is the other half of).
