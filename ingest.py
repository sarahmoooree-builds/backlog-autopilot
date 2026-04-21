"""
ingest.py — Stage 1: Ingest

Normalises, deduplicates, and classifies raw GitHub issues into a consistent
schema. Does NOT prioritise, score, or recommend — those are Planner's job.

Output: list[IngestedIssue]
"""

import json
import logging
import requests
from datetime import datetime
from typing import Optional

import devin_client
from config import INGEST_TIMEOUT, POLL_INTERVAL

logger = logging.getLogger(__name__)

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

    logger.info("Creating Devin ingest session for %d issues…", len(raw_issues))
    try:
        created = devin_client.create_session(prompt, bypass_approval=True)
    except requests.exceptions.RequestException as e:
        return {"status": "error", "session_id": "", "session_url": "",
                "issues": [], "error": f"Could not reach Devin API: {e}"}
    except RuntimeError as e:
        return {"status": "error", "session_id": "", "session_url": "",
                "issues": [], "error": str(e)}

    session_id  = created["session_id"]
    session_url = created["session_url"]
    logger.info("Session created: %s", session_url)

    result = devin_client.poll_until_done(
        session_id,
        timeout=INGEST_TIMEOUT,
        poll_interval=POLL_INTERVAL,
        label="ingest",
    )

    if not result:
        return {"status": "error", "session_id": session_id, "session_url": session_url,
                "issues": [],
                "error": f"Timed out after {INGEST_TIMEOUT // 60} min. Session: {session_url}"}

    # Extract JSON array from session output.
    # First try the session data itself; if that fails, fetch the paginated
    # messages endpoint as a fallback.
    parsed = devin_client.extract_json_array(result)
    if not parsed:
        msgs = devin_client.fetch_messages(session_id, label="ingest")
        parsed = devin_client.extract_json_array(result, messages=msgs)
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

    logger.info("Done — %d issues normalised by Devin.", len(ingested))
    return {
        "status": "complete",
        "session_id": session_id,
        "session_url": session_url,
        "issues": ingested,
        "error": None,
    }


