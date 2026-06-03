# devin-event-automator

An event-driven automation service that turns **GitHub Pull Request labels** into
**Devin AI** sessions. It is built with **FastAPI + Gradio**, exposes the webhook
receiver to the public internet via **ngrok**, and ships with a Gradio admin panel
to manage rules, track triggered tasks, and stream live Devin logs.

## How it works

```
GitHub PR/Issue labeled  ──▶  POST /webhook (FastAPI)
                         │  1. verify X-Hub-Signature-256
                         │  2. return 200 OK immediately
                         ▼
                   BackgroundTasks
                         │  3. look up label -> prompt rule (roles_config.json)
                         │  4. replace {pr_url} placeholder
                         │  5. POST Devin v3 create-session API
                         ▼
                   sessions.json  ──▶  Gradio dashboard (mounted at "/")
```

The webhook handler returns `200 OK` immediately and performs the Devin API call
inside `BackgroundTasks`, so GitHub never times out.

## Features

- **FastAPI `/webhook`** — validates `X-Hub-Signature-256`, acts on
  `pull_request` **and** `issues` events with `action == "labeled"` (the
  `{pr_url}` placeholder resolves to the PR or issue URL), and processes work
  asynchronously.
- **JSON persistence** — `roles_config.json` (label → prompt rules) and
  `sessions.json` (triggered task history). Both are created automatically and are
  read/written defensively.
- **Devin v3 API integration** — creates sessions and fetches status/logs via `httpx`.
- **ngrok lifecycle** — the public tunnel is opened on startup and closed on
  shutdown using an `@asynccontextmanager` lifespan + `pyngrok`.
- **Gradio dashboard** (mounted at `/`) with three tabs:
  1. **Rules Config** — add/update/delete label → prompt rules.
  2. **Session Dashboard** — auto-refreshing `gr.Dataframe` of all triggered tasks,
     with clickable `[View Session](url)` markdown links.
  3. **Live Agent Tracker** — pick a `session_id` and watch its live status/logs.

## Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
| --- | --- |
| `DEVIN_API_KEY` | Devin service-user API key (`Bearer` token). |
| `DEVIN_ORG_ID` | Your Devin organization id (e.g. `org-...`). |
| `GITHUB_WEBHOOK_SECRET` | Secret configured on the GitHub webhook; used to validate signatures. |
| `NGROK_TOKEN` | ngrok auth token. |
| `NGROK_DOMAIN` | Optional reserved/custom ngrok domain (blank → random URL). |

> No secrets are hardcoded — everything is read from the environment.

## One-click Docker startup

```bash
# 1. configure your secrets
cp .env.example .env && edit .env

# 2. build & run
docker compose up --build
```

This will:

- build the image (Python 3.10-slim with the ngrok binary pre-installed),
- mount `./data` so `roles_config.json` / `sessions.json` survive restarts,
- expose the app on <http://localhost:8000> (Gradio panel at `/`),
- open an ngrok tunnel and log the public URL.

After startup, look in the container logs for a line like:

```
ngrok tunnel established: https://<your-subdomain>.ngrok-free.app
GitHub webhook URL: https://<your-subdomain>.ngrok-free.app/webhook
```

### Run locally without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit
python app.py          # or: uvicorn app:app --host 0.0.0.0 --port 8000
```

## Configuring the GitHub webhook

1. In your GitHub repository go to **Settings → Webhooks → Add webhook**.
2. **Payload URL**: the ngrok URL from the logs, with `/webhook` appended, e.g.
   `https://<your-subdomain>.ngrok-free.app/webhook`.
3. **Content type**: `application/json` is recommended, but the receiver also
   accepts GitHub's default `application/x-www-form-urlencoded`.
4. **Secret**: the same value as `GITHUB_WEBHOOK_SECRET` in your `.env`.
5. **Which events?** → *Let me select individual events* → check **Pull requests**
   and/or **Issues** (both `labeled` events are supported).
6. Save. GitHub sends a `ping`; subsequent `pull_request` / `issues` `labeled`
   events will trigger Devin according to your configured rules.

### Default rules

| Label | Prompt |
| --- | --- |
| `c-review` | `Review PR {pr_url}, identify bugs, add comments.` |
| `c-edit` | `Fix issue in PR {pr_url}, write tests, push to new branch, create PR to dev branch and merge. If there is no dev branch, create it first.` |

Edit these (or add your own) from the **Rules Config** tab. `{pr_url}` is replaced
with the real PR link at trigger time.

## Project layout

| File | Purpose |
| --- | --- |
| `app.py` | FastAPI service, webhook route, Devin API calls, ngrok lifespan, Gradio mount, background tasks. |
| `requirements.txt` | Python dependencies. |
| `Dockerfile` | Python 3.10-slim image with ngrok pre-installed. |
| `docker-compose.yml` | Ports, env injection, and a volume for JSON state. |
| `.env.example` | Template for required environment variables. |
| `roles_config.json` | (generated) label → prompt rules. |
| `sessions.json` | (generated) triggered task history. |
