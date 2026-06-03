---
name: testing-webhook-app
description: End-to-end local testing for the FastAPI + Gradio GitHub-webhook -> Devin proxy (app.py). Use when verifying the /webhook flow, label rules, or the Gradio dashboard tabs.
---

# Testing the Devin Event Automator app

## What it is
`app.py` is a FastAPI service with a Gradio dashboard mounted at `/`. GitHub PR
`labeled` webhooks (validated via `X-Hub-Signature-256`) trigger Devin v3 sessions
in a `BackgroundTask`. State lives in `roles_config.json` / `sessions.json` under
`DATA_DIR`.

## Devin Secrets Needed
- For REAL end-to-end runs: `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `GITHUB_WEBHOOK_SECRET`, `NGROK_TOKEN` (optional `NGROK_DOMAIN`).
- For local TESTING none of the above are required — mock the Devin API instead (below). Creating real Devin sessions is wasteful, so prefer the mock.

## Recommended local test setup (no real creds)
1. Create venv + install: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
2. Run a local mock of the Devin v3 API on a spare port (e.g. 9009) implementing:
   - `POST /v3/organizations/{org}/sessions` -> returns `{"session_id":"devin-test123","url":"https://app.devin.ai/sessions/test123","status":"running"}`
   - `GET  /v3/organizations/{org}/sessions/{id}` -> returns `{"status":"running","status_detail":"working","url":...,"messages":[...]}`
3. Start the app pointing at the mock and leaving ngrok off:
   ```bash
   . .venv/bin/activate
   export GITHUB_WEBHOOK_SECRET=testsecret DEVIN_ORG_ID=org-test DEVIN_API_KEY=key-test \
          DEVIN_API_BASE=http://127.0.0.1:9009 DATA_DIR=/tmp/devin_test_data
   uvicorn app:app --host 0.0.0.0 --port 8000
   ```
   `DEVIN_API_BASE` is the override hook (app.py reads it); leaving `NGROK_TOKEN` unset skips the tunnel.

## Trigger the webhook
GitHub signs the raw body. Reproduce with:
```bash
BODY='{"action":"labeled","label":{"name":"c-review"},"pull_request":{"number":42,"html_url":"https://github.com/o/r/pull/42"}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac testsecret | sed 's/^.*= //')"
curl -s -w '\n%{http_code}\n' -X POST http://127.0.0.1:8000/webhook \
  -H 'Content-Type: application/json' -H 'X-GitHub-Event: pull_request' \
  -H "X-Hub-Signature-256: $SIG" -d "$BODY"
```
Expect `200 {"detail":"accepted",...}`. A bad signature must return `401`.

## UI checks (Gradio at http://localhost:8000/)
- **Rules Config**: add a label+prompt, click Add/Update. The status line confirms,
  and `roles_config.json` updates immediately. NOTE: the table may not re-render the
  new row until you switch tabs or click Refresh again — verify via the file on disk
  if the UI looks stale (this might be a Gradio render quirk, not a logic bug).
- **Session Dashboard**: after a webhook fires, the row should show a clickable
  `View Session` link (rendered because the column uses `datatype="markdown"`).
- **Live Agent Tracker**: click "Reload IDs", pick the `session_id`, click "Fetch Now";
  the textbox should show Status/Status detail/URL and a Messages/Events section.

## Gotchas
- The v3 GET endpoint expects a `devin-` prefixed id; `app.py` normalizes this, so
  stored ids like `devin-test123` work directly.
- `*.json` is gitignored, so test state never gets committed.
- Do not commit `test-plan.md` / `test-report.md` or the mock server — they are local test artifacts.
