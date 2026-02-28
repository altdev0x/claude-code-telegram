# Permission Model: CLI-Native Enforcement

**Date**: 2026-02-24
**Status**: Implemented
**Branch**: `feature/cli-scheduler-enhancements`

## Summary

Agent file and bash access is enforced by the Claude Code CLI's native permission
system, not by the SDK wrapper. Each agent workspace has a `.claude/settings.json`
with `dontAsk` mode, explicit allow/deny rules, and two `PreToolUse` hooks:
`file-boundary.sh` for file tool path enforcement and `bash-boundary.sh` for
shell command path enforcement.

## Architecture

```
Claude model decides to use a tool
    ↓
CLI loads {cwd}/.claude/settings.json (via setting_sources=["project"])
    ↓
PreToolUse hooks fire first:
  - file-boundary.sh checks file tool paths (Read/Write/Edit/Glob/Grep/Notebook)
  - bash-boundary.sh checks bash command path tokens
    ↓
Deny rules evaluated (always win over allow)
    ↓
dontAsk mode: tool must be in allow list, otherwise auto-denied
    ↓
Tool executes (or denial is returned as ToolResultBlock(is_error=True))
    ↓
SDK wrapper extracts ToolResultBlock errors → StreamUpdate(type="permission_denied")
    ↓
Telegram user sees denial message
```

### Key Design Decisions

1. **`dontAsk` mode**: Allow rules become a strict allowlist. Tools not listed are
   auto-denied without prompting. The `can_use_tool` callback does NOT fire in this
   mode — all decisions are automatic.

2. **No sandbox config**: The Raspberry Pi 4 (ARM aarch64, Ubuntu 25.10) has no
   functional sandbox runtime (no bubblewrap, firejail, or sandbox-exec). The
   `sandbox` kwarg is not passed to `ClaudeAgentOptions`.

3. **PreToolUse hooks for path enforcement**: Two hooks handle all path-based
   restrictions:
   - `file-boundary.sh` — checks file paths for Read, Write, Edit, MultiEdit,
     Glob, Grep, NotebookRead, NotebookEdit. Allows CWD (read+write),
     `~/.claude/plans/**` (read+write), `~/.claude/skills/**` (read-only).
   - `bash-boundary.sh` — checks all path-like tokens in bash commands against
     the working directory boundary. Closes the `Bash(*)` bypass.

4. **Allow rules are type-only**: `Read` and `Edit` in the allow list have no path
   patterns — the hooks handle path enforcement. This keeps the settings.json
   simple and centralizes path logic in the hooks.

5. **SDK `can_use_tool` callback**: Simplified to bash-only boundary checking as
   defense-in-depth. Only fires in non-`dontAsk` modes.

6. **SecurityValidator stays for Telegram layer**: Used by 6 Telegram-layer
   consumers (middleware, handlers) for input validation before requests reach Claude.
   Decoupled from the SDK layer.

## Permission Configuration

### Agent `settings.json` Template

Each agent workspace (`{work_dir}/.claude/settings.json`):

```json
{
  "defaultMode": "dontAsk",
  "permissions": {
    "allow": [
      "Read",
      "Edit",
      "Bash(*)",
      "Task",
      "TaskOutput",
      "LS",
      "NotebookRead",
      "NotebookEdit",
      "TodoRead",
      "TodoWrite",
      "WebFetch",
      "WebSearch",
      "Skill"
    ],
    "deny": [
      "EnterPlanMode",
      "Edit(.claude/settings.json)",
      "Edit(.claude/settings.local.json)",

      "Bash(sudo *)",
      "Bash(su *)",
      "Bash(su)",
      "..."
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/bash-boundary.sh"
          }
        ]
      },
      {
        "matcher": "Read|Write|Edit|MultiEdit|Glob|Grep|NotebookRead|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/file-boundary.sh"
          }
        ]
      }
    ]
  }
}
```

### Permission Rule Syntax

| Pattern | Meaning |
|---------|---------|
| `Read` | Allow the Read tool (path enforcement via hook) |
| `Edit` | Allow Edit/Write/MultiEdit tools (path enforcement via hook) |
| `Edit(.claude/settings.json)` | Edit the settings file (used in deny) |
| `Bash(*)` | All bash commands (path enforcement via hook) |
| `Bash(sudo *)` | Bash commands starting with `sudo` |

**Evaluation order**: hooks → deny → ask → allow. Hooks fire first and can deny
before other rules are checked. Deny rules take precedence over allow rules.

In `dontAsk` mode, the allow list is a strict allowlist — anything not explicitly
listed is auto-denied. Allow rules have no path patterns; the `file-boundary.sh`
hook handles all path-level enforcement.

### File Boundary Hook

`{work_dir}/.claude/hooks/file-boundary.sh`:

- Fires on Read, Write, Edit, MultiEdit, Glob, Grep, NotebookRead, NotebookEdit
- Extracts file path from tool input (`file_path`, `path`, or `notebook_path`)
- Resolves relative paths against `session_cwd`
- Allowed directories:
  - Working directory (`session_cwd`) → read + write
  - `~/.claude/plans/**` → read + write
  - `~/.claude/skills/**` → read only
- Returns `permissionDecision: "deny"` for anything outside these boundaries
- Returns no output (no decision) for allowed paths — pipeline continues to deny rules

### Bash Boundary Hook

`{work_dir}/.claude/hooks/bash-boundary.sh`:

- Fires on every `Bash` tool call via `PreToolUse` hook
- Parses command tokens using `xargs -n1`
- Checks absolute paths (`/...`), home-relative paths (`~/...`), and traversals (`../`)
- Allows paths within the working directory (from `session_cwd`)
- Allows paths within `~/.claude/`
- Returns `permissionDecision: "deny"` with reason for violations

### Hook + Deny Rule Interaction

The hooks return no output (no decision) for paths inside the allowed boundaries.
The permission pipeline then continues to deny rules. This is how
`Edit(.claude/settings.json)` in the deny list works: the hook allows it (it's
inside CWD), but the deny rule blocks it.

### Deny List Categories

The deny list covers:

| Category | Examples |
|----------|---------|
| Privilege escalation | `sudo`, `su`, `pkexec` |
| System services | `systemctl`, `service`, `journalctl` |
| Package management | `apt`, `dpkg`, `snap`, `pip install`, `npm install -g` |
| Disk operations | `dd`, `mkfs`, `fdisk`, `mount` |
| Network/firewall | `iptables`, `nft` |
| User management | `useradd`, `userdel`, `usermod`, `passwd` |
| File permissions | `chown`, `chmod`, `crontab` |
| Process management | `kill`, `killall`, `nohup` |
| Network tools | `nc`, `ncat`, `socat`, `ssh`, `scp`, `rsync` |
| Self-modification | `Edit(.claude/settings.json)` |
| Plan mode | `EnterPlanMode` |

## SDK Layer Changes

### Removed

- `sandbox` config from `ClaudeAgentOptions` (no runtime on this system)
- `SecurityValidator` dependency from `ClaudeSDKManager` and `can_use_tool` callback
- `_is_claude_internal_path()` and `_CLAUDE_INTERNAL_SUBDIRS` from `monitor.py`
- `sandbox_enabled` and `sandbox_excluded_commands` from `Settings`
- File tool validation in `can_use_tool` callback (CLI handles natively)

### Added

- `ToolResultBlock(is_error=True)` extraction from SDK stream → `StreamUpdate(type="permission_denied")`
- Permission denial display in Telegram verbose output (🚫 icon)

### Simplified

- `can_use_tool` callback: bash boundary check only, no file tool validation
- `ClaudeSDKManager.__init__`: no `security_validator` parameter
- Callback is always wired (not conditional on validator presence)

## Enforcement Layers

```
┌─ Layer 1: PreToolUse Hooks (first in pipeline) ────────────────┐
│                                                                 │
│  file-boundary.sh →  Checks file tool paths against:            │
│                      CWD (r+w), plans (r+w), skills (r-only)    │
│  bash-boundary.sh →  Checks bash command path tokens against    │
│                      working directory boundary                  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
        ↓
┌─ Layer 2: CLI Permission Rules ────────────────────────────────┐
│                                                                 │
│  Deny rules       →  Settings self-modification, sensitive cmds │
│  dontAsk mode     →  Strict allowlist for tool types            │
│  Allow list       →  Read, Edit, Bash(*), Task, etc.            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
        ↓
┌─ Layer 3: SDK can_use_tool (defense-in-depth) ─────────────────┐
│                                                                 │
│  Bash boundary    →  check_bash_directory_boundary() in         │
│                      monitor.py (only fires in non-dontAsk)     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
        ↓
┌─ Layer 4: Telegram Middleware ─────────────────────────────────┐
│                                                                 │
│  SecurityValidator →  Input validation before Claude             │
│  Path traversal    →  Blocks .., ;, &&, $() in user input       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Current Agent Configurations

Both agents share the same `settings.json` structure and hooks. Allow rules are
type-only (`Read`, `Edit`); path enforcement is handled entirely by hooks.

### Herbert (`/home/clawdbot/agents/herbert/`)

- `Read`, `Edit` (no path patterns — hooks enforce boundaries)
- file-boundary hook: CWD (r+w), `~/.claude/plans/**` (r+w), `~/.claude/skills/**` (read-only)
- bash-boundary hook: CWD + `~/.claude/`
- Full deny list + self-modification denied

### Klaus (`/home/clawdbot/agents/klaus/`)

- Same configuration as Herbert
- Same hooks and deny rules

## Verification

Tested interactively via CLI:

| Test | Enforced By | Result |
|------|-------------|--------|
| Read file in work dir | file-boundary hook (allows) | Allowed ✓ |
| Read `/etc/hostname` | file-boundary hook | Denied ✓ |
| Read `~/.claude/skills/...` | file-boundary hook (read-only) | Allowed ✓ |
| Write to `~/.claude/skills/...` | file-boundary hook (read-only) | Denied ✓ |
| Read `~/.claude/plans/...` | file-boundary hook (r+w) | Allowed ✓ |
| Write to `~/.claude/plans/...` | file-boundary hook (r+w) | Allowed ✓ |
| Edit `.claude/settings.json` | deny rule (hook passes, deny catches) | Denied ✓ |
| Write to other agent's dir | file-boundary hook | Denied ✓ |
| `cat /etc/hostname` via Bash | bash-boundary hook | Denied ✓ |
| `touch /tmp/file` via Bash | bash-boundary hook | Denied ✓ |
| `ls -la` in work dir | bash-boundary hook (allows) | Allowed ✓ |
| Path traversal `../../etc/passwd` | file-boundary hook | Denied ✓ |
| Grep in `/etc` | file-boundary hook | Denied ✓ |

## Files Modified

| File | Change |
|------|--------|
| `src/claude/sdk_integration.py` | Simplified callback, removed validator, removed sandbox, added ToolResultBlock extraction |
| `src/claude/monitor.py` | Removed `_is_claude_internal_path`, `_CLAUDE_INTERNAL_SUBDIRS` |
| `src/config/settings.py` | Removed `sandbox_enabled`, `sandbox_excluded_commands` |
| `src/main.py` | Removed `security_validator` from `ClaudeSDKManager()` call |
| `src/bot/orchestrator.py` | Handle `permission_denied` stream updates, added 🚫 icon |
| `tests/unit/test_claude/test_sdk_integration.py` | Rewrote callback tests, removed sandbox tests, added stream tests |
| `tests/unit/test_claude/test_monitor.py` | Removed `TestIsClaudeInternalPath` |
| `{agent}/.claude/settings.json` | Permission rules per agent workspace |
| `{agent}/.claude/hooks/bash-boundary.sh` | PreToolUse hook for bash boundary enforcement |
| `{agent}/.claude/hooks/file-boundary.sh` | PreToolUse hook for file tool path enforcement |
