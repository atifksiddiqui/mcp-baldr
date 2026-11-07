"""Structured educational events for the MCP Learning Lab.

An :class:`EducationalEvent` is a sanitized, read-only snapshot of a single
protocol interaction observed by the gateway.  Events are produced by the
:class:`~mcp_baldr.learning.event_collector.EventCollector` and consumed by
educational interfaces (the ``learn`` CLI today, a web UI later).

Design principles:

- Events never carry secrets; metadata is redacted before an event is built.
- Events are immutable (frozen dataclass) so a consumer cannot mutate the
  collector's history.
- Every event explicitly records whether it describes a *live* runtime
  interaction or a *simulated* educational example (``is_simulated``), so the
  two can never be confused.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CapabilityCategory(StrEnum):
    """High-level MCP capability a protocol event belongs to."""

    INITIALIZATION = "initialization"
    TOOLS = "tools"
    RESOURCES = "resources"
    PROMPTS = "prompts"
    NOTIFICATIONS = "notifications"
    SAMPLING = "sampling"
    ELICITATION = "elicitation"
    COMPLETION = "completion"
    LOGGING = "logging"
    PING = "ping"
    ERROR = "error"
    OTHER = "other"

    @classmethod
    def from_method(cls, method: str) -> CapabilityCategory:
        """Classify a JSON-RPC method name into a capability category.

        The mapping reflects the MCP specification's method namespaces.  A
        method the gateway does not implement is still classified correctly
        for educational display; implementation status is tracked separately
        in the curriculum, not here.
        """
        if not method:
            return cls.OTHER
        if method == "initialize":
            return cls.INITIALIZATION
        if method == "ping":
            return cls.PING
        if method.startswith("notifications/"):
            return cls.NOTIFICATIONS
        if method.startswith("tools/"):
            return cls.TOOLS
        if method.startswith("resources/"):
            return cls.RESOURCES
        if method.startswith("prompts/"):
            return cls.PROMPTS
        if method.startswith("sampling/"):
            return cls.SAMPLING
        if method.startswith("elicitation/"):
            return cls.ELICITATION
        if method.startswith("completion/"):
            return cls.COMPLETION
        if method.startswith("logging/"):
            return cls.LOGGING
        return cls.OTHER


class ProcessingStage(StrEnum):
    """Stage of the gateway pipeline at which an event was observed.

    These stages correspond to real instrumentation points in
    ``mcp_baldr.gateway.server.Gateway`` — they are not invented.  See the
    gateway's ``_serve`` loop and ``call_tool`` for the matching call sites.
    """

    CLIENT_REQUEST = "client_request"
    CLIENT_NOTIFICATION = "client_notification"
    NAMESPACE_RESOLUTION = "namespace_resolution"
    DOWNSTREAM_REQUEST = "downstream_request"
    DOWNSTREAM_RESPONSE = "downstream_response"
    CLIENT_RESPONSE = "client_response"
    ERROR = "error"
    GATEWAY_READY = "gateway_ready"


@dataclass(frozen=True, slots=True)
class EducationalEvent:
    """A sanitized snapshot of one observed protocol interaction.

    Attributes:
        event_id: Unique identifier for this event.
        timestamp: Unix epoch seconds when the event was recorded.
        session_id: Identifier correlating events within one gateway run.
        method: JSON-RPC method name (e.g. ``"tools/call"``), if applicable.
        request_id: JSON-RPC request id, if applicable.
        category: The MCP capability category this event belongs to.
        stage: The gateway pipeline stage at which the event was observed.
        downstream_server: Name of the downstream server involved, if any.
        duration_ms: Processing duration in milliseconds, if measured.
        success: Whether the interaction succeeded (``None`` if not a result).
        is_simulated: ``True`` for educational examples, ``False`` for live
            runtime events.  Never ambiguous.
        metadata: Redacted, display-safe details (already sanitized).
    """

    event_id: str
    timestamp: float
    session_id: str
    stage: ProcessingStage
    category: CapabilityCategory
    method: str | None = None
    request_id: int | str | None = None
    downstream_server: str | None = None
    duration_ms: float | None = None
    success: bool | None = None
    is_simulated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def new_id() -> str:
        """Generate a unique event id."""
        return uuid.uuid4().hex

    @staticmethod
    def now() -> float:
        """Return the current timestamp (Unix epoch seconds)."""
        return time.time()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation for display/transport."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "stage": self.stage.value,
            "category": self.category.value,
            "method": self.method,
            "request_id": self.request_id,
            "downstream_server": self.downstream_server,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "is_simulated": self.is_simulated,
            "metadata": self.metadata,
        }
