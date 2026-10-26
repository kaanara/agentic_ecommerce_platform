"""`aecom` command line.

    aecom init [--demo]                    create tables, registry, OKRs (+ demo catalog & 30 days of history)
    aecom scenario <name>|all [--offline]  run end-to-end scenarios (offline = scripted agents, no API key)
    aecom run "<directive>"                one company run (CEO → Foreman → departments → Judge → CEO)
    aecom select <category> "<need>" [-c Shopify -c Paddle ...]
    aecom approvals [list|approve|reject] [id] [--by NAME] [--note TEXT]
    aecom simulate --days N                market simulator
    aecom kill-switch on|off
    aecom drain | scheduler [--demo] | api | dashboard | mcp <domain>
    aecom events dead | retry [ID ...]     dead-lettered events; retry some or all after fixing the cause
    aecom agentmail keys|create-key NAME|inboxes   (admin, uses AGENTMAIL_API_KEY)
    aecom agentmail poll                   fetch unread inbound mail → Support (same as the scheduler job)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from typing import Any

from aecom.config import ROOT


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="aecom", description="Agentic e-commerce company (PoC)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.add_argument("--demo", action="store_true", help="seed demo catalog and 30 days of simulated history")

    s = sub.add_parser("scenario")
    s.add_argument("name")
    s.add_argument("--offline", action="store_true", help="scripted agents (no API key, deterministic)")

    s = sub.add_parser("run")
    s.add_argument("directive")

    s = sub.add_parser("select")
    s.add_argument("category")
    s.add_argument("need")
    s.add_argument("-c", "--candidate", action="append", default=[])

    s = sub.add_parser("approvals")
    s.add_argument("action", nargs="?", default="list", choices=["list", "approve", "reject"])
    s.add_argument("id", nargs="?")
    s.add_argument("--by", default="cli-user")
    s.add_argument("--note", default="")

    s = sub.add_parser("simulate")
    s.add_argument("--days", type=int, default=1)
    s.add_argument("--support-emails", action="store_true")

    s = sub.add_parser("kill-switch")
    s.add_argument("state", choices=["on", "off"])

    sub.add_parser("drain")
    s = sub.add_parser("events")
    s.add_argument("action", choices=["dead", "retry"])
    s.add_argument("ids", nargs="*", type=int)
    s = sub.add_parser("scheduler")
    s.add_argument("--demo", action="store_true")
    s = sub.add_parser("api")
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("dashboard")
    s = sub.add_parser("mcp")
    s.add_argument("domain", choices=["store", "marketing", "finance", "registry", "all"])
    s = sub.add_parser("agentmail")
    s.add_argument("action", choices=["keys", "create-key", "inboxes", "poll"])
    s.add_argument("name", nargs="?")

    a = p.parse_args(argv)

    if a.cmd == "mcp":  # stdout is the MCP channel — print nothing else
        from aecom.mcp_servers.server import serve

        return serve(a.domain)
    if a.cmd == "api":
        import uvicorn

        return uvicorn.run("aecom.api.app:app", host="127.0.0.1", port=a.port)
    if a.cmd == "dashboard":
        sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(ROOT / "dashboard" / "Home.py")]))
    if a.cmd == "scheduler":
        from aecom.scheduler import main as sched_main

        return sched_main(demo=a.demo)

    import aecom.handlers  # noqa: F401
    from aecom.bootstrap import init, seed_demo

    init()

    if a.cmd == "init":
        if a.demo:
            from aecom.simulator import simulate_day

            seed_demo()
            for d in range(-30, 0):
                simulate_day(d, seed=d)
        print("initialised" + (" with demo data" if a.demo else ""))
    elif a.cmd == "scenario":
        from aecom.scenarios import SCENARIOS, run_scenario

        names = list(SCENARIOS) if a.name == "all" else [a.name]
        for n in names:
            if n not in SCENARIOS:
                sys.exit(f"unknown scenario {n}; choose from: {', '.join(SCENARIOS)}")
            r = asyncio.run(run_scenario(n, offline=a.offline))
            print(f"\n=== {n}: {SCENARIOS[n].title}")
            res = r["result"]
            if isinstance(res, dict) and "summary" in res:
                res = {"summary": res.get("summary"), "verdict": res.get("verdict")}
            _print({"result": res, "pending_approvals": r["pending_approvals"]})
    elif a.cmd == "run":
        from aecom.graph.supervisor import run_company

        st = asyncio.run(run_company(a.directive))
        _print({"run_id": st["run_id"], "plan": st.get("plan"), "verdict": st.get("verdict"), "summary": st.get("summary"),
                "pending_approvals": sorted(set(st.get("escalations", [])))})
    elif a.cmd == "select":
        from aecom.graph.selection import run_selection

        st = asyncio.run(run_selection(a.category, a.need, a.candidate))
        _print({"decision": st.get("decision"), "scorecard": st.get("scorecard")})
    elif a.cmd == "approvals":
        from aecom import approvals
        from aecom.bus.events import drain

        if a.action == "list":
            _print(approvals.list_approvals("pending"))
        elif not a.id:
            sys.exit("approval id required")
        elif a.action == "approve":
            _print(approvals.approve(a.id, a.by, a.note))
            drain()
        else:
            _print(approvals.reject(a.id, a.by, a.note))
    elif a.cmd == "simulate":
        from aecom.simulator import simulate_day

        for d in range(a.days):
            _print(simulate_day(-(a.days - 1 - d), support_emails=a.support_emails))
    elif a.cmd == "kill-switch":
        from aecom.company_settings import set_setting

        set_setting("kill_switch", a.state)
        print(f"kill switch {a.state}")
    elif a.cmd == "drain":
        from aecom.bus.events import drain

        _print_drain(drain())
    elif a.cmd == "events":
        from aecom.bus.events import dead_letters, drain, retry

        if a.action == "dead":
            _print(dead_letters())
        else:
            print(f"re-queued {retry(a.ids or None)} dead-lettered event(s)")
            _print_drain(drain())
    elif a.cmd == "agentmail":
        from aecom.integrations.agentmail import AgentMail

        mail = AgentMail()
        if not mail.key:
            sys.exit("AGENTMAIL_API_KEY not set (.env)")
        if a.action == "keys":
            _print(mail.list_api_keys())
        elif a.action == "inboxes":
            _print(mail.list_inboxes())
        elif a.action == "poll":
            from aecom.bootstrap import init
            from aecom.bus.events import drain
            from aecom.integrations.agentmail import poll_inbox

            init()
            print(f"{poll_inbox()} new inbound email(s)")
            _print_drain(drain())
        else:
            _print(mail.create_api_key(a.name or "aecom-poc"))


def _print_drain(attempted: int) -> None:
    from aecom.bus.events import status

    st = status()
    print(f"attempted {attempted} event(s); waiting for retry: {st['waiting_for_retry']}, "
          f"dead letter: {st['dead_letter']} (see `aecom events dead`)")


if __name__ == "__main__":
    main()
