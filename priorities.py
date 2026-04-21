"""
priorities.py — Natural-language prioritization for the Planner.

Turns a short plain-English goal (e.g. "fix the worst bugs affecting users",
"prioritise quick wins this week", "reduce stale backlog") into a
PlannerStrategy — a bundle of scoring weights plus small, deterministic
per-issue score bonuses.

Design goals:
    - Deterministic and explainable. No LLM call on the hot path.
    - Extensible: add a new intent by appending one entry to STRATEGIES and a
      few phrase rows to _PHRASE_RULES.
    - Graceful: vague or empty input falls back to the "balanced" strategy so
      the planner always produces sensible output.

Pipeline usage:
    intent    = parse_prioritization_intent(user_text)
    strategy  = get_strategy(intent)
    planned   = plan_issues(ingested, strategy=strategy)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Strategy definition
# ---------------------------------------------------------------------------

# Standard weight keys consumed by planner.compute_total_score.
_WEIGHT_KEYS = ("user_impact", "business_impact", "effort", "confidence")


@dataclass(frozen=True)
class PlannerStrategy:
    """A named goal profile the Planner can apply when scoring issues."""

    intent: str                     # short machine id, e.g. "worst_bugs"
    label: str                      # UI label, e.g. "Worst bugs"
    summary: str                    # short explanation shown under the input
    weights: dict                   # 4-key weight dict (user_impact, business_impact, effort, confidence)
    bonus_fn: Optional[Callable[[dict], dict]] = None
    # bonus_fn takes an issue dict and returns a sparse dict of score bumps
    # (e.g. {"user_impact": +2}). Bumps are applied before clamping to 0-10.

    def apply_bonuses(self, scores: dict, issue: dict) -> dict:
        """Return a new scores dict with this strategy's bumps applied and clamped."""
        if self.bonus_fn is None:
            return scores
        bumps = self.bonus_fn(issue) or {}
        adjusted = dict(scores)
        for key, delta in bumps.items():
            if key in adjusted:
                adjusted[key] = max(0, min(10, adjusted[key] + delta))
        return adjusted


# ---------------------------------------------------------------------------
# Bonus rule helpers — each takes an issue dict and returns a sparse bump map.
# They stay small, readable, and data-driven so more intents are easy to add.
# ---------------------------------------------------------------------------

_PRIORITY_LABELS = {"p1", "priority-high", "customer-facing", "revenue", "sla", "critical"}
_BUSINESS_LABELS = {"revenue", "billing", "compliance", "customer-facing", "sla", "p1"}


def _bonus_worst_bugs(issue: dict) -> dict:
    bumps: dict = {}
    if issue.get("issue_type") == "bug":
        bumps["user_impact"] = bumps.get("user_impact", 0) + 2
    labels = set(issue.get("labels", []))
    if labels & _PRIORITY_LABELS:
        bumps["user_impact"] = bumps.get("user_impact", 0) + 1
    if issue.get("age_days", 0) > 30 and issue.get("issue_type") == "bug":
        bumps["user_impact"] = bumps.get("user_impact", 0) + 1
    return bumps


def _bonus_quick_wins(issue: dict) -> dict:
    bumps: dict = {}
    if issue.get("complexity") == "low":
        bumps["effort"] = bumps.get("effort", 0) - 2       # lower effort score = easier
        bumps["confidence"] = bumps.get("confidence", 0) + 1
    if issue.get("scope") == "narrow":
        bumps["effort"] = bumps.get("effort", 0) - 1
    return bumps


def _bonus_business_impact(issue: dict) -> dict:
    bumps: dict = {}
    labels = set(issue.get("labels", []))
    if labels & _BUSINESS_LABELS:
        bumps["business_impact"] = bumps.get("business_impact", 0) + 2
    if issue.get("risk") == "high":
        bumps["business_impact"] = bumps.get("business_impact", 0) + 1
    return bumps


def _bonus_stale_cleanup(issue: dict) -> dict:
    bumps: dict = {}
    age = issue.get("age_days", 0)
    if age > 60:
        bumps["user_impact"] = bumps.get("user_impact", 0) + 2
    elif age > 30:
        bumps["user_impact"] = bumps.get("user_impact", 0) + 1
    if issue.get("complexity") == "low" and issue.get("scope") == "narrow":
        bumps["effort"] = bumps.get("effort", 0) - 2
        bumps["confidence"] = bumps.get("confidence", 0) + 1
    return bumps


# ---------------------------------------------------------------------------
# Strategy catalogue
# ---------------------------------------------------------------------------

BALANCED_INTENT = "balanced"

STRATEGIES: dict = {
    BALANCED_INTENT: PlannerStrategy(
        intent=BALANCED_INTENT,
        label="Balanced",
        summary="user impact, business value, effort, and confidence weighted evenly",
        weights={"user_impact": 0.35, "business_impact": 0.25, "effort": 0.20, "confidence": 0.20},
    ),
    "worst_bugs": PlannerStrategy(
        intent="worst_bugs",
        label="Worst bugs",
        summary="high user impact, severity, and urgency",
        weights={"user_impact": 0.50, "business_impact": 0.20, "effort": 0.10, "confidence": 0.20},
        bonus_fn=_bonus_worst_bugs,
    ),
    "quick_wins": PlannerStrategy(
        intent="quick_wins",
        label="Quick wins",
        summary="low effort and high automation confidence — fastest paths to a merged PR",
        weights={"user_impact": 0.15, "business_impact": 0.10, "effort": 0.45, "confidence": 0.30},
        bonus_fn=_bonus_quick_wins,
    ),
    "business_impact": PlannerStrategy(
        intent="business_impact",
        label="Business impact",
        summary="revenue, customer, and compliance impact — even if issues are harder",
        weights={"user_impact": 0.20, "business_impact": 0.55, "effort": 0.05, "confidence": 0.20},
        bonus_fn=_bonus_business_impact,
    ),
    "stale_cleanup": PlannerStrategy(
        intent="stale_cleanup",
        label="Stale cleanup",
        summary="aging issues and low-effort items that are ready to close",
        weights={"user_impact": 0.30, "business_impact": 0.10, "effort": 0.40, "confidence": 0.20},
        bonus_fn=_bonus_stale_cleanup,
    ),
}


def get_strategy(intent: str) -> PlannerStrategy:
    """Return the strategy for an intent, falling back to balanced."""
    return STRATEGIES.get(intent or BALANCED_INTENT, STRATEGIES[BALANCED_INTENT])


# ---------------------------------------------------------------------------
# Intent parser — deterministic keyword/regex matching
# ---------------------------------------------------------------------------

# Each entry: (compiled regex, intent key, weight). Matches are additive so a
# phrase can reinforce a single intent without stealing from another.
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
    """
    Interpret a freeform prioritization goal and return an intent key.

    Scoring is additive per intent; the highest-scoring intent wins. Empty,
    whitespace-only, or ambiguous input (no rule matches) returns the balanced
    default so the planner always produces a sensible ranking.
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

def describe_strategy(intent: str) -> str:
    """A short 'Prioritizing: …' phrase for the UI."""
    strat = get_strategy(intent)
    return f"Prioritizing: {strat.summary}"


def weight_highlights(intent: str, top_n: int = 2) -> list:
    """Return the top N weights as (pretty_label, percent_int) for a compact badge row."""
    strat = get_strategy(intent)
    total = sum(strat.weights.values()) or 1.0
    pretty = {
        "user_impact":     "User Impact",
        "business_impact": "Business Impact",
        "effort":          "Low Effort",
        "confidence":      "Confidence",
    }
    items = sorted(strat.weights.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [(pretty[k], int(round(v / total * 100))) for k, v in items]
