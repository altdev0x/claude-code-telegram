"""Scheduler API routes for job management.

Exposes JobScheduler CRUD operations over HTTP for the CLI client.
All endpoints require Bearer token auth (WEBHOOK_API_SECRET).
"""

from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

logger = structlog.get_logger()


class AddJobRequest(BaseModel):
    """Request body for adding a scheduled job."""

    job_name: str
    cron_expression: str = ""
    prompt: str
    target_chat_ids: List[int] = Field(default_factory=list)
    working_directory: Optional[str] = None
    skill_name: Optional[str] = None
    created_by: int = 0
    session_mode: str = "isolated"
    trigger_type: str = "cron"
    run_date: Optional[str] = None

    @model_validator(mode="after")
    def validate_trigger(self) -> "AddJobRequest":
        """Ensure cron/date fields are consistent with trigger_type."""
        if self.trigger_type == "cron" and not self.cron_expression:
            raise ValueError("cron_expression is required when trigger_type is 'cron'")
        if self.trigger_type == "date" and not self.run_date:
            raise ValueError("run_date is required when trigger_type is 'date'")
        if self.trigger_type not in ("cron", "date"):
            raise ValueError(f"Invalid trigger_type: {self.trigger_type!r}")
        return self


class AddJobResponse(BaseModel):
    """Response after adding a job."""

    job_id: str
    status: str = "created"


class JobListResponse(BaseModel):
    """Response with list of jobs."""

    jobs: List[Dict[str, Any]]


class JobHistoryResponse(BaseModel):
    """Response with job execution history."""

    job_id: str
    runs: List[Dict[str, Any]]


class RemoveJobResponse(BaseModel):
    """Response after removing a job."""

    job_id: str
    status: str = "removed"


class TriggerJobResponse(BaseModel):
    """Response after manually triggering a job."""

    job_id: str
    status: str = "triggered"


def create_scheduler_router(
    job_scheduler: Any,
    verify_token: Any,
) -> APIRouter:
    """Create the scheduler API router.

    Args:
        job_scheduler: JobScheduler instance.
        verify_token: FastAPI dependency for Bearer token verification.
    """
    router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])

    @router.post("/jobs", response_model=AddJobResponse)
    async def add_job(
        request: AddJobRequest,
        _: None = Depends(verify_token),
    ) -> AddJobResponse:
        """Add a new scheduled job."""
        from pathlib import Path

        working_dir = (
            Path(request.working_directory) if request.working_directory else None
        )

        try:
            job_id = await job_scheduler.add_job(
                job_name=request.job_name,
                cron_expression=request.cron_expression,
                prompt=request.prompt,
                target_chat_ids=request.target_chat_ids,
                working_directory=working_dir,
                skill_name=request.skill_name,
                created_by=request.created_by,
                session_mode=request.session_mode,
                trigger_type=request.trigger_type,
                run_date=request.run_date,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.exception("Failed to add job")
            raise HTTPException(status_code=500, detail=str(e))

        return AddJobResponse(job_id=job_id)

    @router.get("/jobs", response_model=JobListResponse)
    async def list_jobs(
        _: None = Depends(verify_token),
    ) -> JobListResponse:
        """List all active scheduled jobs."""
        jobs = await job_scheduler.list_jobs()
        # Convert non-serializable types
        serializable_jobs = []
        for job in jobs:
            serializable = {
                k: (
                    str(v)
                    if not isinstance(v, (str, int, float, bool, type(None)))
                    else v
                )
                for k, v in job.items()
            }
            serializable_jobs.append(serializable)
        return JobListResponse(jobs=serializable_jobs)

    @router.delete("/jobs/{job_id}", response_model=RemoveJobResponse)
    async def remove_job(
        job_id: str,
        _: None = Depends(verify_token),
    ) -> RemoveJobResponse:
        """Remove a scheduled job and its execution history."""
        await job_scheduler.remove_job(job_id)
        return RemoveJobResponse(job_id=job_id)

    @router.post("/jobs/{job_id}/trigger", response_model=TriggerJobResponse)
    async def trigger_job(
        job_id: str,
        _: None = Depends(verify_token),
    ) -> TriggerJobResponse:
        """Manually trigger a job immediately."""
        try:
            await job_scheduler.trigger_now(job_id)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        except Exception as e:
            logger.exception("Failed to trigger job", job_id=job_id)
            raise HTTPException(status_code=500, detail=str(e))
        return TriggerJobResponse(job_id=job_id)

    @router.get("/jobs/{job_id}/history", response_model=JobHistoryResponse)
    async def job_history(
        job_id: str,
        _: None = Depends(verify_token),
    ) -> JobHistoryResponse:
        """Get execution history for a job."""
        runs = await job_scheduler.get_job_history(job_id)
        # Convert datetime objects to strings for serialization
        serializable_runs = []
        for run in runs:
            serializable = {
                k: (
                    str(v)
                    if not isinstance(v, (str, int, float, bool, type(None)))
                    else v
                )
                for k, v in run.items()
            }
            serializable_runs.append(serializable)
        return JobHistoryResponse(job_id=job_id, runs=serializable_runs)

    return router
