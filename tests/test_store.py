"""Unit tests for the SQLite-backed persistence layer in ``store.py``.

All tests use the ``temp_store`` fixture which points the module at a fresh,
empty SQLite file inside ``tmp_path`` — no real ``pipeline_store.db`` is
ever touched.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Generic CRUD
# ---------------------------------------------------------------------------

class TestGenericCrud:
    def test_set_and_get_record(self, temp_store):
        temp_store.set_record("ingested", 42, {"title": "foo"})
        assert temp_store.get_record("ingested", 42) == {"title": "foo"}

    def test_get_missing_returns_none(self, temp_store):
        assert temp_store.get_record("ingested", 999) is None

    def test_set_record_overwrites(self, temp_store):
        temp_store.set_record("ingested", 1, {"title": "first"})
        temp_store.set_record("ingested", 1, {"title": "second"})
        assert temp_store.get_record("ingested", 1) == {"title": "second"}

    def test_all_records_returns_all_with_issue_id(self, temp_store):
        temp_store.set_record("ingested", 1, {"title": "a"})
        temp_store.set_record("ingested", 2, {"title": "b"})
        all_recs = temp_store.all_records("ingested")
        by_id = {r["issue_id"]: r for r in all_recs}
        assert by_id.keys() == {1, 2}
        assert by_id[1]["title"] == "a"
        assert by_id[2]["title"] == "b"

    def test_delete_record(self, temp_store):
        temp_store.set_record("ingested", 1, {"title": "a"})
        temp_store.delete_record("ingested", 1)
        assert temp_store.get_record("ingested", 1) is None

    def test_invalid_section_rejected(self, temp_store):
        with pytest.raises(ValueError):
            temp_store.get_record("not_a_section", 1)
        with pytest.raises(ValueError):
            temp_store.set_record("not_a_section", 1, {})


# ---------------------------------------------------------------------------
# Approval / dispatch / scope convenience predicates
# ---------------------------------------------------------------------------

class TestApprovalPredicates:
    def test_is_approved_false_when_no_record(self, temp_store):
        assert temp_store.is_approved(1) is False

    def test_is_approved_reflects_set_approval(self, temp_store):
        temp_store.set_approval(1, approved=True)
        assert temp_store.is_approved(1) is True
        temp_store.set_approval(1, approved=False)
        assert temp_store.is_approved(1) is False


class TestDispatchedPredicate:
    def test_is_dispatched_false_when_no_record(self, temp_store):
        assert temp_store.is_dispatched(1) is False

    def test_is_dispatched_true_after_set_execution(
        self, temp_store, sample_execution_session
    ):
        temp_store.set_execution(1, sample_execution_session)
        assert temp_store.is_dispatched(1) is True


class TestScopedPredicate:
    def test_is_scoped_false_when_no_plan(self, temp_store):
        assert temp_store.is_scoped(1) is False

    def test_is_scoped_true_on_complete_status(self, temp_store, sample_scope_plan):
        temp_store.set_scope_plan(1, sample_scope_plan)
        assert temp_store.is_scoped(1) is True

    def test_is_scoped_false_on_pending(self, temp_store, sample_scope_plan):
        plan = dict(sample_scope_plan)
        plan["scope_status"] = "pending"
        temp_store.set_scope_plan(1, plan)
        assert temp_store.is_scoped(1) is False


# ---------------------------------------------------------------------------
# Legacy scope plan field migration
# ---------------------------------------------------------------------------

class TestNormaliseScopePlan:
    def test_promotes_legacy_fields_on_read(self, temp_store):
        # Write a record that only has the legacy field names.
        temp_store.set_record(
            "architect_plans",
            1,
            {
                "issue_id": 1,
                "architect_status": "complete",
                "architected_at": "2026-01-01T00:00:00",
            },
        )
        plan = temp_store.get_scope_plan(1)
        assert plan["scope_status"] == "complete"
        assert plan["scoped_at"] == "2026-01-01T00:00:00"
        # Original legacy fields are preserved (the normaliser is additive).
        assert plan["architect_status"] == "complete"

    def test_normalise_none_is_none(self, temp_store):
        assert temp_store._normalise_scope_plan(None) is None

    def test_normalise_preserves_new_fields(self, temp_store, sample_scope_plan):
        out = temp_store._normalise_scope_plan(sample_scope_plan)
        assert out["scope_status"] == "complete"
        assert out["scoped_at"] == "2026-04-21T00:00:00"

    def test_is_scoped_works_on_legacy_record(self, temp_store):
        temp_store.set_record(
            "architect_plans",
            2,
            {"issue_id": 2, "architect_status": "complete"},
        )
        assert temp_store.is_scoped(2) is True


# ---------------------------------------------------------------------------
# confidence_label helper
# ---------------------------------------------------------------------------

class TestConfidenceLabel:
    def test_high(self, temp_store):
        label, color = temp_store.confidence_label(90)
        assert label == "High Confidence"
        assert color.startswith("#")

    def test_medium(self, temp_store):
        label, _ = temp_store.confidence_label(60)
        assert label == "Review Recommended"

    def test_low(self, temp_store):
        label, _ = temp_store.confidence_label(10)
        assert label == "Human Required"
