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
