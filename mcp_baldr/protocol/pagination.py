"""Opaque, stable cursor pagination for aggregated list results.

The gateway aggregates tools / resources / prompts from many downstream
servers into one namespaced set, then serves them upstream.  MCP ``*/list``
methods are paginated with an opaque ``cursor`` the client echoes back to get
the next page.

Design goals:

- **Opaque**: the client must treat the cursor as a black box.  We encode it
  as base64url JSON so its internals are not an obvious API surface.
- **Stable across membership changes**: downstream servers come and go, so a
  naive integer offset would skip or duplicate items when the set changes.
  Instead the cursor records the *last namespaced id returned*; the next page
  is everything sorted after it.  Adding/removing other items never shifts a
  stable id's position, so no item is skipped or repeated.
- **Tamper-evident / robust**: a malformed, truncated, or hand-crafted cursor
  is rejected with :class:`InvalidCursorError` (the gateway maps this to a
  JSON-RPC invalid-params error) rather than silently misbehaving.

The page size is a gateway setting; a single downstream's own pagination is
irrelevant here because the gateway always fetches a server's full list at
discovery time and paginates the *aggregate* itself.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

#: Cursor schema version — lets the format evolve without silently
#: misinterpreting an old cursor.
_CURSOR_VERSION = 1

#: Default number of items per page when the gateway has no explicit setting.
DEFAULT_PAGE_SIZE = 100


class InvalidCursorError(ValueError):
    """Raised when a pagination cursor is malformed or cannot be decoded."""


def encode_cursor(after_id: str) -> str:
    """Encode an opaque cursor pointing *after* a namespaced id.

    Args:
        after_id: The last namespaced id included in the current page.

    Returns:
        A base64url string the client echoes back to fetch the next page.
    """
    payload = {"v": _CURSOR_VERSION, "after": after_id}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> str:
    """Decode an opaque cursor, returning the ``after`` namespaced id.

    Args:
        cursor: A cursor previously produced by :func:`encode_cursor`.

    Returns:
        The namespaced id the next page should start strictly after.

    Raises:
        InvalidCursorError: If the cursor is not valid base64url, not valid
            JSON, has the wrong shape, or carries an unknown version.
    """
    if not isinstance(cursor, str) or not cursor:
        raise InvalidCursorError("cursor must be a non-empty string")
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise InvalidCursorError(f"cursor is not valid base64url: {exc}") from exc
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidCursorError(f"cursor does not contain valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidCursorError("cursor payload is not an object")
    if payload.get("v") != _CURSOR_VERSION:
        raise InvalidCursorError(f"unsupported cursor version: {payload.get('v')!r}")
    after = payload.get("after")
    if not isinstance(after, str):
        raise InvalidCursorError("cursor is missing a valid 'after' field")
    return after


def paginate(
    items: list[dict[str, Any]],
    key: str,
    cursor: str | None,
    page_size: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Paginate *items* by a stable sort on ``item[key]`` using an opaque cursor.

    Args:
        items: The full, client-facing item dicts (already namespaced).
        key: The dict key holding each item's stable, unique namespaced id
            (e.g. ``"name"`` for tools/prompts, ``"uri"`` for resources).
        cursor: The client's opaque cursor, or ``None`` for the first page.
        page_size: Maximum items to return per page (must be positive).

    Returns:
        ``(page, next_cursor)`` where *page* is the slice of items and
        *next_cursor* is an opaque cursor for the following page, or ``None``
        if this is the last page.

    Raises:
        InvalidCursorError: If *cursor* is provided but invalid.
    """
    if page_size <= 0:
        raise ValueError("page_size must be positive")

    # Stable total order over the aggregate by the item's namespaced id.
    ordered = sorted(items, key=lambda it: str(it.get(key, "")))

    start = 0
    if cursor is not None:
        after_id = decode_cursor(cursor)
        # The next page starts at the first item strictly greater than
        # ``after_id`` in the sorted order.  Using the id (not an index) keeps
        # pagination stable even if other items were added or removed.
        start = _first_index_after(ordered, key, after_id)

    page = ordered[start : start + page_size]
    end = start + len(page)
    next_cursor: str | None = None
    if end < len(ordered) and page:
        next_cursor = encode_cursor(str(page[-1].get(key, "")))
    return page, next_cursor


def _first_index_after(items: list[dict[str, Any]], key: str, after_id: str) -> int:
    """Return the index of the first item whose id sorts strictly after *after_id*."""
    for index, item in enumerate(items):
        if str(item.get(key, "")) > after_id:
            return index
    return len(items)
