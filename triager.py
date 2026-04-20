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

# How long to poll for a triage result (seconds) — Devin needs 4-6 minutes
TRIAGE_TIMEOUT = 360
POLL_INTERVAL = 10


def triage_issue(issue):
    """
    Run a Devin triage session for a single issue.

    Creates a Devin session, polls until it finishes, extracts the JSON report,
    and saves it to triage_store.json — including on failure, so the UI always
    shows something after this call completes.

    Returns the triage result dict (may contain "status": "error" on failure).
    """
    prompt = _build_triage_prompt(issue)

    headers = {
        "Authorization": f"Bearer {DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }

    # --- Step 1: Create the triage session ---
    print(f"[triage] Creating Devin session for issue #{issue['id']}...")
    try:
        response = requests.post(
            f"{DEVIN_API_BASE}/sessions",
            headers=headers,
            json={"prompt": prompt, "bypass_approval": True},
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        err = {"status": "error", "error": f"Could not reach Devin API: {str(e)}"}
        record_triage(issue["id"], "unknown", err)
        return err

    if response.status_code not in (200, 201):
        err = {"status": "error", "error": f"API returned {response.status_code}: {response.text[:200]}"}
        record_triage(issue["id"], "unknown", err)
        return err

    session_data = response.json()
    session_id = session_data.get("session_id")
    session_url = session_data.get("url", f"https://app.devin.ai/sessions/{session_id}")

    if not session_id:
        err = {"status": "error", "error": "No session_id returned from Devin API"}
        record_triage(issue["id"], "unknown", err)
        return err

    print(f"[triage] Session created: {session_url}")

    # --- Step 2: Save a "pending" state immediately so UI shows progress ---
    pending = {
        "status": "pending",
        "session_url": session_url,
        "error": None,
    }
    record_triage(issue["id"], session_id, pending)

    # --- Step 3: Poll until finished ---
    result = _poll_until_done(session_id, headers)

    if not result:
        err = {
            "status": "error",
            "session_url": session_url,
            "error": f"Devin session timed out after {TRIAGE_TIMEOUT // 60} minutes. Session: {session_url}",
        }
        record_triage(issue["id"], session_id, err)
        return err

    final_status = result.get("status", "").lower()
    if final_status == "blocked":
        err = {
            "status": "error",
            "session_url": session_url,
            "error": f"Devin session was blocked — may need human input. Session: {session_url}",
        }
        record_triage(issue["id"], session_id, err)
        return err

    # --- Step 4: Extract and save the triage JSON ---
    triage_data = _extract_triage_json(result)

    if not triage_data:
        err = {
            "status": "error",
            "session_url": session_url,
            "error": f"Devin finished but triage JSON could not be parsed. Session: {session_url}",
        }
        record_triage(issue["id"], session_id, err)
        return err

    # Add the session URL to the result for easy linking
    triage_data["session_url"] = session_url
    record_triage(issue["id"], session_id, triage_data)
    print(f"[triage] Issue #{issue['id']} triaged. Confidence: {triage_data.get('confidence_score')}/100")
    return triage_data


def triage_issues(issues):
    """
    Triage a list of issues. Skips already-triaged ones.
    Returns a dict of {issue_id: triage_result}.
    """
    results = {}
    for issue in issues:
        existing = get_triage(issue["id"])
        if existing and existing.get("status") != "error":
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

Spawn the issue-triager subagent in foreground mode and give it the issue details above.

When the subagent finishes, output ONLY the raw JSON object it produced — no markdown, no commentary, no code blocks. Just the JSON.
"""


def _poll_until_done(session_id, headers):
    """
    Poll the Devin session every POLL_INTERVAL seconds until terminal state or timeout.
    Prints status updates so you can see what's happening.
    Returns the final session dict, or None on timeout.
    """
    deadline = time.time() + TRIAGE_TIMEOUT
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        try:
            response = requests.get(
                f"{DEVIN_API_BASE}/sessions/{session_id}",
                headers=headers,
                timeout=15,
            )
            if response.status_code == 200:
                session = response.json()
                status = session.get("status", "unknown").lower()
                print(f"[triage] Poll #{attempt}: status={status}")
                if status in ("finished", "stopped", "blocked"):
                    return session
            else:
                print(f"[triage] Poll #{attempt}: HTTP {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[triage] Poll #{attempt}: request error — {e}")

        time.sleep(POLL_INTERVAL)

    print(f"[triage] Timed out after {TRIAGE_TIMEOUT}s ({attempt} polls)")
    return None


def _extract_triage_json(session_data):
    """
    Extract the structured triage JSON from the Devin session output.
    Looks in structured_output first, then scans messages for a JSON block.
    """
    required_fields = {
        "confidence_score", "confidence_reasoning", "root_cause_hypothesis",
        "affected_files", "estimated_lines_changed", "next_steps"
    }

    # Try structured_output first
    structured = session_data.get("structured_output")
    if structured and isinstance(structured, dict):
        if required_fields.issubset(structured.keys()):
            return structured

    # Scan messages in reverse (most recent first) for a JSON block
    messages = session_data.get("messages", [])
    for message in reversed(messages):
        content = message.get("content", "")
        if not content:
            continue

        # Try the whole content as JSON
        try:
            parsed = json.loads(content.strip())
            if required_fields.issubset(parsed.keys()):
                return parsed
        except (json.JSONDecodeError, AttributeError):
            pass

        # Try to find a JSON object within the content
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
