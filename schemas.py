"""
schemas.py — Canonical data schemas for every pipeline stage handoff

Every stage in the 5-stage pipeline produces and consumes one of these TypedDicts.
No module should invent its own dict shape — import from here instead.

Pipeline:
  RawIssue → [Stage 1: Ingest] → IngestedIssue
           → [Stage 2: Planner] → PlannedIssue
           → [Checkpoint 2.5: Human Approval] → ApprovalRecord
           → [Stage 3: Scope] → ScopePlan
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
    # Enriched scoring dimensions. Every dimension is "higher = better" —
    # NO hidden inversions in the weighted sum.
    severity: int              # 0–10, how bad it is when it happens
    reach: int                 # 0–10, how many users/customers are affected
    business_value: int        # 0–10, revenue / compliance / SLA importance
    ease: int                  # 0–10, higher = easier to implement
    confidence: int            # 0–10, automation likelihood
    urgency: int               # 0–10, time pressure (age, SLA, comment velocity)

    # Policy-driven tier assignment. Tier is the primary ranking axis;
    # score_within_tier orders issues inside a tier.
    tier: int                  # 1–4 (1 = Critical, 4 = Deferred)
    tier_reason: str           # human-readable explanation of the tier choice
    score_within_tier: float   # weighted sum used for ordering within a tier

    # Kept for backward compatibility with older stored records and with the
    # rule-based recommend() threshold. Derived from tier + score_within_tier
    # for new records.
    total_score: float
    recommended: bool
    recommendation_reason: str
    priority_rank: int         # 1 = highest priority among the batch


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
# Explicit approval before Scope runs.
# ---------------------------------------------------------------------------

class ApprovalRecord(TypedDict):
    issue_id: int
    approved: bool
    approved_at: Optional[str]   # ISO timestamp; None if not yet approved


# ---------------------------------------------------------------------------
# Stage 3 output — Scope (formerly "Architect")
# Technical implementation plan. No code written.
# ---------------------------------------------------------------------------

class ScopePlan(TypedDict):
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
    scope_status: str               # "pending" | "complete" | "error"
    error: Optional[str]
    scoped_at: str


# Backwards-compatible alias — the old name was ArchitectPlan.
ArchitectPlan = ScopePlan


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
# Carries Scope estimates so the Optimizer can diff them.
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
    # Copied from ScopePlan at dispatch time for Optimizer comparison
    estimated_lines_changed: int
    estimated_files: list


# ---------------------------------------------------------------------------
# Stage 5 output — Optimizer
# Retrospective analysis comparing plan vs. reality.
# ---------------------------------------------------------------------------

class _OptimizationRecordBase(TypedDict):
    # Required on every record, regardless of optimizer_mode.
    issue_id: int
    planned_score: dict             # PlannerScore snapshot
    scope_confidence: int           # confidence_score from ScopePlan
    actual_status: str              # terminal ExecutionSession status
    actual_pr_count: int
    estimation_accuracy: str        # "over" | "under" | "accurate"
    lines_delta: int                # positive = underestimate
    files_delta: int                # actual files - estimated files
    pattern_tags: list              # e.g. ["fast-completion", "confidence-mismatch"]
    optimizer_notes: str
    analyzed_at: str
    optimizer_mode: str             # "rule" | "devin"


class OptimizationRecord(_OptimizationRecordBase, total=False):
    # Devin-powered path only (absent / None for rule-based records).
    session_id: Optional[str]       # Devin session id
    session_url: Optional[str]      # Devin session url
    actual_lines_changed: Optional[int]   # real diff stats from PR
    actual_files_changed: Optional[list]  # real files from PR diff
    failure_root_cause: Optional[str]     # why a session got blocked
