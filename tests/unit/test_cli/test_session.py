"""Tests for session CLI commands — JSONL parsing and rendering."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from src.cli.session import (
    _entry_timestamp,
    _find_session_jsonl,
    _get_tool_use_ids,
    _is_tool_result,
    _is_tool_use,
    _parse_jsonl,
    _render_bash_pair,
    _render_entry,
    _summarize_tool_input,
    session,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, entries: List[Dict[str, Any]]) -> None:
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


SAMPLE_USER_TEXT = {
    "role": "user",
    "content": "What files are in this directory?",
    "timestamp": "2026-02-24T10:00:00Z",
}

SAMPLE_ASSISTANT_TEXT = {
    "role": "assistant",
    "content": [
        {"type": "text", "text": "Let me check that for you."},
    ],
    "timestamp": "2026-02-24T10:00:01Z",
}

SAMPLE_TOOL_USE = {
    "role": "assistant",
    "content": [
        {
            "type": "tool_use",
            "id": "tu_abc12345",
            "name": "Bash",
            "input": {"command": "ls -la"},
        },
    ],
    "timestamp": "2026-02-24T10:00:02Z",
}

SAMPLE_TOOL_RESULT = {
    "role": "user",
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": "tu_abc12345",
            "content": [
                {"type": "text", "text": "file1.txt\nfile2.txt\n"},
            ],
        },
    ],
    "timestamp": "2026-02-24T10:00:03Z",
}

SAMPLE_THINKING = {
    "role": "assistant",
    "content": [
        {
            "type": "thinking",
            "thinking": "I need to list the files.\nLet me use Bash.",
        },
    ],
    "timestamp": "2026-02-24T10:00:04Z",
}

SAMPLE_PROGRESS = {
    "role": "progress",
    "timestamp": "2026-02-24T10:00:05Z",
}

SAMPLE_SYSTEM = {
    "role": "system",
    "content": [{"duration": 2.5}],
    "timestamp": "2026-02-24T10:00:06Z",
}

SAMPLE_READ_TOOL = {
    "role": "assistant",
    "content": [
        {
            "type": "tool_use",
            "id": "tu_read999",
            "name": "Read",
            "input": {"file_path": "/home/test/README.md"},
        },
    ],
    "timestamp": "2026-02-24T10:00:07Z",
}


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------


class TestParseJsonl:
    def test_parse_valid_entries(self, tmp_path: Path) -> None:
        p = tmp_path / "test.jsonl"
        _write_jsonl(p, [SAMPLE_USER_TEXT, SAMPLE_ASSISTANT_TEXT])
        entries = _parse_jsonl(p)
        assert len(entries) == 2
        assert entries[0]["role"] == "user"
        assert entries[1]["role"] == "assistant"

    def test_parse_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert _parse_jsonl(p) == []

    def test_parse_skips_invalid_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "mixed.jsonl"
        p.write_text(
            json.dumps(SAMPLE_USER_TEXT) + "\n"
            + "not-json\n"
            + json.dumps(SAMPLE_ASSISTANT_TEXT) + "\n"
        )
        entries = _parse_jsonl(p)
        assert len(entries) == 2


class TestFindSessionJsonl:
    def test_finds_transcript(self, tmp_path: Path) -> None:
        project_dir = tmp_path / ".claude" / "projects" / "-home-test-project"
        project_dir.mkdir(parents=True)
        jsonl = project_dir / "sess-abc-123.jsonl"
        jsonl.write_text("{}\n")

        with patch("src.cli.session.Path.home", return_value=tmp_path):
            result = _find_session_jsonl("sess-abc-123")
        assert result == jsonl

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        project_dir = tmp_path / ".claude" / "projects" / "-home-test"
        project_dir.mkdir(parents=True)

        with patch("src.cli.session.Path.home", return_value=tmp_path):
            result = _find_session_jsonl("nonexistent-id")
        assert result is None

    def test_returns_none_when_no_projects_dir(self, tmp_path: Path) -> None:
        with patch("src.cli.session.Path.home", return_value=tmp_path):
            result = _find_session_jsonl("any-id")
        assert result is None


# ---------------------------------------------------------------------------
# Entry rendering
# ---------------------------------------------------------------------------


class TestEntryTimestamp:
    def test_extracts_iso_timestamp(self) -> None:
        assert _entry_timestamp(SAMPLE_USER_TEXT) == "10:00:00"

    def test_fallback_for_missing_timestamp(self) -> None:
        assert _entry_timestamp({"role": "user", "content": "hi"}) == "??:??:??"


class TestRenderEntry:
    def test_user_text(self) -> None:
        rendered = _render_entry(SAMPLE_USER_TEXT)
        assert rendered is not None
        assert "USER:" in rendered
        assert "What files" in rendered

    def test_assistant_text(self) -> None:
        rendered = _render_entry(SAMPLE_ASSISTANT_TEXT)
        assert rendered is not None
        assert "ASSISTANT:" in rendered
        assert "check that" in rendered

    def test_tool_use(self) -> None:
        rendered = _render_entry(SAMPLE_TOOL_USE)
        assert rendered is not None
        assert "TOOL Bash:" in rendered
        assert "ls -la" in rendered

    def test_tool_result(self) -> None:
        rendered = _render_entry(SAMPLE_TOOL_RESULT)
        assert rendered is not None
        assert "RESULT" in rendered

    def test_thinking_collapsed_by_default(self) -> None:
        rendered = _render_entry(SAMPLE_THINKING, verbose=False)
        assert rendered is not None
        assert "THINKING:" in rendered
        assert "I need to list the files." in rendered
        assert "..." in rendered

    def test_thinking_expanded_in_verbose(self) -> None:
        rendered = _render_entry(SAMPLE_THINKING, verbose=True)
        assert rendered is not None
        assert "Let me use Bash." in rendered

    def test_progress_skipped_by_default(self) -> None:
        rendered = _render_entry(SAMPLE_PROGRESS, verbose=False)
        assert rendered is None

    def test_progress_shown_in_verbose(self) -> None:
        rendered = _render_entry(SAMPLE_PROGRESS, verbose=True)
        assert rendered is not None
        assert "progress" in rendered

    def test_system_without_duration_skipped_by_default(self) -> None:
        entry = {
            "role": "system",
            "content": [{"info": "something"}],
            "timestamp": "2026-02-24T10:00:06Z",
        }
        rendered = _render_entry(entry, verbose=False)
        assert rendered is None

    def test_system_duration_shown(self) -> None:
        rendered = _render_entry(SAMPLE_SYSTEM, verbose=False)
        assert rendered is not None
        assert "duration" in rendered


# ---------------------------------------------------------------------------
# Tool detection helpers
# ---------------------------------------------------------------------------


class TestToolDetection:
    def test_is_tool_use_any(self) -> None:
        assert _is_tool_use(SAMPLE_TOOL_USE) is True
        assert _is_tool_use(SAMPLE_USER_TEXT) is False

    def test_is_tool_use_specific(self) -> None:
        assert _is_tool_use(SAMPLE_TOOL_USE, "Bash") is True
        assert _is_tool_use(SAMPLE_TOOL_USE, "Read") is False

    def test_is_tool_result(self) -> None:
        assert _is_tool_result(SAMPLE_TOOL_RESULT) is True
        assert _is_tool_result(SAMPLE_USER_TEXT) is False

    def test_get_tool_use_ids(self) -> None:
        ids = _get_tool_use_ids(SAMPLE_TOOL_USE)
        assert ids == ["tu_abc12345"]

    def test_get_tool_use_ids_empty(self) -> None:
        assert _get_tool_use_ids(SAMPLE_USER_TEXT) == []


class TestSummarizeToolInput:
    def test_string_input(self) -> None:
        assert _summarize_tool_input("hello world") == "hello world"

    def test_command_dict(self) -> None:
        assert _summarize_tool_input({"command": "ls -la"}) == "ls -la"

    def test_file_path_dict(self) -> None:
        result = _summarize_tool_input({"file_path": "/tmp/foo.py"})
        assert "/tmp/foo.py" in result

    def test_edit_dict(self) -> None:
        result = _summarize_tool_input(
            {"file_path": "/tmp/foo.py", "old_string": "x"}
        )
        assert "(edit)" in result

    def test_pattern_dict(self) -> None:
        result = _summarize_tool_input({"pattern": "*.py"})
        assert "pattern=" in result

    def test_truncation(self) -> None:
        long_cmd = "x" * 200
        result = _summarize_tool_input(long_cmd, max_len=50)
        assert len(result) == 50


# ---------------------------------------------------------------------------
# Bash pair rendering
# ---------------------------------------------------------------------------


class TestRenderBashPair:
    def test_with_result(self) -> None:
        rendered = _render_bash_pair(SAMPLE_TOOL_USE, SAMPLE_TOOL_RESULT)
        assert "$ ls -la" in rendered
        assert "file1.txt" in rendered

    def test_without_result(self) -> None:
        rendered = _render_bash_pair(SAMPLE_TOOL_USE, None)
        assert "$ ls -la" in rendered
        assert "file1.txt" not in rendered


# ---------------------------------------------------------------------------
# CLI inspect command (local, no API)
# ---------------------------------------------------------------------------


class TestInspectCommand:
    def test_inspect_not_found(self) -> None:
        runner = CliRunner()
        with patch("src.cli.session._find_session_jsonl", return_value=None):
            result = runner.invoke(session, ["inspect", "nonexistent-id"])
        assert result.exit_code != 0
        assert "No transcript found" in result.output

    def test_inspect_full_timeline(self, tmp_path: Path) -> None:
        p = tmp_path / "session.jsonl"
        _write_jsonl(p, [SAMPLE_USER_TEXT, SAMPLE_ASSISTANT_TEXT, SAMPLE_TOOL_USE])

        runner = CliRunner()
        with patch("src.cli.session._find_session_jsonl", return_value=p):
            result = runner.invoke(session, ["inspect", "test-session-id"])

        assert result.exit_code == 0
        assert "USER:" in result.output
        assert "ASSISTANT:" in result.output
        assert "TOOL Bash:" in result.output

    def test_inspect_bash_only(self, tmp_path: Path) -> None:
        p = tmp_path / "session.jsonl"
        _write_jsonl(
            p, [SAMPLE_USER_TEXT, SAMPLE_TOOL_USE, SAMPLE_TOOL_RESULT]
        )

        runner = CliRunner()
        with patch("src.cli.session._find_session_jsonl", return_value=p):
            result = runner.invoke(
                session, ["inspect", "test-session-id", "--bash-only"]
            )

        assert result.exit_code == 0
        assert "$ ls -la" in result.output
        assert "USER:" not in result.output

    def test_inspect_tools_only(self, tmp_path: Path) -> None:
        p = tmp_path / "session.jsonl"
        _write_jsonl(
            p, [SAMPLE_USER_TEXT, SAMPLE_TOOL_USE, SAMPLE_READ_TOOL]
        )

        runner = CliRunner()
        with patch("src.cli.session._find_session_jsonl", return_value=p):
            result = runner.invoke(
                session, ["inspect", "test-session-id", "--tools-only"]
            )

        assert result.exit_code == 0
        assert "TOOL Bash:" in result.output
        assert "TOOL Read:" in result.output
        assert "USER:" not in result.output

    def test_inspect_tail(self, tmp_path: Path) -> None:
        p = tmp_path / "session.jsonl"
        entries = [
            {**SAMPLE_USER_TEXT, "content": f"Message {i}"}
            for i in range(10)
        ]
        _write_jsonl(p, entries)

        runner = CliRunner()
        with patch("src.cli.session._find_session_jsonl", return_value=p):
            result = runner.invoke(
                session, ["inspect", "test-session-id", "--tail", "3"]
            )

        assert result.exit_code == 0
        assert "Message 7" in result.output
        assert "Message 8" in result.output
        assert "Message 9" in result.output
        assert "Message 6" not in result.output

    def test_inspect_json_output(self, tmp_path: Path) -> None:
        p = tmp_path / "session.jsonl"
        _write_jsonl(p, [SAMPLE_USER_TEXT])

        runner = CliRunner()
        with patch("src.cli.session._find_session_jsonl", return_value=p):
            result = runner.invoke(
                session, ["inspect", "test-session-id", "--json"]
            )

        assert result.exit_code == 0
        parsed = json.loads(result.output.strip())
        assert parsed["role"] == "user"

    def test_inspect_empty_transcript(self, tmp_path: Path) -> None:
        p = tmp_path / "session.jsonl"
        p.write_text("")

        runner = CliRunner()
        with patch("src.cli.session._find_session_jsonl", return_value=p):
            result = runner.invoke(session, ["inspect", "test-session-id"])

        assert result.exit_code == 0
        assert "empty" in result.output.lower()


# ---------------------------------------------------------------------------
# CLI list / send / response (require API — test connection error handling)
# ---------------------------------------------------------------------------


class TestCliApiCommands:
    def test_list_no_service(self) -> None:
        """list command fails gracefully when service is down."""
        runner = CliRunner()
        env = {"WEBHOOK_API_SECRET": "test", "API_SERVER_PORT": "59999"}
        result = runner.invoke(session, ["list"], env=env)
        assert result.exit_code != 0
        assert "Cannot connect" in result.output or "Error" in result.output

    def test_send_no_service(self) -> None:
        """send command fails gracefully when service is down."""
        runner = CliRunner()
        env = {"WEBHOOK_API_SECRET": "test", "API_SERVER_PORT": "59999"}
        result = runner.invoke(
            session, ["send", "-m", "hello"], env=env
        )
        assert result.exit_code != 0

    def test_response_no_service(self) -> None:
        """response command fails gracefully when service is down."""
        runner = CliRunner()
        env = {"WEBHOOK_API_SECRET": "test", "API_SERVER_PORT": "59999"}
        result = runner.invoke(
            session, ["response", "sess-123"], env=env
        )
        assert result.exit_code != 0

    def test_list_no_secret(self) -> None:
        """list command fails when WEBHOOK_API_SECRET is not set."""
        runner = CliRunner()
        env = {"WEBHOOK_API_SECRET": "", "API_SERVER_PORT": "59999"}
        result = runner.invoke(session, ["list"], env=env)
        assert result.exit_code != 0
        assert "WEBHOOK_API_SECRET" in result.output
