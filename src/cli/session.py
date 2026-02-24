"""Session subcommands — inspect, list, send, and view responses.

``list``, ``send``, and ``response`` are thin HTTP clients to the API server.
``inspect`` runs locally, parsing JSONL session logs from ~/.claude/projects/.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

# ---------------------------------------------------------------------------
# Shared HTTP helpers (same pattern as schedule.py)
# ---------------------------------------------------------------------------


def _get_api_url() -> str:
    port = os.environ.get("API_SERVER_PORT", "8080")
    return f"http://127.0.0.1:{port}"


def _get_auth_header() -> dict:
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
    params: Optional[dict] = None,
) -> dict:
    import urllib.error
    import urllib.parse
    import urllib.request

    url = f"{_get_api_url()}{path}"
    if params:
        # Filter out None values
        filtered = {k: v for k, v in params.items() if v is not None}
        if filtered:
            url += "?" + urllib.parse.urlencode(filtered)

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


# ---------------------------------------------------------------------------
# JSONL parsing helpers for ``inspect``
# ---------------------------------------------------------------------------


def _find_session_jsonl(session_id: str) -> Optional[Path]:
    """Locate the JSONL transcript file for a session ID."""
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return None
    for subdir in claude_dir.iterdir():
        if subdir.is_dir():
            candidate = subdir / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate
    return None


def _parse_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read all entries from a JSONL file."""
    entries: List[Dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _entry_timestamp(entry: Dict[str, Any]) -> str:
    """Extract a display timestamp from an entry, or return '??:??:??'."""
    # Entries may have a top-level 'timestamp' or nested in metadata
    ts = entry.get("timestamp")
    if ts:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return dt.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            pass
    return "??:??:??"


def _entry_iso_timestamp(entry: Dict[str, Any]) -> Optional[str]:
    """Return the raw ISO timestamp string if present."""
    return entry.get("timestamp")


def _summarize_tool_input(tool_input: Any, max_len: int = 120) -> str:
    """Create a short summary of tool input."""
    if isinstance(tool_input, str):
        return tool_input[:max_len]
    if isinstance(tool_input, dict):
        # For common tools, show the most relevant field
        if "command" in tool_input:
            return tool_input["command"][:max_len]
        if "file_path" in tool_input:
            summary = tool_input["file_path"]
            if "old_string" in tool_input:
                summary += " (edit)"
            return summary[:max_len]
        if "pattern" in tool_input:
            return f"pattern={tool_input['pattern']}"[:max_len]
        if "query" in tool_input:
            return f"query={tool_input['query']}"[:max_len]
        # Fallback: dump keys
        return str(list(tool_input.keys()))[:max_len]
    return str(tool_input)[:max_len]


def _render_entry(
    entry: Dict[str, Any],
    verbose: bool = False,
) -> Optional[str]:
    """Render a single JSONL entry as a human-readable line.

    Returns None for entries that should be skipped.
    """
    ts = _entry_timestamp(entry)
    role = entry.get("role") or entry.get("type", "")

    # --- user messages ---
    if role == "user":
        content = entry.get("content")
        if isinstance(content, str):
            return f"[{ts}] USER: {content}"
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_result":
                        tid = str(block.get("tool_use_id", ""))[:8]
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            texts = [
                                b.get("text", "")
                                for b in result_content
                                if isinstance(b, dict) and b.get("type") == "text"
                            ]
                            result_content = "\n".join(texts)
                        return f"[{ts}] RESULT ({tid}): {str(result_content)[:200]}"
                    if block.get("type") == "text":
                        return f"[{ts}] USER: {block.get('text', '')}"
        return f"[{ts}] USER: (complex content)"

    # --- assistant messages ---
    if role == "assistant":
        content = entry.get("content")
        lines = []
        if isinstance(content, str):
            lines.append(f"[{ts}] ASSISTANT: {content}")
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    lines.append(f"[{ts}] ASSISTANT: {block.get('text', '')}")
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    inp = _summarize_tool_input(block.get("input", {}))
                    lines.append(f"[{ts}] TOOL {name}: {inp}")
                elif btype == "thinking":
                    text = block.get("thinking", "") or block.get("text", "")
                    first_line = text.split("\n")[0] if text else ""
                    if verbose:
                        lines.append(f"[{ts}] THINKING: {text}")
                    else:
                        lines.append(f"[{ts}] THINKING: {first_line}...")
        return "\n".join(lines) if lines else None

    # --- system / metadata ---
    if role == "system":
        # Look for turn duration info
        content = entry.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "duration" in str(block):
                    return f"[{ts}] --- system: {json.dumps(block)[:200]} ---"
        return f"[{ts}] --- system ---" if verbose else None

    # --- progress / file-history-snapshot ---
    if role in ("progress", "file-history-snapshot"):
        return f"[{ts}] ({role})" if verbose else None

    # Unknown type — show in verbose mode
    if verbose:
        return f"[{ts}] ({role}): {json.dumps(entry)[:200]}"
    return None


def _is_tool_use(entry: Dict[str, Any], tool_name: Optional[str] = None) -> bool:
    """Check if entry contains a tool_use block, optionally for a specific tool."""
    if entry.get("role") != "assistant":
        return False
    content = entry.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            if tool_name is None or block.get("name") == tool_name:
                return True
    return False


def _is_tool_result(entry: Dict[str, Any]) -> bool:
    """Check if entry is a tool_result."""
    if entry.get("role") != "user":
        return False
    content = entry.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return True
    return False


def _get_tool_use_ids(entry: Dict[str, Any]) -> List[str]:
    """Extract tool_use IDs from an assistant entry."""
    ids = []
    content = entry.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tid = block.get("id")
                if tid:
                    ids.append(tid)
    return ids


def _get_tool_result_id(entry: Dict[str, Any]) -> Optional[str]:
    """Extract tool_use_id from a tool_result entry."""
    content = entry.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return block.get("tool_use_id")
    return None


def _render_bash_pair(
    tool_entry: Dict[str, Any],
    result_entry: Optional[Dict[str, Any]],
) -> str:
    """Render a Bash tool_use + tool_result pair."""
    ts = _entry_timestamp(tool_entry)
    content = tool_entry.get("content", [])
    command = ""
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if block.get("name") == "Bash":
                    command = block.get("input", {}).get("command", "")
                    break

    lines = [f"[{ts}] $ {command}"]

    if result_entry:
        rc = result_entry.get("content")
        if isinstance(rc, list):
            for block in rc:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out = block.get("content", "")
                    if isinstance(out, list):
                        texts = [
                            b.get("text", "")
                            for b in out
                            if isinstance(b, dict) and b.get("type") == "text"
                        ]
                        out = "\n".join(texts)
                    if out:
                        lines.append(str(out))
        elif isinstance(rc, str):
            lines.append(rc)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------


@click.group()
def session() -> None:
    """Observe and interact with Claude sessions."""
    pass


@session.command(name="list")
@click.option("--dir", "directory", default=None, help="Filter by working directory.")
@click.option("--user", "user_id", type=int, default=None, help="Filter by user ID.")
@click.option(
    "--all", "show_all", is_flag=True, help="Include expired/inactive sessions."
)
def list_sessions(
    directory: Optional[str],
    user_id: Optional[int],
    show_all: bool,
) -> None:
    """List known sessions.

    Examples:

        claude-telegram-bot session list

        claude-telegram-bot session list --dir /home/user/project

        claude-telegram-bot session list --all
    """
    params: dict = {}
    if directory:
        params["dir"] = directory
    if user_id is not None:
        params["user_id"] = str(user_id)
    if show_all:
        params["all"] = "true"

    result = _request("GET", "/api/sessions", params=params)
    sessions = result.get("sessions", [])

    if not sessions:
        click.echo("No sessions found.")
        return

    # Header
    click.echo(
        f"  {'SESSION':<12s}  {'USER':>8s}  {'DIRECTORY':<35s}  "
        f"{'LAST USED':<20s}  {'TURNS':>5s}  {'COST':>8s}  T"
    )
    click.echo("  " + "-" * 100)

    for s in sessions:
        sid = s["session_id"][:8]
        user = str(s["user_id"])
        dirpath = s["project_path"]
        if len(dirpath) > 35:
            dirpath = "..." + dirpath[-32:]
        last_used = s["last_used"][:19].replace("T", " ")
        turns = str(s.get("total_turns", 0))
        cost = f"${s.get('total_cost', 0):.4f}"
        transcript = "*" if s.get("has_transcript") else " "
        expired = " (expired)" if s.get("expired") else ""

        click.echo(
            f"  {sid:<12s}  {user:>8s}  {dirpath:<35s}  "
            f"{last_used:<20s}  {turns:>5s}  {cost:>8s}  {transcript}{expired}"
        )


@session.command(name="inspect")
@click.argument("session_id")
@click.option("--tools-only", is_flag=True, help="Show only tool_use blocks.")
@click.option("--bash-only", is_flag=True, help="Show only Bash commands and output.")
@click.option("--tail", "tail_n", type=int, default=None, help="Show last N entries.")
@click.option("--since", default=None, help="Show entries after ISO timestamp.")
@click.option("--json", "raw_json", is_flag=True, help="Output raw JSONL.")
@click.option("--verbose", is_flag=True, help="Show progress/system entries.")
def inspect_session(
    session_id: str,
    tools_only: bool,
    bash_only: bool,
    tail_n: Optional[int],
    since: Optional[str],
    raw_json: bool,
    verbose: bool,
) -> None:
    """Inspect a session's JSONL transcript (runs locally).

    Reads ~/.claude/projects/{slug}/{session-id}.jsonl directly.

    Examples:

        claude-telegram-bot session inspect abc12345-...

        claude-telegram-bot session inspect abc12345 --bash-only

        claude-telegram-bot session inspect abc12345 --tools-only --tail 20

        claude-telegram-bot session inspect abc12345 --json | jq .
    """
    path = _find_session_jsonl(session_id)
    if not path:
        click.echo(f"Error: No transcript found for session {session_id}", err=True)
        click.echo(
            "Looked in ~/.claude/projects/*/. "
            "Ensure the session ID is correct and the transcript exists.",
            err=True,
        )
        sys.exit(1)

    entries = _parse_jsonl(path)

    if not entries:
        click.echo("Transcript is empty.")
        return

    # Filter by --since
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            click.echo(f"Error: Invalid timestamp: {since}", err=True)
            sys.exit(1)
        filtered = []
        for e in entries:
            ts_str = _entry_iso_timestamp(e)
            if ts_str:
                try:
                    entry_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if entry_dt >= since_dt:
                        filtered.append(e)
                except (ValueError, TypeError):
                    filtered.append(e)
            else:
                filtered.append(e)
        entries = filtered

    # --- bash-only mode ---
    if bash_only:
        # Build tool_use_id -> result_entry map
        result_map: Dict[str, Dict[str, Any]] = {}
        for e in entries:
            if _is_tool_result(e):
                tid = _get_tool_result_id(e)
                if tid:
                    result_map[tid] = e

        bash_pairs = []
        for e in entries:
            if _is_tool_use(e, "Bash"):
                for tid in _get_tool_use_ids(e):
                    bash_pairs.append((e, result_map.get(tid)))

        if tail_n is not None:
            bash_pairs = bash_pairs[-tail_n:]

        for tool_entry, result_entry in bash_pairs:
            click.echo(_render_bash_pair(tool_entry, result_entry))
            click.echo()
        return

    # --- tools-only mode ---
    if tools_only:
        tool_entries = [e for e in entries if _is_tool_use(e)]
        if tail_n is not None:
            tool_entries = tool_entries[-tail_n:]
        for e in tool_entries:
            ts = _entry_timestamp(e)
            content = e.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        inp = _summarize_tool_input(block.get("input", {}))
                        click.echo(f"[{ts}] TOOL {name}: {inp}")
        return

    # --- raw json mode ---
    if raw_json:
        if tail_n is not None:
            entries = entries[-tail_n:]
        for e in entries:
            click.echo(json.dumps(e))
        return

    # --- default: full timeline ---
    if tail_n is not None:
        entries = entries[-tail_n:]

    for e in entries:
        rendered = _render_entry(e, verbose=verbose)
        if rendered:
            click.echo(rendered)


@session.command(name="send")
@click.option("--session-id", default=None, help="Resume a specific session.")
@click.option("--dir", "directory", default=None, help="Working directory.")
@click.option("--user", "user_id", type=int, default=None, help="User ID.")
@click.option("--new", "force_new", is_flag=True, help="Force new session.")
@click.option("--message", "-m", required=True, help="Message to send.")
def send_message(
    session_id: Optional[str],
    directory: Optional[str],
    user_id: Optional[int],
    force_new: bool,
    message: str,
) -> None:
    """Send a message to Claude through a session.

    Auto-resumes the most recent session for the directory (like Telegram).
    Use --new to force a fresh session, or --session-id to resume a specific one.

    Examples:

        claude-telegram-bot session send -m "echo hello"

        claude-telegram-bot session send --dir /home/user/project -m "run tests"

        claude-telegram-bot session send --session-id abc123 -m "continue"

        claude-telegram-bot session send --new -m "start fresh"
    """
    payload: dict = {"message": message, "force_new": force_new}
    if session_id:
        payload["session_id"] = session_id
    if directory:
        payload["working_directory"] = directory
    if user_id is not None:
        payload["user_id"] = user_id

    result = _request("POST", "/api/sessions/send", json_data=payload)

    # Session context line
    sid = result.get("session_id", "unknown")
    mode = "new" if force_new else "resumed"
    dir_display = directory or "(default)"
    click.echo(f"Session: {sid} ({mode}) in {dir_display}")
    click.echo()

    # Response content
    content = result.get("content", "")
    click.echo(content)
    click.echo()

    # Metadata
    cost = result.get("cost", 0)
    duration = result.get("duration_ms", 0)
    turns = result.get("num_turns", 0)
    tools = result.get("tools_used", [])

    meta_parts = [
        f"Cost: ${cost:.4f}",
        f"Duration: {duration}ms",
        f"Turns: {turns}",
    ]
    if tools:
        meta_parts.append(f"Tools: {', '.join(tools)}")
    click.echo("  ".join(meta_parts))

    if result.get("is_error"):
        sys.exit(1)


@session.command(name="response")
@click.argument("session_id")
@click.option(
    "--last", "last_n", type=int, default=5, help="Number of recent messages."
)
def show_response(session_id: str, last_n: int) -> None:
    """Show recent prompt/response pairs for a session.

    Examples:

        claude-telegram-bot session response abc12345

        claude-telegram-bot session response abc12345 --last 1
    """
    result = _request(
        "GET",
        f"/api/sessions/{session_id}/messages",
        params={"last": str(last_n)},
    )
    messages = result.get("messages", [])

    if not messages:
        click.echo(f"No messages found for session {session_id}.")
        return

    # Show oldest first
    for msg in reversed(messages):
        ts = msg.get("timestamp", "?")[:19].replace("T", " ")
        cost = msg.get("cost", 0)
        duration = msg.get("duration_ms") or 0

        click.echo(f"[{ts}]  ${cost:.4f}  {duration}ms")
        click.echo(f"  > {msg.get('prompt', '')}")
        response = msg.get("response", "") or "(no response)"
        # Truncate long responses for overview
        if len(response) > 500:
            response = response[:500] + "..."
        click.echo(f"  {response}")
        click.echo()
