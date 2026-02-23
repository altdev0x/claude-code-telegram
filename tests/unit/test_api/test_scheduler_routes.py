"""Tests for scheduler API routes."""

from unittest.mock import AsyncMock, MagicMock

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
    return settings


def make_mock_scheduler():  # type: ignore[no-untyped-def]
    """Create a mock JobScheduler with async methods."""
    scheduler = AsyncMock()
    scheduler.add_job = AsyncMock(return_value="job-123")
    scheduler.list_jobs = AsyncMock(return_value=[])
    scheduler.remove_job = AsyncMock(return_value=True)
    scheduler.get_job_history = AsyncMock(return_value=[])
    return scheduler


def make_client(scheduler=None, **settings_overrides):  # type: ignore[no-untyped-def]
    """Create a test client with scheduler routes mounted."""
    bus = EventBus()
    settings = make_settings(**settings_overrides)
    sched = scheduler or make_mock_scheduler()
    app = create_api_app(bus, settings, job_scheduler=sched)
    return TestClient(app), sched


AUTH_HEADER = {"Authorization": "Bearer test-secret"}


class TestSchedulerRoutes:
    """Tests for /api/scheduler/* endpoints."""

    def test_add_job(self) -> None:
        """POST /api/scheduler/jobs creates a job."""
        client, scheduler = make_client()

        response = client.post(
            "/api/scheduler/jobs",
            json={
                "job_name": "Daily check",
                "cron_expression": "0 9 * * *",
                "prompt": "Run tests",
                "session_mode": "isolated",
            },
            headers=AUTH_HEADER,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job-123"
        assert data["status"] == "created"
        scheduler.add_job.assert_called_once()

    def test_add_job_with_resume_mode(self) -> None:
        """POST with session_mode=resume passes through correctly."""
        client, scheduler = make_client()

        response = client.post(
            "/api/scheduler/jobs",
            json={
                "job_name": "Monitor",
                "cron_expression": "*/30 * * * *",
                "prompt": "Check status",
                "session_mode": "resume",
            },
            headers=AUTH_HEADER,
        )

        assert response.status_code == 200
        call_kwargs = scheduler.add_job.call_args.kwargs
        assert call_kwargs["session_mode"] == "resume"

    def test_list_jobs(self) -> None:
        """GET /api/scheduler/jobs returns job list."""
        scheduler = make_mock_scheduler()
        scheduler.list_jobs.return_value = [
            {
                "job_id": "job-1",
                "job_name": "Test",
                "cron_expression": "0 9 * * *",
                "prompt": "hello",
                "is_active": True,
                "session_mode": "isolated",
            }
        ]
        client, _ = make_client(scheduler=scheduler)

        response = client.get("/api/scheduler/jobs", headers=AUTH_HEADER)

        assert response.status_code == 200
        data = response.json()
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["job_id"] == "job-1"

    def test_remove_job(self) -> None:
        """DELETE /api/scheduler/jobs/{id} removes the job."""
        client, scheduler = make_client()

        response = client.delete("/api/scheduler/jobs/job-123", headers=AUTH_HEADER)

        assert response.status_code == 200
        assert response.json()["status"] == "removed"
        scheduler.remove_job.assert_called_once_with("job-123")

    def test_job_history(self) -> None:
        """GET /api/scheduler/jobs/{id}/history returns run history."""
        scheduler = make_mock_scheduler()
        scheduler.get_job_history.return_value = [
            {
                "id": 1,
                "job_id": "job-1",
                "fired_at": "2026-02-23T09:00:00",
                "completed_at": "2026-02-23T09:00:05",
                "success": True,
                "cost": 0.05,
                "response_summary": "OK",
                "error_message": None,
            }
        ]
        client, _ = make_client(scheduler=scheduler)

        response = client.get("/api/scheduler/jobs/job-1/history", headers=AUTH_HEADER)

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job-1"
        assert len(data["runs"]) == 1
        assert data["runs"][0]["success"] == 1


class TestSchedulerRoutesAuth:
    """Test authentication on scheduler endpoints."""

    def test_missing_auth_returns_401(self) -> None:
        """Requests without auth are rejected."""
        client, _ = make_client()

        response = client.get("/api/scheduler/jobs")
        assert response.status_code == 401

    def test_wrong_token_returns_401(self) -> None:
        """Requests with wrong token are rejected."""
        client, _ = make_client()

        response = client.get(
            "/api/scheduler/jobs",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    def test_no_secret_configured_returns_500(self) -> None:
        """If WEBHOOK_API_SECRET is not set, returns 500."""
        client, _ = make_client(webhook_api_secret="")

        response = client.get(
            "/api/scheduler/jobs",
            headers={"Authorization": "Bearer anything"},
        )
        assert response.status_code == 500


class TestSchedulerRoutesNotMounted:
    """When no scheduler is provided, routes are not mounted."""

    def test_no_scheduler_no_routes(self) -> None:
        """Without a scheduler, /api/scheduler/* returns 404."""
        bus = EventBus()
        settings = make_settings()
        app = create_api_app(bus, settings, job_scheduler=None)
        client = TestClient(app)

        response = client.get("/api/scheduler/jobs", headers=AUTH_HEADER)
        assert response.status_code == 404
