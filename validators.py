"""
validators.py — Runtime validation layer for pipeline stage records.

Each TypedDict in ``schemas.py`` has a mirror Pydantic model here. These
models are the runtime enforcement layer: ``schemas.py`` remains the
documentation/type-hinting source of truth, and these classes validate
the dicts that actually flow through the store at stage boundaries.

Validation is **non-blocking**. ``validate_record`` logs a warning and
returns the original dict on failure so the pipeline keeps moving — this
matches the project convention of "log a warning and continue" rather
than hard-failing mid-stage.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base — all models allow extra keys so adding new fields upstream never
# breaks the pipeline, and missing optional fields just stay None.
# ---------------------------------------------------------------------------

class _StageModel(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Stage 1 — Ingest
# ---------------------------------------------------------------------------

class IngestedIssueModel(_StageModel):
    id: int
    title: Optional[str] = None
    description: Optional[str] = None
    labels: Optional[List[Any]] = None
    age_days: Optional[int] = None
    comments_count: Optional[int] = None
    summary: Optional[str] = None
    issue_type: Optional[str] = None
    complexity: Optional[str] = None
    scope: Optional[str] = None
    risk: Optional[str] = None
    duplicate_of: Optional[int] = None
    ingested_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Stage 2 — Planner
# ---------------------------------------------------------------------------

class PlannerScoreModel(_StageModel):
    user_impact: Optional[int] = None
    business_impact: Optional[int] = None
    effort: Optional[int] = None
    confidence: Optional[int] = None
    total_score: Optional[float] = None
    recommended: Optional[bool] = None
    recommendation_reason: Optional[str] = None
    priority_rank: Optional[int] = None


class PlannedIssueModel(_StageModel):
    id: int
    title: Optional[str] = None
    description: Optional[str] = None
    labels: Optional[List[Any]] = None
    age_days: Optional[int] = None
    comments_count: Optional[int] = None
    summary: Optional[str] = None
    issue_type: Optional[str] = None
    complexity: Optional[str] = None
    scope: Optional[str] = None
    risk: Optional[str] = None
    duplicate_of: Optional[int] = None
    ingested_at: Optional[str] = None
    planner_score: Optional[PlannerScoreModel] = None
    implementation_options: Optional[List[Any]] = None
    planned_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Stage 3 — Scope (formerly "Architect")
# ---------------------------------------------------------------------------

class ScopePlanModel(_StageModel):
    issue_id: int
    confidence_score: Optional[int] = None
    confidence_reasoning: Optional[str] = None
    root_cause_hypothesis: Optional[str] = None
    affected_files: Optional[List[Any]] = None
    estimated_lines_changed: Optional[int] = None
    task_breakdown: Optional[List[Any]] = None
    dependencies: Optional[List[Any]] = None
    risks: Optional[List[Any]] = None
    session_id: Optional[str] = None
    session_url: Optional[str] = None
    scope_status: Optional[str] = None
    error: Optional[str] = None
    scoped_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Stage 4 — Executor
# ---------------------------------------------------------------------------

class ExecutionSessionModel(_StageModel):
    issue_id: int
    session_id: Optional[str] = None
    session_url: Optional[str] = None
    status: Optional[str] = None
    outcome_summary: Optional[str] = None
    pull_requests: Optional[List[Any]] = None
    dispatched_at: Optional[str] = None
    completed_at: Optional[str] = None
    estimated_lines_changed: Optional[int] = None
    estimated_files: Optional[List[Any]] = None


# ---------------------------------------------------------------------------
# Stage 5 — Optimizer
# ---------------------------------------------------------------------------

class OptimizationRecordModel(_StageModel):
    issue_id: int
    planned_score: Optional[dict] = None
    scope_confidence: Optional[int] = None
    actual_status: Optional[str] = None
    actual_pr_count: Optional[int] = None
    estimation_accuracy: Optional[str] = None
    lines_delta: Optional[int] = None
    files_delta: Optional[int] = None
    pattern_tags: Optional[List[Any]] = None
    optimizer_notes: Optional[str] = None
    analyzed_at: Optional[str] = None
    optimizer_mode: Optional[str] = None
    # Devin-powered path only
    session_id: Optional[str] = None
    session_url: Optional[str] = None
    actual_lines_changed: Optional[int] = None
    actual_files_changed: Optional[List[Any]] = None
    failure_root_cause: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def validate_record(data: dict, model_class: type[BaseModel], label: str) -> dict:
    """Validate ``data`` against ``model_class``.

    On success, returns the validated dict (with defaults filled in where
    applicable). On ``ValidationError``, logs a warning with the field
    errors and ``label`` context and returns the original dict unchanged
    so the pipeline keeps running.
    """
    try:
        validated = model_class.model_validate(data)
    except ValidationError as exc:
        logger.warning(
            "Validation failed for %s (%s): %s",
            label,
            model_class.__name__,
            exc.errors(),
        )
        return data
    # Preserve the input shape — emit only the fields that were actually set
    # (including any ``extra="allow"`` extras). This keeps stored records
    # indistinguishable from their pre-validation form while still benefiting
    # from type coercion that Pydantic performed during validation.
    return validated.model_dump(exclude_unset=True)
