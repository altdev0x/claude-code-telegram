# Development Guide

This document provides detailed information for developers working on the Claude Code Telegram Bot.

## Getting Started

### Prerequisites

- Python 3.11 or higher
- Poetry for dependency management
- Git for version control
- Claude authentication (one of):
  - Claude Code CLI installed and authenticated
  - Anthropic API key for direct SDK usage

### Initial Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/RichardAtCT/claude-code-telegram.git
   cd claude-code-telegram
   ```

2. **Install Poetry** (if not already installed):
   ```bash
   pip install poetry
   ```

3. **Install dependencies**:
   ```bash
   make dev
   ```

4. **Set up pre-commit hooks** (optional but recommended):
   ```bash
   poetry run pre-commit install
   ```

5. **Create configuration file**:
   ```bash
   cp .env.example .env
   # Edit .env with your development settings
   ```

## Development Workflow

### Daily Development

1. **Activate the Poetry environment**:
   ```bash
   poetry shell
   ```

2. **Run tests continuously during development**:
   ```bash
   make test
   ```

3. **Format code before committing**:
   ```bash
   make format
   ```

4. **Check code quality**:
   ```bash
   make lint
   ```

### Available Make Commands

```bash
make help          # Show all available commands
make install       # Install production dependencies only
make dev           # Install all dependencies including dev tools
make test          # Run full test suite with coverage
make lint          # Run all code quality checks
make format        # Auto-format all code
make clean         # Clean up generated files
make run           # Run the bot in normal mode
make run-debug     # Run the bot with debug logging

# Version management
make version       # Show current version
make bump-patch    # Bump patch version, commit, and tag
make bump-minor    # Bump minor version, commit, and tag
make bump-major    # Bump major version, commit, and tag
make release       # Push tag to trigger GitHub release workflow
```

## Project Architecture

### Package Structure

```
src/
├── config/           # Pydantic Settings v2 config, env detection, feature flags
│   ├── settings.py
│   ├── loader.py
│   ├── environments.py
│   └── features.py
├── bot/              # Telegram bot implementation
│   ├── core.py
│   ├── orchestrator.py   # Routes agentic/classic handlers, project-topic routing
│   ├── handlers/         # Command, message, and callback handlers
│   ├── middleware/       # Auth, rate limit, security input validation
│   ├── features/         # Git integration, file handling, quick actions, session export
│   └── utils/
├── claude/           # Claude Code integration
│   ├── facade.py         # High-level integration API (ClaudeIntegration)
│   ├── sdk_integration.py # ClaudeSDKManager, async streaming
│   ├── session.py
│   ├── monitor.py        # Tool usage monitoring (ToolMonitor)
│   └── exceptions.py
├── api/              # FastAPI server (webhook auth, scheduler + session routes)
│   ├── server.py
│   ├── auth.py
│   ├── scheduler_routes.py
│   └── session_routes.py
├── cli/              # Click CLI (service, schedule, session commands)
│   ├── main.py           # CLI group entry point
│   ├── service.py        # systemd wrappers
│   ├── schedule.py       # Scheduler HTTP client
│   └── session.py        # Session observability
├── events/           # EventBus (async pub/sub), event types, AgentHandler
├── scheduler/        # APScheduler cron/DateTrigger jobs, SQLite persistence
├── notifications/    # NotificationService, rate-limited Telegram delivery
├── projects/         # Multi-project support (registry, thread manager)
├── storage/          # SQLite via aiosqlite, repository pattern
│   ├── database.py
│   ├── models.py
│   ├── repositories.py
│   ├── facade.py
│   └── session_storage.py
├── security/         # Auth, input validation, rate limiting, audit logging
├── utils/
├── exceptions.py
└── main.py           # Application entry point
```

### Testing Structure

```
tests/
├── unit/               # Unit tests (mirror src structure)
│   ├── test_config.py
│   ├── test_environments.py
│   ├── test_exceptions.py
│   ├── test_orchestrator.py
│   ├── test_api/       # API server and route tests
│   ├── test_bot/       # Bot component tests
│   ├── test_claude/    # Claude integration tests
│   ├── test_cli/       # CLI command tests
│   ├── test_events/    # EventBus and handler tests
│   ├── test_notifications/ # Notification service tests
│   ├── test_projects/  # Multi-project support tests
│   ├── test_scheduler/ # Scheduler tests
│   ├── test_security/  # Security framework tests
│   └── test_storage/   # Storage layer tests
├── integration/        # Integration tests
├── fixtures/           # Test data and fixtures
└── conftest.py         # Pytest configuration
```

## Code Standards

### Code Style

We use strict code formatting and quality tools:

- **Black**: Code formatting with 88-character line length
- **isort**: Import sorting with Black compatibility
- **flake8**: Linting with 88-character line length
- **mypy**: Static type checking with strict settings

### Type Hints

All code must include comprehensive type hints:

```python
from typing import Optional, List, Dict, Any
from pathlib import Path

def process_config(
    settings: Settings, 
    overrides: Optional[Dict[str, Any]] = None
) -> Path:
    """Process configuration with optional overrides."""
    # Implementation
    return Path("/example")
```

### Error Handling

Use the custom exception hierarchy defined in `src/exceptions.py`:

```python
from src.exceptions import ConfigurationError, SecurityError

try:
    # Some operation
    pass
except ValueError as e:
    raise ConfigurationError(f"Invalid configuration: {e}") from e
```

### Logging

Use structured logging throughout:

```python
import structlog

logger = structlog.get_logger()

def some_function():
    logger.info("Operation started", operation="example", user_id=123)
    try:
        # Some operation
        logger.debug("Step completed", step="validation")
    except Exception as e:
        logger.error("Operation failed", error=str(e), operation="example")
        raise
```

## Testing Guidelines

### Test Organization

- **Unit tests**: Test individual functions and classes in isolation
- **Integration tests**: Test component interactions
- **End-to-end tests**: Test complete workflows (planned)

### Writing Tests

```python
import pytest
from src.config import create_test_config

def test_feature_with_config():
    """Test feature with specific configuration."""
    config = create_test_config(
        debug=True,
        claude_max_turns=5
    )
    
    # Test implementation
    assert config.debug is True
    assert config.claude_max_turns == 5

@pytest.mark.asyncio
async def test_async_feature():
    """Test async functionality."""
    # Test async code
    result = await some_async_function()
    assert result is not None
```

### Test Coverage

We aim for >80% test coverage. Current coverage:

- Configuration system: ~95%
- Security framework: ~95%
- Claude integration: ~75%
- Storage layer: ~90%
- Bot components: ~85%
- Exception handling: 100%
- Utilities: 100%
- Overall: ~85%

## Component Overview

All core components are implemented:

- **Configuration**: Pydantic Settings v2, environment overrides, feature flags, cross-field validation
- **Security**: Multi-provider auth (whitelist + token), rate limiting, input validation, path traversal prevention, audit logging
- **Telegram Bot**: Agentic and classic mode handlers, middleware chain, inline keyboards
- **Claude Integration**: SDK streaming, session auto-resume, tool monitoring, cost tracking
- **Storage**: SQLite with migrations, repository pattern, persistent sessions
- **API Server**: FastAPI webhooks, scheduler CRUD, session observability routes
- **CLI**: Click subcommands for service management, scheduling, session inspection
- **Events**: EventBus pub/sub, webhook and scheduled event handling
- **Scheduler**: APScheduler cron + DateTrigger jobs, execution history, session modes
- **Notifications**: Rate-limited Telegram delivery for proactive messages

## Development Environment Configuration

### Required Environment Variables

For development, set these in your `.env` file:

```bash
# Required for basic functionality
TELEGRAM_BOT_TOKEN=test_token_for_development
TELEGRAM_BOT_USERNAME=test_bot
APPROVED_DIRECTORY=/path/to/your/test/projects

# Claude Authentication (choose one method)
# Option 1: Use existing Claude CLI auth (no API key needed)
# Option 2: Direct API key
# ANTHROPIC_API_KEY=sk-ant-api03-your-development-key

# Development settings
DEBUG=true
DEVELOPMENT_MODE=true
LOG_LEVEL=DEBUG
ENVIRONMENT=development

# Optional for testing specific features
ENABLE_GIT_INTEGRATION=true
ENABLE_FILE_UPLOADS=true
ENABLE_QUICK_ACTIONS=true
```

### Running in Development Mode

```bash
# Basic run with environment variables
export TELEGRAM_BOT_TOKEN=test_token
export TELEGRAM_BOT_USERNAME=test_bot  
export APPROVED_DIRECTORY=/tmp/test_projects
make run-debug

# Or with .env file
make run-debug
```

The debug output will show:
- Configuration loading steps
- Environment overrides applied
- Feature flags enabled
- Validation results

## Version Management

### How versioning works

The version is defined in a single place: `pyproject.toml`. At runtime, `src/__init__.py` reads it via `importlib.metadata`. There is no hardcoded version string to keep in sync.

### Cutting a release

```bash
# Bump the version (choose one) — commits, tags, and pushes automatically
make bump-patch    # 1.2.0 -> 1.2.1
make bump-minor    # 1.2.0 -> 1.3.0
make bump-major    # 1.2.0 -> 2.0.0
```

`make bump-*` runs `poetry version`, commits `pyproject.toml`, creates a git tag, and pushes both to GitHub. The tag push triggers the release workflow:

1. Runs the full lint + test suite
2. Creates a GitHub Release with auto-generated release notes
3. Updates the rolling `latest` git tag (stable releases only)

### Pre-releases

Tags containing `-rc`, `-beta`, or `-alpha` (e.g. `v1.3.0-rc1`) are marked as pre-releases on GitHub and do **not** update the `latest` tag.

## Contributing

### Before Submitting a PR

1. **Run the full test suite**:
   ```bash
   make test
   ```

2. **Check code quality**:
   ```bash
   make lint
   ```

3. **Format code**:
   ```bash
   make format
   ```

4. **Update documentation** if needed

5. **Add tests** for new functionality

### Commit Message Format

Use conventional commits:

```
feat: add rate limiting functionality
fix: resolve configuration validation issue
docs: update development guide
test: add tests for authentication system
```

### Code Review Guidelines

- All code must pass linting and type checking
- Test coverage should not decrease
- New features require documentation updates
- Security-related changes require extra review

## Common Development Tasks

### Adding a New Configuration Option

1. **Add to Settings class** in `src/config/settings.py`:
   ```python
   new_setting: bool = Field(False, description="Description of new setting")
   ```

2. **Add to .env.example** with documentation

3. **Add validation** if needed

4. **Write tests** in `tests/unit/test_config.py`

5. **Update documentation** in `docs/configuration.md`

### Adding a New Feature Flag

1. **Add property** to `FeatureFlags` class in `src/config/features.py`:
   ```python
   @property
   def new_feature_enabled(self) -> bool:
       return self.settings.enable_new_feature
   ```

2. **Add to enabled features list**

3. **Write tests**

### Debugging Configuration Issues

1. **Use debug logging**:
   ```bash
   make run-debug
   ```

2. **Check validation errors** in the logs

3. **Verify environment variables**:
   ```bash
   env | grep TELEGRAM
   env | grep CLAUDE
   ```

4. **Test configuration loading**:
   ```python
   from src.config import load_config
   config = load_config()
   print(config.model_dump())
   ```

## Troubleshooting

### Common Issues

1. **Import errors**: Make sure you're in the Poetry environment (`poetry shell`)

2. **Configuration validation errors**: Check that required environment variables are set

3. **Test failures**: Ensure test dependencies are installed (`make dev`)

4. **Type checking errors**: Run `poetry run mypy src` to see detailed errors

5. **Poetry issues**: Try `poetry lock --no-update` to fix lock file issues

### Getting Help

- Check the logs with `make run-debug`
- Review test output with `make test`
- Examine the implementation documentation in `docs/`
- Look at existing code patterns in the completed modules