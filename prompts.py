"""
prompts.py — Prompt templates for Devin-powered pipeline stages

Stage 3 (Scope):     SCOPE_PROMPT — produces a technical implementation plan
Stage 4 (Executor):  EXECUTION_PROMPT — implements the plan and opens a PR
Stage 5 (Optimizer): OPTIMIZER_PROMPT — retrospective batch analysis of
                     completed execution sessions (Devin-powered path)

Stages 1 and 2 (Ingest, Planner) have both rule-based and Devin-powered paths;
Stage 5 (Optimizer) also has both paths. The rule-based optimizer uses crude
heuristics and does not need a prompt — it reads stored data only.

Usage:
    from prompts import SCOPE_PROMPT, EXECUTION_PROMPT, OPTIMIZER_PROMPT
    prompt = SCOPE_PROMPT.format(issue_id=..., title=..., ...)
"""

from config import TARGET_REPO  # noqa: F401 — re-exported for backward compatibility

PLATFORM_CONTEXT = """\
FinServ Co. is an enterprise financial services platform. Their GitHub repo contains issues \
covering payment processing, authentication, compliance reporting, data pipelines, \
customer-facing dashboards, and internal tooling. Issues come from engineers, PMs, and \
support teams and are often inconsistently labeled or described.\
"""


# ---------------------------------------------------------------------------
# Stage 1: Ingest Prompt
#
# Processes a raw batch of GitHub issues from a real enterprise repo.
# Issues may be messy, vaguely described, or inconsistently labeled.
# Devin normalises them into a consistent schema without prioritising.
#
# Format params:
#   platform_context, issues_json
# ---------------------------------------------------------------------------

INGEST_PROMPT = """\
You are a senior engineering analyst processing a raw batch of GitHub issues from an enterprise \
software platform.

{platform_context}

These issues may be poorly labeled, vaguely described, or duplicated. Your job is to read each \
one carefully and produce a clean, normalised record. Do NOT prioritise or score them — \
that happens in a separate step.

## Raw Issues

```json
{issues_json}
```

## Instructions

For each issue:

1. **Normalise the title** — fix typos, remove noise, make it scannable in one line.

2. **Write a clear summary** — 1-2 sentences explaining what is broken or needed, in plain \
English. Assume the reader has no context.

3. **Classify issue_type** — one of:
   - `bug`: something is broken or behaving incorrectly
   - `feature_request`: new capability or behaviour requested
   - `tech_debt`: cleanup, refactor, or migration with no new user-facing behaviour
   - `investigation`: root cause unclear, needs diagnosis before a fix can be written
   - `other`: does not fit the above

4. **Classify complexity** — one of `low`, `medium`, `high`:
   - low: isolated change, one file or component, clear fix path
   - medium: touches 2-3 areas, some domain context needed
   - high: cross-cutting, architectural, or requires significant design decisions

5. **Classify scope** — one of `narrow` (one module/service) or `broad` (cross-cutting, \
multiple teams or services).

6. **Assess risk** — one of `low` or `high`:
   - high if the issue touches authentication, payments, billing, compliance, or core data \
integrity
   - low otherwise

7. **Detect duplicates** — if this issue appears to describe the same problem as another \
issue in the batch, set `duplicate_of` to that issue's id. Otherwise null.

## Required output

Respond with ONLY a JSON array, one object per issue, in the same order as the input:

```json
[
  {{
    "id": <original issue id>,
    "title": "<normalised title>",
    "description": "<original description, unchanged>",
    "labels": ["<normalised label>", ...],
    "age_days": <original value>,
    "comments_count": <original value>,
    "summary": "<1-2 sentence plain-English summary>",
    "issue_type": "<bug|feature_request|tech_debt|investigation|other>",
    "complexity": "<low|medium|high>",
    "scope": "<narrow|broad>",
    "risk": "<low|high>",
    "duplicate_of": <issue id or null>
  }}
]
```

Rules:
- Output raw JSON only — no markdown fences, no commentary, no extra fields
- Preserve the original `id`, `description`, `age_days`, and `comments_count` exactly
- Every issue in the input must appear in the output
"""


# ---------------------------------------------------------------------------
# Stage 2: Planner Prompt
#
# Receives normalised IngestedIssue records and produces a ranked, scored,
# scoped PlannedIssue list with rich narrative reasoning.
# Devin reasons about business context, not just labels.
#
# Format params:
#   platform_context, issues_json
# ---------------------------------------------------------------------------

PLANNER_PROMPT = """\
You are a product and engineering planning analyst for an enterprise software platform.

{platform_context}

You have received a batch of normalised GitHub issues. Your job is to rank and prioritise \
them for autonomous resolution, with clear reasoning that a PM can read and act on.

## Normalised Issues

```json
{issues_json}
```

## Instructions

For each issue, produce the following:

1. **Score on six dimensions (0–10 integers each). Higher = more of the dimension \
— no hidden inversions.**

   - **severity**: How bad is the problem when it happens?
     10 = data loss, outage, blocking crash. 1 = cosmetic or rare nuisance.

   - **reach**: How many users or customers are affected?
     10 = all users or a widespread cohort. 1 = one internal user or a rare edge case.
     Consider: comment count, "all users"/"widespread" signals, customer-facing labels, age.

   - **business_value**: Does fixing this protect revenue, compliance, SLA, or customer trust?
     10 = direct revenue/compliance/SLA risk. 1 = internal tooling with no customer exposure.

   - **ease**: How easy is this to implement? (10 = trivial, 1 = hardest — higher is easier)
     Base on complexity and scope. Low-complexity + narrow-scope = high ease. \
High-complexity + broad-scope = low ease.

   - **confidence**: How likely is this to succeed as an autonomous AI task?
     10 = clear bug, narrow scope, obvious fix. 1 = vague, investigation-style, \
requires human judgment or undocumented context.

   - **urgency**: How much time pressure is on this?
     10 = SLA-breaching, aged 90+ days with active discussion, or explicit critical label. \
1 = fresh issue with no time pressure.

2. **Assign a tier (1–4) and write a one-sentence tier_reason:**

   - **Tier 1 (Critical)**: Severe customer-facing bugs, compliance or SLA risk, or \
data-loss scenarios. Usually high severity AND (high reach OR high urgency).
   - **Tier 2 (High)**: Important work but not an emergency — established bugs with \
moderate reach, or clearly business-relevant features.
   - **Tier 3 (Normal)**: Standard backlog items with reasonable confidence.
   - **Tier 4 (Deferred)**: Investigation-style work, low confidence, duplicates, or \
items with no business urgency.

3. **Compute score_within_tier** (float, 2 decimal places) — the weighted sum used to \
order issues inside a tier. Use the balanced weights:
   `score_within_tier = severity*0.25 + reach*0.20 + business_value*0.20 + ease*0.15 + confidence*0.10 + urgency*0.10`

4. **Compute total_score** (float, 2 decimal places) — kept for backward compatibility:
   `total_score = (4 - tier) * 2.5 + score_within_tier * 0.25`

5. **Recommend or not:**
   - Tier 1 or 2 with confidence ≥ 3 AND risk != "high" AND issue_type != "investigation" → recommended
   - Tier 3 only if ease ≥ 5 AND confidence ≥ 5 → recommended
   - Tier 4 → never recommended
   - risk == "high" OR issue_type == "investigation" → never recommended, regardless of tier

6. **Write recommendation_reason** — 1-2 sentences explaining why this issue is or is not \
recommended. Be specific about what makes it high/low impact, easy/hard, or uncertain. \
This is the sentence a PM reads to decide whether to approve.

7. **Assign priority_rank** — rank all issues 1 to N by (tier ascending, then \
score_within_tier descending). Rank 1 = Tier 1 with highest score_within_tier. \
Non-recommended issues should still be ranked — the rank reflects tier order, not \
recommendation status. No ties, no gaps.

8. **List implementation_options** — for recommended issues only, 1-3 plain-English \
approaches a developer might take (no code). For non-recommended, use [].

9. **Write a scope_summary** — 1 sentence describing what this work touches and what would \
need to change. Example: "Touches the email validation layer in the auth service; \
no downstream dependencies."

## Required output

Respond with ONLY a JSON array, one object per issue:

```json
[
  {{
    "id": <original issue id>,
    "severity": <0-10>,
    "reach": <0-10>,
    "business_value": <0-10>,
    "ease": <0-10>,
    "confidence": <0-10>,
    "urgency": <0-10>,
    "tier": <1-4>,
    "tier_reason": "<1 sentence>",
    "score_within_tier": <float>,
    "total_score": <float>,
    "recommended": <bool>,
    "recommendation_reason": "<1-2 sentences>",
    "priority_rank": <int>,
    "implementation_options": ["<option>", ...],
    "scope_summary": "<1 sentence>"
  }}
]
```

Rules:
- Output raw JSON only — no markdown fences, no commentary
- Every issue in the input must appear in the output
- Rank all N issues from 1 to N — no ties, no gaps
- All six dimension scores must be integers 0–10 (no inversions)
- `score_within_tier` and `total_score` are floats rounded to 2 decimal places
- `tier` is an integer 1–4
"""


# ---------------------------------------------------------------------------
# Stage 1+2 Combined: Analysis Prompt (Devin-powered)
#
# Replaces the separate INGEST_PROMPT + PLANNER_PROMPT Devin calls with a
# single session that normalises AND scores/ranks in one analytical pass.
#
# Format params:
#   platform_context, issues_json
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT = """\
You are a senior engineering analyst and product manager processing a raw batch of GitHub \
issues from an enterprise software platform.

{platform_context}

These issues may be poorly labeled, vaguely described, or duplicated. In a single pass, \
you will normalise each issue AND produce a full priority assessment — so that the output \
is immediately ready for an autonomous engineering pipeline to act on.

## Raw Issues

```json
{issues_json}
```

## Instructions

For each issue, produce the following in order:

### Part 1 — Normalise and Classify

1. **Normalise the title** — fix typos, remove noise, make it scannable in one line.

2. **Write a clear summary** — 1-2 sentences explaining what is broken or needed, in plain \
English. Assume the reader has no context.

3. **Classify issue_type** — one of:
   - `bug`: something is broken or behaving incorrectly
   - `feature_request`: new capability or behaviour requested
   - `tech_debt`: cleanup, refactor, or migration with no new user-facing behaviour
   - `investigation`: root cause unclear, needs diagnosis before a fix can be written
   - `other`: does not fit the above

4. **Classify complexity** — one of `low`, `medium`, `high`:
   - low: isolated change, one file or component, clear fix path
   - medium: touches 2-3 areas, some domain context needed
   - high: cross-cutting, architectural, or requires significant design decisions

5. **Classify scope** — one of `narrow` (one module/service) or `broad` (cross-cutting, \
multiple teams or services).

6. **Assess risk** — one of `low` or `high`:
   - high if the issue touches authentication, payments, billing, compliance, or core data integrity
   - low otherwise

7. **Detect duplicates** — if this issue appears to describe the same problem as another \
issue in the batch, set `duplicate_of` to that issue's id. Otherwise null.

### Part 2 — Score and Prioritise

Score each issue on six dimensions (0–10 integers each). All dimensions are \
"higher = more of the dimension" — no hidden inversions.

8. **severity** — How bad is the problem when it happens?
   10 = data loss, outage, blocking crash. 1 = cosmetic or rare nuisance.

9. **reach** — How many users or customers are affected?
   10 = all users or a widespread cohort. 1 = one internal user or a rare edge case.
   Consider: comment count, "all users"/"widespread" signals, customer-facing labels, age.

10. **business_value** — Does fixing this protect revenue, compliance, SLA, or customer trust?
    10 = direct revenue/compliance/SLA risk. 1 = internal tooling, no customer exposure.

11. **ease** — How easy is this to implement? (10 = trivial, 1 = hardest — higher is easier)
    Base on complexity and scope. Low-complexity + narrow-scope = high ease. \
High-complexity + broad-scope = low ease.

12. **confidence** — How likely is autonomous AI resolution to succeed?
    10 = clear bug, narrow scope, obvious fix path. 1 = vague, investigation-style, \
requires human judgment.

13. **urgency** — How much time pressure is on this?
    10 = SLA-breaching, aged 90+ days with active discussion, or explicit critical label. \
1 = fresh issue with no time pressure.

14. **Assign a tier (1–4) and write a one-sentence tier_reason:**

    - **Tier 1 (Critical)**: Severe customer-facing bugs, compliance or SLA risk, or \
data-loss scenarios. Usually high severity AND (high reach OR high urgency).
    - **Tier 2 (High)**: Important work but not an emergency — established bugs with \
moderate reach, or clearly business-relevant features.
    - **Tier 3 (Normal)**: Standard backlog items with reasonable confidence.
    - **Tier 4 (Deferred)**: Investigation-style work, low confidence, duplicates, or \
items with no business urgency.

15. **Compute score_within_tier** (float, 2 decimal places) — the weighted sum used to \
order issues inside a tier. Use the balanced weights:
    `score_within_tier = severity*0.25 + reach*0.20 + business_value*0.20 + ease*0.15 + confidence*0.10 + urgency*0.10`

16. **Compute total_score** (float, 2 decimal places) — kept for backward compatibility:
    `total_score = (4 - tier) * 2.5 + score_within_tier * 0.25`

17. **Recommend or not:**
    - Tier 1 or 2 with confidence ≥ 3 AND risk != "high" AND issue_type != "investigation" → recommended
    - Tier 3 only if ease ≥ 5 AND confidence ≥ 5 → recommended
    - Tier 4 → never recommended
    - risk == "high" OR issue_type == "investigation" → never recommended, regardless of tier

18. **Write recommendation_reason** — 1-2 sentences explaining why recommended or not. \
Be specific about what makes it high/low impact, easy/hard, or uncertain. This is what a \
PM reads to decide whether to approve.

19. **Assign priority_rank** — rank all issues 1 to N by (tier ascending, then \
score_within_tier descending). Rank 1 = Tier 1 with highest score_within_tier. \
Non-recommended issues should still be ranked — rank reflects tier order, not \
recommendation status. No ties, no gaps.

20. **List implementation_options** — for recommended issues only, 1-3 plain-English \
approaches (no code). For non-recommended, use [].

21. **Write scope_summary** — 1 sentence describing what this work touches and what would \
need to change.

## Required output

Respond with ONLY a JSON array, one object per issue, in priority_rank order (rank 1 first):

```json
[
  {{
    "id": <original issue id>,
    "title": "<normalised title>",
    "description": "<original description, unchanged>",
    "labels": ["<normalised label>", ...],
    "age_days": <original value>,
    "comments_count": <original value>,
    "summary": "<1-2 sentence plain-English summary>",
    "issue_type": "<bug|feature_request|tech_debt|investigation|other>",
    "complexity": "<low|medium|high>",
    "scope": "<narrow|broad>",
    "risk": "<low|high>",
    "duplicate_of": <issue id or null>,
    "severity": <0-10>,
    "reach": <0-10>,
    "business_value": <0-10>,
    "ease": <0-10>,
    "confidence": <0-10>,
    "urgency": <0-10>,
    "tier": <1-4>,
    "tier_reason": "<1 sentence>",
    "score_within_tier": <float>,
    "total_score": <float>,
    "recommended": <bool>,
    "recommendation_reason": "<1-2 sentences>",
    "priority_rank": <int>,
    "implementation_options": ["<option>", ...],
    "scope_summary": "<1 sentence>"
  }}
]
```

Rules:
- Output raw JSON only — no markdown fences, no commentary, no extra fields
- Preserve the original `id`, `description`, `age_days`, and `comments_count` exactly
- Every issue in the input must appear in the output
- Rank all N issues from 1 to N — no ties, no gaps
- All six dimension scores must be integers 0–10 (no inversions)
- `score_within_tier` and `total_score` are floats rounded to 2 decimal places
- `tier` is an integer 1–4
"""


# ---------------------------------------------------------------------------
# Stage 3: Scope Prompt
#
# Instructs Devin to read the codebase and produce a build-ready technical plan.
# Does NOT write code. Does NOT open a PR.
#
# Format params:
#   issue_id, title, description, labels,
#   issue_type, complexity, scope, risk, summary,
#   implementation_options
# ---------------------------------------------------------------------------

SCOPE_PROMPT = """\
You are a senior software architect producing a technical scope plan for an approved GitHub issue.
This issue has been reviewed and approved for autonomous implementation.
Your job is to read the codebase and produce a precise, build-ready technical plan.
You do NOT write code or open pull requests.

## Approved Issue

**Issue #{issue_id}: {title}**

**Description:**
{description}

**Labels:** {labels}

## Planner Assessment

- Issue type: {issue_type}
- Complexity: {complexity}
- Scope: {scope}
- Risk: {risk}
- Summary: {summary}
- Implementation options considered:
{implementation_options}

## Subagent Instructions

Use the `issue-triager` subagent (defined in .devin/agents/issue-triager/AGENT.md) to analyse \
this issue. The triager will read the codebase and return a JSON report.

After the triager finishes, augment its output with the additional fields listed below.

## Required Output

Respond with ONLY a valid JSON object containing exactly these fields:

{{
  "confidence_score": <integer 0-100>,
  "confidence_reasoning": "<1-2 sentences explaining the score>",
  "root_cause_hypothesis": "<specific file, function, and line if possible>",
  "affected_files": ["<real file path>", ...],
  "estimated_lines_changed": <integer>,
  "task_breakdown": [
    "<ordered implementation task 1>",
    "<ordered implementation task 2>"
  ],
  "dependencies": ["<other issue or PR this depends on>"],
  "risks": ["<edge case or blast-radius concern>"]
}}

Rules:
- `task_breakdown` must be ordered, actionable steps — not questions or vague directions
- `affected_files` must be real paths confirmed in the repository
- `dependencies` is [] if none exist
- `risks` should note auth/billing blast radius, test-coverage gaps, and race conditions
- If confidence_score < 25, explain what information is missing in `root_cause_hypothesis`
  and list clarification steps (not implementation steps) in `task_breakdown`
- Output raw JSON only — no markdown fences, no commentary
"""


# ---------------------------------------------------------------------------
# Stage 4: Executor Prompt
#
# Instructs Devin to implement the Scope plan using the two-subagent
# pipeline (issue-explorer → issue-fixer).
#
# Format params:
#   issue_id, title, description, labels,
#   issue_type, complexity, scope, summary,
#   root_cause, affected_files, task_breakdown,
#   scope_confidence, risks
# ---------------------------------------------------------------------------

EXECUTION_PROMPT = """\
You are an autonomous software engineering agent implementing an approved fix.
Follow the Scope Plan below exactly. Do not invent strategy or prioritisation.

## Subagent Instructions

Use the two-agent pipeline defined in `.devin/agents/`:

1. **Spawn `issue-explorer` as a background subagent.** Give it the issue details and the
   Scope Plan below. Ask it to produce an investigation report confirming the root cause
   and flagging any divergence from the plan.

2. **Wait for the explorer to finish.** If the explorer's findings contradict the Scope Plan
   significantly (different root cause, additional affected files not listed), STOP and report
   the discrepancy — do not proceed with an incorrect plan.

3. **Spawn `issue-fixer` as a foreground subagent.** Pass it the explorer's report AND the
   Scope Plan. Instruct it to implement exactly the task breakdown in order, run tests,
   and open a PR.

## Issue Details

**Issue #{issue_id}: {title}**

**Description:**
{description}

**Labels:** {labels}

**Issue Type:** {issue_type} | **Complexity:** {complexity} | **Scope:** {scope}

**Summary:** {summary}

## Scope Plan

**Root Cause:**
{root_cause}

**Affected Files:**
{affected_files}

**Task Breakdown (implement in this order):**
{task_breakdown}

**Scope Confidence:** {scope_confidence}/100

**Risks to Watch:**
{risks}

## Execution Rules

1. Follow the task breakdown in order. Do not skip steps.
2. Make the minimum change necessary. Do not refactor unrelated code.
3. Run the full test suite before opening a PR.
4. If a task in the breakdown is blocked or more complex than expected, STOP and report
   what you found — do not guess or expand scope.
5. Reference the issue number in the PR title: "Fix {title} (#{issue_id})"
6. Do NOT modify auth, billing, or payment files unless they are explicitly listed
   in the affected files above.

## Output

After the fixer completes, output a structured JSON summary:
{{
  "files_modified": ["<path>", ...],
  "changes_description": "<2-3 sentences>",
  "test_results": "pass" | "fail" | "partial",
  "pr_url": "<url or null>",
  "concerns": ["<any follow-up items>"]
}}
"""


# ---------------------------------------------------------------------------
# Stage 5: Optimizer Prompt
#
# Instructs Devin to perform a retrospective batch analysis across all
# completed execution sessions. Unlike the rule-based optimizer, this path
# fetches real PR diff stats and reads Devin session logs to identify root
# causes, producing a richer OptimizationRecord per issue.
#
# Format params:
#   executions_json, scope_plans_json, planned_issues_json
# ---------------------------------------------------------------------------

OPTIMIZER_PROMPT = """\
You are a senior engineering retrospective analyst reviewing a batch of completed \
autonomous-resolution sessions for an enterprise financial services platform.

Work on the GitHub repository https://github.com/sarahmoooree-builds/finserv-platform.

## Subagent Instructions

Use the `issue-optimizer` subagent (defined in `.devin/agents/issue-optimizer/AGENT.md`) to \
drive the retrospective analysis. The subagent knows the pipeline's pattern vocabulary and \
the schema it must return. Feed it the JSON payload below, and augment its output where \
noted in the "Required Output" section.

## Pipeline Data

You are given three parallel JSON collections keyed by issue id. Each element in \
`executions` is a terminal `ExecutionSession` — i.e. `status` is one of `Completed`, \
`Blocked`, or `Awaiting Review`. Every execution has a matching scope plan and planned \
issue (when available). Only analyse issues that appear in `executions`.

### Execution sessions (terminal state only)

```json
{executions_json}
```

### Corresponding scope plans

```json
{scope_plans_json}
```

### Corresponding planned issues (with planner scores)

```json
{planned_issues_json}
```

## Your Task

For each execution in the batch:

1. **Fetch real PR diff stats.** For every pull request listed in `pull_requests`, read the \
actual diff from the `sarahmoooree-builds/finserv-platform` repo. Record the total files \
changed and total lines added + removed. If a session has no PRs (e.g. Blocked), leave the \
real-diff fields null.

2. **Compare actual vs. estimated effort.** Compute `lines_delta` as `actual_lines_changed \
- estimated_lines_changed` and `files_delta` as `len(actual_files_changed) - \
len(scope_plan.affected_files)`. Fall back to the proxy rules in `optimizer.py` \
`_estimate_lines_delta` / `_estimate_files_delta` only if real diff data is unavailable.

3. **Classify estimation accuracy** using the real deltas (not proxies):
   - `"under"` — the session used meaningfully more code than planned \
(lines_delta > 20 **or** files_delta > 2), **or** status is `Blocked`.
   - `"over"` — the session used meaningfully less (lines_delta < -20 **or** files_delta < -2).
   - `"accurate"` — otherwise.

4. **For Blocked sessions**, read the Devin session logs / final messages (the \
`session_url` field links to the session) and summarise the root cause in \
`failure_root_cause`: environment problem, missing information, plan contradiction, test \
failure, scope mismatch, or other. Keep it under 240 characters. For non-Blocked sessions \
this field must be null.

5. **Detect pattern tags** from this fixed vocabulary (use only these exact strings):
   - `fast-completion` — Completed with exactly 1 PR and `lines_delta` within ±20.
   - `confidence-mismatch` — `scope_plan.confidence_score >= 75` but session is Blocked.
   - `underestimated-scope` — `files_delta > 2` or `lines_delta > 50`.
   - `low-effort-win` — planner ease score ≥ 7 (i.e. low effort) and session is Completed.
   - `investigation-leak` — `planned_issue.issue_type == "investigation"` that reached the \
executor.
   - `auth-false-positive` — `planned_issue.risk == "high"` that nevertheless Completed.

6. **Across the batch**, look for systemic patterns that should feed back into earlier \
pipeline stages (e.g. several `underestimated-scope` tags → raise complexity thresholds in \
`ingest.py`; several `investigation-leak` tags → tighten the `recommend()` threshold in \
`planner.py`). Emit one actionable recommendation per root cause, each referencing a \
concrete constant, threshold, or function in the pipeline code (for example `DEFAULT_WEIGHTS` \
in `planner.py`, `SCOPE_TIMEOUT` in `scope.py`, complexity thresholds in `ingest.py`).

## Required Output

Respond with ONLY a JSON **array**, one object per execution in the input (same order is \
fine). Do NOT wrap the array in an outer object, do NOT emit a single combined record, do \
NOT add markdown fences or commentary.

```json
[
  {{
    "issue_id": <int>,
    "actual_status": "<Completed|Blocked|Awaiting Review>",
    "actual_pr_count": <int>,
    "actual_lines_changed": <int or null>,
    "actual_files_changed": ["<real file path>", ...],
    "lines_delta": <int>,
    "files_delta": <int>,
    "estimation_accuracy": "<over|under|accurate>",
    "scope_confidence": <int 0-100>,
    "planned_score": {{ "severity": <int>, "reach": <int>, "business_value": <int>, \
"ease": <int>, "confidence": <int>, "urgency": <int>, "tier": <int>, \
"tier_reason": "<string>", "score_within_tier": <float>, "total_score": <float>, \
"recommended": <bool>, "recommendation_reason": "<string>", "priority_rank": <int> }},
    "pattern_tags": ["<tag from fixed vocabulary>", ...],
    "failure_root_cause": "<=240 chars, or null for non-Blocked",
    "optimizer_notes": "<1-3 sentences of retrospective commentary — cite concrete files, \
functions, or constants when relevant>",
    "recommendations": [
      "<actionable recommendation referencing a specific pipeline constant or threshold>",
      ...
    ]
  }}
]
```

### Critical rules

- Output raw JSON only — no markdown fences, no commentary, no extra fields.
- Return a JSON **array** — one object per issue. Every issue id that appears in `executions` \
must appear in the output **exactly once**.
- Only use pattern tags from the fixed vocabulary above. If none apply, use `[]`.
- `actual_files_changed` and `actual_lines_changed` must reflect the real PR diff, not the \
Scope estimate. Leave them `null` (or `[]` for the file list) if no PR is accessible.
- `failure_root_cause` is `null` for any issue whose session status is not `Blocked`.
- `estimation_accuracy` values must be lower-case `"over"`, `"under"`, or `"accurate"`.
- `recommendations` must reference concrete Python constants or thresholds (for example \
`DEFAULT_WEIGHTS` in `planner.py`, `recommend()` threshold in `planner.py`, complexity \
thresholds in `ingest.py`, `SCOPE_TIMEOUT` in `scope.py`). Generic advice without a concrete \
lever is not acceptable.
- Do not invent PRs, diffs, files, or session ids. If you cannot retrieve real data for a \
field, set it to `null` (or `[]` for the file list).
"""


# ---------------------------------------------------------------------------
# DEPRECATED — Stage 1 (Ingest) is now rule-based; this prompt is no longer used.
# Kept for reference in case an LLM-powered Ingest stage is added later.
# ---------------------------------------------------------------------------

ENRICHMENT_PROMPT = """\
# DEPRECATED — not wired to any API. Ingest is rule-based (see ingest.py).

You are an expert software engineering analyst working on an enterprise monorepo.

Your task is to analyse the following GitHub issue and produce a structured assessment.

## Issue Details
- **Title:** {title}
- **Description:** {description}
- **Labels:** {labels}
- **Age (days):** {age_days}
- **Comments:** {comments_count}

Analyse this issue and return a JSON object with these fields:
summary, issue_type, complexity, scope, risk, candidate, candidate_reason, suggested_approach.

Respond ONLY with the JSON object.
"""
