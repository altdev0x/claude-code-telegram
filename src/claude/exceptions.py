"""Claude-specific exceptions."""

from typing import Optional


class ClaudeError(Exception):
    """Base Claude error."""


class ClaudeTimeoutError(ClaudeError):
    """Operation timed out."""


class ClaudeIdleTimeoutError(ClaudeError):
    """No messages received from SDK within the idle timeout period."""


class ClaudeExecutionError(ClaudeError):
    """Execution failed but partial results may be available."""

    def __init__(
        self,
        error: object,
        partial_content: Optional[str] = None,
        partial_cost: float = 0.0,
        messages_received: int = 0,
    ) -> None:
        self.error = error
        self.partial_content = partial_content
        self.partial_cost = partial_cost
        self.messages_received = messages_received
        super().__init__(str(error))


class ClaudeProcessError(ClaudeError):
    """Process execution failed."""


class ClaudeParsingError(ClaudeError):
    """Failed to parse output."""


class ClaudeSessionError(ClaudeError):
    """Session management error."""


class ClaudeMCPError(ClaudeError):
    """MCP server connection or configuration error."""

    def __init__(self, message: str, server_name: Optional[str] = None) -> None:
        super().__init__(message)
        self.server_name = server_name
