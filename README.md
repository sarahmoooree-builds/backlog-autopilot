# Backlog Autopilot

A backlog resolution system that turns a wall of stale GitHub issues into an actionable pipeline for autonomous resolution.

## The Problem

Enterprise engineering teams accumulate hundreds of open GitHub issues — small bugs, minor feature requests, tech debt cleanups. Senior engineers ignore them because they're focused on platform work. Junior engineers spend too much time understanding issues before they can fix them. The result: a growing wall of stale issues and a backlog that never shrinks.

## The Insight

The bottleneck is not just *fixing* issues. It is *turning messy backlog items into understandable, actionable work*. Most issues sit untouched not because they're hard to fix, but because nobody has taken the time to triage, clarify, and scope them.

## How It Works

Backlog Autopilot implements a five-stage pipeline:

1. **Ingest** — Load issues from the backlog (currently a JSON file; later, the GitHub API).
2. **Enrich** — Classify each issue by type, complexity, scope, and risk. Generate a plain-language summary.
3. **Recommend** — Score each issue and flag the best candidates for autonomous resolution. Clear bugs with narrow scope and low complexity score highest.
4. **Approve** — A human reviews the recommendations and approves a batch for execution.
5. **Execute** — Approved issues are sent to an AI agent for resolution. Results are tracked by status: Completed, Awaiting Review, In Progress, or Blocked.

## What This MVP Includes

| Component | Description |
|---|---|
| `issues.json` | 10 realistic sample GitHub issues with a mix of bugs, feature requests, tech debt, and investigations |
| `scorer.py` | Rule-based enrichment and candidate scoring — classifies type, complexity, scope, risk, and generates recommendations |
| `mock_executor.py` | Simulated execution results that return realistic statuses and outcome summaries |
| `prompts.py` | Prompt templates for future AI agent integration (enrichment + execution prompts) |
| `app.py` | Streamlit UI with KPI cards, issue review, approval workflow, and execution results |

## What Is Mocked Today

The execution layer (`mock_executor.py`) returns simulated results. No AI agent is called. The mock logic maps issue characteristics to plausible outcomes:

- Simple, well-described bugs → Completed
- Medium complexity → Awaiting Review
- Missing context → Blocked

## How Devin Plugs In Later

The system is designed so that replacing `mock_executor.py` with real Devin API calls is the only change needed to go live:

1. **`prompts.py`** already contains structured prompt templates for Devin.
2. **`mock_executor.py`** has a clean `execute_issues()` interface — swap the mock logic for Devin API calls while keeping the same input/output shape.
3. The Streamlit app does not need to change — it consumes execution results regardless of whether they come from a mock or a real agent.

## Setup

### Prerequisites

- Python 3.9+
- pip

### Install

```bash
cd backlog-autopilot
pip install -r requirements.txt
```

### Run

```bash
streamlit run app.py
```

The app will open in your browser at `http://localhost:8501`.

## Project Structure

```
backlog-autopilot/
├── app.py              # Streamlit UI
├── issues.json         # Sample issue data
├── scorer.py           # Enrichment + scoring logic
├── mock_executor.py    # Simulated execution
├── prompts.py          # Prompt templates for Devin
├── requirements.txt    # Python dependencies
└── README.md           # This file
```

## Demo Walkthrough

When screen-sharing this app:

1. Start at the top — the KPI cards show the backlog at a glance.
2. Walk through the "Recommended for Automation" section — explain *why* each issue was flagged.
3. Approve 2-3 issues and click "Run Approved Issues."
4. Show the execution results — point out how different issue types get different outcomes.
5. Scroll to "Human-Owned Issues" — explain why these remain in the human queue.
6. Close with: "Today the execution is mocked. When we plug in Devin, this becomes a live resolution pipeline."
