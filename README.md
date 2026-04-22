# Backlog Autopilot

**Demo walkthrough:** [Loom video](https://www.loom.com/share/6a8c27542a3d4ba999d33ca9ca8bbdf5)

A 5-stage multi-agent pipeline that turns a wall of stale GitHub issues into an autonomous
resolution system — with explicit human approval checkpoints and a feedback loop that learns
from outcomes.

Built for FinServ Co. as a take-home project for Cognition AI (makers of Devin).

---

## The Problem

FinServ Co. has 312 open GitHub issues. Engineers spend their best hours triaging tickets,
context-switching between planning and coding, and manually deciding what to fix next.
The backlog grows faster than it shrinks. Junior engineers burn time on work that could be
automated. Senior engineers get pulled into decisions that a well-structured system could make.

---

## The Solution: A 5-Stage Multi-Agent Pipeline

```
GitHub Issues
     │
     ▼
┌─────────────────┐
│  Stage 1        │  ingest.py
│  INGEST         │  Normalise, deduplicate, classify
└────────┬────────┘
         │ IngestedIssue
         ▼
┌─────────────────┐
│  Stage 2        │  planner.py
│  PLANNER        │  Score, rank, prioritise (rule-based, PM-configurable weights)
└────────┬────────┘
         │ PlannedIssue
         ▼
  ╔═══════════════╗
  ║ Checkpoint    ║  UI checkboxes — Select issues + "Scope Selected Issues"
  ║ 2.5 HUMAN     ║  Explicit approval — nothing is scoped or run without this
  ║ APPROVAL      ║
  ╚═══════╤═══════╝
         │ ApprovalRecord
         ▼
┌─────────────────┐
│  Stage 3        │  scope.py → Devin (issue-triager subagent)
│  SCOPE          │  Read codebase, produce build-ready technical plan
└────────┬────────┘
         │ ScopePlan
         ▼
  ╔═══════════════╗
  ║ Checkpoint    ║  UI review form (triggered when confidence < 75)
  ║ 3.5 HUMAN     ║  Optional gate for risky or low-confidence plans
  ║ REVIEW        ║
  ╚═══════╤═══════╝
         │ ReviewRecord
         ▼
┌─────────────────┐
│  Stage 4        │  executor.py → Devin (issue-explorer + issue-fixer subagents)
│  EXECUTOR       │  Follow the plan, implement fix, run tests, open PR
└────────┬────────┘
         │ ExecutionSession
         ▼
┌─────────────────┐
│  Stage 5        │  optimizer.py (rule-based) / optimizer.py → Devin (issue-optimizer)
│  OPTIMIZER      │  Compare estimated vs. actual, surface patterns, recommend adjustments
└─────────────────┘
         │ OptimizationRecord
         ▼
  Heuristic recommendations fed back into Planner weights
```

---

## Stage Reference

### Stage 1: Ingest (`ingest.py`)

**What it does:**
- Normalises raw GitHub issues (trims whitespace, normalises label casing)
- Detects suspected duplicates via Jaccard title similarity
- Classifies each issue: type, complexity, scope, risk
- Generates a plain-language summary

**What it does NOT do:** prioritise, score, or recommend anything

**Output schema:** `IngestedIssue` — id, title, description, labels, age_days, comments_count,
summary, issue_type, complexity, scope, risk, duplicate_of, ingested_at

---

### Stage 2: Planner (`planner.py`)

**What it does:**
- Scores each issue on four PM-configurable dimensions (0–10 each):
  - **User impact**: affected users, severity, age, comment volume
  - **Business impact**: revenue/compliance/customer-facing labels
  - **Effort** (inverted): complexity × scope → harder = lower score contribution
  - **Automation confidence**: issue type × complexity → likelihood of autonomous success
- Computes a weighted total score and assigns a priority rank
- Recommends issues with `total_score ≥ 6.0` and `risk != "high"` and `type != "investigation"`
- Generates 1–3 plain-English implementation options for recommended issues

**What it does NOT do:** call Devin, write code, open sessions

**Configurable:** weights are exposed as sidebar sliders in the UI

**Output schema:** `PlannedIssue` — all IngestedIssue fields + planner_score (PlannerScore),
implementation_options, planned_at

---

### Checkpoint 2.5: Human Approval

An explicit checkbox-per-issue approval step in the UI. Nothing moves to the Scope stage
without a human selecting issues and clicking "Scope selected issues".

**Output schema:** `ApprovalRecord` — issue_id, approved, approved_at

---

### Stage 3: Scope (`scope.py`)

**What it does:**
- Creates a Devin session (using the `issue-triager` subagent) that reads the codebase
- Devin produces a build-ready technical plan:
  - Confidence score (0–100) with reasoning
  - Root cause hypothesis (specific file/function/line)
  - Affected files (confirmed in the repo)
  - Estimated lines changed
  - Ordered task breakdown (the Executor follows this exactly)
  - Dependencies (other issues/PRs this work depends on)
  - Risks (blast radius, edge cases, test gaps)
- Saves a `pending` record immediately so the UI shows progress during polling
- Polls for up to 10 minutes (600s), then saves an error record if timeout

**What it does NOT do:** write code, open PRs, invent strategy

**Output schema:** `ScopePlan` — issue_id, confidence_score, confidence_reasoning,
root_cause_hypothesis, affected_files, estimated_lines_changed, task_breakdown,
dependencies, risks, session_id, session_url, scope_status, error, scoped_at

---

### Checkpoint 3.5: Human Review

Triggered automatically for issues where `scope_plan.confidence_score < 75`.
The PM sees a review form inside the issue expander with Approve / Proceed Anyway options.
High-confidence issues (≥ 75) skip this checkpoint automatically.

**Output schema:** `ReviewRecord` — issue_id, review_required, review_approved,
review_notes, reviewed_at

---

### Stage 4: Executor (`executor.py`)

**What it does:**
- Requires a complete `ScopePlan` before dispatching — will not guess if the plan is missing
- Creates a Devin session using the two-subagent pipeline:
  1. `issue-explorer`: reads the codebase, confirms root cause, flags divergence from the plan
  2. `issue-fixer`: implements the task breakdown, runs tests, opens a PR
- If the explorer contradicts the Scope plan significantly, Devin stops and reports —
  it does not proceed with an incorrect plan
- For lower-confidence tasks that get blocked: Devin reports the blocker rather than guessing
- Copies Scope estimates (lines, files) into the `ExecutionSession` for Optimizer comparison

**What it does NOT do:** invent strategy, modify issues the Scope stage has not planned

**Output schema:** `ExecutionSession` — issue_id, session_id, session_url, status,
outcome_summary, pull_requests, dispatched_at, completed_at, estimated_lines_changed,
estimated_files

---

### Stage 5: Optimizer (`optimizer.py`)

The Optimizer offers two paths, chosen from the **Optimizer mode** toggle in the UI:

**Rule-based (fast):**
- Reads all terminal `ExecutionSession` records and compares them to their `ScopePlan`
- Estimates accuracy (over/under/accurate) using a proxy model (Blocked = underestimate,
  extra PRs = scope crept)
- Runs locally in seconds; no external API calls

**Devin-powered (thorough):**
- Dispatches a single Devin session using the `issue-optimizer` subagent
- Fetches real PR diff stats (lines added/removed, files changed) from the
  finserv-platform repo for every completed execution
- Reads blocked-session logs to infer `failure_root_cause`
- Produces enriched records with real (not proxy) `lines_delta` / `files_delta`

Both paths:
- Tag recurring patterns: `fast-completion`, `confidence-mismatch`, `underestimated-scope`,
  `low-effort-win`, `investigation-leak`, `auth-false-positive`
- Produce heuristic recommendations for adjusting Planner weights or Ingest thresholds
- Write to the same `optimizations` store section (records carry `optimizer_mode: "rule" | "devin"`)

**What it does NOT do (rule-based path):** call Devin, read the codebase, require any external API

**Output schema:** `OptimizationRecord` — issue_id, planned_score, scope_confidence,
actual_status, actual_pr_count, estimation_accuracy, lines_delta, files_delta,
pattern_tags, optimizer_notes, optimizer_mode, analyzed_at — plus (Devin-powered only)
session_id, session_url, actual_lines_changed, actual_files_changed, failure_root_cause

---

## Data Schemas

| Stage | Schema | Key Fields |
|-------|--------|------------|
| Raw input | `RawIssue` | id, title, description, labels, age_days, comments_count |
| Ingest output | `IngestedIssue` | + summary, issue_type, complexity, scope, risk, duplicate_of |
| Planner output | `PlannedIssue` | + planner_score (PlannerScore), implementation_options |
| Checkpoint 2.5 | `ApprovalRecord` | issue_id, approved, approved_at |
| Scope output | `ScopePlan` | confidence_score, root_cause_hypothesis, task_breakdown, risks |
| Checkpoint 3.5 | `ReviewRecord` | review_required, review_approved, review_notes |
| Executor output | `ExecutionSession` | status, pull_requests, estimated_lines_changed |
| Optimizer output | `OptimizationRecord` | estimation_accuracy, pattern_tags, optimizer_notes |

All schemas are defined in `schemas.py`.

---

## Devin Subagents

| Stage | Python Module | Devin Subagent |
|-------|---------------|----------------|
| Ingest | `ingest.py` | None (rule-based) |
| Planner | `planner.py` | None (rule-based) |
| Scope | `scope.py` | `issue-triager` (reads codebase, returns JSON plan) |
| Executor | `executor.py` | `issue-explorer` (confirms root cause) + `issue-fixer` (implements) |
| Optimizer | `optimizer.py` | `issue-optimizer` (Devin-powered mode only; rule-based mode uses no subagent) |

Subagent definitions are in `finserv-platform/.devin/agents/`:
- `issue-triager/AGENT.md` — read-only analysis, produces confidence JSON
- `issue-explorer/AGENT.md` — confirms root cause, flags plan divergence
- `issue-fixer/AGENT.md` — minimum fix, tests, PR
- `issue-planner/AGENT.md` — scores/ranks a batch of ingested issues (future use)
- `issue-architect/AGENT.md` — produces build-ready plan for one issue (future use)
- `issue-optimizer/AGENT.md` — retrospective pattern analysis (reads real PR diffs + blocked-session logs)

---

## Persistence: pipeline_store.json

All pipeline state is stored in a single JSON file with 7 sections:

```json
{
  "ingested":        { "<issue_id>": "IngestedIssue" },
  "planned":         { "<issue_id>": "PlannedIssue" },
  "approvals":       { "<issue_id>": "ApprovalRecord" },
  "architect_plans": { "<issue_id>": "ScopePlan" },
  "reviews":         { "<issue_id>": "ReviewRecord" },
  "executions":      { "<issue_id>": "ExecutionSession" },
  "optimizations":   { "<issue_id>": "OptimizationRecord" }
}
```

---

## Setup

```bash
pip install -r requirements.txt
```

### Environment variables (`.env`)

```
DEVIN_API_KEY=your_devin_api_key
DEVIN_ORG_ID=your_devin_org_id
```

### Run

```bash
streamlit run app.py
```

---

## Demo Walkthrough

Workflow: **review issues → select any issues → scope → approve → run execution**.

1. Open the app. KPI cards are grounded in the live backlog + resolved-issues data from GitHub.
2. **Sidebar:** Adjust Planner weights to change issue priority ranking in real time.
3. **Issues** panel — Issues are shown in one unified list with two groups:
   - **Recommended for automation** (strong automation candidates)
   - **Recommended for manual handling** (risky, ambiguous, or complex)
   Selection is never locked to a group — you can select issues from either side.
4. Tick the boxes next to the issues you want to work on. **(Checkpoint 2.5 — Human Approval)**
5. Click **"Scope selected issues"** to dispatch Devin scope sessions. Takes 4–6 minutes per issue.
6. For issues that come back with scope confidence < 75, complete the review form inline.
   **(Checkpoint 3.5 — Human Review)**
7. Click **"Run execution"** to dispatch the Executor. Only issues with a complete scope plan
   (and any required review) can be dispatched.
8. **Execution pipeline** — Watch session statuses update. Click "Refresh status" to poll Devin.
9. Once sessions reach a terminal state, click **"Run optimizer"** (Stage 5) to analyse outcomes.
10. **Business Report** tab: live backlog metrics + clearly-labelled projections (assumptions editable).

