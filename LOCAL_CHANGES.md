# Local Changes

Changes made to this fork on top of upstream `RichardAtCT/claude-code-telegram`.

## 1. Add `"skills"` to `_CLAUDE_INTERNAL_SUBDIRS`

**File:** `src/claude/monitor.py`

Added `"skills"` to the `_CLAUDE_INTERNAL_SUBDIRS` allowlist so that Claude Code
can access symlinked skills under `~/.claude/skills/` without being blocked by
directory boundary enforcement.

```python
_CLAUDE_INTERNAL_SUBDIRS: Set[str] = {"plans", "todos", "skills", "settings.json"}
```

## 2. Preserve `setting_sources=["project"]`

**File:** `src/claude/sdk_integration.py` (line 257)

The SDK is configured with `setting_sources=["project"]` to load project-level
settings (from `.claude/settings.json` in the approved directory). This is an
upstream setting that must not be removed — it enables per-project tool
configuration and permissions.

## 3. System prompt uses Claude Code preset with custom append

**File:** `src/claude/sdk_integration.py`

Upstream passes `system_prompt` as a plain string, which **replaces** Claude Code's
entire built-in system prompt (tool instructions, coding guidelines, safety rules,
environment context). Changed to use the SDK's preset mode:

```python
system_prompt=SystemPromptPreset(
    type="preset",
    preset="claude_code",
    append="...",  # our custom instructions appended after the built-in prompt
)
```

The `append` field adds three pieces of context:
- Working directory constraint (`All file operations must stay within ...`)
- Interface channel (`Interface: Telegram chat`)
- Session ID when available (`Your session ID is: ...`)

## 4. Session concurrency guard

**File:** `src/claude/facade.py`

Added a per-user+directory `asyncio.Lock` (`_session_locks`) to `ClaudeIntegration`.
All `run_command()` calls acquire this lock, serializing concurrent access to the
same session scope. Prevents race conditions when a scheduled job and an interactive
user message target the same user+directory simultaneously.

## 5. CLI tool with Click subcommands

**Files:** `src/cli/` (new package), `pyproject.toml`

Replaced the single `src.main:run` entry point with a Click CLI group at
`src.cli.main:cli`. Running `claude-telegram-bot` without a subcommand still
starts the bot (backward compatible). New subcommands:

- `start|stop|restart|status` — thin wrappers around `systemctl --user`
- `logs [-f] [-n N]` — wraps `journalctl --user`
- `schedule add|list|remove|history` — HTTP client to the API server

## 6. Scheduler API routes

**Files:** `src/api/scheduler_routes.py` (new), `src/api/server.py`

Added CRUD API endpoints for job management under `/api/scheduler/`:

- `POST /api/scheduler/jobs` — add job
- `GET /api/scheduler/jobs` — list jobs
- `DELETE /api/scheduler/jobs/{job_id}` — remove job
- `GET /api/scheduler/jobs/{job_id}/history` — execution history

All endpoints require Bearer token auth (`WEBHOOK_API_SECRET`). The API server now
binds to `127.0.0.1` instead of `0.0.0.0` to prevent external access.

## 7. Scheduler session mode

**Files:** `src/scheduler/scheduler.py`, `src/events/types.py`, `src/events/handlers.py`, `src/storage/database.py`

Added `session_mode` field (`isolated` or `resume`) to scheduled jobs:

- **`isolated`** (default) — each run creates a fresh Claude session (`force_new=True`). No context bleed.
- **`resume`** — continues the user's most recent session for that directory. Protected by the concurrency guard (change #4).

New `session_mode` column on `scheduled_jobs` table (migration #5). Passed through
`ScheduledEvent` dataclass to `AgentHandler.handle_scheduled()`.

## 8. Job execution history with retention

**Files:** `src/scheduler/scheduler.py`, `src/events/handlers.py`, `src/storage/database.py`

New `scheduled_job_runs` table (migration #5) tracking: job_id, fired_at,
completed_at, success, response_summary, cost, error_message.

- `AgentHandler.handle_scheduled()` records every run (success or failure)
- Per-job retention: 20 runs max, oldest pruned on insert
- `remove_job()` cascade-deletes associated runs
- Surfaced via `claude-telegram-bot schedule history <job_id>`

## 9. Startup order change

**File:** `src/main.py`

The `JobScheduler` is now initialized **before** the API server so the scheduler
instance can be passed to `create_api_app()` for the scheduler routes. The
`agent_handler.job_scheduler` reference is set after scheduler creation.

## 10. Scheduler identity & DateTrigger

**Files:** `src/scheduler/scheduler.py`, `src/events/types.py`, `src/events/handlers.py`, `src/storage/database.py`, `src/api/scheduler_routes.py`, `src/cli/schedule.py`

Three enhancements and a bug fix:

- **`created_by` propagation**: `ScheduledEvent` carries the Telegram user ID of the
  job creator. `handle_scheduled()` uses it as `user_id` for `run_command()`, so jobs
  execute in the creator's session scope instead of always user 0.
- **`cron_expression` on event**: Passed through to handlers for context and to
  distinguish cron from date jobs.
- **DateTrigger (one-time jobs)**: New `trigger_type` (`cron`|`date`) and `run_date`
  columns (migration #6). `add_job()` branches to `CronTrigger` or `DateTrigger`.
  Date jobs are soft-deleted after firing. Expired date jobs are skipped on startup.
  CLI gains `--at` flag (mutually exclusive with `--cron`). API `AddJobRequest` has
  cross-field validation via `@model_validator`.
- **Bug fix**: `_fire_event()` now sets `job_id` on `ScheduledEvent`. Previously
  `job_id` was always `""`, so `record_job_run()` was silently skipped.

## 11. Cron agent context (`system_prompt_append`)

**Files:** `src/claude/facade.py`, `src/claude/sdk_integration.py`

Added `system_prompt_append: Optional[str]` parameter threaded through the full
`run_command()` → `_run_command_locked()` → `_execute()` → `execute_command()` chain.
`_build_system_prompt()` accepts `extra_append` and includes it in the preset's
`append` field. This allows callers (e.g. scheduled job handler) to inject per-turn
context into the system prompt without modifying the base prompt.

## 12. Cron result handling: boilerplate, [SILENT], system prompt

**File:** `src/events/handlers.py`

`handle_scheduled()` now:

- **Cron system prompt**: Builds a context block with job name, schedule, working
  directory, and the `[SILENT]` output contract. Passed via `system_prompt_append`.
- **Boilerplate header**: Non-silent responses are prefixed with job name, agent
  directory, and timestamp before publishing to `AgentResponseEvent`.
- **`[SILENT]` suppression**: If the agent responds with exactly `[SILENT]`, no
  `AgentResponseEvent` is published (no Telegram notification). The run is still
  recorded with `response_summary="[SILENT]"`.
