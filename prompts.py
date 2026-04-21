"""
prompts.py — Prompt templates for Devin-powered pipeline stages

Stage 3 (Architect): ARCHITECT_PROMPT — produces a technical implementation plan
Stage 4 (Executor):  EXECUTION_PROMPT — implements the plan and opens a PR

Stages 1 and 2 (Ingest, Planner) are rule-based — no Devin prompts needed.
Stage 5 (Optimizer) reads stored data — no Devin prompts needed.

Usage:
    from prompts import ARCHITECT_PROMPT, EXECUTION_PROMPT
    prompt = ARCHITECT_PROMPT.format(issue_id=..., title=..., ...)
"""

TARGET_REPO = "sarahmoooree-builds/finserv-platform"

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

1. **Score on four dimensions (0–10 integers each):**

   - **user_impact**: How many users are affected, and how severely?
     Consider: is this blocking a workflow? Is it intermittent or constant? \
How long has it been open? How many comments suggest user frustration?
     10 = widespread, blocking issue. 1 = cosmetic, affects almost no one.

   - **business_impact**: Does this affect revenue, compliance, SLA, or customer trust?
     10 = direct revenue or compliance risk. 1 = internal tooling with no customer exposure.

   - **effort**: How hard is this to implement? (10 = hardest, 1 = trivial)
     Base this on complexity and scope. A high-effort score means more work for the \
Architect and Executor — factor this into your recommendation.

   - **confidence**: How likely is this to succeed as an autonomous AI task?
     10 = clear bug, narrow scope, obvious fix. 1 = vague, investigation-style, \
requires human judgment or access to undocumented context.

2. **Compute total_score** (float):
   `user_impact * 0.35 + business_impact * 0.25 + (10 - effort) * 0.20 + confidence * 0.20`

3. **Recommend or not:**
   `recommended = total_score >= 6.0 AND risk != "high" AND issue_type != "investigation"`

4. **Write recommendation_reason** — 1-2 sentences explaining why this issue is or is not \
recommended. Be specific: mention what makes it high/low impact or easy/hard. \
This is the sentence a PM reads to decide whether to approve.

5. **Assign priority_rank** — rank all issues 1 to N by total_score descending (1 = highest \
priority). Rank non-recommended issues after recommended ones.

6. **List implementation_options** — for recommended issues only, 1-3 plain-English \
approaches a developer might take (no code). For non-recommended, use [].

7. **Write a scope_summary** — 1 sentence describing what this work touches and what would \
need to change. Example: "Touches the email validation layer in the auth service; \
no downstream dependencies."

## Required output

Respond with ONLY a JSON array, one object per issue:

```json
[
  {{
    "id": <original issue id>,
    "user_impact": <0-10>,
    "business_impact": <0-10>,
    "effort": <0-10>,
    "confidence": <0-10>,
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
- Scores must be integers 0–10; total_score is a float rounded to 2 decimal places
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

Score each issue on four dimensions (0–10 integers each):

8. **user_impact** — How many users are affected, and how severely?
   10 = widespread, blocking issue. 1 = cosmetic, affects almost no one.
   Consider: issue age, comment count, type, priority labels.

9. **business_impact** — Does this affect revenue, compliance, SLA, or customer trust?
   10 = direct revenue or compliance risk. 1 = internal tooling, no customer exposure.

10. **effort** — How hard is this to implement? (10 = hardest, 1 = trivial)
    Base on complexity and scope. High effort = more work for the engineering pipeline.

11. **confidence** — How likely is autonomous AI resolution to succeed?
    10 = clear bug, narrow scope, obvious fix path. 1 = vague, investigation-style, \
requires human judgment.

12. **Compute total_score** (float, 2 decimal places):
    `total_score = user_impact * 0.35 + business_impact * 0.25 + (10 - effort) * 0.20 + confidence * 0.20`

13. **Recommend or not:**
    `recommended = total_score >= 6.0 AND risk != "high" AND issue_type != "investigation"`

14. **Write recommendation_reason** — 1-2 sentences explaining why recommended or not. \
Be specific about what makes it high/low impact or easy/hard. This is what a PM reads to \
decide whether to approve.

15. **Assign priority_rank** — rank all issues 1 to N by total_score descending (1 = highest). \
Rank non-recommended issues after recommended ones. No ties, no gaps.

16. **List implementation_options** — for recommended issues only, 1-3 plain-English \
approaches (no code). For non-recommended, use [].

17. **Write scope_summary** — 1 sentence describing what this work touches and what would \
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
    "user_impact": <0-10>,
    "business_impact": <0-10>,
    "effort": <0-10>,
    "confidence": <0-10>,
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
- Scores must be integers 0–10; total_score is a float rounded to 2 decimal places
"""


# ---------------------------------------------------------------------------
# Stage 3: Architect Prompt
#
# Instructs Devin to read the codebase and produce a build-ready technical plan.
# Does NOT write code. Does NOT open a PR.
#
# Format params:
#   issue_id, title, description, labels,
#   issue_type, complexity, scope, risk, summary,
#   implementation_options
# ---------------------------------------------------------------------------

ARCHITECT_PROMPT = """\
You are a senior software architect performing technical planning for an approved GitHub issue.
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
# Instructs Devin to implement the Architect's plan using the two-subagent
# pipeline (issue-explorer → issue-fixer).
#
# Format params:
#   issue_id, title, description, labels,
#   issue_type, complexity, scope, summary,
#   root_cause, affected_files, task_breakdown,
#   architect_confidence, risks
# ---------------------------------------------------------------------------

EXECUTION_PROMPT = """\
You are an autonomous software engineering agent implementing an approved fix.
Follow the Architect Plan below exactly. Do not invent strategy or prioritisation.

## Subagent Instructions

Use the two-agent pipeline defined in `.devin/agents/`:

1. **Spawn `issue-explorer` as a background subagent.** Give it the issue details and the
   Architect Plan below. Ask it to produce an investigation report confirming the root cause
   and flagging any divergence from the plan.

2. **Wait for the explorer to finish.** If the explorer's findings contradict the Architect Plan
   significantly (different root cause, additional affected files not listed), STOP and report
   the discrepancy — do not proceed with an incorrect plan.

3. **Spawn `issue-fixer` as a foreground subagent.** Pass it the explorer's report AND the
   Architect Plan. Instruct it to implement exactly the task breakdown in order, run tests,
   and open a PR.

## Issue Details

**Issue #{issue_id}: {title}**

**Description:**
{description}

**Labels:** {labels}

**Issue Type:** {issue_type} | **Complexity:** {complexity} | **Scope:** {scope}

**Summary:** {summary}

## Architect Plan

**Root Cause:**
{root_cause}

**Affected Files:**
{affected_files}

**Task Breakdown (implement in this order):**
{task_breakdown}

**Architect Confidence:** {architect_confidence}/100

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
