"""High-level Claude Code integration facade.

Provides simple interface for bot handlers.
"""

import asyncio
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import structlog

from ..config.settings import Settings
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate
from .session import SessionManager

logger = structlog.get_logger()


class ClaudeIntegration:
    """Main integration point for Claude Code."""

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[ClaudeSDKManager] = None,
        session_manager: Optional[SessionManager] = None,
    ):
        """Initialize Claude integration facade."""
        self.config = config
        self.sdk_manager = sdk_manager or ClaudeSDKManager(config)
        self.session_manager = session_manager
        self._session_locks: Dict[Tuple[int, str], asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
        force_new: bool = False,
        ephemeral: bool = False,
        system_prompt_append: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ClaudeResponse:
        """Run Claude Code command with full integration.

        Acquires a per-user+directory lock so that concurrent calls
        (e.g. interactive message vs. scheduled job) are serialized
        and cannot collide on the same session.

        When ``ephemeral`` is True the session manager is bypassed
        entirely — no session entries are created or updated. This
        prevents cron jobs from corrupting the user's active session.
        """
        lock_key = (user_id, str(working_directory))
        async with self._session_locks[lock_key]:
            return await self._run_command_locked(
                prompt=prompt,
                working_directory=working_directory,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                ephemeral=ephemeral,
                system_prompt_append=system_prompt_append,
                model=model,
            )

    async def _run_command_locked(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
        force_new: bool = False,
        ephemeral: bool = False,
        system_prompt_append: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ClaudeResponse:
        """Inner run_command implementation, called while holding the session lock."""
        logger.info(
            "Running Claude command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            prompt_length=len(prompt),
            force_new=force_new,
            ephemeral=ephemeral,
        )

        # Ephemeral mode: bypass session manager entirely.
        # Used by cron jobs to avoid corrupting the user's active session
        # (no get_or_create_session, no update_session).
        if ephemeral:
            return await self._run_ephemeral(
                prompt=prompt,
                working_directory=working_directory,
                user_id=user_id,
                on_stream=on_stream,
                force_new=force_new,
                system_prompt_append=system_prompt_append,
                model=model,
            )

        # If no session_id provided, try to find an existing session for this
        # user+directory combination (auto-resume).
        # Skip auto-resume when force_new is set (e.g. after /new command).
        if not session_id and not force_new:
            existing_session = await self._find_resumable_session(
                user_id, working_directory
            )
            if existing_session:
                session_id = existing_session.session_id
                logger.info(
                    "Auto-resuming existing session for project",
                    session_id=session_id,
                    project_path=str(working_directory),
                    user_id=user_id,
                )

        # Get or create session
        session = await self.session_manager.get_or_create_session(
            user_id, working_directory, session_id
        )

        # Execute command
        try:
            # Continue session if we have an existing session with a real ID
            is_new = getattr(session, "is_new_session", False)
            should_continue = not is_new and bool(session.session_id)

            # For new sessions, don't pass session_id to Claude Code
            claude_session_id = session.session_id if should_continue else None

            try:
                response = await self._execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=claude_session_id,
                    continue_session=should_continue,
                    stream_callback=on_stream,
                    system_prompt_append=system_prompt_append,
                    model=model,
                )
            except Exception as resume_error:
                # If resume failed (e.g., session expired/missing on Claude's side),
                # retry as a fresh session.  The CLI returns a generic exit-code-1
                # when the session is gone, so we catch *any* error during resume.
                if should_continue:
                    logger.warning(
                        "Session resume failed, starting fresh session",
                        failed_session_id=claude_session_id,
                        error=str(resume_error),
                    )
                    # Clean up the stale session
                    await self.session_manager.remove_session(session.session_id)

                    # Create a fresh session and retry
                    session = await self.session_manager.get_or_create_session(
                        user_id, working_directory
                    )
                    response = await self._execute(
                        prompt=prompt,
                        working_directory=working_directory,
                        session_id=None,
                        continue_session=False,
                        stream_callback=on_stream,
                        system_prompt_append=system_prompt_append,
                        model=model,
                    )
                else:
                    raise

            # Update session (assigns real session_id for new sessions)
            await self.session_manager.update_session(session, response)

            # Ensure response has the session's final ID
            response.session_id = session.session_id

            if not response.session_id:
                logger.warning(
                    "No session_id after execution; session cannot be resumed",
                    user_id=user_id,
                )

            logger.info(
                "Claude command completed",
                session_id=response.session_id,
                cost=response.cost,
                duration_ms=response.duration_ms,
                num_turns=response.num_turns,
                is_error=response.is_error,
            )

            return response

        except Exception as e:
            logger.error(
                "Claude command failed",
                error=str(e),
                user_id=user_id,
                session_id=session.session_id,
            )
            raise

    async def _run_ephemeral(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
        force_new: bool = False,
        system_prompt_append: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ClaudeResponse:
        """Execute a command without touching the session store.

        Used by cron jobs so they never create, evict, or overwrite
        session entries that belong to the interactive user.

        For resume-mode cron (force_new=False), does a read-only lookup
        via _find_resumable_session to find a session ID to resume.
        """
        # Read-only: find session to resume (if not force_new)
        session_id_to_resume: Optional[str] = None
        if not force_new:
            existing = await self._find_resumable_session(user_id, working_directory)
            if existing:
                session_id_to_resume = existing.session_id

        should_continue = bool(session_id_to_resume)

        try:
            response = await self._execute(
                prompt=prompt,
                working_directory=working_directory,
                session_id=session_id_to_resume,
                continue_session=should_continue,
                stream_callback=on_stream,
                system_prompt_append=system_prompt_append,
                model=model,
            )
        except Exception:
            if should_continue:
                # Resume failed — retry as fresh session
                logger.warning(
                    "Ephemeral session resume failed, retrying as fresh",
                    failed_session_id=session_id_to_resume,
                )
                response = await self._execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=None,
                    continue_session=False,
                    stream_callback=on_stream,
                    system_prompt_append=system_prompt_append,
                    model=model,
                )
            else:
                raise

        logger.info(
            "Ephemeral command completed",
            cost=response.cost,
            duration_ms=response.duration_ms,
            num_turns=response.num_turns,
            is_error=response.is_error,
        )

        return response

    async def _execute(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable] = None,
        system_prompt_append: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ClaudeResponse:
        """Execute command via SDK."""
        kwargs: Dict[str, Any] = dict(
            prompt=prompt,
            working_directory=working_directory,
            session_id=session_id,
            continue_session=continue_session,
            stream_callback=stream_callback,
        )
        if system_prompt_append is not None:
            kwargs["system_prompt_append"] = system_prompt_append
        if model is not None:
            kwargs["model"] = model
        return await self.sdk_manager.execute_command(**kwargs)

    async def _find_resumable_session(
        self,
        user_id: int,
        working_directory: Path,
    ) -> Optional["ClaudeSession"]:  # noqa: F821
        """Find the most recent resumable session for a user in a directory.

        Returns the session if one exists that is non-expired and has a real
        (non-temporary) session ID from Claude. Returns None otherwise.
        """

        sessions = await self.session_manager._get_user_sessions(user_id)

        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory
            and bool(s.session_id)
            and not s.is_expired(self.config.session_timeout_hours)
        ]

        if not matching_sessions:
            return None

        return max(matching_sessions, key=lambda s: s.last_used)

    async def continue_session(
        self,
        user_id: int,
        working_directory: Path,
        prompt: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> Optional[ClaudeResponse]:
        """Continue the most recent session."""
        logger.info(
            "Continuing session",
            user_id=user_id,
            working_directory=str(working_directory),
            has_prompt=bool(prompt),
        )

        # Get user's sessions
        sessions = await self.session_manager._get_user_sessions(user_id)

        # Find most recent session in this directory (exclude sessions without IDs)
        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory and bool(s.session_id)
        ]

        if not matching_sessions:
            logger.info("No matching sessions found", user_id=user_id)
            return None

        # Get most recent
        latest_session = max(matching_sessions, key=lambda s: s.last_used)

        # Continue session with default prompt if none provided
        # Claude CLI requires a prompt, so we use a placeholder
        return await self.run_command(
            prompt=prompt or "Please continue where we left off",
            working_directory=working_directory,
            user_id=user_id,
            session_id=latest_session.session_id,
            on_stream=on_stream,
        )

    async def get_session_info(
        self, session_id: str, user_id: int
    ) -> Optional[Dict[str, Any]]:
        """Get session information (scoped to requesting user)."""
        return await self.session_manager.get_session_info(session_id, user_id)

    async def get_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        """Get all sessions for a user."""
        sessions = await self.session_manager._get_user_sessions(user_id)
        return [
            {
                "session_id": s.session_id,
                "project_path": str(s.project_path),
                "created_at": s.created_at.isoformat(),
                "last_used": s.last_used.isoformat(),
                "total_cost": s.total_cost,
                "message_count": s.message_count,
                "tools_used": s.tools_used,
                "expired": s.is_expired(self.config.session_timeout_hours),
            }
            for s in sessions
        ]

    async def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions."""
        return await self.session_manager.cleanup_expired_sessions()

    async def get_user_summary(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive user summary."""
        session_summary = await self.session_manager.get_user_session_summary(user_id)

        return {
            "user_id": user_id,
            **session_summary,
        }

    async def shutdown(self) -> None:
        """Shutdown integration and cleanup resources."""
        logger.info("Shutting down Claude integration")

        await self.cleanup_expired_sessions()

        logger.info("Claude integration shutdown complete")
