"""
priorities.py — Goal-driven prioritization for the Planner.

A PlannerStrategy bundles three things for a given product goal (e.g. "worst
bugs", "quick wins"):

    - ``weights``   — how the 6 enriched scoring dimensions combine into a
                      single ``score_within_tier`` number used for ordering
                      inside a tier.
    - ``tier_fn``   — a deterministic policy that assigns each issue to one of
                      four tiers (1 = Critical, 4 = Deferred) with a short
                      human-readable reason.
    - ``label`` / ``summary`` — UI copy for the goal selector.

Design goals:
    - Deterministic and explainable. No LLM call on the hot path.
    - Extensible: add a new goal by appending one entry to ``STRATEGIES`` and
      writing a ``_tier_<name>`` function.
    - Graceful: unknown intents fall back to "balanced" so the planner always
      produces sensible output.

Pipeline usage:
    strategy = get_strategy(intent)          # e.g. "worst_bugs"
    planned  = plan_issues(ingested, strategy=strategy)

The legacy natural-language parser ``parse_prioritization_intent`` is retained
as a utility for the freeform input path but is no longer on the critical UI
path — goal buttons are the primary selector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Strategy definition
# ---------------------------------------------------------------------------

# Standard weight keys consumed by planner.compute_tier_score.
_WEIGHT_KEYS = ("severity", "reach", "business_value", "ease", "confidence", "urgency")

# A tier function takes (issue_dict, scores_dict) and returns (tier, reason).
TierFn = Callable[[dict, dict], "tuple[int, str]"]


@dataclass(frozen=True)
class PlannerStrategy:
    """A named goal profile the Planner can apply when scoring issues."""

    intent: str                     # short machine id, e.g. "worst_bugs"
    label: str                      # UI label, e.g. "Worst bugs"
    summary: str                    # short explanation shown under the input
    weights: dict                   # 6-key weight dict (see _WEIGHT_KEYS)
    tier_fn: Optional[TierFn] = None  # assigns (tier, reason) per issue


# ---------------------------------------------------------------------------
# Shared label sets used by the tier functions.
# ---------------------------------------------------------------------------

_CRITICAL_LABELS = {"critical", "p1", "priority-high", "sla"}
_BUSINESS_LABELS = {"revenue", "billing", "compliance", "customer-facing", "sla", "p1"}
_CUSTOMER_FACING_LABELS = {"customer-facing", "customer", "ux"}


def _labels_of(issue: dict) -> set:
    return {str(lbl).lower() for lbl in issue.get("labels", [])}


# ---------------------------------------------------------------------------
# Tier assignment policies
#
# Each function returns (tier, reason). Tier 1 is the most critical bucket;
# tier 4 is explicitly deprioritized under this goal.
# ---------------------------------------------------------------------------

def _tier_worst_bugs(issue: dict, scores: dict) -> tuple:
    issue_type = issue.get("issue_type", "other")
    labels = _labels_of(issue)
    comments = issue.get("comments_count", 0)
    age = issue.get("age_days", 0)
    risk = issue.get("risk", "low")

    if issue_type != "bug":
        return 4, "Not a bug — deprioritized under worst-bugs goal"

    if labels & _CRITICAL_LABELS:
        hit = sorted(labels & _CRITICAL_LABELS)[0]
        return 1, f"Bug with '{hit}' label"
    if comments >= 10:
        return 1, f"Bug with {comments} comments (high engagement)"
    if age > 60:
        return 1, f"Bug aging {age} days"
    if risk == "high":
        return 1, "High-risk bug (auth/billing)"

    if comments >= 5:
        return 2, f"Bug with {comments} comments"
    if age > 30:
        return 2, f"Bug aging {age} days"
    if labels & _CUSTOMER_FACING_LABELS:
        return 2, "Customer-facing bug"

    return 3, "Bug"


def _tier_quick_wins(issue: dict, scores: dict) -> tuple:
    issue_type = issue.get("issue_type", "other")
    complexity = issue.get("complexity", "medium")
    scope = issue.get("scope", "narrow")
    confidence = scores.get("confidence", 0)

    if issue_type == "investigation":
        return 4, "Investigation — root cause unknown, not a quick win"
    if complexity == "high":
        return 4, "High complexity — too risky for a quick win"
    if confidence < 3:
        return 4, f"Low automation confidence ({confidence}/10)"

    if complexity == "low" and scope == "narrow" and confidence >= 7:
        return 1, "Low effort, narrow scope, high confidence"
    if complexity in ("low", "medium") and scope == "narrow" and confidence >= 5:
        return 2, "Moderate effort, narrow scope"
    if complexity in ("low", "medium") and scope == "broad":
        return 3, "Broader scope reduces quick-win potential"

    return 3, "Standard effort profile"


def _tier_business_impact(issue: dict, scores: dict) -> tuple:
    issue_type = issue.get("issue_type", "other")
    labels = _labels_of(issue)
    risk = issue.get("risk", "low")
    has_business = bool(labels & _BUSINESS_LABELS)

    if has_business and (risk == "high" or issue_type == "bug"):
        return 1, f"Business-critical {issue_type}"
    if has_business:
        return 2, "Has business label"
    if risk == "high":
        return 2, "High-risk area"

    if issue_type in ("bug", "tech_debt"):
        return 3, "No direct business signal"
    if issue_type in ("feature_request", "investigation"):
        return 4, "No business urgency"

    return 3, "No direct business signal"


def _tier_stale_cleanup(issue: dict, scores: dict) -> tuple:
    age = issue.get("age_days", 0)
    complexity = issue.get("complexity", "medium")
    scope = issue.get("scope", "narrow")
    issue_type = issue.get("issue_type", "other")

    if age <= 30:
        return 4, "Recent — not a cleanup target"

    if age > 90 and complexity in ("low", "medium") and issue_type != "investigation":
        return 1, f"Stale {age}+ days, actionable"
    if age > 60 and complexity in ("low", "medium"):
        return 2, f"Aging {age} days"
    if age > 30 and complexity == "low" and scope == "narrow":
        return 2, "Easy stale item"

    return 3, "Moderately stale"


def _tier_balanced(issue: dict, scores: dict) -> tuple:
    issue_type = issue.get("issue_type", "other")
    labels = _labels_of(issue)
    age = issue.get("age_days", 0)
    confidence = scores.get("confidence", 0)
    duplicate_of = issue.get("duplicate_of")

    if duplicate_of is not None:
        return 4, "Duplicate"
    if issue_type == "investigation":
        return 4, "Investigation — needs human triage"
    if confidence < 3:
        return 4, f"Low automation confidence ({confidence}/10)"

    if issue_type == "bug" and (labels & _CRITICAL_LABELS) and confidence >= 5:
        return 1, "Critical bug with good confidence"

    if issue_type == "bug" and age > 30:
        return 2, f"Established bug ({age} days)"
    if labels & _BUSINESS_LABELS:
        return 2, "Business-relevant"

    return 3, "Standard priority"


# ---------------------------------------------------------------------------
# Strategy catalogue
# ---------------------------------------------------------------------------

BALANCED_INTENT = "balanced"

STRATEGIES: dict = {
    BALANCED_INTENT: PlannerStrategy(
        intent=BALANCED_INTENT,
        label="Balanced",
        summary="Even spread across severity, reach, business value, ease, confidence, and urgency",
        weights={
            "severity":       0.25,
            "reach":          0.20,
            "business_value": 0.20,
            "ease":           0.15,
            "confidence":     0.10,
            "urgency":        0.10,
        },
        tier_fn=_tier_balanced,
    ),
    "worst_bugs": PlannerStrategy(
        intent="worst_bugs",
        label="Worst bugs",
        summary="High-severity bugs by customer visibility, blast radius, and urgency",
        weights={
            "severity":   0.35,
            "reach":      0.25,
            "urgency":    0.25,
            "confidence": 0.15,
        },
        tier_fn=_tier_worst_bugs,
    ),
    "quick_wins": PlannerStrategy(
        intent="quick_wins",
        label="Quick wins",
        summary="Low-effort, high-confidence items you can ship fast",
        weights={
            "ease":       0.40,
            "confidence": 0.35,
            "reach":      0.15,
            "severity":   0.10,
        },
        tier_fn=_tier_quick_wins,
    ),
    "business_impact": PlannerStrategy(
        intent="business_impact",
        label="Business impact",
        summary="Revenue, compliance, and customer trust — even if harder",
        weights={
            "business_value": 0.45,
            "severity":       0.20,
            "reach":          0.20,
            "confidence":     0.15,
        },
        tier_fn=_tier_business_impact,
    ),
    "stale_cleanup": PlannerStrategy(
        intent="stale_cleanup",
        label="Stale cleanup",
        summary="Aging issues and easy closures to reduce backlog debt",
        weights={
            "urgency":    0.35,
            "ease":       0.30,
            "reach":      0.20,
            "confidence": 0.15,
        },
        tier_fn=_tier_stale_cleanup,
    ),
}


def get_strategy(intent: str) -> PlannerStrategy:
    """Return the strategy for an intent, falling back to balanced."""
    return STRATEGIES.get(intent or BALANCED_INTENT, STRATEGIES[BALANCED_INTENT])


# ---------------------------------------------------------------------------
# Intent parser — deterministic keyword/regex matching (legacy freeform path)
#
# Retained as a utility; no longer on the critical UI path. The goal buttons
# in app.py are the primary intent selector now.
# ---------------------------------------------------------------------------

_PHRASE_RULES: list = [
    # worst bugs / user pain
    (r"\b(worst|severe|critical|bad(?:dest)?|serious)\s+(bugs?|defects?|issues?)\b", "worst_bugs", 3),
    (r"\bfix(?:ing)?\s+(?:all\s+)?(?:the\s+)?worst\b", "worst_bugs", 3),
    (r"\b(user|customer)[-\s](impact|facing|pain|hurt)\b", "worst_bugs", 2),
    (r"\bcritical\s+(bugs?|issues?|defects?)\b", "worst_bugs", 3),
    (r"\bhigh[-\s]severity\b", "worst_bugs", 2),
    (r"\burgen(?:t|cy)\b", "worst_bugs", 1),

    # quick wins
    (r"\b(quick|fast|easy|low[-\s]?hanging|low[-\s]effort)\s+(wins?|fixes?|issues?|items?)\b", "quick_wins", 3),
    (r"\bquick[-\s]wins?\b", "quick_wins", 3),
    (r"\b(fast|easy)\s+(wins?|issues?)\b", "quick_wins", 2),
    (r"\bhigh\s+confidence\b", "quick_wins", 1),
    (r"\b(this\s+week|today)\b", "quick_wins", 1),

    # business impact
    (r"\b(business|revenue|customer|compliance|sla|regulatory)\s+(impact|value|risk|priority)\b", "business_impact", 3),
    (r"\bhigh[-\s](business|revenue)\s+(impact|value)\b", "business_impact", 3),
    (r"\brevenue\b", "business_impact", 1),
    (r"\bcompliance\b", "business_impact", 1),
    (r"\bbilling\b", "business_impact", 1),

    # stale cleanup
    (r"\b(stale|old|aging|aged|long[-\s]standing)\s+(backlog|issues?|items?)\b", "stale_cleanup", 3),
    (r"\b(reduce|clean\s*up|clear|trim)\b.*\b(backlog|stale|old)\b", "stale_cleanup", 3),
    (r"\bclean\s*up\b", "stale_cleanup", 2),
    (r"\bbacklog\s+(cleanup|hygiene)\b", "stale_cleanup", 3),
]

_COMPILED_RULES = [(re.compile(pat, re.IGNORECASE), intent, weight)
                   for pat, intent, weight in _PHRASE_RULES]


def parse_prioritization_intent(text: Optional[str]) -> str:
    """Interpret a freeform prioritization goal and return an intent key.

    Empty, whitespace-only, or ambiguous input returns the balanced default
    so the planner always produces a sensible ranking.
    """
    if not text or not text.strip():
        return BALANCED_INTENT

    scores: dict = {}
    for regex, intent, weight in _COMPILED_RULES:
        if regex.search(text):
            scores[intent] = scores.get(intent, 0) + weight

    if not scores:
        return BALANCED_INTENT

    # Deterministic tie-break: highest score, then intent key alphabetical.
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

_PRETTY_DIMENSION = {
    "severity":       "Severity",
    "reach":          "Reach",
    "business_value": "Business Value",
    "ease":           "Ease",
    "confidence":     "Confidence",
    "urgency":        "Urgency",
}


def describe_strategy(intent: str) -> str:
    """A short 'Prioritizing: …' phrase for the UI."""
    strat = get_strategy(intent)
    return f"Prioritizing: {strat.summary}"


def weight_highlights(intent: str, top_n: int = 2) -> list:
    """Return the top N weights as (pretty_label, percent_int) for a compact badge row."""
    strat = get_strategy(intent)
    total = sum(strat.weights.values()) or 1.0
    items = sorted(strat.weights.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [
        (_PRETTY_DIMENSION.get(k, k.replace("_", " ").title()),
         int(round(v / total * 100)))
        for k, v in items
    ]


def goal_dimension_highlights(intent: str) -> list:
    """Return (dimension_name, emphasis_level) pairs for the active intent.

    Used by the UI to describe what a goal prioritizes without surfacing the
    raw weight numbers.
    """
    highlights = {
        "worst_bugs": [
            ("Severity", "primary"), ("Reach", "high"),
            ("Urgency", "high"), ("Confidence", "moderate"),
        ],
        "quick_wins": [
            ("Ease", "primary"), ("Confidence", "high"), ("Reach", "moderate"),
        ],
        "business_impact": [
            ("Business value", "primary"), ("Severity", "high"),
            ("Reach", "moderate"),
        ],
        "stale_cleanup": [
            ("Urgency", "primary"), ("Ease", "high"), ("Reach", "moderate"),
        ],
    }
    return highlights.get(intent, [])
