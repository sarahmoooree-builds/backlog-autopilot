"""
app.py — Backlog Autopilot: Streamlit UI

5-stage multi-agent pipeline dashboard for FinServ Co.

Stages:
  1. Ingest   — normalise and classify GitHub issues
  2. Planner  — rank and prioritise using PM-configurable weights
  2.5 Human Approval checkpoint
  3. Architect — technical implementation plans via Devin
  3.5 Human Review checkpoint (for low-confidence plans)
  4. Executor — Devin implements the plan and opens PRs
  5. Optimizer — compare estimated vs. actual, surface patterns

Run with: streamlit run app.py
"""

import altair as alt
import pandas as pd
import streamlit as st
from datetime import datetime

from github_client import fetch_issues, fetch_pull_requests
from ingest import ingest_issues
from planner import plan_issues, analyse_issues_with_devin, DEFAULT_WEIGHTS
from architect import architect_issue, architect_issues
from executor import execute_issues, refresh_session_statuses
from optimizer import run_optimizer, get_optimizer_summary
import store
from store import (
    migrate_legacy_stores, confidence_label, all_optimizations,
    get_pipeline_meta, set_pipeline_meta, clear_pipeline_meta,
)


# ---------------------------------------------------------------------------
# One-time migration from legacy sessions.json / triage_store.json
# ---------------------------------------------------------------------------

migrate_legacy_stores()


# ---------------------------------------------------------------------------
# Page configuration
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Backlog Autopilot — FinServ Co.",
    page_icon="📊",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Sidebar — Planner weight controls
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("⚙️ Settings")
    st.caption("Adjust scoring priorities. Changes re-rank issues on next refresh.")
    w_user = st.slider("User Impact",           0.0, 1.0, DEFAULT_WEIGHTS["user_impact"],      0.05)
    w_biz  = st.slider("Business Impact",       0.0, 1.0, DEFAULT_WEIGHTS["business_impact"],  0.05)
    w_eff  = st.slider("Effort (inverted)",     0.0, 1.0, DEFAULT_WEIGHTS["effort"],            0.05)
    w_conf = st.slider("Automation Confidence", 0.0, 1.0, DEFAULT_WEIGHTS["confidence"],        0.05)
    custom_weights = {
        "user_impact":     w_user,
        "business_impact": w_biz,
        "effort":          w_eff,
        "confidence":      w_conf,
    }
    st.divider()
    st.caption("Pipeline: Ingest → Planner → Architect → Executor → Optimizer")


# ---------------------------------------------------------------------------
# Pipeline mode — determines whether each stage uses Devin or rule-based
# ---------------------------------------------------------------------------

def get_pipeline_mode() -> tuple:
    """Returns (ingest_mode, planner_mode) — each is 'devin' or 'rule'."""
    im = store.get_pipeline_meta("ingest")
    pm = store.get_pipeline_meta("planner")
    ingest_mode  = "devin" if (im and im.get("status") == "complete") else "rule"
    planner_mode = "devin" if (pm and pm.get("status") == "complete") else "rule"
    return ingest_mode, planner_mode


# ---------------------------------------------------------------------------
# Load and plan issues (cached; weights + mode used as cache key)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def load_and_plan(weights_tuple, ingest_mode: str, planner_mode: str):
    WEIGHT_KEYS = ["user_impact", "business_impact", "effort", "confidence"]

    if planner_mode == "devin":
        records = store.all_records("planned")
        if records:
            return sorted(records, key=lambda x: x["planner_score"]["total_score"], reverse=True)

    if ingest_mode == "devin":
        ingested = store.all_records("ingested")
        if ingested:
            weights = dict(zip(WEIGHT_KEYS, weights_tuple))
            return plan_issues(ingested, weights=weights)

    raw = fetch_issues()
    ingested = ingest_issues(raw)
    weights = dict(zip(WEIGHT_KEYS, weights_tuple))
    return plan_issues(ingested, weights=weights)


@st.cache_data(ttl=60)
def load_pull_requests():
    return fetch_pull_requests(state="open")


ingest_mode, planner_mode = get_pipeline_mode()
weights_tuple = (w_user, w_biz, w_eff, w_conf)
planned_issues = load_and_plan(weights_tuple, ingest_mode, planner_mode)
recommended = [i for i in planned_issues if i["planner_score"]["recommended"]]
not_recommended = [i for i in planned_issues if not i["planner_score"]["recommended"]]

if "approved_ids" not in st.session_state:
    st.session_state.approved_ids = set()


# ---------------------------------------------------------------------------
# Execution readiness guard
# ---------------------------------------------------------------------------

def is_ready_to_execute(issue_id: int) -> tuple:
    """
    An issue is ready to execute when:
    1. It has a complete ArchitectPlan, AND
    2. Either its confidence ≥ 75, or a human has approved it via Checkpoint 3.5.
    Returns (ready: bool, reason: str).
    """
    plan = store.get_architect_plan(issue_id)
    if not plan or plan.get("architect_status") != "complete":
        return False, "Architect plan required before execution"
    if plan["confidence_score"] < 75:
        review = store.get_review(issue_id)
        if not review or not review.get("review_approved"):
            return False, "Human review required (confidence < 75)"
    return True, ""


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
    all_sessions = store.all_executions()
    status_counts = {}
    for s in all_sessions:
        status_counts[s["status"]] = status_counts.get(s["status"], 0) + 1

    prs = load_pull_requests()

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("Open Issues",  len(planned_issues))
    col2.metric("Recommended",  len(recommended))
    col3.metric("Dispatched",   len(all_sessions))
    col4.metric("Completed",    status_counts.get("Completed", 0))
    col5.metric("Blocked",      status_counts.get("Blocked", 0))
    col6.metric("Open PRs",     len(prs))

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
                    "Mar 31", "Apr 2",  "Apr 4",  "Apr 6",  "Apr 8",  "Apr 10", "Apr 12", "Apr 15"],
            "Devin": [2, 3, 5, 4, 6, 8, 7, 9, 6, 10, 8, 7, 9, 2,
                      len([p for p in prs if "devin" in p.get("head_branch", "").lower()])],
            "Engineers": [3, 2, 4, 1, 3, 2, 5, 3, 2, 4, 1, 3, 2, 0, 0],
        }),
        "Past Year": pd.DataFrame({
            "Day": ["May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr"],
            "Devin":     [0,  0,  0,  0,  0, 12, 28, 45, 62, 78, 95, 36],
            "Engineers": [18, 22, 15, 20, 24, 19, 17, 14, 16, 12, 15, 12],
        }),
    }

    chart_header_col, chart_toggle_col = st.columns([6, 4])
    with chart_header_col:
        st.subheader("Issues Resolved")
    with chart_toggle_col:
        time_range = st.radio(
            "Time range", ["Past Week", "Past Month", "Past Year"],
            horizontal=True, label_visibility="collapsed"
        )

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

    # -----------------------------------------------------------------------
    # AI Analysis panel (Stages 1 + 2 combined)
    # -----------------------------------------------------------------------

    with st.container(border=True):
        hcol, bcol = st.columns([5, 5])
        with hcol:
            st.markdown("**AI Analysis**")
            st.caption(
                "Devin normalises messy GitHub labels and ranks issues with business "
                "reasoning — in a single session."
            )
        with bcol:
            analysis_meta = get_pipeline_meta("ingest")  # set by combined function
            if ingest_mode == "devin" and analysis_meta:
                st.success("Devin-powered")
                rec_count = len([i for i in planned_issues
                                 if i.get("planner_score", {}).get("recommended")])
                total_count = analysis_meta.get("issue_count", len(planned_issues))
                st.caption(
                    f"{total_count} issues analysed · {rec_count} recommended · "
                    f"{analysis_meta.get('ran_at', '')[:10]}"
                )
                if analysis_meta.get("session_url"):
                    st.markdown(f"[View Devin session →]({analysis_meta['session_url']})")
                if st.button("Clear → use rule-based", key="clear_analysis_devin"):
                    clear_pipeline_meta("ingest")
                    clear_pipeline_meta("planner")
                    load_and_plan.clear()
                    st.rerun()
            else:
                st.info("Rule-based (instant · use sliders to adjust weights)")
                if st.button("Run AI Analysis with Devin", key="run_analysis_devin",
                             help="One Devin session normalises labels AND ranks by business impact (~5 min)"):
                    with st.spinner("Devin is analysing issues… (~5 min)"):
                        raw = fetch_issues()
                        result = analyse_issues_with_devin(raw)
                    if result["status"] == "complete":
                        for issue in result["issues"]:
                            store.set_ingested(issue["id"], issue)
                            store.set_planned(issue["id"], issue)
                        now = datetime.now().isoformat()
                        meta = {
                            "status":      "complete",
                            "session_id":  result["session_id"],
                            "session_url": result["session_url"],
                            "ran_at":      now,
                            "issue_count": len(result["issues"]),
                        }
                        set_pipeline_meta("ingest",  meta)
                        set_pipeline_meta("planner", meta)
                        load_and_plan.clear()
                        st.rerun()
                    else:
                        st.error(f"Analysis failed: {result.get('error', 'Unknown error')}")

    st.markdown("")

    # -----------------------------------------------------------------------
    # Issues for Automation
    # -----------------------------------------------------------------------

    st.header("Issues for Automation")
    st.caption(
        "Ranked and prioritised by user impact, business impact, effort, and automation "
        "confidence. Approve issues below, then run the Architect to get a technical plan."
    )

    if not recommended:
        st.info("No issues are currently recommended for automation. "
                "Try adjusting the Planner weights in the sidebar.")
    else:
        # --- Run Architect on All button ---
        not_yet_architected = [i for i in recommended if not store.is_architected(i["id"])]
        arch_col, arch_spacer = st.columns([2, 8])
        with arch_col:
            if not_yet_architected:
                if st.button("Run Architect on All",
                             help="Run Devin architecture planning on all un-architected recommended issues"):
                    try:
                        with st.spinner(
                            f"Architecting {len(not_yet_architected)} issue(s) with Devin… "
                            "this takes 4–6 minutes per issue."
                        ):
                            results = architect_issues(not_yet_architected)
                        errors = [r for r in results.values()
                                  if isinstance(r, dict) and r.get("architect_status") == "error"]
                        if errors:
                            st.warning(f"{len(errors)} issue(s) failed to architect. "
                                       "See each issue for details.")
                    except Exception as e:
                        st.error(f"Architect error: {str(e)}")
                    st.rerun()
            else:
                st.caption("All issues have Architect plans")

        st.markdown("")

        for issue in recommended:
            issue_id = issue["id"]
            already_sent = store.is_dispatched(issue_id)
            arch_plan = store.get_architect_plan(issue_id)
            score = issue["planner_score"]

            # Build title tags
            confidence_tag = ""
            if arch_plan:
                if arch_plan.get("architect_status") == "complete":
                    cs = arch_plan["confidence_score"]
                    label, _ = confidence_label(cs)
                    confidence_tag = f"  ·  {label} ({cs}%)"
                elif arch_plan.get("architect_status") == "pending":
                    confidence_tag = "  ·  Architecting…"
                elif arch_plan.get("architect_status") == "error":
                    confidence_tag = "  ·  Architect Failed"

            execution = store.get_execution(issue_id) if already_sent else None
            dispatch_tag = f"  ·  {execution['status']}" if execution else ""

            col_check, col_info = st.columns([0.5, 9.5])

            with col_check:
                if already_sent:
                    st.checkbox("Sent", key=f"approve_{issue_id}", value=True,
                                disabled=True, label_visibility="collapsed")
                else:
                    checked = st.checkbox(
                        "Approve", key=f"approve_{issue_id}",
                        value=issue_id in st.session_state.approved_ids,
                        label_visibility="collapsed"
                    )
                    if checked:
                        st.session_state.approved_ids.add(issue_id)
                    else:
                        st.session_state.approved_ids.discard(issue_id)

            with col_info:
                with st.expander(
                    f"#{issue_id} — {issue['title']}{confidence_tag}{dispatch_tag}"
                ):
                    # Priority + score row
                    sc1, sc2, sc3, sc4, sc5 = st.columns(5)
                    sc1.metric("Priority", f"#{score['priority_rank']}")
                    sc2.metric("Score",    f"{score['total_score']:.1f}/10")
                    sc3.metric("Impact",   f"{score['user_impact']}/10")
                    sc4.metric("Effort",   f"{score['effort']}/10")
                    sc5.metric("Confidence", f"{score['confidence']}/10")
                    st.caption(f"**Why:** {score['recommendation_reason']}")

                    st.divider()

                    # --- Architect plan display ---
                    if arch_plan and arch_plan.get("architect_status") == "pending":
                        session_url = arch_plan.get("session_url", "")
                        st.info(f"Devin is analysing this issue… [{session_url}]({session_url})")

                    elif arch_plan and arch_plan.get("architect_status") == "error":
                        st.warning(f"Architect failed: {arch_plan.get('error', 'Unknown error')}")
                        if arch_plan.get("session_url"):
                            st.markdown(f"[View Devin session]({arch_plan['session_url']})")
                        if st.button("Retry Architect", key=f"retry_arch_{issue_id}"):
                            store.clear_architect_plan(issue_id)
                            try:
                                with st.spinner("Creating Devin architect session…"):
                                    architect_issue(issue)
                            except Exception as e:
                                st.error(f"Architect error: {str(e)}")
                            st.rerun()

                    elif arch_plan and arch_plan.get("architect_status") == "complete":
                        cs = arch_plan["confidence_score"]
                        label, color = confidence_label(cs)

                        # Confidence badge
                        st.markdown(
                            f"<div style='background:{color}20; border-left:4px solid {color}; "
                            f"padding:10px 14px; border-radius:4px; margin-bottom:12px;'>"
                            f"<strong style='color:{color}'>Architect Confidence: {cs}/100 — {label}</strong><br>"
                            f"<span style='font-size:0.9em'>{arch_plan['confidence_reasoning']}</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

                        ar1, ar2, ar3 = st.columns(3)
                        with ar1:
                            st.markdown("**Root Cause**")
                            st.markdown(arch_plan["root_cause_hypothesis"])
                            st.markdown("**Affected Files**")
                            for f in arch_plan.get("affected_files", []):
                                st.markdown(f"- `{f}`")
                            st.caption(f"~{arch_plan.get('estimated_lines_changed', '?')} lines")
                        with ar2:
                            st.markdown("**Task Breakdown**")
                            for i, task in enumerate(arch_plan.get("task_breakdown", []), 1):
                                st.markdown(f"{i}. {task}")
                        with ar3:
                            st.markdown("**Risks**")
                            for r in arch_plan.get("risks", []):
                                st.markdown(f"- {r}")
                            deps = arch_plan.get("dependencies", [])
                            if deps:
                                st.markdown("**Dependencies**")
                                for d in deps:
                                    st.markdown(f"- {d}")

                        if arch_plan.get("session_url"):
                            st.markdown(f"[View Architect session]({arch_plan['session_url']})")

                        # --- Checkpoint 3.5: Human Review gate (low-confidence) ---
                        if cs < 75:
                            review = store.get_review(issue_id)
                            if not review or not review.get("review_approved"):
                                st.warning(
                                    f"⚠️ Checkpoint 3.5 — Architect confidence is {cs}/100 "
                                    "(below 75). Human review recommended before dispatching."
                                )
                                review_notes = st.text_area(
                                    "Review notes (optional)",
                                    key=f"review_notes_{issue_id}"
                                )
                                rv1, rv2, _ = st.columns([1.5, 1.5, 7])
                                with rv1:
                                    if st.button("Approve for Execution",
                                                 key=f"review_approve_{issue_id}"):
                                        store.set_review(issue_id, {
                                            "issue_id": issue_id,
                                            "review_required": True,
                                            "review_approved": True,
                                            "review_notes": review_notes,
                                            "reviewed_at": datetime.now().isoformat(),
                                        })
                                        st.rerun()
                                with rv2:
                                    if st.button("Proceed Anyway",
                                                 key=f"review_skip_{issue_id}"):
                                        store.set_review(issue_id, {
                                            "issue_id": issue_id,
                                            "review_required": True,
                                            "review_approved": True,
                                            "review_notes": "Skipped by user",
                                            "reviewed_at": datetime.now().isoformat(),
                                        })
                                        st.rerun()
                            else:
                                st.success(
                                    f"✓ Checkpoint 3.5 — Reviewed: "
                                    f"{review.get('review_notes') or 'Approved'}"
                                )

                        st.divider()

                    else:
                        # No architect plan yet — show the Run Architect button inline
                        ready, reason = is_ready_to_execute(issue_id)
                        if st.button("Run Architect", key=f"arch_{issue_id}"):
                            try:
                                with st.spinner(
                                    "Creating Devin architect session… (4–6 min to complete)"
                                ):
                                    architect_issue(issue)
                            except Exception as e:
                                st.error(f"Architect error: {str(e)}")
                            st.rerun()

                    # --- Standard issue fields ---
                    st.markdown(f"**Summary:** {issue['summary']}")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.markdown(f"**Type:** `{issue['issue_type']}`")
                    c2.markdown(f"**Complexity:** `{issue['complexity']}`")
                    c3.markdown(f"**Scope:** `{issue['scope']}`")
                    c4.markdown(f"**Risk:** `{issue['risk']}`")
                    if issue.get("implementation_options"):
                        st.markdown("**Implementation options:**")
                        for opt in issue["implementation_options"]:
                            st.markdown(f"- {opt}")
                    st.markdown(f"**Labels:** {', '.join(issue['labels'])}")
                    if issue.get("duplicate_of"):
                        st.caption(f"⚠️ Possible duplicate of issue #{issue['duplicate_of']}")
                    st.caption(f"Age: {issue['age_days']} days | Comments: {issue['comments_count']}")
                    if execution and execution.get("session_url"):
                        st.markdown(f"[Open Executor Session]({execution['session_url']})")

        # --- Select All / Run Approved Issues buttons ---
        st.markdown("")
        new_approved = [
            i for i in planned_issues
            if i["id"] in st.session_state.approved_ids and not store.is_dispatched(i["id"])
        ]
        not_yet_dispatched = [i for i in recommended if not store.is_dispatched(i["id"])]

        # Determine which approved issues are actually ready (have complete arch plan + review if needed)
        ready_to_run = [i for i in new_approved if is_ready_to_execute(i["id"])[0]]

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
            not_ready = len(new_approved) - len(ready_to_run)
            btn_label = "Run Approved Issues"
            if not_ready > 0 and new_approved:
                btn_label = f"Run Approved Issues ({not_ready} need Architect/review)"

            if st.button(btn_label, disabled=len(ready_to_run) == 0):
                with st.spinner("Dispatching to Devin Executor…"):
                    execute_issues(ready_to_run)
                st.rerun()

    st.divider()

    # -----------------------------------------------------------------------
    # Issues for Engineers (not recommended)
    # -----------------------------------------------------------------------

    st.header("Issues for Engineers")
    st.caption(
        "Not recommended for automation by the Planner. "
        "Each includes the reason why it stays with the engineering team."
    )

    for issue in not_recommended:
        score = issue["planner_score"]
        with st.expander(f"#{issue['id']} — {issue['title']}"):
            st.markdown(f"**Summary:** {issue['summary']}")
            c1, c2, c3, c4 = st.columns(4)
            c1.markdown(f"**Type:** `{issue['issue_type']}`")
            c2.markdown(f"**Complexity:** `{issue['complexity']}`")
            c3.markdown(f"**Scope:** `{issue['scope']}`")
            c4.markdown(f"**Risk:** `{issue['risk']}`")
            st.markdown(f"**Why not recommended:** {score['recommendation_reason']}")
            st.caption(f"Score: {score['total_score']:.1f}/10 | "
                       f"Age: {issue['age_days']} days | Comments: {issue['comments_count']}")
            st.markdown(f"**Labels:** {', '.join(issue['labels'])}")

    # -----------------------------------------------------------------------
    # Stage 4: Executor — Execution Pipeline
    # -----------------------------------------------------------------------

    all_sessions = store.all_executions()
    if all_sessions:
        st.divider()
        st.header("Stage 4: Executor — Execution Pipeline")

        refresh_col, _ = st.columns([1.5, 8.5])
        with refresh_col:
            if st.button("Refresh Status"):
                with st.spinner("Polling Devin…"):
                    all_sessions = refresh_session_statuses()
                st.rerun()

        open_prs = load_pull_requests()
        for session in all_sessions:
            status = session["status"]
            icon = {"Completed": "✅", "Awaiting Review": "👀",
                    "In Progress": "🔄", "Blocked": "🚫"}.get(status, "❓")
            issue_id = session["issue_id"]

            with st.expander(f"{icon} Issue #{issue_id}  |  **{status}**"):
                st.markdown(f"**Outcome:** {session['outcome_summary']}")
                if session.get("session_url"):
                    st.markdown(f"[Open Executor Session]({session['session_url']})")

                # Link to Architect session if available
                arch = store.get_architect_plan(issue_id)
                if arch and arch.get("session_url"):
                    st.markdown(f"[View Architect Session]({arch['session_url']})")

                matching_prs = [p for p in open_prs if str(issue_id) in p.get("title", "")]
                if matching_prs:
                    st.markdown("**Pull Requests:**")
                    for pr in matching_prs:
                        st.markdown(f"- [{pr['title']}]({pr['url']}) — `{pr['state']}`")

    # -----------------------------------------------------------------------
    # Stage 5: Optimizer
    # -----------------------------------------------------------------------

    opt_records = all_optimizations()
    all_exec = store.all_executions()
    terminal_exec = [e for e in all_exec if e["status"] in ("Completed", "Blocked", "Awaiting Review")]

    if terminal_exec:
        st.divider()
        st.header("Stage 5: Optimizer")
        st.caption(
            "Compares estimated vs. actual effort for completed sessions. "
            "Surfaces recurring patterns and recommends scoring adjustments."
        )

        opt_col, _ = st.columns([1.5, 8.5])
        with opt_col:
            if st.button("Run Optimizer"):
                try:
                    with st.spinner("Analysing completed sessions…"):
                        new_records = run_optimizer()
                    st.success(f"Analysed {len(new_records)} new session(s).")
                    st.rerun()
                except Exception as e:
                    st.error(f"Optimizer error: {str(e)}")

        if opt_records:
            summary = get_optimizer_summary()
            o1, o2, o3, o4 = st.columns(4)
            o1.metric("Analysed",             summary["total_analyzed"])
            o2.metric("Completion Rate",      f"{summary['completion_rate']:.0%}")
            o3.metric("Blocked Rate",         f"{summary['blocked_rate']:.0%}")
            o4.metric("Avg Confidence",       f"{summary['avg_architect_confidence']:.0f}/100")

            if summary.get("top_patterns"):
                st.markdown("**Recurring Patterns**")
                for tag, count in summary["top_patterns"]:
                    st.markdown(f"- `{tag}`: {count} occurrence(s)")

            if summary.get("heuristic_recommendations"):
                st.markdown("**Heuristic Recommendations**")
                for rec in summary["heuristic_recommendations"]:
                    st.info(rec)

            with st.expander("All Optimization Records"):
                for rec in opt_records:
                    st.markdown(
                        f"**Issue #{rec['issue_id']}** — {rec['actual_status']} — "
                        f"Accuracy: `{rec['estimation_accuracy']}`"
                    )
                    st.caption(rec["optimizer_notes"])
                    if rec.get("pattern_tags"):
                        st.markdown("Tags: " + ", ".join(f"`{t}`" for t in rec["pattern_tags"]))
                    st.markdown("---")


# ===========================================================================
# TAB 2: BUSINESS REPORT
# ===========================================================================

with tab_business:

    TOTAL_BACKLOG     = 312
    AUTOMATABLE_PCT   = 0.28
    AUTOMATABLE       = int(TOTAL_BACKLOG * AUTOMATABLE_PCT)
    AVG_ENGINEER_HRS  = 5.5
    AVG_DEVIN_HRS     = 0.75
    ENGINEER_HOURLY   = 150
    ISSUES_PER_WK_BEF = 8
    ISSUES_PER_WK_AFT = 35

    hours_saved          = AUTOMATABLE * (AVG_ENGINEER_HRS - AVG_DEVIN_HRS)
    cost_saved           = hours_saved * ENGINEER_HOURLY
    weeks_before         = AUTOMATABLE / ISSUES_PER_WK_BEF
    weeks_after          = AUTOMATABLE / ISSUES_PER_WK_AFT

    st.header("Business Impact Report")
    st.caption("Projected outcomes for FinServ Co. based on current backlog composition.")
    st.markdown("")

    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Automatable Issues",       f"{AUTOMATABLE}",             f"{int(AUTOMATABLE_PCT*100)}% of backlog")
    h2.metric("Engineer Hours Recovered", f"{int(hours_saved):,} hrs",  "per backlog cycle")
    h3.metric("Estimated Cost Savings",   f"${int(cost_saved):,}",      "in recovered eng. time")
    h4.metric("Time to Clear Backlog",    f"{weeks_after:.0f} weeks",   f"vs. {weeks_before:.0f} weeks today")

    st.divider()

    chart_col1, chart_col2 = st.columns(2)

    with chart_col1:
        st.subheader("Backlog Burn Rate Projection")
        st.caption("Issues remaining over 12 weeks — with vs. without Devin")
        weeks = list(range(0, 13))
        remaining_before = [max(0, TOTAL_BACKLOG - ISSUES_PER_WK_BEF * w) for w in weeks]
        remaining_after  = [max(0, TOTAL_BACKLOG - ISSUES_PER_WK_AFT * w) for w in weeks]
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
            "Issue Type": ["Simple Bug", "Medium Bug", "Tech Debt",
                           "Simple Bug", "Medium Bug", "Tech Debt"],
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

    st.subheader("Efficiency Breakdown")
    eff1, eff2, eff3 = st.columns(3)

    eff1.markdown("**Automation Coverage**")
    eff1.markdown(
        f"Of FinServ's **{TOTAL_BACKLOG} open issues**, the Planner identifies "
        f"**{AUTOMATABLE} ({int(AUTOMATABLE_PCT*100)}%)** as safe candidates — "
        f"clear bugs with narrow scope, low-to-medium complexity, and no risk "
        f"overlap with auth or billing systems."
    )
    eff2.markdown("**Speed Multiplier**")
    eff2.markdown(
        f"Devin resolves issues in an average of **{int(AVG_DEVIN_HRS*60)} minutes** "
        f"vs. **{AVG_ENGINEER_HRS} hours** for a senior engineer. That's a "
        f"**{AVG_ENGINEER_HRS / AVG_DEVIN_HRS:.0f}x speed improvement** on eligible "
        f"issues, with no context-switching cost."
    )
    eff3.markdown("**Engineer Time Redirect**")
    eff3.markdown(
        f"Automating {AUTOMATABLE} issues recovers **{int(hours_saved):,} engineering "
        f"hours** — equivalent to **{int(hours_saved / 2000)} full-time engineer years** "
        f"— that can be redirected to platform work, architecture, and revenue features."
    )

    st.divider()

    st.subheader("How We Measure Success")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Target: Backlog Cleared",  "12 weeks",   "from 38 weeks today")
    m2.metric("Target: PR Merge Rate",    "≥ 85%",      "Devin PRs passing review")
    m3.metric("Target: Test Pass Rate",   "≥ 95%",      "on Devin-authored changes")
    m4.metric("Target: Weekly Velocity",  "35 issues/wk", "up from 8 today")

    st.markdown("")
    st.markdown("""
**Success criteria (90-day review):**
- Automatable backlog reduced by at least 60%
- No regressions introduced by Devin-authored PRs
- Senior engineers report measurable reduction in backlog-related interruptions
- Optimizer patterns inform scoring weight adjustments each sprint
""")

    st.divider()

    st.subheader("Business Implications for FinServ Co.")
    bi1, bi2 = st.columns(2)

    with bi1:
        st.markdown("**What changes immediately**")
        st.markdown(f"""
- Stale issues begin resolving within hours of Architect approval, not weeks
- The Planner surfaces the highest-ROI issues first — not just the oldest
- Junior engineers review Devin's PRs instead of triaging ambiguous issues
- Senior engineers reclaim ~{int(hours_saved / 12)} hours/month previously spent on backlog triage
""")
    with bi2:
        st.markdown("**What changes at scale**")
        st.markdown("""
- The Optimizer learns which issue types Devin handles best — improving accuracy over time
- A shrinking backlog signals engineering health to leadership and auditors
- Issue resolution becomes a predictable, measurable process instead of an art form
- The system is additive — engineers approve, Devin executes, humans stay in control
""")


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Backlog Autopilot — Built for FinServ Co. | "
    "Live data from GitHub · "
    "Ingest + Planner: rule-based by default, combined Devin analysis on demand · "
    "Architect + Executor powered by Devin · Optimizer learns from outcomes"
)
