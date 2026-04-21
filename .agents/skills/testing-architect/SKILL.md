# Testing the Architect flow (backlog-autopilot)

How to verify the Streamlit Architect stage end-to-end against the real Devin v3 API.

## Devin Secrets Needed
- `DEVIN_API_KEY` — plain API key for the Devin v3 API (org-scoped).
- `DEVIN_ORG_ID` — looks like `org-xxxxxxxx...`. Required so sessions created by the architect hit the correct org.

Both should be org-scoped so future sessions pick them up automatically. Export them into the Streamlit process env before launching.

## Local setup (one time per session)
```bash
cd ~/repos/backlog-autopilot
pip install -q -r requirements.txt
export DEVIN_API_KEY="$DEVIN_API_KEY"
export DEVIN_ORG_ID="$DEVIN_ORG_ID"
```

## Running Streamlit headless
```bash
cd ~/repos/backlog-autopilot
nohup streamlit run app.py \
  --server.headless true \
  --server.port 8501 \
  --server.address 127.0.0.1 \
  --browser.gatherUsageStats false \
  > /tmp/streamlit.log 2>&1 &
sleep 3
curl -fsS http://127.0.0.1:8501 >/dev/null && echo OK
```
Open http://127.0.0.1:8501 in Chrome.

## Starting state for tests
`pipeline_store.json` is committed empty. If you need to reset between runs:
```bash
git checkout pipeline_store.json
```
After testing, always revert it back with the same command so no synthetic data is committed.

## Picking a deterministic test issue
The UI defaults to `planner_mode="rule"`, so `load_and_plan()` calls `plan_issues(ingest_issues(fetch_issues()))` from `issues.json`. To find the top-ranked recommended issue without opening the UI:
```bash
python3 - <<'PY'
from github_client import fetch_issues
from ingest import ingest_issues
from planner import plan_issues, DEFAULT_WEIGHTS
issues = plan_issues(ingest_issues(fetch_issues()), weights=DEFAULT_WEIGHTS)
for i in issues:
    if i['planner_score']['recommended']:
        print(i['id'], i['planner_score']['priority_rank'], i['title'][:70])
PY
```
With the default `issues.json` and weights, issue `#20 "Webhook retry logic does not respect exponential backoff"` is rank 1. Use the per-issue **Run Architect** button (inside the expanded row) rather than **Run Architect on All** — there are 4 recommended issues and the "Run on All" button would burn 4× Devin sessions (~20 min, 4× API cost) when a single session is enough to exercise the same code path.

## What to assert (Architect success flow)
The single signal that drives both the row tag and the panel choice is `architect_plan["architect_status"]`. A successful run must write `"complete"`; failure writes `"error"` which the UI renders as `· Architect Failed`.

Observable UI assertions:
1. **Row header tag** (from `app.py` lines 346-354) contains `· High/Medium/Low Confidence (N%)`. `· Architect Failed` is the reverted-code failure signal.
2. **Green/yellow `Architect Confidence: N/100 — label` box** (from `app.py` lines 409-421) is present. The failure signal is `st.warning("Architect failed: …")` (lines 396-399).
3. **Root Cause / Affected Files / Task Breakdown / Risks** all populated from the parsed JSON plan.

Backend evidence:
```bash
python3 -c "
import store
p = store.get_architect_plan(<ISSUE_ID>)
for k in ['architect_status','confidence_score','error','session_url']:
    print(k, '=', p.get(k))
print('task_breakdown length:', len(p.get('task_breakdown') or []))
"
```
Expected after a successful run: `architect_status='complete'`, `confidence_score` 0-100 int, `error=None`, `session_url` is a real `https://app.devin.ai/sessions/...` URL.

## Timing
One Devin architect session takes ~4–6 minutes against the real API. The Streamlit button click is **synchronous** — the spinner blocks until Devin returns, then the page auto-reruns. Don't try to race a refresh; just wait.

## Known gotcha — stdout buffering in /tmp/streamlit.log
The `print("[architect] …")` defensive-logging lines in `architect.py` are often buffered by Python when launched via `streamlit run` and will NOT appear in `/tmp/streamlit.log` until the process exits. The Streamlit framework's own warnings DO appear. If you need to assert on the architect log lines, either:
- Launch Streamlit with `PYTHONUNBUFFERED=1 streamlit run app.py …` (preferred for tests), or
- Verify the code path ran via the store state instead — a `confidence_score` integer + `architect_status="complete"` is only producible if `_extract_architect_json()` returned a non-None plan, which (since `structured_output_schema` is not set on session create) is only possible via the v3 `/sessions/{id}/messages` fetcher.

## Reverted-code failure mode (for designing adversarial tests)
Pre-fix, `_extract_architect_json()` read inline `session_data["messages"]` / `["items"]` with a `content` field — a shape the v3 API no longer returns. Any session that reached `status=running / status_detail=waiting_for_user` (the v3 "work product ready" state) was therefore mapped to `architect_status="error"`. To write an adversarial test, pick any assertion whose value is only produced when the extractor succeeds (e.g. a specific confidence score, a populated task_breakdown, the presence of the green confidence box).

## Useful UI code paths
- `app.py` 312-332 — "Run Architect on All" button.
- `app.py` 345-354 — confidence tag in the row header.
- `app.py` 396-421 — error warning vs. confidence panel.
- `app.py` 492-503 — per-issue "Run Architect" button (cheaper for testing).
- `architect.py` — `_fetch_messages`, `_extract_architect_json`, `_WORK_PRODUCT_READY_DETAILS`, `architect_issue`.
