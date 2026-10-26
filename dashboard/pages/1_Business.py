import pandas as pd
import streamlit as st

from _common import colors, require_login, sql
from aecom import ledger
from aecom.tools.catalog import get_kpis

st.set_page_config(page_title="Business", page_icon="📊", layout="wide")
require_login()
st.title("📊 Business")

days = st.segmented_control("Range", [7, 30, 90], default=30, format_func=lambda d: f"{d} days") or 30

kpis, pnl = get_kpis(), ledger.pnl()
c = st.columns(6)
c[0].metric("Net revenue", f"€{pnl['net_revenue']:,.0f}")
c[1].metric("Gross margin", f"{pnl['gross_margin_pct']}%")
c[2].metric("Net profit", f"€{pnl['net_profit']:,.0f}")
c[3].metric("LTV", f"€{kpis['ltv_eur']}")
c[4].metric("ROAS", kpis["roas"] if kpis["roas"] is not None else "—")
c[5].metric("AI cost / order", f"€{kpis['ai_cost_per_order_eur']}" if kpis["ai_cost_per_order_eur"] is not None else "—")

orders = sql("""SELECT date(created_at) AS day, SUM(amount_eur - vat_eur) AS net_revenue, COUNT(*) AS orders
               FROM commerce_orders WHERE status IN ('paid','delivered') GROUP BY day ORDER BY day""")
if not orders.empty:
    orders["day"] = pd.to_datetime(orders["day"])
    orders = orders[orders["day"] >= orders["day"].max() - pd.Timedelta(days=days)]

left, right = st.columns(2)
with left:
    st.subheader("Net revenue per day (€)")
    if orders.empty:
        st.info("No orders yet. Run `aecom init --demo` or `aecom simulate --days 7`.")
    else:
        st.bar_chart(orders, x="day", y="net_revenue", color=colors(1)[0], height=260)
with right:
    st.subheader("Orders per day")
    if not orders.empty:
        st.line_chart(orders, x="day", y="orders", color=colors(1)[0], height=260)

st.subheader("Spend per day by type (€)")
spend = sql("""SELECT date(created_at) AS day, account, SUM(debit) AS eur FROM ledger_entries
              WHERE account IN ('marketing_expense','cogs','ai_expense') GROUP BY day, account ORDER BY day""")
if spend.empty:
    st.caption("No spend booked yet.")
else:
    wide = spend.pivot_table(index="day", columns="account", values="eur", fill_value=0).reset_index()
    cols = [x for x in ["marketing_expense", "cogs", "ai_expense"] if x in wide.columns]
    st.bar_chart(wide, x="day", y=cols, color=colors(len(cols)), height=260)

a, b = st.columns(2)
with a:
    st.subheader("P&L")
    st.dataframe(pd.DataFrame(list(pnl.items()), columns=["line", "EUR"]), hide_index=True, width="stretch")
with b:
    st.subheader("OKRs")
    okrs = sql("SELECT pillar, key_result, metric, target FROM company_okrs")
    okrs["current"] = okrs["metric"].map(lambda m: kpis.get(m))
    okrs["progress"] = [min(1.0, (cur or 0) / t) if t and m != "ai_cost_per_order_eur" else
                        (1.0 if cur is not None and cur <= t else 0.0) for cur, t, m in zip(okrs["current"], okrs["target"], okrs["metric"])]
    st.dataframe(okrs, hide_index=True, width="stretch",
                 column_config={"progress": st.column_config.ProgressColumn("progress", min_value=0, max_value=1)})

st.subheader("Campaigns")
camp = sql("""SELECT name, channel, status, budget_eur, ROUND(spent_eur,2) AS spent_eur, clicks, conversions,
                     ROUND(revenue_eur,2) AS revenue_eur,
                     CASE WHEN spent_eur > 0 THEN ROUND(revenue_eur / spent_eur, 2) END AS roas
              FROM company_campaigns ORDER BY created_at DESC""")
st.dataframe(camp, hide_index=True, width="stretch")

st.subheader("Catalog & inventory")
st.dataframe(sql("""SELECT p.sku, p.title, p.kind, p.status, p.price_eur, p.unit_cost_eur,
                           SUM(CASE WHEN k.status='available' THEN 1 ELSE 0 END) AS keys_available
                    FROM commerce_products p LEFT JOIN commerce_license_keys k ON k.product_id = p.id
                    GROUP BY p.id, p.sku, p.title, p.kind, p.status, p.price_eur, p.unit_cost_eur"""),
             hide_index=True, width="stretch")
st.subheader("Vendors & platforms")
st.dataframe(sql("SELECT name, category, status, monthly_cost_eur, credential_env FROM company_vendors"), hide_index=True,
             width="stretch")
