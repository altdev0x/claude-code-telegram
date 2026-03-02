"""Integration tests for job parameters (model + watchdog) through the full pipeline.

Tests span the path:
  JobScheduler → DB → EventBus → AgentHandler → ClaudeIntegration
      → ClaudeSDKManager → [mocked ClaudeSDKClient]

The SDK is mocked at the ClaudeSDKClient boundary; everything else is real
(in-memory SQLite, real APScheduler in-process, real event bus).

All tests run sequentially (not parallel) because they share a single DB
fixture and APScheduler instance.
"""

import asyncio
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from src.claude.exceptions import ClaudeExecutionError, ClaudeTimeoutError
from src.claude.facade import ClaudeIntegration
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.settings import Settings
from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.types import AgentResponseEvent, ScheduledEvent
from src.scheduler.scheduler import JobScheduler
from src.storage.database import DatabaseManager
from src.utils.constants import MODEL_MAP


# ---------------------------------------------------------------------------
# SDK message helpers
# ---------------------------------------------------------------------------


def _make_assistant_message(text: str = "Done") -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-6",
    )


def _make_result_message(
    result: str = "Success",
    cost: float = 0.01,
    session_id: str = "test-session-123",
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=500,
        duration_api_ms=400,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=cost,
        result=result,
    )


def _make_fast_client(
    *messages,
    captured_options: list,
) -> MagicMock:
    """Build a mock ClaudeSDKClient that immediately returns the given messages.

    The ClaudeAgentOptions passed to the constructor are appended to
    ``captured_options`` so tests can assert on them.
    """
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def receive():
        for msg in messages:
            yield msg

    qm = AsyncMock()
    qm.receive_messages = receive
    client._query = qm
    return client


def _client_factory(
    *messages,
    captured_options: list,
):
    """Return a factory suitable for use as ClaudeSDKClient side_effect."""

    def factory(options):  # type: ignore[no-untyped-def]
        captured_options.append(options)
        return _make_fast_client(*messages, captured_options=captured_options)

    return factory


def _make_hanging_client(
    *first_messages,
    hang_seconds: float = 10.0,
    captured_options: list,
) -> MagicMock:
    """Client that yields some messages then hangs (simulates idle-timeout)."""
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def receive():
        for msg in first_messages:
            yield msg
        await asyncio.sleep(hang_seconds)

    qm = AsyncMock()
    qm.receive_messages = receive
    client._query = qm
    return client


def _make_slow_steady_client(
    message_groups: list,
    inter_group_delay: float,
    captured_options: list,
) -> MagicMock:
    """Client that yields message groups with a small delay between each group."""
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def receive():
        for i, group in enumerate(message_groups):
            if i > 0:
                await asyncio.sleep(inter_group_delay)
            for msg in group:
                yield msg

    qm = AsyncMock()
    qm.receive_messages = receive
    client._query = qm
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def tmp_db(tmp_path: Path) -> DatabaseManager:
    """Real DatabaseManager backed by a temporary file."""
    db = DatabaseManager(f"sqlite:///{tmp_path / 'test.db'}")
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
async def event_bus() -> EventBus:
    bus = EventBus()
    await bus.start()
    yield bus
    await bus.stop()


@pytest.fixture
def config(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        claude_model="sonnet",          # global default = sonnet
        claude_idle_timeout_seconds=30,  # short global default
        claude_max_turns=10,
    )


@pytest.fixture
def scheduler(
    event_bus: EventBus,
    tmp_db: DatabaseManager,
    tmp_path: Path,
) -> JobScheduler:
    # Do NOT call start() — that would launch APScheduler's background threads
    # which interfere with pytest-asyncio event loop teardown.
    # trigger_now() reads from the DB directly so no APScheduler start needed.
    return JobScheduler(
        event_bus=event_bus,
        db_manager=tmp_db,
        default_working_directory=tmp_path,
    )


@pytest.fixture
def sdk_manager(config: Settings) -> ClaudeSDKManager:
    return ClaudeSDKManager(config)


@pytest.fixture
def claude_integration(
    config: Settings,
    sdk_manager: ClaudeSDKManager,
) -> ClaudeIntegration:
    # session_manager=None is fine because all scheduled-job paths use
    # ephemeral=True + force_new=True (isolated mode), bypassing the session store.
    return ClaudeIntegration(config=config, sdk_manager=sdk_manager)


@pytest.fixture
def agent_handler(
    event_bus: EventBus,
    claude_integration: ClaudeIntegration,
    scheduler: JobScheduler,
    tmp_path: Path,
) -> AgentHandler:
    handler = AgentHandler(
        event_bus=event_bus,
        claude_integration=claude_integration,
        default_working_directory=tmp_path,
        default_user_id=0,
        job_scheduler=scheduler,
    )
    handler.register()
    return handler


# ---------------------------------------------------------------------------
# Helper: subscribe to AgentResponseEvent and collect published notifications
# ---------------------------------------------------------------------------


def _collect_notifications(event_bus: EventBus) -> List[AgentResponseEvent]:
    """Return a list that is populated as AgentResponseEvents are published."""
    received: List[AgentResponseEvent] = []

    async def _capture(event):  # type: ignore[no-untyped-def]
        if isinstance(event, AgentResponseEvent):
            received.append(event)

    event_bus.subscribe(AgentResponseEvent, _capture)
    return received


# ---------------------------------------------------------------------------
# Helper: trigger a job and wait for the AgentHandler to finish
# ---------------------------------------------------------------------------


async def _trigger_and_wait(
    scheduler: JobScheduler,
    event_bus: EventBus,
    job_id: str,
    timeout: float = 5.0,
) -> None:
    """Publish a ScheduledEvent for job_id and wait until the handler fully completes.

    Waits for record_job_run (the scheduler's ``finally`` block) to be called,
    then yields briefly so the event bus can dispatch any queued events
    (e.g. the AgentResponseEvent that was published just before record_job_run).
    """
    done = asyncio.Event()

    original_record = scheduler.record_job_run

    async def _on_record(*args, **kwargs):  # type: ignore[no-untyped-def]
        await original_record(*args, **kwargs)
        done.set()

    scheduler.record_job_run = _on_record  # type: ignore[method-assign]

    await scheduler.trigger_now(job_id)

    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
        # The AgentResponseEvent is published to the queue just BEFORE
        # record_job_run is called.  Yield here so the event bus processor
        # can pick it up and deliver it to subscribers.
        await asyncio.sleep(0.15)
    finally:
        scheduler.record_job_run = original_record  # type: ignore[method-assign]


# ===========================================================================
# Task 1 — Model selection
# ===========================================================================


class TestModelSelection:
    """Integration: per-job model selection flows from scheduler → ClaudeAgentOptions."""

    @patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x)
    async def test_opus_model_stored_in_db_and_reaches_sdk(
        self, _parse, scheduler: JobScheduler, event_bus: EventBus,
        agent_handler: AgentHandler, tmp_path: Path,
    ) -> None:
        """schedule create --model opus → model stored in DB; SDK gets claude-opus-4-6."""
        # 1. Create job with model="opus"
        job_id = await scheduler.add_job(
            job_name="opus-job",
            cron_expression="0 0 * * *",
            prompt="Daily summary",
            target_chat_ids=[123],
            working_directory=tmp_path,
            model="opus",
        )

        # 2. Verify DB stores the friendly name
        job = await scheduler.get_job(job_id)
        assert job is not None
        assert job["model"] == "opus"

        # 3. Trigger and capture ClaudeAgentOptions
        captured: list = []
        factory = _client_factory(
            _make_assistant_message("Done"), _make_result_message(),
            captured_options=captured,
        )
        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory):
            await _trigger_and_wait(scheduler, event_bus, job_id)

        # 4. Verify resolved model ID reached the SDK
        assert captured, "ClaudeSDKClient constructor was never called"
        assert captured[0].model == MODEL_MAP["opus"]  # "claude-opus-4-6"

    @patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x)
    async def test_no_model_defaults_to_sonnet(
        self, _parse, scheduler: JobScheduler, event_bus: EventBus,
        agent_handler: AgentHandler, tmp_path: Path, config: Settings,
    ) -> None:
        """schedule create (no --model) → NULL in DB; SDK gets global default (sonnet)."""
        job_id = await scheduler.add_job(
            job_name="default-model-job",
            cron_expression="0 0 * * *",
            prompt="Do something",
            target_chat_ids=[456],
            working_directory=tmp_path,
            model=None,  # no per-job override
        )

        # Verify DB stores NULL
        job = await scheduler.get_job(job_id)
        assert job is not None
        assert job["model"] is None

        # Trigger and capture options
        captured: list = []
        factory = _client_factory(
            _make_assistant_message("ok"), _make_result_message(),
            captured_options=captured,
        )
        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory):
            await _trigger_and_wait(scheduler, event_bus, job_id)

        assert captured
        # Global default is "sonnet" → resolved to claude-sonnet-4-6
        assert captured[0].model == MODEL_MAP[config.claude_model]

    async def test_invalid_model_rejected_by_model_map_whitelist(self) -> None:
        """schedule create --model gpt4 → click.Choice rejects it before the scheduler.

        Whitelist enforcement lives in the CLI (click.Choice).  At the scheduler
        level an unknown name is passed through unmapped (MODEL_MAP.get fallback).
        This test verifies that unknown names don't appear in MODEL_MAP, so they
        wouldn't pass the CLI validation gate.
        """
        assert "gpt4" not in MODEL_MAP
        assert "gpt-4" not in MODEL_MAP
        assert "invalid-model" not in MODEL_MAP
        # And all valid choices ARE in the map
        for key in ("sonnet", "opus", "haiku"):
            assert key in MODEL_MAP

    @patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x)
    async def test_update_model_persisted_and_used_on_next_run(
        self, _parse, scheduler: JobScheduler, event_bus: EventBus,
        agent_handler: AgentHandler, tmp_path: Path,
    ) -> None:
        """schedule update <id> --model haiku → DB updated; next run uses haiku model."""
        # Create with default (no model)
        job_id = await scheduler.add_job(
            job_name="update-model-job",
            cron_expression="0 0 * * *",
            prompt="Work",
            working_directory=tmp_path,
            model=None,
        )

        # Update to haiku
        await scheduler.update_job(job_id, model="haiku")

        # Verify DB reflects the change
        job = await scheduler.get_job(job_id)
        assert job is not None
        assert job["model"] == "haiku"

        # Trigger and verify SDK sees haiku model ID
        captured: list = []
        factory = _client_factory(
            _make_assistant_message("done"), _make_result_message(),
            captured_options=captured,
        )
        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory):
            await _trigger_and_wait(scheduler, event_bus, job_id)

        assert captured
        assert captured[0].model == MODEL_MAP["haiku"]

    async def test_update_no_changes_preserves_existing_fields(
        self, scheduler: JobScheduler, tmp_path: Path,
    ) -> None:
        """schedule update <id> (no changes) → no-op; all existing fields preserved."""
        job_id = await scheduler.add_job(
            job_name="unchanged-job",
            cron_expression="30 8 * * 1-5",
            prompt="Morning brief",
            target_chat_ids=[789],
            working_directory=tmp_path,
            model="opus",
        )

        original = await scheduler.get_job(job_id)
        assert original is not None

        # update_job with no fields should return True (no-op)
        result = await scheduler.update_job(job_id)  # no kwargs → empty update dict
        assert result is True

        updated = await scheduler.get_job(job_id)
        assert updated is not None

        # All substantive fields must be unchanged
        assert updated["job_name"] == original["job_name"]
        assert updated["prompt"] == original["prompt"]
        assert updated["cron_expression"] == original["cron_expression"]
        assert updated["model"] == original["model"]


# ===========================================================================
# Task 2 — Watchdog + always-notify
# ===========================================================================


class TestWatchdogAndNotifications:
    """Integration: watchdog idle-timeout and Telegram notification delivery."""

    @patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x)
    async def test_successful_job_sends_notification(
        self, _parse, scheduler: JobScheduler, event_bus: EventBus,
        agent_handler: AgentHandler, tmp_path: Path,
    ) -> None:
        """Job completing normally → AgentResponseEvent published to target chat."""
        notifications = _collect_notifications(event_bus)

        job_id = await scheduler.add_job(
            job_name="notify-job",
            cron_expression="0 0 * * *",
            prompt="Say hello",
            target_chat_ids=[1001],
            working_directory=tmp_path,
        )

        captured: list = []
        factory = _client_factory(
            _make_assistant_message("Hello, world!"),
            _make_result_message(result="Hello, world!"),
            captured_options=captured,
        )
        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory):
            await _trigger_and_wait(scheduler, event_bus, job_id)

        # At least one notification published to chat 1001
        assert any(n.chat_id == 1001 for n in notifications), (
            f"No notification for chat 1001; received: {notifications}"
        )
        # Content contains the response
        chat_notifications = [n for n in notifications if n.chat_id == 1001]
        assert any("Hello" in n.text for n in chat_notifications)

    @patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x)
    async def test_idle_timeout_sends_error_notification_with_partial(
        self, _parse, scheduler: JobScheduler, event_bus: EventBus,
        agent_handler: AgentHandler, tmp_path: Path,
    ) -> None:
        """Job idle-timeouts → error notification with partial content + cost sent."""
        notifications = _collect_notifications(event_bus)

        job_id = await scheduler.add_job(
            job_name="timeout-job",
            cron_expression="0 0 * * *",
            prompt="Long task",
            target_chat_ids=[2001],
            working_directory=tmp_path,
            idle_timeout_seconds=1,  # very short: 1 second
        )

        # Client yields one message then hangs — triggers ClaudeExecutionError with partial
        captured: list = []

        def hanging_factory(options):  # type: ignore[no-untyped-def]
            captured.append(options)
            return _make_hanging_client(
                _make_assistant_message("Partial work done"),
                hang_seconds=10.0,
                captured_options=captured,
            )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=hanging_factory
        ):
            await _trigger_and_wait(scheduler, event_bus, job_id, timeout=10.0)

        # An error notification should have been published
        error_notifications = [n for n in notifications if n.chat_id == 2001]
        assert error_notifications, "No notification sent after idle timeout"
        error_text = error_notifications[0].text
        # Should mention failure
        assert "failed" in error_text.lower() or "⚠" in error_text
        # Should include partial content
        assert "Partial work done" in error_text

    @patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x)
    async def test_max_turns_zero_sends_none_to_sdk(
        self, _parse, scheduler: JobScheduler, event_bus: EventBus,
        agent_handler: AgentHandler, tmp_path: Path,
    ) -> None:
        """schedule create --max-turns 0 → SDK receives max_turns=None (unlimited)."""
        job_id = await scheduler.add_job(
            job_name="unlimited-turns-job",
            cron_expression="0 0 * * *",
            prompt="Run forever",
            working_directory=tmp_path,
            max_turns=0,  # 0 = unlimited
        )

        captured: list = []
        factory = _client_factory(
            _make_result_message(), captured_options=captured,
        )
        with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory):
            await _trigger_and_wait(scheduler, event_bus, job_id)

        assert captured
        # max_turns=0 must be translated to None (unlimited) for SDK
        assert captured[0].max_turns is None, (
            f"Expected max_turns=None (unlimited) but got {captured[0].max_turns}"
        )

    @patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x)
    async def test_per_job_idle_timeout_overrides_global(
        self, _parse, scheduler: JobScheduler, event_bus: EventBus,
        agent_handler: AgentHandler, sdk_manager: ClaudeSDKManager,
        tmp_path: Path,
    ) -> None:
        """schedule create --timeout 60 → watchdog uses 60s, not global default (30s).

        Verifies the value by spying on ClaudeSDKManager.execute_command's
        ``idle_timeout`` keyword argument, which is the last hop before the
        parameter enters the watchdog.
        """
        per_job_timeout = 60

        job_id = await scheduler.add_job(
            job_name="custom-timeout-job",
            cron_expression="0 0 * * *",
            prompt="Work with custom timeout",
            working_directory=tmp_path,
            idle_timeout_seconds=per_job_timeout,
        )

        # Verify persisted in DB
        job = await scheduler.get_job(job_id)
        assert job is not None
        assert job["idle_timeout_seconds"] == per_job_timeout

        # Spy on execute_command to capture the idle_timeout kwarg that reaches it.
        # sdk_manager is the SAME instance used by claude_integration → agent_handler.
        captured_kwargs: list = []
        original_execute = sdk_manager.execute_command

        async def _spy_execute(**kwargs):  # type: ignore[no-untyped-def]
            captured_kwargs.append(kwargs)
            return await original_execute(**kwargs)

        sdk_manager.execute_command = _spy_execute  # type: ignore[method-assign]

        captured_opts: list = []
        factory = _client_factory(
            _make_assistant_message("ok"), _make_result_message(),
            captured_options=captured_opts,
        )
        try:
            with patch("src.claude.sdk_integration.ClaudeSDKClient", side_effect=factory):
                await _trigger_and_wait(scheduler, event_bus, job_id)
        finally:
            sdk_manager.execute_command = original_execute  # type: ignore[method-assign]

        assert captured_kwargs, "execute_command was never called"
        actual_idle_timeout = captured_kwargs[0].get("idle_timeout")
        assert actual_idle_timeout == per_job_timeout, (
            f"Expected idle_timeout={per_job_timeout} but got {actual_idle_timeout}; "
            f"full kwargs: {captured_kwargs[0]}"
        )

    @patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x)
    async def test_watchdog_does_not_fire_during_steady_message_flow(
        self, _parse, scheduler: JobScheduler, event_bus: EventBus,
        agent_handler: AgentHandler, tmp_path: Path,
    ) -> None:
        """Long-running job with periodic messages does NOT trigger the watchdog.

        The idle_timeout is 2s; messages arrive every 0.3s — so the watchdog
        never has a gap long enough to fire.
        """
        notifications = _collect_notifications(event_bus)

        job_id = await scheduler.add_job(
            job_name="steady-flow-job",
            cron_expression="0 0 * * *",
            prompt="Long steady job",
            target_chat_ids=[3001],
            working_directory=tmp_path,
            idle_timeout_seconds=2,  # 2s idle timeout
        )

        # Messages arrive every 0.3s — 5 groups well within 2s gap
        message_groups = [
            [_make_assistant_message(f"step {i}")]
            for i in range(5)
        ] + [[_make_result_message(result="All done")]]

        captured: list = []
        slow_client = _make_slow_steady_client(
            message_groups=message_groups,
            inter_group_delay=0.3,
            captured_options=captured,
        )

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=slow_client):
            # Must not raise — watchdog should stay quiet
            await _trigger_and_wait(scheduler, event_bus, job_id, timeout=10.0)

        # Should have received a success notification (not an error)
        success_notifications = [n for n in notifications if n.chat_id == 3001]
        assert success_notifications, "No notification for successful steady-flow job"
        # Success notification should NOT contain failure text
        combined = " ".join(n.text for n in success_notifications)
        assert "failed" not in combined.lower()
        assert "All done" in combined
