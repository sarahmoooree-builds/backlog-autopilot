"""
store.py — Unified persistence layer for all 5 pipeline stages

Replaces state.py (sessions.json) and triage_store.py (triage_store.json).

Single file: pipeline_store.json

Format:
{
  "ingested":        { "<issue_id>": IngestedIssue, ... },
  "planned":         { "<issue_id>": PlannedIssue, ... },
  "approvals":       { "<issue_id>": ApprovalRecord, ... },
  "architect_plans": { "<issue_id>": ArchitectPlan, ... },
  "reviews":         { "<issue_id>": ReviewRecord, ... },
  "executions":      { "<issue_id>": ExecutionSession, ... },
  "optimizations":   { "<issue_id>": OptimizationRecord, ... }
}
"""

import json
import os
from datetime import datetime
from typing import Optional

STORE_FILE = os.path.join(os.path.dirname(__file__), "pipeline_store.json")
LEGACY_SESSIONS_FILE = os.path.join(os.path.dirname(__file__), "sessions.json")
LEGACY_TRIAGE_FILE = os.path.join(os.path.dirname(__file__), "triage_store.json")

SECTIONS = [
    "ingested", "planned", "approvals", "architect_plans",
    "reviews", "executions", "optimizations"
]


# ---------------------------------------------------------------------------
# Core load / save
# ---------------------------------------------------------------------------

def _load() -> dict:
    if not os.path.exists(STORE_FILE):
        result = {s: {} for s in SECTIONS}
        result["pipeline_meta"] = {}
        return result
    with open(STORE_FILE, "r") as f:
        data = json.load(f)
    # Ensure all sections exist (forward-compatible)
    for s in SECTIONS:
        data.setdefault(s, {})
    data.setdefault("pipeline_meta", {})
    return data


def _save(store: dict) -> None:
    with open(STORE_FILE, "w") as f:
        json.dump(store, f, indent=2)


# ---------------------------------------------------------------------------
# Generic CRUD
# ---------------------------------------------------------------------------

def get_record(section: str, issue_id: int) -> Optional[dict]:
    return _load()[section].get(str(issue_id))


def set_record(section: str, issue_id: int, data: dict) -> None:
    store = _load()
    store[section][str(issue_id)] = data
    _save(store)


def all_records(section: str) -> list:
    store = _load()
    return [{"issue_id": int(k), **v} for k, v in store[section].items()]


def delete_record(section: str, issue_id: int) -> None:
    store = _load()
    store[section].pop(str(issue_id), None)
    _save(store)


# ---------------------------------------------------------------------------
# Stage 1 — Ingest
# ---------------------------------------------------------------------------

def get_ingested(issue_id: int) -> Optional[dict]:
    return get_record("ingested", issue_id)


def set_ingested(issue_id: int, data: dict) -> None:
    set_record("ingested", issue_id, data)


# ---------------------------------------------------------------------------
# Stage 2 — Planner
# ---------------------------------------------------------------------------

def get_planned(issue_id: int) -> Optional[dict]:
    return get_record("planned", issue_id)


def set_planned(issue_id: int, data: dict) -> None:
    set_record("planned", issue_id, data)


# ---------------------------------------------------------------------------
# Checkpoint 2.5 — Human Approval
# ---------------------------------------------------------------------------

def is_approved(issue_id: int) -> bool:
    record = get_record("approvals", issue_id)
    return bool(record and record.get("approved"))


def set_approval(issue_id: int, approved: bool) -> None:
    set_record("approvals", issue_id, {
        "issue_id": issue_id,
        "approved": approved,
        "approved_at": datetime.now().isoformat() if approved else None,
    })


def get_approval(issue_id: int) -> Optional[dict]:
    return get_record("approvals", issue_id)


# ---------------------------------------------------------------------------
# Stage 3 — Architect
# ---------------------------------------------------------------------------

def get_architect_plan(issue_id: int) -> Optional[dict]:
    return get_record("architect_plans", issue_id)


def set_architect_plan(issue_id: int, data: dict) -> None:
    set_record("architect_plans", issue_id, data)


def is_architected(issue_id: int) -> bool:
    plan = get_architect_plan(issue_id)
    return bool(plan and plan.get("architect_status") == "complete")


def clear_architect_plan(issue_id: int) -> None:
    delete_record("architect_plans", issue_id)


def all_architect_plans() -> list:
    return all_records("architect_plans")


# ---------------------------------------------------------------------------
# Checkpoint 3.5 — Human Review
# ---------------------------------------------------------------------------

def is_review_required(issue_id: int) -> bool:
    plan = get_architect_plan(issue_id)
    return bool(plan and plan.get("confidence_score", 100) < 75)


def set_review(issue_id: int, data: dict) -> None:
    set_record("reviews", issue_id, data)


def get_review(issue_id: int) -> Optional[dict]:
    return get_record("reviews", issue_id)


# ---------------------------------------------------------------------------
# Stage 4 — Executor
# ---------------------------------------------------------------------------

def is_dispatched(issue_id: int) -> bool:
    return get_record("executions", issue_id) is not None


def get_execution(issue_id: int) -> Optional[dict]:
    return get_record("executions", issue_id)


def set_execution(issue_id: int, data: dict) -> None:
    set_record("executions", issue_id, data)


def update_execution_status(
    issue_id: int,
    status: str,
    outcome: Optional[str] = None,
    prs: Optional[list] = None,
    completed_at: Optional[str] = None,
) -> None:
    store = _load()
    key = str(issue_id)
    if key not in store["executions"]:
        return
    store["executions"][key]["status"] = status
    if outcome:
        store["executions"][key]["outcome_summary"] = outcome
    if prs is not None:
        store["executions"][key]["pull_requests"] = prs
    if completed_at:
        store["executions"][key]["completed_at"] = completed_at
    _save(store)


def all_executions() -> list:
    return all_records("executions")


# ---------------------------------------------------------------------------
# Pipeline meta — tracks Devin session state for Ingest and Planner stages
# Keys: "ingest", "planner"
# ---------------------------------------------------------------------------

def get_pipeline_meta(stage: str) -> Optional[dict]:
    return _load().get("pipeline_meta", {}).get(stage)


def set_pipeline_meta(stage: str, data: dict) -> None:
    s = _load()
    s["pipeline_meta"][stage] = data
    _save(s)


def clear_pipeline_meta(stage: str) -> None:
    s = _load()
    s["pipeline_meta"].pop(stage, None)
    _save(s)


# ---------------------------------------------------------------------------
# Stage 5 — Optimizer
# ---------------------------------------------------------------------------

def get_optimization(issue_id: int) -> Optional[dict]:
    return get_record("optimizations", issue_id)


def set_optimization(issue_id: int, data: dict) -> None:
    set_record("optimizations", issue_id, data)


def all_optimizations() -> list:
    return all_records("optimizations")


# ---------------------------------------------------------------------------
# Shared utility
# ---------------------------------------------------------------------------

def confidence_label(score: int) -> tuple:
    """Convert 0–100 architect confidence score to (label, hex_color)."""
    if score >= 75:
        return "High Confidence", "#28a745"
    elif score >= 50:
        return "Review Recommended", "#fd7e14"
    else:
        return "Human Required", "#dc3545"


# ---------------------------------------------------------------------------
# One-time migration from legacy stores
# ---------------------------------------------------------------------------

def migrate_legacy_stores() -> None:
    """
    Migrate sessions.json → executions section
    and triage_store.json → architect_plans section.

    Safe to call on every app startup — no-op if already migrated.
    Preserves the legacy files; does not delete them.
    """
    store = _load()
    migrated = False

    # Migrate sessions.json → executions
    if os.path.exists(LEGACY_SESSIONS_FILE):
        try:
            with open(LEGACY_SESSIONS_FILE, "r") as f:
                sessions = json.load(f)
            for issue_id_str, session in sessions.items():
                if issue_id_str not in store["executions"]:
                    store["executions"][issue_id_str] = {
                        "issue_id": int(issue_id_str),
                        "session_id": session.get("session_id", "unknown"),
                        "session_url": session.get("session_url", ""),
                        "status": session.get("status", "In Progress"),
                        "outcome_summary": session.get("outcome_summary", ""),
                        "pull_requests": session.get("pull_requests", []),
                        "dispatched_at": session.get("dispatched_at", datetime.now().isoformat()),
                        "completed_at": None,
                        "estimated_lines_changed": 0,
                        "estimated_files": [],
                    }
                    migrated = True
        except (json.JSONDecodeError, OSError):
            pass

    # Migrate triage_store.json → architect_plans
    # Maps: next_steps → task_breakdown, adds dependencies/risks/architect_status
    if os.path.exists(LEGACY_TRIAGE_FILE):
        try:
            with open(LEGACY_TRIAGE_FILE, "r") as f:
                triage = json.load(f)
            for issue_id_str, t in triage.items():
                if issue_id_str not in store["architect_plans"]:
                    status = t.get("status", "complete")
                    if status == "pending":
                        architect_status = "pending"
                    elif status == "error":
                        architect_status = "error"
                    else:
                        architect_status = "complete"

                    store["architect_plans"][issue_id_str] = {
                        "issue_id": int(issue_id_str),
                        "confidence_score": t.get("confidence_score", 0),
                        "confidence_reasoning": t.get("confidence_reasoning", ""),
                        "root_cause_hypothesis": t.get("root_cause_hypothesis", ""),
                        "affected_files": t.get("affected_files", []),
                        "estimated_lines_changed": t.get("estimated_lines_changed", 0),
                        # next_steps → task_breakdown
                        "task_breakdown": t.get("next_steps", t.get("task_breakdown", [])),
                        "dependencies": t.get("dependencies", []),
                        "risks": t.get("risks", []),
                        "session_id": t.get("session_id", "unknown"),
                        "session_url": t.get("session_url", ""),
                        "architect_status": architect_status,
                        "error": t.get("error"),
                        "architected_at": t.get("triaged_at", t.get("architected_at", datetime.now().isoformat())),
                    }
                    migrated = True
        except (json.JSONDecodeError, OSError):
            pass

    if migrated:
        _save(store)
