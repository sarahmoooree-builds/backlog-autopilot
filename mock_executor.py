"""
mock_executor.py — Simulated execution results

This module pretends to be the execution layer (where Devin would plug in).
Given a list of approved issues, it returns realistic-looking statuses and
outcome summaries so the demo feels end-to-end.

When you're ready to integrate Devin, replace the logic in `execute_issues()`
with real Devin API calls. The input/output shape stays the same.
"""

import random


def execute_issues(approved_issues):
    """
    Simulate execution of approved issues.

    Args:
        approved_issues: list of enriched issue dicts that the user approved.

    Returns:
        list of result dicts, each containing:
          - id: the issue id
          - title: the issue title
          - status: one of "Completed", "Awaiting Review", "In Progress", "Blocked"
          - outcome_summary: a short description of what happened
    """
    results = []

    for issue in approved_issues:
        status, outcome = _simulate_outcome(issue)
        results.append({
            "id": issue["id"],
            "title": issue["title"],
            "status": status,
            "outcome_summary": outcome,
        })

    return results


def _simulate_outcome(issue):
    """
    Pick a realistic status and outcome based on the issue's characteristics.

    The logic mirrors what you'd expect from an AI agent:
    - Simple, well-described bugs → usually completed
    - Issues missing detail → often blocked
    - Medium complexity → awaiting review or in progress
    """
    complexity = issue.get("complexity", "medium")
    issue_type = issue.get("issue_type", "other")
    comments = issue.get("comments_count", 0)

    # Simple bugs with clear descriptions → high chance of completion
    if issue_type == "bug" and complexity == "low":
        return "Completed", (
            f"Fix applied. The root cause was identified in the area described by the issue. "
            f"A targeted patch was generated and unit tests pass. Ready for code review."
        )

    # Medium-complexity bugs → awaiting review
    if issue_type == "bug" and complexity == "medium":
        return "Awaiting Review", (
            f"A fix has been drafted but touches multiple code paths. "
            f"Automated tests pass, but the change should be reviewed by a domain expert "
            f"before merging."
        )

    # Tech debt with low complexity → completed
    if issue_type == "tech_debt" and complexity == "low":
        return "Completed", (
            f"The cleanup has been applied. Changes are minimal and localized. "
            f"All existing tests continue to pass."
        )

    # Tech debt with medium complexity → in progress
    if issue_type == "tech_debt" and complexity == "medium":
        return "In Progress", (
            f"Work is underway. The initial refactor is done but downstream "
            f"integration points still need updating. Estimated 60% complete."
        )

    # Issues with very few comments might lack context → blocked
    if comments <= 1:
        return "Blocked", (
            f"Insufficient context to proceed. The issue description does not include "
            f"enough detail to confidently implement a fix. A human should add "
            f"reproduction steps or clarify expected behavior."
        )

    # Default fallback: in progress
    return "In Progress", (
        f"Execution has started. Initial analysis is complete and a solution "
        f"approach has been identified. Work is continuing."
    )
