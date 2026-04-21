"""Unit tests for the rule-based ingest functions.

These cover the pure helpers in ``ingest.py`` — classification, normalisation
and duplicate detection. The Devin-powered code path (``ingest_issues_with_devin``)
is exercised only for its JSON extraction helper in ``test_json_extraction.py``.
"""

from __future__ import annotations

import pytest

from ingest import (
    classify_complexity,
    classify_issue_type,
    classify_scope,
    dedupe_check,
    normalize_issue,
)


# ---------------------------------------------------------------------------
# normalize_issue
# ---------------------------------------------------------------------------

class TestNormalizeIssue:
    def test_strips_nulls_and_whitespace(self):
        raw = {
            "id": 1,
            "title": "  Payment fails  ",
            "description": None,
            "labels": None,
        }
        out = normalize_issue(raw)
        assert out["title"] == "Payment fails"
        assert out["description"] == ""
        assert out["labels"] == []

    def test_lowercases_and_strips_labels(self):
        raw = {"id": 2, "title": "t", "description": "d", "labels": ["Bug", "  P1 "]}
        out = normalize_issue(raw)
        assert out["labels"] == ["bug", "p1"]

    def test_preserves_other_fields(self):
        raw = {
            "id": 3,
            "title": "t",
            "description": "d",
            "labels": [],
            "age_days": 5,
            "comments_count": 2,
        }
        out = normalize_issue(raw)
        assert out["age_days"] == 5
        assert out["comments_count"] == 2
        assert out["id"] == 3

    def test_does_not_mutate_input(self):
        raw = {"id": 4, "title": "  t  ", "description": None, "labels": ["A"]}
        normalize_issue(raw)
        # Original dict untouched
        assert raw["title"] == "  t  "
        assert raw["description"] is None
        assert raw["labels"] == ["A"]


# ---------------------------------------------------------------------------
# classify_issue_type
# ---------------------------------------------------------------------------

def _base_issue(**overrides) -> dict:
    """Build a minimal already-normalised issue dict."""
    issue = {
        "id": 1,
        "title": "",
        "description": "",
        "labels": [],
        "age_days": 0,
        "comments_count": 0,
    }
    issue.update(overrides)
    return issue


class TestClassifyIssueType:
    def test_bug_label(self):
        issue = _base_issue(title="Thing", description="desc", labels=["bug"])
        assert classify_issue_type(issue) == "bug"

    def test_bug_keyword_in_title(self):
        issue = _base_issue(title="App crash on startup", description="")
        assert classify_issue_type(issue) == "bug"

    def test_bug_keyword_in_description(self):
        issue = _base_issue(title="t", description="returns 500 on checkout")
        assert classify_issue_type(issue) == "bug"

    def test_investigation_keyword_beats_feature(self):
        # "add" would trigger feature, but "investigate" takes precedence
        # because INVESTIGATION_KEYWORDS is checked before feature.
        issue = _base_issue(title="Investigate slow checkout", description="add logging")
        assert classify_issue_type(issue) == "investigation"

    def test_feature_request_label(self):
        issue = _base_issue(title="t", description="", labels=["feature-request"])
        assert classify_issue_type(issue) == "feature_request"

    def test_feature_keyword(self):
        issue = _base_issue(title="Support SSO", description="")
        assert classify_issue_type(issue) == "feature_request"

    def test_tech_debt_label(self):
        issue = _base_issue(title="t", description="", labels=["tech-debt"])
        assert classify_issue_type(issue) == "tech_debt"

    def test_other_fallback(self):
        issue = _base_issue(title="Nothing matches here", description="neutral text")
        assert classify_issue_type(issue) == "other"


# ---------------------------------------------------------------------------
# classify_complexity
# ---------------------------------------------------------------------------

class TestClassifyComplexity:
    def test_high_signal_in_description(self):
        issue = _base_issue(
            title="t", description="Needs a refactor of the billing pipeline"
        )
        assert classify_complexity(issue) == "high"

    def test_low_when_short_desc_and_few_comments(self):
        issue = _base_issue(
            title="Fix typo", description="Short desc.", comments_count=1
        )
        assert classify_complexity(issue) == "low"

    def test_medium_when_long_desc(self):
        issue = _base_issue(
            title="t",
            description="x" * 400,
            comments_count=1,
        )
        assert classify_complexity(issue) == "medium"

    def test_medium_when_many_comments(self):
        issue = _base_issue(
            title="t",
            description="short",
            comments_count=10,
        )
        assert classify_complexity(issue) == "medium"


# ---------------------------------------------------------------------------
# classify_scope
# ---------------------------------------------------------------------------

class TestClassifyScope:
    def test_broad_when_signal_matches(self):
        issue = _base_issue(
            title="Rollout across the platform", description=""
        )
        assert classify_scope(issue) == "broad"

    def test_narrow_default(self):
        issue = _base_issue(title="Fix typo in footer", description="")
        assert classify_scope(issue) == "narrow"

    def test_broad_signal_in_description(self):
        issue = _base_issue(
            title="Rewrite module", description="Touches 12 modules"
        )
        assert classify_scope(issue) == "broad"


# ---------------------------------------------------------------------------
# dedupe_check (the "detect_duplicates" logic)
# ---------------------------------------------------------------------------

class TestDedupeCheck:
    def test_detects_near_duplicate(self):
        a = {"id": 1, "title": "Webhook retry crashes on 500"}
        b = {"id": 2, "title": "Webhook retry crashes on 500 errors"}
        assert dedupe_check(a, [a, b]) == 2

    def test_returns_none_when_unique(self):
        a = {"id": 1, "title": "Webhook retry crashes on 500"}
        b = {"id": 2, "title": "Completely unrelated feature request"}
        assert dedupe_check(a, [a, b]) is None

    def test_skips_self_comparison(self):
        a = {"id": 1, "title": "Same title"}
        # Only self in the list — should not match itself.
        assert dedupe_check(a, [a]) is None

    def test_empty_title_does_not_match(self):
        a = {"id": 1, "title": ""}
        b = {"id": 2, "title": ""}
        # Jaccard of two empty sets is 0.0 per the helper.
        assert dedupe_check(a, [a, b]) is None
