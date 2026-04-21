"""config.py — Central configuration for Backlog Autopilot

All infrastructure-level settings (credentials, timeouts, the target repo) are
loaded from environment variables here so every stage of the pipeline reads
from a single source of truth. PM-tunable scoring parameters (e.g. weights,
thresholds) live with the domain logic in `planner.py`, not here.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# GitHub
TARGET_REPO = os.getenv("TARGET_REPO", "sarahmoooree-builds/finserv-platform")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# Devin API
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY")
DEVIN_ORG_ID = os.getenv("DEVIN_ORG_ID")
DEVIN_API_BASE = f"https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}"

# Timeouts (seconds)
INGEST_TIMEOUT = int(os.getenv("INGEST_TIMEOUT", "480"))
PLANNER_TIMEOUT = int(os.getenv("PLANNER_TIMEOUT", "480"))
SCOPE_TIMEOUT = int(os.getenv("SCOPE_TIMEOUT", "360"))
OPTIMIZER_TIMEOUT = int(os.getenv("OPTIMIZER_TIMEOUT", "480"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))

# Concurrency
DEVIN_MAX_CONCURRENT_SESSIONS = int(os.getenv("DEVIN_MAX_CONCURRENT_SESSIONS", "5"))

# Slack notifications (optional — empty disables all Slack sends)
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

# Public URL of the Streamlit app. Embedded in Slack messages so approvers
# can click straight into the approval queue. Safe to leave empty.
STREAMLIT_APP_URL = os.getenv("STREAMLIT_APP_URL", "")
