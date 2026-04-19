"""
prompts.py — Prompt templates for future Devin integration

These templates are NOT wired up to any LLM or API yet. They exist so that
when you're ready to connect Devin (or another AI agent), you have well-structured
prompts ready to go.

Usage (future):
    from prompts import ENRICHMENT_PROMPT, EXECUTION_PROMPT

    # Fill in the template with issue data
    prompt = ENRICHMENT_PROMPT.format(
        title=issue["title"],
        description=issue["description"],
        labels=", ".join(issue["labels"]),
        age_days=issue["age_days"],
        comments_count=issue["comments_count"],
    )

    # Send to Devin API
    response = devin_client.run(prompt)
"""


# ---------------------------------------------------------------------------
# Prompt 1: Issue Enrichment
# ---------------------------------------------------------------------------
# Purpose: Given a raw GitHub issue, produce a structured analysis that helps
# a human (or an AI agent) decide whether and how to resolve it.

ENRICHMENT_PROMPT = """You are an expert software engineering analyst working on an enterprise monorepo.

Your task is to analyze the following GitHub issue and produce a structured assessment.

## Issue Details
- **Title:** {title}
- **Description:** {description}
- **Labels:** {labels}
- **Age (days):** {age_days}
- **Comments:** {comments_count}

## Instructions

Analyze this issue and return a JSON object with the following fields:

1. **summary** (string): A concise one-sentence summary of the issue in plain language.

2. **issue_type** (string): One of "bug", "feature_request", "tech_debt", "investigation", or "other".

3. **complexity** (string): One of "low", "medium", or "high".
   - Low: isolated change, one file or component, clear fix path.
   - Medium: touches 2-3 files, requires some domain context, testable.
   - High: cross-cutting, architectural, requires design decisions.

4. **scope** (string): One of "narrow" or "broad".
   - Narrow: limited to a single module, page, or service.
   - Broad: spans multiple modules, services, or teams.

5. **risk** (string): One of "low" or "high".
   - High if it touches authentication, billing, data integrity, or core infrastructure.
   - Low otherwise.

6. **candidate** (boolean): true if this issue is a good candidate for autonomous AI resolution, false otherwise.

7. **candidate_reason** (string): A brief explanation of why this issue is or is not a good automation candidate.

8. **suggested_approach** (string): If candidate is true, outline the likely fix approach in 2-3 sentences.

Respond ONLY with the JSON object. No additional commentary.
"""


# ---------------------------------------------------------------------------
# Prompt 2: Issue Execution
# ---------------------------------------------------------------------------
# Purpose: Given an approved issue with enrichment data, instruct an AI agent
# to actually implement the fix in the codebase.

EXECUTION_PROMPT = """You are an autonomous software engineering agent working on an enterprise monorepo.

You have been approved to resolve the following GitHub issue.

## Subagent Instructions

Use the two-agent pipeline defined in `.devin/agents/`:

1. **Spawn `issue-explorer` as a background subagent.** Give it the issue details below and ask it to produce an investigation report: root cause, relevant files and line numbers, suggested fix approach, and test coverage notes.

2. **Wait for the explorer to finish.** Read its investigation report carefully.

3. **Spawn `issue-fixer` as a foreground subagent.** Pass it the full investigation report and instruct it to implement the fix, run tests, and open a PR.

## Issue Details
- **Title:** {title}
- **Description:** {description}
- **Labels:** {labels}
- **Issue Type:** {issue_type}
- **Complexity:** {complexity}
- **Scope:** {scope}

## Enrichment Summary
{summary}

## Instructions

1. **Understand the issue**: Read the description carefully. Identify the root cause or the required change.

2. **Locate the relevant code**: Search the repository for the files and functions related to this issue. Use the description, labels, and any file references as starting points.

3. **Plan your fix**: Before writing any code, outline your approach:
   - What files will you modify?
   - What is the expected behavior after your fix?
   - Are there existing tests you need to update?

4. **Implement the fix**:
   - Make the minimum change necessary to resolve the issue.
   - Follow existing code style and conventions in the repository.
   - Do not refactor unrelated code.
   - Add or update tests to cover your change.

5. **Verify your work**:
   - Run the existing test suite and confirm all tests pass.
   - If applicable, manually verify the fix addresses the reported behavior.

6. **Prepare your output**: Return a structured summary including:
   - Files modified (list)
   - Description of changes (2-3 sentences)
   - Test results (pass/fail)
   - Any concerns or follow-up items for human review

## Constraints
- Do NOT modify configuration files, CI pipelines, or infrastructure code unless the issue specifically requires it.
- Do NOT introduce new dependencies without flagging them for review.
- If you encounter ambiguity or cannot confidently resolve the issue, STOP and report what you found instead of guessing.
"""
