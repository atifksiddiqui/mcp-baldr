"""Learning progress tracking, persisted to a JSON file.

Progress (lessons completed, exercises completed, quiz scores) is kept
entirely separate from gateway operational data — it lives in its own file
and is never consulted by the protocol path.  Corruption is tolerated: a
malformed or missing file yields empty progress rather than an error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Default location for the progress file (user home, dotfile).
DEFAULT_PROGRESS_PATH = Path.home() / ".mcp_baldr_learning_progress.json"


@dataclass
class ProgressState:
    """In-memory representation of a learner's progress."""

    lessons_completed: set[str] = field(default_factory=set)
    exercises_completed: set[str] = field(default_factory=set)
    quiz_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lessons_completed": sorted(self.lessons_completed),
            "exercises_completed": sorted(self.exercises_completed),
            "quiz_scores": self.quiz_scores,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgressState:
        return cls(
            lessons_completed=set(data.get("lessons_completed", []) or []),
            exercises_completed=set(data.get("exercises_completed", []) or []),
            quiz_scores=dict(data.get("quiz_scores", {}) or {}),
        )


class ProgressTracker:
    """Load, mutate, and persist :class:`ProgressState` to disk.

    Args:
        path: Location of the JSON progress file.
    """

    def __init__(self, path: str | Path = DEFAULT_PROGRESS_PATH) -> None:
        self.path = Path(path)
        self.state = self._load()

    # -- Persistence ----------------------------------------------------------

    def _load(self) -> ProgressState:
        """Load progress, tolerating a missing or corrupt file."""
        if not self.path.exists():
            return ProgressState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return ProgressState()
            return ProgressState.from_dict(raw)
        except (json.JSONDecodeError, OSError, ValueError):
            # Corrupt or unreadable — start fresh rather than crash.
            return ProgressState()

    def save(self) -> None:
        """Persist the current state to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.state.to_dict(), indent=2),
            encoding="utf-8",
        )

    # -- Mutations ------------------------------------------------------------

    def complete_lesson(self, lesson_id: str) -> None:
        """Mark a lesson complete and persist."""
        self.state.lessons_completed.add(lesson_id)
        self.save()

    def complete_exercise(self, exercise_id: str) -> None:
        """Mark an exercise complete and persist."""
        self.state.exercises_completed.add(exercise_id)
        self.save()

    def record_quiz(self, quiz_id: str, score: float) -> None:
        """Record a quiz score (0.0–1.0), keeping the best, and persist."""
        prior = self.state.quiz_scores.get(quiz_id)
        if prior is None or score > prior:
            self.state.quiz_scores[quiz_id] = score
        self.save()

    # -- Queries --------------------------------------------------------------

    def is_lesson_complete(self, lesson_id: str) -> bool:
        return lesson_id in self.state.lessons_completed

    def is_exercise_complete(self, exercise_id: str) -> bool:
        return exercise_id in self.state.exercises_completed

    def summary(self) -> dict[str, Any]:
        """Return a compact progress summary for display."""
        return {
            "lessons_completed": len(self.state.lessons_completed),
            "exercises_completed": len(self.state.exercises_completed),
            "quizzes_taken": len(self.state.quiz_scores),
            "average_quiz_score": (
                round(sum(self.state.quiz_scores.values()) / len(self.state.quiz_scores), 3)
                if self.state.quiz_scores
                else None
            ),
        }
