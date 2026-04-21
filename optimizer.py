"""
optimizer.py — Stage 5: Optimizer

Compares estimated vs. actual effort and outcome for completed execution sessions.
Surfaces recurring patterns and produces heuristic adjustment recommendations.

No external API calls. No Devin sessions.
Reads pipeline_store.json, writes pipeline_store.json.

Output: list[OptimizationRecord]
"""

from datetime import datetime
from typing import Optional
from collections import Counter

import store

TERMINAL_STATUSES = {"Completed", "Blocked", "Awaiting Review"}


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def run_optimizer() -> list:
    """
    Analyse all execution sessions in a terminal state that don't yet have an
    OptimizationRecord. Returns a list of newly produced records.
    """
    executions = store.all_executions()
    new_records = []

    for ex in executions:
        if ex["status"] not in TERMINAL_STATUSES:
            continue
        if store.get_optimization(ex["issue_id"]):
            continue   # already analysed

        record = analyze_outcome(ex["issue_id"])
        if record:
            new_records.append(record)

    return new_records


def analyze_outcome(issue_id: int) -> Optional[dict]:
    """
    Produce an OptimizationRecord for a single completed issue.
    Returns None if the issue hasn't reached a terminal status yet.
    """
    execution = store.get_execution(issue_id)
    if not execution or execution["status"] not in TERMINAL_STATUSES:
        return None

    scope_plan = store.get_scope_plan(issue_id)
    planned_issue = store.get_planned(issue_id)

    lines_delta = _estimate_lines_delta(execution, scope_plan)
    files_delta = _estimate_files_delta(execution, scope_plan)
    estimation_accuracy = _classify_accuracy(lines_delta, files_delta, execution["status"])
    pattern_tags = _detect_patterns(execution, scope_plan, planned_issue)
    notes = _generate_notes(execution, scope_plan, pattern_tags)

    record = {
        "issue_id": issue_id,
        "planned_score": planned_issue.get("planner_score", {}) if planned_issue else {},
        "scope_confidence": scope_plan.get("confidence_score", 0) if scope_plan else 0,
        "actual_status": execution["status"],
        "actual_pr_count": len(execution.get("pull_requests", [])),
        "estimation_accuracy": estimation_accuracy,
        "lines_delta": lines_delta,
        "files_delta": files_delta,
        "pattern_tags": pattern_tags,
        "optimizer_notes": notes,
        "analyzed_at": datetime.now().isoformat(),
    }
    store.set_optimization(issue_id, record)
    return record


def get_optimizer_summary() -> dict:
    """
    Aggregate statistics across all OptimizationRecords.
    Returns a dict ready for display in the Optimizer UI panel.
    """
    records = store.all_optimizations()

    if not records:
        return {
            "total_analyzed": 0,
            "accuracy_breakdown": {"over": 0, "under": 0, "accurate": 0},
            "top_patterns": [],
            "avg_scope_confidence": 0.0,
            "completion_rate": 0.0,
            "blocked_rate": 0.0,
            "heuristic_recommendations": [],
        }

    total = len(records)
    accuracy = Counter(r["estimation_accuracy"] for r in records)
    all_tags = [tag for r in records for tag in r.get("pattern_tags", [])]
    top_patterns = Counter(all_tags).most_common(6)
    # Prefer new key, fall back to legacy records.
    avg_confidence = sum(
        r.get("scope_confidence", r.get("architect_confidence", 0)) for r in records
    ) / total
    completed = sum(1 for r in records if r["actual_status"] == "Completed")
    blocked = sum(1 for r in records if r["actual_status"] == "Blocked")

    summary = {
        "total_analyzed": total,
        "accuracy_breakdown": {
            "over": accuracy.get("over", 0),
            "under": accuracy.get("under", 0),
            "accurate": accuracy.get("accurate", 0),
        },
        "top_patterns": top_patterns,
        "avg_scope_confidence": round(avg_confidence, 1),
        "completion_rate": round(completed / total, 2),
        "blocked_rate": round(blocked / total, 2),
        "heuristic_recommendations": get_heuristic_recommendations(
            total, accuracy, top_patterns, avg_confidence, completed / total
        ),
    }
    return summary


# ---------------------------------------------------------------------------
# Internal analysis helpers
# ---------------------------------------------------------------------------

def _estimate_lines_delta(execution: dict, scope_plan: Optional[dict]) -> int:
    """
    Proxy for actual lines-changed delta (Devin API does not expose PR diff stats).

    Logic:
    - Blocked: likely underestimate → +30
    - Completed + more than 1 PR: scope crept → +15 per extra PR
    - Completed + 1 PR: roughly accurate → 0
    - Awaiting Review: insufficient signal → 0
    """
    status = execution.get("status", "")
    pr_count = len(execution.get("pull_requests", []))

    if status == "Blocked":
        return 30
    if status == "Completed":
        if pr_count > 1:
            return (pr_count - 1) * 15
        return 0
    return 0


def _estimate_files_delta(execution: dict, scope_plan: Optional[dict]) -> int:
    """
    Compare the number of estimated files (from ScopePlan) to the number
    of files recorded at dispatch time in the ExecutionSession.
    """
    if not scope_plan:
        return 0
    estimated_count = len(scope_plan.get("affected_files", []))
    dispatched_count = len(execution.get("estimated_files", []))
    return dispatched_count - estimated_count


def _classify_accuracy(lines_delta: int, files_delta: int, status: str) -> str:
    """Classify overall estimation accuracy as over, under, or accurate."""
    if status == "Blocked":
        return "under"
    if lines_delta > 20 or files_delta > 2:
        return "under"
    if lines_delta < -20 or files_delta < -2:
        return "over"
    return "accurate"


def _detect_patterns(
    execution: dict,
    scope_plan: Optional[dict],
    planned_issue: Optional[dict],
) -> list:
    """
    Tag recurring patterns from the fixed vocabulary.

    Tags:
      auth-false-positive   — risk='high' issue that was nevertheless completed
      underestimated-scope  — files_delta > 2
      confidence-mismatch   — scope confidence ≥ 75 but Blocked
      fast-completion       — Completed with exactly 1 PR
      investigation-leak    — investigation type reached Executor
      low-effort-win        — planner effort ≤ 3 and Completed
    """
    tags = []
    status = execution.get("status", "")
    pr_count = len(execution.get("pull_requests", []))
    confidence = scope_plan.get("confidence_score", 0) if scope_plan else 0
    files_delta = _estimate_files_delta(execution, scope_plan)
    issue_type = planned_issue.get("issue_type", "") if planned_issue else ""
    planner_effort = (planned_issue or {}).get("planner_score", {}).get("effort", 5)

    # Did a high-risk issue actually complete?
    planned_risk = (planned_issue or {}).get("risk", "")
    if planned_risk == "high" and status == "Completed":
        tags.append("auth-false-positive")

    if files_delta > 2:
        tags.append("underestimated-scope")

    if confidence >= 75 and status == "Blocked":
        tags.append("confidence-mismatch")

    if status == "Completed" and pr_count == 1:
        tags.append("fast-completion")

    if issue_type == "investigation":
        tags.append("investigation-leak")

    if planner_effort <= 3 and status == "Completed":
        tags.append("low-effort-win")

    return tags


def _generate_notes(
    execution: dict,
    scope_plan: Optional[dict],
    pattern_tags: list,
) -> str:
    """Generate a plain-language optimizer commentary for display in the UI."""
    status = execution.get("status", "unknown")
    confidence = scope_plan.get("confidence_score", 0) if scope_plan else 0
    pr_count = len(execution.get("pull_requests", []))

    parts = []

    if "fast-completion" in pattern_tags:
        parts.append("Completed cleanly with 1 PR — Scope estimate was accurate.")
    elif status == "Completed":
        parts.append(f"Completed with {pr_count} PR(s).")

    if "confidence-mismatch" in pattern_tags:
        parts.append(
            f"High Scope confidence ({confidence}/100) but session was blocked — "
            f"review the Scope plan for hidden complexity."
        )

    if "underestimated-scope" in pattern_tags:
        parts.append(
            "More files were touched than estimated — consider raising complexity "
            "thresholds in ingest.py for similar issues."
        )

    if "investigation-leak" in pattern_tags:
        parts.append(
            "Investigation-type issue reached the Executor — this should be caught "
            "by the Planner. Review the recommend() threshold in planner.py."
        )

    if "low-effort-win" in pattern_tags:
        parts.append(
            "Low-effort issue completed successfully — consider boosting effort "
            "weight in DEFAULT_WEIGHTS to surface more of these."
        )

    if not parts:
        parts.append(f"Session reached terminal status: {status}. No notable patterns detected.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Heuristic recommendations
# ---------------------------------------------------------------------------

def get_heuristic_recommendations(
    total: int,
    accuracy: Counter,
    top_patterns: list,
    avg_confidence: float,
    completion_rate: float,
) -> list:
    """
    Produce 2–5 actionable plain-language recommendations based on observed patterns.
    """
    recs = []
    pattern_dict = dict(top_patterns)

    under_rate = accuracy.get("under", 0) / total if total else 0
    over_rate = accuracy.get("over", 0) / total if total else 0

    if under_rate > 0.4:
        recs.append(
            f"Scope is underestimated in {int(under_rate*100)}% of sessions. "
            f"Consider reducing the HIGH_COMPLEXITY_SIGNALS threshold in ingest.py "
            f"so more issues are classified as medium/high complexity."
        )

    if over_rate > 0.4:
        recs.append(
            f"Scope is overestimated in {int(over_rate*100)}% of sessions. "
            f"Consider relaxing complexity scoring — Devin is resolving these faster than expected."
        )

    if pattern_dict.get("confidence-mismatch", 0) >= 2:
        recs.append(
            f"Scope confidence mismatch on {pattern_dict['confidence-mismatch']} session(s). "
            f"The Scope stage may be overestimating confidence on certain issue types — "
            f"review the SCOPE_PROMPT constraints around edge-case assessment."
        )

    if pattern_dict.get("low-effort-win", 0) >= 2:
        recs.append(
            f"{pattern_dict['low-effort-win']} low-effort issues completed cleanly. "
            f"Consider increasing the effort weight in DEFAULT_WEIGHTS (planner.py) "
            f"to surface more of these high-value, quick wins."
        )

    if pattern_dict.get("investigation-leak", 0) >= 1:
        recs.append(
            "Investigation-type issues are reaching the Executor. "
            "Tighten the recommend() rule in planner.py to hard-block all investigation types."
        )

    if completion_rate < 0.5 and total >= 3:
        recs.append(
            f"Overall completion rate is {int(completion_rate*100)}% — below 50%. "
            f"Consider raising the RECOMMEND_THRESHOLD in planner.py or lowering the "
            f"confidence gate for Checkpoint 3.5 reviews."
        )

    if avg_confidence > 80 and completion_rate > 0.8:
        recs.append(
            f"High confidence ({avg_confidence:.0f}/100 avg) and strong completion rate "
            f"({int(completion_rate*100)}%). Pipeline is performing well — "
            f"consider expanding automation coverage by lowering RECOMMEND_THRESHOLD."
        )

    return recs[:5]  # cap at 5 to keep the UI clean
