"""
github_client.py — Fetch live data from GitHub

Pulls real issues and PRs from the finserv-platform repo using the
GitHub REST API v3 via the `requests` library.
"""

from datetime import datetime, timezone, timedelta

import requests

from config import GITHUB_TOKEN, SESSION, TARGET_REPO

API_ROOT = "https://api.github.com"
BASE_URL = f"{API_ROOT}/repos/{TARGET_REPO}"


def _headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _paginate(url, headers=None, params=None):
    """
    Follow `Link: <...>; rel="next"` headers and yield every item across all pages.

    The first request uses `params`; subsequent requests use the full `next` URL
    as returned by GitHub (which already contains the pagination cursor).
    """
    if headers is None:
        headers = _headers()
    if params is None:
        params = {}

    next_url = url
    next_params = params

    while next_url:
        response = SESSION.get(next_url, headers=headers, params=next_params, timeout=30)
        if not response.ok:
            raise RuntimeError(
                f"GitHub API request failed: {response.status_code} "
                f"{response.reason} for {next_url} — body: {response.text[:500]}"
            )

        data = response.json()
        if not isinstance(data, list):
            raise RuntimeError(
                f"Expected JSON array from {next_url}, got {type(data).__name__}: "
                f"{str(data)[:200]}"
            )

        for item in data:
            yield item

        # After the first request, the Link header's next URL already carries
        # the cursor, so we must not re-send the original params.
        next_url = response.links.get("next", {}).get("url")
        next_params = None


def _parse_iso(value):
    """Parse a GitHub ISO 8601 timestamp (e.g. '2025-01-02T03:04:05Z')."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_issues(state="open"):
    """
    Fetch issues from the target repo (excluding pull requests).

    Returns a list of dicts matching the shape the scorer expects:
      - id, title, description, labels, age_days, comments_count
    """
    url = f"{BASE_URL}/issues"
    params = {"state": state, "per_page": 100}

    now = datetime.now(timezone.utc)
    issues = []
    for raw in _paginate(url, params=params):
        # The issues endpoint returns pull requests too; skip them.
        if "pull_request" in raw:
            continue

        created = _parse_iso(raw["created_at"])
        age_days = (now - created).days

        issues.append({
            "id": raw["number"],
            "title": raw["title"],
            "description": raw.get("body") or "",
            "labels": [label["name"] for label in raw.get("labels", [])],
            "age_days": age_days,
            "comments_count": raw.get("comments", 0) or 0,
        })

    return issues


def fetch_pull_requests(state="open"):
    """
    Fetch PRs from the target repo.

    Returns a list of dicts with:
      - number, title, state, url, head_branch, created_at, merged_at
    """
    url = f"{BASE_URL}/pulls"
    params = {"state": state, "per_page": 100}

    prs = []
    for raw in _paginate(url, params=params):
        head = raw.get("head") or {}
        prs.append({
            "number": raw["number"],
            "title": raw["title"],
            "state": raw["state"],
            "url": raw.get("html_url", ""),
            "head_branch": head.get("ref", "") or "",
            "created_at": raw.get("created_at", "") or "",
            "merged_at": raw.get("merged_at"),
        })

    return prs


def fetch_closed_issues(days=30):
    """
    Fetch closed issues from the target repo with their closed_at timestamp,
    keeping only those closed within the last `days` days.

    Returns a list of dicts:
      - number, title, closed_at (ISO string), created_at
    Used for the "Issues resolved" chart so we ground the UI in real data.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    url = f"{BASE_URL}/issues"
    params = {
        "state": "closed",
        "since": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "per_page": 100,
    }

    closed = []
    for raw in _paginate(url, params=params):
        # Skip PRs returned by the issues endpoint.
        if "pull_request" in raw:
            continue
        closed_at = raw.get("closed_at")
        if not closed_at:
            continue
        dt = _parse_iso(closed_at)
        if dt < cutoff:
            continue
        closed.append({
            "number": raw["number"],
            "title": raw["title"],
            "closed_at": closed_at,
            "created_at": raw.get("created_at", "") or "",
        })
    return closed


def fetch_merged_prs(days=30):
    """
    Fetch merged PRs within the last `days` days with author attribution.
    Returns list of dicts: {number, title, merged_at, author_login, head_branch, is_devin_authored}.
    Used by the dashboard for real throughput data.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    url = f"{BASE_URL}/pulls"
    params = {
        "state": "closed",
        "sort": "updated",
        "direction": "desc",
        "per_page": 100,
    }

    out = []
    for raw in _paginate(url, params=params):
        merged_at = raw.get("merged_at")
        if not merged_at:
            continue
        dt = _parse_iso(merged_at)
        if dt < cutoff:
            # Results are sorted by updated desc; updated_at >= merged_at, so
            # once we see a merged PR older than the cutoff we might still
            # encounter newer ones in updated-order. Keep scanning rather than
            # breaking early.
            continue

        author = ((raw.get("user") or {}).get("login")) or ""
        head = (raw.get("head") or {}).get("ref", "") or ""
        is_devin = "devin" in author.lower() or head.lower().startswith("devin/")
        out.append({
            "number": raw["number"],
            "title": raw["title"],
            "merged_at": merged_at,
            "author_login": author,
            "head_branch": head,
            "is_devin_authored": is_devin,
        })
    return out


def fetch_merged_prs_count(days=7):
    """
    Count how many PRs were merged in the last N days, split by Devin vs. engineers.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    url = f"{BASE_URL}/pulls"
    params = {
        "state": "closed",
        "sort": "updated",
        "direction": "desc",
        "per_page": 100,
    }

    devin_count = 0
    engineer_count = 0
    for raw in _paginate(url, params=params):
        merged_at = raw.get("merged_at")
        if not merged_at:
            continue
        dt = _parse_iso(merged_at)
        if dt < cutoff:
            continue
        author = ((raw.get("user") or {}).get("login")) or ""
        head = (raw.get("head") or {}).get("ref", "") or ""
        if "devin" in author.lower() or head.lower().startswith("devin/"):
            devin_count += 1
        else:
            engineer_count += 1

    return {"devin": devin_count, "engineers": engineer_count}
