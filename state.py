"""
state.py — Persistent state tracker for Devin sessions

Stores a mapping of issue_id → session info in a local JSON file so the app
knows which issues have already been dispatched and can poll for updates.

State file format (sessions.json):
{
    "17": {
        "session_id": "abc123",
        "session_url": "https://app.devin.ai/sessions/abc123",
        "status": "In Progress",
        "outcome_summary": "Devin session created...",
        "dispatched_at": "2026-04-16T12:00:00"
    }
}
"""

import json
import os
from datetime import datetime

STATE_FILE = os.path.join(os.path.dirname(__file__), "sessions.json")


def load_state():
    """Load session state from disk. Returns empty dict if no state file."""
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    """Write session state to disk."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def record_session(issue_id, session_id, session_url, status, outcome_summary):
    """Record a new or updated session for an issue."""
    state = load_state()
    state[str(issue_id)] = {
        "session_id": session_id,
        "session_url": session_url,
        "status": status,
        "outcome_summary": outcome_summary,
        "dispatched_at": datetime.now().isoformat(),
    }
    save_state(state)


def is_dispatched(issue_id):
    """Check if an issue has already been sent to Devin."""
    state = load_state()
    return str(issue_id) in state


def get_session(issue_id):
    """Get session info for an issue, or None if not dispatched."""
    state = load_state()
    return state.get(str(issue_id))


def update_session_status(issue_id, status, outcome_summary=None):
    """Update the status of an existing session."""
    state = load_state()
    key = str(issue_id)
    if key in state:
        state[key]["status"] = status
        if outcome_summary:
            state[key]["outcome_summary"] = outcome_summary
        save_state(state)


def get_all_sessions():
    """Return all tracked sessions as a list of dicts with issue_id included."""
    state = load_state()
    sessions = []
    for issue_id, info in state.items():
        sessions.append({"issue_id": int(issue_id), **info})
    return sessions
