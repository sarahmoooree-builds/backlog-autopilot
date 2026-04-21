"""
optimizer.py — Stage 5: Optimizer

Compares estimated vs. actual effort and outcome for completed execution sessions.
Surfaces recurring patterns and produces heuristic adjustment recommendations.

Two paths:
  - Rule-based  (`run_optimizer`): fast, local, proxy deltas only. No Devin calls.
  - Devin-powered (`run_optimizer_with_devin`): dispatches a Devin session that
    reads real PR diffs from the finserv-platform repo and inspects blocked
    session logs to produce richer retrospective records. Mirrors the API
    interaction pattern used by `scope.py`.

Reads pipeline_store.json, writes pipeline_store.json.

Output: list[OptimizationRecord]
"""

import json
import logging
import requests
from datetime import datetime
from typing import Optional
from collections import Counter

import store
import devin_client
from config import (
    DEVIN_API_KEY,
    DEVIN_ORG_ID,
    OPTIMIZER_TIMEOUT,
    POLL_INTERVAL,
    TARGET_REPO,  # noqa: F401 — re-exported for callers that import it here
)
from planner import migrate_legacy_score
from prompts import OPTIMIZER_PROMPT

logger = logging.getLogger(__name__)

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
        "optimizer_mode": "rule",
        "session_id": None,
        "session_url": None,
        "actual_lines_changed": None,
        "actual_files_changed": None,
        "failure_root_cause": None,
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
            "mode_breakdown": {"rule": 0, "devin": 0},
            "heuristic_recommendations": [],
        }

    total = len(records)
    accuracy = Counter(r["estimation_accuracy"] for r in records)
    all_tags = [tag for r in records for tag in r.get("pattern_tags", [])]
    top_patterns = Counter(all_tags).most_common(6)
    mode_breakdown = Counter(r.get("optimizer_mode", "rule") for r in records)
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
        "mode_breakdown": {
            "rule": mode_breakdown.get("rule", 0),
            "devin": mode_breakdown.get("devin", 0),
        },
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
    Proxy for actual files-changed delta vs. the Scope plan.

    When real data is available (e.g. from the Devin-powered optimizer's
    `actual_files_changed`, or from the PRs Devin actually opened), compare
    against the Scope plan's affected_files. Otherwise fall back to a proxy
    based on status + PR count, mirroring `_estimate_lines_delta`:
      - Blocked: likely underestimate → +3
      - Completed + more than 1 PR: scope crept → +2 per extra PR
      - Otherwise: 0
    """
    if not scope_plan:
        return 0
    estimated_count = len(scope_plan.get("affected_files", []))

    actual_files = execution.get("actual_files_changed")
    if isinstance(actual_files, int):
        return actual_files - estimated_count
    if isinstance(actual_files, (list, tuple, set)):
        return len(actual_files) - estimated_count

    status = execution.get("status", "")
    pr_count = len(execution.get("pull_requests", []))

    if status == "Blocked":
        return 3
    if status == "Completed" and pr_count > 1:
        return (pr_count - 1) * 2
    return 0


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
      low-effort-win        — planner ease ≥ 7 (easy issue) and Completed
    """
    tags = []
    status = execution.get("status", "")
    pr_count = len(execution.get("pull_requests", []))
    confidence = scope_plan.get("confidence_score", 0) if scope_plan else 0
    files_delta = _estimate_files_delta(execution, scope_plan)
    issue_type = planned_issue.get("issue_type", "") if planned_issue else ""
    planner_score = migrate_legacy_score(
        (planned_issue or {}).get("planner_score", {}) or {}
    )
    planner_ease = planner_score.get("ease", 5)

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

    # ease ≥ 7 is the new "low-effort" threshold (ease = 10 - old-effort).
    if planner_ease >= 7 and status == "Completed":
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


# ---------------------------------------------------------------------------
# Devin-powered path — dispatches a single Devin session that analyses the
# whole batch of terminal executions, reads real PR diffs, and produces
# enriched OptimizationRecords (one per issue).
#
# Mirrors the API interaction pattern used by scope.py: same headers, same
# polling/status vocabulary, same message-fetch fallback. The only differences
# are the prompt shape and that the response is a JSON array, not an object.
# ---------------------------------------------------------------------------

def run_optimizer_with_devin() -> list:
    """
    Devin-powered counterpart to `run_optimizer`.

    Collects every terminal ExecutionSession that does not yet have an
    OptimizationRecord, dispatches a single Devin session with the full
    batch, and persists one enriched OptimizationRecord per issue. Returns
    the list of newly saved records (empty list if nothing to analyse or
    on failure).
    """
    if not DEVIN_API_KEY or not DEVIN_ORG_ID:
        raise RuntimeError(
            "DEVIN_API_KEY and DEVIN_ORG_ID must be set to run the "
            "Devin-powered optimizer. Set them in .env."
        )

    # --- Step 1: gather pending executions + their scope/planned data ---
    executions = [
        e for e in store.all_executions()
        if e["status"] in TERMINAL_STATUSES
        and not store.get_optimization(e["issue_id"])
    ]
    if not executions:
        logger.info("No pending terminal executions — nothing to analyse.")
        return []

    scope_plans = [
        store.get_scope_plan(e["issue_id"]) for e in executions
    ]
    planned_issues = [
        store.get_planned(e["issue_id"]) for e in executions
    ]
    prompt = _build_optimizer_prompt(executions, scope_plans, planned_issues)

    # --- Step 2: create the Devin session ---
    logger.info(
        "Creating Devin session for %d terminal execution(s)…", len(executions),
    )
    try:
        created = devin_client.create_session(prompt, bypass_approval=True)
    except requests.exceptions.RequestException as e:
        # No records have been persisted yet, so nothing to clean up. Raising
        # ensures the caller (app.py) surfaces the error instead of silently
        # caching a failure that would block future retries.
        raise RuntimeError(f"Could not reach Devin API: {str(e)}") from e

    session_id = created["session_id"] or ""
    session_url = created["session_url"]

    if not session_id:
        raise RuntimeError("No session_id returned from Devin API")

    logger.info("Session created: %s", session_url)

    # --- Step 3: mark each issue as pending so the UI can show progress ---
    for ex in executions:
        store.set_optimization(
            ex["issue_id"],
            _pending_devin_record(ex, session_id, session_url),
        )

    # --- Step 4: poll until Devin finishes ---
    try:
        result = devin_client.poll_until_done(
            session_id,
            timeout=OPTIMIZER_TIMEOUT,
            poll_interval=POLL_INTERVAL,
            label="optimizer",
        )
        if not result:
            raise RuntimeError(
                f"Devin session timed out after {OPTIMIZER_TIMEOUT // 60} minutes. "
                f"Session: {session_url}"
            )

        final_status = (result.get("status") or "").lower()
        final_detail = (result.get("status_detail") or "").lower()
        has_structured_output = bool(result.get("structured_output"))
        logger.info(
            "Session terminal state: status=%r detail=%r "
            "structured_output_present=%s",
            final_status, final_detail, has_structured_output,
        )

        # --- Step 5: pull messages + extract the JSON array ---
        messages = devin_client.fetch_messages(session_id, label="optimizer")
        devin_count = sum(
            1 for m in messages if (m.get("source") or "").lower() == "devin"
        )
        logger.info(
            "Fetched %d message(s) (devin-authored: %d)",
            len(messages), devin_count,
        )

        records = devin_client.extract_json_array(result, messages)
        if not records:
            raise RuntimeError(
                "Devin finished but optimizer JSON array could not be parsed. "
                f"Session: {session_url}"
            )

        # --- Step 6: persist one enriched record per issue ---
        by_id = {}
        for r in records:
            if not isinstance(r, dict):
                continue
            raw_id = r.get("issue_id")
            if raw_id is None:
                continue
            try:
                by_id[int(raw_id)] = r
            except (TypeError, ValueError):
                continue

        saved = []
        now = datetime.now().isoformat()
        for ex in executions:
            issue_id = ex["issue_id"]
            devin_rec = by_id.get(issue_id)
            if not devin_rec:
                # Devin didn't return a record for this issue — fall back to
                # the rule-based analysis so the UI still has a row, but mark
                # it so it's clear the Devin path missed this one. Clear the
                # pending placeholder first so analyze_outcome can write a
                # fresh record (it returns None when one already exists — the
                # store.get_optimization short-circuit is inside analyze_outcome).
                store.clear_optimization(issue_id)
                fallback = analyze_outcome(issue_id) or {}
                if fallback:
                    fallback["optimizer_mode"] = "rule"
                    fallback["session_id"] = session_id
                    fallback["session_url"] = session_url
                    fallback["optimizer_notes"] = (
                        (fallback.get("optimizer_notes") or "")
                        + " [Devin optimizer did not return a record for this issue; "
                          "rule-based fallback used.]"
                    ).strip()
                    store.set_optimization(issue_id, fallback)
                    saved.append(fallback)
                continue

            scope_plan = store.get_scope_plan(issue_id) or {}
            planned = store.get_planned(issue_id) or {}
            record = _normalise_devin_record(
                devin_rec, ex, scope_plan, planned,
                session_id=session_id, session_url=session_url, analyzed_at=now,
            )
            store.set_optimization(issue_id, record)
            saved.append(record)
    except Exception:
        # Clean up any still-pending placeholders so the affected issues
        # remain eligible for future optimizer runs (rule-based or
        # Devin-powered). This covers failures anywhere in Steps 4–6 —
        # polling, extraction, or persistence. Records already finalised
        # earlier in the loop are preserved because the cleanup check only
        # removes entries that still look like the "in progress" placeholder.
        for ex in executions:
            existing = store.get_optimization(ex["issue_id"])
            if existing and existing.get("optimizer_mode") == "devin" and \
                    existing.get("session_id") == session_id and \
                    (existing.get("optimizer_notes") or "").startswith(
                        "Devin optimizer analysis in progress"):
                store.clear_optimization(ex["issue_id"])
        raise

    logger.info(
        "Saved %d Devin-powered optimization record(s).", len(saved),
    )
    return saved


# ---------------------------------------------------------------------------
# Devin-powered helpers
# ---------------------------------------------------------------------------

def _build_optimizer_prompt(
    executions: list,
    scope_plans: list,
    planned_issues: list,
) -> str:
    """Format the OPTIMIZER_PROMPT with the JSON-serialised batch payload."""
    executions_json = json.dumps(
        [_trim_execution(e) for e in executions],
        indent=2,
        default=str,
    )
    scope_plans_json = json.dumps(
        [_trim_scope_plan(p) for p in scope_plans if p],
        indent=2,
        default=str,
    )
    planned_issues_json = json.dumps(
        [_trim_planned_issue(p) for p in planned_issues if p],
        indent=2,
        default=str,
    )
    return OPTIMIZER_PROMPT.format(
        executions_json=executions_json,
        scope_plans_json=scope_plans_json,
        planned_issues_json=planned_issues_json,
    )


def _trim_execution(ex: dict) -> dict:
    """Keep only the fields Devin needs; drop noisy repeated data."""
    return {
        "issue_id": ex.get("issue_id"),
        "session_id": ex.get("session_id"),
        "session_url": ex.get("session_url"),
        "status": ex.get("status"),
        "outcome_summary": ex.get("outcome_summary"),
        "pull_requests": ex.get("pull_requests", []),
        "dispatched_at": ex.get("dispatched_at"),
        "completed_at": ex.get("completed_at"),
        "estimated_lines_changed": ex.get("estimated_lines_changed", 0),
        "estimated_files": ex.get("estimated_files", []),
    }


def _trim_scope_plan(plan: dict) -> dict:
    return {
        "issue_id": plan.get("issue_id"),
        "confidence_score": plan.get("confidence_score", 0),
        "root_cause_hypothesis": plan.get("root_cause_hypothesis", ""),
        "affected_files": plan.get("affected_files", []),
        "estimated_lines_changed": plan.get("estimated_lines_changed", 0),
        "task_breakdown": plan.get("task_breakdown", []),
        "risks": plan.get("risks", []),
        "session_url": plan.get("session_url"),
    }


def _trim_planned_issue(issue: dict) -> dict:
    return {
        "id": issue.get("id"),
        "title": issue.get("title"),
        "issue_type": issue.get("issue_type"),
        "complexity": issue.get("complexity"),
        "scope": issue.get("scope"),
        "risk": issue.get("risk"),
        "planner_score": issue.get("planner_score", {}),
    }


def _pending_devin_record(ex: dict, session_id: str, session_url: str) -> dict:
    """Placeholder written immediately so the UI can show progress."""
    return {
        "issue_id": ex["issue_id"],
        "planned_score": {},
        "scope_confidence": 0,
        "actual_status": ex.get("status", ""),
        "actual_pr_count": len(ex.get("pull_requests", [])),
        "estimation_accuracy": "accurate",
        "lines_delta": 0,
        "files_delta": 0,
        "pattern_tags": [],
        "optimizer_notes": "Devin optimizer analysis in progress…",
        "optimizer_mode": "devin",
        "session_id": session_id,
        "session_url": session_url,
        "actual_lines_changed": None,
        "actual_files_changed": None,
        "failure_root_cause": None,
        "analyzed_at": datetime.now().isoformat(),
    }


def _normalise_devin_record(
    devin_rec: dict,
    execution: dict,
    scope_plan: dict,
    planned: dict,
    *,
    session_id: str,
    session_url: str,
    analyzed_at: str,
) -> dict:
    """
    Merge Devin's JSON output with the locally-known execution/scope data so
    the resulting record is always complete and matches OptimizationRecord.
    """
    planner_score = planned.get("planner_score", {}) if planned else {}
    scope_confidence = scope_plan.get("confidence_score", 0) if scope_plan else 0

    actual_files = devin_rec.get("actual_files_changed")
    if not isinstance(actual_files, list):
        actual_files = None

    tags = devin_rec.get("pattern_tags")
    if not isinstance(tags, list):
        tags = []

    notes = devin_rec.get("optimizer_notes") or ""
    recs = devin_rec.get("recommendations") or []
    if isinstance(recs, list) and recs:
        notes = (notes + " Recommendations: " + " | ".join(str(r) for r in recs)).strip()

    accuracy = (devin_rec.get("estimation_accuracy") or "accurate").lower()
    if accuracy not in ("over", "under", "accurate"):
        accuracy = "accurate"

    status = devin_rec.get("actual_status") or execution.get("status", "")
    # Normalise to the title-case vocabulary used by the Executor
    # ("Completed", "Blocked", "Awaiting Review"). Downstream summary metrics
    # (completion_rate, blocked_rate) and pattern detection do exact-match
    # comparisons, so accept any casing Devin may emit.
    _STATUS_ALIASES = {
        "completed": "Completed",
        "blocked": "Blocked",
        "awaiting review": "Awaiting Review",
    }
    if status:
        status = _STATUS_ALIASES.get(status.strip().lower(), status)
    failure_cause = devin_rec.get("failure_root_cause")
    if status != "Blocked":
        failure_cause = None

    return {
        "issue_id": execution["issue_id"],
        "planned_score": devin_rec.get("planned_score") or planner_score,
        "scope_confidence": _coerce_optional_int(
            devin_rec.get("scope_confidence", scope_confidence)
        ) or 0,
        "actual_status": status,
        "actual_pr_count": _coerce_optional_int(
            devin_rec.get("actual_pr_count", len(execution.get("pull_requests", [])))
        ) or 0,
        "estimation_accuracy": accuracy,
        "lines_delta": _coerce_optional_int(devin_rec.get("lines_delta", 0)) or 0,
        "files_delta": _coerce_optional_int(devin_rec.get("files_delta", 0)) or 0,
        "pattern_tags": [str(t) for t in tags],
        "optimizer_notes": notes or "Devin optimizer produced no commentary.",
        "optimizer_mode": "devin",
        "session_id": session_id,
        "session_url": session_url,
        "actual_lines_changed": _coerce_optional_int(devin_rec.get("actual_lines_changed")),
        "actual_files_changed": actual_files,
        "failure_root_cause": failure_cause,
        "analyzed_at": analyzed_at,
    }


def _coerce_optional_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


