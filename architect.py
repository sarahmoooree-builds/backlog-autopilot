"""
architect.py — Stage 3: Architect

Converts Planner-approved issues into technical implementation plans by
dispatching a Devin session that reads the finserv-platform codebase.

The Architect decides HOW to build each approved issue. It produces:
  - Confidence score and reasoning
  - Root cause hypothesis (specific file / function / line)
  - Affected files (confirmed in the repo by Devin)
  - Estimated lines changed
  - Ordered task breakdown (ready for the Executor to follow)
  - Dependencies and risks

The Architect does NOT write code or open PRs.
Output is saved to store.py (architect_plans section).
"""

import json
import os
import time
import requests
from datetime import datetime
from dotenv import load_dotenv

import store
from prompts import ARCHITECT_PROMPT

load_dotenv()
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY")
DEVIN_ORG_ID = os.getenv("DEVIN_ORG_ID")

TARGET_REPO = "sarahmoooree-builds/finserv-platform"
DEVIN_API_BASE = f"https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}"

ARCHITECT_TIMEOUT = 360   # seconds — Devin needs 4–6 minutes to read the codebase
POLL_INTERVAL = 10        # seconds between status polls


def architect_issue(planned_issue: dict) -> dict:
    """
    Run a Devin architect session for a single planned issue.

    Creates a Devin session, saves a pending record immediately so the UI
    shows progress, polls until finished, extracts the JSON plan, and saves
    the result to the store — including on failure, so the UI always shows
    something after this call returns.

    Returns the ArchitectPlan dict (may contain architect_status="error" on failure).
    """
    issue_id = planned_issue["id"]
    prompt = _build_architect_prompt(planned_issue)

    headers = {
        "Authorization": f"Bearer {DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }

    # --- Step 1: Create the architect session ---
    print(f"[architect] Creating Devin session for issue #{issue_id}...")
    try:
        response = requests.post(
            f"{DEVIN_API_BASE}/sessions",
            headers=headers,
            json={"prompt": prompt, "bypass_approval": True},
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        err = _error_plan(issue_id, "", f"Could not reach Devin API: {str(e)}")
        store.set_architect_plan(issue_id, err)
        return err

    if response.status_code not in (200, 201):
        err = _error_plan(issue_id, "", f"API returned {response.status_code}: {response.text[:200]}")
        store.set_architect_plan(issue_id, err)
        return err

    session_data = response.json()
    session_id = session_data.get("session_id")
    session_url = session_data.get("url", f"https://app.devin.ai/sessions/{session_id}")

    if not session_id:
        err = _error_plan(issue_id, "", "No session_id returned from Devin API")
        store.set_architect_plan(issue_id, err)
        return err

    print(f"[architect] Session created: {session_url}")

    # --- Step 2: Save pending state immediately so UI shows progress ---
    pending = _pending_plan(issue_id, session_id, session_url)
    store.set_architect_plan(issue_id, pending)

    # --- Step 3: Poll until finished ---
    result = _poll_until_done(session_id, headers)

    if not result:
        err = _error_plan(
            issue_id, session_url,
            f"Devin session timed out after {ARCHITECT_TIMEOUT // 60} minutes. "
            f"Session: {session_url}"
        )
        err["session_id"] = session_id
        store.set_architect_plan(issue_id, err)
        return err

    # --- Step 4: Extract and save the architect JSON ---
    # In the Devin v3 API the session-retrieval response does NOT include
    # messages — they live behind a separate paginated endpoint. Fetch them
    # explicitly before trying to parse the structured plan.
    final_status = (result.get("status") or "").lower()
    final_detail = (result.get("status_detail") or "").lower()
    has_structured_output = bool(result.get("structured_output"))
    print(
        f"[architect] Session terminal state for issue #{issue_id}: "
        f"status={final_status!r} detail={final_detail!r} "
        f"structured_output_present={has_structured_output}"
    )

    messages = _fetch_messages(session_id, headers)
    print(
        f"[architect] Fetched {len(messages)} message(s) for issue #{issue_id} "
        f"(devin-authored: {sum(1 for m in messages if (m.get('source') or '').lower() == 'devin')})"
    )

    # Attempt extraction even when the session is still 'running' +
    # 'waiting_for_user' / 'finished' — Devin often emits the full JSON plan
    # and then waits for further instructions rather than fully exiting.
    plan_data = _extract_architect_json(result, messages)

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
            f"[architect] Could not parse architect JSON for issue #{issue_id} "
            f"— recording error. status={final_status!r} detail={final_detail!r}"
        )
        err = _error_plan(
            issue_id, session_url,
            f"Devin finished but architect JSON could not be parsed"
            f"{suspended_note}{errored_note}{awaiting_note}. Session: {session_url}"
        )
        err["session_id"] = session_id
        store.set_architect_plan(issue_id, err)
        return err

    print(
        f"[architect] Parsed architect JSON for issue #{issue_id} "
        f"(source={'structured_output' if has_structured_output else 'messages'}, "
        f"status={final_status!r}, detail={final_detail!r})"
    )

    architect_plan = {
        "issue_id": issue_id,
        **plan_data,
        "session_id": session_id,
        "session_url": session_url,
        "architect_status": "complete",
        "error": None,
        "architected_at": datetime.now().isoformat(),
    }
    store.set_architect_plan(issue_id, architect_plan)
    print(f"[architect] Issue #{issue_id} architected. "
          f"Confidence: {plan_data.get('confidence_score')}/100")
    return architect_plan


def architect_issues(planned_issues: list) -> dict:
    """
    Run the Architect on a list of planned issues.
    Skips issues that already have a complete architect plan.
    Returns a dict of {issue_id: ArchitectPlan}.
    """
    results = {}
    for issue in planned_issues:
        existing = store.get_architect_plan(issue["id"])
        if existing and existing.get("architect_status") == "complete":
            results[issue["id"]] = existing
            continue
        results[issue["id"]] = architect_issue(issue)
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_architect_prompt(planned_issue: dict) -> str:
    """Build the architect prompt from the ARCHITECT_PROMPT template."""
    score = planned_issue.get("planner_score", {})
    options = planned_issue.get("implementation_options", [])
    return (
        f"Work on the GitHub repository https://github.com/{TARGET_REPO}.\n\n"
        + ARCHITECT_PROMPT.format(
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
        "architect_status": "pending",
        "error": None,
        "architected_at": datetime.now().isoformat(),
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
        "architect_status": "error",
        "error": error_msg,
        "architected_at": datetime.now().isoformat(),
    }


# Devin v3 statuses the architect treats as "still working" — anything else
# is treated as terminal. See:
# https://docs.devin.ai/api-reference/v3/sessions/get-organizations-session
_NON_TERMINAL_STATUSES = (
    "new", "creating", "claimed", "running", "resuming",
    # legacy aliases retained for safety if older payloads appear
    "starting", "queued", "initializing", "created",
)
# status_detail values (with status="running") that indicate Devin has
# produced its final work product and is simply idle/awaiting the next step.
_WORK_PRODUCT_READY_DETAILS = ("waiting_for_user", "finished")


def _poll_until_done(session_id: str, headers: dict):
    """
    Poll the Devin session every POLL_INTERVAL seconds until a terminal state
    or timeout. Prints status updates for visibility.
    Returns the final session dict, or None on timeout.
    """
    deadline = time.time() + ARCHITECT_TIMEOUT
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
                status = (session.get("status") or "unknown").lower()
                detail = (session.get("status_detail") or "").lower()
                print(f"[architect] Poll #{attempt}: status={status!r} detail={detail!r}")
                if status not in _NON_TERMINAL_STATUSES:
                    print(f"[architect] Poll #{attempt}: terminal status reached ({status!r})")
                    return session
                if detail in _WORK_PRODUCT_READY_DETAILS:
                    print(
                        f"[architect] Poll #{attempt}: Devin work product ready "
                        f"(status={status!r}, detail={detail!r})"
                    )
                    return session
            else:
                print(f"[architect] Poll #{attempt}: HTTP {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[architect] Poll #{attempt}: request error — {e}")

        time.sleep(POLL_INTERVAL)

    print(f"[architect] Timed out after {ARCHITECT_TIMEOUT}s ({attempt} polls)")
    return None


def _fetch_messages(session_id: str, headers: dict) -> list:
    """
    Fetch all messages for a Devin session from the v3 messages endpoint.

    The v3 session-retrieval endpoint does not include messages inline — they
    are served via a separate paginated endpoint. Returns a list of message
    dicts in chronological order (may be empty on error).
    """
    url = f"{DEVIN_API_BASE}/sessions/{session_id}/messages"
    messages: list = []
    cursor = None
    pages = 0
    max_pages = 20  # hard stop to avoid runaway pagination

    while pages < max_pages:
        params = {"first": 200}
        if cursor:
            params["after"] = cursor
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except requests.exceptions.RequestException as e:
            print(f"[architect] _fetch_messages: request error — {e}")
            break
        if response.status_code != 200:
            print(
                f"[architect] _fetch_messages: HTTP {response.status_code} "
                f"— {response.text[:200]}"
            )
            break
        payload = response.json()
        items = payload.get("items") or []
        messages.extend(items)
        if not payload.get("has_next_page"):
            break
        cursor = payload.get("end_cursor")
        if not cursor:
            break
        pages += 1
    return messages


_REQUIRED_PLAN_FIELDS = frozenset({
    "confidence_score", "confidence_reasoning", "root_cause_hypothesis",
    "affected_files", "estimated_lines_changed", "task_breakdown",
    "dependencies", "risks",
})


def _extract_architect_json(session_data: dict, messages: list | None = None):
    """
    Extract the structured architect JSON from the Devin session output.

    Order of precedence:
      1. session_data["structured_output"] (populated when a
         structured_output_schema is provided on session create).
      2. Devin-authored messages (fetched separately from the v3 messages
         endpoint), scanned most-recent first. Each message uses the
         ``message`` field in v3; we also fall back to ``content`` for
         forward/backward compatibility.

    Returns the parsed dict if all required fields are present, else None.
    """
    # 1. structured_output
    structured = session_data.get("structured_output")
    if structured and isinstance(structured, dict):
        if _REQUIRED_PLAN_FIELDS.issubset(structured.keys()):
            return structured

    # 2. scan messages in reverse (most recent first)
    candidates = messages
    if candidates is None:
        # Backward-compat: some callers may still pass session payloads that
        # contain inline messages under legacy keys.
        candidates = (session_data.get("messages")
                      or session_data.get("items")
                      or [])

    for message in reversed(candidates):
        # v3 uses "message"; older payloads used "content". Accept either.
        content = message.get("message") or message.get("content") or ""
        source = (message.get("source") or "").lower()
        # Skip user-authored messages when we can identify them.
        if source and source != "devin":
            continue
        if not content:
            continue

        parsed = _parse_plan_from_text(content)
        if parsed is not None:
            return parsed

    return None


def _parse_plan_from_text(text: str):
    """Try to extract a valid architect-plan JSON object from a text blob."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()

    # Try the whole blob as JSON.
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict) and _REQUIRED_PLAN_FIELDS.issubset(parsed.keys()):
            return parsed
    except (json.JSONDecodeError, AttributeError):
        pass

    # Try the largest balanced JSON object within the text.
    start = stripped.find("{")
    end = stripped.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            parsed = json.loads(stripped[start:end])
            if isinstance(parsed, dict) and _REQUIRED_PLAN_FIELDS.issubset(parsed.keys()):
                return parsed
        except json.JSONDecodeError:
            pass

    return None
