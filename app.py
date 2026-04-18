"""
app.py — Backlog Autopilot: Streamlit UI

A backlog resolution system that turns a wall of stale issues
into an actionable pipeline for autonomous resolution.

Run with: streamlit run app.py
"""

import altair as alt
import pandas as pd
import streamlit as st
from github_client import fetch_issues, fetch_pull_requests
from scorer import enrich_issues
from executor import execute_issues, refresh_session_statuses
from state import is_dispatched, get_all_sessions, get_session


# ---------------------------------------------------------------------------
# Page configuration
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Backlog Autopilot — FinServ Co.",
    page_icon="📊",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Load live issues from GitHub and enrich them
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)  # Re-fetch every 60 seconds
def load_and_enrich():
    """Fetch live issues from GitHub and run them through the scoring pipeline."""
    raw_issues = fetch_issues()
    return enrich_issues(raw_issues)


@st.cache_data(ttl=60)
def load_pull_requests():
    """Fetch open PRs from the target repo."""
    return fetch_pull_requests(state="open")


enriched_issues = load_and_enrich()

# Separate recommended vs. not recommended
recommended = [i for i in enriched_issues if i["candidate"]]
not_recommended = [i for i in enriched_issues if not i["candidate"]]


# ---------------------------------------------------------------------------
# Session state — tracks approvals
# ---------------------------------------------------------------------------

if "approved_ids" not in st.session_state:
    st.session_state.approved_ids = set()


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.image("finserv_logo.svg", width=280)
st.title("Backlog Autopilot")

st.divider()


# ---------------------------------------------------------------------------
# Dashboard — KPI cards + resolution chart
# ---------------------------------------------------------------------------

# Count statuses from tracked sessions
all_sessions = get_all_sessions()
status_counts = {}
for s in all_sessions:
    status_counts[s["status"]] = status_counts.get(s["status"], 0) + 1

dispatched_count = len(all_sessions)
prs = load_pull_requests()

col1, col2, col3, col4, col5, col6 = st.columns(6)
col1.metric("Open Issues", len(enriched_issues))
col2.metric("Recommended", len(recommended))
col3.metric("Dispatched", dispatched_count)
col4.metric("Completed", status_counts.get("Completed", 0))
col5.metric("Blocked", status_counts.get("Blocked", 0))
col6.metric("Open PRs", len(prs))

st.markdown("")

# Resolution chart — combines real PR data with mock historical data
# In production, this would be fully real. For now, we show mock history
# with real data for the most recent days.
CHART_DATA = {
    "Past Week": pd.DataFrame({
        "Day": ["Apr 9", "Apr 10", "Apr 11", "Apr 12", "Apr 13", "Apr 14", "Apr 15"],
        "Devin": [4, 7, 5, 9, 6, 2, len([p for p in prs if "devin" in p.get("head_branch", "").lower()])],
        "Engineers": [2, 3, 1, 2, 4, 0, 0],
    }),
    "Past Month": pd.DataFrame({
        "Day": ["Mar 17", "Mar 19", "Mar 21", "Mar 23", "Mar 25", "Mar 27", "Mar 29",
                "Mar 31", "Apr 2", "Apr 4", "Apr 6", "Apr 8", "Apr 10", "Apr 12", "Apr 15"],
        "Devin": [2, 3, 5, 4, 6, 8, 7, 9, 6, 10, 8, 7, 9, 2, len([p for p in prs if "devin" in p.get("head_branch", "").lower()])],
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
    "Select the ones you'd like to send to Devin."
)

if not recommended:
    st.info("No issues are currently recommended for automation.")
else:
    for issue in recommended:
        already_sent = is_dispatched(issue["id"])

        col_check, col_info = st.columns([0.5, 9.5])

        with col_check:
            if already_sent:
                # Show a disabled-looking checkmark for already-dispatched issues
                st.checkbox(
                    "Sent",
                    key=f"approve_{issue['id']}",
                    value=True,
                    disabled=True,
                    label_visibility="collapsed",
                )
            else:
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
            # Show dispatch status in the expander title
            if already_sent:
                session = get_session(issue["id"])
                tag = f"  ·  {session['status']}"
            else:
                tag = ""

            with st.expander(f"#{issue['id']} — {issue['title']}{tag}"):
                st.markdown(f"**Summary:** {issue['summary']}")

                tag_col1, tag_col2, tag_col3, tag_col4 = st.columns(4)
                tag_col1.markdown(f"**Type:** `{issue['issue_type']}`")
                tag_col2.markdown(f"**Complexity:** `{issue['complexity']}`")
                tag_col3.markdown(f"**Scope:** `{issue['scope']}`")
                tag_col4.markdown(f"**Risk:** `{issue['risk']}`")

                st.markdown(f"**Why recommended:** {issue['candidate_reason']}")
                st.markdown(f"**Labels:** {', '.join(issue['labels'])}")
                st.caption(f"Age: {issue['age_days']} days | Comments: {issue['comments_count']}")

                if already_sent and session.get("session_url"):
                    st.markdown(f"[Open Devin Session]({session['session_url']})")

    # Action buttons
    st.markdown("")

    # Only count not-yet-dispatched approvals
    new_approved = [i for i in enriched_issues if i["id"] in st.session_state.approved_ids and not is_dispatched(i["id"])]
    not_yet_dispatched = [i for i in recommended if not is_dispatched(i["id"])]

    btn_left, btn_spacer, btn_right = st.columns([1.2, 7.2, 1.6])

    with btn_left:
        if not_yet_dispatched:
            if st.button("Select All"):
                for issue in not_yet_dispatched:
                    st.session_state.approved_ids.add(issue["id"])
                st.rerun()
        else:
            st.caption("All dispatched")

    with btn_right:
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
        if st.button("Run Approved Issues", disabled=len(new_approved) == 0):
            with st.spinner("Dispatching to Devin..."):
                execute_issues(new_approved)
            st.rerun()


st.divider()


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
# Section: Execution Pipeline
# ---------------------------------------------------------------------------

all_sessions = get_all_sessions()
if all_sessions:
    st.divider()
    st.header("Execution Pipeline")

    # Refresh button to poll Devin for latest status
    refresh_col, spacer_col = st.columns([1.5, 8.5])
    with refresh_col:
        if st.button("Refresh Status"):
            with st.spinner("Polling Devin..."):
                all_sessions = refresh_session_statuses()
            st.rerun()

    # Load PRs to match against sessions
    open_prs = load_pull_requests()

    for session in all_sessions:
        status = session["status"]
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

        issue_id = session["issue_id"]

        with st.expander(f"{status_icon} Issue #{issue_id}  |  **{status}**"):
            st.markdown(f"**Outcome:** {session['outcome_summary']}")

            if session.get("session_url"):
                st.markdown(f"[Open Devin Session]({session['session_url']})")

            # Show linked PRs
            matching_prs = [p for p in open_prs if str(issue_id) in p.get("title", "")]
            if matching_prs:
                st.markdown("**Pull Requests:**")
                for pr in matching_prs:
                    st.markdown(f"- [{pr['title']}]({pr['url']}) — `{pr['state']}`")


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Backlog Autopilot — Built for FinServ Co. | "
    "Live data from GitHub · Execution powered by Devin"
)
