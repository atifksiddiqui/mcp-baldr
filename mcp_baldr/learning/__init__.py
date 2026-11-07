"""MCP Learning Lab — educational layer for the Baldr gateway.

This package is a loosely-coupled educational module.  It observes protocol
activity through sanitized events, serves structured lesson content, and
tracks learning progress.  It never participates in protocol processing: the
gateway operates identically whether or not the Learning Lab is active.

Public API:

- :class:`EducationalEvent`, :class:`CapabilityCategory`,
  :class:`ProcessingStage` — the sanitized event model.
- :class:`EventCollector` — bounded, redacting event store.
- :func:`redact` — the sensitive-data redaction boundary.
- :class:`ProgressTracker` — JSON-backed progress persistence.
- curriculum accessors — structured lesson content.
"""

from mcp_baldr.learning.curriculum import (
    all_lessons,
    all_modules,
    capability_status,
    get_lesson,
)
from mcp_baldr.learning.event_collector import EventCollector
from mcp_baldr.learning.events import (
    CapabilityCategory,
    EducationalEvent,
    ProcessingStage,
)
from mcp_baldr.learning.lessons import (
    DocLink,
    Lesson,
    Level,
    Module,
    SourceRef,
    SupportStatus,
)
from mcp_baldr.learning.progress import ProgressState, ProgressTracker
from mcp_baldr.learning.redaction import redact, redact_env

__all__ = [
    "CapabilityCategory",
    "DocLink",
    "EducationalEvent",
    "EventCollector",
    "Lesson",
    "Level",
    "Module",
    "ProcessingStage",
    "ProgressState",
    "ProgressTracker",
    "SourceRef",
    "SupportStatus",
    "all_lessons",
    "all_modules",
    "capability_status",
    "get_lesson",
    "redact",
    "redact_env",
]
