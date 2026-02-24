# Claude Code Telegram Bot -- Project Overview

## Project Description

A Telegram bot that provides remote access to Claude Code, allowing developers to interact with their projects from anywhere. The default interaction model is **agentic mode** -- a conversational interface where users chat naturally with Claude. A classic terminal-like mode with 13 commands is also available.

## Core Objectives

1. **Remote Development Access**: Enable developers to use Claude Code from any device with Telegram
2. **Security-First Design**: Implement robust security boundaries to prevent unauthorized access
3. **Conversational Interface**: Natural language interaction as the primary mode (agentic mode)
4. **Session Persistence**: Maintain Claude Code context across conversations and project switches
5. **Event-Driven Automation**: Support webhooks, scheduled jobs, and proactive notifications

## Target Users

- Developers who need coding assistance while mobile
- Teams wanting shared Claude Code access
- Users who prefer chat-based interfaces for development tasks
- Developers managing multiple projects remotely

## Key Features

### Agentic Mode (Default)
- Natural language conversation with Claude -- no commands needed
- Minimal command set: `/start`, `/new`, `/status`
- Automatic session persistence per user/project directory
- File and image upload support

### Classic Mode
- Terminal-like commands (cd, ls, pwd)
- Project quick-switching with visual selection
- Inline keyboards for common actions
- Git status integration
- Session export in multiple formats

### Event-Driven Platform
- **Event Bus**: Async pub/sub system with typed event subscriptions
- **Webhook API**: FastAPI server receiving GitHub and generic webhooks with signature verification
- **Job Scheduler**: APScheduler cron and one-time (DateTrigger) jobs with persistent storage, creator identity, agent context, and `[SILENT]` suppression
- **Notifications**: Rate-limited Telegram delivery for agent responses

### Claude Code Integration
- Full Claude Code SDK integration via `ClaudeSDKClient`
- Session management per user/project with auto-resume
- Tool usage visibility with verbose output levels
- Cost tracking and limits
- Permission violation surfacing in Telegram

### Security & Access Control
- CLI-native permission enforcement (`dontAsk` mode with per-agent `settings.json`)
- PreToolUse bash-boundary hook for shell command path enforcement
- User authentication (whitelist and token-based)
- Rate limiting per user
- Webhook authentication (HMAC-SHA256, Bearer token)
- Telegram input validation via SecurityValidator
- Audit logging

## Technical Architecture

### Components

1. **MessageOrchestrator** (`src/bot/orchestrator.py`)
   - Routes to agentic or classic handlers based on mode
   - Dependency injection for all handlers

2. **Configuration** (`src/config/`)
   - Pydantic Settings v2 with environment variables
   - Feature flags for dynamic functionality control

3. **Authentication** (`src/security/`)
   - User verification, token management, permission checking
   - Input validation and security middleware

4. **Claude Integration** (`src/claude/`)
   - SDK and CLI backends via facade pattern
   - Session state management and auto-resume

5. **Storage Layer** (`src/storage/`)
   - SQLite database with repository pattern
   - Session persistence, analytics, cost tracking

6. **Event Bus** (`src/events/`)
   - Async pub/sub with typed subscriptions
   - AgentHandler bridges events to Claude
   - EventSecurityMiddleware validates events

7. **Webhook API** (`src/api/`)
   - FastAPI server for external webhooks
   - GitHub HMAC-SHA256 + generic Bearer token auth

8. **Scheduler** (`src/scheduler/`)
   - APScheduler with cron and DateTrigger (one-time) triggers
   - Persistent job storage in SQLite
   - Creator identity propagation and cron agent context
   - `[SILENT]` response suppression for quiet scheduled runs

9. **Notifications** (`src/notifications/`)
   - Rate-limited Telegram delivery
   - Message splitting and broadcast support

### Data Flow

**Agentic mode (direct messages):**
```
User Message -> Telegram -> Middleware Chain -> MessageOrchestrator
    -> ClaudeIntegration.run_command() -> Response -> Telegram
```

**External triggers (webhooks/scheduler):**
```
Webhook/Cron -> EventBus -> AgentHandler -> ClaudeIntegration
    -> AgentResponseEvent -> NotificationService -> Telegram
```

### Security Model

- **CLI Permission Enforcement**: `dontAsk` mode with per-agent `settings.json` — strict tool allowlist, deny rules for sensitive commands, PreToolUse bash-boundary hook
- **Directory Isolation**: File tools restricted to agent working directory via CLI rules; bash commands path-checked by hook
- **User Authentication**: Whitelist or token-based access
- **Rate Limiting**: Prevent abuse and control costs
- **Webhook Verification**: HMAC-SHA256 and Bearer token authentication
- **Audit Trail**: Log all operations for security review
- **Telegram Input Validation**: SecurityValidator sanitizes user inputs at middleware layer

## Development Principles

1. **Security First**: Every feature must consider security implications
2. **Conversational by Default**: Agentic mode as the primary interaction model
3. **Event-Driven**: Decoupled components communicating through the event bus
4. **Testability**: Comprehensive test coverage
5. **Documentation**: Clear docs for users and contributors
