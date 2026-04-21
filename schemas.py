"""
schemas.py — Canonical data schemas for every pipeline stage handoff

Every stage in the 5-stage pipeline produces and consumes one of these TypedDicts.
No module should invent its own dict shape — import from here instead.

Pipeline:
  RawIssue → [Stage 1: Ingest] → IngestedIssue
           → [Stage 2: Planner] → PlannedIssue
           → [Checkpoint 2.5: Human Approval] → ApprovalRecord
           → [Stage 3: Architect] → ArchitectPlan
           → [Checkpoint 3.5: Human Review] → ReviewRecord
           → [Stage 4: Executor] → ExecutionSession
           → [Stage 5: Optimizer] → OptimizationRecord
"""

from typing import Optional

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict


# ---------------------------------------------------------------------------
# Stage 1 input — raw GitHub issue from github_client.py
# ---------------------------------------------------------------------------

class RawIssue(TypedDict):
    id: int
    title: str
    description: str
    labels: list
    age_days: int
    comments_count: int


# ---------------------------------------------------------------------------
# Stage 1 output — Ingest
# Normalised, deduped, and classified. No recommendation or priority logic.
# ---------------------------------------------------------------------------

class IngestedIssue(TypedDict):
    id: int
    title: str
    description: str
    labels: list
    age_days: int
    comments_count: int
    summary: str            # plain-language one-liner
    issue_type: str         # "bug" | "feature_request" | "tech_debt" | "investigation" | "other"
    complexity: str         # "low" | "medium" | "high"
    scope: str              # "narrow" | "broad"
    risk: str               # "low" | "high"
    duplicate_of: Optional[int]  # issue id of suspected duplicate, or None
    ingested_at: str        # ISO timestamp


# ---------------------------------------------------------------------------
# Stage 2 output — Planner
# Scored, ranked, and annotated. No code written, no Devin sessions.
# ---------------------------------------------------------------------------

class PlannerScore(TypedDict):
    user_impact: int        # 0–10
    business_impact: int    # 0–10
    effort: int             # 0–10  (10 = hardest; inverted in total_score)
    confidence: int         # 0–10  (automation likelihood)
    total_score: float      # weighted sum
    recommended: bool       # True if total_score >= 6.0 and risk/type allow it
    recommendation_reason: str
    priority_rank: int      # 1 = highest priority among the batch


class PlannedIssue(TypedDict):
    # All IngestedIssue fields
    id: int
    title: str
    description: str
    labels: list
    age_days: int
    comments_count: int
    summary: str
    issue_type: str
    complexity: str
    scope: str
    risk: str
    duplicate_of: Optional[int]
    ingested_at: str
    # Planner additions
    planner_score: PlannerScore
    implementation_options: list   # 1–3 plain-English options, no code
    planned_at: str


# ---------------------------------------------------------------------------
# Checkpoint 2.5 — Human Approval
# Explicit approval before Architect runs.
# ---------------------------------------------------------------------------

class ApprovalRecord(TypedDict):
    issue_id: int
    approved: bool
    approved_at: Optional[str]   # ISO timestamp; None if not yet approved


# ---------------------------------------------------------------------------
# Stage 3 output — Architect
# Technical implementation plan. No code written.
# ---------------------------------------------------------------------------

class ArchitectPlan(TypedDict):
    issue_id: int
    confidence_score: int           # 0–100
    confidence_reasoning: str
    root_cause_hypothesis: str      # specific file, function, line
    affected_files: list            # real repo paths confirmed by Devin
    estimated_lines_changed: int
    task_breakdown: list            # ordered, actionable tasks (renamed from next_steps)
    dependencies: list              # other issues/PRs this depends on; [] if none
    risks: list                     # edge cases, blast radius notes
    session_id: str
    session_url: str
    architect_status: str           # "pending" | "complete" | "error"
    error: Optional[str]
    architected_at: str


# ---------------------------------------------------------------------------
# Checkpoint 3.5 — Human Review (optional gate for low-confidence plans)
# ---------------------------------------------------------------------------

class ReviewRecord(TypedDict):
    issue_id: int
    review_required: bool           # True when confidence_score < 75
    review_approved: Optional[bool]
    review_notes: Optional[str]
    reviewed_at: Optional[str]


# ---------------------------------------------------------------------------
# Stage 4 output — Executor
# Tracks a Devin implementation session.
# Carries Architect estimates so the Optimizer can diff them.
# ---------------------------------------------------------------------------

class ExecutionSession(TypedDict):
    issue_id: int
    session_id: str
    session_url: str
    status: str                     # "In Progress" | "Awaiting Review" | "Completed" | "Blocked"
    outcome_summary: str
    pull_requests: list             # [{number, title, url}]
    dispatched_at: str
    completed_at: Optional[str]
    # Copied from ArchitectPlan at dispatch time for Optimizer comparison
    estimated_lines_changed: int
    estimated_files: list


# ---------------------------------------------------------------------------
# Stage 5 output — Optimizer
# Retrospective analysis comparing plan vs. reality.
# ---------------------------------------------------------------------------

class OptimizationRecord(TypedDict):
    issue_id: int
    planned_score: dict             # PlannerScore snapshot
    architect_confidence: int       # confidence_score from ArchitectPlan
    actual_status: str              # terminal ExecutionSession status
    actual_pr_count: int
    estimation_accuracy: str        # "over" | "under" | "accurate"
    lines_delta: int                # proxy: positive = underestimate
    files_delta: int                # actual files - estimated files (proxy)
    pattern_tags: list              # e.g. ["fast-completion", "confidence-mismatch"]
    optimizer_notes: str
    analyzed_at: str
