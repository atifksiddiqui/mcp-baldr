"""Data structures for Learning Lab curriculum content.

Lessons are *data*, not code paths — each lesson carries explanations at
three depth levels (beginner / intermediate / advanced), authoritative
documentation links, and references to real Baldr source files.  Rendering
is left to consumers (the ``learn`` CLI today, a web UI later).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Level(StrEnum):
    """Explanation depth selected by the learner (global Learning Mode)."""

    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class SupportStatus(StrEnum):
    """Whether a capability is actually implemented in Baldr.

    Used to honestly label lessons: a capability is only ever shown as
    ``IMPLEMENTED`` when the gateway truly supports it.
    """

    IMPLEMENTED = "implemented"
    PARTIAL = "partial"
    NOT_IMPLEMENTED = "not_implemented"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A reference to a real Baldr source file (and optional symbol)."""

    path: str
    symbol: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class DocLink:
    """A link to authoritative external documentation."""

    title: str
    url: str


@dataclass(frozen=True, slots=True)
class Lesson:
    """A single Learning Lab lesson.

    Attributes:
        lesson_id: Stable identifier (used for progress tracking).
        title: Human-readable title.
        summary: One-line description.
        explanations: Depth-leveled prose keyed by :class:`Level`.
        support: Implementation status in Baldr (honest labeling).
        protocol_example: Optional JSON-RPC example as a display string.
        source_refs: References to real Baldr implementation files.
        doc_links: Links to authoritative MCP / SDK documentation.
    """

    lesson_id: str
    title: str
    summary: str
    explanations: dict[Level, str]
    support: SupportStatus = SupportStatus.IMPLEMENTED
    protocol_example: str | None = None
    source_refs: tuple[SourceRef, ...] = field(default_factory=tuple)
    doc_links: tuple[DocLink, ...] = field(default_factory=tuple)

    def explanation(self, level: Level) -> str:
        """Return the explanation for *level*, falling back to beginner."""
        return self.explanations.get(level) or self.explanations.get(Level.BEGINNER, "")


@dataclass(frozen=True, slots=True)
class Module:
    """A named group of related lessons."""

    module_id: str
    title: str
    description: str
    lessons: tuple[Lesson, ...] = field(default_factory=tuple)
