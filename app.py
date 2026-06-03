"""GitHub Webhook -> Devin AI proxy service.

A FastAPI service that receives GitHub webhooks (pull request *and* issue
``labeled`` events), asynchronously triggers Devin AI sessions based on the
label, and exposes a Gradio admin panel (mounted at "/") to manage label rules,
track task status, and stream live Devin execution logs. The public webhook
receiver is exposed via an ngrok tunnel.

All configuration and state is read from environment variables and small JSON
files. No secrets are hardcoded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import gradio as gr
import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, Request
from fastapi.responses import JSONResponse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("devin-event-automator")

# ---------------------------------------------------------------------------
# Configuration (all sensitive values come from the environment)
# ---------------------------------------------------------------------------
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY", "")
DEVIN_ORG_ID = os.getenv("DEVIN_ORG_ID", "")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
NGROK_TOKEN = os.getenv("NGROK_TOKEN", "")
NGROK_DOMAIN = os.getenv("NGROK_DOMAIN", "")

DEVIN_API_BASE = os.getenv("DEVIN_API_BASE", "https://api.devin.ai")
APP_PORT = int(os.getenv("PORT", "8000"))

# JSON state lives in a configurable directory so it can be mounted as a volume.
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
ROLES_CONFIG_PATH = DATA_DIR / "roles_config.json"
SESSIONS_PATH = DATA_DIR / "sessions.json"

DEFAULT_ROLES_CONFIG: dict[str, str] = {
    "c-review": "Review PR {pr_url}, identify bugs, add comments.",
    "c-edit": (
        "Fix issue in PR {pr_url}, write tests, push to new branch, create PR "
        "to dev branch and merge. If there is no dev branch, create it first."
    ),
}

SESSION_COLUMNS = [
    "PR Number",
    "Label",
    "session_id",
    "devin_url",
    "Status",
    "Created Time",
]


# ---------------------------------------------------------------------------
# JSON persistence helpers (with defensive error handling)
# ---------------------------------------------------------------------------
def _read_json(path: Path, default: Any) -> Any:
    """Read JSON from ``path`` returning ``default`` on any error/missing file."""
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read %s (%s); using default.", path, exc)
        return default


def _write_json(path: Path, data: Any) -> None:
    """Atomically write ``data`` as JSON to ``path``."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        tmp.replace(path)
    except OSError as exc:
        logger.error("Failed to write %s (%s).", path, exc)


def load_roles_config() -> dict[str, str]:
    """Return the label -> prompt mapping, seeding defaults on first run."""
    if not ROLES_CONFIG_PATH.exists():
        _write_json(ROLES_CONFIG_PATH, DEFAULT_ROLES_CONFIG)
        return dict(DEFAULT_ROLES_CONFIG)
    data = _read_json(ROLES_CONFIG_PATH, {})
    if not isinstance(data, dict):
        logger.warning("roles_config.json malformed; resetting to defaults.")
        _write_json(ROLES_CONFIG_PATH, DEFAULT_ROLES_CONFIG)
        return dict(DEFAULT_ROLES_CONFIG)
    return data


def save_roles_config(config: dict[str, str]) -> None:
    _write_json(ROLES_CONFIG_PATH, config)


def load_sessions() -> list[dict[str, Any]]:
    data = _read_json(SESSIONS_PATH, [])
    if not isinstance(data, list):
        logger.warning("sessions.json malformed; resetting to empty list.")
        return []
    return data


def save_sessions(sessions: list[dict[str, Any]]) -> None:
    _write_json(SESSIONS_PATH, sessions)


def append_session(record: dict[str, Any]) -> None:
    sessions = load_sessions()
    sessions.insert(0, record)
    save_sessions(sessions)


# ---------------------------------------------------------------------------
# Devin API interaction module
# ---------------------------------------------------------------------------
def _devin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }


def _normalize_session_id(session_id: str) -> str:
    """The v3 GET endpoint expects a ``devin-`` prefixed id."""
    if session_id and not session_id.startswith("devin-"):
        return f"devin-{session_id}"
    return session_id


async def create_devin_session(prompt: str) -> dict[str, Any]:
    """Create a Devin session and return the parsed JSON response.

    Raises ``httpx.HTTPStatusError`` on a non-2xx response.
    """
    url = f"{DEVIN_API_BASE}/v3/organizations/{DEVIN_ORG_ID}/sessions"
    payload = {"prompt": prompt}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=_devin_headers(), json=payload)
        resp.raise_for_status()
        return resp.json()


async def get_devin_session(session_id: str) -> dict[str, Any]:
    """Fetch the current state of a Devin session."""
    sid = _normalize_session_id(session_id)
    url = f"{DEVIN_API_BASE}/v3/organizations/{DEVIN_ORG_ID}/sessions/{sid}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=_devin_headers())
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Webhook background processing
# ---------------------------------------------------------------------------
async def process_labeled_event(number: Any, target_url: str, label: str) -> None:
    """Background task: resolve the label rule and trigger a Devin session.

    Works for both pull requests and issues; ``target_url`` is the PR/issue URL
    substituted into the ``{pr_url}`` placeholder.
    """
    roles = load_roles_config()
    prompt_template = roles.get(label)
    if not prompt_template:
        logger.info("No rule configured for label '%s'; skipping.", label)
        return

    prompt = prompt_template.replace("{pr_url}", target_url)
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    record: dict[str, Any] = {
        "PR Number": number,
        "Label": label,
        "session_id": "",
        "devin_url": "",
        "Status": "pending",
        "Created Time": created_at,
    }

    try:
        data = await create_devin_session(prompt)
        record["session_id"] = data.get("session_id", "")
        record["devin_url"] = data.get("url", "")
        record["Status"] = data.get("status", "created")
        logger.info(
            "Created Devin session %s for #%s (label=%s).",
            record["session_id"],
            number,
            label,
        )
    except httpx.HTTPStatusError as exc:
        record["Status"] = f"error: HTTP {exc.response.status_code}"
        logger.error(
            "Devin API error for #%s: %s - %s",
            number,
            exc.response.status_code,
            exc.response.text[:500],
        )
    except (httpx.HTTPError, ValueError) as exc:
        record["Status"] = f"error: {exc}"
        logger.error("Failed to create Devin session for #%s: %s", number, exc)

    append_session(record)


def parse_webhook_payload(body: bytes) -> dict[str, Any] | None:
    """Parse a GitHub webhook body regardless of its content type.

    GitHub can deliver the payload either as raw JSON (``application/json``) or
    as ``application/x-www-form-urlencoded`` (the default), where the body is
    ``payload=<url-encoded JSON>``. We accept both so the receiver works no
    matter how the webhook content type is configured.
    """
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return None

    # Case 1: raw JSON body.
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Case 2: form-encoded body with a `payload` field.
    if "payload=" in text:
        values = parse_qs(text).get("payload")
        if values:
            try:
                data = json.loads(values[0])
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, ValueError):
                return None
    return None


def verify_signature(body: bytes, signature_header: str | None) -> bool:
    """Validate the GitHub ``X-Hub-Signature-256`` header.

    If no webhook secret is configured we skip verification (useful for local
    testing) but log a warning.
    """
    if not GITHUB_WEBHOOK_SECRET:
        logger.warning("GITHUB_WEBHOOK_SECRET not set; skipping signature check.")
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    received = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


# ---------------------------------------------------------------------------
# ngrok lifecycle management
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start an ngrok tunnel on startup and tear it down on shutdown."""
    tunnel = None
    if NGROK_TOKEN:
        try:
            from pyngrok import conf, ngrok

            conf.get_default().auth_token = NGROK_TOKEN
            connect_kwargs: dict[str, Any] = {"addr": APP_PORT, "proto": "http"}
            if NGROK_DOMAIN:
                connect_kwargs["domain"] = NGROK_DOMAIN
            tunnel = ngrok.connect(**connect_kwargs)
            public_url = tunnel.public_url
            logger.info("ngrok tunnel established: %s", public_url)
            logger.info("GitHub webhook URL: %s/webhook", public_url)
        except Exception as exc:  # noqa: BLE001 - tunnel is best-effort
            logger.error("Failed to start ngrok tunnel: %s", exc)
    else:
        logger.warning("NGROK_TOKEN not set; skipping ngrok tunnel startup.")

    # Ensure config exists at startup.
    load_roles_config()

    try:
        yield
    finally:
        if tunnel is not None:
            try:
                from pyngrok import ngrok

                ngrok.disconnect(tunnel.public_url)
                ngrok.kill()
                logger.info("ngrok tunnel closed.")
            except Exception as exc:  # noqa: BLE001
                logger.error("Error closing ngrok tunnel: %s", exc)


app = FastAPI(title="Devin Event Automator", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> JSONResponse:
    """Receive GitHub webhooks and trigger Devin sessions asynchronously."""
    body = await request.body()

    if not verify_signature(body, x_hub_signature_256):
        logger.warning("Invalid webhook signature; rejecting request.")
        return JSONResponse(status_code=401, content={"detail": "invalid signature"})

    payload = parse_webhook_payload(body)
    if payload is None:
        logger.warning("Could not parse webhook body as JSON or form-encoded payload.")
        return JSONResponse(status_code=400, content={"detail": "invalid payload"})

    action = payload.get("action")
    logger.info("Webhook received: event=%s action=%s", x_github_event, action)

    # Handle "labeled" events for both pull requests and issues.
    supported_events = {"pull_request", "issues"}
    if x_github_event and x_github_event not in supported_events:
        return JSONResponse(content={"detail": f"ignored event: {x_github_event}"})

    if action != "labeled":
        return JSONResponse(content={"detail": "ignored: action is not 'labeled'"})

    # The labeled object lives under "pull_request" (PR events) or "issue"
    # (issue events). Support both so the same rules apply to either.
    target = payload.get("pull_request") or payload.get("issue") or {}
    target_url = target.get("html_url", "")
    number = target.get("number")
    label = (payload.get("label") or {}).get("name", "")

    if not target_url or not label:
        return JSONResponse(
            status_code=400,
            content={"detail": "missing issue/pull_request url or label"},
        )

    # Offload the heavy lifting so we can return 200 immediately.
    background_tasks.add_task(process_labeled_event, number, target_url, label)
    logger.info("Queued Devin trigger for #%s (label=%s).", number, label)
    return JSONResponse(
        content={"detail": "accepted", "number": number, "label": label}
    )


# ---------------------------------------------------------------------------
# Gradio dashboard
# ---------------------------------------------------------------------------
def _roles_to_rows() -> list[list[str]]:
    return [[label, prompt] for label, prompt in load_roles_config().items()]


def refresh_rules() -> list[list[str]]:
    return _roles_to_rows()


def upsert_rule(label: str, prompt: str) -> tuple[list[list[str]], str]:
    label = (label or "").strip()
    prompt = (prompt or "").strip()
    if not label or not prompt:
        return _roles_to_rows(), "Both label and prompt are required."
    config = load_roles_config()
    action = "Updated" if label in config else "Added"
    config[label] = prompt
    save_roles_config(config)
    return _roles_to_rows(), f"{action} rule for label '{label}'."


def delete_rule(label: str) -> tuple[list[list[str]], str]:
    label = (label or "").strip()
    config = load_roles_config()
    if label in config:
        del config[label]
        save_roles_config(config)
        return _roles_to_rows(), f"Deleted rule for label '{label}'."
    return _roles_to_rows(), f"No rule found for label '{label}'."


def _sessions_to_rows() -> list[list[Any]]:
    rows: list[list[Any]] = []
    for s in load_sessions():
        url = s.get("devin_url", "")
        url_md = f"[View Session]({url})" if url else ""
        rows.append(
            [
                s.get("PR Number", ""),
                s.get("Label", ""),
                s.get("session_id", ""),
                url_md,
                s.get("Status", ""),
                s.get("Created Time", ""),
            ]
        )
    return rows


def refresh_sessions() -> list[list[Any]]:
    return _sessions_to_rows()


def list_session_ids() -> list[str]:
    return [s.get("session_id", "") for s in load_sessions() if s.get("session_id")]


def _format_session_log(data: dict[str, Any]) -> str:
    """Render a human-readable view of a Devin session's status and logs."""
    lines: list[str] = []
    lines.append(f"Status: {data.get('status', 'unknown')}")
    if data.get("status_detail"):
        lines.append(f"Status detail: {data['status_detail']}")
    if data.get("url"):
        lines.append(f"URL: {data['url']}")
    if data.get("title"):
        lines.append(f"Title: {data['title']}")
    if "acus_consumed" in data:
        lines.append(f"ACUs consumed: {data['acus_consumed']}")

    # Pull requests, if any.
    prs = data.get("pull_requests") or []
    if prs:
        lines.append("\nPull Requests:")
        for pr in prs:
            if isinstance(pr, dict):
                lines.append(f"  - {pr.get('url', pr)}")
            else:
                lines.append(f"  - {pr}")

    # Messages / events for live tracking (defensive: keys vary by API).
    events = data.get("messages") or data.get("events") or []
    if events:
        lines.append("\nMessages / Events:")
        for ev in events:
            if isinstance(ev, dict):
                ts = ev.get("timestamp") or ev.get("created_at") or ""
                kind = ev.get("type") or ev.get("role") or ev.get("kind") or "event"
                text = (
                    ev.get("message")
                    or ev.get("content")
                    or ev.get("text")
                    or json.dumps(ev, ensure_ascii=False)
                )
                prefix = f"[{ts}] " if ts else ""
                lines.append(f"  {prefix}{kind}: {text}")
            else:
                lines.append(f"  {ev}")

    if data.get("structured_output"):
        lines.append("\nStructured Output:")
        lines.append(json.dumps(data["structured_output"], indent=2, ensure_ascii=False))

    return "\n".join(lines)


async def fetch_session_log(session_id: str) -> str:
    if not session_id:
        return "Select a session_id to view its live progress."
    try:
        data = await get_devin_session(session_id)
    except httpx.HTTPStatusError as exc:
        return f"Error fetching session: HTTP {exc.response.status_code}\n{exc.response.text[:500]}"
    except (httpx.HTTPError, ValueError) as exc:
        return f"Error fetching session: {exc}"
    return _format_session_log(data)


def build_gradio_app() -> gr.Blocks:
    with gr.Blocks(title="Devin Event Automator") as demo:
        gr.Markdown("# Devin Event Automator\nGitHub PR label -> Devin AI automation control panel.")

        # ---- Tab 1: Rules Config ----
        with gr.Tab("Rules Config"):
            gr.Markdown("Map GitHub labels to Devin prompt rules. Use `{pr_url}` as a placeholder.")
            rules_table = gr.Dataframe(
                value=_roles_to_rows(),
                headers=["Label", "Prompt"],
                datatype=["str", "str"],
                interactive=False,
                wrap=True,
                label="Current Rules",
            )
            with gr.Row():
                label_in = gr.Textbox(label="Label", placeholder="e.g. c-review")
                prompt_in = gr.Textbox(label="Prompt", placeholder="Review PR {pr_url} ...", lines=2)
            with gr.Row():
                save_btn = gr.Button("Add / Update", variant="primary")
                delete_btn = gr.Button("Delete")
                refresh_rules_btn = gr.Button("Refresh")
            rules_status = gr.Markdown()

            save_btn.click(upsert_rule, [label_in, prompt_in], [rules_table, rules_status])
            delete_btn.click(delete_rule, [label_in], [rules_table, rules_status])
            refresh_rules_btn.click(refresh_rules, None, rules_table)

        # ---- Tab 2: Session Dashboard ----
        with gr.Tab("Session Dashboard"):
            gr.Markdown("Historical Devin tasks triggered by webhooks (auto-refreshes).")
            sessions_table = gr.Dataframe(
                value=_sessions_to_rows(),
                headers=SESSION_COLUMNS,
                datatype=["str", "str", "str", "markdown", "str", "str"],
                interactive=False,
                wrap=True,
                label="Sessions",
            )
            refresh_sessions_btn = gr.Button("Refresh Now")
            refresh_sessions_btn.click(refresh_sessions, None, sessions_table)
            # Periodic auto-refresh.
            timer = gr.Timer(10.0)
            timer.tick(refresh_sessions, None, sessions_table)

        # ---- Tab 3: Live Agent Tracker ----
        with gr.Tab("Live Agent Tracker"):
            gr.Markdown("Select a session to stream its live status and logs from Devin.")
            with gr.Row():
                session_dd = gr.Dropdown(
                    choices=list_session_ids(),
                    label="session_id",
                    interactive=True,
                )
                reload_ids_btn = gr.Button("Reload IDs")
            log_box = gr.Textbox(label="Live Progress / Logs", lines=20, interactive=False)
            fetch_btn = gr.Button("Fetch Now", variant="primary")

            reload_ids_btn.click(lambda: gr.update(choices=list_session_ids()), None, session_dd)
            fetch_btn.click(fetch_session_log, session_dd, log_box)
            log_timer = gr.Timer(8.0)
            log_timer.tick(fetch_session_log, session_dd, log_box)

    return demo


# Mount the Gradio dashboard at the application root.
gradio_app = build_gradio_app()
app = gr.mount_gradio_app(app, gradio_app, path="/")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=APP_PORT, reload=False)
