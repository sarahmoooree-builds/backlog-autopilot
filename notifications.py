"""
notifications.py — Slack notifications for the Backlog Autopilot pipeline.

Sent from headless contexts (CLI, GitHub Actions) as well as Streamlit.
All helpers are safe no-ops when ``SLACK_WEBHOOK_URL`` is unset, so local
development and tests never accidentally post to Slack.
"""

from typing import Optional

import requests

from config import SLACK_WEBHOOK_URL, STREAMLIT_APP_URL


def send_slack_notification(
    webhook_url: Optional[str],
    message: str,
    blocks: Optional[list] = None,
) -> bool:
    """POST a message to a Slack incoming webhook.

    Returns True on success, False otherwise. Missing/empty ``webhook_url``
    is treated as a no-op (returns False) rather than an error so the
    pipeline keeps running in environments where Slack isn't configured.
    """
    if not webhook_url:
        return False

    payload: dict = {"text": message}
    if blocks:
        payload["blocks"] = blocks

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"[notifications] Slack webhook request failed: {e}")
        return False

    if not response.ok:
        print(
            f"[notifications] Slack webhook returned {response.status_code}: "
            f"{response.text[:200]}"
        )
        return False
    return True


def _approval_link() -> str:
    """Markdown-style link back to the Streamlit UI for human approval."""
    if STREAMLIT_APP_URL:
        return f" <{STREAMLIT_APP_URL}|Review in Backlog Autopilot>"
    return ""


def notify_completion(issue_id: int, session_url: str) -> bool:
    """Notify that a Devin execution session finished successfully."""
    message = (
        f":white_check_mark: Issue #{issue_id} completed. PR ready for review. "
        f"<{session_url}|View Devin session>"
    )
    return send_slack_notification(SLACK_WEBHOOK_URL, message)


def notify_blocked(issue_id: int, session_url: str) -> bool:
    """Notify that a Devin execution session is blocked and needs human input."""
    message = (
        f":warning: Issue #{issue_id} blocked. Human input needed. "
        f"<{session_url}|View Devin session>"
    )
    return send_slack_notification(SLACK_WEBHOOK_URL, message)


def notify_approval_needed(issue_ids: list) -> bool:
    """Notify that one or more issues scored above the recommendation
    threshold and are awaiting human approval in the Streamlit UI."""
    if not issue_ids:
        return False
    ids = ", ".join(f"#{i}" for i in issue_ids)
    count = len(issue_ids)
    noun = "issue" if count == 1 else "issues"
    message = (
        f":mag: {count} {noun} scored above threshold and await approval: "
        f"{ids}.{_approval_link()}"
    )
    return send_slack_notification(SLACK_WEBHOOK_URL, message)


def notify_new_recommended_issue(issue_id: int, title: str, score: float) -> bool:
    """Notify that a newly-ingested/rescored issue crossed the recommendation
    threshold. Used by the webhook handler to give real-time visibility."""
    message = (
        f":sparkles: Issue #{issue_id} scored {score:.1f}/10 — recommended for "
        f"automation. _{title}_{_approval_link()}"
    )
    return send_slack_notification(SLACK_WEBHOOK_URL, message)
