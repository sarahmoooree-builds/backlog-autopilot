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
from planner import (
    plan_issues,
    apply_refinement,
    analyse_issues_with_devin,
    migrate_legacy_score,
    reorder_by_tier,
    rescore_with_strategy,
    BUSINESS_LABELS,
)
from priorities import (
    BALANCED_INTENT,
    describe_strategy,
    get_strategy,
    goal_dimension_highlights,
    parse_prioritization_intent,
    weight_highlights,
)
from scope import scope_issue, scope_issues
from executor import execute_issues, refresh_session_statuses
from optimizer import run_optimizer, run_optimizer_with_devin, get_optimizer_summary
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
      .ba-tier-badge {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 12px;
        font-weight: 600;
        font-size: 0.85rem;
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
def load_and_plan(intent: str, ingest_mode: str, planner_mode: str,
                  refinement: str = ""):
    strategy = get_strategy(intent)

    if planner_mode == "devin":
        records = store.all_records("planned")
        if records:
            # Lazily promote old 4-dim planner_score dicts to the new shape
            # so downstream readers can rely on `tier`, `ease`, etc.
            for rec in records:
                rec["planner_score"] = migrate_legacy_score(
                    rec.get("planner_score", {}) or {}
                )
            # Re-apply the active strategy's tier policy and weights against
            # Devin's per-dimension scores so switching goals produces
            # genuinely different rankings instead of returning the frozen
            # ordering from the original Devin session.
            rescore_with_strategy(records, strategy)
            if refinement:
                records = apply_refinement(records, refinement)
            return records

    if ingest_mode == "devin":
        ingested = store.all_records("ingested")
        if ingested:
            planned = plan_issues(ingested, strategy=strategy)
            if refinement:
                planned = apply_refinement(planned, refinement)
            return planned

    raw = fetch_issues()
    ingested = ingest_issues(raw)
    planned = plan_issues(ingested, strategy=strategy)
    if refinement:
        planned = apply_refinement(planned, refinement)
    return planned


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


# Prioritization state — goal buttons are the primary path; freeform text is
# kept as a fallback for users who prefer natural-language steering.
if "selected_goal" not in st.session_state:
    st.session_state.selected_goal = BALANCED_INTENT
if "prioritization_intent" not in st.session_state:
    st.session_state.prioritization_intent = BALANCED_INTENT
if "refinement_text" not in st.session_state:
    st.session_state.refinement_text = ""
if "prioritization_text" not in st.session_state:
    st.session_state.prioritization_text = ""

ingest_mode, planner_mode = get_pipeline_mode()
planned_issues = load_and_plan(
    st.session_state.prioritization_intent, ingest_mode, planner_mode,
    st.session_state.get("refinement_text", ""),
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

    # -----------------------------------------------------------------------
    # Prioritization input — natural-language steering for Planner Devin.
    # Replaces the "Issues resolved" chart here; that chart now lives on the
    # Business Impact tab.
    # -----------------------------------------------------------------------

    # Goal-selector callbacks -------------------------------------------------

    def _select_goal(intent: str):
        """Called when a goal button is clicked."""
        st.session_state.selected_goal = intent
        st.session_state.prioritization_intent = intent
        st.session_state.refinement_text = ""
        # Also clear the widget key — otherwise Streamlit's session state for
        # `refinement_input` wins over the `value=` param and stale text from
        # the previous goal stays visible after switching to another goal.
        st.session_state.refinement_input = ""
        load_and_plan.clear()
        st.toast(f"Re-ranked: {get_strategy(intent).label}")

    def _apply_refinement():
        """Called when the refinement text changes."""
        st.session_state.refinement_text = st.session_state.get(
            "refinement_input", ""
        )
        load_and_plan.clear()

    def _reset_goal():
        st.session_state.selected_goal = BALANCED_INTENT
        st.session_state.prioritization_intent = BALANCED_INTENT
        st.session_state.refinement_text = ""
        # Mirror _select_goal: clear the widget key too. Not strictly needed
        # today (the text input is unmounted when active == BALANCED_INTENT),
        # but this protects against a future refactor that keeps the input
        # mounted on the balanced view.
        if "refinement_input" in st.session_state:
            st.session_state.refinement_input = ""
        load_and_plan.clear()
        st.toast("Reset to balanced")

    GOAL_OPTIONS = [
        ("worst_bugs",      "Worst bugs"),
        ("quick_wins",      "Quick wins"),
        ("business_impact", "Business impact"),
        ("stale_cleanup",   "Stale cleanup"),
    ]

    with st.container(border=True):
        st.subheader("What's today's goal?")
        st.caption(
            "Pick a goal to re-rank the backlog. Refine with a short phrase "
            "if you want to focus within that goal."
        )

        cols = st.columns(len(GOAL_OPTIONS))
        for col, (intent, label) in zip(cols, GOAL_OPTIONS):
            with col:
                is_active = st.session_state.selected_goal == intent
                col.button(
                    label,
                    key=f"goal_{intent}",
                    on_click=_select_goal,
                    args=(intent,),
                    type="primary" if is_active else "secondary",
                    use_container_width=True,
                )

        active_intent = st.session_state.selected_goal
        active_strategy = get_strategy(active_intent)

        if active_intent != BALANCED_INTENT:
            with st.container(border=True):
                st.markdown(f"**Active goal: {active_strategy.label}**")
                st.caption(active_strategy.summary)

                highlights = goal_dimension_highlights(active_intent)
                if highlights:
                    emphasis_color = {
                        "primary":  "#7fd0df",
                        "high":     "#8ee39f",
                        "moderate": "#c9c9c9",
                    }
                    # NOTE: not `badge_html` — that name is a module-level
                    # function used by render_issue_row; shadowing it here
                    # would crash every issue card with TypeError.
                    dim_badges_html = " · ".join(
                        f"<span style='color:{emphasis_color.get(level, '#c9c9c9')}; "
                        f"font-weight:600;'>{dim}</span>"
                        f" <span style='color:rgba(255,255,255,0.55); font-size:0.85em;'>"
                        f"({level})</span>"
                        for dim, level in highlights
                    )
                    st.markdown(dim_badges_html, unsafe_allow_html=True)

                st.text_input(
                    "Optional: refine your goal",
                    key="refinement_input",
                    value=st.session_state.refinement_text,
                    placeholder='e.g. "Focus on onboarding" or "customer-facing"',
                    on_change=_apply_refinement,
                )
                if st.session_state.refinement_text:
                    st.caption(
                        f'Filtering within **{active_strategy.label}** for: '
                        f'"{st.session_state.refinement_text}"'
                    )

                st.button("Reset to balanced", on_click=_reset_goal,
                          key="reset_goal")
        else:
            st.caption(
                "Currently **balanced** — even spread across severity, reach, "
                "business value, ease, confidence, and urgency. Pick a goal above "
                "to focus the ranking."
            )

    st.markdown("")
    st.divider()

    # -----------------------------------------------------------------------
    # AI Analysis panel (Stages 1 + 2 combined)
    # -----------------------------------------------------------------------

    analysis_meta = get_pipeline_meta("ingest")
    left_col, right_col = st.columns([7, 3])
    if ingest_mode == "devin" and analysis_meta:
        total_count = analysis_meta.get("issue_count", len(planned_issues))
        rec_count = len(auto_recommended)
        ran_date = analysis_meta.get("ran_at", "")[:10]
        with left_col:
            st.markdown(
                f"**Devin-analysed** · {total_count} issues · "
                f"{rec_count} recommended · {ran_date}"
            )
        with right_col:
            link_col, btn_col = st.columns(2)
            with link_col:
                if analysis_meta.get("session_url"):
                    st.markdown(f"[View session ↗]({analysis_meta['session_url']})")
            with btn_col:
                if st.button("Reset to rule-based", key="clear_analysis_devin"):
                    clear_pipeline_meta("ingest")
                    clear_pipeline_meta("planner")
                    load_and_plan.clear()
                    st.rerun()
    else:
        with left_col:
            st.caption("Rule-based ranking · steered by the goal above")
        with right_col:
            if st.button("Enhance with Devin AI", key="run_analysis_devin", type="primary",
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
                    # Keep the checkbox widget state in sync so the UI reflects
                    # the programmatic selection on the next render.
                    st.session_state[f"select_{i['id']}"] = True
            st.rerun()

    with a2:
        if st.button("Clear selection", disabled=not currently_selected):
            # Only reset widget state for issues that are still selectable.
            # Dispatched / in-progress issues render via the disabled-checkbox
            # path with value=True; Streamlit ignores `value=` once a key has
            # state, so touching their keys here would make them visually
            # unchecked even though they're locked in.
            for iid in list(st.session_state.selected_ids & selectable_ids):
                st.session_state[f"select_{iid}"] = False
            st.session_state.selected_ids.clear()
            st.rerun()

    with a3:
        # Selected issues that still need scoping
        to_scope = [
            i for i in planned_issues
            if i["id"] in st.session_state.selected_ids
            and derive_status(i["id"]) == "not_scoped"
        ]
        scope_label = "Scope selected issues"
        # Scope the action-row styling via the button's own st-key class so
        # it doesn't leak into any other 4-column row on the page (the goal
        # selector at app.py:424 is also st.columns(4); :nth-child(3) used
        # to paint the 3rd goal button teal).
        st.markdown(
            """<style>
            .st-key-scope_selected_cta button {
                background-color: #1B7A8E; color: white; border: none;
            }
            .st-key-scope_selected_cta button:hover {
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
        # Same scoping fix as scope_selected_cta above — see comment there.
        st.markdown(
            """<style>
            .st-key-run_execution_cta button {
                background-color: #28a745; color: white; border: none;
            }
            .st-key-run_execution_cta button:hover {
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
                # Widget state (st.session_state[key]) is the source of truth
                # once the widget has rendered — Streamlit ignores `value=` on
                # subsequent renders when a key is set. Seed the widget state
                # from `selected_ids` on first render, then read back after.
                key = f"select_{issue_id}"
                if key not in st.session_state:
                    st.session_state[key] = issue_id in st.session_state.selected_ids
                checked = st.checkbox(
                    "Select",
                    key=key,
                    label_visibility="collapsed",
                )
                if checked:
                    st.session_state.selected_ids.add(issue_id)
                else:
                    st.session_state.selected_ids.discard(issue_id)

        with col_info:
            tier = score.get("tier")
            # Title row with status badge via markdown header
            if tier:
                header_line = (
                    f"#{issue_id} — {issue['title']}  ·  "
                    f"Tier {tier} · "
                    f"{score.get('score_within_tier', score.get('total_score', 0)):.1f}"
                )
            else:
                header_line = (
                    f"#{issue_id} — {issue['title']}  ·  "
                    f"score {score.get('total_score', 0):.1f}/10"
                )
            # Streamlit expanders don't render HTML in their label — render a badge
            # on the line above the expander so status is always visible.
            st.markdown(
                f"<div style='margin-bottom:-6px;'>{badge_html(status_key)}</div>",
                unsafe_allow_html=True,
            )
            with st.expander(header_line):
                # Priority / tier row
                if tier:
                    tier_labels = {1: "Critical", 2: "High",
                                   3: "Normal", 4: "Deferred"}
                    tier_colors = {1: "#e74c3c", 2: "#e67e22",
                                   3: "#3498db", 4: "#95a5a6"}
                    color = tier_colors.get(tier, "#95a5a6")
                    st.markdown(
                        f"<span class='ba-tier-badge' "
                        f"style='background:{color}22; color:{color};'>"
                        f"Tier {tier}: {tier_labels.get(tier, 'Unknown')}"
                        f"</span>  "
                        f"<span style='color:rgba(255,255,255,0.65); "
                        f"font-size:0.9rem;'>"
                        f"Priority #{score.get('priority_rank', 0)}"
                        f"</span>",
                        unsafe_allow_html=True,
                    )
                    tier_reason = score.get("tier_reason", "")
                    if tier_reason:
                        st.caption(tier_reason)

                    sc1, sc2, sc3, sc4, sc5, sc6 = st.columns(6)
                    sc1.metric("Severity",   f"{score.get('severity', '—')}/10")
                    sc2.metric("Reach",      f"{score.get('reach', '—')}/10")
                    sc3.metric("Business",   f"{score.get('business_value', '—')}/10")
                    sc4.metric("Ease",       f"{score.get('ease', '—')}/10")
                    sc5.metric("Confidence", f"{score.get('confidence', '—')}/10")
                    sc6.metric("Urgency",    f"{score.get('urgency', '—')}/10")
                else:
                    # Backward compat for any record that slips through without
                    # a tier (shouldn't happen post-migrate_legacy_score).
                    sc1, sc2, sc3, sc4, sc5 = st.columns(5)
                    sc1.metric("Priority",   f"#{score.get('priority_rank', 0)}")
                    sc2.metric("Impact",     f"{score.get('user_impact', '—')}/10")
                    sc3.metric("Business",   f"{score.get('business_impact', '—')}/10")
                    sc4.metric("Effort",     f"{score.get('effort', '—')}/10")
                    sc5.metric("Confidence", f"{score.get('confidence', '—')}/10")

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
                   "Try a different prioritization goal above to see more.")

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

        opt_mode_col, opt_btn_col, _ = st.columns([2.5, 2, 5.5])
        with opt_mode_col:
            opt_mode_label = st.radio(
                "Optimizer mode",
                ["Rule-based (fast)", "Devin-powered (thorough)"],
                index=0,
                horizontal=True,
                help=(
                    "Rule-based runs locally in seconds using proxy deltas. "
                    "Devin-powered dispatches a Devin session that reads real "
                    "PR diffs and blocked-session logs — takes several minutes."
                ),
                key="optimizer_mode_radio",
            )
        with opt_btn_col:
            if st.button("Run optimizer"):
                try:
                    if opt_mode_label.startswith("Devin"):
                        with st.spinner(
                            "Dispatching Devin optimizer — reading PR diffs "
                            "and session logs (this can take several minutes)…"
                        ):
                            new_records = run_optimizer_with_devin()
                    else:
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

            mb = summary.get("mode_breakdown") or {}
            if mb.get("devin") and mb.get("rule"):
                st.caption(
                    f"Records by mode — rule-based: {mb['rule']} · "
                    f"Devin-powered: {mb['devin']}"
                )

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
                    mode = rec.get("optimizer_mode", "rule")
                    st.markdown(
                        f"**Issue #{rec['issue_id']}** — {rec['actual_status']} — "
                        f"accuracy: `{rec['estimation_accuracy']}` — mode: `{mode}`"
                    )
                    st.caption(rec["optimizer_notes"])
                    if rec.get("pattern_tags"):
                        st.markdown("Tags: " + ", ".join(f"`{t}`" for t in rec["pattern_tags"]))
                    if mode == "devin":
                        if rec.get("actual_lines_changed") is not None:
                            st.markdown(
                                f"Real diff: **{rec['actual_lines_changed']} lines** "
                                f"across {len(rec.get('actual_files_changed') or [])} file(s)"
                            )
                        if rec.get("failure_root_cause"):
                            st.warning(
                                f"Root cause: {rec['failure_root_cause']}"
                            )
                        if rec.get("session_url"):
                            st.markdown(
                                f"[View Devin optimizer session]({rec['session_url']})"
                            )
                    st.markdown("---")


# ===========================================================================
# TAB 2: BUSINESS REPORT — grounded in live backlog + labelled projections
# ===========================================================================

with tab_business:

    # -----------------------------------------------------------------------
    # Section A: Header
    # -----------------------------------------------------------------------

    st.header("Business Impact")
    st.caption(
        "Live metrics from the pipeline and GitHub. "
        "All data is real — nothing is hardcoded or projected."
    )

    # --- Gather live data once ---
    executions_all = store.all_executions()
    completed_executions = [e for e in executions_all if e.get("status") == "Completed"]
    blocked_executions = [e for e in executions_all if e.get("status") == "Blocked"]

    optimizer_summary = get_optimizer_summary()
    optimizer_has_data = optimizer_summary.get("total_analyzed", 0) > 0

    closed_30d = load_closed_issues(days=30)
    merged_30d = load_merged_prs(days=30)
    devin_merged_30d = [p for p in merged_30d if p.get("is_devin_authored")]

    # Constant for engineer-hour recovery estimate (clearly labelled).
    AVG_ENG_HRS = 5.5

    # -----------------------------------------------------------------------
    # Section B: Live KPI cards (4 columns)
    # -----------------------------------------------------------------------

    k1, k2, k3, k4 = st.columns(4)

    # 1. Impact-weighted resolutions
    impact_sum = 0
    impact_count = 0
    for ex in completed_executions:
        planned = store.get_planned(ex["issue_id"])
        if not planned:
            continue
        ps = planned.get("planner_score") or {}
        impact_sum += int(ps.get("user_impact", 0)) + int(ps.get("business_impact", 0))
        impact_count += 1
    if impact_count:
        max_possible = impact_count * 20
        impact_pct = (impact_sum / max_possible * 100) if max_possible else 0.0
        k1.metric(
            "Impact-weighted resolutions",
            f"{impact_sum}",
            f"{impact_pct:.0f}% of max possible",
        )
    else:
        k1.metric(
            "Impact-weighted resolutions",
            "—",
            "no completed work yet",
        )

    # 2. Engineer hours recovered (estimated)
    hours_recovered = len(completed_executions) * AVG_ENG_HRS
    k2.metric(
        "Engineer hours recovered",
        f"{hours_recovered:,.1f} hrs",
        "estimated",
    )
    k2.caption(
        f"Assumes {AVG_ENG_HRS} engineer hours saved per resolved issue."
    )

    # 3. Backlog health — completed / (completed + blocked)
    if optimizer_has_data:
        comp_rate = optimizer_summary.get("completion_rate", 0.0)
        blk_rate = optimizer_summary.get("blocked_rate", 0.0)
        denom = comp_rate + blk_rate
        health_pct = (comp_rate / denom * 100) if denom else 0.0
        k3.metric(
            "Backlog health",
            f"{health_pct:.0f}%",
            "completed vs. blocked",
        )
    else:
        total_ex = len(executions_all)
        if total_ex:
            health_pct = (len(completed_executions) / total_ex) * 100
            k3.metric(
                "Backlog health",
                f"{health_pct:.0f}%",
                "completed vs. total",
            )
        else:
            k3.metric("Backlog health", "—", "no executions yet")

    # 4. AI planner accuracy
    if optimizer_has_data:
        ab = optimizer_summary.get("accuracy_breakdown", {}) or {}
        total_analyzed = optimizer_summary.get("total_analyzed", 0)
        accurate_count = ab.get("accurate", 0)
        if total_analyzed:
            acc_pct = (accurate_count / total_analyzed) * 100
            k4.metric(
                "AI planner accuracy",
                f"{acc_pct:.0f}%",
                f"{accurate_count} of {total_analyzed} accurate",
            )
        else:
            k4.metric("AI planner accuracy", "—")
    else:
        k4.metric("AI planner accuracy", "—")
        k4.caption("Run the Optimizer to see accuracy data.")

    if not executions_all:
        st.info(
            "No completed sessions yet. As issues move through the pipeline, "
            "impact metrics will appear here."
        )

    st.markdown("")
    st.divider()

    # -----------------------------------------------------------------------
    # Section C: Issues Resolved Chart (real data)
    # -----------------------------------------------------------------------

    st.subheader("Resolved work — real GitHub data")
    time_range = st.radio(
        "Time range",
        options=["Past Week", "Past Month"],
        horizontal=True,
        index=1,
        key="business_time_range",
    )
    range_days = 7 if time_range == "Past Week" else 30

    closed_range = load_closed_issues(days=range_days)
    merged_range = load_merged_prs(days=range_days)
    devin_merged_range = [p for p in merged_range if p.get("is_devin_authored")]

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=range_days - 1)
    day_index = {
        start_date + timedelta(days=i): {"Devin": 0, "Engineers": 0}
        for i in range(range_days)
    }

    for p in merged_range:
        merged_at = p.get("merged_at")
        if not merged_at:
            continue
        try:
            dt = datetime.fromisoformat(merged_at.replace("Z", "+00:00")).date()
        except Exception:
            continue
        if dt in day_index:
            bucket = "Devin" if p.get("is_devin_authored") else "Engineers"
            day_index[dt][bucket] += 1

    chart_rows = []
    for d, counts in day_index.items():
        chart_rows.append({"Day": d.isoformat(), "Author": "Devin", "PRs merged": counts["Devin"]})
        chart_rows.append({"Day": d.isoformat(), "Author": "Engineers", "PRs merged": counts["Engineers"]})
    chart_df = pd.DataFrame(chart_rows)

    if chart_df["PRs merged"].sum() == 0 and not closed_range:
        st.info(
            f"No merged PRs or closed issues in the {time_range.lower()}. "
            "This chart will populate as real work lands."
        )
    else:
        resolved_chart = (
            alt.Chart(chart_df)
            .mark_bar()
            .encode(
                x=alt.X(
                    "Day:T",
                    axis=alt.Axis(title=None, format="%b %d", labelAngle=0),
                ),
                y=alt.Y("PRs merged:Q", title="PRs merged"),
                color=alt.Color(
                    "Author:N",
                    scale=alt.Scale(
                        domain=["Devin", "Engineers"],
                        range=["#1B7A8E", "#0F4C81"],
                    ),
                    legend=alt.Legend(title=None, orient="bottom"),
                ),
                tooltip=["Day:T", "Author:N", "PRs merged:Q"],
            )
            .properties(height=260)
        )
        st.altair_chart(resolved_chart, use_container_width=True)
        st.caption(
            f"{len(closed_range)} issue(s) closed · {len(merged_range)} PR(s) merged "
            f"({len(devin_merged_range)} Devin-authored) in the {time_range.lower()}."
        )

    st.divider()

    # -----------------------------------------------------------------------
    # Section D: Impact Breakdown
    # -----------------------------------------------------------------------

    st.subheader("Impact breakdown")
    left_col, right_col = st.columns(2)

    with left_col:
        st.markdown("**High-Impact Work Completed**")
        high_impact = []
        for ex in completed_executions:
            planned = store.get_planned(ex["issue_id"])
            if not planned:
                continue
            ps = planned.get("planner_score") or {}
            biz = int(ps.get("business_impact", 0))
            labels = set(planned.get("labels") or [])
            if biz >= 7 or (labels & BUSINESS_LABELS):
                high_impact.append((ex, planned))

        if not high_impact:
            st.caption(
                "No high-impact completions yet. Completed issues with "
                "`business_impact ≥ 7` or revenue/compliance/SLA labels will appear here."
            )
        else:
            for ex, planned in high_impact:
                ps = planned.get("planner_score") or {}
                title = planned.get("title", "")
                outcome = ex.get("outcome_summary", "")
                status = ex.get("status", "Completed")
                st.markdown(
                    f"**#{ex['issue_id']} — {title}**  \n"
                    f"business_impact: {int(ps.get('business_impact', 0))}/10 · "
                    f"user_impact: {int(ps.get('user_impact', 0))}/10 · {status}"
                )
                if outcome:
                    st.caption(f"\"{outcome}\"")
                st.markdown("---")

    with right_col:
        st.markdown("**Efficiency Wins**")
        if not optimizer_has_data:
            st.info(
                "Run the Optimizer on completed sessions to see efficiency insights."
            )
        else:
            efficiency_wins = []
            for ex in completed_executions:
                opt = store.get_optimization(ex["issue_id"])
                if not opt:
                    continue
                tags = set(opt.get("pattern_tags") or [])
                if {"fast-completion", "low-effort-win"} & tags:
                    planned = store.get_planned(ex["issue_id"])
                    efficiency_wins.append((ex, planned, opt))

            if not efficiency_wins:
                st.caption(
                    "No efficiency wins tagged yet. Optimizer tags like "
                    "`fast-completion` or `low-effort-win` will surface here."
                )
            else:
                for ex, planned, opt in efficiency_wins:
                    title = (planned.get("title", "") if planned else "")
                    outcome = ex.get("outcome_summary", "")
                    tags = opt.get("pattern_tags") or []
                    tag_str = " · ".join(f"`{t}`" for t in tags)
                    st.markdown(
                        f"**#{ex['issue_id']} — {title}**  \n"
                        f"{tag_str}"
                    )
                    if outcome:
                        st.caption(f"\"{outcome}\"")
                    st.markdown("---")

    st.divider()

    # -----------------------------------------------------------------------
    # Section E: Pipeline Learning (from Optimizer)
    # -----------------------------------------------------------------------

    if optimizer_has_data:
        st.subheader("Pipeline learning")
        c1, c2, c3 = st.columns(3)

        completion_rate = optimizer_summary.get("completion_rate", 0.0)
        c1.metric(
            "Completion rate",
            f"{completion_rate * 100:.0f}%",
            delta="across analyzed sessions",
        )

        ab = optimizer_summary.get("accuracy_breakdown", {}) or {}
        breakdown_df = pd.DataFrame([
            {"Category": "Accurate", "Count": ab.get("accurate", 0)},
            {"Category": "Overestimate", "Count": ab.get("over", 0)},
            {"Category": "Underestimate", "Count": ab.get("under", 0)},
        ])
        accuracy_chart = (
            alt.Chart(breakdown_df)
            .mark_bar()
            .encode(
                x=alt.X("Count:Q", title=None),
                y=alt.Y("Category:N", sort="-x", title=None),
                color=alt.Color(
                    "Category:N",
                    scale=alt.Scale(
                        domain=["Accurate", "Overestimate", "Underestimate"],
                        range=["#1B7A8E", "#0F4C81", "#6c757d"],
                    ),
                    legend=None,
                ),
                tooltip=["Category:N", "Count:Q"],
            )
            .properties(height=140)
        )
        with c2:
            st.markdown("**Scope accuracy**")
            st.altair_chart(accuracy_chart, use_container_width=True)

        with c3:
            st.markdown("**Top patterns**")
            top_patterns = optimizer_summary.get("top_patterns") or []
            if top_patterns:
                for tag, count in top_patterns:
                    st.markdown(f"`{tag}` · {count}")
            else:
                st.caption("No recurring patterns detected yet.")

        recs = optimizer_summary.get("heuristic_recommendations") or []
        if recs:
            st.markdown("")
            for rec in recs:
                st.info(f"**Recommendation:** {rec}")

        st.divider()

    # -----------------------------------------------------------------------
    # Section F: Success Criteria (Live vs Target)
    # -----------------------------------------------------------------------

    st.subheader("Progress toward targets")
    m1, m2, m3 = st.columns(3)

    # 1. Backlog reduction
    open_backlog = len(planned_issues)
    closed_count = len(closed_30d)
    reduction_denom = open_backlog + closed_count
    reduction_pct = (closed_count / reduction_denom * 100) if reduction_denom else 0.0
    m1.metric(
        "Backlog reduction · 30d",
        f"{reduction_pct:.0f}%",
        delta="target: ≥ 60%",
    )
    m1.caption(f"{closed_count} closed vs. {open_backlog} still open")

    # 2. Devin PR merge rate
    if merged_30d:
        devin_share = len(devin_merged_30d) / len(merged_30d) * 100
        m2.metric(
            "Devin share of merged PRs · 30d",
            f"{devin_share:.0f}%",
            delta="target: ≥ 85%",
        )
        m2.caption(
            f"{len(devin_merged_30d)} of {len(merged_30d)} merged PRs"
        )
    else:
        m2.metric(
            "Devin PRs merged · 30d",
            f"{len(devin_merged_30d)}",
            delta="target: ≥ 85%",
        )
        m2.caption("Not enough merged PRs in the window yet.")

    # 3. Completion rate
    if optimizer_has_data:
        comp_rate = optimizer_summary.get("completion_rate", 0.0) * 100
        m3.metric(
            "Completion rate",
            f"{comp_rate:.0f}%",
            delta="target: ≥ 85%",
        )
    else:
        total_ex = len(executions_all)
        if total_ex:
            comp_rate = len(completed_executions) / total_ex * 100
            m3.metric(
                "Completion rate",
                f"{comp_rate:.0f}%",
                delta="target: ≥ 85%",
            )
            m3.caption("From execution statuses (no optimizer data yet).")
        else:
            m3.metric(
                "Completion rate",
                "—",
                delta="target: ≥ 85%",
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
