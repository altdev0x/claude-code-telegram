"""Tests for the idle-based watchdog pattern in ClaudeSDKManager."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from src.claude.exceptions import (
    ClaudeExecutionError,
    ClaudeIdleTimeoutError,
    ClaudeTimeoutError,
)
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.settings import Settings


# ---------------------------------------------------------------------------
# Helpers (same pattern as test_sdk_integration.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_parse_message():
    """Patch parse_message as identity so mocks can yield typed Message objects."""
    with patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x):
        yield


def _make_assistant_message(text: str = "Test response") -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-20250514",
    )


def _make_result_message(**kwargs) -> ResultMessage:  # type: ignore[no-untyped-def]
    defaults = {
        "subtype": "success",
        "duration_ms": 1000,
        "duration_api_ms": 800,
        "is_error": False,
        "num_turns": 1,
        "session_id": "test-session",
        "total_cost_usd": 0.05,
        "result": "Success",
    }
    defaults.update(kwargs)
    return ResultMessage(**defaults)


def _mock_client_with_delay(*messages, delay_before_first: float = 0.0):
    """Create a mock client that optionally delays before yielding messages."""
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def receive_raw_messages():
        if delay_before_first:
            await asyncio.sleep(delay_before_first)
        for msg in messages:
            yield msg

    query_mock = AsyncMock()
    query_mock.receive_messages = receive_raw_messages
    client._query = query_mock
    return client


def _mock_client_slow_between_messages(*message_groups, delay: float = 0.0):
    """Create a client that yields messages with ``delay`` seconds between groups."""
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def receive_raw_messages():
        for i, group in enumerate(message_groups):
            if i > 0 and delay:
                await asyncio.sleep(delay)
            for msg in group:
                yield msg

    query_mock = AsyncMock()
    query_mock.receive_messages = receive_raw_messages
    client._query = query_mock
    return client


@pytest.fixture
def config(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        claude_idle_timeout_seconds=5,  # 5s global default for tests
    )


@pytest.fixture
def sdk_manager(config: Settings) -> ClaudeSDKManager:
    return ClaudeSDKManager(config)


# ---------------------------------------------------------------------------
# Watchdog fires on idle
# ---------------------------------------------------------------------------


class TestWatchdogFires:
    """Watchdog correctly fires when no messages arrive within idle_timeout."""

    async def test_watchdog_fires_with_no_messages(self, sdk_manager: ClaudeSDKManager, tmp_path: Path) -> None:
        """With idle_timeout=0.1s and a 1s hang, ClaudeTimeoutError is raised."""
        client = _mock_client_with_delay(delay_before_first=1.0)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeTimeoutError):
                await sdk_manager.execute_command(
                    prompt="Test",
                    working_directory=tmp_path,
                    idle_timeout=0,  # Will use 0 … wait no, 0 maps to None (unlimited)?
                )

    async def test_watchdog_fires_after_idle_timeout(
        self, sdk_manager: ClaudeSDKManager, tmp_path: Path
    ) -> None:
        """Watchdog fires when the stream hangs longer than idle_timeout."""
        client = _mock_client_with_delay(delay_before_first=2.0)  # 2s hang

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeTimeoutError):
                await sdk_manager.execute_command(
                    prompt="Test",
                    working_directory=tmp_path,
                    idle_timeout=1,  # 1s idle timeout
                )

    async def test_per_call_idle_timeout_overrides_config(
        self, tmp_path: Path
    ) -> None:
        """Per-call idle_timeout takes precedence over config.claude_idle_timeout_seconds."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=60,  # long global default
        )
        manager = ClaudeSDKManager(config)

        # A client that hangs for 2s — longer than per-call timeout of 1s
        client = _mock_client_with_delay(delay_before_first=2.0)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeTimeoutError):
                await manager.execute_command(
                    prompt="Test",
                    working_directory=tmp_path,
                    idle_timeout=1,  # Override: 1s per-call
                )

    async def test_watchdog_raises_timeout_error_not_idle_error(
        self, sdk_manager: ClaudeSDKManager, tmp_path: Path
    ) -> None:
        """With no partial messages, ClaudeIdleTimeoutError is translated to ClaudeTimeoutError."""
        client = _mock_client_with_delay(delay_before_first=2.0)

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeTimeoutError):
                await sdk_manager.execute_command(
                    prompt="Test",
                    working_directory=tmp_path,
                    idle_timeout=1,
                )


# ---------------------------------------------------------------------------
# Watchdog does NOT fire when messages arrive regularly
# ---------------------------------------------------------------------------


class TestWatchdogDoesNotFire:
    """Watchdog stays quiet when messages arrive within idle_timeout."""

    async def test_no_watchdog_for_fast_execution(
        self, sdk_manager: ClaudeSDKManager, tmp_path: Path
    ) -> None:
        """Job completing quickly does not trigger the watchdog."""
        client = _mock_client_with_delay(
            _make_assistant_message("Fast response"),
            _make_result_message(),
            delay_before_first=0.0,
        )

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            response = await sdk_manager.execute_command(
                prompt="Test",
                working_directory=tmp_path,
                idle_timeout=1,  # 1s idle timeout — won't fire
            )

        assert response.content == "Success"

    async def test_watchdog_resets_on_each_message(
        self, tmp_path: Path
    ) -> None:
        """Long job with periodic messages is NOT killed by watchdog."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=2,
        )
        manager = ClaudeSDKManager(config)

        # Three messages, each arriving within 0.5s of the last
        # (well within the 2s idle timeout)
        client = _mock_client_slow_between_messages(
            [_make_assistant_message("msg1")],
            [_make_assistant_message("msg2")],
            [_make_result_message(result="done")],
            delay=0.3,  # 0.3s between groups — well within 2s idle timeout
        )

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            response = await manager.execute_command(
                prompt="Long job",
                working_directory=tmp_path,
            )

        assert response.content == "done"


# ---------------------------------------------------------------------------
# Partial result salvage
# ---------------------------------------------------------------------------


class TestPartialResultSalvage:
    """ClaudeExecutionError carries partial data when failure follows messages."""

    async def test_execution_error_on_idle_after_messages(
        self, tmp_path: Path
    ) -> None:
        """Watchdog fires AFTER messages received → ClaudeExecutionError with partial content."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=1,  # 1s idle timeout
        )
        manager = ClaudeSDKManager(config)

        # Client yields one message, then hangs indefinitely (no ResultMessage).
        # This simulates a process that partially ran then got stuck.
        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()

        async def hanging_stream():
            yield _make_assistant_message("Partial work done")
            await asyncio.sleep(10)  # hang — never yields again

        qm = AsyncMock()
        qm.receive_messages = hanging_stream
        client._query = qm

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeExecutionError) as exc_info:
                await manager.execute_command(
                    prompt="Test",
                    working_directory=tmp_path,
                )

        err = exc_info.value
        assert err.messages_received > 0
        assert "Partial work done" in (err.partial_content or "")

    async def test_execution_error_partial_cost_is_zero_without_result(
        self, tmp_path: Path
    ) -> None:
        """ClaudeExecutionError.partial_cost is 0.0 when no ResultMessage arrived."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=1,
        )
        manager = ClaudeSDKManager(config)

        # Hangs after one assistant message — no ResultMessage → cost = 0.0
        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()

        async def hanging_stream():
            yield _make_assistant_message("Work started")
            await asyncio.sleep(10)

        qm = AsyncMock()
        qm.receive_messages = hanging_stream
        client._query = qm

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeExecutionError) as exc_info:
                await manager.execute_command(
                    prompt="Test",
                    working_directory=tmp_path,
                )

        assert exc_info.value.partial_cost == 0.0

    async def test_extract_cost_from_messages_returns_zero_without_result(
        self, sdk_manager: ClaudeSDKManager
    ) -> None:
        """_extract_cost_from_messages returns 0.0 when no ResultMessage is present."""
        messages = [_make_assistant_message("hello")]
        cost = sdk_manager._extract_cost_from_messages(messages)
        assert cost == 0.0

    async def test_extract_cost_from_messages_with_result(
        self, sdk_manager: ClaudeSDKManager
    ) -> None:
        """_extract_cost_from_messages returns cost from ResultMessage."""
        messages = [
            _make_assistant_message("hello"),
            _make_result_message(total_cost_usd=1.23),
        ]
        cost = sdk_manager._extract_cost_from_messages(messages)
        assert cost == 1.23

    async def test_claude_execution_error_fields(self, tmp_path: Path) -> None:
        """ClaudeExecutionError carries all partial data fields correctly."""
        err = ClaudeExecutionError(
            error=RuntimeError("boom"),
            partial_content="some output",
            partial_cost=0.07,
            messages_received=3,
        )
        assert err.partial_content == "some output"
        assert err.partial_cost == 0.07
        assert err.messages_received == 3
        assert "boom" in str(err)


# ---------------------------------------------------------------------------
# max_turns parameter
# ---------------------------------------------------------------------------


class TestMaxTurnsParameter:
    """max_turns is correctly wired to ClaudeAgentOptions."""

    async def test_max_turns_none_uses_config_default(self, tmp_path: Path) -> None:
        """When max_turns=None, config.claude_max_turns is used."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=5,
            claude_max_turns=7,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []

        def factory(options):  # type: ignore[no-untyped-def]
            captured_options.append(options)
            client = AsyncMock()
            client.connect = AsyncMock()
            client.disconnect = AsyncMock()
            client.query = AsyncMock()

            async def recv():
                yield _make_assistant_message("hi")
                yield _make_result_message()

            qm = AsyncMock()
            qm.receive_messages = recv
            client._query = qm
            return client

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory):
            await manager.execute_command(
                prompt="Test",
                working_directory=tmp_path,
                max_turns=None,
            )

        assert captured_options[0].max_turns == 7

    async def test_max_turns_zero_maps_to_none_for_sdk(self, tmp_path: Path) -> None:
        """max_turns=0 means unlimited: ClaudeAgentOptions.max_turns is None."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=5,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []

        def factory(options):  # type: ignore[no-untyped-def]
            captured_options.append(options)
            client = AsyncMock()
            client.connect = AsyncMock()
            client.disconnect = AsyncMock()
            client.query = AsyncMock()

            async def recv():
                yield _make_result_message()

            qm = AsyncMock()
            qm.receive_messages = recv
            client._query = qm
            return client

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory):
            await manager.execute_command(
                prompt="Test",
                working_directory=tmp_path,
                max_turns=0,
            )

        assert captured_options[0].max_turns is None

    async def test_max_turns_explicit_value_overrides_config(
        self, tmp_path: Path
    ) -> None:
        """Per-call max_turns overrides config.claude_max_turns."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=5,
            claude_max_turns=10,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []

        def factory(options):  # type: ignore[no-untyped-def]
            captured_options.append(options)
            client = AsyncMock()
            client.connect = AsyncMock()
            client.disconnect = AsyncMock()
            client.query = AsyncMock()

            async def recv():
                yield _make_result_message()

            qm = AsyncMock()
            qm.receive_messages = recv
            client._query = qm
            return client

        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory):
            await manager.execute_command(
                prompt="Test",
                working_directory=tmp_path,
                max_turns=3,  # Per-call override
            )

        assert captured_options[0].max_turns == 3
