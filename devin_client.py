"""
devin_client.py — Shared Devin v3 API client.

Encapsulates session creation, polling, message fetching, and JSON
extraction logic used by the Ingest, Planner, Scope, and Executor stages.

Error contract for create_session:
  - Network errors propagate as requests.exceptions.RequestException.
  - Non-2xx responses are raised as RuntimeError with a message of the form
    ``f"API returned {status}: {body[:200]}"`` — matching the inline error
    strings previously produced by each stage.
"""

import json
import os
import time
import requests
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

DEVIN_API_KEY = os.getenv("DEVIN_API_KEY")
DEVIN_ORG_ID = os.getenv("DEVIN_ORG_ID")
DEVIN_API_BASE = f"https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}"

# Devin v3 statuses treated as "still working" — anything else is terminal.
# See https://docs.devin.ai/api-reference/v3/sessions/get-organizations-session
_NON_TERMINAL_STATUSES = (
    "new", "creating", "claimed", "running", "resuming",
    # legacy aliases retained for safety if older payloads appear
    "starting", "queued", "initializing", "created",
)
# status_detail values (with status="running") that indicate Devin has
# produced its final work product and is idle/awaiting the next step.
_WORK_PRODUCT_READY_DETAILS = ("waiting_for_user", "finished")


def _auth_headers(include_content_type: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {DEVIN_API_KEY}"}
    if include_content_type:
        headers["Content-Type"] = "application/json"
    return headers


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def create_session(prompt: str, bypass_approval: bool = True,
                   idempotency_key: Optional[str] = None) -> dict:
    """POST /sessions to start a new Devin session.

    Returns ``{"session_id": str, "session_url": str, "raw": dict}``.
    Raises ``requests.exceptions.RequestException`` on network errors and
    ``RuntimeError`` on non-2xx responses.
    """
    payload = {"prompt": prompt, "bypass_approval": bypass_approval}
    headers = _auth_headers(include_content_type=True)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    response = requests.post(
        f"{DEVIN_API_BASE}/sessions",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"API returned {response.status_code}: {response.text[:200]}"
        )

    data = response.json()
    session_id = data.get("session_id", "")
    session_url = data.get("url", f"https://app.devin.ai/sessions/{session_id}")
    return {"session_id": session_id, "session_url": session_url, "raw": data}


def get_session(session_id: str) -> Optional[dict]:
    """GET a single snapshot of the current session state.

    Returns the raw API dict or ``None`` on any error. Used by consumers that
    need a one-shot read rather than the full ``poll_until_done`` loop.
    """
    try:
        response = requests.get(
            f"{DEVIN_API_BASE}/sessions/{session_id}",
            headers=_auth_headers(),
            timeout=15,
        )
        if response.status_code == 200:
            return response.json()
    except requests.exceptions.RequestException:
        pass
    return None


def poll_until_done(session_id: str, timeout: int = 360,
                    poll_interval: int = 10,
                    label: str = "devin") -> Optional[dict]:
    """Poll a Devin session until a terminal state or timeout.

    Uses the canonical non-terminal status set and work-product-ready detail
    values. Returns the final session dict or ``None`` on timeout.

    ``label`` controls the log prefix, e.g. ``"ingest"`` → ``[ingest] Poll #1``.
    """
    headers = _auth_headers()
    deadline = time.time() + timeout
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        try:
            response = requests.get(
                f"{DEVIN_API_BASE}/sessions/{session_id}",
                headers=headers,
                timeout=15,
            )
            if response.status_code == 200:
                session = response.json()
                status = (session.get("status") or "unknown").lower()
                detail = (session.get("status_detail") or "").lower()
                print(f"[{label}] Poll #{attempt}: status={status!r} detail={detail!r}")
                if status not in _NON_TERMINAL_STATUSES:
                    print(f"[{label}] Poll #{attempt}: terminal status reached ({status!r})")
                    return session
                if detail in _WORK_PRODUCT_READY_DETAILS:
                    print(
                        f"[{label}] Poll #{attempt}: Devin work product ready "
                        f"(status={status!r}, detail={detail!r})"
                    )
                    return session
            else:
                print(f"[{label}] Poll #{attempt}: HTTP {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[{label}] Poll #{attempt}: request error — {e}")

        time.sleep(poll_interval)

    print(f"[{label}] Timed out after {timeout}s ({attempt} polls)")
    return None


def fetch_messages(session_id: str, label: str = "devin_client") -> list:
    """Fetch all messages for a session from the v3 messages endpoint.

    Paginates through ``?first=200`` pages up to ``max_pages=20``. Returns a
    list of message dicts in chronological order; may be empty on error.
    """
    url = f"{DEVIN_API_BASE}/sessions/{session_id}/messages"
    headers = _auth_headers()
    messages: list = []
    cursor = None
    pages = 0
    max_pages = 20

    while pages < max_pages:
        params = {"first": 200}
        if cursor:
            params["after"] = cursor
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except requests.exceptions.RequestException as e:
            print(f"[{label}] fetch_messages: request error — {e}")
            break
        if response.status_code != 200:
            print(
                f"[{label}] fetch_messages: HTTP {response.status_code} "
                f"— {response.text[:200]}"
            )
            break
        payload = response.json()
        items = payload.get("items") or []
        messages.extend(items)
        if not payload.get("has_next_page"):
            break
        cursor = payload.get("end_cursor")
        if not cursor:
            break
        pages += 1
    return messages


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_json_array(session_data: dict,
                       messages: Optional[list] = None) -> Optional[list]:
    """Extract a JSON array from Devin session output.

    Precedence:
      1. ``session_data["structured_output"]`` when it is a non-empty list.
      2. Messages — either the ``messages`` parameter or message-list fields
         on the session payload (``messages``, ``items``, ``conversation``,
         ``history``). Scanned most-recent first; content is read from
         ``content``, ``message``, ``text``, or ``body``.
      3. Top-level text fields (``output``, ``result``, ``response``,
         ``output_text``, ``last_message``).
    """
    structured = session_data.get("structured_output")
    if isinstance(structured, list) and structured:
        return structured

    candidates: list = []
    if messages:
        candidates.extend(messages)
    for msg_field in ("messages", "items", "conversation", "history"):
        msgs = session_data.get(msg_field) or []
        if isinstance(msgs, list):
            candidates.extend(msgs)

    for message in reversed(candidates):
        if not isinstance(message, dict):
            continue
        for content_field in ("content", "message", "text", "body"):
            content = message.get(content_field, "")
            if not content or not isinstance(content, str):
                continue
            try:
                parsed = json.loads(content.strip())
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, AttributeError):
                pass
            start = content.find("[")
            end = content.rfind("]") + 1
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(content[start:end])
                    if isinstance(parsed, list):
                        return parsed
                except json.JSONDecodeError:
                    pass

    for field in ("output", "result", "response", "output_text", "last_message"):
        val = session_data.get(field)
        if not val or not isinstance(val, str):
            continue
        try:
            parsed = json.loads(val.strip())
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, AttributeError):
            pass
        start = val.find("[")
        end = val.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(val[start:end])
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass

    return None


def extract_json_object(session_data: dict,
                        messages: Optional[list] = None,
                        required_fields: Optional[set] = None) -> Optional[dict]:
    """Extract a JSON object from Devin session output.

    Precedence:
      1. ``session_data["structured_output"]`` when it is a dict and (when
         ``required_fields`` is set) contains every required key.
      2. Devin-authored messages scanned most-recent first. Content is read
         from ``message`` (v3) or ``content`` (legacy). Messages whose
         ``source`` is set and not ``"devin"`` are skipped.

    Returns the matching dict or ``None``.
    """
    required = required_fields or set()

    structured = session_data.get("structured_output")
    if structured and isinstance(structured, dict):
        if not required or required.issubset(structured.keys()):
            return structured

    candidates = messages
    if candidates is None:
        candidates = (session_data.get("messages")
                      or session_data.get("items")
                      or [])

    for message in reversed(candidates):
        if not isinstance(message, dict):
            continue
        source = (message.get("source") or "").lower()
        if source and source != "devin":
            continue
        content = message.get("message") or message.get("content") or ""
        if not content or not isinstance(content, str):
            continue
        parsed = _parse_object_from_text(content, required)
        if parsed is not None:
            return parsed

    return None


def _parse_object_from_text(text: str, required: set) -> Optional[dict]:
    """Try to extract a JSON object from a text blob, optionally gated by required keys."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict) and (not required or required.issubset(parsed.keys())):
            return parsed
    except (json.JSONDecodeError, AttributeError):
        pass

    start = stripped.find("{")
    end = stripped.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            parsed = json.loads(stripped[start:end])
            if isinstance(parsed, dict) and (not required or required.issubset(parsed.keys())):
                return parsed
        except json.JSONDecodeError:
            pass

    return None
