"""Tests for JobScheduler — job history, retention, session_mode, DateTrigger, trigger_now."""

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


def _mock_ap_job(job_id: str = "job-1") -> MagicMock:
    """Create a mock APScheduler job with modify() support."""
    mock_job = MagicMock(id=job_id)
    mock_job.kwargs = {}
    mock_job.modify = MagicMock()
    return mock_job


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
            mock_ap.return_value = _mock_ap_job("job-1")
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
            mock_ap.return_value = _mock_ap_job("job-2")
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


class TestDateTrigger:
    """Test DateTrigger (one-time job) support."""

    async def test_add_job_date_trigger(self, scheduler):
        """add_job with trigger_type='date' persists correctly."""
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        with patch.object(scheduler._scheduler, "add_job") as mock_ap:
            mock_ap.return_value = _mock_ap_job("job-d1")
            job_id = await scheduler.add_job(
                job_name="one-time",
                prompt="do it",
                trigger_type="date",
                run_date=future,
            )

        assert job_id == "job-d1"
        jobs = await scheduler.list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["trigger_type"] == "date"
        assert jobs[0]["run_date"] == future
        assert jobs[0]["cron_expression"] == ""

    async def test_add_job_cron_requires_expression(self, scheduler):
        """trigger_type='cron' without cron_expression raises ValueError."""
        with pytest.raises(ValueError, match="cron_expression is required"):
            await scheduler.add_job(
                job_name="test",
                prompt="hello",
                trigger_type="cron",
                cron_expression="",
            )

    async def test_add_job_date_requires_run_date(self, scheduler):
        """trigger_type='date' without run_date raises ValueError."""
        with pytest.raises(ValueError, match="run_date is required"):
            await scheduler.add_job(
                job_name="test",
                prompt="hello",
                trigger_type="date",
            )

    async def test_add_job_invalid_trigger_type(self, scheduler):
        """Invalid trigger_type raises ValueError."""
        with pytest.raises(ValueError, match="Invalid trigger_type"):
            await scheduler.add_job(
                job_name="test",
                prompt="hello",
                trigger_type="interval",
                cron_expression="0 * * * *",
            )


class TestFireEvent:
    """Test _fire_event sets job_id and propagates identity fields."""

    async def test_fire_event_sets_job_id(self, scheduler, event_bus):
        """_fire_event sets job_id on the published ScheduledEvent (bug fix)."""
        published: list = []
        event_bus.publish = AsyncMock(side_effect=lambda e: published.append(e))

        await scheduler._fire_event(
            job_id="job-42",
            job_name="test",
            prompt="hello",
            working_directory="/tmp",
            target_chat_ids=[],
            skill_name=None,
            cron_expression="0 9 * * *",
        )

        assert len(published) == 1
        assert published[0].job_id == "job-42"

    async def test_fire_event_passes_created_by(self, scheduler, event_bus):
        """_fire_event propagates created_by through the event."""
        published: list = []
        event_bus.publish = AsyncMock(side_effect=lambda e: published.append(e))

        await scheduler._fire_event(
            job_id="job-1",
            job_name="test",
            prompt="hello",
            working_directory="/tmp",
            target_chat_ids=[],
            skill_name=None,
            created_by=999,
            cron_expression="0 9 * * *",
        )

        assert published[0].created_by == 999

    async def test_fire_event_passes_cron_expression(self, scheduler, event_bus):
        """_fire_event propagates cron_expression through the event."""
        published: list = []
        event_bus.publish = AsyncMock(side_effect=lambda e: published.append(e))

        await scheduler._fire_event(
            job_id="job-1",
            job_name="test",
            prompt="hello",
            working_directory="/tmp",
            target_chat_ids=[],
            skill_name=None,
            cron_expression="*/5 * * * *",
        )

        assert published[0].cron_expression == "*/5 * * * *"

    async def test_fire_event_soft_deletes_date_job(self, scheduler, event_bus):
        """_fire_event soft-deletes one-time jobs (empty cron_expression)."""
        # Insert a date job directly
        async with scheduler.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt,
                 working_directory, is_active, trigger_type, run_date)
                VALUES (?, ?, ?, ?, ?, 1, 'date', '2026-12-01T00:00:00')
                """,
                ("job-once", "once", "", "do it", "/tmp"),
            )
            await conn.commit()

        await scheduler._fire_event(
            job_id="job-once",
            job_name="once",
            prompt="do it",
            working_directory="/tmp",
            target_chat_ids=[],
            skill_name=None,
            cron_expression="",  # empty = date job
        )

        # Should be soft-deleted
        jobs = await scheduler.list_jobs()
        assert not any(j["job_id"] == "job-once" for j in jobs)

    async def test_fire_event_does_not_delete_cron_job(self, scheduler, event_bus):
        """_fire_event does NOT delete recurring cron jobs."""
        async with scheduler.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt,
                 working_directory, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                ("job-cron", "cron", "0 9 * * *", "hello", "/tmp"),
            )
            await conn.commit()

        await scheduler._fire_event(
            job_id="job-cron",
            job_name="cron",
            prompt="hello",
            working_directory="/tmp",
            target_chat_ids=[],
            skill_name=None,
            cron_expression="0 9 * * *",
        )

        jobs = await scheduler.list_jobs()
        assert any(j["job_id"] == "job-cron" for j in jobs)


class TestLoadJobsFromDB:
    """Test _load_jobs_from_db with date trigger support."""

    async def test_skips_expired_date_jobs(self, scheduler):
        """Expired date jobs are skipped (and soft-deleted) on load."""
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        async with scheduler.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt,
                 working_directory, is_active, trigger_type, run_date)
                VALUES (?, ?, ?, ?, ?, 1, 'date', ?)
                """,
                ("job-expired", "expired", "", "hello", "/tmp", past),
            )
            await conn.commit()

        await scheduler._load_jobs_from_db()

        # Should not be registered in APScheduler
        ap_job_ids = [j.id for j in scheduler._scheduler.get_jobs()]
        assert "job-expired" not in ap_job_ids

    async def test_loads_future_date_jobs(self, scheduler):
        """Future date jobs are loaded into APScheduler."""
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        async with scheduler.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt,
                 working_directory, is_active, trigger_type, run_date)
                VALUES (?, ?, ?, ?, ?, 1, 'date', ?)
                """,
                ("job-future", "future", "", "hello", "/tmp", future),
            )
            await conn.commit()

        # Need to start scheduler for get_jobs to work
        scheduler._scheduler.start()
        try:
            await scheduler._load_jobs_from_db()
            ap_job_ids = [j.id for j in scheduler._scheduler.get_jobs()]
            assert "job-future" in ap_job_ids
        finally:
            scheduler._scheduler.shutdown(wait=False)

    async def test_loads_cron_jobs_with_identity_fields(self, scheduler):
        """Cron jobs loaded from DB include job_id, created_by, cron_expression in kwargs."""
        async with scheduler.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt,
                 working_directory, is_active, created_by)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                ("job-k", "ktest", "0 9 * * *", "hello", "/tmp", 42),
            )
            await conn.commit()

        scheduler._scheduler.start()
        try:
            await scheduler._load_jobs_from_db()
            ap_jobs = scheduler._scheduler.get_jobs()
            job = next(j for j in ap_jobs if j.id == "job-k")
            assert job.kwargs["job_id"] == "job-k"
            assert job.kwargs["created_by"] == 42
            assert job.kwargs["cron_expression"] == "0 9 * * *"
        finally:
            scheduler._scheduler.shutdown(wait=False)


class TestGetJob:
    """Test get_job lookup."""

    async def test_get_job_returns_active_job(self, scheduler):
        """get_job returns a dict for an active job."""
        async with scheduler.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt,
                 working_directory, is_active, target_chat_ids, created_by)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                ("job-g1", "getter", "0 9 * * *", "hello", "/tmp", "111,222", 42),
            )
            await conn.commit()

        job = await scheduler.get_job("job-g1")
        assert job is not None
        assert job["job_name"] == "getter"
        assert job["created_by"] == 42

    async def test_get_job_returns_none_for_missing(self, scheduler):
        """get_job returns None when job doesn't exist."""
        assert await scheduler.get_job("nonexistent") is None

    async def test_get_job_returns_none_for_inactive(self, scheduler):
        """get_job returns None for soft-deleted jobs."""
        async with scheduler.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt,
                 working_directory, is_active)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                ("job-inactive", "gone", "0 9 * * *", "hello", "/tmp"),
            )
            await conn.commit()

        assert await scheduler.get_job("job-inactive") is None


class TestTriggerNow:
    """Test manual job triggering via trigger_now."""

    async def test_trigger_publishes_event(self, scheduler, event_bus):
        """trigger_now publishes a ScheduledEvent with correct fields."""
        async with scheduler.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt,
                 working_directory, is_active, target_chat_ids,
                 session_mode, created_by, skill_name)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    "job-t1", "trigger-test", "0 9 * * *", "run tests",
                    "/tmp/proj", "111,222", "resume", 42, None,
                ),
            )
            await conn.commit()

        published: list = []
        event_bus.publish = AsyncMock(side_effect=lambda e: published.append(e))

        result = await scheduler.trigger_now("job-t1")

        assert result is True
        assert len(published) == 1
        evt = published[0]
        assert evt.job_id == "job-t1"
        assert evt.job_name == "trigger-test"
        assert evt.prompt == "run tests"
        assert evt.target_chat_ids == [111, 222]
        assert evt.session_mode == "resume"
        assert evt.created_by == 42
        assert evt.cron_expression == "0 9 * * *"

    async def test_trigger_nonexistent_raises(self, scheduler):
        """trigger_now raises ValueError for missing jobs."""
        with pytest.raises(ValueError, match="Job not found"):
            await scheduler.trigger_now("no-such-job")

    async def test_trigger_does_not_delete_date_job(self, scheduler, event_bus):
        """trigger_now does NOT soft-delete one-time jobs."""
        async with scheduler.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt,
                 working_directory, is_active, trigger_type, run_date)
                VALUES (?, ?, ?, ?, ?, 1, 'date', '2026-12-01T00:00:00')
                """,
                ("job-once-t", "once", "", "do it", "/tmp"),
            )
            await conn.commit()

        event_bus.publish = AsyncMock()
        await scheduler.trigger_now("job-once-t")

        # Job should still be active
        job = await scheduler.get_job("job-once-t")
        assert job is not None
