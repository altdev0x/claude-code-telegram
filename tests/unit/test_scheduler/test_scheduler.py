"""Tests for JobScheduler — job history, retention, session_mode."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.events.bus import EventBus
from src.scheduler.scheduler import JobScheduler
from src.storage.database import DatabaseManager


@pytest.fixture
async def db_manager(tmp_path):
    """Create a real in-memory database with migrations applied."""
    db = DatabaseManager(f"sqlite:///{tmp_path}/test.db")
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def scheduler(event_bus, db_manager):
    return JobScheduler(
        event_bus=event_bus,
        db_manager=db_manager,
        default_working_directory=Path("/tmp/test"),
    )


class TestJobHistory:
    """Test job execution history recording and retrieval."""

    async def test_record_and_retrieve_job_run(self, scheduler):
        """A recorded run appears in get_job_history."""
        now = datetime.now(UTC)
        await scheduler.record_job_run(
            job_id="job-1",
            fired_at=now,
            completed_at=now + timedelta(seconds=5),
            success=True,
            response_summary="All good",
            cost=0.05,
        )

        history = await scheduler.get_job_history("job-1")
        assert len(history) == 1
        assert history[0]["job_id"] == "job-1"
        assert history[0]["success"] == 1
        assert history[0]["cost"] == 0.05
        assert history[0]["response_summary"] == "All good"

    async def test_history_ordered_most_recent_first(self, scheduler):
        """History is returned with most recent run first."""
        base = datetime.now(UTC)
        for i in range(3):
            await scheduler.record_job_run(
                job_id="job-1",
                fired_at=base + timedelta(hours=i),
                completed_at=base + timedelta(hours=i, seconds=5),
                success=True,
            )

        history = await scheduler.get_job_history("job-1")
        assert len(history) == 3
        # Most recent first
        assert history[0]["fired_at"] > history[1]["fired_at"]

    async def test_retention_prunes_beyond_20(self, scheduler):
        """Only the 20 most recent runs are kept per job."""
        base = datetime.now(UTC)
        for i in range(25):
            await scheduler.record_job_run(
                job_id="job-1",
                fired_at=base + timedelta(hours=i),
                completed_at=base + timedelta(hours=i, seconds=1),
                success=True,
                response_summary=f"Run {i}",
            )

        history = await scheduler.get_job_history("job-1")
        assert len(history) == 20
        # Oldest run should be run 5 (0-4 pruned)
        assert history[-1]["response_summary"] == "Run 5"

    async def test_retention_is_per_job(self, scheduler):
        """Retention limit is scoped per job — other jobs are unaffected."""
        base = datetime.now(UTC)
        for i in range(22):
            await scheduler.record_job_run(
                job_id="job-A",
                fired_at=base + timedelta(hours=i),
                completed_at=base + timedelta(hours=i, seconds=1),
                success=True,
            )
        await scheduler.record_job_run(
            job_id="job-B",
            fired_at=base,
            completed_at=base + timedelta(seconds=1),
            success=True,
        )

        assert len(await scheduler.get_job_history("job-A")) == 20
        assert len(await scheduler.get_job_history("job-B")) == 1

    async def test_error_run_records_message(self, scheduler):
        """Failed runs store the error message."""
        now = datetime.now(UTC)
        await scheduler.record_job_run(
            job_id="job-1",
            fired_at=now,
            completed_at=now + timedelta(seconds=1),
            success=False,
            error_message="SDK timeout",
        )

        history = await scheduler.get_job_history("job-1")
        assert history[0]["success"] == 0
        assert history[0]["error_message"] == "SDK timeout"


class TestCascadeDelete:
    """Test that removing a job also removes its run history."""

    async def test_remove_job_deletes_runs(self, scheduler):
        """remove_job() cascades to delete associated runs."""
        # Add a job via direct DB insert (bypass APScheduler for test isolation)
        async with scheduler.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt,
                 working_directory, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                ("job-1", "test", "0 9 * * *", "hello", "/tmp"),
            )
            await conn.commit()

        now = datetime.now(UTC)
        await scheduler.record_job_run(
            job_id="job-1",
            fired_at=now,
            completed_at=now + timedelta(seconds=1),
            success=True,
        )

        assert len(await scheduler.get_job_history("job-1")) == 1

        await scheduler.remove_job("job-1")

        assert len(await scheduler.get_job_history("job-1")) == 0


class TestSessionMode:
    """Test session_mode parameter on add_job."""

    async def test_add_job_with_session_mode(self, scheduler):
        """add_job persists session_mode to the database."""
        with patch.object(scheduler._scheduler, "add_job") as mock_ap:
            mock_ap.return_value = MagicMock(id="job-1")
            await scheduler.add_job(
                job_name="test",
                cron_expression="0 9 * * *",
                prompt="hello",
                session_mode="resume",
            )

        jobs = await scheduler.list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["session_mode"] == "resume"

    async def test_add_job_default_session_mode(self, scheduler):
        """Default session_mode is 'isolated'."""
        with patch.object(scheduler._scheduler, "add_job") as mock_ap:
            mock_ap.return_value = MagicMock(id="job-2")
            await scheduler.add_job(
                job_name="test",
                cron_expression="0 9 * * *",
                prompt="hello",
            )

        jobs = await scheduler.list_jobs()
        assert jobs[0]["session_mode"] == "isolated"

    async def test_invalid_session_mode_raises(self, scheduler):
        """Invalid session_mode values are rejected."""
        with pytest.raises(ValueError, match="Invalid session_mode"):
            await scheduler.add_job(
                job_name="test",
                cron_expression="0 9 * * *",
                prompt="hello",
                session_mode="invalid",
            )
