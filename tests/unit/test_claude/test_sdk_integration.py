"""Test Claude SDK integration."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from src.claude.sdk_integration import (
    ClaudeResponse,
    ClaudeSDKManager,
    StreamUpdate,
)
from src.config.settings import Settings


@pytest.fixture(autouse=True)
def _patch_parse_message():
    """Patch parse_message as identity so mocks can yield typed Message objects."""
    with patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x):
        yield


def _make_assistant_message(text="Test response"):
    """Create an AssistantMessage with proper structure for current SDK version."""
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-20250514",
    )


def _make_result_message(**kwargs):
    """Create a ResultMessage with sensible defaults."""
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


def _mock_client(*messages):
    """Create a mock ClaudeSDKClient that yields the given messages.

    Returns a factory function suitable for patching ClaudeSDKClient.
    Uses connect()/disconnect() pattern (not async context manager).
    """
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def receive_raw_messages():
        for msg in messages:
            yield msg

    query_mock = AsyncMock()
    query_mock.receive_messages = receive_raw_messages
    client._query = query_mock

    return client


def _mock_client_factory(*messages, capture_options=None):
    """Create a factory that returns a mock client, optionally capturing options."""

    def factory(options):
        if capture_options is not None:
            capture_options.append(options)
        return _mock_client(*messages)

    return factory


class TestClaudeSDKManager:
    """Test Claude SDK manager."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config without API key."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=2,  # Short timeout for testing
        )

    @pytest.fixture
    def sdk_manager(self, config):
        """Create SDK manager."""
        return ClaudeSDKManager(config)

    async def test_sdk_manager_initialization_with_api_key(self, tmp_path):
        """Test SDK manager initialization with API key."""
        from src.config.settings import Settings

        # Test with API key provided
        config_with_key = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            anthropic_api_key="test-api-key",
            claude_idle_timeout_seconds=2,
        )

        # Store original env var
        original_api_key = os.environ.get("ANTHROPIC_API_KEY")

        try:
            ClaudeSDKManager(config_with_key)

            # Check that API key was set in environment
            assert os.environ.get("ANTHROPIC_API_KEY") == "test-api-key"

        finally:
            # Restore original env var
            if original_api_key:
                os.environ["ANTHROPIC_API_KEY"] = original_api_key
            elif "ANTHROPIC_API_KEY" in os.environ:
                del os.environ["ANTHROPIC_API_KEY"]

    async def test_sdk_manager_initialization_without_api_key(self, config):
        """Test SDK manager initialization without API key (uses CLI auth)."""
        # Store original env var
        original_api_key = os.environ.get("ANTHROPIC_API_KEY")

        try:
            # Remove any existing API key
            if "ANTHROPIC_API_KEY" in os.environ:
                del os.environ["ANTHROPIC_API_KEY"]

            ClaudeSDKManager(config)

            # Check that no API key was set (should use CLI auth)
            assert config.anthropic_api_key_str is None

        finally:
            # Restore original env var
            if original_api_key:
                os.environ["ANTHROPIC_API_KEY"] = original_api_key

    async def test_execute_command_success(self, sdk_manager):
        """Test successful command execution."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="test-session", total_cost_usd=0.05),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                session_id="test-session",
            )

        # Verify response
        assert isinstance(response, ClaudeResponse)
        assert response.session_id == "test-session"
        assert response.duration_ms >= 0
        assert not response.is_error
        assert response.cost == 0.05

    async def test_execute_command_uses_result_content(self, sdk_manager):
        """Test that ResultMessage.result is used for content when available."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Assistant text"),
            _make_result_message(result="Final result from ResultMessage"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.content == "Final result from ResultMessage"

    async def test_execute_command_falls_back_to_messages(self, sdk_manager):
        """Test fallback to message extraction when result is None."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Extracted from messages"),
            _make_result_message(result=None),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.content == "Extracted from messages"

    async def test_execute_command_with_streaming(self, sdk_manager):
        """Test command execution with streaming callback."""
        stream_updates = []

        async def stream_callback(update: StreamUpdate):
            stream_updates.append(update)

        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                stream_callback=stream_callback,
            )

        # Verify streaming was called
        assert len(stream_updates) > 0
        assert any(update.type == "assistant" for update in stream_updates)

    async def test_execute_command_timeout(self, sdk_manager):
        """Test command execution timeout."""
        from src.claude.exceptions import ClaudeTimeoutError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()

        async def hanging_receive():
            await asyncio.sleep(5)  # Exceeds 2s timeout
            yield  # Never reached

        query_mock = AsyncMock()
        query_mock.receive_messages = hanging_receive
        client._query = query_mock

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeTimeoutError):
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

    def test_get_active_process_count(self, sdk_manager):
        """Test active process count is always 0."""
        assert sdk_manager.get_active_process_count() == 0

    async def test_execute_command_passes_mcp_config(self, tmp_path):
        """Test that MCP config is passed to ClaudeAgentOptions when enabled."""
        # Create a valid MCP config file
        mcp_config_file = tmp_path / "mcp_config.json"
        mcp_config_file.write_text(
            '{"mcpServers": {"test-server": {"command": "echo", "args": ["hello"]}}}'
        )

        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=2,
            enable_mcp=True,
            mcp_config_path=str(mcp_config_file),
        )

        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        # Verify MCP config was parsed and passed as dict to options
        assert len(captured_options) == 1
        assert captured_options[0].mcp_servers == {
            "test-server": {"command": "echo", "args": ["hello"]}
        }

    async def test_execute_command_no_mcp_when_disabled(self, sdk_manager):
        """Test that MCP config is NOT passed when MCP is disabled."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        # Verify MCP config was NOT set (should be empty default)
        assert len(captured_options) == 1
        assert captured_options[0].mcp_servers == {}

    async def test_execute_command_passes_resume_session(self, sdk_manager):
        """Test that session_id is passed as options.resume for continuation."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="test-session"),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Continue working",
                working_directory=Path("/test"),
                session_id="existing-session-id",
                continue_session=True,
            )

        assert len(captured_options) == 1
        assert captured_options[0].resume == "existing-session-id"

    async def test_execute_command_no_resume_for_new_session(self, sdk_manager):
        """Test that resume is not set for new sessions."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="new-session"),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="New prompt",
                working_directory=Path("/test"),
                session_id=None,
                continue_session=False,
            )

        assert len(captured_options) == 1
        assert (
            not hasattr(captured_options[0], "resume") or not captured_options[0].resume
        )


class TestClaudeAgentOptionsWiring:
    """Test system_prompt, allowed/disallowed tools, and sandbox absence on ClaudeAgentOptions."""

    async def test_system_prompt_uses_preset_dict(self, tmp_path):
        """Test that system_prompt is a Claude Code preset dict."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=2,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        sp = captured_options[0].system_prompt
        assert isinstance(sp, dict)
        assert sp["type"] == "preset"
        assert sp["preset"] == "claude_code"
        assert str(tmp_path) in sp["append"]
        assert "relative paths" in sp["append"].lower()

    async def test_disallowed_tools_passed_to_options(self, tmp_path):
        """Test that disallowed_tools from config are passed to ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=2,
            claude_disallowed_tools=["WebFetch", "WebSearch"],
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].disallowed_tools == ["WebFetch", "WebSearch"]

    async def test_allowed_tools_passed_to_options(self, tmp_path):
        """Test that allowed_tools from config are passed to ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=2,
            claude_allowed_tools=["Read", "Write", "Bash"],
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].allowed_tools == ["Read", "Write", "Bash"]

    async def test_no_sandbox_in_options(self, tmp_path):
        """Test that sandbox is not set on ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=2,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        # sandbox should be the default (empty/not set), not explicitly configured
        opts = captured_options[0]
        assert not getattr(opts, "sandbox", None)


class TestClaudeMCPErrors:
    """Test MCP-specific error handling."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=2,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        """Create SDK manager."""
        return ClaudeSDKManager(config)

    async def test_mcp_connection_error_raises_mcp_error(self, sdk_manager):
        """Test that MCP connection errors raise ClaudeMCPError."""
        from claude_agent_sdk import CLIConnectionError

        from src.claude.exceptions import ClaudeMCPError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock(
            side_effect=CLIConnectionError("MCP server failed to start")
        )

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeMCPError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        assert "MCP server" in str(exc_info.value)

    async def test_mcp_process_error_raises_mcp_error(self, sdk_manager):
        """Test that MCP process errors raise ClaudeMCPError."""
        from claude_agent_sdk import ProcessError

        from src.claude.exceptions import ClaudeMCPError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock(
            side_effect=ProcessError("Failed to start MCP server: connection refused")
        )

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeMCPError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        assert "MCP" in str(exc_info.value)


class TestPermissionDeniedStream:
    """Test ToolResultBlock(is_error=True) extraction from SDK stream."""

    @pytest.fixture
    def config(self, tmp_path):
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=2,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    async def test_permission_denied_stream_update(self, sdk_manager):
        """Verify StreamUpdate(type='permission_denied') is emitted for error ToolResultBlock."""
        error_block = ToolResultBlock(
            tool_use_id="tool-123",
            content="Access denied: /etc/passwd is outside allowed paths",
            is_error=True,
        )
        user_msg = UserMessage(content=[error_block])

        mock_factory = _mock_client_factory(
            _make_assistant_message("Let me read that file"),
            user_msg,
            _make_assistant_message("I was denied access"),
            _make_result_message(result="Could not read file"),
        )

        stream_updates: list[StreamUpdate] = []

        async def stream_cb(update: StreamUpdate) -> None:
            stream_updates.append(update)

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Read /etc/passwd",
                working_directory=Path("/test"),
                stream_callback=stream_cb,
            )

        denied = [u for u in stream_updates if u.type == "permission_denied"]
        assert len(denied) == 1
        assert "Access denied" in denied[0].content
        assert denied[0].metadata["tool_use_id"] == "tool-123"

    async def test_permission_denied_content_extracted(self, sdk_manager):
        """Verify error text is captured verbatim from ToolResultBlock."""
        error_text = "User denied access to Read on /etc/shadow"
        error_block = ToolResultBlock(
            tool_use_id="tool-456",
            content=error_text,
            is_error=True,
        )
        user_msg = UserMessage(content=[error_block])

        mock_factory = _mock_client_factory(
            user_msg,
            _make_assistant_message("Understood"),
            _make_result_message(result="Denied"),
        )

        stream_updates: list[StreamUpdate] = []

        async def stream_cb(update: StreamUpdate) -> None:
            stream_updates.append(update)

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test",
                working_directory=Path("/test"),
                stream_callback=stream_cb,
            )

        denied = [u for u in stream_updates if u.type == "permission_denied"]
        assert len(denied) == 1
        assert denied[0].content == error_text


class TestSessionIdFallback:
    """Test fallback session ID extraction from StreamEvent messages."""

    @pytest.fixture
    def config(self, tmp_path):
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=2,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    async def test_session_id_from_stream_event_fallback(self, sdk_manager):
        """Test that session_id is extracted from StreamEvent when ResultMessage has None."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-123",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "stream-session-123"

    async def test_session_id_from_stream_event_empty_string(self, sdk_manager):
        """Test fallback triggers when ResultMessage session_id is empty string."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-456",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id="", result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "stream-session-456"

    async def test_no_fallback_when_result_has_session_id(self, sdk_manager):
        """Test that ResultMessage session_id takes priority over StreamEvent."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-999",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id="result-session-abc", result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        # ResultMessage session_id should win
        assert response.session_id == "result-session-abc"

    async def test_fallback_skips_stream_events_without_session_id(self, sdk_manager):
        """Test that StreamEvents without session_id are skipped in fallback."""
        stream_event_no_id = StreamEvent(
            uuid="evt-1",
            session_id=None,
            event={"type": "content_block_start"},
        )
        stream_event_with_id = StreamEvent(
            uuid="evt-2",
            session_id="found-session",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event_no_id,
            stream_event_with_id,
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "found-session"

    async def test_no_session_id_anywhere_falls_back_to_input(self, sdk_manager):
        """Test that input session_id is used when neither ResultMessage nor StreamEvent provide one."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                session_id="input-session-id",
            )

        # Should fall back to the input session_id
        assert response.session_id == "input-session-id"


class TestBuildSystemPrompt:
    """Test ClaudeSDKManager._build_system_prompt()."""

    @pytest.fixture
    def sdk_manager(self, tmp_path):
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_idle_timeout_seconds=2,
        )
        return ClaudeSDKManager(config)

    def test_returns_preset_dict(self, sdk_manager):
        """Return value is a dict with type, preset, and append keys."""
        result = sdk_manager._build_system_prompt(Path("/work"), session_id=None)
        assert isinstance(result, dict)
        assert result["type"] == "preset"
        assert result["preset"] == "claude_code"
        assert "append" in result

    def test_without_session_id(self, sdk_manager):
        """Without session_id, append contains working dir and interface but no session line."""
        result = sdk_manager._build_system_prompt(Path("/work"), session_id=None)
        append = result["append"]
        assert "/work" in append
        assert "Interface: Telegram chat" in append
        assert "session ID" not in append

    def test_with_session_id(self, sdk_manager):
        """With session_id, append includes all three parts."""
        result = sdk_manager._build_system_prompt(
            Path("/work"), session_id="sess-abc-123"
        )
        append = result["append"]
        assert "/work" in append
        assert "Interface: Telegram chat" in append
        assert "Your session ID is: sess-abc-123" in append

    def test_interface_always_present(self, sdk_manager):
        """Interface: Telegram chat is always in append regardless of session_id."""
        for sid in [None, "", "some-id"]:
            result = sdk_manager._build_system_prompt(Path("/work"), session_id=sid)
            assert "Interface: Telegram chat" in result["append"]
