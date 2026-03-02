"""Schedule subcommands — thin HTTP client to the API server."""

import json
import os
import sys
from typing import Optional

import click

from ..utils.constants import MODEL_MAP


def _get_api_url() -> str:
    """Build the base API URL from environment."""
    port = os.environ.get("API_SERVER_PORT", "8080")
    return f"http://127.0.0.1:{port}"


def _get_auth_header() -> dict:
    """Build the authorization header from environment."""
    secret = os.environ.get("WEBHOOK_API_SECRET", "")
    if not secret:
        click.echo(
            "Error: WEBHOOK_API_SECRET not set. "
            "Set it in your .env file or environment.",
            err=True,
        )
        sys.exit(1)
    return {"Authorization": f"Bearer {secret}"}


def _request(
    method: str,
    path: str,
    json_data: Optional[dict] = None,
) -> dict:
    """Make an HTTP request to the API server.

    Uses urllib to avoid adding requests as a dependency.
    """
    import urllib.error
    import urllib.request

    url = f"{_get_api_url()}{path}"
    headers = _get_auth_header()
    headers["Content-Type"] = "application/json"

    body = json.dumps(json_data).encode() if json_data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        if "Connection refused" in str(e):
            click.echo(
                "Error: Cannot connect to the bot service. "
                "Is it running? Start it with: claude-telegram-bot start",
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        try:
            detail = json.loads(body_text).get("detail", body_text)
        except json.JSONDecodeError:
            detail = body_text
        click.echo(f"Error ({e.code}): {detail}", err=True)
        sys.exit(1)


@click.group()
def schedule() -> None:
    """Manage scheduled jobs."""
    pass


@schedule.command(name="add")
@click.option("--name", required=True, help="Human-readable job name.")
@click.option("--cron", default=None, help='Cron expression (e.g. "0 9 * * 1-5").')
@click.option(
    "--at",
    "run_date",
    default=None,
    help='One-time run date in ISO 8601 (e.g. "2026-03-01T09:00:00").',
)
@click.option("--prompt", required=True, help="Prompt to send to Claude.")
@click.option(
    "--chat-id",
    multiple=True,
    type=int,
    help="Target Telegram chat ID (repeatable).",
)
@click.option("--working-dir", default=None, help="Working directory for Claude.")
@click.option("--skill", default=None, help="Skill to invoke.")
@click.option(
    "--session-mode",
    type=click.Choice(["isolated", "resume"]),
    default="isolated",
    help="Session mode: isolated (fresh) or resume (continue user session).",
)
@click.option(
    "--model",
    type=click.Choice(list(MODEL_MAP.keys())),
    default="sonnet",
    help="Claude model to use (default: sonnet).",
)
def add_job(
    name: str,
    cron: Optional[str],
    run_date: Optional[str],
    prompt: str,
    chat_id: tuple,
    working_dir: Optional[str],
    skill: Optional[str],
    session_mode: str,
    model: str,
) -> None:
    """Add a new scheduled job.

    Exactly one of --cron or --at is required.
    """
    if cron and run_date:
        click.echo("Error: --cron and --at are mutually exclusive.", err=True)
        sys.exit(1)
    if not cron and not run_date:
        click.echo("Error: one of --cron or --at is required.", err=True)
        sys.exit(1)

    trigger_type = "cron" if cron else "date"

    payload: dict = {
        "job_name": name,
        "cron_expression": cron or "",
        "prompt": prompt,
        "target_chat_ids": list(chat_id),
        "session_mode": session_mode,
        "trigger_type": trigger_type,
        "model": model,
    }
    if run_date:
        payload["run_date"] = run_date
    if working_dir:
        payload["working_directory"] = working_dir
    if skill:
        payload["skill_name"] = skill

    result = _request("POST", "/api/scheduler/jobs", json_data=payload)
    click.echo(f"Job created: {result['job_id']}")


@schedule.command(name="update")
@click.argument("job_id")
@click.option("--name", default=None, help="New human-readable job name.")
@click.option("--cron", default=None, help='New cron expression (e.g. "0 9 * * 1-5").')
@click.option(
    "--at",
    "run_date",
    default=None,
    help='New one-time run date in ISO 8601 (e.g. "2026-03-01T09:00:00").',
)
@click.option("--prompt", default=None, help="New prompt to send to Claude.")
@click.option(
    "--chat-id",
    multiple=True,
    type=int,
    help="Replace target Telegram chat IDs (repeatable).",
)
@click.option("--working-dir", default=None, help="New working directory for Claude.")
@click.option(
    "--session-mode",
    type=click.Choice(["isolated", "resume"]),
    default=None,
    help="New session mode.",
)
@click.option(
    "--model",
    type=click.Choice(list(MODEL_MAP.keys())),
    default=None,
    help="New Claude model.",
)
@click.option("--active/--inactive", default=None, help="Enable or disable the job.")
def update_job(
    job_id: str,
    name: Optional[str],
    cron: Optional[str],
    run_date: Optional[str],
    prompt: Optional[str],
    chat_id: tuple,
    working_dir: Optional[str],
    session_mode: Optional[str],
    model: Optional[str],
    active: Optional[bool],
) -> None:
    """Update an existing scheduled job.

    Only the options explicitly provided are changed.
    """
    payload: dict = {}
    if name is not None:
        payload["job_name"] = name
    if cron is not None:
        payload["cron_expression"] = cron
    if run_date is not None:
        payload["run_date"] = run_date
    if prompt is not None:
        payload["prompt"] = prompt
    if chat_id:
        payload["target_chat_ids"] = list(chat_id)
    if working_dir is not None:
        payload["working_directory"] = working_dir
    if session_mode is not None:
        payload["session_mode"] = session_mode
    if model is not None:
        payload["model"] = model
    if active is not None:
        payload["is_active"] = active

    if not payload:
        click.echo("No fields to update. Provide at least one option.", err=True)
        sys.exit(1)

    _request("PATCH", f"/api/scheduler/jobs/{job_id}", json_data=payload)
    click.echo(f"Job {job_id} updated.")


@schedule.command(name="list")
def list_jobs() -> None:
    """List all active scheduled jobs."""
    result = _request("GET", "/api/scheduler/jobs")
    jobs = result.get("jobs", [])

    if not jobs:
        click.echo("No scheduled jobs.")
        return

    for job in jobs:
        mode = job.get("session_mode", "isolated")
        ttype = job.get("trigger_type", "cron")
        if ttype == "date":
            schedule_str = f"at {job.get('run_date', '?')}"
        else:
            schedule_str = job.get("cron_expression", "?")
        click.echo(
            f"  {job['job_id']}  "
            f"{job['job_name']:<25s}  "
            f"{schedule_str:<25s}  "
            f"mode={mode}"
        )


@schedule.command(name="remove")
@click.argument("job_id")
def remove_job(job_id: str) -> None:
    """Remove a scheduled job by ID."""
    _request("DELETE", f"/api/scheduler/jobs/{job_id}")
    click.echo(f"Job {job_id} removed.")


@schedule.command(name="trigger")
@click.argument("job_id")
def trigger_job(job_id: str) -> None:
    """Manually trigger a job immediately."""
    _request("POST", f"/api/scheduler/jobs/{job_id}/trigger")
    click.echo(f"Job {job_id} triggered.")


@schedule.command(name="history")
@click.argument("job_id")
def job_history(job_id: str) -> None:
    """Show execution history for a job."""
    result = _request("GET", f"/api/scheduler/jobs/{job_id}/history")
    runs = result.get("runs", [])

    if not runs:
        click.echo(f"No execution history for job {job_id}.")
        return

    for run in runs:
        status_str = "OK" if run.get("success") else "FAIL"
        cost = run.get("cost", 0)
        fired = run.get("fired_at", "?")
        error = run.get("error_message")
        line = f"  {fired}  {status_str}  ${cost:.4f}"
        if error:
            line += f"  error={error}"
        click.echo(line)
