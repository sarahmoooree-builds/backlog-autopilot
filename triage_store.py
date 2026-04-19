"""
triage_store.py — Persistent storage for Devin triage results

Stores per-issue triage reports in triage_store.json. Each entry includes
a confidence score, root cause hypothesis, affected files, estimated lines
changed, and concrete next steps.

Format:
{
    "12": {
        "confidence_score": 87,
        "confidence_reasoning": "Root cause clearly identified...",
        "root_cause_hypothesis": "The email validation regex at line 14...",
        "affected_files": ["auth/login_service.py"],
        "estimated_lines_changed": 2,
        "next_steps": ["Add + to regex", "Confirm test passes", "Open PR"],
        "triaged_at": "2026-04-18T10:22:00",
        "session_id": "abc123"
    }
}
"""

import json
import os
from datetime import datetime

TRIAGE_FILE = os.path.join(os.path.dirname(__file__), "triage_store.json")


def load_triage():
    """Load all triage results from disk. Returns empty dict if no file."""
    if not os.path.exists(TRIAGE_FILE):
        return {}
    with open(TRIAGE_FILE, "r") as f:
        return json.load(f)


def save_triage(data):
    """Write all triage results to disk."""
    with open(TRIAGE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def record_triage(issue_id, session_id, triage_result):
    """
    Save a triage result for an issue.

    Args:
        issue_id: GitHub issue number
        session_id: Devin session ID that produced this result
        triage_result: dict with confidence_score, reasoning, hypothesis, files, lines, steps
    """
    data = load_triage()
    data[str(issue_id)] = {
        **triage_result,
        "session_id": session_id,
        "triaged_at": datetime.now().isoformat(),
    }
    save_triage(data)


def get_triage(issue_id):
    """Get the triage result for an issue, or None if not yet triaged."""
    data = load_triage()
    return data.get(str(issue_id))


def is_triaged(issue_id):
    """Check if an issue has already been triaged."""
    data = load_triage()
    return str(issue_id) in data


def clear_triage(issue_id):
    """Remove the triage result for an issue (forces re-triage)."""
    data = load_triage()
    if str(issue_id) in data:
        del data[str(issue_id)]
        save_triage(data)


def get_all_triaged():
    """Return all triage results as a list with issue_id included."""
    data = load_triage()
    return [{"issue_id": int(k), **v} for k, v in data.items()]


def confidence_label(score):
    """Convert a numeric confidence score to a display label and color."""
    if score >= 75:
        return "High Confidence", "#28a745"
    elif score >= 50:
        return "Review Recommended", "#fd7e14"
    else:
        return "Human Required", "#dc3545"
