# claude-code-telegram — Enhancement Tracker

## CLI

### Unified CLI tool
**Status:** Proposed
**Priority:** High
**Depends on:** —

Single entrypoint for service management and administration. Wraps systemd for process lifecycle (no reimplementation), adds subcommands for scheduler and other admin tasks.

```
claude-telegram-bot start|stop|restart|status
claude-telegram-bot schedule add|list|remove|update
claude-telegram-bot logs [--follow]
```

Replaces the current split workflow: `systemctl --user` for service control, source code edits for job management.

---

## Scheduler

### CLI job management
**Status:** Proposed
**Priority:** High
**Depends on:** Unified CLI tool

CRUD for scheduled jobs via the CLI. Persists to SQLite and (if the service is running) registers with APScheduler at runtime. No source code changes or restarts needed.

```
claude-telegram-bot schedule add \
  --name "Daily health check" \
  --cron "0 9 * * 1-5" \
  --prompt "Run tests and report" \
  --chat-id 8200705927 \
  --working-dir /home/clawdbot/claude-telegram-workspace/klaus \
  --session-mode isolated

claude-telegram-bot schedule list
claude-telegram-bot schedule remove <job_id>
```

---

### Session mode per cron job
**Status:** Proposed
**Priority:** High
**Depends on:** CLI job management

Add a `session_mode` field to scheduled job configuration with two options:

- **`isolated`** (default) — each run creates a fresh session. Best for stateless tasks: health checks, reports, reminders. No context bleed, predictable cost.
- **`resume`** — continues the most recent session for that job's user+directory. Best for tasks that build on previous state (e.g., ongoing monitoring).

**Changes required:**
- New `session_mode` column on `scheduled_jobs` table (default: `"isolated"`)
- Pass through `ScheduledEvent` dataclass
- `AgentHandler.handle_scheduled()` sets `force_new=True` when mode is `isolated`

---

### Job execution history
**Status:** Proposed
**Priority:** Medium
**Depends on:** CLI job management

No logs of past runs exist. Add a `scheduled_job_runs` table tracking:
- job_id, fired_at, completed_at, success/failure, response summary, cost

Enables: `claude-telegram-bot schedule history <job_id>`, failure alerting, cost tracking per job.

---

### Retry logic on failure
**Status:** Proposed
**Priority:** Low
**Depends on:** Job execution history

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

## Security / Tool Validation

### Feed validation errors back to Claude instead of killing the session
**Status:** Done (Phase 3, `can_use_tool` callback)
**Priority:** High
**Depends on:** —

Implemented via `_make_can_use_tool_callback` in `sdk_integration.py`. The callback validates tool calls preventively using `PermissionResultDeny`, which returns the error message to Claude as a denied tool result. Claude sees the denial reason and can retry with a valid path or inform the user — no session abort needed.

---

## Session Identity

### Session ID self-awareness
**Status:** Done
**Priority:** Medium
**Depends on:** —

Make each Claude Code instance aware of its own session ID by injecting it into the system prompt appendix at session launch time. Enables Claude to reference its session in logs, cross-session communication (e.g., scheduled jobs leaving notes for interactive sessions), and debugging ("which session produced this output?").

> Research completed 2026-02-22. Tested against `claude-agent-sdk` v0.1.39.

**Key finding — chicken-and-egg timing:**

Session IDs are not generated locally. They come from the Claude Code CLI backend in the `ResultMessage` after the first response. This creates a timing gap:

| Scenario | Session ID known? | Can inject? |
|---|---|---|
| New session, first message | No — ID doesn't exist yet | No |
| New session, second+ message | Yes — captured from first response | Yes (it's a resume now) |
| Resumed session | Yes — stored in DB | Yes |

Claude would be aware of its session ID **from the second message onward**. For resumed sessions (the common case for ongoing work), it's available immediately on the first message.

**Implementation approach:**

In `sdk_integration.py` (lines 185-188), the system prompt is currently hardcoded and rebuilt on every `execute_command()` call via a new `ClaudeAgentOptions`. When resuming (i.e., `session_id` is set), append the ID:

```python
base_prompt = (
    f"All file operations must stay within {working_directory}. "
    "Use relative paths."
)
if session_id:
    base_prompt += f"\n\nYour session ID is: {session_id}"

options = ClaudeAgentOptions(
    system_prompt=base_prompt,
    ...
)
```

The SDK re-applies the system prompt on resume, so this works with the existing architecture — no lifecycle changes needed.

**Changes required:**
- Thread `session_id` parameter through to `execute_command()` in `sdk_integration.py` (currently only used for `options.resume`)
- Conditionally append session ID to the system prompt string when non-empty
- No database, session manager, or event bus changes needed

---

### Interface channel awareness
**Status:** Done
**Priority:** Medium
**Depends on:** —

The Claude Code agent has no way to detect whether it's being invoked via Telegram or the CLI terminal. The bridge is the only component that knows the session originates from Telegram, but it doesn't communicate this. As a result, the agent cannot adapt formatting or behavior to the interface — concise mobile-friendly messages for Telegram, richer output for the terminal.

**Implementation approach:**

In `sdk_integration.py` (lines 185-188), append a channel identifier to the existing system prompt:

```python
base_prompt = (
    f"All file operations must stay within {working_directory}. "
    "Use relative paths.\n\n"
    "Interface: Telegram chat"
)
```

The bridge states the fact; the agent's own instructions (CLAUDE.md / memory) decide what to do with it. CLI invocations naturally lack this line, which is itself a distinguishing signal.

**Changes required:**
- Add `"Interface: Telegram chat"` to the system prompt in `sdk_integration.py`
- No configuration, database, or lifecycle changes needed

**Design note:** Keep the injection factual, not behavioral. The bridge should not dictate "be concise" — that's the agent's concern. A neutral label lets each agent workspace define its own channel-specific behavior via its own instructions.

---

### Implementation notes

**Nested session guard:** Test scripts using the SDK from within a Claude Code session must `os.environ.pop("CLAUDECODE", None)` before importing the SDK, or the CLI refuses to start.

**`rate_limit_event` parse error:** The SDK (v0.1.39) throws `MessageParseError` on `rate_limit_event` messages. The project already handles this in `sdk_integration.py` by using raw `_query.receive_messages()` with try/except on `parse_message`. Any new command handlers should follow the same pattern.
