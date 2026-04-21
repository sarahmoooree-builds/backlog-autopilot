"""Unit tests for the tier-assignment policies and goal strategies."""

from __future__ import annotations

import pytest

from priorities import (
    BALANCED_INTENT,
    STRATEGIES,
    _tier_balanced,
    _tier_business_impact,
    _tier_quick_wins,
    _tier_stale_cleanup,
    _tier_worst_bugs,
    describe_strategy,
    get_strategy,
    goal_dimension_highlights,
    parse_prioritization_intent,
    weight_highlights,
)


def _issue(**overrides) -> dict:
    base = {
        "id": 1,
        "title": "t",
        "description": "d",
        "labels": [],
        "age_days": 0,
        "comments_count": 0,
        "issue_type": "other",
        "complexity": "medium",
        "scope": "narrow",
        "risk": "low",
        "duplicate_of": None,
    }
    base.update(overrides)
    return base


def _scores(**overrides) -> dict:
    base = {
        "severity": 5, "reach": 5, "business_value": 5,
        "ease": 5, "confidence": 5, "urgency": 5,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _tier_worst_bugs
# ---------------------------------------------------------------------------

class TestTierWorstBugs:
    def test_non_bug_is_tier4(self):
        tier, reason = _tier_worst_bugs(_issue(issue_type="feature_request"), _scores())
        assert tier == 4
        assert "not a bug" in reason.lower()

    def test_bug_with_critical_label_is_tier1(self):
        tier, reason = _tier_worst_bugs(
            _issue(issue_type="bug", labels=["critical"]), _scores()
        )
        assert tier == 1
        assert "critical" in reason.lower()

    def test_bug_with_many_comments_is_tier1(self):
        tier, _ = _tier_worst_bugs(
            _issue(issue_type="bug", comments_count=15), _scores()
        )
        assert tier == 1

    def test_old_bug_is_tier1(self):
        tier, _ = _tier_worst_bugs(_issue(issue_type="bug", age_days=90), _scores())
        assert tier == 1

    def test_high_risk_bug_is_tier1(self):
        tier, _ = _tier_worst_bugs(
            _issue(issue_type="bug", risk="high"), _scores()
        )
        assert tier == 1

    def test_moderate_bug_is_tier2(self):
        tier, _ = _tier_worst_bugs(
            _issue(issue_type="bug", comments_count=6), _scores()
        )
        assert tier == 2

    def test_plain_bug_is_tier3(self):
        tier, _ = _tier_worst_bugs(_issue(issue_type="bug"), _scores())
        assert tier == 3


# ---------------------------------------------------------------------------
# _tier_quick_wins
# ---------------------------------------------------------------------------

class TestTierQuickWins:
    def test_easy_and_confident_is_tier1(self):
        tier, _ = _tier_quick_wins(
            _issue(complexity="low", scope="narrow"),
            _scores(confidence=8),
        )
        assert tier == 1

    def test_moderate_is_tier2(self):
        tier, _ = _tier_quick_wins(
            _issue(complexity="medium", scope="narrow"),
            _scores(confidence=6),
        )
        assert tier == 2

    def test_broad_scope_is_tier3(self):
        tier, _ = _tier_quick_wins(
            _issue(complexity="low", scope="broad"),
            _scores(confidence=8),
        )
        assert tier == 3

    def test_high_complexity_is_tier4(self):
        tier, _ = _tier_quick_wins(
            _issue(complexity="high"), _scores(confidence=8),
        )
        assert tier == 4

    def test_investigation_is_tier4(self):
        tier, _ = _tier_quick_wins(
            _issue(issue_type="investigation"), _scores(confidence=10),
        )
        assert tier == 4

    def test_low_confidence_is_tier4(self):
        tier, _ = _tier_quick_wins(
            _issue(complexity="low", scope="narrow"), _scores(confidence=1),
        )
        assert tier == 4


# ---------------------------------------------------------------------------
# _tier_business_impact
# ---------------------------------------------------------------------------

class TestTierBusinessImpact:
    def test_business_bug_is_tier1(self):
        tier, _ = _tier_business_impact(
            _issue(issue_type="bug", labels=["revenue"]), _scores()
        )
        assert tier == 1

    def test_business_high_risk_is_tier1(self):
        tier, _ = _tier_business_impact(
            _issue(issue_type="feature_request", labels=["sla"], risk="high"),
            _scores(),
        )
        assert tier == 1

    def test_business_label_only_is_tier2(self):
        tier, _ = _tier_business_impact(
            _issue(issue_type="tech_debt", labels=["compliance"]), _scores()
        )
        assert tier == 2

    def test_high_risk_without_label_is_tier2(self):
        tier, _ = _tier_business_impact(
            _issue(issue_type="bug", risk="high"), _scores()
        )
        # Bug + risk=high gets tier 2 (no business label → not "business-critical")
        assert tier == 2

    def test_feature_without_business_is_tier4(self):
        tier, _ = _tier_business_impact(
            _issue(issue_type="feature_request"), _scores()
        )
        assert tier == 4

    def test_bug_without_business_is_tier3(self):
        tier, _ = _tier_business_impact(_issue(issue_type="bug"), _scores())
        assert tier == 3


# ---------------------------------------------------------------------------
# _tier_stale_cleanup
# ---------------------------------------------------------------------------

class TestTierStaleCleanup:
    def test_recent_is_tier4(self):
        tier, _ = _tier_stale_cleanup(_issue(age_days=10), _scores())
        assert tier == 4

    def test_very_old_actionable_is_tier1(self):
        tier, _ = _tier_stale_cleanup(
            _issue(age_days=120, complexity="low", issue_type="bug"), _scores()
        )
        assert tier == 1

    def test_investigation_at_90_days_not_tier1(self):
        tier, _ = _tier_stale_cleanup(
            _issue(age_days=120, complexity="low", issue_type="investigation"),
            _scores(),
        )
        assert tier != 1

    def test_aging_is_tier2(self):
        tier, _ = _tier_stale_cleanup(
            _issue(age_days=75, complexity="medium"), _scores()
        )
        assert tier == 2

    def test_moderately_stale_is_tier3(self):
        tier, _ = _tier_stale_cleanup(
            _issue(age_days=45, complexity="high", scope="broad"), _scores()
        )
        assert tier == 3


# ---------------------------------------------------------------------------
# _tier_balanced
# ---------------------------------------------------------------------------

class TestTierBalanced:
    def test_critical_bug_is_tier1(self):
        tier, _ = _tier_balanced(
            _issue(issue_type="bug", labels=["critical"]), _scores(confidence=7),
        )
        assert tier == 1

    def test_aging_bug_is_tier2(self):
        tier, _ = _tier_balanced(
            _issue(issue_type="bug", age_days=60), _scores(confidence=5),
        )
        assert tier == 2

    def test_business_label_is_tier2(self):
        tier, _ = _tier_balanced(
            _issue(issue_type="tech_debt", labels=["revenue"]), _scores(confidence=5),
        )
        assert tier == 2

    def test_investigation_is_tier4(self):
        tier, _ = _tier_balanced(
            _issue(issue_type="investigation"), _scores(confidence=10),
        )
        assert tier == 4

    def test_low_confidence_is_tier4(self):
        tier, _ = _tier_balanced(_issue(issue_type="bug"), _scores(confidence=1))
        assert tier == 4

    def test_duplicate_is_tier4(self):
        tier, _ = _tier_balanced(
            _issue(issue_type="bug", duplicate_of=42), _scores(confidence=8),
        )
        assert tier == 4

    def test_standard_is_tier3(self):
        tier, _ = _tier_balanced(
            _issue(issue_type="tech_debt"), _scores(confidence=5),
        )
        assert tier == 3


# ---------------------------------------------------------------------------
# STRATEGIES catalogue + get_strategy
# ---------------------------------------------------------------------------

class TestStrategies:
    def test_all_strategies_have_tier_fn(self):
        for intent, strat in STRATEGIES.items():
            assert strat.tier_fn is not None, f"{intent} missing tier_fn"
            assert strat.intent == intent
            assert strat.label
            assert strat.summary

    def test_all_weights_use_new_dimensions(self):
        allowed = {"severity", "reach", "business_value",
                   "ease", "confidence", "urgency"}
        for intent, strat in STRATEGIES.items():
            for key in strat.weights:
                assert key in allowed, f"{intent} has unknown weight key {key}"

    def test_weights_sum_roughly_to_one(self):
        for intent, strat in STRATEGIES.items():
            total = sum(strat.weights.values())
            assert 0.95 <= total <= 1.05, f"{intent} weights sum to {total}"

    def test_unknown_intent_falls_back_to_balanced(self):
        assert get_strategy("does-not-exist").intent == BALANCED_INTENT
        assert get_strategy(None).intent == BALANCED_INTENT  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# goal_dimension_highlights + weight_highlights
# ---------------------------------------------------------------------------

class TestGoalDimensionHighlights:
    def test_known_intents_return_highlights(self):
        for intent in ("worst_bugs", "quick_wins", "business_impact",
                       "stale_cleanup"):
            highlights = goal_dimension_highlights(intent)
            assert highlights, f"no highlights for {intent}"
            for label, emphasis in highlights:
                assert isinstance(label, str) and label
                assert emphasis in {"primary", "high", "moderate"}

    def test_balanced_returns_empty(self):
        assert goal_dimension_highlights(BALANCED_INTENT) == []


class TestWeightHighlights:
    def test_uses_pretty_dimension_names(self):
        items = weight_highlights("worst_bugs", top_n=2)
        labels = {label for label, _ in items}
        assert "Severity" in labels or "Reach" in labels

    def test_returns_percentages(self):
        items = weight_highlights("quick_wins", top_n=2)
        for _, pct in items:
            assert 0 <= pct <= 100


# ---------------------------------------------------------------------------
# parse_prioritization_intent — legacy freeform path still works
# ---------------------------------------------------------------------------

class TestParseIntent:
    def test_empty_returns_balanced(self):
        assert parse_prioritization_intent("") == BALANCED_INTENT
        assert parse_prioritization_intent(None) == BALANCED_INTENT
        assert parse_prioritization_intent("   ") == BALANCED_INTENT

    def test_worst_bugs_phrase(self):
        assert parse_prioritization_intent("fix the worst bugs today") == "worst_bugs"

    def test_quick_wins_phrase(self):
        assert parse_prioritization_intent("show me quick wins") == "quick_wins"

    def test_business_impact_phrase(self):
        assert parse_prioritization_intent(
            "what has the highest business impact"
        ) == "business_impact"

    def test_stale_cleanup_phrase(self):
        assert parse_prioritization_intent("clean up the stale backlog") == "stale_cleanup"


# ---------------------------------------------------------------------------
# describe_strategy
# ---------------------------------------------------------------------------

def test_describe_strategy_includes_summary():
    text = describe_strategy("worst_bugs")
    assert text.startswith("Prioritizing: ")
    assert "severity" in text.lower() or "bug" in text.lower()
