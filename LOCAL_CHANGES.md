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
