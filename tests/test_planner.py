"""Unit tests for the rule-based planner scoring and recommendation logic."""

from __future__ import annotations

import pytest

from planner import (
    DEFAULT_WEIGHTS,
    RECOMMEND_THRESHOLD,
    compute_total_score,
    recommend,
    score_business_impact,
    score_confidence,
    score_effort,
    score_user_impact,
)


def _issue(**overrides) -> dict:
    """Build a minimal IngestedIssue-shaped dict for planner scoring."""
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
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# score_user_impact
# ---------------------------------------------------------------------------

class TestScoreUserImpact:
    def test_bug_gets_boost(self):
        assert score_user_impact(_issue(issue_type="bug")) > score_user_impact(
            _issue(issue_type="other")
        )

    def test_investigation_gets_penalty(self):
        assert score_user_impact(_issue(issue_type="investigation")) < 5

    def test_stale_bug_scores_higher(self):
        young = score_user_impact(_issue(issue_type="bug", age_days=1))
        old = score_user_impact(_issue(issue_type="bug", age_days=90))
        assert old > young

    def test_comment_volume_boosts(self):
        quiet = score_user_impact(_issue(issue_type="bug", comments_count=0))
        loud = score_user_impact(_issue(issue_type="bug", comments_count=20))
        assert loud > quiet

    def test_priority_label_boost(self):
        plain = score_user_impact(_issue(issue_type="bug"))
        hot = score_user_impact(_issue(issue_type="bug", labels=["p1"]))
        assert hot > plain

    def test_clamped_between_0_and_10(self):
        extreme = score_user_impact(
            _issue(
                issue_type="bug",
                age_days=500,
                comments_count=500,
                labels=["p1", "critical"],
            )
        )
        assert 0 <= extreme <= 10


# ---------------------------------------------------------------------------
# score_business_impact
# ---------------------------------------------------------------------------

class TestScoreBusinessImpact:
    def test_business_label_boosts(self):
        plain = score_business_impact(_issue())
        revenue = score_business_impact(_issue(labels=["revenue"]))
        assert revenue > plain

    def test_high_risk_boosts(self):
        plain = score_business_impact(_issue())
        hot = score_business_impact(_issue(risk="high"))
        assert hot > plain

    def test_bug_over_tech_debt(self):
        bug = score_business_impact(_issue(issue_type="bug"))
        td = score_business_impact(_issue(issue_type="tech_debt"))
        assert bug > td

    def test_clamped(self):
        extreme = score_business_impact(
            _issue(labels=["revenue", "billing"], risk="high", issue_type="bug")
        )
        assert 0 <= extreme <= 10


# ---------------------------------------------------------------------------
# score_effort (complexity/scope → effort mapping)
# ---------------------------------------------------------------------------

class TestScoreEffort:
    @pytest.mark.parametrize(
        "complexity,scope,expected",
        [
            ("low", "narrow", 2),
            ("low", "broad", 4),
            ("medium", "narrow", 5),
            ("medium", "broad", 7),
            ("high", "narrow", 8),
            ("high", "broad", 9),
        ],
    )
    def test_effort_map(self, complexity, scope, expected):
        assert score_effort(_issue(complexity=complexity, scope=scope)) == expected

    def test_unknown_pair_returns_default(self):
        # An unexpected complexity value should fall back to 5.
        assert score_effort(_issue(complexity="bogus", scope="narrow")) == 5


# ---------------------------------------------------------------------------
# score_confidence
# ---------------------------------------------------------------------------

class TestScoreConfidence:
    def test_high_risk_caps_low(self):
        assert score_confidence(_issue(risk="high")) == 2

    def test_investigation_caps_lowest(self):
        assert score_confidence(_issue(issue_type="investigation")) == 1

    def test_broad_feature_request_caps_low(self):
        assert (
            score_confidence(
                _issue(issue_type="feature_request", scope="broad", complexity="low")
            )
            == 1
        )

    def test_high_complexity_returns_2(self):
        assert score_confidence(_issue(complexity="high")) == 2

    def test_broad_scope_returns_3(self):
        assert (
            score_confidence(
                _issue(issue_type="bug", complexity="low", scope="broad")
            )
            == 3
        )

    def test_easy_bug_maxes_out(self):
        assert (
            score_confidence(
                _issue(issue_type="bug", complexity="low", scope="narrow")
            )
            == 10
        )

    def test_tech_debt_narrow_low(self):
        assert (
            score_confidence(
                _issue(issue_type="tech_debt", complexity="low", scope="narrow")
            )
            == 7
        )


# ---------------------------------------------------------------------------
# compute_total_score
# ---------------------------------------------------------------------------

class TestComputeTotalScore:
    def test_weighted_sum_with_defaults(self):
        scores = {"user_impact": 8, "business_impact": 6, "effort": 2, "confidence": 10}
        total = compute_total_score(scores, DEFAULT_WEIGHTS)
        # Manually: 8*.35 + 6*.25 + (10-2)*.20 + 10*.20 = 2.8 + 1.5 + 1.6 + 2.0 = 7.9
        assert total == pytest.approx(7.9, abs=0.01)
        assert 0 <= total <= 10

    def test_weights_are_normalised(self):
        scores = {"user_impact": 10, "business_impact": 10, "effort": 0, "confidence": 10}
        # Unnormalised weights — should produce the same result as normalised equivalents.
        raw = {"user_impact": 2, "business_impact": 2, "effort": 2, "confidence": 2}
        assert compute_total_score(scores, raw) == pytest.approx(10.0, abs=0.01)

    def test_zero_weights_do_not_crash(self):
        scores = {"user_impact": 5, "business_impact": 5, "effort": 5, "confidence": 5}
        weights = {"user_impact": 0, "business_impact": 0, "effort": 0, "confidence": 0}
        # Falls back to divisor 1.0, so all contributions are 0.
        result = compute_total_score(scores, weights)
        assert result == 0.0

    def test_effort_is_inverted(self):
        hi_effort = compute_total_score(
            {"user_impact": 5, "business_impact": 5, "effort": 10, "confidence": 5},
            DEFAULT_WEIGHTS,
        )
        lo_effort = compute_total_score(
            {"user_impact": 5, "business_impact": 5, "effort": 0, "confidence": 5},
            DEFAULT_WEIGHTS,
        )
        # Lower effort should produce a higher total (because effort is inverted).
        assert lo_effort > hi_effort


# ---------------------------------------------------------------------------
# recommend
# ---------------------------------------------------------------------------

class TestRecommend:
    def test_high_risk_blocked(self):
        ok, reason = recommend(9.5, _issue(risk="high"))
        assert ok is False
        assert "High-risk" in reason

    def test_investigation_blocked(self):
        ok, reason = recommend(9.5, _issue(issue_type="investigation"))
        assert ok is False
        assert "investigation" in reason.lower()

    def test_below_threshold_blocked(self):
        score = RECOMMEND_THRESHOLD - 0.1
        ok, reason = recommend(score, _issue(issue_type="bug"))
        assert ok is False
        assert "below" in reason.lower() or "threshold" in reason.lower()

    def test_recommended_when_above_threshold(self):
        ok, reason = recommend(RECOMMEND_THRESHOLD + 0.5, _issue(issue_type="bug"))
        assert ok is True
        assert "good automation candidate" in reason

    def test_exactly_at_threshold_is_recommended(self):
        ok, _ = recommend(RECOMMEND_THRESHOLD, _issue(issue_type="bug"))
        assert ok is True
