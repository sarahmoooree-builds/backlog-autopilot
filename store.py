"""
store.py — Unified persistence layer for all 5 pipeline stages

Backend: SQLite (``pipeline_store.db``) with ACID guarantees. Previously a
single ``pipeline_store.json`` file — the JSON format is still read once at
startup and migrated into SQLite, then renamed to ``pipeline_store.json.migrated``.

Schema: one table per section — ``ingested``, ``planned``, ``approvals``,
``architect_plans``, ``reviews``, ``executions``, ``optimizations``, plus
``pipeline_meta``. Each row has a text primary key (``issue_id`` or ``stage``
for ``pipeline_meta``) and a ``data TEXT NOT NULL`` JSON blob. This keeps the
dict-based public API unchanged while adding per-operation concurrency safety.

Scope (Stage 3) was previously called "Architect". The table name
(``architect_plans``) is preserved so existing data keeps working; per-record
field names (``architect_status`` → ``scope_status``, ``architected_at`` →
``scoped_at``) are migrated on load.
"""

import json
import os
import sqlite3
from datetime import datetime
from typing import Optional

STORE_FILE = os.path.join(os.path.dirname(__file__), "pipeline_store.json")
STORE_DB = os.path.join(os.path.dirname(__file__), "pipeline_store.db")
LEGACY_SESSIONS_FILE = os.path.join(os.path.dirname(__file__), "sessions.json")
LEGACY_TRIAGE_FILE = os.path.join(os.path.dirname(__file__), "triage_store.json")

SECTIONS = [
    "ingested", "planned", "approvals", "architect_plans",
    "reviews", "executions", "optimizations"
]

# Whitelist of valid table names. Section names are interpolated directly
# into SQL, so every public function validates against this set first.
_VALID_TABLES = set(SECTIONS) | {"pipeline_meta"}


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    # check_same_thread=False so Streamlit's thread-per-session model can reuse
    # the module from multiple threads. Each call opens its own connection and
    # SQLite serialises concurrent writes at the file level.
    return sqlite3.connect(STORE_DB, check_same_thread=False)


def _init_db() -> None:
    with _conn() as conn:
        for section in SECTIONS:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {section} ("
                "issue_id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pipeline_meta ("
            "stage TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )


def _validate_section(section: str) -> None:
    if section not in _VALID_TABLES:
        raise ValueError(f"Unknown store section: {section!r}")


# Ensure schema exists before any caller runs a query.
_init_db()


# ---------------------------------------------------------------------------
# One-time migration from pipeline_store.json → SQLite
# ---------------------------------------------------------------------------

def _migrate_from_json() -> None:
    """Load pipeline_store.json (if present) into SQLite, then back it up.

    Idempotent: after a successful migration the JSON file is renamed to
    ``pipeline_store.json.migrated`` so subsequent calls are no-ops.
    """
    if not os.path.exists(STORE_FILE):
        return
    try:
        with open(STORE_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    with _conn() as conn:
        for section in SECTIONS:
            records = data.get(section, {}) or {}
            for issue_id, record in records.items():
                conn.execute(
                    f"INSERT OR REPLACE INTO {section} (issue_id, data) VALUES (?, ?)",
                    (str(issue_id), json.dumps(record)),
                )
        meta = data.get("pipeline_meta", {}) or {}
        for stage, record in meta.items():
            conn.execute(
                "INSERT OR REPLACE INTO pipeline_meta (stage, data) VALUES (?, ?)",
                (str(stage), json.dumps(record)),
            )

    backup = STORE_FILE + ".migrated"
    try:
        os.replace(STORE_FILE, backup)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Generic CRUD
# ---------------------------------------------------------------------------

def get_record(section: str, issue_id: int) -> Optional[dict]:
    _validate_section(section)
    with _conn() as conn:
        row = conn.execute(
            f"SELECT data FROM {section} WHERE issue_id = ?",
            (str(issue_id),),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def set_record(section: str, issue_id: int, data: dict) -> None:
    _validate_section(section)
    with _conn() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {section} (issue_id, data) VALUES (?, ?)",
            (str(issue_id), json.dumps(data)),
        )


def all_records(section: str) -> list:
    _validate_section(section)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT issue_id, data FROM {section}"
        ).fetchall()
    return [{"issue_id": int(k), **json.loads(v)} for k, v in rows]


def delete_record(section: str, issue_id: int) -> None:
    _validate_section(section)
    with _conn() as conn:
        conn.execute(
            f"DELETE FROM {section} WHERE issue_id = ?",
            (str(issue_id),),
        )


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
# Stage 3 — Scope (formerly "Architect")
#
# The table name `architect_plans` is retained for backward compatibility;
# per-record `architect_status`/`architected_at` are normalised to
# `scope_status`/`scoped_at` on read.
# ---------------------------------------------------------------------------

def _normalise_scope_plan(plan: Optional[dict]) -> Optional[dict]:
    """Promote legacy `architect_status`/`architected_at` to new keys in memory."""
    if not plan:
        return plan
    if "scope_status" not in plan and "architect_status" in plan:
        plan["scope_status"] = plan["architect_status"]
    if "scoped_at" not in plan and "architected_at" in plan:
        plan["scoped_at"] = plan["architected_at"]
    return plan


def get_scope_plan(issue_id: int) -> Optional[dict]:
    return _normalise_scope_plan(get_record("architect_plans", issue_id))


def set_scope_plan(issue_id: int, data: dict) -> None:
    set_record("architect_plans", issue_id, data)


def is_scoped(issue_id: int) -> bool:
    plan = get_scope_plan(issue_id)
    return bool(plan and plan.get("scope_status") == "complete")


def clear_scope_plan(issue_id: int) -> None:
    delete_record("architect_plans", issue_id)


def all_scope_plans() -> list:
    return [_normalise_scope_plan(p) for p in all_records("architect_plans")]


# --- Backwards-compatible aliases (deprecated; use scope_* names) ---
get_architect_plan = get_scope_plan
set_architect_plan = set_scope_plan
is_architected = is_scoped
clear_architect_plan = clear_scope_plan
all_architect_plans = all_scope_plans


# ---------------------------------------------------------------------------
# Checkpoint 3.5 — Human Review
# ---------------------------------------------------------------------------

def is_review_required(issue_id: int) -> bool:
    plan = get_scope_plan(issue_id)
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
    current = get_record("executions", issue_id)
    if current is None:
        return
    current["status"] = status
    if outcome:
        current["outcome_summary"] = outcome
    if prs is not None:
        current["pull_requests"] = prs
    if completed_at:
        current["completed_at"] = completed_at
    set_record("executions", issue_id, current)


def all_executions() -> list:
    return all_records("executions")


# ---------------------------------------------------------------------------
# Pipeline meta — tracks Devin session state for Ingest and Planner stages
# Keys: "ingest", "planner"
# ---------------------------------------------------------------------------

def get_pipeline_meta(stage: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT data FROM pipeline_meta WHERE stage = ?",
            (stage,),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def set_pipeline_meta(stage: str, data: dict) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pipeline_meta (stage, data) VALUES (?, ?)",
            (stage, json.dumps(data)),
        )


def clear_pipeline_meta(stage: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM pipeline_meta WHERE stage = ?",
            (stage,),
        )


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
    """Convert 0–100 scope confidence score to (label, hex_color)."""
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
    Run every legacy-to-current migration in order:
      1. pipeline_store.json     → SQLite tables
      2. sessions.json           → executions section
      3. triage_store.json       → architect_plans section (ScopePlan records)

    Safe to call on every app startup — each step is a no-op once migrated.
    Legacy files are renamed (pipeline_store.json → .migrated) or left in
    place (sessions.json, triage_store.json) depending on prior behaviour.
    """
    _migrate_from_json()

    # Migrate sessions.json → executions
    if os.path.exists(LEGACY_SESSIONS_FILE):
        try:
            with open(LEGACY_SESSIONS_FILE, "r") as f:
                sessions = json.load(f)
            for issue_id_str, session in sessions.items():
                if get_record("executions", issue_id_str) is not None:
                    continue
                set_record("executions", issue_id_str, {
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
                })
        except (json.JSONDecodeError, OSError):
            pass

    # Migrate triage_store.json → architect_plans (ScopePlan records)
    # Maps: next_steps → task_breakdown, status → scope_status
    if os.path.exists(LEGACY_TRIAGE_FILE):
        try:
            with open(LEGACY_TRIAGE_FILE, "r") as f:
                triage = json.load(f)
            for issue_id_str, t in triage.items():
                if get_record("architect_plans", issue_id_str) is not None:
                    continue
                status = t.get("status", "complete")
                if status == "pending":
                    scope_status = "pending"
                elif status == "error":
                    scope_status = "error"
                else:
                    scope_status = "complete"

                set_record("architect_plans", issue_id_str, {
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
                    "scope_status": scope_status,
                    "error": t.get("error"),
                    "scoped_at": t.get(
                        "triaged_at",
                        t.get("architected_at",
                              t.get("scoped_at", datetime.now().isoformat())),
                    ),
                })
        except (json.JSONDecodeError, OSError):
            pass
