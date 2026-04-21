"""
config.py — shared HTTP session with retry logic.

Every Devin API and GitHub API call in this project goes through the
shared `SESSION` exported here. The session is configured with urllib3's
`Retry` so transient failures (429, 5xx, connection drops) are retried
automatically with exponential backoff instead of surfacing as fire-once
errors to the caller.

urllib3 logs retry attempts at DEBUG level, so enabling DEBUG logging on
the `urllib3.connectionpool` logger (or the root logger) will surface
the retry activity for debugging.
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _build_session() -> requests.Session:
    """Build a requests.Session with retries on transient HTTP failures."""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,  # 1s, 2s, 4s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _build_session()
