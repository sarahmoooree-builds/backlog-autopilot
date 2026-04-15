"""
app.py — Backlog Autopilot: Streamlit UI

A backlog resolution system that turns a wall of stale issues
into an actionable pipeline for autonomous resolution.

Run with: streamlit run app.py
"""

import json
import streamlit as st
from scorer import enrich_issues
from mock_executor import execute_issues


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

# Display the FinServ Co. logo
st.image("finserv_logo.svg", width=280)

st.title("Backlog Autopilot")
st.markdown(
    "**FinServ Co. Engineering** — Turn 300+ stale GitHub issues into an actionable pipeline. "
    "Issues are enriched, scored, and recommended for autonomous resolution — "
    "you approve, the system executes."
)

st.divider()


# ---------------------------------------------------------------------------
# KPI summary cards
# ---------------------------------------------------------------------------

# Count statuses from execution results
status_counts = {}
for r in st.session_state.execution_results:
    status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

col1, col2, col3, col4, col5 = st.columns(5)

col1.metric("Total Issues", len(enriched_issues))
col2.metric("Recommended", len(recommended))
col3.metric("Approved", len(st.session_state.approved_ids))
col4.metric("Completed", status_counts.get("Completed", 0))
col5.metric("Blocked", status_counts.get("Blocked", 0))

st.divider()


# ---------------------------------------------------------------------------
# Section: Recommended issues (approve for execution)
# ---------------------------------------------------------------------------

st.header("Recommended for Automation")
st.caption(
    "These issues scored well on clarity, scope, and complexity. "
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

st.divider()


# ---------------------------------------------------------------------------
# Section: Run execution
# ---------------------------------------------------------------------------

st.header("Execute Approved Issues")

approved_issues = [i for i in enriched_issues if i["id"] in st.session_state.approved_ids]

if approved_issues:
    st.write(f"**{len(approved_issues)}** issue(s) approved and ready to run.")

    if st.button("Run Approved Issues", type="primary"):
        with st.spinner("Running execution pipeline..."):
            results = execute_issues(approved_issues)
            st.session_state.execution_results = results
        st.rerun()
else:
    st.info("Select recommended issues above, then run them here.")


# ---------------------------------------------------------------------------
# Section: Execution results
# ---------------------------------------------------------------------------

if st.session_state.execution_results:
    st.divider()
    st.header("Execution Results")

    for result in st.session_state.execution_results:
        # Color-code the status
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

st.divider()


# ---------------------------------------------------------------------------
# Section: Human-owned issues (not recommended)
# ---------------------------------------------------------------------------

st.header("Human-Owned Issues")
st.caption(
    "These issues were not recommended for automation. "
    "Each one includes a reason why it remains in the human queue."
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
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Backlog Autopilot MVP — Built for FinServ Co. | "
    "Execution is currently mocked. In production, approved issues are sent to Devin for resolution."
)
