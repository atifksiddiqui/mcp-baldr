"""Bounded in-memory collector for educational protocol events.

The :class:`EventCollector` is the single ingestion point for the Learning
Lab.  The gateway's existing inspector call sites feed it observations; it
redacts them, builds :class:`~mcp_baldr.learning.events.EducationalEvent`
objects, and retains a bounded history for educational consumers.

Overhead is bounded by design (requirement: bounded runtime overhead):

- History is a ``collections.deque`` with a fixed ``maxlen``; old events are
  discarded automatically.
- Recording is O(1) plus the cost of redaction over the supplied metadata.
- When no collector is attached, the gateway does no educational work at all.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable, Iterator

from mcp_baldr.learning.events import (
    CapabilityCategory,
    EducationalEvent,
    ProcessingStage,
)
from mcp_baldr.learning.redaction import redact

#: Default maximum number of events retained in memory.
DEFAULT_MAX_EVENTS = 500


class EventCollector:
    """Collects, redacts, correlates, and serves educational events.

    Args:
        session_id: Identifier correlating events within one gateway run.
        max_events: Maximum events retained before the oldest are discarded.
    """

    def __init__(self, session_id: str, max_events: int = DEFAULT_MAX_EVENTS) -> None:
        self.session_id = session_id
        self._events: deque[EducationalEvent] = deque(maxlen=max_events)
        # Tracks request start times by (request_id) to compute durations
        # when the matching response is recorded.  Bounded implicitly by the
        # request/response lifecycle; cleared on correlation.
        self._pending_starts: dict[int | str, float] = {}

    # -- Recording ------------------------------------------------------------

    def record(
        self,
        stage: ProcessingStage,
        *,
        method: str | None = None,
        request_id: int | str | None = None,
        downstream_server: str | None = None,
        success: bool | None = None,
        is_simulated: bool = False,
        metadata: dict | None = None,
        category: CapabilityCategory | None = None,
    ) -> EducationalEvent:
        """Record one observation and return the stored event.

        Metadata is redacted here; callers may pass raw protocol data.
        """
        cat = category or (
            CapabilityCategory.from_method(method) if method else CapabilityCategory.OTHER
        )
        if stage is ProcessingStage.ERROR:
            cat = CapabilityCategory.ERROR

        duration_ms: float | None = None
        if request_id is not None:
            if stage is ProcessingStage.CLIENT_REQUEST:
                self._pending_starts[request_id] = time.perf_counter()
            elif stage in (ProcessingStage.CLIENT_RESPONSE, ProcessingStage.ERROR):
                start = self._pending_starts.pop(request_id, None)
                if start is not None:
                    duration_ms = round((time.perf_counter() - start) * 1000.0, 3)

        event = EducationalEvent(
            event_id=EducationalEvent.new_id(),
            timestamp=EducationalEvent.now(),
            session_id=self.session_id,
            stage=stage,
            category=cat,
            method=method,
            request_id=request_id,
            downstream_server=downstream_server,
            duration_ms=duration_ms,
            success=success,
            is_simulated=is_simulated,
            metadata=redact(metadata) if metadata else {},
        )
        self._events.append(event)
        return event

    # -- Querying -------------------------------------------------------------

    def all_events(self) -> list[EducationalEvent]:
        """Return all retained events, oldest first."""
        return list(self._events)

    def filter(
        self,
        *,
        category: CapabilityCategory | None = None,
        stage: ProcessingStage | None = None,
        simulated: bool | None = None,
        request_id: int | str | None = None,
    ) -> list[EducationalEvent]:
        """Return events matching all supplied criteria.

        Any argument left as ``None`` is not filtered on.
        """
        results: Iterable[EducationalEvent] = self._events
        if category is not None:
            results = (e for e in results if e.category is category)
        if stage is not None:
            results = (e for e in results if e.stage is stage)
        if simulated is not None:
            results = (e for e in results if e.is_simulated is simulated)
        if request_id is not None:
            results = (e for e in results if e.request_id == request_id)
        return list(results)

    def correlated(self, request_id: int | str) -> list[EducationalEvent]:
        """Return all events sharing a request id, in recorded order.

        This reconstructs the lifecycle of a single request — client
        request, namespace resolution, downstream request/response, and the
        client response or error.
        """
        return [e for e in self._events if e.request_id == request_id]

    def get(self, event_id: str) -> EducationalEvent | None:
        """Return a single event by id, or ``None`` if absent."""
        for event in self._events:
            if event.event_id == event_id:
                return event
        return None

    def clear(self) -> None:
        """Discard all retained events and pending correlations."""
        self._events.clear()
        self._pending_starts.clear()

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[EducationalEvent]:
        return iter(self._events)
