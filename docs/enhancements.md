# claude-code-telegram — Enhancement Tracker

## Scheduler — Cron Job Agent Awareness

> An "agent" in this context is a working directory. Each directory has its own
> CLAUDE.md, skills, memory files, and configuration. The `working_directory`
> field on a scheduled job already identifies which agent should execute it.

### Use `created_by` as execution user ID
**Status:** Proposed
**Priority:** High
**Depends on:** —

Currently `handle_scheduled()` runs every cron job as `default_user_id` (typically `0`
or the first allowed user). This means:
- In `resume` mode, all jobs share the same session lineage regardless of who created them
- The per-user+directory concurrency lock groups unrelated jobs together
- Session history is attributed to the service account, not the actual user

**Fix:** Pass `created_by` through `ScheduledEvent` and use it as `user_id` in
`run_command()`. The `created_by` field already exists in the `scheduled_jobs` table
but is ignored at execution time.

**Changes required:**
- Add `created_by: int` field to `ScheduledEvent` dataclass
- Pass `created_by` from `_fire_event()` to the event
- Use `event.created_by` instead of `self.default_user_id` in `handle_scheduled()`

---

### Cron result message boilerplate
**Status:** Proposed
**Priority:** High
**Depends on:** —

Cron results are delivered as plain Telegram messages via NotificationService with no
metadata. When a user has multiple agents (working directories) with concurrent jobs,
it's impossible to tell which agent produced which result.

**Fix:** Prepend a static header to every cron result message before publishing the
`AgentResponseEvent`:

```
📋 Scheduled Task: {job_name}
📁 Agent: {working_directory}
⏰ {timestamp}

{agent response}
```

**Changes required:**
- Build boilerplate string in `handle_scheduled()` from `ScheduledEvent` fields
- Prepend to `response.content` before publishing `AgentResponseEvent`
- Keep raw `response.content` in `response_summary` for job history (no boilerplate)

---

### Cron session system prompt
**Status:** Proposed
**Priority:** High
**Depends on:** Use `created_by` as execution user ID

Cron sessions currently use the same system prompt as interactive sessions. The agent
has no awareness that it's running a scheduled task, what the task is called, or that
its final message will be delivered to the user via Telegram.

**Fix:** Pass a dedicated system prompt (via `SystemPromptPreset.append` or a plain
`system_prompt` string) when starting cron sessions. The prompt establishes the
execution context and the output contract.

```
You are executing a scheduled task.
- Task: {job_name}
- Schedule: {cron_expression}
- Working directory: {working_directory}

Your response will be delivered to the user as a Telegram message.
If the outcome does not require the user's attention, respond with
exactly [SILENT] and nothing else. Otherwise, write a concise message
summarizing the outcome.
```

> **SDK constraint (v0.1.39):** `system_prompt` is set once at session creation.
> Cannot be injected mid-session. This is fine for cron sessions since they are
> always fresh (isolated mode) or forked.

**Changes required:**
- Build cron system prompt string in `handle_scheduled()` from event fields
- Thread it through `run_command()` → SDK as `system_prompt` or `append` on the preset
- `run_command()` needs a new optional `system_prompt_append` parameter (or similar)
- `ClaudeSDKManager` merges the cron append with any existing preset append

---

### Agent-controlled notification suppression
**Status:** Proposed
**Priority:** High
**Depends on:** Cron session system prompt

The system prompt tells the agent it can respond with `[SILENT]` when the result
doesn't warrant user attention. The delivery layer needs to honor this.

**Fix:** After cron execution, check the response for the `[SILENT]` sentinel before
publishing the `AgentResponseEvent`. If silent, log the run in job history but skip
Telegram delivery.

**Changes required:**
- In `handle_scheduled()`, after getting `response.content`:
  - Strip and check if content equals `[SILENT]`
  - If silent: set `response_summary = "[SILENT]"` in job history, skip event publish
  - If not silent: publish with boilerplate as normal
- Job history always records the run regardless of silence

---

### One-time job execution (DateTrigger)
**Status:** Proposed
**Priority:** Medium
**Depends on:** —

Currently all jobs use `CronTrigger` (recurring). APScheduler 3.x also provides
`DateTrigger` — fires exactly once at a specific datetime, then auto-removes itself
from the in-memory job store.

```python
from apscheduler.triggers.date import DateTrigger
scheduler.add_job(func, 'date', run_date='2026-03-01 14:30:00')
```

**Schema changes (new migration):**
- Add `trigger_type TEXT DEFAULT 'cron'` column
- Add `run_date TEXT` column (ISO 8601, null for cron jobs)
- Make `cron_expression` nullable (SQLite requires table recreation)

**Code changes:**
- `add_job()` — accept optional `run_date` parameter, branch on trigger type
- `_load_jobs_from_db()` — reconstruct `DateTrigger` or `CronTrigger` based on
  `trigger_type`; skip/soft-delete date jobs where `run_date < now` to prevent
  re-firing after a service restart
- `_fire_event()` — after a date job fires, soft-delete the DB row (APScheduler
  auto-removes from memory but doesn't touch SQLite)
- CLI `schedule add` — add `--at` flag as alternative to `--cron`
- API `POST /api/scheduler/jobs` — accept `trigger_type` + `run_date` fields

**Gotcha:** If the service restarts after a date job fired but before the DB row was
cleaned up, `_load_jobs_from_db()` would re-register it and it would fire again
(APScheduler treats past `run_date` as immediately due). The load logic must guard
against this.

---

### Retry logic on failure
**Status:** Proposed
**Priority:** Low
**Depends on:** Job execution history (done)

Currently jobs fire once per schedule match with no retry. Add configurable retry (max attempts, backoff) for jobs that fail due to transient errors.

---

## Context & Compaction

> Research completed 2026-02-21. Tested against `claude-agent-sdk` v0.1.39.

### Background

The Claude Code CLI supports `/compact` (summarize and replace conversation history) and `/context` (show context window usage). The SDK does not expose dedicated methods for either, but both **work when sent as regular user messages** via `client.query()`. The SDK is content-agnostic — it wraps any string as a `{"type": "user", "content": "..."}` JSON message and forwards it to the CLI subprocess over stdin. The CLI (running with `--input-format stream-json`) recognizes and processes slash commands from this input.

Additionally, `ResultMessage.usage` returns detailed token counts that the project currently ignores, and `client.get_server_info()` advertises all available CLI commands (including `/compact` and `/context`) at connection time.

---

### Token usage tracking
**Status:** Proposed
**Priority:** High
**Depends on:** —

`ResultMessage.usage` is returned on every response but never extracted. Actual structure observed at runtime:

```json
{
  "input_tokens": 3,
  "cache_creation_input_tokens": 2535,
  "cache_read_input_tokens": 15993,
  "output_tokens": 6,
  "server_tool_use": {
    "web_search_requests": 0,
    "web_fetch_requests": 0
  },
  "service_tier": "standard",
  "cache_creation": {
    "ephemeral_1h_input_tokens": 2535,
    "ephemeral_5m_input_tokens": 0
  }
}
```

Currently used from `ResultMessage`: `total_cost_usd`, `session_id`, `result`, `num_turns`, `duration_ms`. The `usage` dict is ignored.

**Changes required:**
- Add `usage: dict` field to `ClaudeResponse` dataclass
- Extract from `ResultMessage` in `sdk_integration.py` alongside existing fields
- Store cumulative token counts in session metadata (SQLite)
- Surface in `/status` output: `Tokens: 15k in / 2k out / $0.05`

---

### Conversation compaction (`/compact`)
**Status:** Proposed (verified working via SDK)
**Priority:** Medium
**Depends on:** —

Sending `/compact` (or `/compact <custom summarization instructions>`) through `client.query()` triggers real compaction on the CLI side. The session ID is preserved. Compaction costs tokens (the CLI summarizes history before discarding it).

**Response stream observed during testing:**
```
System(status) → System(status) → System(init) → System(compact_boundary) → UserMessage → UserMessage → ResultMessage
```

The `compact_boundary` system message marks where compaction occurred. Works with the project's existing session-resume architecture — compaction happens inside a resumed session, no lifecycle changes needed.

**Changes required:**
- Register `/compact` as an agentic-mode Telegram command in `orchestrator.py`
- Forward `/compact [optional instructions]` via `client.query()` to the current session
- Parse response stream for `compact_boundary` to confirm success
- Reply to user with confirmation

---

### Context inspection (`/context`)
**Status:** Proposed (verified working via SDK)
**Priority:** Medium
**Depends on:** Token usage tracking (for the alternative approach)

Sending `/context` through `client.query()` triggers the CLI's context display.

**Response stream observed during testing:**
```
System(init) → UserMessage → ResultMessage
```

Context data is inside the `SystemMessage`/`UserMessage` payloads — needs `data` attribute inspection to extract the actual numbers.

**Two implementation options:**

1. **Forward to CLI** — send `/context` via `client.query()`, parse `SystemMessage` data for context window metrics. More accurate (includes context window %).
2. **Compute locally** — derive from accumulated `ResultMessage.usage` data (from token tracking above). Simpler but no context window percentage.

**Changes required:**
- Register `/context` as an agentic-mode Telegram command
- Either forward to CLI and parse, or compute from stored token data
- Format and display to user

---

### Command discovery via `get_server_info()`
**Status:** Research finding
**Priority:** Low
**Depends on:** —

After connection, `client.get_server_info()` returns all CLI slash commands with descriptions and argument hints. Currently not called anywhere in the project. Could be used to dynamically discover and validate commands rather than hardcoding them.

```python
info = await client.get_server_info()
# info["commands"] → 12 commands including compact, context, extra-usage, ...
# info["output_style"], info["available_output_styles"], info["models"], info["account"], info["pid"]
```

---

### Auto-compaction awareness
**Status:** Proposed
**Priority:** Low
**Depends on:** Conversation compaction

The SDK's hook system includes a `PreCompact` event:

```python
class PreCompactHookInput(BaseHookInput):
    hook_event_name: Literal["PreCompact"]
    trigger: Literal["manual", "auto"]
    custom_instructions: str | None
```

Allows observing (and optionally blocking) compaction events. Cannot trigger compaction — observation only. Useful for logging or notifying users when auto-compaction fires.

**Changes required:**
- Register `PreCompact` hook in `ClaudeAgentOptions.hooks`
- Log compaction events with trigger type
- Optionally notify user: "Context was getting full — conversation auto-compacted."

---

## Implementation Notes

**Nested session guard:** Test scripts using the SDK from within a Claude Code session must `os.environ.pop("CLAUDECODE", None)` before importing the SDK, or the CLI refuses to start.

**`rate_limit_event` parse error:** The SDK (v0.1.39) throws `MessageParseError` on `rate_limit_event` messages. The project already handles this in `sdk_integration.py` by using raw `_query.receive_messages()` with try/except on `parse_message`. Any new command handlers should follow the same pattern.

**SDK message capabilities (v0.1.39, researched 2026-02-23):**
- The SDK can only **send** `type: "user"` messages to the CLI. No system or assistant message injection mid-session.
- `system_prompt` is set once at session creation via `ClaudeAgentOptions`. Supports plain string or `SystemPromptPreset` with `append`.
- `fork_session=True` + `resume=<id>` branches from an existing session into a new session ID (original preserved). Does not allow injecting new starting context — carries full prior conversation.
- `additionalContext` in hook outputs (`UserPromptSubmit`, `SessionStart`) provides ephemeral per-turn context, not persisted in session history.
- No API to read back session history, append messages without triggering a turn, or pre-seed a new session with arbitrary conversation history.
