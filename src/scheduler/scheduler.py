"""Job scheduler for recurring and one-time agent tasks.

Wraps APScheduler's AsyncIOScheduler and publishes ScheduledEvents
to the event bus when jobs fire.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from apscheduler.schedulers.asyncio import (
    AsyncIOScheduler,  # type: ignore[import-untyped]
)
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.date import DateTrigger  # type: ignore[import-untyped]

from ..events.bus import EventBus
from ..events.types import ScheduledEvent
from ..storage.database import DatabaseManager

logger = structlog.get_logger()


class JobScheduler:
    """Cron scheduler that publishes ScheduledEvents to the event bus."""

    def __init__(
        self,
        event_bus: EventBus,
        db_manager: DatabaseManager,
        default_working_directory: Path,
    ) -> None:
        self.event_bus = event_bus
        self.db_manager = db_manager
        self.default_working_directory = default_working_directory
        self._scheduler = AsyncIOScheduler()

    async def start(self) -> None:
        """Load persisted jobs and start the scheduler."""
        await self._load_jobs_from_db()
        self._scheduler.start()
        logger.info("Job scheduler started")

    async def stop(self) -> None:
        """Shutdown the scheduler gracefully."""
        self._scheduler.shutdown(wait=False)
        logger.info("Job scheduler stopped")

    async def add_job(
        self,
        job_name: str,
        cron_expression: str = "",
        prompt: str = "",
        target_chat_ids: Optional[List[int]] = None,
        working_directory: Optional[Path] = None,
        skill_name: Optional[str] = None,
        created_by: int = 0,
        session_mode: str = "isolated",
        trigger_type: str = "cron",
        run_date: Optional[str] = None,
        max_turns: Optional[int] = None,
        idle_timeout_seconds: Optional[int] = None,
    ) -> str:
        """Add a new scheduled job.

        Args:
            job_name: Human-readable job name.
            cron_expression: Cron-style schedule (e.g. "0 9 * * 1-5").
                Required for trigger_type="cron".
            prompt: The prompt to send to Claude when the job fires.
            target_chat_ids: Telegram chat IDs to send the response to.
            working_directory: Working directory for Claude execution.
            skill_name: Optional skill to invoke.
            created_by: Telegram user ID of the creator.
            session_mode: "isolated" (fresh session) or "resume" (continue
                          the user's most recent session for the directory).
            trigger_type: "cron" for recurring or "date" for one-time.
            run_date: ISO 8601 datetime for date triggers.
            max_turns: Override max conversation turns (None = global default,
                0 = unlimited).
            idle_timeout_seconds: Override idle watchdog timeout in seconds
                (None = global default).

        Returns:
            The job ID.
        """
        if session_mode not in ("isolated", "resume"):
            raise ValueError(f"Invalid session_mode: {session_mode!r}")
        if trigger_type not in ("cron", "date"):
            raise ValueError(f"Invalid trigger_type: {trigger_type!r}")
        if trigger_type == "cron" and not cron_expression:
            raise ValueError("cron_expression is required for trigger_type='cron'")
        if trigger_type == "date" and not run_date:
            raise ValueError("run_date is required for trigger_type='date'")

        if trigger_type == "cron":
            trigger = CronTrigger.from_crontab(cron_expression)
        else:
            trigger = DateTrigger(run_date=run_date)

        work_dir = working_directory or self.default_working_directory

        job = self._scheduler.add_job(
            self._fire_event,
            trigger=trigger,
            kwargs={
                "job_id": "",  # placeholder, back-filled below
                "job_name": job_name,
                "prompt": prompt,
                "working_directory": str(work_dir),
                "target_chat_ids": target_chat_ids or [],
                "skill_name": skill_name,
                "session_mode": session_mode,
                "created_by": created_by,
                "cron_expression": cron_expression,
                "max_turns": max_turns,
                "idle_timeout_seconds": idle_timeout_seconds,
            },
            name=job_name,
        )

        # Back-fill the auto-generated job_id into kwargs
        job.modify(
            kwargs={
                **job.kwargs,
                "job_id": job.id,
            }
        )

        # Persist to database
        await self._save_job(
            job_id=job.id,
            job_name=job_name,
            cron_expression=cron_expression,
            prompt=prompt,
            target_chat_ids=target_chat_ids or [],
            working_directory=str(work_dir),
            skill_name=skill_name,
            created_by=created_by,
            session_mode=session_mode,
            trigger_type=trigger_type,
            run_date=run_date,
            max_turns=max_turns,
            idle_timeout_seconds=idle_timeout_seconds,
        )

        logger.info(
            "Scheduled job added",
            job_id=job.id,
            job_name=job_name,
            trigger_type=trigger_type,
            cron=cron_expression or None,
            run_date=run_date or None,
            session_mode=session_mode,
        )
        return str(job.id)

    async def remove_job(self, job_id: str) -> bool:
        """Remove a scheduled job and its execution history."""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            logger.warning("Job not found in scheduler", job_id=job_id)

        await self._delete_job(job_id)
        await self._delete_job_runs(job_id)
        logger.info("Scheduled job removed", job_id=job_id)
        return True

    async def list_jobs(self) -> List[Dict[str, Any]]:
        """List all scheduled jobs from the database."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scheduled_jobs WHERE is_active = 1 ORDER BY created_at"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Look up a single active job by ID.

        Returns:
            Job dict or None if not found / inactive.
        """
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scheduled_jobs WHERE job_id = ? AND is_active = 1",
                (job_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def trigger_now(self, job_id: str) -> bool:
        """Manually trigger a job immediately via the event bus.

        Constructs and publishes a ScheduledEvent without going through
        APScheduler, so one-time jobs are NOT soft-deleted.

        Raises:
            ValueError: If the job does not exist or is inactive.
        """
        job = await self.get_job(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")

        chat_ids_str = job.get("target_chat_ids", "")
        chat_ids = (
            [int(x) for x in chat_ids_str.split(",") if x.strip()]
            if isinstance(chat_ids_str, str) and chat_ids_str
            else []
        )

        event = ScheduledEvent(
            job_id=job_id,
            job_name=job.get("job_name", ""),
            prompt=job.get("prompt", ""),
            working_directory=Path(job.get("working_directory", ".")),
            target_chat_ids=chat_ids,
            skill_name=job.get("skill_name"),
            session_mode=job.get("session_mode", "isolated"),
            created_by=job.get("created_by", 0),
            cron_expression=job.get("cron_expression", ""),
            idle_timeout_seconds=job.get("idle_timeout_seconds"),
            max_turns=job.get("max_turns"),
        )

        logger.info(
            "Manual job trigger",
            job_id=job_id,
            job_name=job.get("job_name"),
            event_id=event.id,
        )

        await self.event_bus.publish(event)
        return True

    async def record_job_run(
        self,
        job_id: str,
        fired_at: datetime,
        completed_at: datetime,
        success: bool,
        response_summary: Optional[str] = None,
        cost: float = 0.0,
        error_message: Optional[str] = None,
    ) -> None:
        """Record a job execution and enforce per-job retention (20 runs)."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_job_runs
                (job_id, fired_at, completed_at, success, response_summary,
                 cost, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    fired_at.isoformat(),
                    completed_at.isoformat(),
                    success,
                    response_summary,
                    cost,
                    error_message,
                ),
            )
            # Prune old runs beyond retention limit
            await conn.execute(
                """
                DELETE FROM scheduled_job_runs
                WHERE job_id = ? AND id NOT IN (
                    SELECT id FROM scheduled_job_runs
                    WHERE job_id = ?
                    ORDER BY fired_at DESC
                    LIMIT 20
                )
                """,
                (job_id, job_id),
            )
            await conn.commit()

    async def get_job_history(
        self, job_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Get execution history for a job."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM scheduled_job_runs
                WHERE job_id = ?
                ORDER BY fired_at DESC
                LIMIT ?
                """,
                (job_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def _fire_event(
        self,
        job_id: str,
        job_name: str,
        prompt: str,
        working_directory: str,
        target_chat_ids: List[int],
        skill_name: Optional[str],
        session_mode: str = "isolated",
        created_by: int = 0,
        cron_expression: str = "",
        max_turns: Optional[int] = None,
        idle_timeout_seconds: Optional[int] = None,
    ) -> None:
        """Called by APScheduler when a job triggers. Publishes a ScheduledEvent."""
        event = ScheduledEvent(
            job_id=job_id,
            job_name=job_name,
            prompt=prompt,
            working_directory=Path(working_directory),
            target_chat_ids=target_chat_ids,
            skill_name=skill_name,
            session_mode=session_mode,
            created_by=created_by,
            cron_expression=cron_expression,
            idle_timeout_seconds=idle_timeout_seconds,
            max_turns=max_turns,
        )

        logger.info(
            "Scheduled job fired",
            job_id=job_id,
            job_name=job_name,
            event_id=event.id,
            session_mode=session_mode,
        )

        await self.event_bus.publish(event)

        # Soft-delete one-time (date) jobs after firing
        if not cron_expression:
            try:
                await self._delete_job(job_id)
                logger.info("One-time job soft-deleted after firing", job_id=job_id)
            except Exception:
                logger.exception("Failed to soft-delete one-time job", job_id=job_id)

    async def _load_jobs_from_db(self) -> None:
        """Load persisted jobs and re-register them with APScheduler."""
        try:
            async with self.db_manager.get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT * FROM scheduled_jobs WHERE is_active = 1"
                )
                rows = list(await cursor.fetchall())

            loaded = 0
            for row in rows:
                row_dict = dict(row)
                try:
                    ttype = row_dict.get("trigger_type", "cron") or "cron"
                    cron_expr = row_dict.get("cron_expression", "")
                    run_date_str = row_dict.get("run_date")

                    if ttype == "date":
                        if not run_date_str:
                            logger.warning(
                                "Skipping date job with no run_date",
                                job_id=row_dict["job_id"],
                            )
                            continue
                        run_dt = datetime.fromisoformat(run_date_str)
                        if run_dt.tzinfo is None:
                            run_dt = run_dt.replace(tzinfo=UTC)
                        if run_dt < datetime.now(UTC):
                            logger.info(
                                "Skipping expired date job",
                                job_id=row_dict["job_id"],
                                run_date=run_date_str,
                            )
                            await self._delete_job(row_dict["job_id"])
                            continue
                        trigger = DateTrigger(run_date=run_date_str)
                    else:
                        trigger = CronTrigger.from_crontab(cron_expr)

                    # Parse target_chat_ids from stored string
                    chat_ids_str = row_dict.get("target_chat_ids", "")
                    chat_ids = (
                        [int(x) for x in chat_ids_str.split(",") if x.strip()]
                        if chat_ids_str
                        else []
                    )

                    self._scheduler.add_job(
                        self._fire_event,
                        trigger=trigger,
                        kwargs={
                            "job_id": row_dict["job_id"],
                            "job_name": row_dict["job_name"],
                            "prompt": row_dict["prompt"],
                            "working_directory": row_dict["working_directory"],
                            "target_chat_ids": chat_ids,
                            "skill_name": row_dict.get("skill_name"),
                            "session_mode": row_dict.get("session_mode", "isolated"),
                            "created_by": row_dict.get("created_by", 0),
                            "cron_expression": cron_expr,
                            "max_turns": row_dict.get("max_turns"),
                            "idle_timeout_seconds": row_dict.get(
                                "idle_timeout_seconds"
                            ),
                        },
                        id=row_dict["job_id"],
                        name=row_dict["job_name"],
                        replace_existing=True,
                    )
                    loaded += 1
                    logger.debug(
                        "Loaded scheduled job from DB",
                        job_id=row_dict["job_id"],
                        job_name=row_dict["job_name"],
                        trigger_type=ttype,
                    )
                except Exception:
                    logger.exception(
                        "Failed to load scheduled job",
                        job_id=row_dict.get("job_id"),
                    )

            logger.info("Loaded scheduled jobs from database", count=loaded)
        except Exception:
            # Table might not exist yet on first run
            logger.debug("No scheduled_jobs table found, starting fresh")

    async def _save_job(
        self,
        job_id: str,
        job_name: str,
        cron_expression: str,
        prompt: str,
        target_chat_ids: List[int],
        working_directory: str,
        skill_name: Optional[str],
        created_by: int,
        session_mode: str = "isolated",
        trigger_type: str = "cron",
        run_date: Optional[str] = None,
        max_turns: Optional[int] = None,
        idle_timeout_seconds: Optional[int] = None,
    ) -> None:
        """Persist a job definition to the database."""
        chat_ids_str = ",".join(str(cid) for cid in target_chat_ids)
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt, target_chat_ids,
                 working_directory, skill_name, created_by, is_active,
                 session_mode, trigger_type, run_date, max_turns,
                 idle_timeout_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_name,
                    cron_expression,
                    prompt,
                    chat_ids_str,
                    working_directory,
                    skill_name,
                    created_by,
                    session_mode,
                    trigger_type,
                    run_date,
                    max_turns,
                    idle_timeout_seconds,
                ),
            )
            await conn.commit()

    async def _delete_job(self, job_id: str) -> None:
        """Soft-delete a job from the database."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                "UPDATE scheduled_jobs SET is_active = 0 WHERE job_id = ?",
                (job_id,),
            )
            await conn.commit()

    async def _delete_job_runs(self, job_id: str) -> None:
        """Delete all execution history for a job."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                "DELETE FROM scheduled_job_runs WHERE job_id = ?",
                (job_id,),
            )
            await conn.commit()
