"""Tests for session API routes."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src.api.server import create_api_app
from src.events.bus import EventBus


def make_settings(**overrides):  # type: ignore[no-untyped-def]
    """Create a minimal mock settings object."""
    settings = MagicMock()
    settings.development_mode = True
    settings.github_webhook_secret = "gh-secret"
    settings.webhook_api_secret = overrides.get("webhook_api_secret", "test-secret")
    settings.api_server_port = 8080
    settings.debug = False
    settings.session_timeout_hours = 24
    settings.approved_directory = "/home/test/project"
    settings.allowed_users = [12345]
    return settings


def make_mock_claude_integration():  # type: ignore[no-untyped-def]
    """Create a mock ClaudeIntegration."""
    integration = AsyncMock()
    response = MagicMock()
    response.session_id = "sess-abc-123"
    response.content = "Hello from Claude"
    response.cost = 0.0042
    response.duration_ms = 1500
    response.num_turns = 2
    response.tools_used = [{"name": "Bash", "input": {"command": "echo hi"}}]
    response.is_error = False
    response.error_type = None
    integration.run_command = AsyncMock(return_value=response)
    return integration


def make_mock_db_manager():  # type: ignore[no-untyped-def]
    """Create a mock DatabaseManager with session data."""
    db = MagicMock()
    conn = AsyncMock()
    cursor = AsyncMock()

    # Simulate session rows
    now = datetime.now(UTC)
    mock_row = {
        "session_id": "sess-abc-123",
        "user_id": 12345,
        "project_path": "/home/test/project",
        "created_at": now,
        "last_used": now,
        "total_cost": 0.01,
        "total_turns": 5,
        "message_count": 3,
        "is_active": True,
    }

    cursor.fetchall = AsyncMock(return_value=[MagicMock(**mock_row)])
    conn.execute = AsyncMock(return_value=cursor)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    db.get_connection = MagicMock(return_value=conn)

    return db


def make_client(  # type: ignore[no-untyped-def]
    claude_integration=None,
    db_manager=None,
    **settings_overrides,
):
    """Create a test client with session routes mounted."""
    bus = EventBus()
    settings = make_settings(**settings_overrides)
    integration = claude_integration or make_mock_claude_integration()
    db = db_manager or make_mock_db_manager()
    app = create_api_app(
        bus, settings, db_manager=db, claude_integration=integration
    )
    return TestClient(app), integration, db


AUTH_HEADER = {"Authorization": "Bearer test-secret"}


class TestSessionRoutes:
    """Tests for /api/sessions/* endpoints."""

    def test_list_sessions(self) -> None:
        """GET /api/sessions returns session list."""
        client, _, _ = make_client()

        # Patch SessionModel.from_row in the storage models module
        with patch("src.storage.models.SessionModel.from_row") as mock_from_row:
            mock_session = MagicMock()
            mock_session.session_id = "sess-abc-123"
            mock_session.user_id = 12345
            mock_session.project_path = "/home/test/project"
            mock_session.created_at = datetime.now(UTC)
            mock_session.last_used = datetime.now(UTC)
            mock_session.total_cost = 0.01
            mock_session.total_turns = 5
            mock_session.message_count = 3
            mock_from_row.return_value = mock_session

            response = client.get("/api/sessions", headers=AUTH_HEADER)

        assert response.status_code == 200
        data = response.json()
        assert "sessions" in data

    def test_list_sessions_requires_auth(self) -> None:
        """GET /api/sessions without auth returns 401."""
        client, _, _ = make_client()

        response = client.get("/api/sessions")
        assert response.status_code == 401

    def test_send_message(self) -> None:
        """POST /api/sessions/send sends a message."""
        client, integration, _ = make_client()

        response = client.post(
            "/api/sessions/send",
            json={
                "message": "echo hello",
                "user_id": 12345,
                "working_directory": "/home/test/project",
            },
            headers=AUTH_HEADER,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "sess-abc-123"
        assert data["content"] == "Hello from Claude"
        assert data["cost"] == 0.0042
        assert data["duration_ms"] == 1500
        assert data["num_turns"] == 2
        assert data["tools_used"] == ["Bash"]
        assert data["is_error"] is False

        integration.run_command.assert_called_once()

    def test_send_message_defaults_user(self) -> None:
        """POST /api/sessions/send defaults user_id from settings."""
        client, integration, _ = make_client()

        response = client.post(
            "/api/sessions/send",
            json={"message": "test"},
            headers=AUTH_HEADER,
        )

        assert response.status_code == 200
        call_kwargs = integration.run_command.call_args
        assert call_kwargs.kwargs["user_id"] == 12345

    def test_send_message_force_new(self) -> None:
        """POST /api/sessions/send with force_new passes it through."""
        client, integration, _ = make_client()

        response = client.post(
            "/api/sessions/send",
            json={"message": "fresh start", "force_new": True},
            headers=AUTH_HEADER,
        )

        assert response.status_code == 200
        call_kwargs = integration.run_command.call_args
        assert call_kwargs.kwargs["force_new"] is True

    def test_send_message_requires_auth(self) -> None:
        """POST /api/sessions/send without auth returns 401."""
        client, _, _ = make_client()

        response = client.post(
            "/api/sessions/send",
            json={"message": "test"},
        )
        assert response.status_code == 401

    def test_get_messages(self) -> None:
        """GET /api/sessions/{id}/messages returns message history."""
        client, _, db = make_client()

        # Mock MessageRepository
        with patch("src.storage.repositories.MessageRepository") as mock_repo_cls:
            mock_repo = AsyncMock()
            mock_msg = MagicMock()
            mock_msg.prompt = "hello"
            mock_msg.response = "hi there"
            mock_msg.cost = 0.001
            mock_msg.duration_ms = 500
            mock_msg.timestamp = datetime.now(UTC)
            mock_repo.get_session_messages = AsyncMock(return_value=[mock_msg])
            mock_repo_cls.return_value = mock_repo

            response = client.get(
                "/api/sessions/sess-abc-123/messages",
                headers=AUTH_HEADER,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "sess-abc-123"
        assert len(data["messages"]) == 1
        assert data["messages"][0]["prompt"] == "hello"
        assert data["messages"][0]["response"] == "hi there"

    def test_get_messages_with_last(self) -> None:
        """GET /api/sessions/{id}/messages?last=2 limits results."""
        client, _, db = make_client()

        with patch("src.storage.repositories.MessageRepository") as mock_repo_cls:
            mock_repo = AsyncMock()
            mock_repo.get_session_messages = AsyncMock(return_value=[])
            mock_repo_cls.return_value = mock_repo

            response = client.get(
                "/api/sessions/sess-abc-123/messages?last=2",
                headers=AUTH_HEADER,
            )

        assert response.status_code == 200
        mock_repo.get_session_messages.assert_called_once_with(
            "sess-abc-123", limit=2
        )
