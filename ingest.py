"""
ingest.py — Stage 1: Ingest

Normalises, deduplicates, and classifies raw GitHub issues into a consistent
schema. Does NOT prioritise, score, or recommend — those are Planner's job.

Output: list[IngestedIssue]
"""

import json
import time
import requests
from datetime import datetime
from typing import Optional

from config import DEVIN_API_BASE, DEVIN_API_KEY, INGEST_TIMEOUT, POLL_INTERVAL

# ---------------------------------------------------------------------------
# Classification keyword lists
# ---------------------------------------------------------------------------

BUG_KEYWORDS = ["bug", "error", "broken", "fix", "crash", "fail", "500", "404", "truncated"]
FEATURE_KEYWORDS = ["feature-request", "add", "build", "new", "support", "integration"]
TECH_DEBT_KEYWORDS = ["tech-debt", "refactor", "migrate", "update", "cleanup"]
INVESTIGATION_KEYWORDS = ["investigate", "slow", "unknown", "no clear", "needs investigation"]

HIGH_COMPLEXITY_SIGNALS = [
    "architecture", "migrate", "refactor", "evaluate", "multiple services",
    "design-system", "oauth", "pipeline", "downstream", "80+", "12 modules",
]
BROAD_SCOPE_SIGNALS = [
    "across the platform", "all component", "all user-facing",
    "4-5 downstream", "multiple customers", "12 modules",
]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalize_issue(issue: dict) -> dict:
    """
    Strip nulls, trim whitespace, and normalise label casing.
    Returns a clean copy — does not mutate the input.
    """
    return {
        **issue,
        "title": (issue.get("title") or "").strip(),
        "description": (issue.get("description") or "").strip(),
        "labels": [str(l).lower().strip() for l in (issue.get("labels") or [])],
    }


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity on word sets."""
    set_a = set(a.lower().split())
    set_b = set(b.lower().split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def dedupe_check(issue: dict, all_issues: list) -> Optional[int]:
    """
    Check whether a similar issue already exists in the batch.
    Returns the issue_id of the first suspected duplicate (Jaccard ≥ 0.7),
    or None if no duplicate is found. Skips self-comparison.
    """
    for other in all_issues:
        if other["id"] == issue["id"]:
            continue
        if _jaccard(issue["title"], other["title"]) >= 0.7:
            return other["id"]
    return None


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def classify_issue_type(issue: dict) -> str:
    """Determine if the issue is a bug, feature request, tech debt, or investigation."""
    title_lower = issue["title"].lower()
    desc_lower = issue["description"].lower()
    labels_lower = issue["labels"]  # already normalised by normalize_issue
    combined = title_lower + " " + desc_lower + " ".join(labels_lower)

    if any(kw in labels_lower for kw in ["bug"]) or any(kw in combined for kw in BUG_KEYWORDS):
        return "bug"
    if any(kw in combined for kw in INVESTIGATION_KEYWORDS):
        return "investigation"
    if any(kw in labels_lower for kw in ["feature-request"]) or any(kw in combined for kw in FEATURE_KEYWORDS):
        return "feature_request"
    if any(kw in labels_lower for kw in ["tech-debt"]) or any(kw in combined for kw in TECH_DEBT_KEYWORDS):
        return "tech_debt"
    return "other"


def classify_complexity(issue: dict) -> str:
    """Rate complexity as low, medium, or high based on description signals."""
    combined = (issue["title"] + " " + issue["description"]).lower()

    if any(signal in combined for signal in HIGH_COMPLEXITY_SIGNALS):
        return "high"

    desc_length = len(issue["description"])
    if desc_length < 250 and issue["comments_count"] <= 3:
        return "low"

    return "medium"


def classify_scope(issue: dict) -> str:
    """Determine if the issue is narrow (one component) or broad (cross-cutting)."""
    combined = (issue["title"] + " " + issue["description"]).lower()
    if any(signal in combined for signal in BROAD_SCOPE_SIGNALS):
        return "broad"
    return "narrow"


def assess_risk(issue: dict, issue_type: str, complexity: str, scope: str) -> str:
    """Assess risk level based on type, complexity, scope, and labels."""
    labels_lower = issue["labels"]  # already normalised

    if any(s in labels_lower for s in ["auth", "billing", "architecture"]):
        return "high"
    if complexity == "high" and scope == "broad":
        return "high"
    return "low"


def generate_summary(issue: dict, issue_type: str, complexity: str) -> str:
    """Create a one-line plain-language summary of the issue."""
    title = issue["title"]
    if issue_type == "bug":
        return f"Bug: {title}. Reported {issue['age_days']} days ago with {issue['comments_count']} comments."
    if issue_type == "feature_request":
        return f"Feature request: {title}. Open for {issue['age_days']} days."
    if issue_type == "investigation":
        return f"Investigation needed: {title}. Root cause unclear."
    if issue_type == "tech_debt":
        return f"Tech debt: {title}. {complexity.capitalize()} complexity cleanup."
    return f"{title} — open for {issue['age_days']} days."


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def ingest_issues(raw_issues: list) -> list:
    """
    Stage 1: Ingest.

    Normalises, deduplicates, and classifies raw GitHub issues.
    Does NOT score, rank, or recommend — that is the Planner's job.

    Returns a list of IngestedIssue dicts.
    """
    ingested = []

    for raw in raw_issues:
        issue = normalize_issue(raw)
        issue_type = classify_issue_type(issue)
        complexity = classify_complexity(issue)
        scope = classify_scope(issue)
        risk = assess_risk(issue, issue_type, complexity, scope)
        summary = generate_summary(issue, issue_type, complexity)
        duplicate_of = dedupe_check(issue, raw_issues)

        ingested.append({
            **issue,
            "summary": summary,
            "issue_type": issue_type,
            "complexity": complexity,
            "scope": scope,
            "risk": risk,
            "duplicate_of": duplicate_of,
            "ingested_at": datetime.now().isoformat(),
        })

    return ingested


# ---------------------------------------------------------------------------
# Devin-powered ingest (Stage 1 — premium path)
# ---------------------------------------------------------------------------

def ingest_issues_with_devin(raw_issues: list) -> dict:
    """
    Stage 1 (Devin-powered): Send the full batch of raw GitHub issues to a
    single Devin session for intelligent normalisation and classification.

    Unlike the rule-based path, Devin reads between the lines of vague
    descriptions, corrects inconsistent labels, and flags subtle duplicates.

    Returns a dict:
      {
        "status": "pending" | "complete" | "error",
        "session_id": str,
        "session_url": str,
        "issues": list[IngestedIssue] | [],
        "error": str | None,
      }
    Caller should save the session state to store and poll/display accordingly.
    """
    from prompts import INGEST_PROMPT, PLATFORM_CONTEXT

    issues_json = json.dumps(raw_issues, indent=2)
    prompt = INGEST_PROMPT.format(
        platform_context=PLATFORM_CONTEXT,
        issues_json=issues_json,
    )

    headers = {
        "Authorization": f"Bearer {DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }

    print(f"[ingest] Creating Devin ingest session for {len(raw_issues)} issues…")
    try:
        response = requests.post(
            f"{DEVIN_API_BASE}/sessions",
            headers=headers,
            json={"prompt": prompt, "bypass_approval": True},
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        return {"status": "error", "session_id": "", "session_url": "",
                "issues": [], "error": f"Could not reach Devin API: {e}"}

    if response.status_code not in (200, 201):
        return {"status": "error", "session_id": "", "session_url": "",
                "issues": [],
                "error": f"API returned {response.status_code}: {response.text[:200]}"}

    data = response.json()
    session_id  = data.get("session_id", "")
    session_url = data.get("url", f"https://app.devin.ai/sessions/{session_id}")
    print(f"[ingest] Session created: {session_url}")

    # Poll until done.
    # Devin text-output sessions (no repo) often complete their task and then
    # "await instructions" — which may appear as "running" indefinitely in the API
    # rather than a true terminal status. So we also exit early when valid JSON
    # is detectable in the response, regardless of reported status.
    deadline = time.time() + INGEST_TIMEOUT
    attempt  = 0
    result   = None
    # "claimed" = session created but Devin hasn't started yet — keep polling
    _NON_TERMINAL = ("running", "starting", "queued", "initializing", "created", "claimed")
    while time.time() < deadline:
        attempt += 1
        try:
            r = requests.get(
                f"{DEVIN_API_BASE}/sessions/{session_id}",
                headers={"Authorization": f"Bearer {DEVIN_API_KEY}"},
                timeout=15,
            )
            if r.status_code == 200:
                session = r.json()
                status  = session.get("status", "").lower()
                detail  = session.get("status_detail", "")
                print(f"[ingest] Poll #{attempt}: status={status!r} detail={detail!r}")

                # Exit on any status that isn't a known in-progress state
                if status not in _NON_TERMINAL:
                    result = session
                    break

                # Devin stays status="running" after finishing; done state is
                # signalled by status_detail="waiting_for_user" (Devin went to sleep).
                if detail == "waiting_for_user":
                    print(f"[ingest] Poll #{attempt}: Devin finished (waiting_for_user)")
                    result = session
                    break

                # Also exit early if extractable JSON appears in the response body.
                if attempt >= 3 and _extract_json_array(session):
                    print(f"[ingest] Poll #{attempt}: JSON found in response, exiting early")
                    result = session
                    break
        except requests.exceptions.RequestException as e:
            print(f"[ingest] Poll #{attempt}: {e}")
        time.sleep(POLL_INTERVAL)

    if not result:
        return {"status": "error", "session_id": session_id, "session_url": session_url,
                "issues": [],
                "error": f"Timed out after {INGEST_TIMEOUT // 60} min. Session: {session_url}"}

    # Extract JSON array from session output.
    # First try the session data itself; if that fails, try the messages endpoint.
    parsed = _extract_json_array(result)
    if not parsed:
        parsed = _fetch_and_extract_messages(session_id)
    if not parsed:
        return {"status": "error", "session_id": session_id, "session_url": session_url,
                "issues": [],
                "error": f"Could not parse ingest JSON from session or messages. Session: {session_url}"}

    # Stamp ingested_at and normalise any missing fields
    now = datetime.now().isoformat()
    ingested = []
    for item in parsed:
        ingested.append({
            **item,
            "ingested_at": now,
        })

    print(f"[ingest] Done — {len(ingested)} issues normalised by Devin.")
    return {
        "status": "complete",
        "session_id": session_id,
        "session_url": session_url,
        "issues": ingested,
        "error": None,
    }


def _extract_json_array(session_data: dict):
    """
    Extract a JSON array from Devin session output. Returns list or None.
    Checks structured_output, then scans message lists under several field names,
    then checks top-level text fields as a last resort.
    """
    # Try structured_output first
    structured = session_data.get("structured_output")
    if isinstance(structured, list) and structured:
        return structured

    # Build a flat list of message-like dicts from any message-list field
    candidates = []
    for msg_field in ("messages", "items", "conversation", "history"):
        msgs = session_data.get(msg_field) or []
        if isinstance(msgs, list):
            candidates.extend(msgs)

    for message in reversed(candidates):
        if not isinstance(message, dict):
            continue
        # Devin v3 may use "content", "message", "text", or "body"
        for content_field in ("content", "message", "text", "body"):
            content = message.get(content_field, "")
            if not content or not isinstance(content, str):
                continue
            try:
                parsed = json.loads(content.strip())
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, AttributeError):
                pass
            start = content.find("[")
            end   = content.rfind("]") + 1
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(content[start:end])
                    if isinstance(parsed, list):
                        return parsed
                except json.JSONDecodeError:
                    pass

    # Last resort: check top-level string fields (output, result, response, etc.)
    for field in ("output", "result", "response", "output_text", "last_message"):
        val = session_data.get(field)
        if not val or not isinstance(val, str):
            continue
        try:
            parsed = json.loads(val.strip())
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, AttributeError):
            pass
        start = val.find("[")
        end   = val.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(val[start:end])
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass

    return None


def _fetch_and_extract_messages(session_id: str):
    """
    Fallback: fetch session messages from the Devin messages endpoint and
    scan them for a JSON array. Returns list or None.
    """
    endpoints = [
        f"{DEVIN_API_BASE}/sessions/{session_id}/messages",
        f"{DEVIN_API_BASE}/sessions/{session_id}?include_messages=true",
    ]
    for url in endpoints:
        try:
            r = requests.get(
                url,
                headers={"Authorization": f"Bearer {DEVIN_API_KEY}"},
                timeout=15,
            )
            print(f"[ingest] Messages endpoint {url} → HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                print(f"[ingest] Messages response type={type(data).__name__} "
                      f"keys={list(data.keys()) if isinstance(data, dict) else f'list[{len(data)}]'}")
                if isinstance(data, list):
                    result = _extract_json_array({"messages": data})
                else:
                    result = _extract_json_array(data)
                if result:
                    print(f"[ingest] Extracted JSON from fallback endpoint: {url}")
                    return result
        except requests.exceptions.RequestException as e:
            print(f"[ingest] Messages endpoint {url} → error: {e}")
    return None
