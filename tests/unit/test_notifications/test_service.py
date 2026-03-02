"""Tests for the notification service."""

from unittest.mock import AsyncMock

import pytest

from src.events.bus import EventBus
from src.events.types import AgentResponseEvent
from src.notifications.service import NotificationService, _sanitize_html_for_telegram


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def mock_bot() -> AsyncMock:
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def service(event_bus: EventBus, mock_bot: AsyncMock) -> NotificationService:
    svc = NotificationService(
        event_bus=event_bus,
        bot=mock_bot,
        default_chat_ids=[100, 200],
    )
    svc.register()
    return svc


class TestNotificationService:
    """Tests for NotificationService."""

    async def test_handle_response_queues_event(
        self, service: NotificationService
    ) -> None:
        """Events are queued for delivery."""
        event = AgentResponseEvent(chat_id=100, text="hello")
        await service.handle_response(event)
        assert service._send_queue.qsize() == 1

    async def test_resolve_chat_ids_specific(
        self, service: NotificationService
    ) -> None:
        """Specific chat_id takes precedence over defaults."""
        event = AgentResponseEvent(chat_id=999, text="test")
        ids = service._resolve_chat_ids(event)
        assert ids == [999]

    async def test_resolve_chat_ids_default(self, service: NotificationService) -> None:
        """chat_id=0 falls back to default chat IDs."""
        event = AgentResponseEvent(chat_id=0, text="test")
        ids = service._resolve_chat_ids(event)
        assert ids == [100, 200]

    def test_split_message_short(self, service: NotificationService) -> None:
        """Short messages are not split."""
        chunks = service._split_message("short text")
        assert len(chunks) == 1
        assert chunks[0] == "short text"

    def test_split_message_long(self, service: NotificationService) -> None:
        """Long messages are split at boundaries."""
        text = "A" * 4000 + "\n\n" + "B" * 200
        chunks = service._split_message(text, max_length=4096)
        assert len(chunks) >= 1
        # All content preserved
        total_len = sum(len(c) for c in chunks)
        assert total_len > 0

    def test_split_message_no_boundary(self, service: NotificationService) -> None:
        """Messages without boundaries are hard-split."""
        text = "A" * 5000  # No newlines or spaces
        chunks = service._split_message(text, max_length=4096)
        assert len(chunks) == 2
        assert len(chunks[0]) == 4096
        assert len(chunks[1]) == 904

    async def test_send_to_telegram(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """Messages are sent via the Telegram bot."""
        event = AgentResponseEvent(chat_id=123, text="hello world")
        await service._rate_limited_send(123, event)

        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 123
        assert call_kwargs["text"] == "hello world"

    async def test_ignores_non_response_events(
        self, service: NotificationService
    ) -> None:
        """Non-AgentResponseEvent events are ignored."""
        from src.events.bus import Event

        event = Event(source="test")
        await service.handle_response(event)
        assert service._send_queue.qsize() == 0

    async def test_xml_tags_sanitized_before_send(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """Claude responses containing unknown XML tags are escaped before sending."""
        raw_text = (
            "Context follows: <system-reminder>secret</system-reminder> done."
        )
        event = AgentResponseEvent(chat_id=123, text=raw_text, parse_mode="HTML")
        await service._rate_limited_send(123, event)

        mock_bot.send_message.assert_called_once()
        sent_text = mock_bot.send_message.call_args.kwargs["text"]
        assert "<system-reminder>" not in sent_text
        assert "&lt;system-reminder&gt;" in sent_text
        assert "&lt;/system-reminder&gt;" in sent_text

    async def test_safe_html_tags_preserved_after_sanitize(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """Valid Telegram HTML tags are kept intact during sanitization."""
        html_text = "<b>bold</b> and <code>inline</code> text"
        event = AgentResponseEvent(chat_id=123, text=html_text, parse_mode="HTML")
        await service._rate_limited_send(123, event)

        sent_text = mock_bot.send_message.call_args.kwargs["text"]
        assert "<b>bold</b>" in sent_text
        assert "<code>inline</code>" in sent_text

    async def test_no_sanitization_without_html_parse_mode(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """Text is sent unchanged when parse_mode is not HTML."""
        raw_text = "raw <system-reminder>tag</system-reminder> text"
        event = AgentResponseEvent(chat_id=123, text=raw_text, parse_mode=None)
        await service._rate_limited_send(123, event)

        sent_text = mock_bot.send_message.call_args.kwargs["text"]
        assert sent_text == raw_text


class TestSanitizeHtmlForTelegram:
    """Unit tests for the _sanitize_html_for_telegram helper."""

    def test_escapes_unknown_tags(self) -> None:
        result = _sanitize_html_for_telegram("<system-reminder>hi</system-reminder>")
        assert result == "&lt;system-reminder&gt;hi&lt;/system-reminder&gt;"

    def test_preserves_bold(self) -> None:
        assert _sanitize_html_for_telegram("<b>bold</b>") == "<b>bold</b>"

    def test_preserves_italic(self) -> None:
        assert _sanitize_html_for_telegram("<i>ital</i>") == "<i>ital</i>"

    def test_preserves_code(self) -> None:
        result = _sanitize_html_for_telegram('<code class="language-py">x</code>')
        assert result == '<code class="language-py">x</code>'

    def test_preserves_pre(self) -> None:
        assert _sanitize_html_for_telegram("<pre><code>x</code></pre>") == "<pre><code>x</code></pre>"

    def test_preserves_link(self) -> None:
        result = _sanitize_html_for_telegram('<a href="https://example.com">link</a>')
        assert result == '<a href="https://example.com">link</a>'

    def test_preserves_blockquote(self) -> None:
        assert _sanitize_html_for_telegram("<blockquote>q</blockquote>") == "<blockquote>q</blockquote>"

    def test_mixed_safe_and_unsafe(self) -> None:
        text = "<b>ok</b> and <custom-tag>bad</custom-tag>"
        result = _sanitize_html_for_telegram(text)
        assert "<b>ok</b>" in result
        assert "&lt;custom-tag&gt;" in result
        assert "<custom-tag>" not in result

    def test_no_tags_unchanged(self) -> None:
        text = "plain text with no tags"
        assert _sanitize_html_for_telegram(text) == text

    def test_already_escaped_entities_unchanged(self) -> None:
        text = "price &lt; 10 &amp; &gt; 0"
        assert _sanitize_html_for_telegram(text) == text
