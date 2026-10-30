---
budget_eur_per_day: 4
---
# Engineer policy
Reads are T0. `eng_write_file` is T2 (bounded by its own allow/deny path list, not a numeric limit) and
`eng_run_tests` is T1 - both reserved for the Engineer and denied under the kill switch, in safe mode and while
agents are paused, like any other side-effecting tool. `eng_open_pr` is T3: it always waits for a human, whatever
the diff looks like; its own refusals (diff size, denied paths, a secret-looking string, tests not passed on HEAD,
a second open branch, the daily cap) run only once that approval executes, and are not something a stage or a
budget can raise. Not chat-eligible and not dispatched by the Foreman's department list - a human starts its work
directly, by design: the roadmap (E3) calls it the highest-risk agent on purpose.
