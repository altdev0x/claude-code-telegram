# Architecture

## Claude SDK Integration

`ClaudeIntegration` (facade in `src/claude/facade.py`) wraps `ClaudeSDKManager` (`src/claude/sdk_integration.py`), which uses `claude-agent-sdk` with `ClaudeSDKClient` for async streaming. Session IDs come from Claude's `ResultMessage`, not generated locally.

Sessions auto-resume: per user+directory, persisted in SQLite. A per-user+directory `asyncio.Lock` in `ClaudeIntegration` serializes concurrent `run_command()` calls (e.g. interactive message vs. scheduled job) to prevent session collisions.

## Request Flow

### Agentic Mode (default, `AGENTIC_MODE=true`)

```
Telegram message -> Security middleware (group -3) -> Auth middleware (group -2)
-> Rate limit (group -1) -> MessageOrchestrator.agentic_text() (group 10)
-> ClaudeIntegration.run_command() -> SDK
-> Response parsed -> Stored in SQLite -> Sent back to Telegram
```

### External Triggers (webhooks, scheduler)

```
Webhook POST /webhooks/{provider} -> Signature verification -> Deduplication
-> Publish WebhookEvent to EventBus -> AgentHandler.handle_webhook()
-> ClaudeIntegration.run_command() -> Publish AgentResponseEvent
-> NotificationService -> Rate-limited Telegram delivery
```

### Classic Mode (`AGENTIC_MODE=false`)

Same middleware chain, but routes through full command/message handlers in `src/bot/handlers/` with 13 commands and inline keyboards.

## Dependency Injection

Bot handlers access dependencies via `context.bot_data`:

```python
context.bot_data["auth_manager"]
context.bot_data["claude_integration"]
context.bot_data["storage"]
context.bot_data["security_validator"]
```

## Key Directories

| Directory | Description |
|-----------|-------------|
| `src/config/` | Pydantic Settings v2 config with env detection, feature flags (`features.py`), YAML project loader (`loader.py`) |
| `src/bot/handlers/` | Telegram command, message, and callback handlers (classic mode + project thread commands) |
| `src/bot/middleware/` | Auth, rate limit, security input validation |
| `src/bot/features/` | Git integration, file handling, quick actions, session export |
| `src/bot/orchestrator.py` | MessageOrchestrator: routes to agentic or classic handlers, project-topic routing |
| `src/claude/` | Claude integration facade, SDK/CLI managers, session management, tool monitoring |
| `src/projects/` | Multi-project support: `registry.py` (YAML project config), `thread_manager.py` (Telegram topic sync/routing) |
| `src/storage/` | SQLite via aiosqlite, repository pattern (users, sessions, messages, tool_usage, audit_log, cost_tracking, project_threads, scheduled_job_runs) |
| `src/security/` | Multi-provider auth (whitelist + token), input validators (with optional `disable_security_patterns`), rate limiter, audit logging |
| `src/events/` | EventBus (async pub/sub), event types, AgentHandler (with job run recording), EventSecurityMiddleware |
| `src/api/` | FastAPI server (bound to `127.0.0.1`): webhook auth, scheduler CRUD routes (`scheduler_routes.py`), session observability routes (`session_routes.py`) |
| `src/scheduler/` | APScheduler cron and one-time (DateTrigger) jobs, persistent SQLite storage, job execution history (20-run retention), session mode (`isolated`/`resume`), creator identity propagation, cron agent context via `system_prompt_append` |
| `src/cli/` | Click CLI: `main.py` (group entry point), `service.py` (systemd wrappers), `schedule.py` (scheduler HTTP client), `session.py` (session observability) |
| `src/notifications/` | NotificationService, rate-limited Telegram delivery |

## Security Model

5-layer defense: authentication (whitelist/token) → CLI permission enforcement (`dontAsk` mode + deny rules + PreToolUse hook) → Telegram input validation (`SecurityValidator`) → rate limiting (token bucket) → audit logging.

- **CLI permissions**: Each agent workspace has a `.claude/settings.json` with `dontAsk` mode. Two `PreToolUse` hooks enforce path boundaries: `file-boundary.sh` restricts file tools to CWD, `~/.claude/plans/**` (r+w), and `~/.claude/skills/**` (read-only); `bash-boundary.sh` checks path tokens in shell commands. Sensitive bash commands and settings self-modification are denied via deny rules.
- **SDK `can_use_tool` callback**: Simplified to bash-only boundary checking as defense-in-depth (only fires in non-`dontAsk` modes).
- **`SecurityValidator`**: Validates Telegram user inputs at the middleware layer (path traversal, command injection, secret file access). Relaxed with `DISABLE_SECURITY_PATTERNS=true`.
- **Permission violations**: Denied tool calls return `ToolResultBlock(is_error=True)` in the SDK stream, extracted as `StreamUpdate(type="permission_denied")` and shown in Telegram.
- **Webhook authentication**: GitHub HMAC-SHA256 signature verification, generic Bearer token, atomic deduplication via `webhook_events` table.

See [Security Policy](../SECURITY.md) for the full threat model, and [Permission Model](permission-refactoring.md) for the detailed permission architecture.

## Configuration

Settings loaded from environment variables via Pydantic Settings. Feature flags in `src/config/features.py` control runtime behavior.

See [Configuration Guide](configuration.md) for the full environment variable reference.
