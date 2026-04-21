"""
executor.py — Stage 4: Executor

Sends Scope-approved issues to Devin for autonomous implementation.
The Executor follows the Scope plan — it does not invent strategy or
prioritisation.

For high-confidence, well-scoped issues: Devin implements, tests, and opens a PR.
For lower-confidence issues blocked during execution: Devin reports the blocker
instead of guessing.

Requires a complete ScopePlan in the store before dispatching.
"""

import requests
from datetime import datetime

import store
from config import DEVIN_API_BASE, DEVIN_API_KEY, TARGET_REPO
from prompts import EXECUTION_PROMPT


def execute_issues(planned_issues: list) -> list:
    """
    Send each approved, scoped issue to Devin as a new execution session.
    Skips issues that are already dispatched (duplicate prevention).
    Skips issues that have no complete ScopePlan (logged as a warning).

    Returns a list of result dicts for display in the UI.
    """
    results = []

    for issue in planned_issues:
        issue_id = issue["id"]

        # Skip if already dispatched
        if store.is_dispatched(issue_id):
            existing = store.get_execution(issue_id)
            results.append({
                "id": issue_id,
                "title": issue["title"],
                "status": existing["status"],
                "outcome_summary": existing["outcome_summary"],
                "session_url": existing.get("session_url"),
                "already_dispatched": True,
            })
            continue

        # Require a complete ScopePlan before dispatching
        scope_plan = store.get_scope_plan(issue_id)
        if not scope_plan or scope_plan.get("scope_status") != "complete":
            print(f"[executor] Skipping issue #{issue_id} — no complete Scope plan found.")
            results.append({
                "id": issue_id,
                "title": issue["title"],
                "status": "Blocked",
                "outcome_summary": "Cannot dispatch: Scope plan is missing or incomplete.",
                "session_url": None,
                "already_dispatched": False,
            })
            continue

        result = _create_devin_session(issue, scope_plan)
        results.append(result)

    return results


def refresh_session_statuses() -> list:
    """
    Poll the Devin API for updated status on all tracked execution sessions.
    Updates the store with the latest info.
    Returns a list of updated session dicts.
    """
    sessions = store.all_executions()
    updated = []

    for session in sessions:
        session_id = session.get("session_id")
        if not session_id or session_id == "unknown":
            updated.append(session)
            continue

        # Skip sessions already in a terminal state
        if session["status"] in ("Completed", "Blocked"):
            updated.append(session)
            continue

        live_data = _poll_session(session_id)
        if live_data:
            new_status = _map_devin_status(live_data)
            outcome = _build_outcome_summary(live_data)
            prs = live_data.get("pull_requests", [])
            completed_at = datetime.now().isoformat() if new_status in ("Completed", "Blocked") else None

            store.update_execution_status(
                session["issue_id"], new_status, outcome, prs, completed_at
            )
            session["status"] = new_status
            session["outcome_summary"] = outcome
            session["pull_requests"] = prs

        updated.append(session)

    return updated


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_prompt(planned_issue: dict, scope_plan: dict) -> str:
    """
    Fill in the EXECUTION_PROMPT with both planned issue data and the Scope Plan.
    Prefixes with repo context so Devin knows where to work.
    """
    task_breakdown = "\n".join(
        f"{i+1}. {t}" for i, t in enumerate(scope_plan.get("task_breakdown", []))
    )
    affected_files = "\n".join(
        f"- {f}" for f in scope_plan.get("affected_files", [])
    )
    risks = "\n".join(
        f"- {r}" for r in scope_plan.get("risks", [])
    ) or "None identified."

    task_prompt = EXECUTION_PROMPT.format(
        issue_id=planned_issue["id"],
        title=planned_issue["title"],
        description=planned_issue["description"],
        labels=", ".join(planned_issue.get("labels", [])),
        issue_type=planned_issue.get("issue_type", "unknown"),
        complexity=planned_issue.get("complexity", "unknown"),
        scope=planned_issue.get("scope", "unknown"),
        summary=planned_issue.get("summary", planned_issue["title"]),
        root_cause=scope_plan.get("root_cause_hypothesis", "See session for details."),
        affected_files=affected_files or "- See Scope session for details.",
        task_breakdown=task_breakdown or "1. Implement the fix per the Scope session.",
        scope_confidence=scope_plan.get("confidence_score", 0),
        risks=risks,
    )
    return f"Work on the GitHub repository https://github.com/{TARGET_REPO}.\n\n{task_prompt}"


def _create_devin_session(planned_issue: dict, scope_plan: dict) -> dict:
    """
    Create a Devin execution session for a single issue.
    Copies Scope estimates into the ExecutionSession for Optimizer comparison.
    """
    issue_id = planned_issue["id"]
    prompt = _build_prompt(planned_issue, scope_plan)

    headers = {
        "Authorization": f"Bearer {DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            f"{DEVIN_API_BASE}/sessions",
            headers=headers,
            json={"prompt": prompt, "bypass_approval": True},
            timeout=30,
        )

        if response.status_code in (200, 201):
            data = response.json()
            session_id = data.get("session_id", "unknown")
            session_url = data.get("url", f"https://app.devin.ai/sessions/{session_id}")
            status = "In Progress"
            outcome = (
                f"Devin session created. Session ID: {session_id}. "
                f"Devin is implementing the Scope plan against {TARGET_REPO}."
            )

            # Save to persistent store, carrying Scope estimates for Optimizer
            store.set_execution(issue_id, {
                "issue_id": issue_id,
                "session_id": session_id,
                "session_url": session_url,
                "status": status,
                "outcome_summary": outcome,
                "pull_requests": [],
                "dispatched_at": datetime.now().isoformat(),
                "completed_at": None,
                "estimated_lines_changed": scope_plan.get("estimated_lines_changed", 0),
                "estimated_files": scope_plan.get("affected_files", []),
            })

            return {
                "id": issue_id,
                "title": planned_issue["title"],
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
                "id": issue_id,
                "title": planned_issue["title"],
                "status": status,
                "outcome_summary": outcome,
                "session_url": None,
                "already_dispatched": False,
            }

    except requests.exceptions.RequestException as e:
        return {
            "id": issue_id,
            "title": planned_issue["title"],
            "status": "Blocked",
            "outcome_summary": f"Could not reach Devin API: {str(e)}",
            "session_url": None,
            "already_dispatched": False,
        }


def _poll_session(session_id: str):
    """GET the current state of a Devin session. Returns raw API dict or None."""
    headers = {"Authorization": f"Bearer {DEVIN_API_KEY}"}
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


def _map_devin_status(session_data: dict) -> str:
    """
    Map Devin's session status to our display status.

    Devin statuses: new, running, paused, stopped, finished, blocked
    Our statuses:   In Progress, Awaiting Review, Completed, Blocked
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
        return "Awaiting Review" if has_prs else "In Progress"
    if devin_status == "paused":
        return "Awaiting Review"
    return "In Progress"


def _build_outcome_summary(session_data: dict) -> str:
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
