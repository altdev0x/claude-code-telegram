"""Session API routes for observability and interaction.

Exposes session listing, message sending, and message history over HTTP
for the CLI client. All endpoints require Bearer token auth (WEBHOOK_API_SECRET).
"""

from pathlib import Path
from typing import Any, List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

logger = structlog.get_logger()


class SendMessageRequest(BaseModel):
    """Request body for sending a message to a session."""

    message: str
    user_id: Optional[int] = None
    working_directory: Optional[str] = None
    session_id: Optional[str] = None
    force_new: bool = False


class SendMessageResponse(BaseModel):
    """Response after sending a message."""

    session_id: str
    content: str
    cost: float
    duration_ms: int
    num_turns: int
    tools_used: List[str] = Field(default_factory=list)
    is_error: bool = False


class SessionInfo(BaseModel):
    """Session summary for listing."""

    session_id: str
    user_id: int
    project_path: str
    created_at: str
    last_used: str
    total_cost: float = 0.0
    total_turns: int = 0
    message_count: int = 0
    has_transcript: bool = False
    expired: bool = False


class SessionListResponse(BaseModel):
    """Response with list of sessions."""

    sessions: List[SessionInfo]


class MessageInfo(BaseModel):
    """Single prompt/response pair."""

    prompt: str
    response: Optional[str] = None
    cost: float = 0.0
    duration_ms: Optional[int] = None
    timestamp: str


class MessageListResponse(BaseModel):
    """Response with message history."""

    session_id: str
    messages: List[MessageInfo]


def create_session_router(
    claude_integration: Any,
    db_manager: Any,
    settings: Any,
    verify_token: Any,
) -> APIRouter:
    """Create the session API router.

    Args:
        claude_integration: ClaudeIntegration instance.
        db_manager: DatabaseManager instance.
        settings: Settings instance.
        verify_token: FastAPI dependency for Bearer token verification.
    """
    router = APIRouter(prefix="/api/sessions", tags=["sessions"])

    def _has_transcript(session_id: str) -> bool:
        """Check if a JSONL transcript exists for a session."""
        claude_dir = Path.home() / ".claude" / "projects"
        if not claude_dir.exists():
            return False
        for subdir in claude_dir.iterdir():
            if subdir.is_dir():
                jsonl = subdir / f"{session_id}.jsonl"
                if jsonl.exists():
                    return True
        return False

    @router.get("", response_model=SessionListResponse)
    async def list_sessions(
        dir: Optional[str] = None,
        user_id: Optional[int] = None,
        all: bool = False,
        _: None = Depends(verify_token),
    ) -> SessionListResponse:
        """List sessions with optional filtering."""
        from datetime import UTC, datetime

        from ..storage.models import SessionModel

        # Build query — include inactive sessions only when requested
        query = "SELECT * FROM sessions"
        conditions = []
        params: List[Any] = []

        if not all:
            conditions.append("is_active = TRUE")
        if dir:
            conditions.append("project_path = ?")
            params.append(dir)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY last_used DESC"

        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()

        sessions_raw = [SessionModel.from_row(row) for row in rows]

        timeout_hours = settings.session_timeout_hours
        result = []
        for s in sessions_raw:
            age_hours = (
                datetime.now(UTC) - s.last_used
            ).total_seconds() / 3600
            expired = age_hours > timeout_hours

            result.append(
                SessionInfo(
                    session_id=s.session_id,
                    user_id=s.user_id,
                    project_path=str(s.project_path),
                    created_at=s.created_at.isoformat(),
                    last_used=s.last_used.isoformat(),
                    total_cost=s.total_cost,
                    total_turns=s.total_turns,
                    message_count=s.message_count,
                    has_transcript=_has_transcript(s.session_id),
                    expired=expired,
                )
            )

        return SessionListResponse(sessions=result)

    @router.post("/send", response_model=SendMessageResponse)
    async def send_message(
        request: SendMessageRequest,
        _: None = Depends(verify_token),
    ) -> SendMessageResponse:
        """Send a message through Claude integration."""
        # Resolve defaults
        user_id = request.user_id
        if user_id is None:
            if settings.allowed_users:
                user_id = settings.allowed_users[0]
            else:
                raise HTTPException(
                    status_code=400,
                    detail="user_id required (no default user configured)",
                )

        working_directory = Path(
            request.working_directory or str(settings.approved_directory)
        )

        try:
            response = await claude_integration.run_command(
                prompt=request.message,
                working_directory=working_directory,
                user_id=user_id,
                session_id=request.session_id,
                force_new=request.force_new,
            )
        except Exception as e:
            logger.exception("Session send failed")
            raise HTTPException(status_code=500, detail=str(e))

        # Record the interaction for audit trail
        try:
            from ..storage.facade import Storage

            storage = Storage.__new__(Storage)
            storage.db_manager = db_manager
            from ..storage.repositories import (
                CostTrackingRepository,
                MessageRepository,
                SessionRepository,
                ToolUsageRepository,
                UserRepository,
            )

            storage.messages = MessageRepository(db_manager)
            storage.tools = ToolUsageRepository(db_manager)
            storage.costs = CostTrackingRepository(db_manager)
            storage.users = UserRepository(db_manager)
            storage.sessions = SessionRepository(db_manager)
            from ..storage.repositories import AuditLogRepository

            storage.audit = AuditLogRepository(db_manager)

            await storage.save_claude_interaction(
                user_id=user_id,
                session_id=response.session_id,
                prompt=request.message,
                response=response,
            )
        except Exception:
            logger.warning(
                "Failed to save CLI session interaction to storage",
                exc_info=True,
            )

        return SendMessageResponse(
            session_id=response.session_id,
            content=response.content,
            cost=response.cost,
            duration_ms=response.duration_ms,
            num_turns=response.num_turns,
            tools_used=[t["name"] for t in response.tools_used],
            is_error=response.is_error,
        )

    @router.get("/{session_id}/messages", response_model=MessageListResponse)
    async def get_messages(
        session_id: str,
        last: int = 5,
        _: None = Depends(verify_token),
    ) -> MessageListResponse:
        """Get message history for a session."""
        from ..storage.repositories import MessageRepository

        message_repo = MessageRepository(db_manager)
        messages = await message_repo.get_session_messages(session_id, limit=last)

        return MessageListResponse(
            session_id=session_id,
            messages=[
                MessageInfo(
                    prompt=m.prompt,
                    response=m.response,
                    cost=m.cost,
                    duration_ms=m.duration_ms,
                    timestamp=(
                        m.timestamp.isoformat()
                        if hasattr(m.timestamp, "isoformat")
                        else str(m.timestamp)
                    ),
                )
                for m in messages
            ],
        )

    return router
