# claude-code-telegram — Enhancement Tracker

## Scheduler

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
