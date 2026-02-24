# Permission Model: CLI-Native Enforcement

**Date**: 2026-02-24
**Status**: Implemented
**Branch**: `feature/cli-scheduler-enhancements`

## Summary

Agent file and bash access is enforced by the Claude Code CLI's native permission
system, not by the SDK wrapper. Each agent workspace has a `.claude/settings.json`
with `dontAsk` mode, explicit allow/deny rules, and a `PreToolUse` hook for bash
directory boundary enforcement.

## Architecture

```
Claude model decides to use a tool
    ↓
CLI loads {cwd}/.claude/settings.json (via setting_sources=["project"])
    ↓
Deny rules evaluated first (always win)
    ↓
dontAsk mode: tool must be in allow list, otherwise auto-denied
    ↓
PreToolUse hook fires for Bash: bash-boundary.sh checks all paths
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

3. **PreToolUse hook for bash**: `Bash(*)` in the allow list is necessary for Claude
   to run shell commands, but it bypasses Read/Edit path restrictions. The
   `bash-boundary.sh` hook closes this gap by checking every path-like token in
   bash commands against the allowed directory.

4. **SDK `can_use_tool` callback**: Simplified to bash-only boundary checking. File
   tool validation removed — the CLI handles it via deny/allow rules natively. The
   callback still fires in non-`dontAsk` modes as defense-in-depth.

5. **SecurityValidator stays for Telegram layer**: Used by 6 Telegram-layer
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
      "Read(./**)",
      "Read(~/.claude/**)",
      "Edit(./**)",
      "Edit(~/.claude/**)",
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
      }
    ]
  }
}
```

### Permission Rule Syntax

| Pattern | Meaning |
|---------|---------|
| `Read(./**)` | Read files in CWD and subdirectories |
| `Read(~/.claude/**)` | Read files under `~/.claude/` |
| `Edit(.claude/settings.json)` | Edit the settings file (used in deny) |
| `Bash(*)` | All bash commands (further restricted by hook) |
| `Bash(sudo *)` | Bash commands starting with `sudo` |

**Evaluation order**: deny → ask → allow. Deny rules always take precedence.

In `dontAsk` mode, the allow list is a strict allowlist — anything not explicitly
listed is auto-denied.

### Bash Boundary Hook

`{work_dir}/.claude/hooks/bash-boundary.sh`:

- Fires on every `Bash` tool call via `PreToolUse` hook
- Parses command tokens using `xargs -n1`
- Checks absolute paths (`/...`), home-relative paths (`~/...`), and traversals (`../`)
- Allows paths within the working directory (from `session_cwd`)
- Allows paths within `~/.claude/`
- Returns `permissionDecision: "deny"` with reason for violations

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
┌─ Layer 1: CLI Native (primary) ────────────────────────────────┐
│                                                                 │
│  dontAsk mode     →  Strict allowlist for all tools             │
│  settings.json    →  Deny rules for sensitive commands          │
│  CWD restriction  →  File tools restricted to CWD by default   │
│  Read/Edit rules  →  ./** and ~/.claude/** only                 │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
        ↓
┌─ Layer 2: PreToolUse Hook ─────────────────────────────────────┐
│                                                                 │
│  bash-boundary.sh →  Checks all path tokens in bash commands    │
│                      against working directory boundary          │
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

### Herbert (`/home/clawdbot/claude-telegram-workspace/herbert/`)

- `Read(./**)`, `Read(~/.claude/**)`, `Edit(./**)`, `Edit(~/.claude/**)`
- Full deny list + bash-boundary hook
- Self-modification denied

### Klaus (`/home/clawdbot/claude-telegram-workspace/klaus/`)

- `Read(./**)`, `Read(~/.claude/skills/**)`, `Edit(./**)`
- No `Edit(~/.claude/**)` — read-only access to Claude internals except skills
- Full deny list + bash-boundary hook
- Self-modification denied

## Verification

Tested interactively via CLI:

| Test | Result |
|------|--------|
| Read file in work dir | Allowed ✓ |
| Read `/etc/passwd` | Denied by CLI (not in allow list) ✓ |
| `cat /etc/hostname` via Bash | Denied by hook ✓ |
| `touch /tmp/file` via Bash | Denied by hook ✓ |
| `ls -la` in work dir | Allowed ✓ |
| Edit `.claude/settings.json` | Denied by deny rule ✓ |

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
