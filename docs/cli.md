# CLI Tool Reference

The `claude-telegram-bot` command provides service management, job scheduling, and session observability. It is installed as a Poetry console script pointing to `src.cli.main:cli`.

## Default Behavior

Running `claude-telegram-bot` without a subcommand starts the bot (backward compatible with the original entry point).

## Service Commands

Thin wrappers around `systemctl --user` and `journalctl --user`:

```bash
claude-telegram-bot run                    # Start bot in foreground (default)
claude-telegram-bot start                  # Start systemd service
claude-telegram-bot stop                   # Stop systemd service
claude-telegram-bot restart                # Restart systemd service
claude-telegram-bot status                 # Show service status
claude-telegram-bot logs [-f] [-n 50]      # View service logs (-f to follow)
```

See [Systemd Setup](../SYSTEMD_SETUP.md) for service installation.

## Schedule Commands

Manage cron and one-time jobs via the running bot's API server. Requires `WEBHOOK_API_SECRET` to be set and the bot to be running with `ENABLE_API_SERVER=true` and `ENABLE_SCHEDULER=true`.

### Add a Job

```bash
# Recurring cron job
claude-telegram-bot schedule add \
  --name "Daily check" \
  --cron "0 9 * * 1-5" \
  --prompt "Run tests and report" \
  --chat-id 123456 \
  --session-mode isolated

# One-time job (DateTrigger)
claude-telegram-bot schedule add \
  --name "Deploy reminder" \
  --at "2026-03-01T09:00:00" \
  --prompt "Remind about deploy" \
  --chat-id 123456
```

| Flag | Required | Description |
|------|----------|-------------|
| `--name` | Yes | Human-readable job name |
| `--cron` | One of `--cron` / `--at` | Cron expression (e.g. `"0 9 * * 1-5"`) |
| `--at` | One of `--cron` / `--at` | ISO 8601 datetime for one-time execution |
| `--prompt` | Yes | Prompt sent to Claude on each trigger |
| `--chat-id` | Yes | Telegram chat ID for notifications |
| `--session-mode` | No | `isolated` (default) or `resume` |

### List / Remove / Trigger / History

```bash
claude-telegram-bot schedule list              # List active jobs
claude-telegram-bot schedule remove <job_id>   # Remove a job (cascade-deletes runs)
claude-telegram-bot schedule trigger <job_id>  # Manually trigger a job now
claude-telegram-bot schedule history <job_id>  # Show execution history (last 20 runs)
```

## Session Commands

Observe and interact with Claude sessions. Most commands talk to the API server; `inspect` reads local JSONL files directly.

### List Sessions

```bash
claude-telegram-bot session list               # List sessions for current directory
claude-telegram-bot session list --dir /path   # Filter by working directory
claude-telegram-bot session list --user 12345  # Filter by Telegram user ID
claude-telegram-bot session list --all         # Show all sessions
```

### Inspect Transcript (Local)

Parses `~/.claude/projects/` JSONL transcript files directly. Works even when the bot is stopped.

```bash
claude-telegram-bot session inspect <session_id>
claude-telegram-bot session inspect <session_id> --bash-only    # Only Bash tool calls
claude-telegram-bot session inspect <session_id> --tools-only   # Only tool calls
claude-telegram-bot session inspect <session_id> --tail 20      # Last 20 entries
claude-telegram-bot session inspect <session_id> --since "2026-02-20T00:00:00"
claude-telegram-bot session inspect <session_id> --json         # Raw JSON output
```

### Send Message

Send a message through Claude integration via the API server.

```bash
claude-telegram-bot session send --message "run tests"
claude-telegram-bot session send --message "check status" --dir /path/to/project
claude-telegram-bot session send --message "hello" --session-id abc123
claude-telegram-bot session send --message "fresh start" --new             # Force new session
claude-telegram-bot session send --message "check" --user 12345
```

### Show Responses

Show recent prompt/response pairs from the message history.

```bash
claude-telegram-bot session response <session_id>
claude-telegram-bot session response <session_id> --last 5     # Last 5 pairs
```

## Prerequisites

| Command group | Requires bot running | Requires `WEBHOOK_API_SECRET` |
|--------------|---------------------|------------------------------|
| Service (`start`, `stop`, etc.) | No | No |
| Schedule | Yes | Yes |
| Session (`list`, `send`, `response`) | Yes | Yes |
| Session `inspect` | No (reads local files) | No |
