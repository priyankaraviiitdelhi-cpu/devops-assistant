"""
ui_cost.py - the "Cost optimization" page. Read-only: nothing in AWS is changed.
"""
import pandas as pd
import streamlit as st

import cost_agent


def render():
    st.subheader("Cost optimization")
    st.write("Scans your AWS account for waste and estimates monthly savings. "
             "**Read-only: nothing is changed or deleted.**")

    demo = st.toggle("Demo mode (sample data)", value=True,
                     help="Turn off to scan your real AWS account in ap-south-1.",
                     on_change=lambda: (st.session_state.pop("cost", None),
                                        st.session_state.pop("cost_report", None)))
    if st.button("Run cost scan", type="primary"):
        with st.spinner("Scanning..." if demo else "Scanning your AWS account (read-only)..."):
            st.session_state.cost = cost_agent.demo_scan() if demo else cost_agent.scan()
            st.session_state.pop("cost_report", None)

    result = st.session_state.get("cost")
    if not result:
        return

    findings = result["findings"]
    total = sum(f["monthly_savings"] for f in findings)
    label = "DEMO DATA" if result["mode"] == "demo" else f"Live scan of {result['region']}"
    st.caption(f"{label} - {result['scanned_at']}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Est. monthly savings", f"${total:,.2f}")
    c2.metric("Findings", len(findings))
    c3.metric("Spend, last 30 days", f"${sum(result['spend'].values()):,.2f}")

    for err in result["errors"]:
        st.warning(err)

    if result["spend"]:
        st.markdown("#### Spend by service (last 30 days)")
        spend_df = pd.DataFrame(list(result["spend"].items()), columns=["Service", "USD"]).set_index("Service")
        st.bar_chart(spend_df, horizontal=True)

    st.markdown("#### Findings")
    if findings:
        df = pd.DataFrame(findings).sort_values("monthly_savings", ascending=False)
        st.dataframe(df, hide_index=True, column_config={
            "category": "Category", "resource": "Resource", "detail": "Detail", "action": "Suggested action",
            "monthly_savings": st.column_config.NumberColumn("Est. savings / month", format="$%.2f"),
        })
    else:
        st.success("No waste found. Your account looks clean.")

    st.markdown("#### AI report")
    if st.button("Generate AI report"):
        with st.spinner("Writing the report..."):
            try:
                st.session_state.cost_report = cost_agent.summarize(result)
            except Exception as e:
                st.error(f"Something went wrong: {e}")
    if "cost_report" in st.session_state:
        st.markdown(st.session_state.cost_report)
        st.download_button("Download report (.md)", st.session_state.cost_report,
                           file_name="aws_cost_report.md")