"""
planner.py — Stage 2: Planner

Takes IngestedIssue records and produces PlannedIssue records with an enriched
6-dimension priority score, a policy-assigned tier (1–4), a recommendation
flag, and lightweight implementation options.

Does NOT call Devin (for the rule-based path). Does NOT write code. Does NOT
open sessions. The Planner decides WHAT to work on. The Scope stage decides
HOW to build it.

Scoring dimensions (all "higher = better", no hidden inversions):
    severity        how bad it is when it happens
    reach           how many users / customers are affected
    business_value  revenue / compliance / SLA importance
    ease            higher = easier to implement (replaces old `effort`)
    confidence      automation likelihood
    urgency         time pressure (age, SLA, comment velocity)

Output: list[PlannedIssue] sorted by (tier, -score_within_tier).
"""

import json
import re
import requests
from datetime import datetime

import devin_client
from config import PLANNER_TIMEOUT, POLL_INTERVAL
from priorities import PlannerStrategy, get_strategy, BALANCED_INTENT

# ---------------------------------------------------------------------------
# Configurable PM weights
# Used when plan_issues is called with a raw weight dict instead of a
# PlannerStrategy. Kept for backward compatibility with older call sites.
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS = {
    "severity":       0.25,
    "reach":          0.20,
    "business_value": 0.20,
    "ease":           0.15,
    "confidence":     0.10,
    "urgency":        0.10,
}

# Threshold retained for backward compat with stored legacy records.
# The new recommend() path keys off tier + confidence + risk instead.
RECOMMEND_THRESHOLD = 6.0

# Label sets used inside scoring functions. Kept local so planner.py stays
# self-contained; priorities.py has its own tier-policy sets.
PRIORITY_LABELS = {"p1", "priority-high", "customer-facing", "revenue", "sla", "critical"}
BUSINESS_LABELS = {"revenue", "billing", "compliance", "customer-facing", "sla", "p1"}
_CRITICAL_LABELS = {"critical", "p1", "sla"}
_CUSTOMER_FACING_LABELS = {"customer-facing", "customer", "ux"}


# ---------------------------------------------------------------------------
# Description signal helpers
# ---------------------------------------------------------------------------

_BLOCKING_RE = re.compile(r"\b(blocks?|blocking|cannot|prevents?|breaks?)\b", re.IGNORECASE)
_DATA_LOSS_RE = re.compile(r"\b(data.?loss|corrupt|truncat|delet|crash|500)", re.IGNORECASE)
_KNOWN_FIX_RE = re.compile(r"\.(py|js|ts|jsx|tsx)\b|line\s+\d+", re.IGNORECASE)
_WIDESPREAD_RE = re.compile(r"\b(all users|widespread|multiple customers|every (user|customer))\b", re.IGNORECASE)


def _has_blocking_signal(issue: dict) -> bool:
    return bool(_BLOCKING_RE.search(issue.get("description", "") or ""))


def _has_data_loss_signal(issue: dict) -> bool:
    return bool(_DATA_LOSS_RE.search(issue.get("description", "") or ""))


def _has_known_fix_path(issue: dict) -> bool:
    return bool(_KNOWN_FIX_RE.search(issue.get("description", "") or ""))


def _has_widespread_signal(issue: dict) -> bool:
    return bool(_WIDESPREAD_RE.search(issue.get("description", "") or ""))


def _labels_of(issue: dict) -> set:
    return {str(lbl).lower() for lbl in issue.get("labels", [])}


def _clamp(value: int) -> int:
    return max(0, min(10, int(value)))


# ---------------------------------------------------------------------------
# Scoring functions — each returns an integer 0–10
# ---------------------------------------------------------------------------

def score_severity(issue: dict) -> int:
    """How bad is it when this issue hits? Blocking symptoms raise the floor."""
    score = 3
    issue_type = issue.get("issue_type", "other")
    labels = _labels_of(issue)

    if issue_type == "bug":
        score += 3
    if labels & _CRITICAL_LABELS:
        score += 2
    if issue.get("risk") == "high":
        score += 1
    if _has_blocking_signal(issue) or _has_data_loss_signal(issue):
        score += 1
    if issue_type == "feature_request":
        score -= 2

    return _clamp(score)


def score_reach(issue: dict) -> int:
    """How many users / customers are affected?"""
    score = 3
    comments = issue.get("comments_count", 0)
    labels = _labels_of(issue)

    if comments >= 10:
        score += 2
    elif comments >= 5:
        score += 1

    if labels & _CUSTOMER_FACING_LABELS:
        score += 1
    if issue.get("age_days", 0) > 60:
        score += 1
    if _has_widespread_signal(issue):
        score += 1

    return _clamp(score)


def score_business_value(issue: dict) -> int:
    """Revenue / compliance / SLA importance."""
    score = 2
    labels = _labels_of(issue)

    if labels & BUSINESS_LABELS:
        score += 3
    if issue.get("risk") == "high":
        score += 2
    if issue.get("issue_type") == "bug":
        score += 1
    if labels & _CUSTOMER_FACING_LABELS:
        score += 1

    return _clamp(score)


def score_ease(issue: dict) -> int:
    """How easy is this to implement? Higher = easier. NO hidden inversion."""
    complexity = issue.get("complexity", "medium")
    scope = issue.get("scope", "narrow")

    ease_map = {
        ("low",    "narrow"): 9,
        ("low",    "broad"):  6,
        ("medium", "narrow"): 5,
        ("medium", "broad"):  3,
        ("high",   "narrow"): 2,
        ("high",   "broad"):  1,
    }
    score = ease_map.get((complexity, scope), 5)
    if _has_known_fix_path(issue):
        score += 1

    return _clamp(score)


def score_urgency(issue: dict) -> int:
    """Time pressure — age, SLA labels, and active discussion velocity."""
    score = 3
    age = issue.get("age_days", 0)
    comments = issue.get("comments_count", 0)
    labels = _labels_of(issue)

    if age > 90:
        score += 3
    elif age > 60:
        score += 2
    elif age > 30:
        score += 1

    # Active-discussion proxy: comments outpace age (guard against age=0).
    if age > 0 and comments > age / 10:
        score += 1
    elif age == 0 and comments >= 1:
        score += 1

    if labels & _CRITICAL_LABELS:
        score += 2

    return _clamp(score)


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


def compute_tier_score(scores: dict, weights: dict) -> float:
    """
    Weighted sum of the enriched scoring dimensions. Every dimension is
    "higher = better" (``ease`` is already inverted in scoring) so the raw
    weighted sum is directly usable — no hidden inversions.

    Weights are normalised so the result stays in roughly [0, 10] regardless
    of what values the caller provides.
    """
    total_weight = sum(weights.values()) or 1.0
    return round(
        sum(scores.get(k, 0) * (w / total_weight) for k, w in weights.items()),
        2,
    )


def compute_total_score(scores: dict, weights: dict) -> float:
    """
    DEPRECATED — retained for backward compatibility with stored records that
    still use the old four-dimension shape (user_impact, business_impact,
    effort, confidence). New code should use :func:`compute_tier_score`.
    """
    total_weight = sum(weights.values()) or 1.0
    w = {k: v / total_weight for k, v in weights.items()}
    return round(
        scores.get("user_impact", 0)     * w.get("user_impact", 0) +
        scores.get("business_impact", 0) * w.get("business_impact", 0) +
        (10 - scores.get("effort", 0))   * w.get("effort", 0) +
        scores.get("confidence", 0)      * w.get("confidence", 0),
        2,
    )


def _derive_total_score(tier: int, score_within_tier: float) -> float:
    """Map (tier, score_within_tier) → a legacy 0–10 total_score number.

    Used to keep ``planner_score.total_score`` populated on new records so old
    code paths that sort or threshold on ``total_score`` keep working during
    the UI transition.
    """
    return round((4 - tier) * 2.5 + score_within_tier * 0.25, 2)


# ---------------------------------------------------------------------------
# Backward-compatibility: migrate legacy 4-dim planner_score on read
# ---------------------------------------------------------------------------

def migrate_legacy_score(planner_score: dict) -> dict:
    """
    Promote an old 4-dim ``planner_score`` dict to the new 6-dim + tier shape.

    Idempotent: if the dict already has a ``tier`` field, it is returned
    untouched. Missing new fields are filled in from the nearest legacy proxy
    (e.g. ``ease = 10 - effort``).
    """
    if not isinstance(planner_score, dict) or "tier" in planner_score:
        return planner_score

    user_impact = planner_score.get("user_impact", 5)
    business_impact = planner_score.get("business_impact", 4)
    effort = planner_score.get("effort", 5)
    total_score = planner_score.get("total_score", 0.0)

    planner_score.setdefault("severity", _clamp(user_impact))
    planner_score.setdefault("reach", _clamp(user_impact))
    planner_score.setdefault("business_value", _clamp(business_impact))
    planner_score.setdefault("ease", _clamp(10 - effort))
    planner_score.setdefault("urgency", _clamp(user_impact))
    planner_score.setdefault("tier", 3)
    planner_score.setdefault("tier_reason", "Legacy score — migrated on read")
    planner_score.setdefault("score_within_tier", float(total_score))
    return planner_score


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


_TIER_LABEL = {1: "Critical", 2: "High", 3: "Normal", 4: "Deferred"}


def recommend(tier: int, scores: dict, issue: dict) -> tuple:
    """
    Decide whether to recommend this issue for autonomous resolution.
    Returns (recommended: bool, reason: str).

    Hard blocks (regardless of tier):
        - risk == "high"
        - issue_type == "investigation"

    Otherwise:
        - T1 / T2 with confidence ≥ 3 → recommended
        - T3 only when ease ≥ 5 and confidence ≥ 5 → recommended
        - T4 → never recommended
    """
    issue_type = issue.get("issue_type", "other")
    risk = issue.get("risk", "low")
    confidence = scores.get("confidence", 0)
    ease = scores.get("ease", 0)

    if risk == "high":
        return False, "High-risk area (auth/billing/architecture) — requires human oversight"

    if issue_type == "investigation":
        return False, "Root cause is unknown — needs human investigation before automation"

    tier_label = _TIER_LABEL.get(tier, f"T{tier}")

    if tier == 4:
        return False, f"{tier_label} — deprioritized under the active goal"

    if tier in (1, 2):
        if confidence < 3:
            return False, f"{tier_label} but low automation confidence ({confidence}/10)"
        return True, f"{tier_label} priority — good automation candidate (confidence {confidence}/10)"

    # Tier 3
    if ease >= 5 and confidence >= 5:
        return True, f"{tier_label} with high ease ({ease}/10) and confidence ({confidence}/10)"

    return False, f"{tier_label} — insufficient ease/confidence for automation"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _score_issue(issue: dict) -> dict:
    """Compute all 6 enriched scoring dimensions for an issue."""
    return {
        "severity":       score_severity(issue),
        "reach":          score_reach(issue),
        "business_value": score_business_value(issue),
        "ease":           score_ease(issue),
        "confidence":     score_confidence(issue),
        "urgency":        score_urgency(issue),
    }


def plan_issues(ingested: list, weights: dict = None,
                strategy: PlannerStrategy = None) -> list:
    """
    Stage 2: Planner.

    Scores, ranks, and annotates each ingested issue. Returns a list of
    PlannedIssue dicts sorted by ``(tier, -score_within_tier)`` with
    ``priority_rank`` reflecting final order (1 = highest priority).

    Two ways to steer the ranking:
        - ``strategy``: a PlannerStrategy bundling weights + a tier policy.
          Produced by priorities.get_strategy(). Preferred.
        - ``weights``: raw weight dict (legacy path). When provided without a
          strategy the balanced tier policy is used.

    When both are omitted the balanced default is applied.
    """
    if strategy is None:
        strategy = get_strategy(BALANCED_INTENT)
    active_weights = weights if weights is not None else strategy.weights

    scored = []

    for issue in ingested:
        base_scores = _score_issue(issue)

        if strategy.tier_fn is not None:
            tier, tier_reason = strategy.tier_fn(issue, base_scores)
        else:
            tier, tier_reason = 3, "No tier policy — default"

        score_within_tier = compute_tier_score(base_scores, active_weights)
        total_score = _derive_total_score(tier, score_within_tier)
        recommended, reason = recommend(tier, base_scores, issue)
        options = generate_implementation_options(issue) if recommended else []

        # New 6-dim + tier fields are the canonical shape. Legacy
        # user_impact / business_impact / effort keys are populated from the
        # nearest proxy so older UI code paths that read them during the
        # multi-PR rollout keep rendering without error.
        planner_score = {
            "severity":              base_scores["severity"],
            "reach":                 base_scores["reach"],
            "business_value":        base_scores["business_value"],
            "ease":                  base_scores["ease"],
            "confidence":            base_scores["confidence"],
            "urgency":               base_scores["urgency"],
            "tier":                  tier,
            "tier_reason":           tier_reason,
            "score_within_tier":     score_within_tier,
            "total_score":           total_score,
            "recommended":           recommended,
            "recommendation_reason": reason,
            "priority_rank":         0,  # filled in after sort
            # Legacy proxies for backward compatibility (removed in a later PR)
            "user_impact":     base_scores["severity"],
            "business_impact": base_scores["business_value"],
            "effort":          _clamp(10 - base_scores["ease"]),
        }

        scored.append({
            **issue,
            "planner_score": planner_score,
            "implementation_options": options,
            "planned_at": datetime.now().isoformat(),
        })

    # Primary sort is tier (ascending — T1 first); within a tier,
    # higher score_within_tier wins.
    _reorder_by_tier(scored)
    return scored


def _reorder_by_tier(issues: list) -> list:
    """Sort by (tier, -score_within_tier) and re-assign priority_rank in place."""
    issues.sort(key=lambda x: (
        x["planner_score"]["tier"],
        -x["planner_score"]["score_within_tier"],
    ))
    for rank, issue in enumerate(issues, start=1):
        issue["planner_score"]["priority_rank"] = rank
    return issues


def apply_refinement(issues: list, refinement_text: str) -> list:
    """Boost score_within_tier for issues matching refinement keywords.

    Matching is case-insensitive substring across title, description, and
    labels. Each matched keyword adds 0.5 to ``score_within_tier``, capped at
    +1.5. Re-sorts (tier, -score_within_tier) and re-assigns priority_rank.
    """
    if not refinement_text or not refinement_text.strip():
        return issues

    keywords = [kw for kw in refinement_text.lower().split() if kw]
    if not keywords:
        return issues

    for issue in issues:
        searchable = " ".join([
            str(issue.get("title", "")),
            str(issue.get("description", "")),
            " ".join(str(lbl) for lbl in issue.get("labels", [])),
        ]).lower()
        match_count = sum(1 for kw in keywords if kw in searchable)
        if match_count > 0:
            boost = min(1.5, match_count * 0.5)
            issue["planner_score"]["score_within_tier"] = round(
                issue["planner_score"].get("score_within_tier", 0.0) + boost, 2
            )

    _reorder_by_tier(issues)
    return issues


# ---------------------------------------------------------------------------
# Devin-powered planner (Stage 2 — premium path)
# ---------------------------------------------------------------------------

def _build_devin_planner_score(item: dict) -> dict:
    """Build a planner_score dict from a Devin JSON item.

    Devin may return the new 6-dim + tier schema or the legacy 4-dim one (or
    a mix). Map legacy keys onto the new shape and fill in sensible defaults
    for missing fields so the dict always satisfies downstream readers.
    """
    severity = item.get("severity", item.get("user_impact", 5))
    business_value = item.get("business_value", item.get("business_impact", 4))
    if "ease" in item:
        ease = item["ease"]
    elif "effort" in item:
        ease = _clamp(10 - item["effort"])
    else:
        ease = 5

    score_within_tier = item.get(
        "score_within_tier",
        item.get("total_score", 0.0),
    )

    return {
        "severity":              _clamp(severity),
        "reach":                 _clamp(item.get("reach", 5)),
        "business_value":        _clamp(business_value),
        "ease":                  _clamp(ease),
        "confidence":            _clamp(item.get("confidence", 4)),
        "urgency":               _clamp(item.get("urgency", 5)),
        "tier":                  int(item.get("tier", 3)),
        "tier_reason":           item.get("tier_reason", "Devin-scored"),
        "score_within_tier":     float(score_within_tier),
        "total_score":           float(item.get("total_score", 0.0)),
        "recommended":           bool(item.get("recommended", False)),
        "recommendation_reason": item.get("recommendation_reason", ""),
        "priority_rank":         int(item.get("priority_rank", 0)),
        # Legacy proxies so older UI paths still render during rollout.
        "user_impact":     _clamp(severity),
        "business_impact": _clamp(business_value),
        "effort":          _clamp(10 - ease),
    }


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
            "planner_score": _build_devin_planner_score(item),
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
            "planner_score": _build_devin_planner_score(item),
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



