"""Unit tests for the rule-based planner scoring and recommendation logic."""

from __future__ import annotations

import pytest

from planner import (
    DEFAULT_WEIGHTS,
    _build_devin_planner_score,
    apply_refinement,
    compute_tier_score,
    compute_total_score,
    migrate_legacy_score,
    plan_issues,
    recommend,
    rescore_with_strategy,
    score_business_value,
    score_confidence,
    score_ease,
    score_reach,
    score_severity,
    score_urgency,
)
from priorities import get_strategy


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
        "duplicate_of": None,
        "summary": "",
        "ingested_at": "2026-04-21T00:00:00",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# score_severity
# ---------------------------------------------------------------------------

class TestScoreSeverity:
    def test_bug_scores_higher_than_other(self):
        assert score_severity(_issue(issue_type="bug")) > score_severity(
            _issue(issue_type="other")
        )

    def test_critical_label_adds_two(self):
        baseline = score_severity(_issue(issue_type="bug"))
        with_label = score_severity(_issue(issue_type="bug", labels=["critical"]))
        assert with_label == min(10, baseline + 2)

    def test_blocking_keyword_raises_score(self):
        baseline = score_severity(_issue(issue_type="bug", description="sometimes fails"))
        boosted = score_severity(
            _issue(issue_type="bug", description="this blocks signup flow")
        )
        assert boosted > baseline

    def test_data_loss_keyword_raises_score(self):
        baseline = score_severity(_issue(issue_type="bug", description="minor"))
        boosted = score_severity(_issue(issue_type="bug", description="data loss seen"))
        assert boosted > baseline

    def test_feature_request_penalty(self):
        assert score_severity(_issue(issue_type="feature_request")) < score_severity(
            _issue(issue_type="other")
        )

    def test_clamped_to_0_10(self):
        loaded = _issue(
            issue_type="bug",
            labels=["critical", "p1", "sla"],
            risk="high",
            description="data loss blocks every user",
        )
        assert 0 <= score_severity(loaded) <= 10


# ---------------------------------------------------------------------------
# score_reach
# ---------------------------------------------------------------------------

class TestScoreReach:
    def test_many_comments_boost(self):
        low = score_reach(_issue(comments_count=0))
        high = score_reach(_issue(comments_count=12))
        assert high > low

    def test_customer_facing_label_boosts(self):
        with_label = score_reach(_issue(labels=["customer-facing"]))
        without = score_reach(_issue(labels=[]))
        assert with_label > without

    def test_widespread_keyword_boosts(self):
        baseline = score_reach(_issue(description="single occurrence"))
        boosted = score_reach(_issue(description="affects all users of the app"))
        assert boosted > baseline

    def test_aging_issue_boosts_reach(self):
        fresh = score_reach(_issue(age_days=5))
        stale = score_reach(_issue(age_days=120))
        assert stale > fresh


# ---------------------------------------------------------------------------
# score_business_value
# ---------------------------------------------------------------------------

class TestScoreBusinessValue:
    def test_business_label_boosts(self):
        without = score_business_value(_issue(labels=[]))
        with_label = score_business_value(_issue(labels=["revenue"]))
        assert with_label > without

    def test_high_risk_boosts(self):
        baseline = score_business_value(_issue(risk="low"))
        boosted = score_business_value(_issue(risk="high"))
        assert boosted > baseline

    def test_bug_vs_feature(self):
        bug = score_business_value(_issue(issue_type="bug"))
        feature = score_business_value(_issue(issue_type="feature_request"))
        assert bug >= feature


# ---------------------------------------------------------------------------
# score_ease
# ---------------------------------------------------------------------------

class TestScoreEase:
    def test_low_narrow_is_easiest(self):
        assert score_ease(_issue(complexity="low", scope="narrow")) == 9

    def test_high_broad_is_hardest(self):
        assert score_ease(_issue(complexity="high", scope="broad")) == 1

    def test_known_fix_path_boosts(self):
        baseline = score_ease(_issue(complexity="medium", scope="narrow", description="tricky"))
        boosted = score_ease(
            _issue(complexity="medium", scope="narrow",
                   description="see retry.py line 42")
        )
        assert boosted > baseline

    def test_higher_is_easier_no_inversion(self):
        # ease is NOT inverted — high numbers mean easy.
        easy = score_ease(_issue(complexity="low", scope="narrow"))
        hard = score_ease(_issue(complexity="high", scope="broad"))
        assert easy > hard


# ---------------------------------------------------------------------------
# score_confidence (decision tree kept from the previous implementation)
# ---------------------------------------------------------------------------

class TestScoreConfidence:
    def test_high_risk_caps_confidence_low(self):
        assert score_confidence(_issue(risk="high")) <= 2

    def test_investigation_caps_confidence_low(self):
        assert score_confidence(_issue(issue_type="investigation")) <= 3

    def test_easy_bug_maxes_out(self):
        assert score_confidence(
            _issue(issue_type="bug", complexity="low", scope="narrow")
        ) == 10


# ---------------------------------------------------------------------------
# score_urgency
# ---------------------------------------------------------------------------

class TestScoreUrgency:
    def test_older_is_more_urgent(self):
        fresh = score_urgency(_issue(age_days=5))
        stale = score_urgency(_issue(age_days=120))
        assert stale > fresh

    def test_sla_label_boosts(self):
        without = score_urgency(_issue(age_days=10, labels=[]))
        with_sla = score_urgency(_issue(age_days=10, labels=["sla"]))
        assert with_sla > without

    def test_zero_age_does_not_crash(self):
        # guard against div-by-zero in the comment-velocity proxy
        assert 0 <= score_urgency(_issue(age_days=0, comments_count=3)) <= 10


# ---------------------------------------------------------------------------
# compute_tier_score
# ---------------------------------------------------------------------------

class TestComputeTierScore:
    def test_weighted_sum_no_inversion(self):
        scores = {
            "severity": 10, "reach": 10, "business_value": 10,
            "ease": 10, "confidence": 10, "urgency": 10,
        }
        assert compute_tier_score(scores, DEFAULT_WEIGHTS) == pytest.approx(10.0, abs=0.01)

    def test_zeros_produce_zero(self):
        scores = {k: 0 for k in
                  ("severity", "reach", "business_value", "ease", "confidence", "urgency")}
        assert compute_tier_score(scores, DEFAULT_WEIGHTS) == 0.0

    def test_missing_dimension_defaults_to_zero(self):
        # Absent keys must not KeyError — they count as 0 in the weighted sum.
        total = compute_tier_score({"severity": 10}, DEFAULT_WEIGHTS)
        assert 0 <= total <= 10


# ---------------------------------------------------------------------------
# recommend — tier-aware
# ---------------------------------------------------------------------------

class TestRecommend:
    def test_high_risk_blocked_regardless_of_tier(self):
        ok, reason = recommend(1, {"confidence": 10, "ease": 10},
                               _issue(risk="high"))
        assert ok is False
        assert "High-risk" in reason

    def test_investigation_blocked_regardless_of_tier(self):
        ok, _ = recommend(1, {"confidence": 10, "ease": 10},
                          _issue(issue_type="investigation"))
        assert ok is False

    def test_tier1_recommended(self):
        ok, _ = recommend(1, {"confidence": 8, "ease": 7}, _issue(issue_type="bug"))
        assert ok is True

    def test_tier2_recommended(self):
        ok, _ = recommend(2, {"confidence": 5, "ease": 5}, _issue(issue_type="bug"))
        assert ok is True

    def test_tier1_blocked_when_confidence_low(self):
        ok, _ = recommend(1, {"confidence": 2, "ease": 10}, _issue(issue_type="bug"))
        assert ok is False

    def test_tier3_recommended_when_easy_and_confident(self):
        ok, _ = recommend(3, {"confidence": 6, "ease": 7}, _issue(issue_type="bug"))
        assert ok is True

    def test_tier3_not_recommended_when_uncertain(self):
        ok, _ = recommend(3, {"confidence": 3, "ease": 3}, _issue(issue_type="bug"))
        assert ok is False

    def test_tier4_never_recommended(self):
        ok, _ = recommend(4, {"confidence": 10, "ease": 10}, _issue(issue_type="bug"))
        assert ok is False


# ---------------------------------------------------------------------------
# plan_issues — end-to-end on a small batch
# ---------------------------------------------------------------------------

class TestPlanIssues:
    def test_produces_new_planner_score_fields(self):
        issues = [_issue(id=1, issue_type="bug", labels=["critical"], age_days=10,
                         complexity="low", scope="narrow")]
        planned = plan_issues(issues, strategy=get_strategy("worst_bugs"))
        assert len(planned) == 1
        ps = planned[0]["planner_score"]
        for key in ("severity", "reach", "business_value", "ease", "confidence",
                    "urgency", "tier", "tier_reason", "score_within_tier",
                    "total_score", "recommended", "priority_rank"):
            assert key in ps, f"missing {key}"

    def test_sort_is_by_tier_then_score(self):
        issues = [
            # Non-bug → tier 4 under worst_bugs
            _issue(id=1, issue_type="feature_request", age_days=5),
            # Critical bug → tier 1
            _issue(id=2, issue_type="bug", labels=["critical"], age_days=60,
                   complexity="low", scope="narrow"),
            # Ordinary bug → tier 2 or 3
            _issue(id=3, issue_type="bug", age_days=40, complexity="medium",
                   scope="narrow"),
        ]
        planned = plan_issues(issues, strategy=get_strategy("worst_bugs"))
        tiers = [p["planner_score"]["tier"] for p in planned]
        assert tiers == sorted(tiers), "plan_issues must return tier-ascending order"
        assert planned[0]["id"] == 2, "critical bug should rank first"
        assert planned[-1]["id"] == 1, "non-bug should rank last under worst_bugs"

    def test_priority_rank_is_1_indexed(self):
        issues = [_issue(id=i, issue_type="bug") for i in range(1, 4)]
        planned = plan_issues(issues, strategy=get_strategy("balanced"))
        ranks = [p["planner_score"]["priority_rank"] for p in planned]
        assert ranks == [1, 2, 3]

    def test_legacy_fields_populated_for_backward_compat(self):
        issues = [_issue(id=1, issue_type="bug", complexity="low", scope="narrow")]
        planned = plan_issues(issues, strategy=get_strategy("balanced"))
        ps = planned[0]["planner_score"]
        # Legacy keys still populated so the old UI path renders safely.
        assert "user_impact" in ps
        assert "business_impact" in ps
        assert "effort" in ps
        assert ps["effort"] == max(0, min(10, 10 - ps["ease"]))


# ---------------------------------------------------------------------------
# apply_refinement
# ---------------------------------------------------------------------------

class TestApplyRefinement:
    def _planned_issue(self, title, **overrides) -> dict:
        base = {
            "id": overrides.get("id", 1),
            "title": title,
            "description": overrides.get("description", ""),
            "labels": overrides.get("labels", []),
            "planner_score": {
                "tier": 2,
                "score_within_tier": 5.0,
                "priority_rank": 0,
            },
        }
        return base

    def test_empty_refinement_is_noop(self):
        issues = [self._planned_issue("one"), self._planned_issue("two", id=2)]
        before = [i["planner_score"]["score_within_tier"] for i in issues]
        apply_refinement(issues, "")
        after = [i["planner_score"]["score_within_tier"] for i in issues]
        assert before == after

    def test_matching_keyword_boosts_score(self):
        issues = [
            self._planned_issue("Onboarding is broken", id=1),
            self._planned_issue("Pricing page typo", id=2),
        ]
        apply_refinement(issues, "onboarding")
        boosted = next(i for i in issues if i["id"] == 1)
        assert boosted["planner_score"]["score_within_tier"] > 5.0

    def test_refinement_reorders_priority_rank(self):
        issues = [
            self._planned_issue("Pricing page typo", id=1),
            self._planned_issue("Onboarding flow broken", id=2),
        ]
        # Both start at score_within_tier=5.0, but id=1 is first.
        apply_refinement(issues, "onboarding")
        # The boosted issue should now have priority_rank 1.
        by_id = {i["id"]: i for i in issues}
        assert by_id[2]["planner_score"]["priority_rank"] == 1

    def test_boost_is_capped(self):
        issues = [self._planned_issue("onboarding onboarding onboarding onboarding",
                                      id=1)]
        apply_refinement(issues, "onboarding onboarding onboarding onboarding")
        # Multiple keyword hits cap at +1.5.
        assert issues[0]["planner_score"]["score_within_tier"] <= 6.5 + 0.01


# ---------------------------------------------------------------------------
# migrate_legacy_score — backward compatibility
# ---------------------------------------------------------------------------

class TestMigrateLegacyScore:
    def test_legacy_score_gets_new_fields(self):
        legacy = {
            "user_impact": 7,
            "business_impact": 6,
            "effort": 3,
            "confidence": 8,
            "total_score": 7.5,
            "recommended": True,
            "recommendation_reason": "Score 7.5/10 — good automation candidate",
            "priority_rank": 1,
        }
        migrated = migrate_legacy_score(legacy)
        assert migrated["severity"] == 7
        assert migrated["business_value"] == 6
        assert migrated["ease"] == 7   # 10 - effort
        assert migrated["confidence"] == 8
        assert migrated["tier"] == 3
        assert migrated["tier_reason"]
        assert migrated["score_within_tier"] == 7.5
        # Original fields still present
        assert migrated["user_impact"] == 7
        assert migrated["total_score"] == 7.5

    def test_already_migrated_is_idempotent(self):
        new = {
            "severity": 7, "reach": 5, "business_value": 6, "ease": 8,
            "confidence": 9, "urgency": 4, "tier": 1,
            "tier_reason": "Critical bug", "score_within_tier": 8.1,
            "total_score": 8.1, "recommended": True,
            "recommendation_reason": "", "priority_rank": 1,
        }
        result = migrate_legacy_score(dict(new))
        for k, v in new.items():
            assert result[k] == v

    def test_empty_dict_becomes_defaults(self):
        migrated = migrate_legacy_score({})
        expected_keys = (
            # new 6-dim
            "severity", "reach", "business_value", "ease", "confidence", "urgency",
            # tier policy
            "tier", "tier_reason", "score_within_tier",
            # legacy meta
            "total_score", "recommended", "recommendation_reason", "priority_rank",
            # legacy dimension keys still read by the old UI card path
            "user_impact", "business_impact", "effort",
        )
        for k in expected_keys:
            assert k in migrated, f"missing {k}"

    def test_migrated_dict_is_sortable_by_total_score(self):
        # Regression: stored records with null/empty planner_score must not
        # KeyError when load_and_plan sorts by planner_score["total_score"].
        records = [
            {"planner_score": migrate_legacy_score({})},
            {"planner_score": migrate_legacy_score({"total_score": 6.0,
                                                    "user_impact": 7})},
        ]
        records.sort(key=lambda r: r["planner_score"]["total_score"],
                     reverse=True)
        assert records[0]["planner_score"]["total_score"] == 6.0

    def test_non_dict_is_returned_unchanged(self):
        assert migrate_legacy_score(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compute_total_score — deprecated legacy weighted sum kept for back-compat
# ---------------------------------------------------------------------------

class TestBuildDevinPlannerScore:
    """Devin-produced records must sort consistently with rule-based ones."""

    def test_tier1_total_score_beats_tier3_regardless_of_devin_value(self):
        # Devin returns a tier-1 item with a low total_score and a tier-3 item
        # with a higher total_score. After we derive total_score from tier,
        # the tier-1 item MUST still sort above the tier-3 item.
        tier1 = _build_devin_planner_score(
            {"tier": 1, "score_within_tier": 5.0, "total_score": 2.0}
        )
        tier3 = _build_devin_planner_score(
            {"tier": 3, "score_within_tier": 9.0, "total_score": 9.0}
        )
        assert tier1["total_score"] > tier3["total_score"]

    def test_missing_total_score_is_derived(self):
        ps = _build_devin_planner_score({"tier": 1, "score_within_tier": 8.0})
        # A tier-1 item with any positive score_within_tier should beat a
        # tier-4 record with score_within_tier=10.
        tier4 = _build_devin_planner_score(
            {"tier": 4, "score_within_tier": 10.0}
        )
        assert ps["total_score"] > tier4["total_score"]

    def test_legacy_effort_is_inverted_to_ease(self):
        ps = _build_devin_planner_score({"effort": 2})
        assert ps["ease"] == 8
        # Legacy proxy `effort` is also re-populated for backward-compat UI.
        assert ps["effort"] == 2


class TestComputeTotalScoreLegacy:
    def test_legacy_weighted_sum_still_works(self):
        legacy_scores = {"user_impact": 8, "business_impact": 6, "effort": 2,
                         "confidence": 10}
        legacy_weights = {"user_impact": 0.35, "business_impact": 0.25,
                          "effort": 0.20, "confidence": 0.20}
        # 8*.35 + 6*.25 + (10-2)*.20 + 10*.20 = 7.9
        total = compute_total_score(legacy_scores, legacy_weights)
        assert total == pytest.approx(7.9, abs=0.01)


# ---------------------------------------------------------------------------
# rescore_with_strategy — Devin-path goal switching
# ---------------------------------------------------------------------------

_PLANNER_SCORE_KEYS = {
    "severity", "reach", "business_value", "ease", "confidence", "urgency",
    "tier",
}


def _planned_issue(**overrides) -> dict:
    """Build a minimal PlannedIssue-shaped dict for rescore_with_strategy tests.

    Keyword args that look like planner_score dimensions (severity, reach,
    business_value, ease, confidence, urgency, tier) go into planner_score;
    everything else goes on the top-level issue dict. Mirrors the pattern used
    by the existing ``_issue()`` helper.
    """
    score_overrides = {k: overrides.pop(k) for k in list(overrides)
                       if k in _PLANNER_SCORE_KEYS}
    issue = _issue(**overrides)
    issue["planner_score"] = {
        "severity":              score_overrides.get("severity", 5),
        "reach":                 score_overrides.get("reach", 5),
        "business_value":        score_overrides.get("business_value", 5),
        "ease":                  score_overrides.get("ease", 5),
        "confidence":            score_overrides.get("confidence", 5),
        "urgency":               score_overrides.get("urgency", 5),
        "tier":                  score_overrides.get("tier", 3),
        "tier_reason":           "",
        "score_within_tier":     0.0,
        "total_score":           0.0,
        "recommended":           False,
        "recommendation_reason": "",
        "priority_rank":         0,
    }
    return issue


class TestRescoreWithStrategy:
    def test_worst_bugs_demotes_non_bugs(self):
        """Non-bug issues should be tier 4 under worst_bugs."""
        issues = [_planned_issue(issue_type="feature_request", tier=2)]
        strategy = get_strategy("worst_bugs")
        rescore_with_strategy(issues, strategy)
        assert issues[0]["planner_score"]["tier"] == 4

    def test_quick_wins_promotes_easy_issues(self):
        """Low-complexity narrow-scope issues should be tier 1 under quick_wins."""
        issues = [_planned_issue(issue_type="bug", complexity="low",
                                 scope="narrow", confidence=8, tier=3)]
        strategy = get_strategy("quick_wins")
        rescore_with_strategy(issues, strategy)
        assert issues[0]["planner_score"]["tier"] == 1

    def test_different_goals_produce_different_order(self):
        """Switching goals should reorder the list."""
        bug = _planned_issue(id=1, issue_type="bug", complexity="high",
                             labels=["critical"])
        easy = _planned_issue(id=2, issue_type="tech_debt", complexity="low",
                              scope="narrow")

        issues_a = [dict(bug), dict(easy)]
        rescore_with_strategy(issues_a, get_strategy("worst_bugs"))
        order_a = [i["id"] for i in issues_a]

        issues_b = [dict(bug), dict(easy)]
        rescore_with_strategy(issues_b, get_strategy("quick_wins"))
        order_b = [i["id"] for i in issues_b]

        assert order_a != order_b

    def test_preserves_dimension_scores(self):
        """Dimension scores (severity, reach, etc.) should not be overwritten."""
        issues = [_planned_issue(severity=9, reach=2)]
        rescore_with_strategy(issues, get_strategy("worst_bugs"))
        assert issues[0]["planner_score"]["severity"] == 9
        assert issues[0]["planner_score"]["reach"] == 2
