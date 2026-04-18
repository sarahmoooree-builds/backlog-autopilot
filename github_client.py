"""
github_client.py — Fetch live data from GitHub

Pulls real issues and PRs from the finserv-platform repo using the
GitHub REST API (via the gh CLI for simplicity and auth).
"""

import json
import subprocess

# The repo Backlog Autopilot monitors
REPO = "sarahmoooree-builds/finserv-platform"


def fetch_issues(state="open"):
    """
    Fetch open issues from the target repo.

    Returns a list of dicts matching the shape the scorer expects:
      - id, title, description, labels, age_days, comments_count
    """
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--repo", REPO,
            "--state", state,
            "--limit", "50",
            "--json", "number,title,body,labels,createdAt,comments",
        ],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"Error fetching issues: {result.stderr}")
        return []

    raw_issues = json.loads(result.stdout)

    # Convert to the format our scorer expects
    issues = []
    for raw in raw_issues:
        # Calculate age in days from createdAt
        from datetime import datetime, timezone
        created = datetime.fromisoformat(raw["createdAt"].replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - created).days

        issues.append({
            "id": raw["number"],
            "title": raw["title"],
            "description": raw.get("body", "") or "",
            "labels": [label["name"] for label in raw.get("labels", [])],
            "age_days": age_days,
            "comments_count": len(raw.get("comments", [])),
        })

    return issues


def fetch_pull_requests(state="open"):
    """
    Fetch PRs from the target repo.

    Returns a list of dicts with:
      - number, title, state, url, head_branch, created_at
    """
    result = subprocess.run(
        [
            "gh", "pr", "list",
            "--repo", REPO,
            "--state", state,
            "--limit", "50",
            "--json", "number,title,state,url,headRefName,createdAt,mergedAt",
        ],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"Error fetching PRs: {result.stderr}")
        return []

    raw_prs = json.loads(result.stdout)

    prs = []
    for raw in raw_prs:
        prs.append({
            "number": raw["number"],
            "title": raw["title"],
            "state": raw["state"],
            "url": raw["url"],
            "head_branch": raw.get("headRefName", ""),
            "created_at": raw.get("createdAt", ""),
            "merged_at": raw.get("mergedAt"),
        })

    return prs


def fetch_merged_prs_count(days=7):
    """
    Count how many PRs were merged in the last N days.
    Useful for the dashboard metrics.
    """
    result = subprocess.run(
        [
            "gh", "pr", "list",
            "--repo", REPO,
            "--state", "merged",
            "--limit", "100",
            "--json", "mergedAt,author",
        ],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        return {"devin": 0, "engineers": 0}

    prs = json.loads(result.stdout)

    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    devin_count = 0
    engineer_count = 0

    for pr in prs:
        if not pr.get("mergedAt"):
            continue
        merged = datetime.fromisoformat(pr["mergedAt"].replace("Z", "+00:00"))
        if merged >= cutoff:
            author = pr.get("author", {}).get("login", "")
            if "devin" in author.lower():
                devin_count += 1
            else:
                engineer_count += 1

    return {"devin": devin_count, "engineers": engineer_count}
