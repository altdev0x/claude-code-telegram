"""Unit tests for per-job model selection (Task 1).

Covers:
- MODEL_MAP constant validity
- Config default is "sonnet"
- CLI --model validation
- schedule update subcommand option parsing
- Model resolution in SDK integration (per-job > global default)
- Model field threaded through ScheduledEvent
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from src.cli.main import cli
from src.events.types import ScheduledEvent
from src.utils.constants import MODEL_MAP


# ---------------------------------------------------------------------------
# MODEL_MAP validation
# ---------------------------------------------------------------------------


class TestModelMap:
    """Validate the MODEL_MAP constant."""

    def test_all_expected_names_present(self) -> None:
        assert "sonnet" in MODEL_MAP
        assert "opus" in MODEL_MAP
        assert "haiku" in MODEL_MAP

    def test_model_ids_are_strings(self) -> None:
        for name, model_id in MODEL_MAP.items():
            assert isinstance(model_id, str), f"Expected str for {name!r}"
            assert model_id, f"Model ID for {name!r} must not be empty"

    def test_sonnet_maps_to_correct_id(self) -> None:
        assert MODEL_MAP["sonnet"] == "claude-sonnet-4-6"

    def test_opus_maps_to_correct_id(self) -> None:
        assert MODEL_MAP["opus"] == "claude-opus-4-6"

    def test_haiku_maps_to_correct_id(self) -> None:
        assert MODEL_MAP["haiku"] == "claude-haiku-4-5-20251001"

    def test_all_model_ids_contain_claude_prefix(self) -> None:
        for name, model_id in MODEL_MAP.items():
            assert model_id.startswith("claude-"), (
                f"Model ID for {name!r} should start with 'claude-', got {model_id!r}"
            )


# ---------------------------------------------------------------------------
# Config default
# ---------------------------------------------------------------------------


class TestConfigDefault:
    """Config claude_model default should be 'sonnet'."""

    def test_claude_model_default_is_sonnet(self, tmp_path: Path) -> None:
        from src.config.settings import Settings

        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(tmp_path),
        )
        assert settings.claude_model == "sonnet"

    def test_claude_model_can_be_overridden(self, tmp_path: Path) -> None:
        from src.config.settings import Settings

        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(tmp_path),
            claude_model="opus",
        )
        assert settings.claude_model == "opus"


# ---------------------------------------------------------------------------
# CLI --model option on schedule add
# ---------------------------------------------------------------------------


def _mock_urlopen(response_data: dict) -> MagicMock:
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


class TestCliModelOption:
    """Tests for --model on schedule add."""

    @patch("urllib.request.urlopen")
    def test_model_defaults_to_sonnet(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen({"job_id": "j1", "status": "created"})
        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})

        result = runner.invoke(
            cli,
            ["schedule", "add", "--name", "x", "--cron", "0 9 * * *", "--prompt", "hi"],
        )

        assert result.exit_code == 0
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload["model"] == "sonnet"

    @patch("urllib.request.urlopen")
    def test_model_opus_accepted(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen({"job_id": "j2", "status": "created"})
        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})

        result = runner.invoke(
            cli,
            [
                "schedule",
                "add",
                "--name",
                "x",
                "--cron",
                "0 9 * * *",
                "--prompt",
                "hi",
                "--model",
                "opus",
            ],
        )

        assert result.exit_code == 0
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload["model"] == "opus"

    @patch("urllib.request.urlopen")
    def test_model_haiku_accepted(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen({"job_id": "j3", "status": "created"})
        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})

        result = runner.invoke(
            cli,
            [
                "schedule",
                "add",
                "--name",
                "x",
                "--cron",
                "0 9 * * *",
                "--prompt",
                "hi",
                "--model",
                "haiku",
            ],
        )

        assert result.exit_code == 0
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload["model"] == "haiku"

    def test_invalid_model_rejected(self) -> None:
        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})

        result = runner.invoke(
            cli,
            [
                "schedule",
                "add",
                "--name",
                "x",
                "--cron",
                "0 9 * * *",
                "--prompt",
                "hi",
                "--model",
                "gpt-4",
            ],
        )

        assert result.exit_code != 0

    def test_claude_model_id_rejected_by_choice(self) -> None:
        """Full model IDs are not valid CLI choices; use friendly names."""
        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})

        result = runner.invoke(
            cli,
            [
                "schedule",
                "add",
                "--name",
                "x",
                "--cron",
                "0 9 * * *",
                "--prompt",
                "hi",
                "--model",
                "claude-sonnet-4-6",
            ],
        )

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# schedule update subcommand
# ---------------------------------------------------------------------------


class TestScheduleUpdateSubcommand:
    """Tests for schedule update <job-id> [OPTIONS]."""

    @patch("urllib.request.urlopen")
    def test_update_name(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen({"job_id": "j1", "status": "updated"})
        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})

        result = runner.invoke(
            cli, ["schedule", "update", "j1", "--name", "New Name"]
        )

        assert result.exit_code == 0
        assert "updated" in result.output.lower()
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "PATCH"
        payload = json.loads(req.data.decode())
        assert payload == {"job_name": "New Name"}

    @patch("urllib.request.urlopen")
    def test_update_model(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen({"job_id": "j1", "status": "updated"})
        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})

        result = runner.invoke(
            cli, ["schedule", "update", "j1", "--model", "haiku"]
        )

        assert result.exit_code == 0
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload == {"model": "haiku"}

    @patch("urllib.request.urlopen")
    def test_update_multiple_fields(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen({"job_id": "j1", "status": "updated"})
        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})

        result = runner.invoke(
            cli,
            [
                "schedule",
                "update",
                "j1",
                "--model",
                "opus",
                "--cron",
                "0 10 * * *",
                "--prompt",
                "new prompt",
            ],
        )

        assert result.exit_code == 0
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload["model"] == "opus"
        assert payload["cron_expression"] == "0 10 * * *"
        assert payload["prompt"] == "new prompt"

    @patch("urllib.request.urlopen")
    def test_update_active_flag(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen({"job_id": "j1", "status": "updated"})
        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})

        result = runner.invoke(
            cli, ["schedule", "update", "j1", "--inactive"]
        )

        assert result.exit_code == 0
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload["is_active"] is False

    def test_update_no_options_exits_nonzero(self) -> None:
        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})
        result = runner.invoke(cli, ["schedule", "update", "j1"])
        assert result.exit_code != 0

    def test_update_invalid_model_rejected(self) -> None:
        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})
        result = runner.invoke(
            cli, ["schedule", "update", "j1", "--model", "invalid-model"]
        )
        assert result.exit_code != 0

    def test_update_help_shows_in_schedule_group(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "--help"])
        assert result.exit_code == 0
        assert "update" in result.output


# ---------------------------------------------------------------------------
# Model resolution in SDK integration
# ---------------------------------------------------------------------------


class TestModelResolution:
    """Tests for model resolution logic in ClaudeSDKManager.execute_command."""

    def _make_manager(self, claude_model: str = "sonnet") -> "ClaudeSDKManager":
        from src.claude.sdk_integration import ClaudeSDKManager

        config = MagicMock()
        config.claude_model = claude_model
        config.claude_max_turns = 5
        config.claude_allowed_tools = []
        config.claude_disallowed_tools = []
        config.claude_cli_path = None
        config.claude_idle_timeout_seconds = 60
        config.enable_mcp = False
        config.anthropic_api_key_str = None
        return ClaudeSDKManager(config)

    @patch("src.claude.sdk_integration.ClaudeSDKClient")
    @pytest.mark.asyncio
    async def test_per_job_model_overrides_global(
        self, mock_client_cls: MagicMock
    ) -> None:
        """When model='opus' is passed, it should resolve to claude-opus-4-6."""
        manager = self._make_manager(claude_model="sonnet")

        captured_options: list = []

        async def fake_receive():  # type: ignore[return]
            # async generator that yields nothing
            return
            yield  # noqa: unreachable

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.query = AsyncMock()
        mock_client._query = MagicMock()
        mock_client._query.receive_messages = MagicMock(return_value=fake_receive())
        mock_client.disconnect = AsyncMock()
        mock_client_cls.side_effect = lambda opts: (
            captured_options.append(opts) or mock_client
        )

        # Command runs to completion (no ResultMessage → empty response)
        await manager.execute_command(
            prompt="hello",
            working_directory=Path("/tmp"),
            model="opus",
        )

        assert len(captured_options) == 1
        assert captured_options[0].model == "claude-opus-4-6"

    @patch("src.claude.sdk_integration.ClaudeSDKClient")
    @pytest.mark.asyncio
    async def test_global_default_used_when_no_job_model(
        self, mock_client_cls: MagicMock
    ) -> None:
        """When model=None, config.claude_model (sonnet) is used as fallback."""
        manager = self._make_manager(claude_model="sonnet")

        captured_options: list = []

        async def fake_receive():  # type: ignore[return]
            return
            yield  # noqa: unreachable

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.query = AsyncMock()
        mock_client._query = MagicMock()
        mock_client._query.receive_messages = MagicMock(return_value=fake_receive())
        mock_client.disconnect = AsyncMock()
        mock_client_cls.side_effect = lambda opts: (
            captured_options.append(opts) or mock_client
        )

        await manager.execute_command(
            prompt="hello",
            working_directory=Path("/tmp"),
        )

        assert len(captured_options) == 1
        assert captured_options[0].model == "claude-sonnet-4-6"

    def test_model_map_lookup_falls_back_to_raw_id(self) -> None:
        """Unknown friendly names are passed through as-is (backward compat)."""
        raw = "claude-3-5-sonnet-20241022"
        result = MODEL_MAP.get(raw, raw)
        assert result == raw

    def test_haiku_resolves_correctly(self) -> None:
        raw = "haiku"
        assert MODEL_MAP.get(raw, raw) == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Model field on ScheduledEvent
# ---------------------------------------------------------------------------


class TestScheduledEventModel:
    """ScheduledEvent should carry an optional model field."""

    def test_model_defaults_to_none(self) -> None:
        event = ScheduledEvent(job_id="j1", job_name="test", prompt="hi")
        assert event.model is None

    def test_model_can_be_set(self) -> None:
        event = ScheduledEvent(
            job_id="j1",
            job_name="test",
            prompt="hi",
            model="opus",
        )
        assert event.model == "opus"

    def test_model_flows_through_scheduled_event(self) -> None:
        """Confirm model is a proper dataclass field."""
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(ScheduledEvent)}
        assert "model" in field_names
