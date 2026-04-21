"""config.py — Central configuration for Backlog Autopilot

All infrastructure-level settings (credentials, timeouts, the target repo) are
loaded from environment variables here so every stage of the pipeline reads
from a single source of truth. PM-tunable scoring parameters (e.g. weights,
thresholds) live with the domain logic in `planner.py`, not here.
"""

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"


def configure_logging() -> None:
    """Configure the root logger with a stdout StreamHandler.

    Idempotent: safe to call multiple times (e.g. from multiple entrypoints
    or from within Streamlit's rerun loop). If the root logger already has
    handlers we only refresh the level so the LOG_LEVEL env var still takes
    effect.
    """
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)


# Configure logging eagerly on import so any module that does
# ``from config import ...`` has a working root logger without extra setup.
configure_logging()

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
