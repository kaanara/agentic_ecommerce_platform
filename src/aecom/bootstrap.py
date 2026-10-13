"""Database initialisation and demo seed data."""

from __future__ import annotations

from sqlalchemy import select

from aecom.company_settings import get_setting, set_setting
from aecom.db import Okr, RegistryTool, Vendor, init_db, session
from aecom.policy.gate import load_policy
from aecom.registry.tools import seed_registry

DEFAULT_OKRS = [
    ("financial", "Profitable growth of the e-product business", "Reach €5,000 net revenue this quarter", "net_revenue_eur", 5000),
    ("financial", "Profitable growth of the e-product business", "Keep gross margin at or above 70%", "gross_margin_pct", 70),
    ("customer", "Customers come back and trust us", "Repeat-purchase rate of 15% or more", "repeat_rate_pct", 15),
    ("customer", "Customers come back and trust us", "Average LTV of €60 or more", "ltv_eur", 60),
    ("operational", "Lean, automated, compliant operations", "AI cost per order of €0.50 or less", "ai_cost_per_order_eur", 0.5),
    ("operational", "Lean, automated, compliant operations", "Paid campaigns ROAS of 2.5 or more", "roas", 2.5),
]

DEMO_PRODUCTS = [
    # sku, title, kind, price, unit cost, vendor, keys
    ("TPL-NOTION-FREELANCE-V1", "Notion Freelancer OS — template bundle", "download", 29.00, 0.0, None, 0),
    ("LIC-FOCUSFLOW-PRO-V1", "FocusFlow Pro — lifetime licence", "license", 49.00, 9.00, "KeyForge", 40),
    ("CRS-AI-AUTOMATION-V1", "AI Automation for Small Shops — video course", "course", 79.00, 0.0, None, 0),
]


def init(with_registry_examples: bool = True) -> None:
    init_db()
    seed_registry()
    for key, value in load_policy()["company_settings_defaults"].items():
        if get_setting(key) is None:
            set_setting(key, str(value))
    if get_setting("kill_switch") is None:
        set_setting("kill_switch", "off")
    with session() as s:
        if not s.scalars(select(Okr)).first():
            for pillar, objective, kr, metric, target in DEFAULT_OKRS:
                s.add(Okr(pillar=pillar, objective=objective, key_result=kr, metric=metric, target=target))
        if with_registry_examples and s.get(RegistryTool, "aecom-store") is None:
            # The company's own store MCP, approved for nobody by default — external use is read-only anyway.
            s.add(RegistryTool(name="aecom-store", kind="mcp_server", status="approved", side_effect=True,
                               description="Company store MCP server (catalog, inventory, orders)",
                               config={"transport": "stdio", "command": "uv", "args": ["run", "aecom", "mcp", "store"],
                                       "read_only_tools": ["list_products", "get_inventory", "list_orders", "get_order"]},
                               approved_by="system"))


def seed_demo() -> None:
    from aecom.store.adapter import get_store
    from aecom.tools.catalog import CATALOG

    store = get_store()
    with session() as s:
        if not s.scalars(select(Vendor).where(Vendor.name == "KeyForge")).first():
            s.add(Vendor(name="KeyForge", category="asset_supplier", monthly_cost_eur=29, status="active",
                         notes="Wholesale licence-key supplier (mock feed)"))
    for sku, title, kind, price, cost, vendor, keys in DEMO_PRODUCTS:
        if store.get_product(sku) is None:
            store.create_product(sku, title, price, kind, f"{title}.", cost, vendor)
            if keys:
                CATALOG["add_license_keys"].fn(sku=sku, count=keys)
            store.set_status(sku, "active")
