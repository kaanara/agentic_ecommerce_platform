import pandas as pd
import streamlit as st

from _common import mask_df, require_login, sql
from aecom import run_detail
from aecom.observability import mask

st.set_page_config(page_title="Run detail", page_icon="🔎", layout="wide")
require_login()
st.title("🔎 Run detail")
st.caption("Everything recorded for one run: agent runs, tool calls, approvals, audit entries, errors, decisions and "
           "incidents, in time order. Secrets are masked.")

recent = run_detail.recent_run_ids(100)
options = [r["run_id"] for r in recent]
labels = {r["run_id"]: f"{r['started_at']:%Y-%m-%d %H:%M} · {', '.join(r['agents'])} · {r['status']} · {r['run_id']}"
          for r in recent}
wanted = st.query_params.get("run_id")
if wanted and wanted not in options:
    options.insert(0, wanted)
if not options:
    st.info("No agent runs yet. Try `aecom scenario all --offline`.")
    st.stop()
run_id = st.selectbox("Run", options, index=options.index(wanted) if wanted else 0,
                      format_func=lambda r: labels.get(r, r))
if run_id != wanted:
    st.query_params["run_id"] = run_id  # shareable link

s = run_detail.summary(run_id)
c = st.columns(6)
c[0].metric("Status", s["status"])
c[1].metric("Agents", len(s["agents"]))
c[2].metric("Tool calls", s["tool_calls"])
c[3].metric("Approvals", s["approvals"])
c[4].metric("Errors", s["errors"])
c[5].metric("LLM cost", f"€{s['cost_eur']:.4f}")
st.caption(f"Agents: {', '.join(s['agents']) or '—'} · tools: {', '.join(s['tools_used']) or '—'} · "
           f"started {s['started_at'] or '—'} · ended {s['ended_at'] or '—'}")

st.subheader("Timeline")
tl = run_detail.timeline(run_id)
kinds = sorted({e["kind"] for e in tl})
shown = st.multiselect("Kinds", kinds, default=kinds)
st.dataframe(mask_df(pd.DataFrame([e for e in tl if e["kind"] in shown], columns=["ts", "kind", "actor", "summary", "ref"]),
                     ["summary"]),
             hide_index=True, width="stretch")

st.subheader("Approvals")
apr = sql("""SELECT id, created_at, decided_at, status, risk_tier, agent_id, tool_name, decided_by, reason,
                    decision_note, substr(result, 1, 300) AS result
             FROM ops_approvals WHERE run_id = :r ORDER BY created_at""", r=run_id)
if apr.empty:
    st.caption("No approvals in this run.")


def txt(value: object) -> str:
    """Masked text for a dataframe cell; empty for NULL/NaN."""
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else mask(str(value))


for _, a in apr.iterrows():
    with st.expander(f"{a['tool_name']} · {a['status']} · {a['risk_tier']} · {a['id']}"):
        st.markdown(f"**Agent:** {a['agent_id']} · **decided by:** {txt(a['decided_by']) or '—'} "
                    f"at {txt(a['decided_at']) or '—'}")
        st.markdown(f"**Reason:** {txt(a['reason'])}")
        if txt(a["decision_note"]):
            st.markdown(f"**Note:** {txt(a['decision_note'])}")
        if txt(a["result"]):
            st.code(txt(a["result"]))
if (apr["status"] == "pending").any():
    st.markdown("✅ [Decide pending approvals](/Approvals)")

st.subheader("Errors")
err = sql("""SELECT id, ts, component, exc_type, message, traceback FROM ops_errors WHERE run_id = :r ORDER BY id""",
          r=run_id)
if err.empty:
    st.caption("No errors in this run.")
for _, e in err.iterrows():
    with st.expander(f"{e['ts']} · {e['component']} · {e['exc_type']}"):
        st.text(txt(e["message"]))
        st.code(txt(e["traceback"]), language="python")
