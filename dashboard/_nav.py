"""Navigation for the dashboard: sections, icons and which page files exist.

Home.py (the entrypoint that `aecom dashboard` and the systemd unit run) is only a router: page config, the login gate,
then ``build_navigation().run()``. Pages not built yet are skipped (the app works at every step).
Material icon names: https://fonts.google.com/icons
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import streamlit as st

HERE = Path(__file__).resolve().parent
PAGES_DIR = HERE / "app_pages"


class PageDef(NamedTuple):
    section: str
    file: str
    title: str
    icon: str
    url_path: str
    hidden: bool = False


PAGE_DEFS: list[PageDef] = [
    PageDef("Operate", "overview.py", "Overview", "dashboard", ""),
    PageDef("Operate", "chat.py", "Chat", "forum", "chat"),
    PageDef("Operate", "approvals.py", "Approvals", "fact_check", "approvals"),
    PageDef("Insight", "business.py", "Business", "insights", "business"),
    PageDef("Insight", "agents.py", "Agents", "smart_toy", "agents"),
    PageDef("Insight", "costs.py", "Costs", "payments", "costs"),
    PageDef("Insight", "reports.py", "Reports", "description", "reports"),
    PageDef("Govern", "trust.py", "Trust", "verified_user", "trust"),
    PageDef("Govern", "health.py", "Health", "monitor_heart", "health"),
    PageDef("Govern", "operations.py", "Operations", "build", "operations"),
    PageDef("Govern", "accounting.py", "Accounting", "account_balance", "accounting"),
    PageDef("Govern", "settings.py", "Settings", "settings", "settings"),
    PageDef("Govern", "run_detail.py", "Run detail", "search", "run", hidden=True),  # reached by link (?run_id=)
]


def available(defs: list[PageDef] = PAGE_DEFS, pages_dir: Path = PAGES_DIR) -> list[PageDef]:
    return [d for d in defs if (pages_dir / d.file).exists()]


def sections(defs: list[PageDef]) -> dict[str, list[PageDef]]:
    out: dict[str, list[PageDef]] = {}
    for d in defs:
        out.setdefault(d.section, []).append(d)
    return out


def _unread() -> int:
    try:
        from aecom import conversations

        return conversations.unread_total()
    except Exception:  # noqa: BLE001 - a badge must never break navigation
        return 0


def build_navigation() -> st.navigation:
    """Register the pages and return the current one (call ``.run()`` on it). Call after ``require_login()``."""
    from _common import refresh_settings

    refresh_settings()
    unread = _unread()
    nav: dict[str, list] = {}
    for section, defs in sections(available()).items():
        pages = []
        for d in defs:
            title = f"{d.title} ({unread})" if d.file == "chat.py" and unread else d.title
            pages.append(st.Page(str(PAGES_DIR / d.file), title=title, icon=f":material/{d.icon}:",
                                 url_path=d.url_path or None, default=d.url_path == "",
                                 visibility="hidden" if d.hidden else "visible"))
        nav[section] = pages
    with st.sidebar:
        st.text_input("Your name", key="reviewer_name", placeholder="recorded in the audit log",
                      help="Used for approvals, switches and settings changes.")
    return st.navigation(nav)
