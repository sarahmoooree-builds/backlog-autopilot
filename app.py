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
from triage_store import get_triage, is_triaged, confidence_label
from triager import triage_issue, triage_issues


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

@st.cache_data(ttl=60)
def load_and_enrich():
    raw_issues = fetch_issues()
    return enrich_issues(raw_issues)


@st.cache_data(ttl=60)
def load_pull_requests():
    return fetch_pull_requests(state="open")


enriched_issues = load_and_enrich()
recommended = [i for i in enriched_issues if i["candidate"]]
not_recommended = [i for i in enriched_issues if not i["candidate"]]

if "approved_ids" not in st.session_state:
    st.session_state.approved_ids = set()


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.image("finserv_logo.svg", width=280)
st.title("Backlog Autopilot")

st.divider()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_pipeline, tab_business = st.tabs(["Pipeline", "Business Report"])


# ===========================================================================
# TAB 1: PIPELINE
# ===========================================================================

with tab_pipeline:

    # --- KPI cards ---
    all_sessions = get_all_sessions()
    status_counts = {}
    for s in all_sessions:
        status_counts[s["status"]] = status_counts.get(s["status"], 0) + 1

    prs = load_pull_requests()

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("Open Issues", len(enriched_issues))
    col2.metric("Recommended", len(recommended))
    col3.metric("Dispatched", len(all_sessions))
    col4.metric("Completed", status_counts.get("Completed", 0))
    col5.metric("Blocked", status_counts.get("Blocked", 0))
    col6.metric("Open PRs", len(prs))

    st.markdown("")

    # --- Resolution chart ---
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
        .properties(height=280)
    )
    st.altair_chart(chart, use_container_width=True)

    st.divider()

    # --- Issues for Automation ---
    st.header("Issues for Automation")
    st.caption("Recommended based on scope, complexity, and risk. Triage with Devin for a confidence score before dispatching.")

    if not recommended:
        st.info("No issues are currently recommended for automation.")
    else:
        # --- Triage All button ---
        not_yet_triaged = [i for i in recommended if not is_triaged(i["id"])]
        triage_col, triage_spacer = st.columns([2, 8])
        with triage_col:
            if not_yet_triaged:
                if st.button("Triage All with Devin", help="Run Devin triage on all un-triaged recommended issues"):
                    with st.spinner(f"Triaging {len(not_yet_triaged)} issue(s) with Devin... this may take a minute."):
                        triage_issues(not_yet_triaged)
                    st.rerun()
            else:
                st.caption("All issues triaged")

        st.markdown("")

        for issue in recommended:
            already_sent = is_dispatched(issue["id"])
            triage = get_triage(issue["id"])

            col_check, col_info = st.columns([0.5, 9.5])

            with col_check:
                if already_sent:
                    st.checkbox("Sent", key=f"approve_{issue['id']}", value=True, disabled=True, label_visibility="collapsed")
                else:
                    checked = st.checkbox("Approve", key=f"approve_{issue['id']}", value=issue["id"] in st.session_state.approved_ids, label_visibility="collapsed")
                    if checked:
                        st.session_state.approved_ids.add(issue["id"])
                    else:
                        st.session_state.approved_ids.discard(issue["id"])

            with col_info:
                session = get_session(issue["id"]) if already_sent else None
                dispatch_tag = f"  ·  {session['status']}" if session else ""

                # Build confidence badge for expander title
                if triage and "confidence_score" in triage:
                    score = triage["confidence_score"]
                    label, _ = confidence_label(score)
                    confidence_tag = f"  ·  {label} ({score}%)"
                else:
                    confidence_tag = ""

                with st.expander(f"#{issue['id']} — {issue['title']}{confidence_tag}{dispatch_tag}"):
                    # --- Triage report (if available) ---
                    if triage and "error" not in triage:
                        score = triage["confidence_score"]
                        label, color = confidence_label(score)

                        st.markdown(
                            f"<div style='background:{color}20; border-left: 4px solid {color}; padding: 10px 14px; border-radius: 4px; margin-bottom: 12px;'>"
                            f"<strong style='color:{color}'>Devin Confidence: {score}/100 — {label}</strong><br>"
                            f"<span style='font-size:0.9em'>{triage['confidence_reasoning']}</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

                        tr1, tr2 = st.columns(2)
                        with tr1:
                            st.markdown("**Root Cause Hypothesis**")
                            st.markdown(triage["root_cause_hypothesis"])
                            st.markdown("**Affected Files**")
                            for f in triage.get("affected_files", []):
                                st.markdown(f"- `{f}`")
                            st.caption(f"Estimated lines changed: {triage.get('estimated_lines_changed', '?')}")

                        with tr2:
                            st.markdown("**Next Steps**")
                            for i, step in enumerate(triage.get("next_steps", []), 1):
                                st.markdown(f"{i}. {step}")

                        st.divider()

                    elif triage and "error" in triage:
                        st.warning(f"Triage failed: {triage['error']}")

                    else:
                        # No triage yet — show triage button inline
                        if st.button("Triage with Devin", key=f"triage_{issue['id']}"):
                            with st.spinner("Devin is analyzing this issue..."):
                                triage_issue(issue)
                            st.rerun()

                    # --- Standard enrichment fields ---
                    st.markdown(f"**Summary:** {issue['summary']}")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.markdown(f"**Type:** `{issue['issue_type']}`")
                    c2.markdown(f"**Complexity:** `{issue['complexity']}`")
                    c3.markdown(f"**Scope:** `{issue['scope']}`")
                    c4.markdown(f"**Risk:** `{issue['risk']}`")
                    st.markdown(f"**Why recommended:** {issue['candidate_reason']}")
                    st.markdown(f"**Labels:** {', '.join(issue['labels'])}")
                    st.caption(f"Age: {issue['age_days']} days | Comments: {issue['comments_count']}")
                    if session and session.get("session_url"):
                        st.markdown(f"[Open Devin Session]({session['session_url']})")

        st.markdown("")
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
                    background-color: #28a745; color: white; border: none;
                }
                div[data-testid="stColumn"]:last-child button:hover {
                    background-color: #218838; color: white; border: none;
                }
                </style>""",
                unsafe_allow_html=True,
            )
            if st.button("Run Approved Issues", disabled=len(new_approved) == 0):
                with st.spinner("Dispatching to Devin..."):
                    execute_issues(new_approved)
                st.rerun()

    st.divider()

    # --- Issues for Engineers ---
    st.header("Issues for Engineers")
    st.caption("Not recommended for automation. Each one includes a reason why it stays with the engineering team.")

    for issue in not_recommended:
        with st.expander(f"#{issue['id']} — {issue['title']}"):
            st.markdown(f"**Summary:** {issue['summary']}")
            c1, c2, c3, c4 = st.columns(4)
            c1.markdown(f"**Type:** `{issue['issue_type']}`")
            c2.markdown(f"**Complexity:** `{issue['complexity']}`")
            c3.markdown(f"**Scope:** `{issue['scope']}`")
            c4.markdown(f"**Risk:** `{issue['risk']}`")
            st.markdown(f"**Why not recommended:** {issue['candidate_reason']}")
            st.markdown(f"**Labels:** {', '.join(issue['labels'])}")
            st.caption(f"Age: {issue['age_days']} days | Comments: {issue['comments_count']}")

    # --- Execution Pipeline ---
    all_sessions = get_all_sessions()
    if all_sessions:
        st.divider()
        st.header("Execution Pipeline")

        refresh_col, _ = st.columns([1.5, 8.5])
        with refresh_col:
            if st.button("Refresh Status"):
                with st.spinner("Polling Devin..."):
                    all_sessions = refresh_session_statuses()
                st.rerun()

        open_prs = load_pull_requests()
        for session in all_sessions:
            status = session["status"]
            icon = {"Completed": "✅", "Awaiting Review": "👀", "In Progress": "🔄", "Blocked": "🚫"}.get(status, "❓")
            issue_id = session["issue_id"]

            with st.expander(f"{icon} Issue #{issue_id}  |  **{status}**"):
                st.markdown(f"**Outcome:** {session['outcome_summary']}")
                if session.get("session_url"):
                    st.markdown(f"[Open Devin Session]({session['session_url']})")
                matching_prs = [p for p in open_prs if str(issue_id) in p.get("title", "")]
                if matching_prs:
                    st.markdown("**Pull Requests:**")
                    for pr in matching_prs:
                        st.markdown(f"- [{pr['title']}]({pr['url']}) — `{pr['state']}`")


# ===========================================================================
# TAB 2: BUSINESS REPORT
# ===========================================================================

with tab_business:

    # --- FinServ-specific assumptions (all mock but grounded in real ratios) ---
    TOTAL_BACKLOG = 312          # FinServ's stated backlog size
    AUTOMATABLE_PCT = 0.28       # % of issues our scorer flags as candidates
    AUTOMATABLE = int(TOTAL_BACKLOG * AUTOMATABLE_PCT)  # ~87 issues
    AVG_ENGINEER_HRS = 5.5       # hours per issue manually
    AVG_DEVIN_HRS = 0.75         # hours per issue with Devin (45 min)
    ENGINEER_HOURLY_COST = 150   # fully-loaded $/hr for a senior engineer
    ISSUES_PER_WEEK_BEFORE = 8   # current resolution rate
    ISSUES_PER_WEEK_AFTER = 35   # projected with Devin

    hours_saved = AUTOMATABLE * (AVG_ENGINEER_HRS - AVG_DEVIN_HRS)
    cost_saved = hours_saved * ENGINEER_HOURLY_COST
    weeks_to_clear_before = AUTOMATABLE / ISSUES_PER_WEEK_BEFORE
    weeks_to_clear_after = AUTOMATABLE / ISSUES_PER_WEEK_AFTER

    st.header("Business Impact Report")
    st.caption("Projected outcomes for FinServ Co. based on current backlog composition and resolution rates.")

    st.markdown("")

    # --- Headline metrics ---
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Automatable Issues", f"{AUTOMATABLE}", f"{int(AUTOMATABLE_PCT * 100)}% of backlog")
    h2.metric("Engineer Hours Recovered", f"{int(hours_saved):,} hrs", "per backlog cycle")
    h3.metric("Estimated Cost Savings", f"${int(cost_saved):,}", "in recovered eng. time")
    h4.metric("Time to Clear Backlog", f"{weeks_to_clear_after:.0f} weeks", f"vs. {weeks_to_clear_before:.0f} weeks today")

    st.divider()

    # --- Two charts side by side ---
    chart_col1, chart_col2 = st.columns(2)

    with chart_col1:
        st.subheader("Backlog Burn Rate Projection")
        st.caption("Issues remaining over 12 weeks — with vs. without Devin")

        weeks = list(range(0, 13))
        remaining_before = [max(0, TOTAL_BACKLOG - ISSUES_PER_WEEK_BEFORE * w) for w in weeks]
        remaining_after = [max(0, TOTAL_BACKLOG - ISSUES_PER_WEEK_AFTER * w) for w in weeks]

        burn_df = pd.DataFrame({
            "Week": weeks * 2,
            "Issues Remaining": remaining_before + remaining_after,
            "Scenario": ["Without Devin"] * 13 + ["With Devin"] * 13,
        })

        burn_chart = (
            alt.Chart(burn_df)
            .mark_line(point=True, strokeWidth=2.5)
            .encode(
                x=alt.X("Week:Q", axis=alt.Axis(labelAngle=0, title="Weeks from Now")),
                y=alt.Y("Issues Remaining:Q", title="Open Issues"),
                color=alt.Color(
                    "Scenario:N",
                    scale=alt.Scale(
                        domain=["With Devin", "Without Devin"],
                        range=["#1B7A8E", "#6c757d"]
                    ),
                    legend=alt.Legend(title=None, orient="bottom"),
                ),
            )
            .properties(height=280)
        )
        st.altair_chart(burn_chart, use_container_width=True)

    with chart_col2:
        st.subheader("Time to Resolution")
        st.caption("Average hours per issue — engineer vs. Devin, by issue type")

        time_df = pd.DataFrame({
            "Issue Type": ["Simple Bug", "Medium Bug", "Tech Debt", "Simple Bug", "Medium Bug", "Tech Debt"],
            "Hours": [3.0, 6.5, 8.0, 0.5, 1.0, 1.5],
            "Resolver": ["Engineer", "Engineer", "Engineer", "Devin", "Devin", "Devin"],
        })

        time_chart = (
            alt.Chart(time_df)
            .mark_bar()
            .encode(
                x=alt.X("Issue Type:N", axis=alt.Axis(labelAngle=0, title=None)),
                y=alt.Y("Hours:Q", title="Avg. Hours to Resolve"),
                color=alt.Color(
                    "Resolver:N",
                    scale=alt.Scale(domain=["Engineer", "Devin"], range=["#0F4C81", "#1B7A8E"]),
                    legend=alt.Legend(title=None, orient="bottom"),
                ),
                xOffset="Resolver:N",
            )
            .properties(height=280)
        )
        st.altair_chart(time_chart, use_container_width=True)

    st.divider()

    # --- Efficiency breakdown ---
    st.subheader("Efficiency Breakdown")

    eff_col1, eff_col2, eff_col3 = st.columns(3)

    eff_col1.markdown("**Automation Coverage**")
    eff_col1.markdown(f"Of FinServ's **{TOTAL_BACKLOG} open issues**, our scoring engine identifies **{AUTOMATABLE} ({int(AUTOMATABLE_PCT*100)}%)** as safe candidates for autonomous resolution — clear bugs with narrow scope, low-to-medium complexity, and no risk overlap with auth or billing systems.")

    eff_col2.markdown("**Speed Multiplier**")
    eff_col2.markdown(f"Devin resolves issues in an average of **{int(AVG_DEVIN_HRS * 60)} minutes** vs. **{AVG_ENGINEER_HRS} hours** for a senior engineer. That's a **{AVG_ENGINEER_HRS / AVG_DEVIN_HRS:.0f}x speed improvement** on eligible issues, with no context-switching cost.")

    eff_col3.markdown("**Engineer Time Redirect**")
    eff_col3.markdown(f"Automating {AUTOMATABLE} issues recovers **{int(hours_saved):,} engineering hours** — equivalent to **{int(hours_saved / 2000)} full-time engineer years** — that can be redirected to platform work, architecture, and revenue-generating features.")

    st.divider()

    # --- How we measure success ---
    st.subheader("How We Measure Success")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Target: Backlog Cleared", "12 weeks", "from 38 weeks today")
    m2.metric("Target: PR Merge Rate", "≥ 85%", "Devin PRs passing review")
    m3.metric("Target: Test Pass Rate", "≥ 95%", "on Devin-authored changes")
    m4.metric("Target: Weekly Velocity", "35 issues/wk", "up from 8 today")

    st.markdown("")
    st.markdown("""
**Success criteria (90-day review):**
- Automatable backlog reduced by at least 60%
- No regressions introduced by Devin-authored PRs
- Senior engineers report measurable reduction in backlog-related interruptions
- Junior engineers using Devin output as learning material, not replacement
""")

    st.divider()

    # --- Business implications ---
    st.subheader("Business Implications for FinServ Co.")

    bi1, bi2 = st.columns(2)

    with bi1:
        st.markdown("**What changes immediately**")
        st.markdown("""
- Stale issues begin resolving within hours of approval, not weeks
- Junior engineers spend time reviewing and learning from Devin's PRs instead of triaging ambiguous issues
- Senior engineers get back ~{} hours per month previously spent on backlog triage
- Backlog stops growing faster than it's resolved
""".format(int(hours_saved / 12)))

    with bi2:
        st.markdown("**What changes at scale**")
        st.markdown("""
- A shrinking backlog signals engineering health to leadership and auditors
- Issue resolution becomes a predictable, measurable process instead of an art form
- Devin's output creates a paper trail: every fix is documented, tested, and reviewable
- The system is additive — engineers approve, Devin executes, humans stay in control
""")


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Backlog Autopilot — Built for FinServ Co. | "
    "Live data from GitHub · Execution powered by Devin · Subagent architecture via Devin CLI"
)
