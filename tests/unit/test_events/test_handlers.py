"""Tests for event handlers."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

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

    async def test_scheduled_event_with_skill(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Scheduled events with skill_name prepend the skill invocation."""
        mock_response = MagicMock()
        mock_response.content = "Done"
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
