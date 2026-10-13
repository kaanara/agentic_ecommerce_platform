# aecom wiki

**aecom** is a proof of concept for a company that runs an e-products business (licences, templates, courses and other digital goods) with a team of AI agents. It is built on **LangGraph** and **Claude**, governed by a policy gate, and gives humans the final say on anything risky.

## Start here
| If you want to… | Read |
|---|---|
| Run it in 2 minutes | [Getting Started](Getting-Started) |
| Understand how the pieces fit | [Architecture](Architecture) |
| Meet the agents / add one | [Agents](Agents) |
| Know what agents may or may not do | [Policy and Approvals](Policy-and-Approvals) |
| See the end-to-end business flows | [Workflows and Scenarios](Workflows-and-Scenarios) |
| Add a tool, MCP server or skill | [Tools, MCP and Skills](Tools-MCP-and-Skills) |
| Query the database | [Data Model](Data-Model) |
| Use the dashboards | [Dashboards](Dashboards) |
| Connect email, a real store, ad platforms | [Integrations](Integrations) |
| Configure env vars | [Configuration](Configuration) |
| Fix something | [Operations and Troubleshooting](Operations-and-Troubleshooting) |
| See what's next | [Roadmap](Roadmap) |

## The company in one picture
```
Human board ── Approvals dashboard · API · CLI · kill switch
     ▲ escalations                    │ approve = execute exactly as parked
CEO (Opus) → Foreman (Sonnet) → Store Ops · Marketing · Support · Supply (Haiku), Finance · Researcher (Sonnet)
     ▲                                                                   │
     └──────── CEO close ◀── Judge (Sonnet, may veto once) ◀─────────────┘
                     every tool call → Policy gate (T0–T4)
```

## Principles
1. **Agents decide, humans provision.** Agents never create accounts, accept terms, handle passwords or pay.
2. **One gate for everything.** Built-in tools, MCP tools, event reflexes and external MCP clients all pass through the same policy.
3. **Approve the exact action.** Escalations are stored as a tool call plus its arguments. Approving runs that exact call; it is never regenerated.
4. **Self-management is governed.** Agents can propose and sandbox tools. Only humans can promote or grant them.
5. **Cost is a KPI.** Every LLM call is booked to the ledger, budgeted per agent, and tracked as "AI cost per order".
