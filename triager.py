"""
triager.py — Devin-powered issue triage

Dispatches issues to a Devin triage session using the issue-triager subagent.
Devin reads the finserv-platform codebase and returns a structured JSON report
with a confidence score, root cause hypothesis, affected files, and next steps.

The triage session is separate from the execution session — it produces analysis
only, no code changes or PRs.
"""

import json
import os
import time
import requests
from dotenv import load_dotenv
from triage_store import record_triage, get_triage

load_dotenv()
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY")
DEVIN_ORG_ID = os.getenv("DEVIN_ORG_ID")

TARGET_REPO = "sarahmoooree-builds/finserv-platform"
DEVIN_API_BASE = f"https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}"

# How long to poll for a triage result (seconds)
TRIAGE_TIMEOUT = 180
POLL_INTERVAL = 8


def triage_issue(issue):
    """
    Run a Devin triage session for a single issue.

    Spawns a Devin session instructing it to use the issue-triager subagent,
    polls until the session finishes, extracts the JSON report from the output,
    and saves it to triage_store.json.

    Returns the triage result dict, or None on failure.
    """
    prompt = _build_triage_prompt(issue)

    headers = {
        "Authorization": f"Bearer {DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }

    # Create the triage session
    try:
        response = requests.post(
            f"{DEVIN_API_BASE}/sessions",
            headers=headers,
            json={"prompt": prompt},
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        return {"error": f"Could not reach Devin API: {str(e)}"}

    if response.status_code not in (200, 201):
        return {"error": f"API error {response.status_code}: {response.text[:200]}"}

    session_id = response.json().get("session_id")
    if not session_id:
        return {"error": "No session_id returned from Devin API"}

    # Poll until finished or timeout
    result = _poll_until_done(session_id, headers)
    if not result:
        return {"error": f"Triage session {session_id} timed out or failed"}

    # Extract the JSON triage report from the session output
    triage_data = _extract_triage_json(result)
    if not triage_data:
        return {"error": "Could not parse triage JSON from Devin output"}

    # Persist to triage_store
    record_triage(issue["id"], session_id, triage_data)
    return triage_data


def triage_issues(issues):
    """
    Triage a list of issues. Returns a dict of {issue_id: triage_result}.
    Skips issues that have already been triaged.
    """
    results = {}
    for issue in issues:
        existing = get_triage(issue["id"])
        if existing:
            results[issue["id"]] = existing
            continue
        results[issue["id"]] = triage_issue(issue)
    return results


def _build_triage_prompt(issue):
    """Build the triage prompt for a single issue."""
    return f"""Work on the GitHub repository https://github.com/{TARGET_REPO}.

Use the `issue-triager` subagent (defined in .devin/agents/issue-triager/AGENT.md) to analyze the following issue.

## Issue to Triage

**Issue #{issue['id']}: {issue['title']}**

**Description:**
{issue['description']}

**Labels:** {', '.join(issue['labels'])}

## Instructions

Spawn the issue-triager subagent in foreground mode. Provide it with the issue details above.

When the subagent completes, output its JSON response as your final message — nothing else. Do not summarize, do not add commentary. Output only the raw JSON object.
"""


def _poll_until_done(session_id, headers):
    """
    Poll the Devin session until it reaches a terminal state.
    Returns the final session dict, or None if timeout/failure.
    """
    deadline = time.time() + TRIAGE_TIMEOUT

    while time.time() < deadline:
        try:
            response = requests.get(
                f"{DEVIN_API_BASE}/sessions/{session_id}",
                headers=headers,
                timeout=15,
            )
            if response.status_code == 200:
                session = response.json()
                status = session.get("status", "").lower()
                if status in ("finished", "stopped", "blocked"):
                    return session
        except requests.exceptions.RequestException:
            pass

        time.sleep(POLL_INTERVAL)

    return None


def _extract_triage_json(session_data):
    """
    Extract the structured triage JSON from the Devin session output.

    Devin should output only JSON as its final message. We look for a valid
    JSON object containing 'confidence_score' in the structured output.
    """
    required_fields = {
        "confidence_score", "confidence_reasoning", "root_cause_hypothesis",
        "affected_files", "estimated_lines_changed", "next_steps"
    }

    # Try structured_output first (if Devin supports it)
    structured = session_data.get("structured_output")
    if structured and isinstance(structured, dict):
        if required_fields.issubset(structured.keys()):
            return structured

    # Fall back to scanning the session messages for a JSON block
    messages = session_data.get("messages", [])
    for message in reversed(messages):
        content = message.get("content", "")
        if not content:
            continue
        # Try to parse the whole content as JSON
        try:
            parsed = json.loads(content.strip())
            if required_fields.issubset(parsed.keys()):
                return parsed
        except (json.JSONDecodeError, AttributeError):
            pass
        # Try to extract a JSON block from within the content
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(content[start:end])
                if required_fields.issubset(parsed.keys()):
                    return parsed
            except json.JSONDecodeError:
                pass

    return None
