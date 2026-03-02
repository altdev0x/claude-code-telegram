"""Tests for event handlers."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.claude.exceptions import ClaudeExecutionError
from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.types import AgentResponseEvent, ScheduledEvent, WebhookEvent


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def mock_claude() -> AsyncMock:
    mock = AsyncMock()
    mock.run_command = AsyncMock()
    return mock


@pytest.fixture
def mock_scheduler() -> AsyncMock:
    mock = AsyncMock()
    mock.record_job_run = AsyncMock()
    return mock


@pytest.fixture
def agent_handler(
    event_bus: EventBus, mock_claude: AsyncMock, mock_scheduler: AsyncMock
) -> AgentHandler:
    handler = AgentHandler(
        event_bus=event_bus,
        claude_integration=mock_claude,
        default_working_directory=Path("/tmp/test"),
        default_user_id=42,
        job_scheduler=mock_scheduler,
    )
    handler.register()
    return handler


class TestAgentHandler:
    """Tests for AgentHandler."""

    async def test_webhook_event_triggers_claude(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Webhook events are processed through Claude."""
        mock_response = MagicMock()
        mock_response.content = "Analysis complete"
        mock_claude.run_command.return_value = mock_response

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(event):  # type: ignore[no-untyped-def]
            published.append(event)
            await original_publish(event)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = WebhookEvent(
            provider="github",
            event_type_name="push",
            payload={"ref": "refs/heads/main"},
            delivery_id="del-1",
        )

        await agent_handler.handle_webhook(event)

        mock_claude.run_command.assert_called_once()
        call_kwargs = mock_claude.run_command.call_args
        assert "github" in call_kwargs.kwargs["prompt"].lower()

        # Should publish an AgentResponseEvent
        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        assert response_events[0].text == "Analysis complete"

    async def test_scheduled_event_triggers_claude(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Scheduled events invoke Claude with the job's prompt."""
        mock_response = MagicMock()
        mock_response.content = "Standup summary"
        mock_response.cost = 0.05
        mock_claude.run_command.return_value = mock_response

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(event):  # type: ignore[no-untyped-def]
            published.append(event)
            await original_publish(event)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = ScheduledEvent(
            job_name="standup",
            prompt="Generate daily standup",
            target_chat_ids=[100],
        )

        await agent_handler.handle_scheduled(event)

        mock_claude.run_command.assert_called_once()
        assert "standup" in mock_claude.run_command.call_args.kwargs["prompt"].lower()

        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        assert response_events[0].chat_id == 100
        # Notification text includes header and original content
        text = response_events[0].text
        assert "<b>standup</b>" in text
        assert "Standup summary" in text

    async def test_scheduled_event_with_skill(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Scheduled events with skill_name prepend the skill invocation."""
        mock_response = MagicMock()
        mock_response.content = "Done"
        mock_response.cost = 0.01
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(
            job_name="standup",
            prompt="morning report",
            skill_name="daily-standup",
            target_chat_ids=[100],
        )

        await agent_handler.handle_scheduled(event)

        prompt = mock_claude.run_command.call_args.kwargs["prompt"]
        assert prompt.startswith("/daily-standup")
        assert "morning report" in prompt

    async def test_claude_error_does_not_propagate(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Agent errors are logged but don't crash the handler."""
        mock_claude.run_command.side_effect = RuntimeError("SDK error")

        event = WebhookEvent(
            provider="github",
            event_type_name="push",
            payload={},
        )

        # Should not raise
        await agent_handler.handle_webhook(event)

    def test_build_webhook_prompt(self, agent_handler: AgentHandler) -> None:
        """Webhook prompt includes provider and event info."""
        event = WebhookEvent(
            provider="github",
            event_type_name="pull_request",
            payload={"action": "opened", "number": 42},
        )

        prompt = agent_handler._build_webhook_prompt(event)
        assert "github" in prompt.lower()
        assert "pull_request" in prompt
        assert "action: opened" in prompt

    def test_payload_summary_truncation(self, agent_handler: AgentHandler) -> None:
        """Large payloads are truncated in the summary."""
        big_payload = {"key": "x" * 3000}
        summary = agent_handler._summarize_payload(big_payload)
        assert len(summary) <= 2100  # 2000 + truncation message

    async def test_isolated_mode_sets_force_new(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """Isolated session_mode passes force_new=True to run_command."""
        mock_response = MagicMock()
        mock_response.content = "Done"
        mock_response.cost = 0.01
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(
            job_id="job-1",
            job_name="test",
            prompt="hello",
            target_chat_ids=[100],
            session_mode="isolated",
        )

        await agent_handler.handle_scheduled(event)

        call_kwargs = mock_claude.run_command.call_args.kwargs
        assert call_kwargs["force_new"] is True
        assert call_kwargs["ephemeral"] is True

    async def test_resume_mode_no_force_new(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """Resume session_mode passes force_new=False to run_command."""
        mock_response = MagicMock()
        mock_response.content = "Done"
        mock_response.cost = 0.01
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(
            job_id="job-1",
            job_name="test",
            prompt="hello",
            target_chat_ids=[100],
            session_mode="resume",
        )

        await agent_handler.handle_scheduled(event)

        call_kwargs = mock_claude.run_command.call_args.kwargs
        assert call_kwargs["force_new"] is False
        assert call_kwargs["ephemeral"] is True

    async def test_job_run_recorded_on_success(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        mock_scheduler: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """Successful scheduled runs are recorded in job history."""
        mock_response = MagicMock()
        mock_response.content = "Result"
        mock_response.cost = 0.02
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(
            job_id="job-1",
            job_name="test",
            prompt="hello",
            target_chat_ids=[100],
        )

        await agent_handler.handle_scheduled(event)

        mock_scheduler.record_job_run.assert_called_once()
        call_kwargs = mock_scheduler.record_job_run.call_args.kwargs
        assert call_kwargs["job_id"] == "job-1"
        assert call_kwargs["success"] is True
        assert call_kwargs["cost"] == 0.02
        assert call_kwargs["response_summary"] == "Result"

    async def test_job_run_recorded_on_failure(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        mock_scheduler: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """Failed scheduled runs are recorded with the error message."""
        mock_claude.run_command.side_effect = RuntimeError("SDK crash")

        event = ScheduledEvent(
            job_id="job-1",
            job_name="test",
            prompt="hello",
            target_chat_ids=[100],
        )

        await agent_handler.handle_scheduled(event)

        mock_scheduler.record_job_run.assert_called_once()
        call_kwargs = mock_scheduler.record_job_run.call_args.kwargs
        assert call_kwargs["success"] is False
        assert "SDK crash" in call_kwargs["error_message"]

    async def test_scheduled_notification_header_format(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """Scheduled notifications include a formatted header with job metadata."""
        mock_response = MagicMock()
        mock_response.content = "All tests passed."
        mock_response.cost = 0.13
        mock_claude.run_command.return_value = mock_response

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(event):  # type: ignore[no-untyped-def]
            published.append(event)
            await original_publish(event)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = ScheduledEvent(
            job_name="nightly tests",
            prompt="Run the full test suite",
            target_chat_ids=[200],
            working_directory="/home/user/projects/myapp",
            session_mode="isolated",
        )

        await agent_handler.handle_scheduled(event)

        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        text = response_events[0].text

        # Header line 1: emoji + bold job name
        assert "\U0001f4cb <b>nightly tests</b>" in text
        # Header line 2: short dir, session mode, cost
        assert "myapp" in text
        assert "isolated" in text
        assert "$0.13" in text
        # Original content follows after blank line
        assert "\nAll tests passed." in text

    async def test_scheduled_notification_broadcast_has_header(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """Broadcast path (no target_chat_ids) also includes the header."""
        mock_response = MagicMock()
        mock_response.content = "Done"
        mock_response.cost = 0.0
        mock_claude.run_command.return_value = mock_response

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(event):  # type: ignore[no-untyped-def]
            published.append(event)
            await original_publish(event)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = ScheduledEvent(
            job_name="health check",
            prompt="Check status",
            target_chat_ids=[],
            working_directory="/home/user/workspace/klaus",
        )

        await agent_handler.handle_scheduled(event)

        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        text = response_events[0].text
        assert "<b>health check</b>" in text
        assert "klaus" in text

    class TestSilentSuppression:
        """Tests for [SILENT] flag detection and delivery suppression."""

        @pytest.mark.parametrize(
            "content",
            [
                "[SILENT]",
                "[silent]",
                "[Silent]",
                "  [SILENT]  ",
                "`[SILENT]`",
                "` [SILENT] `",
                "Some output\n[SILENT]",
                "Some output\n\n`[SILENT]`\n",
                "Summary here\n\n[SILENT]\n\n",
            ],
            ids=[
                "exact",
                "lowercase",
                "mixed-case",
                "padded",
                "backtick-wrapped",
                "backtick-padded",
                "after-content",
                "after-content-backtick-trailing-newline",
                "trailing-blank-lines",
            ],
        )
        def test_is_silent_positive(self, content: str) -> None:
            assert AgentHandler._is_silent(content) is True

        @pytest.mark.parametrize(
            "content",
            [
                "Normal response",
                "Contains [SILENT] in middle\nMore text",
                "SILENT",
                "[LOUD]",
                "",
            ],
            ids=[
                "normal",
                "mid-text",
                "no-brackets",
                "wrong-keyword",
                "empty",
            ],
        )
        def test_is_silent_negative(self, content: str) -> None:
            assert AgentHandler._is_silent(content) is False

        async def test_silent_suppresses_delivery(
            self,
            event_bus: EventBus,
            mock_claude: AsyncMock,
            mock_scheduler: AsyncMock,
            agent_handler: AgentHandler,
        ) -> None:
            """[SILENT] response suppresses Telegram delivery but records the run."""
            mock_response = MagicMock()
            mock_response.content = "Nothing actionable.\n\n`[SILENT]`"
            mock_response.cost = 0.46
            mock_claude.run_command.return_value = mock_response

            published: list = []
            original_publish = event_bus.publish

            async def capture_publish(event):  # type: ignore[no-untyped-def]
                published.append(event)
                await original_publish(event)

            event_bus.publish = capture_publish  # type: ignore[assignment]

            event = ScheduledEvent(
                job_id="job-hb",
                job_name="Heartbeat 8:00CET",
                prompt="Run heartbeat",
                target_chat_ids=[100],
            )

            await agent_handler.handle_scheduled(event)

            # No AgentResponseEvent should be published
            response_events = [
                e for e in published if isinstance(e, AgentResponseEvent)
            ]
            assert len(response_events) == 0

            # Job run should still be recorded
            mock_scheduler.record_job_run.assert_called_once()
            call_kwargs = mock_scheduler.record_job_run.call_args.kwargs
            assert call_kwargs["success"] is True
            assert call_kwargs["response_summary"] == "[SILENT]"
            assert call_kwargs["cost"] == 0.46


class TestAlwaysNotify:
    """AgentHandler always notifies the user on failure."""

    async def test_claude_execution_error_publishes_notification(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        mock_scheduler: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """ClaudeExecutionError triggers an AgentResponseEvent with error details."""
        mock_claude.run_command.side_effect = ClaudeExecutionError(
            error=RuntimeError("idle timeout"),
            partial_content="Some partial work",
            partial_cost=0.03,
            messages_received=2,
        )

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(evt):  # type: ignore[no-untyped-def]
            published.append(evt)
            await original_publish(evt)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = ScheduledEvent(
            job_id="job-fail",
            job_name="nightly job",
            prompt="do work",
            target_chat_ids=[200],
        )

        await agent_handler.handle_scheduled(event)

        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        text = response_events[0].text
        assert "nightly job" in text
        assert "failed" in text.lower()
        assert "Some partial work" in text

    async def test_claude_execution_error_records_partial_cost(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        mock_scheduler: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """Job run is recorded with partial_cost from ClaudeExecutionError."""
        mock_claude.run_command.side_effect = ClaudeExecutionError(
            error=RuntimeError("boom"),
            partial_content="partial",
            partial_cost=0.11,
            messages_received=5,
        )

        event = ScheduledEvent(
            job_id="job-cost",
            job_name="cost test",
            prompt="do work",
            target_chat_ids=[100],
        )

        await agent_handler.handle_scheduled(event)

        mock_scheduler.record_job_run.assert_called_once()
        call_kwargs = mock_scheduler.record_job_run.call_args.kwargs
        assert call_kwargs["success"] is False
        assert call_kwargs["cost"] == 0.11

    async def test_generic_exception_publishes_notification(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        mock_scheduler: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """Any generic Exception also triggers an AgentResponseEvent."""
        mock_claude.run_command.side_effect = RuntimeError("unexpected crash")

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(evt):  # type: ignore[no-untyped-def]
            published.append(evt)
            await original_publish(evt)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = ScheduledEvent(
            job_id="job-crash",
            job_name="crash job",
            prompt="do work",
            target_chat_ids=[300],
        )

        await agent_handler.handle_scheduled(event)

        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        text = response_events[0].text
        assert "crash job" in text
        assert "failed" in text.lower()

    async def test_generic_exception_notification_sent_to_target_chats(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        mock_scheduler: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """Error notification is sent to all target_chat_ids."""
        mock_claude.run_command.side_effect = RuntimeError("error")

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(evt):  # type: ignore[no-untyped-def]
            published.append(evt)
            await original_publish(evt)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = ScheduledEvent(
            job_id="job-multi",
            job_name="multi-target",
            prompt="do work",
            target_chat_ids=[100, 200],
        )

        await agent_handler.handle_scheduled(event)

        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        chat_ids = {e.chat_id for e in response_events}
        assert 100 in chat_ids
        assert 200 in chat_ids

    async def test_generic_exception_broadcast_when_no_targets(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        mock_scheduler: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """Error notification uses chat_id=0 broadcast when no target_chat_ids."""
        mock_claude.run_command.side_effect = RuntimeError("error")

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(evt):  # type: ignore[no-untyped-def]
            published.append(evt)
            await original_publish(evt)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = ScheduledEvent(
            job_id="job-bcast",
            job_name="broadcast",
            prompt="do work",
            target_chat_ids=[],
        )

        await agent_handler.handle_scheduled(event)

        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        assert response_events[0].chat_id == 0

    async def test_per_job_idle_timeout_passed_to_run_command(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        mock_scheduler: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """idle_timeout_seconds from ScheduledEvent is forwarded to run_command."""
        mock_response = MagicMock()
        mock_response.content = "Done"
        mock_response.cost = 0.01
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(
            job_id="job-timeout",
            job_name="long job",
            prompt="do work",
            target_chat_ids=[100],
            idle_timeout_seconds=900,
        )

        await agent_handler.handle_scheduled(event)

        call_kwargs = mock_claude.run_command.call_args.kwargs
        assert call_kwargs["idle_timeout_seconds"] == 900

    async def test_per_job_max_turns_passed_to_run_command(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        mock_scheduler: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """max_turns from ScheduledEvent is forwarded to run_command."""
        mock_response = MagicMock()
        mock_response.content = "Done"
        mock_response.cost = 0.01
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(
            job_id="job-turns",
            job_name="limited turns",
            prompt="do work",
            target_chat_ids=[100],
            max_turns=5,
        )

        await agent_handler.handle_scheduled(event)

        call_kwargs = mock_claude.run_command.call_args.kwargs
        assert call_kwargs["max_turns"] == 5

    async def test_per_job_none_timeout_passes_none_to_run_command(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        mock_scheduler: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """None idle_timeout_seconds (use global default) passes None to run_command."""
        mock_response = MagicMock()
        mock_response.content = "Done"
        mock_response.cost = 0.01
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(
            job_id="job-default",
            job_name="default timeout",
            prompt="do work",
            target_chat_ids=[100],
            idle_timeout_seconds=None,
        )

        await agent_handler.handle_scheduled(event)

        call_kwargs = mock_claude.run_command.call_args.kwargs
        assert call_kwargs["idle_timeout_seconds"] is None

    async def test_execution_error_no_partial_content_omits_block(
        self,
        event_bus: EventBus,
        mock_claude: AsyncMock,
        mock_scheduler: AsyncMock,
        agent_handler: AgentHandler,
    ) -> None:
        """Error notification without partial_content does not include partial output section."""
        mock_claude.run_command.side_effect = ClaudeExecutionError(
            error=RuntimeError("timeout"),
            partial_content=None,
            partial_cost=0.0,
            messages_received=0,
        )

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(evt):  # type: ignore[no-untyped-def]
            published.append(evt)
            await original_publish(evt)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = ScheduledEvent(
            job_id="job-empty",
            job_name="empty partial",
            prompt="do work",
            target_chat_ids=[100],
        )

        await agent_handler.handle_scheduled(event)

        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        text = response_events[0].text
        assert "Last partial output" not in text
