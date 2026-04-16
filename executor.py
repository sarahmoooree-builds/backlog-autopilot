"""
executor.py — Devin API integration

Sends approved issues to Devin for autonomous resolution.
Each issue becomes a Devin session targeting the finserv-platform repo.

The execute_issues() function has the same input/output shape as
mock_executor.py, so the Streamlit app works with either one.
"""

import os
import requests
from dotenv import load_dotenv
from prompts import EXECUTION_PROMPT

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

    Args:
        approved_issues: list of enriched issue dicts that the user approved.

    Returns:
        list of result dicts, each containing:
          - id: the issue id
          - title: the issue title
          - status: "In Progress", "Completed", "Blocked", etc.
          - outcome_summary: description of what happened
          - session_url: link to the Devin session (if created)
    """
    results = []

    for issue in approved_issues:
        result = _create_devin_session(issue)
        results.append(result)

    return results


def _build_prompt(issue):
    """Fill in the execution prompt template with issue data."""
    return EXECUTION_PROMPT.format(
        title=issue["title"],
        description=issue["description"],
        labels=", ".join(issue["labels"]),
        issue_type=issue.get("issue_type", "unknown"),
        complexity=issue.get("complexity", "unknown"),
        scope=issue.get("scope", "unknown"),
        summary=issue.get("summary", issue["title"]),
    )


def _create_devin_session(issue):
    """
    Create a Devin session for a single issue.

    Makes a POST request to the Devin API to start a new session.
    Returns a result dict with status info.
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

            return {
                "id": issue["id"],
                "title": issue["title"],
                "status": "In Progress",
                "outcome_summary": (
                    f"Devin session created successfully. "
                    f"Session ID: {session_id}. "
                    f"Devin is now working on this issue against {TARGET_REPO}."
                ),
                "session_url": session_url,
            }
        else:
            return {
                "id": issue["id"],
                "title": issue["title"],
                "status": "Blocked",
                "outcome_summary": (
                    f"Failed to create Devin session. "
                    f"API returned status {response.status_code}: {response.text[:200]}"
                ),
                "session_url": None,
            }

    except requests.exceptions.RequestException as e:
        return {
            "id": issue["id"],
            "title": issue["title"],
            "status": "Blocked",
            "outcome_summary": f"Could not reach Devin API: {str(e)}",
            "session_url": None,
        }
