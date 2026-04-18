"""
executor.py — Devin API integration

Sends approved issues to Devin for autonomous resolution.
Tracks sessions in state.py and can poll Devin for live status updates.
"""

import os
import requests
from dotenv import load_dotenv
from prompts import EXECUTION_PROMPT
from state import record_session, is_dispatched, get_session, update_session_status, get_all_sessions

# Load API credentials from .env
load_dotenv()
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY")
DEVIN_ORG_ID = os.getenv("DEVIN_ORG_ID")

# The repo Devin will work against
TARGET_REPO = "sarahmoooree-builds/finserv-platform"

DEVIN_API_BASE = f"https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}"


def execute_issues(approved_issues):
    """
    Send each approved issue to Devin as a new session.
    Skips issues that have already been dispatched (duplicate prevention).

    Returns a list of result dicts for display in the UI.
    """
    results = []

    for issue in approved_issues:
        # Skip if already dispatched
        if is_dispatched(issue["id"]):
            existing = get_session(issue["id"])
            results.append({
                "id": issue["id"],
                "title": issue["title"],
                "status": existing["status"],
                "outcome_summary": existing["outcome_summary"],
                "session_url": existing.get("session_url"),
                "already_dispatched": True,
            })
            continue

        # Create new Devin session
        result = _create_devin_session(issue)
        results.append(result)

    return results


def refresh_session_statuses():
    """
    Poll the Devin API for updated status on all tracked sessions.
    Updates state.py with the latest info.

    Returns a list of updated session dicts.
    """
    sessions = get_all_sessions()
    updated = []

    for session in sessions:
        session_id = session.get("session_id")
        if not session_id or session_id == "unknown":
            updated.append(session)
            continue

        # Skip sessions that are already in a terminal state
        if session["status"] in ("Completed", "Blocked"):
            updated.append(session)
            continue

        # Poll Devin for current status
        live_status = _poll_session(session_id)
        if live_status:
            new_status = _map_devin_status(live_status)
            outcome = _build_outcome_summary(live_status)
            update_session_status(session["issue_id"], new_status, outcome)
            session["status"] = new_status
            session["outcome_summary"] = outcome
            # Capture PRs if any
            if live_status.get("pull_requests"):
                session["pull_requests"] = live_status["pull_requests"]

        updated.append(session)

    return updated


def _build_prompt(issue):
    """Fill in the execution prompt template with issue data, prefixed with repo context."""
    task_prompt = EXECUTION_PROMPT.format(
        title=issue["title"],
        description=issue["description"],
        labels=", ".join(issue["labels"]),
        issue_type=issue.get("issue_type", "unknown"),
        complexity=issue.get("complexity", "unknown"),
        scope=issue.get("scope", "unknown"),
        summary=issue.get("summary", issue["title"]),
    )
    return f"Work on the GitHub repository https://github.com/{TARGET_REPO}.\n\n{task_prompt}"


def _create_devin_session(issue):
    """
    Create a Devin session for a single issue.
    Records the session in state.py for tracking.
    """
    prompt = _build_prompt(issue)

    headers = {
        "Authorization": f"Bearer {DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "prompt": prompt,
    }

    try:
        response = requests.post(
            f"{DEVIN_API_BASE}/sessions",
            headers=headers,
            json=payload,
            timeout=30,
        )

        if response.status_code in (200, 201):
            data = response.json()
            session_id = data.get("session_id", "unknown")
            session_url = data.get("url", f"https://app.devin.ai/sessions/{session_id}")
            status = "In Progress"
            outcome = (
                f"Devin session created. Session ID: {session_id}. "
                f"Devin is working on this issue against {TARGET_REPO}."
            )

            # Save to persistent state
            record_session(issue["id"], session_id, session_url, status, outcome)

            return {
                "id": issue["id"],
                "title": issue["title"],
                "status": status,
                "outcome_summary": outcome,
                "session_url": session_url,
                "already_dispatched": False,
            }
        else:
            status = "Blocked"
            outcome = (
                f"Failed to create Devin session. "
                f"API returned status {response.status_code}: {response.text[:200]}"
            )
            return {
                "id": issue["id"],
                "title": issue["title"],
                "status": status,
                "outcome_summary": outcome,
                "session_url": None,
                "already_dispatched": False,
            }

    except requests.exceptions.RequestException as e:
        return {
            "id": issue["id"],
            "title": issue["title"],
            "status": "Blocked",
            "outcome_summary": f"Could not reach Devin API: {str(e)}",
            "session_url": None,
            "already_dispatched": False,
        }


def _poll_session(session_id):
    """
    GET the current state of a Devin session.
    Returns the raw API response dict, or None on failure.
    """
    headers = {
        "Authorization": f"Bearer {DEVIN_API_KEY}",
    }

    try:
        response = requests.get(
            f"{DEVIN_API_BASE}/sessions/{session_id}",
            headers=headers,
            timeout=15,
        )
        if response.status_code == 200:
            return response.json()
    except requests.exceptions.RequestException:
        pass

    return None


def _map_devin_status(session_data):
    """
    Map Devin's session status to our display status.

    Devin statuses (from API): new, running, paused, stopped, finished, blocked
    Our statuses: In Progress, Awaiting Review, Completed, Blocked
    """
    devin_status = session_data.get("status", "").lower()
    has_prs = bool(session_data.get("pull_requests"))

    if devin_status == "finished" and has_prs:
        return "Completed"
    if devin_status == "finished":
        return "Awaiting Review"
    if devin_status in ("blocked", "stopped"):
        return "Blocked"
    if devin_status in ("running", "new"):
        if has_prs:
            return "Awaiting Review"
        return "In Progress"
    if devin_status == "paused":
        return "Awaiting Review"

    return "In Progress"


def _build_outcome_summary(session_data):
    """Build a human-readable outcome summary from Devin session data."""
    status = session_data.get("status", "unknown")
    prs = session_data.get("pull_requests", [])

    if prs:
        pr_links = ", ".join([f"PR #{pr.get('number', '?')}" for pr in prs])
        return f"Devin opened {len(prs)} pull request(s): {pr_links}. Session status: {status}."

    if status == "finished":
        return "Devin finished working on this issue. Check the session for details."

    if status in ("blocked", "stopped"):
        return f"Devin session is {status}. May need human input to proceed."

    return f"Devin is currently working on this issue. Session status: {status}."
