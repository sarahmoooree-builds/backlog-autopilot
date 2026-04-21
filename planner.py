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
import requests
from datetime import datetime

import devin_client
from config import PLANNER_TIMEOUT, POLL_INTERVAL
from priorities import PlannerStrategy, get_strategy, BALANCED_INTENT

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

def plan_issues(ingested: list, weights: dict = None,
                strategy: PlannerStrategy = None) -> list:
    """
    Stage 2: Planner.

    Scores, ranks, and annotates each ingested issue.
    Returns a list of PlannedIssue dicts sorted by total_score descending
    (priority_rank 1 = highest priority).

    Two ways to steer the ranking:
        - `strategy`: a PlannerStrategy bundling weights + small per-issue
          score bonuses. Produced by priorities.get_strategy() from a parsed
          natural-language prioritization goal. Preferred.
        - `weights`: raw weight dict (legacy). Used when `strategy` is None.

    When both are omitted the balanced default is applied.
    """
    if strategy is None and weights is None:
        strategy = get_strategy(BALANCED_INTENT)

    if strategy is not None:
        active_weights = strategy.weights
    else:
        active_weights = weights

    scored = []

    for issue in ingested:
        base_scores = {
            "user_impact":     score_user_impact(issue),
            "business_impact": score_business_impact(issue),
            "effort":          score_effort(issue),
            "confidence":      score_confidence(issue),
        }
        if strategy is not None:
            base_scores = strategy.apply_bonuses(base_scores, issue)

        total = compute_total_score(base_scores, active_weights)
        recommended, reason = recommend(total, issue)
        options = generate_implementation_options(issue) if recommended else []

        scored.append({
            **issue,
            "planner_score": {
                "user_impact":          base_scores["user_impact"],
                "business_impact":      base_scores["business_impact"],
                "effort":               base_scores["effort"],
                "confidence":           base_scores["confidence"],
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

    print(f"[planner] Creating Devin planner session for {len(ingested)} issues…")
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
    print(f"[planner] Session created: {session_url}")

    result = devin_client.poll_until_done(
        session_id,
        timeout=PLANNER_TIMEOUT,
        poll_interval=POLL_INTERVAL,
        label="planner",
    )

    if not result:
        return {"status": "error", "session_id": session_id, "session_url": session_url,
                "issues": [],
                "error": f"Timed out after {PLANNER_TIMEOUT // 60} min. Session: {session_url}"}

    # Extract JSON — try session data first, then the messages endpoint as fallback.
    parsed = devin_client.extract_json_array(result)
    if not parsed:
        msgs = devin_client.fetch_messages(session_id, label="planner")
        parsed = devin_client.extract_json_array(result, messages=msgs)
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

    print(f"[analyse] Creating Devin analysis session for {len(raw_issues)} issues…")
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
    print(f"[analyse] Session created: {session_url}")

    result = devin_client.poll_until_done(
        session_id,
        timeout=PLANNER_TIMEOUT,
        poll_interval=POLL_INTERVAL,
        label="analyse",
    )

    if not result:
        return {"status": "error", "session_id": session_id, "session_url": session_url,
                "issues": [],
                "error": f"Timed out after {PLANNER_TIMEOUT // 60} min. Session: {session_url}"}

    parsed = devin_client.extract_json_array(result)
    if not parsed:
        msgs = devin_client.fetch_messages(session_id, label="analyse")
        parsed = devin_client.extract_json_array(result, messages=msgs)
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



