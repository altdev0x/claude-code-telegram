"""Tests for CLI commands."""

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from src.cli.main import cli
from src.cli.schedule import _get_api_url


class TestCLIGroup:
    """Test the top-level CLI group."""

    def test_help_output(self) -> None:
        """CLI --help works."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Claude Code Telegram Bot" in result.output

    def test_schedule_help(self) -> None:
        """schedule --help lists subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "--help"])
        assert result.exit_code == 0
        assert "add" in result.output
        assert "list" in result.output
        assert "remove" in result.output
        assert "history" in result.output


class TestServiceCommands:
    """Test service lifecycle commands."""

    @patch("src.cli.service.subprocess.run")
    def test_start_calls_systemctl(self, mock_run: MagicMock) -> None:
        """start command calls systemctl --user start."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        runner = CliRunner()
        result = runner.invoke(cli, ["start"])
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["systemctl", "--user", "start", "claude-telegram-bot"]

    @patch("src.cli.service.subprocess.run")
    def test_stop_calls_systemctl(self, mock_run: MagicMock) -> None:
        """stop command calls systemctl --user stop."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        runner = CliRunner()
        result = runner.invoke(cli, ["stop"])
        cmd = mock_run.call_args[0][0]
        assert cmd == ["systemctl", "--user", "stop", "claude-telegram-bot"]

    @patch("src.cli.service.subprocess.run")
    def test_logs_with_follow(self, mock_run: MagicMock) -> None:
        """logs -f passes --follow to journalctl."""
        mock_run.return_value = MagicMock(returncode=0)
        runner = CliRunner()
        result = runner.invoke(cli, ["logs", "-f"])
        cmd = mock_run.call_args[0][0]
        assert "-f" in cmd
        assert "-u" in cmd


def _mock_urlopen(response_data: dict, status: int = 200):
    """Create a mock for urllib.request.urlopen."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


class TestScheduleCommands:
    """Test schedule subcommands."""

    @patch("urllib.request.urlopen")
    def test_add_job(self, mock_urlopen: MagicMock) -> None:
        """schedule add sends POST and shows job ID."""
        mock_urlopen.return_value = _mock_urlopen(
            {"job_id": "abc-123", "status": "created"}
        )

        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})
        result = runner.invoke(
            cli,
            [
                "schedule",
                "add",
                "--name",
                "Daily check",
                "--cron",
                "0 9 * * *",
                "--prompt",
                "Run tests",
            ],
        )

        assert result.exit_code == 0
        assert "abc-123" in result.output

    @patch("urllib.request.urlopen")
    def test_list_jobs_empty(self, mock_urlopen: MagicMock) -> None:
        """schedule list with no jobs shows message."""
        mock_urlopen.return_value = _mock_urlopen({"jobs": []})

        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})
        result = runner.invoke(cli, ["schedule", "list"])

        assert result.exit_code == 0
        assert "No scheduled jobs" in result.output

    @patch("urllib.request.urlopen")
    def test_list_jobs_with_results(self, mock_urlopen: MagicMock) -> None:
        """schedule list shows job details."""
        mock_urlopen.return_value = _mock_urlopen(
            {
                "jobs": [
                    {
                        "job_id": "job-1",
                        "job_name": "Health check",
                        "cron_expression": "0 9 * * *",
                        "session_mode": "isolated",
                    }
                ]
            }
        )

        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})
        result = runner.invoke(cli, ["schedule", "list"])

        assert result.exit_code == 0
        assert "job-1" in result.output
        assert "Health check" in result.output

    @patch("urllib.request.urlopen")
    def test_remove_job(self, mock_urlopen: MagicMock) -> None:
        """schedule remove sends DELETE."""
        mock_urlopen.return_value = _mock_urlopen(
            {"job_id": "job-1", "status": "removed"}
        )

        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})
        result = runner.invoke(cli, ["schedule", "remove", "job-1"])

        assert result.exit_code == 0
        assert "removed" in result.output

    @patch("urllib.request.urlopen")
    def test_history(self, mock_urlopen: MagicMock) -> None:
        """schedule history shows run details."""
        mock_urlopen.return_value = _mock_urlopen(
            {
                "job_id": "job-1",
                "runs": [
                    {
                        "fired_at": "2026-02-23T09:00:00",
                        "success": True,
                        "cost": 0.05,
                        "error_message": None,
                    }
                ],
            }
        )

        runner = CliRunner(env={"WEBHOOK_API_SECRET": "test"})
        result = runner.invoke(cli, ["schedule", "history", "job-1"])

        assert result.exit_code == 0
        assert "OK" in result.output
        assert "0.0500" in result.output

    def test_missing_secret_shows_error(self) -> None:
        """schedule commands fail gracefully without WEBHOOK_API_SECRET."""
        runner = CliRunner(env={"WEBHOOK_API_SECRET": ""})
        result = runner.invoke(cli, ["schedule", "list"])

        assert result.exit_code != 0
        assert "WEBHOOK_API_SECRET" in result.output


class TestApiUrl:
    """Test API URL construction."""

    def test_default_port(self) -> None:
        assert "8080" in _get_api_url()

    @patch.dict("os.environ", {"API_SERVER_PORT": "9090"})
    def test_custom_port(self) -> None:
        assert "9090" in _get_api_url()
