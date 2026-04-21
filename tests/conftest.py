"""Shared pytest fixtures for the backlog-autopilot test suite.

All fixtures here are pure-Python: no real Devin API calls, no real GitHub
calls, no real filesystem state outside of ``tmp_path``.
"""

from __future__ import annotations

import os
import sys

import pytest

# Ensure the repo root is on sys.path so ``import store`` / ``import ingest``
# work regardless of the directory pytest is invoked from.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ---------------------------------------------------------------------------
# Sample schema dicts
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_raw_issue() -> dict:
    """Minimal raw GitHub issue dict before ingest normalisation."""
    return {
        "id": 101,
        "title": "  Webhook retry crashes on 500  ",
        "description": "Server returns 500 and the retry loop never stops.",
        "labels": ["Bug", "  webhooks "],
        "age_days": 12,
        "comments_count": 4,
    }


@pytest.fixture
def sample_ingested_issue() -> dict:
    """A canonical IngestedIssue dict used by planner/optimizer tests."""
    return {
        "id": 101,
        "title": "Webhook retry crashes on 500",
        "description": "Server returns 500 and the retry loop never stops.",
        "labels": ["bug", "webhooks"],
        "age_days": 12,
        "comments_count": 4,
        "summary": "Bug: Webhook retry crashes on 500.",
        "issue_type": "bug",
        "complexity": "low",
        "scope": "narrow",
        "risk": "low",
        "duplicate_of": None,
        "ingested_at": "2026-04-21T00:00:00",
    }


@pytest.fixture
def sample_planned_issue(sample_ingested_issue) -> dict:
    """A canonical PlannedIssue dict (IngestedIssue + planner_score)."""
    return {
        **sample_ingested_issue,
        "planner_score": {
            "user_impact": 7,
            "business_impact": 6,
            "effort": 2,
            "confidence": 10,
            "total_score": 7.8,
            "recommended": True,
            "recommendation_reason": "Score 7.8/10 — good automation candidate",
            "priority_rank": 1,
        },
        "implementation_options": [
            "Locate and patch the defect directly in the relevant module.",
        ],
        "planned_at": "2026-04-21T00:00:00",
    }


@pytest.fixture
def sample_scope_plan() -> dict:
    """A canonical ScopePlan dict (Stage 3 output)."""
    return {
        "issue_id": 101,
        "confidence_score": 85,
        "confidence_reasoning": "Clear repro and small blast radius.",
        "root_cause_hypothesis": "webhooks/retry.py retry_on_error ignores 5xx",
        "affected_files": ["webhooks/retry.py", "tests/test_retry.py"],
        "estimated_lines_changed": 18,
        "task_breakdown": ["Add 5xx handling", "Backoff on failure"],
        "dependencies": [],
        "risks": [],
        "session_id": "sess_abc",
        "session_url": "https://app.devin.ai/sessions/sess_abc",
        "scope_status": "complete",
        "error": None,
        "scoped_at": "2026-04-21T00:00:00",
    }


@pytest.fixture
def sample_execution_session() -> dict:
    """A canonical ExecutionSession dict (Stage 4 output)."""
    return {
        "issue_id": 101,
        "session_id": "sess_exec",
        "session_url": "https://app.devin.ai/sessions/sess_exec",
        "status": "Completed",
        "outcome_summary": "Fix applied and PR merged.",
        "pull_requests": ["https://github.com/org/repo/pull/1"],
        "dispatched_at": "2026-04-21T00:00:00",
        "completed_at": "2026-04-21T00:10:00",
        "estimated_lines_changed": 18,
        "estimated_files": ["webhooks/retry.py", "tests/test_retry.py"],
    }


# ---------------------------------------------------------------------------
# Isolated SQLite-backed store fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    """Yield the ``store`` module pointed at a fresh, empty SQLite DB.

    The store module initialises its schema at import time against the
    default ``STORE_DB`` path. To isolate tests we:
      1. Import the module
      2. Point ``store.STORE_DB`` at a file inside ``tmp_path``
      3. Run ``_init_db()`` against the new path
      4. Yield the module
    """
    import store as _store  # noqa: WPS433 — deliberately late import

    new_db = tmp_path / "pipeline_store.db"
    monkeypatch.setattr(_store, "STORE_DB", str(new_db))
    _store._init_db()
    yield _store


# ---------------------------------------------------------------------------
# Devin API response fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def devin_structured_output_response() -> dict:
    """Session payload where Devin returned results via structured_output."""
    return {
        "session_id": "sess_structured",
        "status": "completed",
        "structured_output": [
            {"id": 1, "title": "A"},
            {"id": 2, "title": "B"},
        ],
    }


@pytest.fixture
def devin_messages_response() -> dict:
    """Session payload where the JSON array lives inside a message ``content``."""
    return {
        "session_id": "sess_messages",
        "status": "completed",
        "messages": [
            {"role": "assistant", "content": "Working on it..."},
            {
                "role": "assistant",
                "content": '[{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]',
            },
        ],
    }


@pytest.fixture
def devin_embedded_json_response() -> dict:
    """Session payload where JSON is embedded inside prose — needs substring parse."""
    return {
        "session_id": "sess_embedded",
        "status": "completed",
        "messages": [
            {
                "role": "assistant",
                "content": (
                    "Here is the result:\n"
                    '[{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]\n'
                    "Let me know if you need more."
                ),
            }
        ],
    }


@pytest.fixture
def devin_garbage_response() -> dict:
    """Session payload that contains no parseable JSON array."""
    return {
        "session_id": "sess_garbage",
        "status": "completed",
        "messages": [
            {"role": "assistant", "content": "I could not complete the task."},
        ],
    }
