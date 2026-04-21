"""config.py — Central configuration for Backlog Autopilot

All infrastructure-level settings (credentials, timeouts, the target repo) are
loaded from environment variables here so every stage of the pipeline reads
from a single source of truth. PM-tunable scoring parameters (e.g. weights,
thresholds) live with the domain logic in `planner.py`, not here.

This module also exports a shared ``SESSION`` — a ``requests.Session`` pre-
configured with urllib3's ``Retry`` so every Devin and GitHub API call in the
pipeline retries transient 429/5xx failures and connection drops instead of
surfacing them as fire-once errors. urllib3 emits retry attempts at DEBUG
level on ``urllib3.connectionpool`` / ``urllib3.util.retry``, so enabling
DEBUG logging there will surface retry activity automatically.
"""

import logging
import os
import sys

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
SCOPE_TIMEOUT = int(os.getenv("SCOPE_TIMEOUT", "600"))
OPTIMIZER_TIMEOUT = int(os.getenv("OPTIMIZER_TIMEOUT", "480"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))

# Concurrency
DEVIN_MAX_CONCURRENT_SESSIONS = int(os.getenv("DEVIN_MAX_CONCURRENT_SESSIONS", "5"))


# ---------------------------------------------------------------------------
# Shared HTTP session with automatic retry on transient failures
# ---------------------------------------------------------------------------

def _build_session() -> requests.Session:
    """Build a requests.Session with retries on transient HTTP failures.

    Only idempotent methods are retried. POST is listed nowhere — and,
    more importantly, every POST in this project (Devin session creation
    at ``/sessions``) bypasses this session entirely and uses
    ``requests.post`` directly. In urllib3's ``Retry``, ``allowed_methods``
    is only consulted for status- and read-error retries; the
    connection-error path retries regardless of method, so POSTing via
    ``SESSION`` would silently spawn duplicate orphaned Devin sessions on
    DNS / connection-refused / connect-timeout failures. Keep the
    ``allowed_methods=["GET"]`` setting defensive, but rely on the
    per-call-site ``requests.post`` to guarantee non-idempotent requests
    are never retried.
    """
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,  # 1s, 2s, 4s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _build_session()
