"""
planner.py — Stage 2: Planner

Takes IngestedIssue records and produces PlannedIssue records with a four-
dimension priority score, a recommendation flag, and lightweight implementation
options.

Does NOT call Devin. Does NOT write code. Does NOT open sessions.
The Planner decides WHAT to work on. The Scope stage decides HOW to build it.

Output: list[PlannedIssue] sorted by total_score descending (rank 1 = best)
"""

import json
import os
import time
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_DEVIN_API_KEY  = os.getenv("DEVIN_API_KEY")
_DEVIN_ORG_ID   = os.getenv("DEVIN_ORG_ID")
_DEVIN_API_BASE = f"https://api.devin.ai/v3/organizations/{_DEVIN_ORG_ID}"
_PLANNER_TIMEOUT = 480   # 8 minutes for a full batch
_POLL_INTERVAL   = 10

# ---------------------------------------------------------------------------
# Configurable PM weights
# Exposed in app.py as sidebar sliders so a PM can tune priorities in real time.
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS = {
    "user_impact":      0.35,
    "business_impact":  0.25,
    "effort":           0.20,   # inverted before weighting: (10 - effort)
    "confidence":       0.20,
}

# Threshold above which an issue is "recommended" for automation
RECOMMEND_THRESHOLD = 6.0

# Labels that signal high business or user priority
PRIORITY_LABELS = {"p1", "priority-high", "customer-facing", "revenue", "sla", "critical"}
BUSINESS_LABELS = {"revenue", "billing", "compliance", "customer-facing", "sla", "p1"}


# ---------------------------------------------------------------------------
# Scoring functions — each returns an integer 0–10
# ---------------------------------------------------------------------------

def score_user_impact(issue: dict) -> int:
    """
    How many users are affected and how severely?
    Signals: issue age (staler = more impact), comment count, type, priority labels.
    """
    score = 5  # baseline

    # Issue type: bugs hurt users now; investigations are unclear
    if issue["issue_type"] == "bug":
        score += 2
    elif issue["issue_type"] == "investigation":
        score -= 2
    elif issue["issue_type"] == "feature_request":
        score -= 1

    # Age: stale bugs have broader user impact
    age = issue.get("age_days", 0)
    if age > 60:
        score += 2
    elif age > 30:
        score += 1

    # Comment volume: more comments = more users affected
    comments = issue.get("comments_count", 0)
    if comments >= 10:
        score += 2
    elif comments >= 5:
        score += 1

    # Priority labels
    labels = set(issue.get("labels", []))
    if labels & PRIORITY_LABELS:
        score += 2

    return max(0, min(10, score))


def score_business_impact(issue: dict) -> int:
    """
    How much does this affect revenue, compliance, or customer-facing features?
    """
    score = 4  # baseline

    labels = set(issue.get("labels", []))
    if labels & BUSINESS_LABELS:
        score += 3

    # High-risk issues (auth, billing) that made it this far = moderate business value
    if issue["risk"] == "high":
        score += 1

    # Bugs are business impacting; investigations and feature requests less so
    if issue["issue_type"] == "bug":
        score += 2
    elif issue["issue_type"] == "tech_debt":
        score += 1

    return max(0, min(10, score))


def score_effort(issue: dict) -> int:
    """
    How hard is this to implement? 10 = hardest.
    This score is INVERTED before being included in total_score:
    total contribution = (10 - effort) * weight.
    """
    complexity = issue.get("complexity", "medium")
    scope = issue.get("scope", "narrow")

    effort_map = {
        ("low",    "narrow"): 2,
        ("low",    "broad"):  4,
        ("medium", "narrow"): 5,
        ("medium", "broad"):  7,
        ("high",   "narrow"): 8,
        ("high",   "broad"):  9,
    }
    return effort_map.get((complexity, scope), 5)


def score_confidence(issue: dict) -> int:
    """
    How likely is autonomous resolution to succeed?
    Mirrors the old evaluate_candidate logic but returns a 0–10 score.
    """
    issue_type = issue.get("issue_type", "other")
    complexity = issue.get("complexity", "medium")
    scope = issue.get("scope", "narrow")
    risk = issue.get("risk", "low")

    if risk == "high":
        return 2   # Devin can attempt but human review likely needed

    if issue_type == "investigation":
        return 1   # Unknown root cause = very low automation confidence

    if issue_type == "feature_request" and (scope == "broad" or complexity == "high"):
        return 1

    if complexity == "high":
        return 2

    if scope == "broad":
        return 3

    if issue_type == "bug" and complexity == "low" and scope == "narrow":
        return 10
    if issue_type == "bug" and complexity == "medium" and scope == "narrow":
        return 8
    if issue_type == "tech_debt" and complexity == "low" and scope == "narrow":
        return 7
    if issue_type == "tech_debt" and complexity == "medium" and scope == "narrow":
        return 6
    if issue_type == "feature_request" and complexity == "low" and scope == "narrow":
        return 5

    return 4


def compute_total_score(scores: dict, weights: dict) -> float:
    """
    Weighted sum. Effort is inverted so that easier issues score higher.
    Normalises weights so they always sum to 1.0, keeping the result in [0, 10]
    regardless of what values the sidebar sliders are set to.
    """
    total_weight = sum(weights.values()) or 1.0
    w = {k: v / total_weight for k, v in weights.items()}
    return round(
        scores["user_impact"]     * w["user_impact"] +
        scores["business_impact"] * w["business_impact"] +
        (10 - scores["effort"])   * w["effort"] +
        scores["confidence"]      * w["confidence"],
        2,
    )


# ---------------------------------------------------------------------------
# Recommendation logic
# ---------------------------------------------------------------------------

def generate_implementation_options(issue: dict) -> list:
    """
    Suggest 1–3 plain-English implementation options.
    Rule-based, no code. The Scope stage will refine these into a build plan.
    """
    issue_type = issue.get("issue_type", "other")
    complexity = issue.get("complexity", "medium")
    title = issue.get("title", "")

    if issue_type == "bug" and complexity == "low":
        return [
            f"Locate and patch the defect directly in the relevant module.",
            f"Add a regression test to confirm the fix holds.",
        ]
    if issue_type == "bug" and complexity == "medium":
        return [
            f"Trace the failure path from the symptom to the root cause, then apply a targeted fix.",
            f"Consider whether a more defensive guard upstream would prevent recurrence.",
        ]
    if issue_type == "tech_debt":
        return [
            f"Identify all call sites and refactor incrementally.",
            f"Ensure existing tests pass before and after the change.",
        ]
    if issue_type == "feature_request":
        return [
            f"Implement the minimal version of the feature behind a flag.",
            f"Wire into existing patterns rather than introducing new abstractions.",
        ]
    return [f"Review the issue description and find the simplest correct fix."]


def recommend(total_score: float, issue: dict) -> tuple:
    """
    Decide whether to recommend this issue for autonomous resolution.
    Returns (recommended: bool, reason: str).
    """
    issue_type = issue.get("issue_type", "other")
    risk = issue.get("risk", "low")

    if risk == "high":
        return False, f"High-risk area (auth/billing/architecture) — requires human oversight (score {total_score:.1f}/10)"

    if issue_type == "investigation":
        return False, f"Root cause is unknown — needs human investigation before automation (score {total_score:.1f}/10)"

    if total_score < RECOMMEND_THRESHOLD:
        return False, f"Score {total_score:.1f}/10 is below the automation threshold of {RECOMMEND_THRESHOLD} — keep human-owned"

    return True, f"Score {total_score:.1f}/10 — good automation candidate based on impact, effort, and confidence"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def plan_issues(ingested: list, weights: dict = None) -> list:
    """
    Stage 2: Planner.

    Scores, ranks, and annotates each ingested issue.
    Returns a list of PlannedIssue dicts sorted by total_score descending
    (priority_rank 1 = highest priority).
    """
    if weights is None:
        weights = DEFAULT_WEIGHTS

    scored = []

    for issue in ingested:
        ui = score_user_impact(issue)
        bi = score_business_impact(issue)
        ef = score_effort(issue)
        co = score_confidence(issue)
        total = compute_total_score({"user_impact": ui, "business_impact": bi,
                                     "effort": ef, "confidence": co}, weights)
        recommended, reason = recommend(total, issue)
        options = generate_implementation_options(issue) if recommended else []

        scored.append({
            **issue,
            "planner_score": {
                "user_impact":          ui,
                "business_impact":      bi,
                "effort":               ef,
                "confidence":           co,
                "total_score":          total,
                "recommended":          recommended,
                "recommendation_reason": reason,
                "priority_rank":        0,  # filled in after sort
            },
            "implementation_options": options,
            "planned_at": datetime.now().isoformat(),
        })

    # Sort by total_score descending and assign priority ranks
    scored.sort(key=lambda x: x["planner_score"]["total_score"], reverse=True)
    for rank, issue in enumerate(scored, start=1):
        issue["planner_score"]["priority_rank"] = rank

    return scored


# ---------------------------------------------------------------------------
# Devin-powered planner (Stage 2 — premium path)
# ---------------------------------------------------------------------------

def plan_issues_with_devin(ingested: list) -> dict:
    """
    Stage 2 (Devin-powered): Send the full batch of normalised issues to a
    single Devin session for intelligent prioritisation, ranking, and scoping.

    Unlike the rule-based path, Devin reasons about business context, relative
    priority across the full batch, and produces narrative reasoning a PM can act on.

    Returns a dict:
      {
        "status": "pending" | "complete" | "error",
        "session_id": str,
        "session_url": str,
        "issues": list[PlannedIssue] | [],
        "error": str | None,
      }
    """
    from prompts import PLANNER_PROMPT, PLATFORM_CONTEXT

    issues_json = json.dumps(ingested, indent=2)
    prompt = PLANNER_PROMPT.format(
        platform_context=PLATFORM_CONTEXT,
        issues_json=issues_json,
    )

    headers = {
        "Authorization": f"Bearer {_DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }

    print(f"[planner] Creating Devin planner session for {len(ingested)} issues…")
    try:
        response = requests.post(
            f"{_DEVIN_API_BASE}/sessions",
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
    print(f"[planner] Session created: {session_url}")

    # Poll until done.
    # Same early-exit strategy as ingest: detect JSON in the response rather than
    # relying solely on a terminal status that may never arrive for text-output sessions.
    deadline = time.time() + _PLANNER_TIMEOUT
    attempt  = 0
    result   = None
    _NON_TERMINAL = ("running", "starting", "queued", "initializing", "created", "claimed")
    while time.time() < deadline:
        attempt += 1
        try:
            r = requests.get(
                f"{_DEVIN_API_BASE}/sessions/{session_id}",
                headers={"Authorization": f"Bearer {_DEVIN_API_KEY}"},
                timeout=15,
            )
            if r.status_code == 200:
                session = r.json()
                status  = session.get("status", "").lower()
                detail  = session.get("status_detail", "")
                print(f"[planner] Poll #{attempt}: status={status!r} detail={detail!r}")

                if status not in _NON_TERMINAL:
                    result = session
                    break

                if detail == "waiting_for_user":
                    print(f"[planner] Poll #{attempt}: Devin finished (waiting_for_user)")
                    result = session
                    break

                if attempt >= 3 and _extract_json_array(session):
                    print(f"[planner] Poll #{attempt}: JSON found in response, exiting early")
                    result = session
                    break
        except requests.exceptions.RequestException as e:
            print(f"[planner] Poll #{attempt}: {e}")
        time.sleep(_POLL_INTERVAL)

    if not result:
        return {"status": "error", "session_id": session_id, "session_url": session_url,
                "issues": [],
                "error": f"Timed out after {_PLANNER_TIMEOUT // 60} min. Session: {session_url}"}

    # Extract JSON — try session data first, then the messages endpoint as fallback.
    parsed = _extract_json_array(result)
    if not parsed:
        parsed = _fetch_and_extract_messages(session_id)
    if not parsed:
        return {"status": "error", "session_id": session_id, "session_url": session_url,
                "issues": [],
                "error": f"Could not parse planner JSON from session or messages. Session: {session_url}"}

    # Merge Devin's scores with the full ingested issue data
    now = datetime.now().isoformat()
    ingested_by_id = {str(i["id"]): i for i in ingested}
    planned = []
    for item in parsed:
        issue_id = str(item.get("id", ""))
        base = ingested_by_id.get(issue_id, {})
        planned.append({
            **base,
            "planner_score": {
                "user_impact":           item.get("user_impact", 5),
                "business_impact":       item.get("business_impact", 4),
                "effort":                item.get("effort", 5),
                "confidence":            item.get("confidence", 4),
                "total_score":           item.get("total_score", 0.0),
                "recommended":           item.get("recommended", False),
                "recommendation_reason": item.get("recommendation_reason", ""),
                "priority_rank":         item.get("priority_rank", 0),
            },
            "implementation_options": item.get("implementation_options", []),
            "scope_summary":          item.get("scope_summary", ""),
            "planned_at": now,
        })

    # Sort by total_score descending (Devin may have already ranked, but ensure it)
    planned.sort(key=lambda x: x["planner_score"]["total_score"], reverse=True)

    print(f"[planner] Done — {len(planned)} issues prioritised by Devin.")
    return {
        "status": "complete",
        "session_id": session_id,
        "session_url": session_url,
        "issues": planned,
        "error": None,
    }


def _extract_json_array(session_data: dict):
    """
    Extract a JSON array from Devin session output. Returns list or None.
    Checks structured_output, then message lists under several field names,
    then top-level text fields as a last resort.
    """
    structured = session_data.get("structured_output")
    if isinstance(structured, list) and structured:
        return structured

    candidates = []
    for msg_field in ("messages", "items", "conversation", "history"):
        msgs = session_data.get(msg_field) or []
        if isinstance(msgs, list):
            candidates.extend(msgs)

    for message in reversed(candidates):
        if not isinstance(message, dict):
            continue
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


# ---------------------------------------------------------------------------
# Devin-powered combined analysis (Stages 1+2 — preferred Devin path)
# ---------------------------------------------------------------------------

def analyse_issues_with_devin(raw_issues: list) -> dict:
    """
    Combined Stage 1+2 (Devin-powered): normalise AND score/rank raw GitHub
    issues in a single Devin session.

    Takes raw issues directly (no pre-normalisation needed). Returns a dict:
      {
        "status": "complete" | "error",
        "session_id": str,
        "session_url": str,
        "issues": list[PlannedIssue] | [],
        "error": str | None,
      }
    """
    from prompts import ANALYSIS_PROMPT, PLATFORM_CONTEXT

    issues_json = json.dumps(raw_issues, indent=2)
    prompt = ANALYSIS_PROMPT.format(
        platform_context=PLATFORM_CONTEXT,
        issues_json=issues_json,
    )

    headers = {
        "Authorization": f"Bearer {_DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }

    print(f"[analyse] Creating Devin analysis session for {len(raw_issues)} issues…")
    try:
        response = requests.post(
            f"{_DEVIN_API_BASE}/sessions",
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
    print(f"[analyse] Session created: {session_url}")

    # Poll until done — same terminal detection as ingest/planner
    deadline = time.time() + _PLANNER_TIMEOUT
    attempt  = 0
    result   = None
    _NON_TERMINAL = ("running", "starting", "queued", "initializing", "created", "claimed")
    while time.time() < deadline:
        attempt += 1
        try:
            r = requests.get(
                f"{_DEVIN_API_BASE}/sessions/{session_id}",
                headers={"Authorization": f"Bearer {_DEVIN_API_KEY}"},
                timeout=15,
            )
            if r.status_code == 200:
                session = r.json()
                status = session.get("status", "").lower()
                detail = session.get("status_detail", "")
                print(f"[analyse] Poll #{attempt}: status={status!r} detail={detail!r}")

                if status not in _NON_TERMINAL:
                    result = session
                    break
                if detail == "waiting_for_user":
                    print(f"[analyse] Poll #{attempt}: Devin finished (waiting_for_user)")
                    result = session
                    break
                if attempt >= 3 and _extract_json_array(session):
                    print(f"[analyse] Poll #{attempt}: JSON found in response, exiting early")
                    result = session
                    break
        except requests.exceptions.RequestException as e:
            print(f"[analyse] Poll #{attempt}: {e}")
        time.sleep(_POLL_INTERVAL)

    if not result:
        return {"status": "error", "session_id": session_id, "session_url": session_url,
                "issues": [],
                "error": f"Timed out after {_PLANNER_TIMEOUT // 60} min. Session: {session_url}"}

    parsed = _extract_json_array(result)
    if not parsed:
        parsed = _fetch_and_extract_messages(session_id, label="analyse")
    if not parsed:
        return {"status": "error", "session_id": session_id, "session_url": session_url,
                "issues": [],
                "error": f"Could not parse analysis JSON. Session: {session_url}"}

    # Build PlannedIssue records from the combined output
    now = datetime.now().isoformat()
    raw_by_id = {str(i["id"]): i for i in raw_issues}
    planned = []
    for item in parsed:
        issue_id = str(item.get("id", ""))
        base = raw_by_id.get(issue_id, {})
        planned.append({
            **base,
            # Normalised fields from Devin
            "title":       item.get("title",       base.get("title", "")),
            "labels":      item.get("labels",      base.get("labels", [])),
            "summary":     item.get("summary",     ""),
            "issue_type":  item.get("issue_type",  "other"),
            "complexity":  item.get("complexity",  "medium"),
            "scope":       item.get("scope",       "narrow"),
            "risk":        item.get("risk",        "low"),
            "duplicate_of": item.get("duplicate_of"),
            # Scoring fields from Devin
            "planner_score": {
                "user_impact":           item.get("user_impact",   5),
                "business_impact":       item.get("business_impact", 4),
                "effort":                item.get("effort",        5),
                "confidence":            item.get("confidence",    4),
                "total_score":           item.get("total_score",   0.0),
                "recommended":           item.get("recommended",   False),
                "recommendation_reason": item.get("recommendation_reason", ""),
                "priority_rank":         item.get("priority_rank", 0),
            },
            "implementation_options": item.get("implementation_options", []),
            "scope_summary":          item.get("scope_summary", ""),
            "ingested_at": now,
            "planned_at":  now,
        })

    # Ensure sorted by priority_rank
    planned.sort(key=lambda x: x["planner_score"]["priority_rank"])

    print(f"[analyse] Done — {len(planned)} issues analysed by Devin.")
    return {
        "status":      "complete",
        "session_id":  session_id,
        "session_url": session_url,
        "issues":      planned,
        "error":       None,
    }


def _fetch_and_extract_messages(session_id: str, label: str = "planner"):
    """
    Fallback: fetch session messages from the Devin messages endpoint and
    scan them for a JSON array. Returns list or None.
    """
    endpoints = [
        f"{_DEVIN_API_BASE}/sessions/{session_id}/messages",
        f"{_DEVIN_API_BASE}/sessions/{session_id}?include_messages=true",
    ]
    for url in endpoints:
        try:
            r = requests.get(
                url,
                headers={"Authorization": f"Bearer {_DEVIN_API_KEY}"},
                timeout=15,
            )
            print(f"[{label}] Messages endpoint {url} → HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                print(f"[{label}] Messages response type={type(data).__name__} "
                      f"keys={list(data.keys()) if isinstance(data, dict) else f'list[{len(data)}]'}")
                if isinstance(data, list):
                    result = _extract_json_array({"messages": data})
                else:
                    result = _extract_json_array(data)
                if result:
                    print(f"[{label}] Extracted JSON from fallback endpoint: {url}")
                    return result
        except requests.exceptions.RequestException as e:
            print(f"[{label}] Messages endpoint {url} → error: {e}")
    return None
