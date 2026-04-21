"""
app.py — Backlog Autopilot: Streamlit UI

5-stage multi-agent pipeline dashboard for FinServ Co.

Stages:
  1. Ingest   — normalise and classify GitHub issues
  2. Planner  — rank and prioritise using PM-configurable weights
  2.5 Human approval (select & scope)
  3. Scope    — technical implementation plans via Devin
  3.5 Human review (for low-confidence scope plans)
  4. Executor — Devin implements the plan and opens PRs
  5. Optimizer — compare estimated vs. actual, surface patterns

Run with: streamlit run app.py
"""

from collections import Counter
from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import streamlit as st

from github_client import (
    fetch_issues,
    fetch_pull_requests,
    fetch_closed_issues,
    fetch_merged_prs,
)
from ingest import ingest_issues
from planner import plan_issues, analyse_issues_with_devin
from priorities import (
    BALANCED_INTENT,
    describe_strategy,
    get_strategy,
    parse_prioritization_intent,
    weight_highlights,
)
from scope import scope_issue, scope_issues
from executor import execute_issues, refresh_session_statuses
from optimizer import run_optimizer, get_optimizer_summary
import store
from store import (
    migrate_legacy_stores,
    confidence_label,
    all_optimizations,
    get_pipeline_meta,
    set_pipeline_meta,
    clear_pipeline_meta,
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
# Global style — tighter hierarchy, subtle dividers, consistent badges
# ---------------------------------------------------------------------------

st.markdown(
    """
    <style>
      /* Status badges */
      .ba-badge {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
        letter-spacing: 0.01em;
        border: 1px solid rgba(255,255,255,0.08);
      }
      .ba-group-header {
        font-size: 1.05rem;
        font-weight: 600;
        margin: 0.25rem 0 0.35rem 0;
      }
      .ba-group-sub {
        color: rgba(255,255,255,0.65);
        font-size: 0.88rem;
        margin-bottom: 0.75rem;
      }
      .ba-section-divider {
        margin: 1.2rem 0 1.1rem 0;
        border-top: 1px dashed rgba(255,255,255,0.12);
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Sidebar — pipeline overview only.
# Prioritization is expressed through the natural-language input on the
# Pipeline tab, not manual sliders.
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Backlog Autopilot")
    st.caption(
        "Steer the Planner from the main tab by describing what you're "
        "prioritizing in plain English — no manual weight tuning required."
    )
    st.divider()
    st.caption("Pipeline: Ingest → Planner → Scope → Executor → Optimizer")


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
# Load and plan issues (cached; intent + mode used as cache key)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def load_and_plan(intent: str, ingest_mode: str, planner_mode: str):
    strategy = get_strategy(intent)

    if planner_mode == "devin":
        records = store.all_records("planned")
        if records:
            return sorted(records, key=lambda x: x["planner_score"]["total_score"], reverse=True)

    if ingest_mode == "devin":
        ingested = store.all_records("ingested")
        if ingested:
            return plan_issues(ingested, strategy=strategy)

    raw = fetch_issues()
    ingested = ingest_issues(raw)
    return plan_issues(ingested, strategy=strategy)


@st.cache_data(ttl=60)
def load_pull_requests():
    return fetch_pull_requests(state="open")


@st.cache_data(ttl=300)
def load_closed_issues(days: int = 30):
    """Closed issues in the last `days` days — real data for the resolved chart."""
    try:
        return fetch_closed_issues(days=days)
    except Exception:
        return []


@st.cache_data(ttl=300)
def load_merged_prs(days: int = 30):
    try:
        return fetch_merged_prs(days=days)
    except Exception:
        return []


# Prioritization state — interpreted intent drives the planner.
if "prioritization_text" not in st.session_state:
    st.session_state.prioritization_text = ""
if "prioritization_intent" not in st.session_state:
    st.session_state.prioritization_intent = BALANCED_INTENT

ingest_mode, planner_mode = get_pipeline_mode()
planned_issues = load_and_plan(
    st.session_state.prioritization_intent, ingest_mode, planner_mode
)
auto_recommended = [i for i in planned_issues if i["planner_score"]["recommended"]]
manual_recommended = [i for i in planned_issues if not i["planner_score"]["recommended"]]

if "selected_ids" not in st.session_state:
    st.session_state.selected_ids = set()
# Migrate old key name if present from an earlier session
if "approved_ids" in st.session_state:
    legacy = st.session_state.get("approved_ids") or set()
    if legacy:
        st.session_state.selected_ids |= set(legacy)
    del st.session_state["approved_ids"]


# ---------------------------------------------------------------------------
# Status derivation — single source of truth for badges
# ---------------------------------------------------------------------------

# (label, bg, fg) — dark-mode friendly chips
STATUS_STYLE = {
    "not_scoped":      ("Not scoped",        "rgba(150,150,150,0.18)", "#c9c9c9"),
    "scoping":         ("Scoping…",          "rgba(40,130,210,0.22)",  "#7fb8ff"),
    "scoped":          ("Scoped",            "rgba(40,167,69,0.20)",   "#7ad18d"),
    "scope_review":    ("Scoped · review needed", "rgba(253,126,20,0.22)", "#ffb176"),
    "scope_failed":    ("Scope failed",      "rgba(220,53,69,0.22)",   "#ff8a94"),
    "ready":           ("Ready for execution", "rgba(40,167,69,0.28)", "#8ee39f"),
    "in_progress":     ("In progress",       "rgba(40,130,210,0.22)",  "#7fb8ff"),
    "awaiting_review": ("Awaiting review",   "rgba(253,126,20,0.22)",  "#ffb176"),
    "completed":       ("Completed",         "rgba(40,167,69,0.28)",   "#8ee39f"),
    "blocked":         ("Blocked",           "rgba(220,53,69,0.22)",   "#ff8a94"),
}


def derive_status(issue_id: int) -> str:
    """
    Resolve the canonical status key for an issue based on scope + execution state.
    """
    execution = store.get_execution(issue_id)
    if execution:
        s = execution.get("status", "")
        if s == "Completed":
            return "completed"
        if s == "Awaiting Review":
            return "awaiting_review"
        if s == "Blocked":
            return "blocked"
        if s == "In Progress":
            return "in_progress"

    plan = store.get_scope_plan(issue_id)
    if not plan:
        return "not_scoped"

    status = plan.get("scope_status", "")
    if status == "pending":
        return "scoping"
    if status == "error":
        return "scope_failed"
    if status == "complete":
        cs = plan.get("confidence_score", 0)
        if cs < 75:
            review = store.get_review(issue_id)
            if not review or not review.get("review_approved"):
                return "scope_review"
        return "ready"
    return "not_scoped"


def badge_html(status_key: str) -> str:
    label, bg, fg = STATUS_STYLE.get(status_key, STATUS_STYLE["not_scoped"])
    return (
        f"<span class='ba-badge' style='background:{bg};color:{fg};'>"
        f"{label}</span>"
    )


def is_ready_to_execute(issue_id: int) -> tuple:
    """
    Ready to execute when a complete ScopePlan exists AND either confidence ≥ 75
    or a human has approved via Checkpoint 3.5.
    """
    plan = store.get_scope_plan(issue_id)
    if not plan or plan.get("scope_status") != "complete":
        return False, "Scope plan required before execution"
    if plan["confidence_score"] < 75:
        review = store.get_review(issue_id)
        if not review or not review.get("review_approved"):
            return False, "Human review required (confidence < 75)"
    return True, ""


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

try:
    st.image("finserv_logo.svg", width=280)
except Exception:
    pass
st.title("Backlog Autopilot")
st.caption(
    "Review issues · select the ones you want to work on · scope them with Devin · "
    "approve · run execution."
)
st.divider()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_pipeline, tab_business = st.tabs(["Pipeline", "Business Impact"])


# ===========================================================================
# TAB 1: PIPELINE
# ===========================================================================

with tab_pipeline:

    # -----------------------------------------------------------------------
    # KPI cards — grounded in real data only
    # -----------------------------------------------------------------------

    all_sessions = store.all_executions()
    status_counts: dict = {}
    for s in all_sessions:
        status_counts[s["status"]] = status_counts.get(s["status"], 0) + 1

    prs = load_pull_requests()

    scoped_ids = {
        int(p["issue_id"]) for p in store.all_scope_plans()
        if p.get("scope_status") == "complete"
    }
    ready_ids = {i["id"] for i in planned_issues if is_ready_to_execute(i["id"])[0]}
    closed_recent = load_closed_issues(days=30)

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Open issues",          len(planned_issues))
    k2.metric("Scoped",                len(scoped_ids))
    k3.metric("Ready for execution",   len(ready_ids))
    k4.metric("In progress",           status_counts.get("In Progress", 0))
    k5.metric("Completed (tracked)",   status_counts.get("Completed", 0))
    k6.metric("Resolved · 30d (repo)", len(closed_recent))

    st.markdown("")

    # -----------------------------------------------------------------------
    # Prioritization input — natural-language steering for Planner Devin.
    # Replaces the "Issues resolved" chart here; that chart now lives on the
    # Business Impact tab.
    # -----------------------------------------------------------------------

    def _apply_prioritization_text():
        text = st.session_state.get("prioritization_input", "")
        st.session_state.prioritization_text = text
        st.session_state.prioritization_intent = parse_prioritization_intent(text)
        load_and_plan.clear()

    with st.container(border=True):
        st.subheader("What are you prioritizing?")
        st.caption(
            "Describe today's goal in plain English. We'll adjust issue scoring "
            "automatically — no manual weight tuning."
        )
        st.text_input(
            "Prioritization goal",
            key="prioritization_input",
            placeholder="e.g. I want to fix the worst bugs affecting users",
            on_change=_apply_prioritization_text,
            label_visibility="collapsed",
        )

        active_intent = st.session_state.prioritization_intent
        active_strategy = get_strategy(active_intent)
        has_text = bool(st.session_state.get("prioritization_input", "").strip())

        summary_col, badges_col = st.columns([6, 4])
        with summary_col:
            if active_intent == BALANCED_INTENT and not has_text:
                st.caption(
                    f"**Default strategy — {active_strategy.label}.** "
                    "Enter a goal above to re-rank the backlog."
                )
            else:
                st.markdown(
                    f"**{active_strategy.label}** — "
                    f"{describe_strategy(active_intent).replace('Prioritizing: ', '')}"
                )
        with badges_col:
            highlights = weight_highlights(active_intent, top_n=2)
            badge_html = " ".join(
                f"<span style='background:rgba(27,122,142,0.18); color:#7fd0df; "
                f"padding:4px 10px; border-radius:12px; font-size:0.8em; "
                f"margin-right:6px;'>{label} {pct}%</span>"
                for label, pct in highlights
            )
            st.markdown(
                f"<div style='text-align:right'>{badge_html}</div>",
                unsafe_allow_html=True,
            )

        if has_text:
            if st.button("Reset to balanced", key="reset_prioritization"):
                # Clear the widget key as well — Streamlit ignores `value=` once a
                # widget key lives in session_state, so we must reset it explicitly
                # or the stale text persists through the rerun.
                st.session_state.prioritization_input = ""
                st.session_state.prioritization_text = ""
                st.session_state.prioritization_intent = BALANCED_INTENT
                load_and_plan.clear()
                st.rerun()

    st.markdown("")
    st.divider()

    # -----------------------------------------------------------------------
    # AI Analysis panel (Stages 1 + 2 combined)
    # -----------------------------------------------------------------------

    with st.container(border=True):
        hcol, bcol = st.columns([5, 5])
        with hcol:
            st.markdown("**AI analysis**")
            st.caption(
                "Devin normalises messy GitHub labels and ranks issues with business "
                "reasoning — in a single session."
            )
        with bcol:
            analysis_meta = get_pipeline_meta("ingest")
            if ingest_mode == "devin" and analysis_meta:
                st.success("Devin-powered")
                rec_count = len(auto_recommended)
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
                st.info("Rule-based (instant · steered by the prioritization goal above)")
                if st.button("Run AI analysis with Devin", key="run_analysis_devin",
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
    # Unified issue list
    # -----------------------------------------------------------------------

    st.subheader("Issues")
    st.caption(
        "Select any issue to include it in the next scoping run. The recommendation "
        "groups below are guidance — selection is never locked to a single group."
    )

    selectable_ids = {
        i["id"] for i in planned_issues
        if derive_status(i["id"]) == "not_scoped" and not store.is_dispatched(i["id"])
    }
    currently_selected = st.session_state.selected_ids & selectable_ids

    # --- Action row ---
    a1, a2, a3, a4 = st.columns([2.0, 2.2, 2.2, 3.6])

    with a1:
        if st.button("Select all recommended", disabled=not auto_recommended):
            for i in auto_recommended:
                if i["id"] in selectable_ids:
                    st.session_state.selected_ids.add(i["id"])
            st.rerun()

    with a2:
        if st.button("Clear selection", disabled=not currently_selected):
            st.session_state.selected_ids.clear()
            st.rerun()

    with a3:
        # Selected issues that still need scoping
        to_scope = [
            i for i in planned_issues
            if i["id"] in st.session_state.selected_ids
            and derive_status(i["id"]) == "not_scoped"
        ]
        scope_label = f"Scope selected issues ({len(to_scope)})" if to_scope else "Scope selected issues"
        st.markdown(
            """<style>
            div[data-testid="stColumn"]:nth-child(3) button {
                background-color: #1B7A8E; color: white; border: none;
            }
            div[data-testid="stColumn"]:nth-child(3) button:hover {
                background-color: #145c6b; color: white; border: none;
            }
            </style>""",
            unsafe_allow_html=True,
        )
        if st.button(scope_label, disabled=len(to_scope) == 0, key="scope_selected_cta"):
            try:
                with st.spinner(
                    f"Scoping {len(to_scope)} issue(s) with Devin… "
                    "this takes 4–6 minutes per issue."
                ):
                    results = scope_issues(to_scope)
                errors = [r for r in results.values()
                          if isinstance(r, dict) and r.get("scope_status") == "error"]
                if errors:
                    st.warning(
                        f"{len(errors)} issue(s) failed to scope. "
                        "Open them below for details."
                    )
            except Exception as e:
                st.error(f"Scope error: {str(e)}")
            st.rerun()

    with a4:
        # Run selected that are ready
        to_run = [
            i for i in planned_issues
            if i["id"] in st.session_state.selected_ids
            and is_ready_to_execute(i["id"])[0]
            and not store.is_dispatched(i["id"])
        ]
        not_ready_selected = [
            i for i in planned_issues
            if i["id"] in st.session_state.selected_ids
            and not is_ready_to_execute(i["id"])[0]
            and not store.is_dispatched(i["id"])
        ]
        run_label = f"Run execution ({len(to_run)})" if to_run else "Run execution"
        st.markdown(
            """<style>
            div[data-testid="stColumn"]:nth-child(4) button {
                background-color: #28a745; color: white; border: none;
            }
            div[data-testid="stColumn"]:nth-child(4) button:hover {
                background-color: #218838; color: white; border: none;
            }
            </style>""",
            unsafe_allow_html=True,
        )
        if st.button(run_label, disabled=len(to_run) == 0, key="run_execution_cta"):
            with st.spinner("Dispatching to Devin Executor…"):
                execute_issues(to_run)
            st.rerun()
        if not_ready_selected and not to_run:
            st.caption(
                f"{len(not_ready_selected)} selected issue(s) need scoping or review first."
            )

    st.markdown("")

    # --- Group 1: Recommended for automation ---

    st.markdown(
        "<div class='ba-group-header'>Recommended for automation</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='ba-group-sub'>"
        f"{len(auto_recommended)} issue(s) scored as strong automation candidates — "
        f"narrow scope, clear intent, low-to-medium complexity."
        f"</div>",
        unsafe_allow_html=True,
    )

    def render_issue_row(issue: dict, recommended_group: bool) -> None:
        issue_id = issue["id"]
        score = issue["planner_score"]
        status_key = derive_status(issue_id)
        already_sent = store.is_dispatched(issue_id)
        scope_plan = store.get_scope_plan(issue_id)

        col_check, col_info = st.columns([0.45, 9.55])

        with col_check:
            if already_sent or status_key in ("completed", "in_progress", "awaiting_review",
                                               "blocked", "scoping"):
                st.checkbox(
                    "Selected",
                    key=f"select_{issue_id}",
                    value=True,
                    disabled=True,
                    label_visibility="collapsed",
                )
            else:
                checked = st.checkbox(
                    "Select",
                    key=f"select_{issue_id}",
                    value=issue_id in st.session_state.selected_ids,
                    label_visibility="collapsed",
                )
                if checked:
                    st.session_state.selected_ids.add(issue_id)
                else:
                    st.session_state.selected_ids.discard(issue_id)

        with col_info:
            # Title row with status badge via markdown header
            header_line = (
                f"#{issue_id} — {issue['title']}  ·  "
                f"score {score['total_score']:.1f}/10"
            )
            # Streamlit expanders don't render HTML in their label — render a badge
            # on the line above the expander so status is always visible.
            st.markdown(
                f"<div style='margin-bottom:-6px;'>{badge_html(status_key)}</div>",
                unsafe_allow_html=True,
            )
            with st.expander(header_line):
                # Priority row
                sc1, sc2, sc3, sc4, sc5 = st.columns(5)
                sc1.metric("Priority",   f"#{score['priority_rank']}")
                sc2.metric("Impact",     f"{score['user_impact']}/10")
                sc3.metric("Business",   f"{score['business_impact']}/10")
                sc4.metric("Effort",     f"{score['effort']}/10")
                sc5.metric("Confidence", f"{score['confidence']}/10")

                # Recommendation reason — always shown so "why" is obvious
                st.markdown(
                    f"**Why {'recommended' if recommended_group else 'kept for manual handling'}:** "
                    f"{score['recommendation_reason']}"
                )

                st.divider()

                # --- Scope plan display ---
                if scope_plan and scope_plan.get("scope_status") == "pending":
                    session_url = scope_plan.get("session_url", "")
                    if session_url:
                        st.info(
                            f"Devin is scoping this issue… "
                            f"[open session]({session_url})"
                        )
                    else:
                        st.info("Devin is scoping this issue…")

                elif scope_plan and scope_plan.get("scope_status") == "error":
                    st.warning(f"Scope failed: {scope_plan.get('error', 'Unknown error')}")
                    if scope_plan.get("session_url"):
                        st.markdown(f"[View Devin session]({scope_plan['session_url']})")
                    if st.button("Retry scope", key=f"retry_scope_{issue_id}"):
                        store.clear_scope_plan(issue_id)
                        try:
                            with st.spinner("Creating Devin scope session…"):
                                scope_issue(issue)
                        except Exception as e:
                            st.error(f"Scope error: {str(e)}")
                        st.rerun()

                elif scope_plan and scope_plan.get("scope_status") == "complete":
                    cs = scope_plan["confidence_score"]
                    label, color = confidence_label(cs)

                    st.markdown(
                        f"<div style='background:{color}20; border-left:4px solid {color}; "
                        f"padding:10px 14px; border-radius:4px; margin-bottom:12px;'>"
                        f"<strong style='color:{color}'>Scope confidence: {cs}/100 — {label}</strong><br>"
                        f"<span style='font-size:0.9em'>{scope_plan['confidence_reasoning']}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

                    ar1, ar2, ar3 = st.columns(3)
                    with ar1:
                        st.markdown("**Root cause**")
                        st.markdown(scope_plan["root_cause_hypothesis"])
                        st.markdown("**Affected files**")
                        for f in scope_plan.get("affected_files", []):
                            st.markdown(f"- `{f}`")
                        st.caption(f"~{scope_plan.get('estimated_lines_changed', '?')} lines")
                    with ar2:
                        st.markdown("**Task breakdown**")
                        for i, task in enumerate(scope_plan.get("task_breakdown", []), 1):
                            st.markdown(f"{i}. {task}")
                    with ar3:
                        st.markdown("**Risks**")
                        for r in scope_plan.get("risks", []):
                            st.markdown(f"- {r}")
                        deps = scope_plan.get("dependencies", [])
                        if deps:
                            st.markdown("**Dependencies**")
                            for d in deps:
                                st.markdown(f"- {d}")

                    if scope_plan.get("session_url"):
                        st.markdown(f"[View scope session]({scope_plan['session_url']})")

                    # Checkpoint 3.5 — Human review gate (low-confidence)
                    if cs < 75:
                        review = store.get_review(issue_id)
                        if not review or not review.get("review_approved"):
                            st.warning(
                                f"Scope confidence is {cs}/100 (below 75). "
                                "Human review recommended before dispatching."
                            )
                            review_notes = st.text_area(
                                "Review notes (optional)",
                                key=f"review_notes_{issue_id}",
                            )
                            rv1, rv2, _ = st.columns([1.5, 1.5, 7])
                            with rv1:
                                if st.button("Approve for execution",
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
                                if st.button("Proceed anyway",
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
                                f"Reviewed: {review.get('review_notes') or 'Approved'}"
                            )

                    st.divider()

                else:
                    # Not yet scoped — inline per-issue scope button
                    if not already_sent:
                        if st.button("Scope this issue", key=f"scope_{issue_id}"):
                            try:
                                with st.spinner(
                                    "Creating Devin scope session… (4–6 min to complete)"
                                ):
                                    scope_issue(issue)
                            except Exception as e:
                                st.error(f"Scope error: {str(e)}")
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
                if issue.get("labels"):
                    st.markdown(f"**Labels:** {', '.join(issue['labels'])}")
                if issue.get("duplicate_of"):
                    st.caption(f"Possible duplicate of issue #{issue['duplicate_of']}")
                st.caption(
                    f"Age: {issue['age_days']} days · Comments: {issue['comments_count']}"
                )
                execution = store.get_execution(issue_id)
                if execution and execution.get("session_url"):
                    st.markdown(f"[Open executor session]({execution['session_url']})")

    if auto_recommended:
        for issue in auto_recommended:
            render_issue_row(issue, recommended_group=True)
    else:
        st.caption("No issues currently scored as automation-ready. "
                   "Adjust the Planner weights in the sidebar to see more.")

    # --- Clear grouped divider ---
    st.markdown("<div class='ba-section-divider'></div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='ba-group-header'>Recommended for manual handling</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='ba-group-sub'>"
        f"{len(manual_recommended)} issue(s) scored as better suited for engineers — "
        f"risky area, ambiguous scope, or high complexity. You can still select any "
        f"of these to scope them anyway."
        f"</div>",
        unsafe_allow_html=True,
    )

    if manual_recommended:
        for issue in manual_recommended:
            render_issue_row(issue, recommended_group=False)
    else:
        st.caption("No issues in this group right now.")

    # -----------------------------------------------------------------------
    # Execution pipeline — in-flight and terminal Devin sessions
    # -----------------------------------------------------------------------

    if all_sessions:
        st.divider()
        st.subheader("Execution pipeline")
        st.caption(
            "Devin execution sessions dispatched from this app. "
            "Use refresh to pull the latest status from the Devin API."
        )

        refresh_col, _ = st.columns([1.5, 8.5])
        with refresh_col:
            if st.button("Refresh status"):
                with st.spinner("Polling Devin…"):
                    all_sessions = refresh_session_statuses()
                st.rerun()

        open_prs = load_pull_requests()
        # Map Devin status → internal status_key for badge reuse
        EXEC_STATUS_MAP = {
            "Completed":       "completed",
            "Awaiting Review": "awaiting_review",
            "In Progress":     "in_progress",
            "Blocked":         "blocked",
        }

        for session in all_sessions:
            status = session["status"]
            status_key = EXEC_STATUS_MAP.get(status, "in_progress")
            issue_id = session["issue_id"]

            st.markdown(
                f"<div style='margin-top:4px;margin-bottom:-6px;'>"
                f"{badge_html(status_key)}</div>",
                unsafe_allow_html=True,
            )
            with st.expander(f"Issue #{issue_id}"):
                st.markdown(f"**Outcome:** {session['outcome_summary']}")
                if session.get("session_url"):
                    st.markdown(f"[Open executor session]({session['session_url']})")

                sp = store.get_scope_plan(issue_id)
                if sp and sp.get("session_url"):
                    st.markdown(f"[View scope session]({sp['session_url']})")

                matching_prs = [p for p in open_prs if str(issue_id) in p.get("title", "")]
                if matching_prs:
                    st.markdown("**Pull requests:**")
                    for pr in matching_prs:
                        st.markdown(f"- [{pr['title']}]({pr['url']}) — `{pr['state']}`")

    # -----------------------------------------------------------------------
    # Optimizer
    # -----------------------------------------------------------------------

    opt_records = all_optimizations()
    terminal_exec = [
        e for e in all_sessions
        if e["status"] in ("Completed", "Blocked", "Awaiting Review")
    ]

    if terminal_exec:
        st.divider()
        st.subheader("Optimizer")
        st.caption(
            "Compares estimated vs. actual effort for completed sessions, surfaces "
            "recurring patterns, and suggests scoring adjustments."
        )

        opt_col, _ = st.columns([1.5, 8.5])
        with opt_col:
            if st.button("Run optimizer"):
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
            o1.metric("Analysed",        summary["total_analyzed"])
            o2.metric("Completion rate", f"{summary['completion_rate']:.0%}")
            o3.metric("Blocked rate",    f"{summary['blocked_rate']:.0%}")
            o4.metric("Avg scope confidence",
                      f"{summary.get('avg_scope_confidence', 0):.0f}/100")

            if summary.get("top_patterns"):
                st.markdown("**Recurring patterns**")
                for tag, count in summary["top_patterns"]:
                    st.markdown(f"- `{tag}`: {count} occurrence(s)")

            if summary.get("heuristic_recommendations"):
                st.markdown("**Heuristic recommendations**")
                for rec in summary["heuristic_recommendations"]:
                    st.info(rec)

            with st.expander("All optimization records"):
                for rec in opt_records:
                    st.markdown(
                        f"**Issue #{rec['issue_id']}** — {rec['actual_status']} — "
                        f"accuracy: `{rec['estimation_accuracy']}`"
                    )
                    st.caption(rec["optimizer_notes"])
                    if rec.get("pattern_tags"):
                        st.markdown("Tags: " + ", ".join(f"`{t}`" for t in rec["pattern_tags"]))
                    st.markdown("---")


# ===========================================================================
# TAB 2: BUSINESS REPORT — grounded in live backlog + labelled projections
# ===========================================================================

with tab_business:

    # --- Real, measurable data ---
    live_backlog = len(planned_issues)
    live_automatable = len(auto_recommended)
    live_automatable_pct = (live_automatable / live_backlog) if live_backlog else 0.0

    closed_30d = load_closed_issues(days=30)
    merged_30d = load_merged_prs(days=30)
    devin_merged_30d = [p for p in merged_30d if p["is_devin_authored"]]

    st.header("Business impact")
    st.caption(
        "Everything labelled **Live** is pulled directly from the monitored repository. "
        "Everything labelled **Projection** is a modelled forecast using transparent "
        "assumptions — not measured data."
    )
    st.markdown("")

    # -----------------------------------------------------------------------
    # Live metrics (real data)
    # -----------------------------------------------------------------------

    st.markdown("#### Live metrics")
    l1, l2, l3, l4 = st.columns(4)
    l1.metric("Open backlog", f"{live_backlog}", "source: GitHub (open issues)")
    l2.metric(
        "Automatable share",
        f"{int(live_automatable_pct * 100)}%" if live_backlog else "—",
        f"{live_automatable} of {live_backlog} recommended"
        if live_backlog else "no issues",
    )
    l3.metric("Issues resolved · 30d", f"{len(closed_30d)}", "source: GitHub (closed)")
    l4.metric(
        "PRs merged · 30d",
        f"{len(merged_30d)}",
        f"{len(devin_merged_30d)} Devin-authored",
    )

    st.markdown("")

    # -----------------------------------------------------------------------
    # Issues resolved per day — moved here from the Pipeline tab so the
    # pipeline workflow stays focused on steering the Planner.
    # -----------------------------------------------------------------------

    resolved_col, resolved_info = st.columns([6, 4])
    with resolved_col:
        st.subheader("Issues resolved · last 30 days")
    with resolved_info:
        st.caption(
            f"Source: closed issues in `sarahmoooree-builds/finserv-platform`. "
            f"{len(merged_30d)} PR(s) merged in the same window."
        )

    if closed_30d:
        rows = []
        for c in closed_30d:
            try:
                dt = datetime.fromisoformat(c["closed_at"].replace("Z", "+00:00"))
            except Exception:
                continue
            rows.append(dt.date())

        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=29)
        day_index = {start + timedelta(days=i): 0 for i in range(30)}
        for d in rows:
            if d in day_index:
                day_index[d] += 1

        resolved_df = pd.DataFrame(
            [{"Day": d.isoformat(), "Resolved": n} for d, n in day_index.items()]
        )
        resolved_chart = (
            alt.Chart(resolved_df)
            .mark_bar(color="#1B7A8E")
            .encode(
                x=alt.X("Day:T", axis=alt.Axis(title=None, format="%b %d", labelAngle=0)),
                y=alt.Y("Resolved:Q", title="Issues closed"),
                tooltip=["Day:T", "Resolved:Q"],
            )
            .properties(height=230)
        )
        st.altair_chart(resolved_chart, use_container_width=True)
    else:
        st.info(
            "No issues have been closed in the last 30 days on the target repo — "
            "so there is no real trend to plot yet. The live metrics above are grounded "
            "in live counts; this chart will populate as issues are resolved."
        )

    st.markdown("")

    # -----------------------------------------------------------------------
    # Projection (clearly labelled)
    # -----------------------------------------------------------------------

    st.markdown("#### Projection")
    st.caption(
        "The values below are **projections**, not measurements. They use the assumptions "
        "shown in the expander so you can see exactly what they are based on — and change "
        "them if needed."
    )

    with st.expander("Projection assumptions", expanded=False):
        ast1, ast2, ast3 = st.columns(3)
        with ast1:
            avg_eng_hrs = st.number_input(
                "Avg. engineer hours per automatable issue",
                min_value=0.25, max_value=40.0, value=5.5, step=0.25,
            )
            eng_hourly = st.number_input(
                "Engineer loaded hourly cost ($)",
                min_value=50, max_value=500, value=150, step=10,
            )
        with ast2:
            avg_devin_hrs = st.number_input(
                "Avg. Devin hours per automatable issue",
                min_value=0.05, max_value=10.0, value=0.75, step=0.05,
            )
            issues_per_wk_before = st.number_input(
                "Issues closed per week (baseline)",
                min_value=1, max_value=200,
                value=max(1, len(closed_30d) // 4) if closed_30d else 8,
            )
        with ast3:
            issues_per_wk_after = st.number_input(
                "Issues closed per week (with autopilot)",
                min_value=1, max_value=500, value=35,
            )

    # Derived projections
    hours_saved = live_automatable * (avg_eng_hrs - avg_devin_hrs)
    cost_saved = hours_saved * eng_hourly
    weeks_before = (live_backlog / issues_per_wk_before) if issues_per_wk_before else 0
    weeks_after = (live_backlog / issues_per_wk_after) if issues_per_wk_after else 0

    p1, p2, p3 = st.columns(3)
    p1.metric(
        "Projected engineer hours recovered",
        f"{int(max(hours_saved, 0)):,} hrs",
        "per backlog cycle",
    )
    p2.metric(
        "Projected cost savings",
        f"${int(max(cost_saved, 0)):,}",
        "in recovered engineer time",
    )
    p3.metric(
        "Projected time to clear backlog",
        f"{weeks_after:.0f} wks" if weeks_after else "—",
        f"vs. {weeks_before:.0f} wks today" if weeks_before else None,
    )

    st.markdown("")
    st.markdown("##### Backlog burn-rate projection")
    st.caption(
        "Modelled forecast over 12 weeks using the assumptions above. "
        "This is an illustration of the scenario, not a measurement."
    )

    weeks = list(range(0, 13))
    remaining_before = [max(0, live_backlog - issues_per_wk_before * w) for w in weeks]
    remaining_after = [max(0, live_backlog - issues_per_wk_after * w) for w in weeks]
    burn_df = pd.DataFrame({
        "Week": weeks * 2,
        "Issues Remaining": remaining_before + remaining_after,
        "Scenario": ["Without autopilot"] * 13 + ["With autopilot"] * 13,
    })
    burn_chart = (
        alt.Chart(burn_df)
        .mark_line(point=True, strokeWidth=2.5)
        .encode(
            x=alt.X("Week:Q", axis=alt.Axis(labelAngle=0, title="Weeks from now")),
            y=alt.Y("Issues Remaining:Q", title="Open issues"),
            color=alt.Color(
                "Scenario:N",
                scale=alt.Scale(
                    domain=["With autopilot", "Without autopilot"],
                    range=["#1B7A8E", "#6c757d"],
                ),
                legend=alt.Legend(title=None, orient="bottom"),
            ),
            tooltip=["Week:Q", "Scenario:N", "Issues Remaining:Q"],
        )
        .properties(height=280)
    )
    st.altair_chart(burn_chart, use_container_width=True)

    st.divider()

    # -----------------------------------------------------------------------
    # Efficiency narrative — cites only data we can measure or model transparently
    # -----------------------------------------------------------------------

    st.subheader("Where the gains come from")
    eff1, eff2, eff3 = st.columns(3)

    eff1.markdown("**Automation coverage (live)**")
    eff1.markdown(
        f"Of **{live_backlog} open issues** in the repo, the Planner flags "
        f"**{live_automatable} ({int(live_automatable_pct * 100)}%)** as "
        f"automation candidates — issues with narrow scope, clear intent, and "
        f"low-to-medium complexity. Everything else is recommended for manual handling."
    )

    eff2.markdown("**Speed (projection)**")
    eff2.markdown(
        f"Assuming {avg_devin_hrs:.2f} hrs per issue for Devin vs. "
        f"{avg_eng_hrs:.1f} hrs for a senior engineer, autopilot is modelled "
        f"at roughly **{max(avg_eng_hrs / max(avg_devin_hrs, 0.01), 1):.0f}× faster** "
        f"on eligible issues. Hard proof will come from the Optimizer as real "
        f"sessions complete."
    )

    eff3.markdown("**Engineer time redirect (projection)**")
    eff3.markdown(
        f"Under these assumptions, automating {live_automatable} issues would recover "
        f"**~{int(max(hours_saved, 0)):,} engineer hours** that can be redirected to "
        f"platform, architecture, and revenue work. This scales linearly with the "
        f"automatable share of the backlog."
    )

    st.divider()

    # -----------------------------------------------------------------------
    # Success criteria (phrased as targets, not achievements)
    # -----------------------------------------------------------------------

    st.subheader("Success criteria (targets)")
    m1, m2, m3 = st.columns(3)
    m1.metric("Target · backlog reduction", "≥ 60%", "over 90 days")
    m2.metric("Target · Devin PR merge rate", "≥ 85%", "passing review")
    m3.metric("Target · regression rate",   "0",      "on Devin-authored PRs")

    st.markdown("")
    st.markdown(
        "**How we'll know it's working:** the Optimizer (Stage 5) compares Scope "
        "estimates to actual PR outcomes and surfaces pattern tags such as "
        "`underestimated-scope` or `confidence-mismatch`. Those tags are grounded "
        "in completed sessions — not in projections."
    )


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Backlog Autopilot — Built for FinServ Co. | "
    "Live data from GitHub · "
    "Ingest + Planner: rule-based by default, combined Devin analysis on demand · "
    "Scope + Executor powered by Devin · Optimizer learns from outcomes"
)
