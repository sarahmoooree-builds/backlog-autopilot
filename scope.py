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

import requests
from datetime import datetime

import devin_client
import store
from config import POLL_INTERVAL, SCOPE_TIMEOUT, TARGET_REPO
from prompts import SCOPE_PROMPT


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

    # --- Step 1: Create the scope session ---
    print(f"[scope] Creating Devin session for issue #{issue_id}...")
    try:
        created = devin_client.create_session(prompt, bypass_approval=True)
    except requests.exceptions.RequestException as e:
        err = _error_plan(issue_id, "", f"Could not reach Devin API: {str(e)}")
        store.set_scope_plan(issue_id, err)
        return err
    except RuntimeError as e:
        err = _error_plan(issue_id, "", str(e))
        store.set_scope_plan(issue_id, err)
        return err

    session_id = created["session_id"]
    session_url = created["session_url"]

    if not session_id:
        err = _error_plan(issue_id, "", "No session_id returned from Devin API")
        store.set_scope_plan(issue_id, err)
        return err

    print(f"[scope] Session created: {session_url}")

    # --- Step 2: Save pending state immediately so UI shows progress ---
    pending = _pending_plan(issue_id, session_id, session_url)
    store.set_scope_plan(issue_id, pending)

    # --- Step 3: Poll until finished ---
    result = devin_client.poll_until_done(
        session_id,
        timeout=SCOPE_TIMEOUT,
        poll_interval=POLL_INTERVAL,
        label="scope",
    )

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
    # In the Devin v3 API the session-retrieval response does NOT include
    # messages — they live behind a separate paginated endpoint. Fetch them
    # explicitly before trying to parse the structured plan.
    final_status = (result.get("status") or "").lower()
    final_detail = (result.get("status_detail") or "").lower()
    has_structured_output = bool(result.get("structured_output"))
    print(
        f"[scope] Session terminal state for issue #{issue_id}: "
        f"status={final_status!r} detail={final_detail!r} "
        f"structured_output_present={has_structured_output}"
    )

    messages = devin_client.fetch_messages(session_id, label="scope")
    print(
        f"[scope] Fetched {len(messages)} message(s) for issue #{issue_id} "
        f"(devin-authored: {sum(1 for m in messages if (m.get('source') or '').lower() == 'devin')})"
    )

    # Attempt extraction even when the session is still 'running' +
    # 'waiting_for_user' / 'finished' — Devin often emits the full JSON plan
    # and then waits for further instructions rather than fully exiting.
    plan_data = devin_client.extract_json_object(
        result, messages=messages, required_fields=_REQUIRED_PLAN_FIELDS
    )

    if not plan_data:
        suspended_note = (
            " (session was suspended before producing a plan)"
            if final_status == "suspended" else ""
        )
        errored_note = (
            " (session reported an error state)"
            if final_status == "error" else ""
        )
        awaiting_note = (
            " (session awaiting further instructions)"
            if final_detail == "waiting_for_user" else ""
        )
        print(
            f"[scope] Could not parse scope JSON for issue #{issue_id} "
            f"— recording error. status={final_status!r} detail={final_detail!r}"
        )
        err = _error_plan(
            issue_id, session_url,
            f"Devin finished but scope JSON could not be parsed"
            f"{suspended_note}{errored_note}{awaiting_note}. Session: {session_url}"
        )
        err["session_id"] = session_id
        store.set_scope_plan(issue_id, err)
        return err

    print(
        f"[scope] Parsed scope JSON for issue #{issue_id} "
        f"(status={final_status!r}, detail={final_detail!r})"
    )

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


_REQUIRED_PLAN_FIELDS = frozenset({
    "confidence_score", "confidence_reasoning", "root_cause_hypothesis",
    "affected_files", "estimated_lines_changed", "task_breakdown",
    "dependencies", "risks",
})
