"""
app.py — Backlog Autopilot: Streamlit UI

A backlog resolution system that turns a wall of stale issues
into an actionable pipeline for autonomous resolution.

Run with: streamlit run app.py
"""

import json
import altair as alt
import pandas as pd
import streamlit as st
from scorer import enrich_issues
from executor import execute_issues


# ---------------------------------------------------------------------------
# Page configuration
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Backlog Autopilot — FinServ Co.",
    page_icon="📊",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Load and enrich issues (cached so it only runs once per session)
# ---------------------------------------------------------------------------

@st.cache_data
def load_and_enrich():
    """Load raw issues from JSON and run them through the scoring pipeline."""
    with open("issues.json", "r") as f:
        raw_issues = json.load(f)
    return enrich_issues(raw_issues)


enriched_issues = load_and_enrich()

# Separate recommended vs. not recommended
recommended = [i for i in enriched_issues if i["candidate"]]
not_recommended = [i for i in enriched_issues if not i["candidate"]]


# ---------------------------------------------------------------------------
# Session state — tracks approvals and execution results
# ---------------------------------------------------------------------------

if "approved_ids" not in st.session_state:
    st.session_state.approved_ids = set()

if "execution_results" not in st.session_state:
    st.session_state.execution_results = []


# ---------------------------------------------------------------------------
# Header — FinServ Co. branding
# ---------------------------------------------------------------------------

st.image("finserv_logo.svg", width=280)
st.title("Backlog Autopilot")

st.divider()


# ---------------------------------------------------------------------------
# Dashboard — KPI cards + resolution chart
# ---------------------------------------------------------------------------

# KPI cards
status_counts = {}
for r in st.session_state.execution_results:
    status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total Issues", len(enriched_issues))
col2.metric("Recommended", len(recommended))
col3.metric("Approved", len(st.session_state.approved_ids))
col4.metric("Completed", status_counts.get("Completed", 0))
col5.metric("Blocked", status_counts.get("Blocked", 0))

st.markdown("")

# Resolution chart — mock data showing Devin vs. Engineers
CHART_DATA = {
    "Past Week": pd.DataFrame({
        "Day": ["Apr 7", "Apr 8", "Apr 9", "Apr 10", "Apr 11", "Apr 12", "Apr 13"],
        "Devin": [4, 7, 5, 9, 6, 2, 3],
        "Engineers": [2, 3, 1, 2, 4, 0, 0],
    }),
    "Past Month": pd.DataFrame({
        "Day": ["Mar 17", "Mar 19", "Mar 21", "Mar 23", "Mar 25", "Mar 27", "Mar 29",
                "Mar 31", "Apr 2", "Apr 4", "Apr 6", "Apr 8", "Apr 10", "Apr 12", "Apr 13"],
        "Devin": [2, 3, 5, 4, 6, 8, 7, 9, 6, 10, 8, 7, 9, 2, 3],
        "Engineers": [3, 2, 4, 1, 3, 2, 5, 3, 2, 4, 1, 3, 2, 0, 0],
    }),
    "Past Year": pd.DataFrame({
        "Day": ["May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr"],
        "Devin": [0, 0, 0, 0, 0, 12, 28, 45, 62, 78, 95, 36],
        "Engineers": [18, 22, 15, 20, 24, 19, 17, 14, 16, 12, 15, 12],
    }),
}

chart_header_col, chart_toggle_col = st.columns([6, 4])
with chart_header_col:
    st.subheader("Issues Resolved")
with chart_toggle_col:
    time_range = st.radio("Time range", ["Past Week", "Past Month", "Past Year"], horizontal=True, label_visibility="collapsed")

# Melt the data into long format for Altair and preserve label order
chart_df = CHART_DATA[time_range]
day_order = chart_df["Day"].tolist()
chart_melted = chart_df.melt("Day", var_name="Source", value_name="Issues")

chart = (
    alt.Chart(chart_melted)
    .mark_line(point=True, strokeWidth=2.5)
    .encode(
        x=alt.X("Day:N", sort=day_order, axis=alt.Axis(labelAngle=0, title=None)),
        y=alt.Y("Issues:Q", title="Issues Resolved"),
        color=alt.Color(
            "Source:N",
            scale=alt.Scale(domain=["Devin", "Engineers"], range=["#1B7A8E", "#0F4C81"]),
            legend=alt.Legend(title=None, orient="bottom"),
        ),
    )
    .properties(height=300)
)
st.altair_chart(chart, use_container_width=True)

st.divider()


# ---------------------------------------------------------------------------
# Section: Issues for Automation
# ---------------------------------------------------------------------------

st.header("Issues for Automation")
st.caption(
    "These issues are recommended to automate based on scope and complexity. "
    "Select the ones you'd like to send to the execution pipeline."
)

if not recommended:
    st.info("No issues are currently recommended for automation.")
else:
    for issue in recommended:
        col_check, col_info = st.columns([0.5, 9.5])

        with col_check:
            checked = st.checkbox(
                "Approve",
                key=f"approve_{issue['id']}",
                value=issue["id"] in st.session_state.approved_ids,
                label_visibility="collapsed",
            )
            if checked:
                st.session_state.approved_ids.add(issue["id"])
            else:
                st.session_state.approved_ids.discard(issue["id"])

        with col_info:
            with st.expander(f"#{issue['id']} — {issue['title']}"):
                st.markdown(f"**Summary:** {issue['summary']}")

                tag_col1, tag_col2, tag_col3, tag_col4 = st.columns(4)
                tag_col1.markdown(f"**Type:** `{issue['issue_type']}`")
                tag_col2.markdown(f"**Complexity:** `{issue['complexity']}`")
                tag_col3.markdown(f"**Scope:** `{issue['scope']}`")
                tag_col4.markdown(f"**Risk:** `{issue['risk']}`")

                st.markdown(f"**Why recommended:** {issue['candidate_reason']}")
                st.markdown(f"**Labels:** {', '.join(issue['labels'])}")
                st.caption(f"Age: {issue['age_days']} days | Comments: {issue['comments_count']}")

    # Action buttons — Select All on the left, Run Approved on the right
    st.markdown("")
    approved_issues = [i for i in enriched_issues if i["id"] in st.session_state.approved_ids]

    btn_left, btn_spacer, btn_right = st.columns([1.2, 7.2, 1.6])

    with btn_left:
        if st.button("Select All"):
            for issue in recommended:
                st.session_state.approved_ids.add(issue["id"])
            st.rerun()

    with btn_right:
        # Green button via inline CSS
        st.markdown(
            """<style>
            div[data-testid="stColumn"]:last-child button {
                background-color: #28a745;
                color: white;
                border: none;
            }
            div[data-testid="stColumn"]:last-child button:hover {
                background-color: #218838;
                color: white;
                border: none;
            }
            </style>""",
            unsafe_allow_html=True,
        )
        if st.button("Run Approved Issues", disabled=len(approved_issues) == 0):
            with st.spinner("Running execution pipeline..."):
                results = execute_issues(approved_issues)
                st.session_state.execution_results = results
            st.rerun()


# ---------------------------------------------------------------------------
# Section: Issues for Engineers
# ---------------------------------------------------------------------------

st.header("Issues for Engineers")
st.caption(
    "These issues were not recommended for automation. "
    "Each one includes a reason why it stays with the engineering team."
)

if not not_recommended:
    st.info("All issues are currently recommended — nice!")
else:
    for issue in not_recommended:
        with st.expander(f"#{issue['id']} — {issue['title']}"):
            st.markdown(f"**Summary:** {issue['summary']}")

            tag_col1, tag_col2, tag_col3, tag_col4 = st.columns(4)
            tag_col1.markdown(f"**Type:** `{issue['issue_type']}`")
            tag_col2.markdown(f"**Complexity:** `{issue['complexity']}`")
            tag_col3.markdown(f"**Scope:** `{issue['scope']}`")
            tag_col4.markdown(f"**Risk:** `{issue['risk']}`")

            st.markdown(f"**Why not recommended:** {issue['candidate_reason']}")
            st.markdown(f"**Labels:** {', '.join(issue['labels'])}")
            st.caption(f"Age: {issue['age_days']} days | Comments: {issue['comments_count']}")


# ---------------------------------------------------------------------------
# Section: Execution results
# ---------------------------------------------------------------------------

if st.session_state.execution_results:
    st.divider()
    st.header("Execution Results")

    for result in st.session_state.execution_results:
        status = result["status"]
        if status == "Completed":
            status_icon = "✅"
        elif status == "Awaiting Review":
            status_icon = "👀"
        elif status == "In Progress":
            status_icon = "🔄"
        elif status == "Blocked":
            status_icon = "🚫"
        else:
            status_icon = "❓"

        with st.expander(f"{status_icon} #{result['id']} — {result['title']}  |  **{status}**"):
            st.markdown(f"**Status:** {status}")
            st.markdown(f"**Outcome:** {result['outcome_summary']}")
            if result.get("session_url"):
                st.markdown(f"[Open Devin Session]({result['session_url']})")


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Backlog Autopilot MVP — Built for FinServ Co. | "
    "Execution is currently mocked. In production, approved issues are sent to Devin for resolution."
)
