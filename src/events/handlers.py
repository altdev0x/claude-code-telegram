"""Event handlers that bridge the event bus to Claude and Telegram.

AgentHandler: translates events into ClaudeIntegration.run_command() calls.
NotificationHandler: subscribes to AgentResponseEvent and delivers to Telegram.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from ..bot.utils.html_format import escape_html
from ..claude.exceptions import ClaudeExecutionError
from ..claude.facade import ClaudeIntegration
from .bus import Event, EventBus
from .types import AgentResponseEvent, ScheduledEvent, WebhookEvent

logger = structlog.get_logger()


class AgentHandler:
    """Translates incoming events into Claude agent executions.

    Webhook and scheduled events are converted into prompts and sent
    to ClaudeIntegration.run_command(). The response is published
    back as an AgentResponseEvent for delivery.
    """

    def __init__(
        self,
        event_bus: EventBus,
        claude_integration: ClaudeIntegration,
        default_working_directory: Path,
        default_user_id: int = 0,
        job_scheduler: Optional["JobScheduler"] = None,  # noqa: F821
    ) -> None:
        self.event_bus = event_bus
        self.claude = claude_integration
        self.default_working_directory = default_working_directory
        self.default_user_id = default_user_id
        self.job_scheduler = job_scheduler

    def register(self) -> None:
        """Subscribe to events that need agent processing."""
        self.event_bus.subscribe(WebhookEvent, self.handle_webhook)
        self.event_bus.subscribe(ScheduledEvent, self.handle_scheduled)

    async def handle_webhook(self, event: Event) -> None:
        """Process a webhook event through Claude."""
        if not isinstance(event, WebhookEvent):
            return

        logger.info(
            "Processing webhook event through agent",
            provider=event.provider,
            event_type=event.event_type_name,
            delivery_id=event.delivery_id,
        )

        prompt = self._build_webhook_prompt(event)

        try:
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=self.default_working_directory,
                user_id=self.default_user_id,
            )

            if response.content:
                # We don't know which chat to send to from a webhook alone.
                # The notification service needs configured target chats.
                # Publish with chat_id=0 — the NotificationService
                # will broadcast to configured notification_chat_ids.
                await self.event_bus.publish(
                    AgentResponseEvent(
                        chat_id=0,
                        text=response.content,
                        originating_event_id=event.id,
                    )
                )
        except Exception:
            logger.exception(
                "Agent execution failed for webhook event",
                provider=event.provider,
                event_id=event.id,
            )

    async def handle_scheduled(self, event: Event) -> None:
        """Process a scheduled event through Claude."""
        if not isinstance(event, ScheduledEvent):
            return

        logger.info(
            "Processing scheduled event through agent",
            job_id=event.job_id,
            job_name=event.job_name,
            session_mode=event.session_mode,
        )

        prompt = event.prompt
        if event.skill_name:
            prompt = (
                f"/{event.skill_name}\n\n{prompt}" if prompt else f"/{event.skill_name}"
            )

        working_dir = event.working_directory or self.default_working_directory
        force_new = event.session_mode == "isolated"

        fired_at = datetime.now(UTC)
        success = False
        error_message: Optional[str] = None
        response_summary: Optional[str] = None
        cost = 0.0

        try:
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=working_dir,
                user_id=self.default_user_id,
                force_new=force_new,
                ephemeral=True,
                model=event.model,
                idle_timeout_seconds=event.idle_timeout_seconds,
                max_turns=event.max_turns,
            )

            success = True
            cost = response.cost
            if response.content:
                silent = self._is_silent(response.content)

                if silent:
                    response_summary = "[SILENT]"
                    logger.info(
                        "Scheduled job signalled [SILENT], suppressing delivery",
                        job_id=event.job_id,
                        job_name=event.job_name,
                    )
                else:
                    response_summary = response.content[:500]

                    header = self._format_scheduled_header(
                        event, response.cost, working_dir
                    )
                    formatted_text = f"{header}\n{response.content}"

                    await self._publish_to_target_chats(event, formatted_text)

        except ClaudeExecutionError as exc:
            # Partial results available — format a rich failure notification
            error_message = str(exc)[:500]
            cost = exc.partial_cost
            logger.exception(
                "Scheduled job failed with partial results",
                job_id=event.job_id,
                messages_received=exc.messages_received,
                partial_cost=exc.partial_cost,
            )
            elapsed_minutes = int(
                (datetime.now(UTC) - fired_at).total_seconds() / 60
            )
            truncated = (exc.partial_content or "")[:500]
            error_text = self._format_execution_error(
                event=event,
                elapsed_minutes=elapsed_minutes,
                reason=str(exc.error),
                cost=exc.partial_cost,
                messages_received=exc.messages_received,
                partial_content=truncated,
            )
            await self._publish_to_target_chats(event, error_text)

        except Exception as exc:
            error_message = str(exc)[:500]
            logger.exception(
                "Agent execution failed for scheduled event",
                job_id=event.job_id,
                event_id=event.id,
            )
            elapsed_minutes = int(
                (datetime.now(UTC) - fired_at).total_seconds() / 60
            )
            error_text = self._format_execution_error(
                event=event,
                elapsed_minutes=elapsed_minutes,
                reason=str(exc),
                cost=0.0,
                messages_received=0,
                partial_content=None,
            )
            await self._publish_to_target_chats(event, error_text)

        finally:
            if self.job_scheduler and event.job_id:
                try:
                    await self.job_scheduler.record_job_run(
                        job_id=event.job_id,
                        fired_at=fired_at,
                        completed_at=datetime.now(UTC),
                        success=success,
                        response_summary=response_summary,
                        cost=cost,
                        error_message=error_message,
                    )
                except Exception:
                    logger.exception(
                        "Failed to record job run",
                        job_id=event.job_id,
                    )

    def _format_execution_error(
        self,
        event: ScheduledEvent,
        elapsed_minutes: int,
        reason: str,
        cost: float,
        messages_received: int,
        partial_content: Optional[str],
    ) -> str:
        """Build an HTML error notification for a failed scheduled job."""
        lines = [
            f"\u26a0\ufe0f Job <b>{escape_html(event.job_name)}</b> failed"
            f" after {elapsed_minutes} minute(s).",
            "",
            f"Reason: {escape_html(reason)}",
            f"Cost incurred: ${cost:.2f}",
            f"Messages received: {messages_received}",
        ]
        if partial_content:
            lines += [
                "",
                "Last partial output:",
                f"<blockquote>{escape_html(partial_content)}</blockquote>",
            ]
        return "\n".join(lines)

    async def _publish_to_target_chats(
        self, event: ScheduledEvent, text: str
    ) -> None:
        """Publish a notification to all target chats, or broadcast if none configured."""
        targets = event.target_chat_ids or []
        if targets:
            for chat_id in targets:
                await self.event_bus.publish(
                    AgentResponseEvent(
                        chat_id=chat_id,
                        text=text,
                        originating_event_id=event.id,
                    )
                )
        else:
            await self.event_bus.publish(
                AgentResponseEvent(
                    chat_id=0,
                    text=text,
                    originating_event_id=event.id,
                )
            )

    def _format_scheduled_header(
        self,
        event: ScheduledEvent,
        cost: float,
        working_dir: Path,
    ) -> str:
        """Build an HTML header for scheduled job notifications."""
        short_dir = Path(working_dir).name

        return (
            f"\U0001f4cb <b>{escape_html(event.job_name)}</b>\n"
            f"<i>{escape_html(short_dir)} \u00b7 {event.session_mode}"
            f" \u00b7 ${cost:.2f}</i>"
        )

    @staticmethod
    def _is_silent(content: str) -> bool:
        """Check whether the agent response signals [SILENT].

        Matches when the last non-empty line — after stripping whitespace
        and optional backtick wrapping — equals [SILENT] (case-insensitive).
        """
        for line in reversed(content.splitlines()):
            stripped = line.strip().strip("`").strip()
            if stripped:
                return stripped.upper() == "[SILENT]"
        return False

    def _build_webhook_prompt(self, event: WebhookEvent) -> str:
        """Build a Claude prompt from a webhook event."""
        payload_summary = self._summarize_payload(event.payload)

        return (
            f"A {event.provider} webhook event occurred.\n"
            f"Event type: {event.event_type_name}\n"
            f"Payload summary:\n{payload_summary}\n\n"
            f"Analyze this event and provide a concise summary. "
            f"Highlight anything that needs my attention."
        )

    def _summarize_payload(self, payload: Dict[str, Any], max_depth: int = 2) -> str:
        """Create a readable summary of a webhook payload."""
        lines: List[str] = []
        self._flatten_dict(payload, lines, max_depth=max_depth)
        # Cap at 2000 chars to keep prompt reasonable
        summary = "\n".join(lines)
        if len(summary) > 2000:
            summary = summary[:2000] + "\n... (truncated)"
        return summary

    def _flatten_dict(
        self,
        data: Any,
        lines: list,
        prefix: str = "",
        depth: int = 0,
        max_depth: int = 2,
    ) -> None:
        """Flatten a nested dict into key: value lines."""
        if depth >= max_depth:
            lines.append(f"{prefix}: ...")
            return

        if isinstance(data, dict):
            for key, value in data.items():
                full_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, (dict, list)):
                    self._flatten_dict(value, lines, full_key, depth + 1, max_depth)
                else:
                    val_str = str(value)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "..."
                    lines.append(f"{full_key}: {val_str}")
        elif isinstance(data, list):
            lines.append(f"{prefix}: [{len(data)} items]")
            for i, item in enumerate(data[:3]):  # Show first 3 items
                self._flatten_dict(item, lines, f"{prefix}[{i}]", depth + 1, max_depth)
        else:
            lines.append(f"{prefix}: {data}")
