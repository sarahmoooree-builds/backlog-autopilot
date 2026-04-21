"""
cli.py — Headless entry point for the Backlog Autopilot pipeline.

Wraps the rule-based stages (``ingest``, ``planner``, ``executor``) with a
small argparse-based CLI so GitHub Actions / cron can run the pipeline
outside of Streamlit. Every subcommand is idempotent and can be scheduled
independently.

Subcommands
-----------
  ingest          Fetch issues from GitHub and run rule-based ingest.
  plan            Score ingested issues. Pass --full-refresh to re-score
                  from scratch (daily 6am run).
  rescore         Re-score a single issue by ID (webhook-driven).
  refresh-status  Poll the Devin API for execution session updates.
  notify          Send approval-needed Slack reminders for any recommended
                  issues still awaiting approval.

Exit code is 0 on success, 1 on any uncaught exception. Output is plain
text so it's readable in GitHub Actions logs.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import store
from config import TARGET_REPO
from executor import refresh_session_statuses
from github_client import fetch_issues
from ingest import ingest_issues, normalize_issue
from ingest import (
    assess_risk,
    classify_complexity,
    classify_issue_type,
    classify_scope,
    dedupe_check,
    generate_summary,
)
from notifications import notify_approval_needed, notify_new_recommended_issue
from planner import RECOMMEND_THRESHOLD, plan_issues
from priorities import BALANCED_INTENT, get_strategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _persist_ingested(issues: list) -> None:
    """Write a batch of ingested issues to the store, replacing any existing
    record for the same issue ID."""
    for issue in issues:
        store.set_ingested(issue["id"], issue)


def _persist_planned(issues: list) -> None:
    for issue in issues:
        store.set_planned(issue["id"], issue)


def _reingest_single(raw_issue: dict, peers: list) -> dict:
    """Run ingest classification for a single raw issue, using ``peers`` as
    the dedupe corpus. Mirrors the per-issue logic inside ``ingest_issues``
    without re-processing the whole batch."""
    normalised = normalize_issue(raw_issue)
    issue_type = classify_issue_type(normalised)
    complexity = classify_complexity(normalised)
    scope = classify_scope(normalised)
    risk = assess_risk(normalised, issue_type, complexity, scope)
    summary = generate_summary(normalised, issue_type, complexity)
    duplicate_of = dedupe_check(normalised, peers)

    from datetime import datetime as _dt
    return {
        **normalised,
        "summary": summary,
        "issue_type": issue_type,
        "complexity": complexity,
        "scope": scope,
        "risk": risk,
        "duplicate_of": duplicate_of,
        "ingested_at": _dt.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

def cmd_ingest(_args: argparse.Namespace) -> int:
    """Fetch issues from GitHub and save normalised records to the store."""
    print(f"[cli] Fetching open issues from {TARGET_REPO}…")
    raw = fetch_issues()
    print(f"[cli] Fetched {len(raw)} issue(s).")

    ingested = ingest_issues(raw)
    _persist_ingested(ingested)

    # Clear the Devin-ingest meta so the rule-based records are the source
    # of truth for the planner that runs next.
    store.clear_pipeline_meta("ingest")

    print(f"[cli] Ingested and stored {len(ingested)} issue(s).")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Score ingested issues. ``--full-refresh`` clears planner state first
    so every issue is re-scored from scratch (used for the daily 6am run).
    """
    if args.full_refresh:
        print("[cli] Full refresh requested — clearing prior plan + notification state.")
        for record in store.all_records("planned"):
            store.delete_record("planned", record["issue_id"])
        store.clear_pipeline_meta("planner")
        store.clear_pipeline_meta("notifications")

    ingested = store.all_records("ingested")
    if not ingested:
        print("[cli] No ingested issues in store — run `python cli.py ingest` first.")
        return 0

    strategy = get_strategy(BALANCED_INTENT)
    planned = plan_issues(ingested, strategy=strategy)
    _persist_planned(planned)

    recommended = [p for p in planned if p["planner_score"]["recommended"]]
    print(
        f"[cli] Planned {len(planned)} issue(s); "
        f"{len(recommended)} above threshold {RECOMMEND_THRESHOLD}."
    )
    return 0


def cmd_rescore(args: argparse.Namespace) -> int:
    """Re-ingest and re-score a single issue. Used by the webhook handler
    when an issue is opened, labelled, or edited. If the issue crosses
    ``RECOMMEND_THRESHOLD`` after scoring, a Slack notification is sent
    immediately."""
    issue_id = args.issue_id
    print(f"[cli] Re-scoring issue #{issue_id}…")

    raw = fetch_issues()
    target = next((i for i in raw if int(i["id"]) == int(issue_id)), None)
    if target is None:
        print(
            f"[cli] Issue #{issue_id} is not in the open set for {TARGET_REPO}. "
            "It may be closed, renamed, or from a different repo. Skipping."
        )
        return 0

    ingested = _reingest_single(target, raw)
    store.set_ingested(issue_id, ingested)

    peers = store.all_records("ingested") or [ingested]
    strategy = get_strategy(BALANCED_INTENT)
    planned = plan_issues(peers, strategy=strategy)
    _persist_planned(planned)

    updated = next((p for p in planned if int(p["id"]) == int(issue_id)), None)
    if updated is None:
        print(f"[cli] Issue #{issue_id} was rescored but not found in planner output.")
        return 0

    score = updated["planner_score"]
    print(
        f"[cli] Issue #{issue_id} rescored: total_score={score['total_score']:.2f}, "
        f"recommended={score['recommended']}."
    )

    if score["recommended"]:
        notify_new_recommended_issue(
            issue_id=issue_id,
            title=updated.get("title", ""),
            score=score["total_score"],
        )
    return 0


def cmd_refresh_status(_args: argparse.Namespace) -> int:
    """Poll the Devin API for updates to in-flight execution sessions and
    persist any status changes."""
    print("[cli] Refreshing execution session statuses…")
    sessions = refresh_session_statuses()
    terminal = sum(1 for s in sessions if s["status"] in ("Completed", "Blocked"))
    print(f"[cli] Refreshed {len(sessions)} session(s); {terminal} in terminal state.")
    return 0


def cmd_notify(_args: argparse.Namespace) -> int:
    """Send a reminder to Slack for any recommended issues that are still
    awaiting human approval. Unlike the per-event notifications fired from
    ``plan_issues``, this always re-notifies on every call — it's meant to
    run on the 6-hour backstop schedule to surface aging approvals."""
    planned = store.all_records("planned")
    pending = [
        p["id"] for p in planned
        if p["planner_score"]["recommended"]
        and not store.is_approved(p["id"])
        and not store.is_dispatched(p["id"])
    ]
    if not pending:
        print("[cli] No recommended issues awaiting approval.")
        return 0

    sent = notify_approval_needed(sorted(pending))
    if sent:
        print(f"[cli] Sent approval-needed reminder for {len(pending)} issue(s).")
    else:
        print(
            f"[cli] {len(pending)} issue(s) awaiting approval but Slack webhook "
            "is not configured or the request failed."
        )
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backlog-autopilot",
        description="Headless CLI entry point for the Backlog Autopilot pipeline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_p = subparsers.add_parser(
        "ingest", help="Fetch issues from GitHub and run rule-based ingest."
    )
    ingest_p.set_defaults(func=cmd_ingest)

    plan_p = subparsers.add_parser(
        "plan", help="Run planner on ingested issues."
    )
    plan_p.add_argument(
        "--full-refresh",
        action="store_true",
        help="Re-score all issues from scratch (daily 6am run).",
    )
    plan_p.set_defaults(func=cmd_plan)

    rescore_p = subparsers.add_parser(
        "rescore", help="Re-score a single changed issue by ID."
    )
    rescore_p.add_argument("--issue-id", type=int, required=True)
    rescore_p.set_defaults(func=cmd_rescore)

    refresh_p = subparsers.add_parser(
        "refresh-status", help="Poll Devin for execution session status updates."
    )
    refresh_p.set_defaults(func=cmd_refresh_status)

    notify_p = subparsers.add_parser(
        "notify", help="Send Slack reminders for pending approvals."
    )
    notify_p.set_defaults(func=cmd_notify)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 — surface CI-visible traceback
        print(f"[cli] Unhandled exception in `{args.command}`: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
