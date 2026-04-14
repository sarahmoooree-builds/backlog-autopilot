"""
scorer.py — Issue enrichment and candidate scoring

Takes raw GitHub issues and produces enriched versions with:
- A plain-language summary
- Classification (type, complexity, scope, risk)
- A recommendation on whether this issue is a good candidate for autonomous resolution

The scoring rules are intentionally simple and rule-based so you can
read, understand, and tweak them without any ML background.
"""


# ---------------------------------------------------------------------------
# Keywords used for classification
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
# Classification helpers
# ---------------------------------------------------------------------------

def classify_issue_type(issue):
    """Determine if the issue is a bug, feature request, tech debt, or investigation."""
    title_lower = issue["title"].lower()
    desc_lower = issue["description"].lower()
    labels_lower = [l.lower() for l in issue["labels"]]
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


def classify_complexity(issue):
    """Rate complexity as low, medium, or high based on description signals."""
    combined = (issue["title"] + " " + issue["description"]).lower()

    # High complexity: architecture changes, multi-service work, large scope
    if any(signal in combined for signal in HIGH_COMPLEXITY_SIGNALS):
        return "high"

    # Low complexity: short description + few comments + clear language
    desc_length = len(issue["description"])
    if desc_length < 250 and issue["comments_count"] <= 3:
        return "low"

    return "medium"


def classify_scope(issue):
    """Determine if the issue is narrow (one file/component) or broad (cross-cutting)."""
    combined = (issue["title"] + " " + issue["description"]).lower()

    if any(signal in combined for signal in BROAD_SCOPE_SIGNALS):
        return "broad"
    return "narrow"


def assess_risk(issue, issue_type, complexity, scope):
    """Assess risk level based on type, complexity, scope, and labels."""
    labels_lower = [l.lower() for l in issue["labels"]]

    # High risk: touches auth, billing, or architecture — or is high complexity + broad
    if any(sensitive in labels_lower for sensitive in ["auth", "billing", "architecture"]):
        return "high"
    if complexity == "high" and scope == "broad":
        return "high"

    return "low"


def generate_summary(issue, issue_type, complexity):
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
# Candidate recommendation logic
# ---------------------------------------------------------------------------

def evaluate_candidate(issue_type, complexity, scope, risk):
    """
    Decide if this issue is a good candidate for autonomous resolution.

    Good candidates:
      - Clear bugs with narrow scope and low/medium complexity
      - Low-risk tech debt with narrow scope

    Bad candidates:
      - Vague feature requests or investigations
      - Anything with broad scope or high complexity
      - High-risk issues (auth, billing, architecture)

    Returns (is_candidate: bool, reason: str)
    """
    # --- Disqualifiers ---
    if risk == "high":
        return False, "High-risk area — requires human oversight"

    if issue_type == "investigation":
        return False, "Root cause is unknown — needs human investigation first"

    if issue_type == "feature_request" and (scope == "broad" or complexity == "high"):
        return False, "Broad feature request — requires product and design input"

    if complexity == "high":
        return False, "High complexity — too many moving parts for autonomous resolution"

    if scope == "broad":
        return False, "Broad scope — changes span multiple modules or systems"

    # --- Qualifiers ---
    if issue_type == "bug" and complexity == "low" and scope == "narrow":
        return True, "Clear bug with narrow scope and low complexity — ideal for automation"

    if issue_type == "bug" and complexity == "medium" and scope == "narrow":
        return True, "Well-defined bug with medium complexity — good automation candidate"

    if issue_type == "tech_debt" and complexity in ("low", "medium") and scope == "narrow":
        return True, "Contained tech debt cleanup — safe for autonomous resolution"

    # Default: not confident enough to recommend
    return False, "Does not clearly fit automation criteria — keep human-owned"


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------

def enrich_issues(issues):
    """
    Take a list of raw issues and return enriched versions.

    Each enriched issue gets all original fields plus:
      - summary, issue_type, complexity, scope, risk
      - candidate (bool), candidate_reason (str)
    """
    enriched = []

    for issue in issues:
        issue_type = classify_issue_type(issue)
        complexity = classify_complexity(issue)
        scope = classify_scope(issue)
        risk = assess_risk(issue, issue_type, complexity, scope)
        candidate, candidate_reason = evaluate_candidate(issue_type, complexity, scope, risk)
        summary = generate_summary(issue, issue_type, complexity)

        enriched_issue = {
            **issue,  # keep all original fields
            "summary": summary,
            "issue_type": issue_type,
            "complexity": complexity,
            "scope": scope,
            "risk": risk,
            "candidate": candidate,
            "candidate_reason": candidate_reason,
        }
        enriched.append(enriched_issue)

    return enriched
