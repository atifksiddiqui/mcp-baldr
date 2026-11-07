#!/usr/bin/env python3
"""MCP Learning Lab — terminal front-end (Phase 1).

A read interface over the ``mcp_baldr.learning`` module.  It renders the
curriculum (MCP Fundamentals), the honest capability-support matrix, and a
learner's progress — all in the terminal.  This is deliberately a thin
viewer: the educational *data* lives in the learning module and is reused by
any future UI.

Live protocol events are produced only by a running gateway with learning
mode enabled (``mcp-baldr <config> --learn``).  This viewer therefore shows
curriculum content and progress, and clearly explains where live events come
from rather than fabricating any.

Usage::

    python scripts/learn.py lessons                 # list all lessons
    python scripts/learn.py lesson <id> [--level L]  # show one lesson
    python scripts/learn.py capabilities             # capability support matrix
    python scripts/learn.py progress                 # your learning progress
    python scripts/learn.py complete <lesson_id>     # mark a lesson complete
"""

from __future__ import annotations

import argparse
import sys

from mcp_baldr.learning import (
    ProgressTracker,
    all_lessons,
    all_modules,
    get_lesson,
)
from mcp_baldr.learning.curriculum import CAPABILITY_SUPPORT
from mcp_baldr.learning.lessons import Level, SupportStatus

# -- ANSI helpers (TTY-aware) --------------------------------------------------

_COLOR = sys.stdout.isatty()
_C = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
}


def _c(name: str, text: str) -> str:
    if not _COLOR:
        return text
    return f"{_C[name]}{text}{_C['reset']}"


_STATUS_COLOR = {
    SupportStatus.IMPLEMENTED: "green",
    SupportStatus.PARTIAL: "yellow",
    SupportStatus.NOT_IMPLEMENTED: "dim",
}

_STATUS_LABEL = {
    SupportStatus.IMPLEMENTED: "implemented",
    SupportStatus.PARTIAL: "partial",
    SupportStatus.NOT_IMPLEMENTED: "not implemented",
}


# -- Commands ------------------------------------------------------------------


def cmd_lessons(_args: argparse.Namespace) -> int:
    """List all lessons grouped by module, with completion status."""
    tracker = ProgressTracker()
    for module in all_modules():
        print(_c("bold", f"\n{module.title}") + _c("dim", f"  ({module.module_id})"))
        print(_c("dim", f"  {module.description}"))
        for lesson in module.lessons:
            done = tracker.is_lesson_complete(lesson.lesson_id)
            mark = _c("green", "✓") if done else _c("dim", "·")
            status = (
                ""
                if lesson.support is SupportStatus.IMPLEMENTED
                else _c(_STATUS_COLOR[lesson.support], f"  [{_STATUS_LABEL[lesson.support]}]")
            )
            print(f"  {mark} {_c('cyan', lesson.lesson_id)}{status}")
            print(f"      {lesson.summary}")
    print()
    return 0


def cmd_lesson(args: argparse.Namespace) -> int:
    """Show a single lesson at the chosen explanation level."""
    lesson = get_lesson(args.lesson_id)
    if lesson is None:
        print(_c("red", f"No lesson with id '{args.lesson_id}'."), file=sys.stderr)
        print("Run 'python scripts/learn.py lessons' to list available ids.", file=sys.stderr)
        return 1

    level = Level(args.level)
    print(_c("bold", f"\n{lesson.title}"))
    print(_c("dim", f"{lesson.lesson_id}  ·  level: {level.value}"))
    if lesson.support is not SupportStatus.IMPLEMENTED:
        print(_c(_STATUS_COLOR[lesson.support], f"[{_STATUS_LABEL[lesson.support]} in Baldr]"))
    print()
    print(lesson.explanation(level))

    if lesson.protocol_example:
        print(_c("bold", "\nProtocol example:"))
        print(_c("green", f"  {lesson.protocol_example}"))

    if lesson.source_refs:
        print(_c("bold", "\nBaldr source references:"))
        for ref in lesson.source_refs:
            loc = ref.path + (f" :: {ref.symbol}" if ref.symbol else "")
            note = f"  — {ref.note}" if ref.note else ""
            print(f"  {_c('cyan', loc)}{_c('dim', note)}")

    if lesson.doc_links:
        print(_c("bold", "\nFurther reading:"))
        for link in lesson.doc_links:
            print(f"  {link.title}: {_c('dim', link.url)}")

    print(_c("dim", "\nTip: --level beginner|intermediate|advanced changes the depth.\n"))
    return 0


def cmd_capabilities(_args: argparse.Namespace) -> int:
    """Show the honest capability-support matrix."""
    print(_c("bold", "\nMCP capability support in Baldr"))
    print(_c("dim", "Only capabilities Baldr actually implements are marked implemented.\n"))
    width = max(len(m) for m in CAPABILITY_SUPPORT)
    for method, status in CAPABILITY_SUPPORT.items():
        label = _c(_STATUS_COLOR[status], _STATUS_LABEL[status])
        print(f"  {method.ljust(width)}   {label}")
    print()
    return 0


def cmd_progress(_args: argparse.Namespace) -> int:
    """Show the learner's progress summary."""
    tracker = ProgressTracker()
    summary = tracker.summary()
    total_lessons = len(all_lessons())
    print(_c("bold", "\nLearning progress"))
    print(f"  lessons completed:   {summary['lessons_completed']} / {total_lessons}")
    print(f"  exercises completed: {summary['exercises_completed']}")
    print(f"  quizzes taken:       {summary['quizzes_taken']}")
    if summary["average_quiz_score"] is not None:
        print(f"  average quiz score:  {summary['average_quiz_score']:.0%}")
    print(_c("dim", f"\n  progress file: {tracker.path}\n"))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    """Mark a lesson complete."""
    lesson = get_lesson(args.lesson_id)
    if lesson is None:
        print(_c("red", f"No lesson with id '{args.lesson_id}'."), file=sys.stderr)
        return 1
    tracker = ProgressTracker()
    tracker.complete_lesson(lesson.lesson_id)
    print(_c("green", f"Marked '{lesson.lesson_id}' complete."))
    return 0


# -- Entry point ---------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="learn",
        description="MCP Learning Lab — terminal viewer for the Baldr curriculum.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("lessons", help="List all lessons.")

    p_lesson = sub.add_parser("lesson", help="Show a single lesson.")
    p_lesson.add_argument("lesson_id", help="Lesson id (see 'lessons').")
    p_lesson.add_argument(
        "--level",
        choices=[level.value for level in Level],
        default=Level.BEGINNER.value,
        help="Explanation depth (default: beginner).",
    )

    sub.add_parser("capabilities", help="Show the capability support matrix.")
    sub.add_parser("progress", help="Show your learning progress.")

    p_complete = sub.add_parser("complete", help="Mark a lesson complete.")
    p_complete.add_argument("lesson_id", help="Lesson id to mark complete.")

    args = parser.parse_args(argv)

    dispatch = {
        "lessons": cmd_lessons,
        "lesson": cmd_lesson,
        "capabilities": cmd_capabilities,
        "progress": cmd_progress,
        "complete": cmd_complete,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
