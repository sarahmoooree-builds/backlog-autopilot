"""Unit tests for the rule-based optimizer analysis helpers."""

from __future__ import annotations

from collections import Counter

import pytest

from optimizer import (
    _classify_accuracy,
    _detect_patterns,
    _estimate_files_delta,
    _estimate_lines_delta,
    _generate_notes,
    get_heuristic_recommendations,
)


def _exec(**overrides) -> dict:
    base = {
        "issue_id": 1,
        "status": "Completed",
        "pull_requests": [],
        "estimated_files": [],
    }
    base.update(overrides)
    return base


def _plan(**overrides) -> dict:
    base = {
        "issue_id": 1,
        "confidence_score": 80,
        "affected_files": [],
    }
    base.update(overrides)
    return base


def _planned(**overrides) -> dict:
    base = {
        "id": 1,
        "issue_type": "bug",
        "risk": "low",
        "planner_score": {"effort": 5},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _estimate_lines_delta
# ---------------------------------------------------------------------------

class TestEstimateLinesDelta:
    def test_blocked_adds_30(self):
        assert _estimate_lines_delta(_exec(status="Blocked"), None) == 30

    def test_completed_single_pr_is_zero(self):
        ex = _exec(status="Completed", pull_requests=["p1"])
        assert _estimate_lines_delta(ex, None) == 0

    def test_completed_extra_prs_add_15_each(self):
        ex = _exec(status="Completed", pull_requests=["p1", "p2", "p3"])
        assert _estimate_lines_delta(ex, None) == 30  # (3-1) * 15

    def test_awaiting_review_is_zero(self):
        ex = _exec(status="Awaiting Review")
        assert _estimate_lines_delta(ex, None) == 0


# ---------------------------------------------------------------------------
# _estimate_files_delta
# ---------------------------------------------------------------------------

class TestEstimateFilesDelta:
    def test_returns_zero_without_scope_plan(self):
        assert _estimate_files_delta(_exec(estimated_files=["a.py"]), None) == 0

    def test_positive_when_dispatched_more_than_estimated(self):
        ex = _exec(estimated_files=["a.py", "b.py", "c.py"])
        plan = _plan(affected_files=["a.py"])
        assert _estimate_files_delta(ex, plan) == 2

    def test_negative_when_fewer_files_dispatched(self):
        ex = _exec(estimated_files=["a.py"])
        plan = _plan(affected_files=["a.py", "b.py", "c.py"])
        assert _estimate_files_delta(ex, plan) == -2


# ---------------------------------------------------------------------------
# _classify_accuracy
# ---------------------------------------------------------------------------

class TestClassifyAccuracy:
    def test_blocked_is_under(self):
        assert _classify_accuracy(0, 0, "Blocked") == "under"

    def test_large_positive_lines_delta_is_under(self):
        assert _classify_accuracy(25, 0, "Completed") == "under"

    def test_large_positive_files_delta_is_under(self):
        assert _classify_accuracy(0, 3, "Completed") == "under"

    def test_large_negative_lines_delta_is_over(self):
        assert _classify_accuracy(-25, 0, "Completed") == "over"

    def test_large_negative_files_delta_is_over(self):
        assert _classify_accuracy(0, -3, "Completed") == "over"

    def test_small_deltas_are_accurate(self):
        assert _classify_accuracy(5, 1, "Completed") == "accurate"


# ---------------------------------------------------------------------------
# _detect_patterns
# ---------------------------------------------------------------------------

class TestDetectPatterns:
    def test_auth_false_positive_when_high_risk_completes(self):
        tags = _detect_patterns(
            _exec(status="Completed", pull_requests=["p1"]),
            _plan(),
            _planned(risk="high"),
        )
        assert "auth-false-positive" in tags

    def test_underestimated_scope_tag(self):
        ex = _exec(estimated_files=["a", "b", "c", "d"])
        plan = _plan(affected_files=["a"])
        tags = _detect_patterns(ex, plan, _planned())
        assert "underestimated-scope" in tags

    def test_confidence_mismatch(self):
        tags = _detect_patterns(
            _exec(status="Blocked"),
            _plan(confidence_score=90),
            _planned(),
        )
        assert "confidence-mismatch" in tags

    def test_fast_completion(self):
        tags = _detect_patterns(
            _exec(status="Completed", pull_requests=["p1"]),
            _plan(),
            _planned(),
        )
        assert "fast-completion" in tags

    def test_investigation_leak(self):
        tags = _detect_patterns(
            _exec(status="Awaiting Review"),
            _plan(),
            _planned(issue_type="investigation"),
        )
        assert "investigation-leak" in tags

    def test_low_effort_win(self):
        tags = _detect_patterns(
            _exec(status="Completed", pull_requests=["p1"]),
            _plan(),
            _planned(planner_score={"effort": 2}),
        )
        assert "low-effort-win" in tags

    def test_no_tags_for_quiet_case(self):
        tags = _detect_patterns(
            _exec(status="Awaiting Review"),
            _plan(confidence_score=50),
            _planned(issue_type="bug", planner_score={"effort": 5}),
        )
        assert tags == []


# ---------------------------------------------------------------------------
# _generate_notes
# ---------------------------------------------------------------------------

class TestGenerateNotes:
    def test_fast_completion_note(self):
        notes = _generate_notes(
            _exec(status="Completed"), _plan(), ["fast-completion"]
        )
        assert "Scope estimate was accurate" in notes

    def test_confidence_mismatch_note(self):
        notes = _generate_notes(
            _exec(status="Blocked"),
            _plan(confidence_score=90),
            ["confidence-mismatch"],
        )
        assert "High Scope confidence" in notes
        assert "90/100" in notes

    def test_underestimated_scope_note(self):
        notes = _generate_notes(
            _exec(status="Completed"), _plan(), ["underestimated-scope"]
        )
        assert "More files were touched" in notes

    def test_investigation_leak_note(self):
        notes = _generate_notes(
            _exec(status="Completed"), _plan(), ["investigation-leak"]
        )
        assert "Investigation-type issue" in notes

    def test_low_effort_win_note(self):
        notes = _generate_notes(
            _exec(status="Completed"), _plan(), ["low-effort-win"]
        )
        assert "Low-effort" in notes

    def test_fallback_when_no_patterns(self):
        notes = _generate_notes(
            _exec(status="Awaiting Review"), _plan(), []
        )
        assert "Awaiting Review" in notes
        assert "No notable patterns" in notes


# ---------------------------------------------------------------------------
# get_heuristic_recommendations
# ---------------------------------------------------------------------------

class TestGetHeuristicRecommendations:
    def test_high_under_rate_triggers(self):
        recs = get_heuristic_recommendations(
            total=10,
            accuracy=Counter({"under": 6, "accurate": 3, "over": 1}),
            top_patterns=[],
            avg_confidence=50.0,
            completion_rate=0.7,
        )
        assert any("underestimated" in r.lower() for r in recs)

    def test_high_over_rate_triggers(self):
        recs = get_heuristic_recommendations(
            total=10,
            accuracy=Counter({"over": 6, "accurate": 3, "under": 1}),
            top_patterns=[],
            avg_confidence=50.0,
            completion_rate=0.7,
        )
        assert any("overestimated" in r.lower() for r in recs)

    def test_confidence_mismatch_triggers(self):
        recs = get_heuristic_recommendations(
            total=5,
            accuracy=Counter({"accurate": 5}),
            top_patterns=[("confidence-mismatch", 3)],
            avg_confidence=70.0,
            completion_rate=0.7,
        )
        assert any("confidence mismatch" in r.lower() for r in recs)

    def test_investigation_leak_triggers(self):
        recs = get_heuristic_recommendations(
            total=5,
            accuracy=Counter({"accurate": 5}),
            top_patterns=[("investigation-leak", 1)],
            avg_confidence=70.0,
            completion_rate=0.7,
        )
        assert any("investigation" in r.lower() for r in recs)

    def test_low_completion_rate_triggers(self):
        recs = get_heuristic_recommendations(
            total=5,
            accuracy=Counter({"accurate": 5}),
            top_patterns=[],
            avg_confidence=70.0,
            completion_rate=0.3,
        )
        assert any("completion rate" in r.lower() for r in recs)

    def test_strong_pipeline_triggers_expand(self):
        recs = get_heuristic_recommendations(
            total=5,
            accuracy=Counter({"accurate": 5}),
            top_patterns=[],
            avg_confidence=90.0,
            completion_rate=0.9,
        )
        assert any("expanding automation" in r.lower() for r in recs)

    def test_capped_at_5(self):
        recs = get_heuristic_recommendations(
            total=10,
            accuracy=Counter({"under": 5, "over": 5}),
            top_patterns=[
                ("confidence-mismatch", 3),
                ("low-effort-win", 3),
                ("investigation-leak", 2),
            ],
            avg_confidence=90.0,
            completion_rate=0.9,
        )
        assert len(recs) <= 5

    def test_zero_total_does_not_crash(self):
        # Early callers sometimes pass total=0 when no records exist.
        recs = get_heuristic_recommendations(
            total=0,
            accuracy=Counter(),
            top_patterns=[],
            avg_confidence=0.0,
            completion_rate=0.0,
        )
        assert isinstance(recs, list)
