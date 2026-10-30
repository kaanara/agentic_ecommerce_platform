import pandas as pd
import streamlit as st

from _common import empty_state, page_header, require_login
from aecom import reports

require_login()
page_header("Reports", "description",
            "Immutable snapshots: code computes the numbers, agents only write the commentary. A correction is a "
            "new version, never an edit.")

everything = reports.list_reports()
kinds = sorted({r["kind"] for r in everything})
if not kinds:
    empty_state("description", "No reports yet",
                "The scheduler stores the daily flash, the weekly review (Monday) and the governance report (1st "
                "of the month).")
    st.stop()

kind = st.segmented_control("Kind", kinds, default=kinds[0], format_func=reports.report_title) or kinds[0]
rows = [r for r in everything if r["kind"] == kind]

tab_report, tab_versions = st.tabs(["Report", "All versions"])

with tab_versions:
    st.dataframe(pd.DataFrame([{"period_start": r["period_start"], "period_end": r["period_end"],
                                "version": r["version"], "created_by": r["created_by"], "created_at": r["created_at"],
                                "sha256": r["sha256"][:12], "id": r["id"]} for r in rows]),
                 hide_index=True, width="stretch")

with tab_report:
    labels = {r["id"]: f"{(r['period_start'] or '')[:10]} to {(r['period_end'] or '')[:10]} · v{r['version']} · "
                       f"{r['created_by']}" for r in rows}
    chosen = st.selectbox("Version", list(labels), format_func=labels.get)
    report = next(r for r in rows if r["id"] == chosen)

    with st.container(border=True):
        st.markdown(f"### {reports.report_title(report['kind'])} v{report['version']}")
        st.caption(f"{report['id']} · created {report['created_at']} by {report['created_by']} · "
                   f"sha256 {report['sha256']}")
        st.text(report["text"])

    stem = f"{report['kind']}_{(report['period_start'] or '')[:10]}_v{report['version']}"
    downloads = st.container(horizontal=True)
    downloads.download_button("Markdown", reports.to_markdown(report), file_name=f"{stem}.md", mime="text/markdown")
    downloads.download_button("CSV", reports.to_csv(report), file_name=f"{stem}.csv", mime="text/csv")

    st.subheader("Data")
    st.dataframe(pd.DataFrame(reports.flatten(report["data"]), columns=["key", "value"]).astype(str),
                 hide_index=True, width="stretch")
    with st.expander("Raw JSON"):
        st.json(report["data"])
