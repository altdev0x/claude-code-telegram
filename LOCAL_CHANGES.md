# Local Changes vs Upstream

Tracks deliberate modifications to our fork that diverge from
[RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram).
Review this file before merging upstream to avoid accidentally reverting local work.

Last synced with upstream: 2026-02-22 (commit `921dd36`)

---

## `ADDITIONAL_ALLOWED_PATHS` — configurable path allowlist

**Why:** Upstream hardcodes `~/.claude/plans/`, `~/.claude/todos/`, and
`~/.claude/settings.json` as internal exceptions in `ToolMonitor`. We need a
general-purpose mechanism to allow arbitrary extra directories (e.g. shared
workspaces, notes folders) without code changes.

**Files changed:**

| File | What |
|------|------|
| `src/config/settings.py` | New `additional_allowed_paths: Optional[List[Path]]` field + `parse_additional_allowed_paths` validator (resolves, expands `~`, validates existence) |
| `src/claude/monitor.py` | `check_bash_directory_boundary()` accepts `additional_allowed_paths` param; checks tokens against all allowed dirs, not just `approved_directory` |
| `src/security/validators.py` | `SecurityValidator.__init__` accepts `additional_allowed_paths`; `validate_path()` checks the extra dirs before rejecting |
| `src/main.py` | Passes `config.additional_allowed_paths` to `SecurityValidator` constructor |
| `.env.example` | Documents the new `ADDITIONAL_ALLOWED_PATHS` env var |

**Merge notes:** Upstream's `_is_claude_internal_path()` in `monitor.py` covers
Read/Write/Edit tool validation for Claude's own dirs. Our change covers Bash
boundary checks and the `SecurityValidator` path checks. They are complementary —
both should be kept. The two modifications touch different functions so merges
should apply cleanly.

---

## Upstream changes we deliberately kept (not local divergence)

These are upstream defaults we chose **not** to override, noted here for
awareness during future merges:

| Setting | Location | Our value | Upstream value | Reason |
|---------|----------|-----------|----------------|--------|
| `setting_sources=["project"]` | `sdk_integration.py:179` | `["project"]` | `["project"]` | Required for workspace `.claude/settings.json` and `CLAUDE.md` to load. SDK default (`None`) passes `--setting-sources ""` which disables all filesystem settings. |
| `continue_conversation=True` | `sdk_integration.py:202` | `True` | `True` | Redundant with `options.resume` but harmless. Could be dropped in a future cleanup. |
