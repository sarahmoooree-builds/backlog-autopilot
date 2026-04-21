"""
scope.py — Stage 3: Scope (formerly "Architect")

Converts Planner-approved issues into technical implementation plans by
dispatching a Devin session that reads the finserv-platform codebase.

The Scope stage decides HOW to build each approved issue. It produces:
  - Confidence score and reasoning
  - Root cause hypothesis (specific file / function / line)
  - Affected files (confirmed in the repo by Devin)
  - Estimated lines changed
  - Ordered task breakdown (ready for the Executor to follow)
  - Dependencies and risks

It does NOT write code or open PRs.
Output is saved to store.py (scope_plans section).
"""

import json
import os
import time
import requests
from datetime import datetime
from dotenv import load_dotenv

import store
from prompts import SCOPE_PROMPT

load_dotenv()
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY")
DEVIN_ORG_ID = os.getenv("DEVIN_ORG_ID")

TARGET_REPO = "sarahmoooree-builds/finserv-platform"
DEVIN_API_BASE = f"https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}"

SCOPE_TIMEOUT = 360   # seconds — Devin needs 4–6 minutes to read the codebase
POLL_INTERVAL = 10    # seconds between status polls


def scope_issue(planned_issue: dict) -> dict:
    """
    Run a Devin scoping session for a single planned issue.

    Creates a Devin session, saves a pending record immediately so the UI
    shows progress, polls until finished, extracts the JSON plan, and saves
    the result to the store — including on failure, so the UI always shows
    something after this call returns.

    Returns the ScopePlan dict (may contain scope_status="error" on failure).
    """
    issue_id = planned_issue["id"]
    prompt = _build_scope_prompt(planned_issue)

    headers = {
        "Authorization": f"Bearer {DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }

    # --- Step 1: Create the scope session ---
    print(f"[scope] Creating Devin session for issue #{issue_id}...")
    try:
        response = requests.post(
            f"{DEVIN_API_BASE}/sessions",
            headers=headers,
            json={"prompt": prompt, "bypass_approval": True},
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        err = _error_plan(issue_id, "", f"Could not reach Devin API: {str(e)}")
        store.set_scope_plan(issue_id, err)
        return err

    if response.status_code not in (200, 201):
        err = _error_plan(issue_id, "", f"API returned {response.status_code}: {response.text[:200]}")
        store.set_scope_plan(issue_id, err)
        return err

    session_data = response.json()
    session_id = session_data.get("session_id")
    session_url = session_data.get("url", f"https://app.devin.ai/sessions/{session_id}")

    if not session_id:
        err = _error_plan(issue_id, "", "No session_id returned from Devin API")
        store.set_scope_plan(issue_id, err)
        return err

    print(f"[scope] Session created: {session_url}")

    # --- Step 2: Save pending state immediately so UI shows progress ---
    pending = _pending_plan(issue_id, session_id, session_url)
    store.set_scope_plan(issue_id, pending)

    # --- Step 3: Poll until finished ---
    result = _poll_until_done(session_id, headers)

    if not result:
        err = _error_plan(
            issue_id, session_url,
            f"Devin session timed out after {SCOPE_TIMEOUT // 60} minutes. "
            f"Session: {session_url}"
        )
        err["session_id"] = session_id
        store.set_scope_plan(issue_id, err)
        return err

    # --- Step 4: Extract and save the scope JSON ---
    plan_data = _extract_scope_json(result)

    if not plan_data:
        final_status = result.get("status", "").lower()
        detail = " (session awaiting further instructions)" if final_status == "blocked" else ""
        err = _error_plan(
            issue_id, session_url,
            f"Devin finished but scope JSON could not be parsed{detail}. Session: {session_url}"
        )
        err["session_id"] = session_id
        store.set_scope_plan(issue_id, err)
        return err

    scope_plan = {
        "issue_id": issue_id,
        **plan_data,
        "session_id": session_id,
        "session_url": session_url,
        "scope_status": "complete",
        "error": None,
        "scoped_at": datetime.now().isoformat(),
    }
    store.set_scope_plan(issue_id, scope_plan)
    print(f"[scope] Issue #{issue_id} scoped. "
          f"Confidence: {plan_data.get('confidence_score')}/100")
    return scope_plan


def scope_issues(planned_issues: list) -> dict:
    """
    Run scoping on a list of planned issues.
    Skips issues that already have a complete scope plan.
    Returns a dict of {issue_id: ScopePlan}.
    """
    results = {}
    for issue in planned_issues:
        existing = store.get_scope_plan(issue["id"])
        if existing and existing.get("scope_status") == "complete":
            results[issue["id"]] = existing
            continue
        results[issue["id"]] = scope_issue(issue)
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_scope_prompt(planned_issue: dict) -> str:
    """Build the scope prompt from the SCOPE_PROMPT template."""
    options = planned_issue.get("implementation_options", [])
    return (
        f"Work on the GitHub repository https://github.com/{TARGET_REPO}.\n\n"
        + SCOPE_PROMPT.format(
            issue_id=planned_issue["id"],
            title=planned_issue["title"],
            description=planned_issue["description"],
            labels=", ".join(planned_issue.get("labels", [])),
            issue_type=planned_issue.get("issue_type", "unknown"),
            complexity=planned_issue.get("complexity", "unknown"),
            scope=planned_issue.get("scope", "unknown"),
            risk=planned_issue.get("risk", "unknown"),
            summary=planned_issue.get("summary", planned_issue["title"]),
            implementation_options="\n".join(f"- {o}" for o in options) if options else "None provided",
        )
    )


def _pending_plan(issue_id: int, session_id: str, session_url: str) -> dict:
    return {
        "issue_id": issue_id,
        "confidence_score": 0,
        "confidence_reasoning": "",
        "root_cause_hypothesis": "",
        "affected_files": [],
        "estimated_lines_changed": 0,
        "task_breakdown": [],
        "dependencies": [],
        "risks": [],
        "session_id": session_id,
        "session_url": session_url,
        "scope_status": "pending",
        "error": None,
        "scoped_at": datetime.now().isoformat(),
    }


def _error_plan(issue_id: int, session_url: str, error_msg: str) -> dict:
    return {
        "issue_id": issue_id,
        "confidence_score": 0,
        "confidence_reasoning": "",
        "root_cause_hypothesis": "",
        "affected_files": [],
        "estimated_lines_changed": 0,
        "task_breakdown": [],
        "dependencies": [],
        "risks": [],
        "session_id": "unknown",
        "session_url": session_url,
        "scope_status": "error",
        "error": error_msg,
        "scoped_at": datetime.now().isoformat(),
    }


def _poll_until_done(session_id: str, headers: dict):
    """
    Poll the Devin session every POLL_INTERVAL seconds until a terminal state
    or timeout. Prints status updates for visibility.
    Returns the final session dict, or None on timeout.
    """
    deadline = time.time() + SCOPE_TIMEOUT
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
                detail = session.get("status_detail", "")
                print(f"[scope] Poll #{attempt}: status={status!r} detail={detail!r}")
                _NON_TERMINAL = ("running", "starting", "queued", "initializing", "created", "claimed")
                if status not in _NON_TERMINAL:
                    return session
                if detail == "waiting_for_user":
                    print(f"[scope] Poll #{attempt}: Devin finished (waiting_for_user)")
                    return session
            else:
                print(f"[scope] Poll #{attempt}: HTTP {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[scope] Poll #{attempt}: request error — {e}")

        time.sleep(POLL_INTERVAL)

    print(f"[scope] Timed out after {SCOPE_TIMEOUT}s ({attempt} polls)")
    return None


def _extract_scope_json(session_data: dict):
    """
    Extract the structured scope JSON from the Devin session output.
    Checks structured_output first, then scans messages in reverse.
    Returns the parsed dict if all required fields are present, else None.
    """
    required_fields = {
        "confidence_score", "confidence_reasoning", "root_cause_hypothesis",
        "affected_files", "estimated_lines_changed", "task_breakdown",
        "dependencies", "risks",
    }

    # Try structured_output first
    structured = session_data.get("structured_output")
    if structured and isinstance(structured, dict):
        if required_fields.issubset(structured.keys()):
            return structured

    # Scan messages in reverse (most recent first) for a JSON block
    messages = (session_data.get("messages")
                or session_data.get("items")
                or [])
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
