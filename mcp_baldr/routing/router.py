"""Unified request router for the MCP aggregation gateway.

Every incoming MCP request is dispatched through a single router rather than
a growing ``if method == ...`` ladder.  A capability handler registers the
methods it serves; the router validates the method exists and invokes the
handler.

This keeps routing logic in one place so tools, resources, prompts, and other
capabilities do not each re-implement dispatch.  Per-capability concerns
(namespace resolution, destination selection, downstream forwarding) live in
the handlers the gateway registers, not here.

The router is intentionally transport-agnostic and side-effect-free except
for invoking handlers: the gateway owns I/O, telemetry, and error framing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

#: A handler takes the JSON-RPC ``params`` and returns the JSON-RPC ``result``.
MethodHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class MethodNotFoundError(Exception):
    """Raised when no handler is registered for a requested method.

    The gateway maps this to JSON-RPC error code ``-32601``.
    """


class RequestRouter:
    """Maps MCP method names to async handlers.

    Handlers are registered once at gateway startup.  Dispatch is an O(1)
    dict lookup, and an unknown method raises :class:`MethodNotFoundError`
    so the gateway can return a proper ``-32601`` response.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, MethodHandler] = {}

    def register(self, method: str, handler: MethodHandler) -> None:
        """Register *handler* for an MCP *method* (e.g. ``"tools/list"``)."""
        if method in self._handlers:
            logger.warning("Overriding existing handler", extra={"method": method})
        self._handlers[method] = handler

    def register_many(self, handlers: dict[str, MethodHandler]) -> None:
        """Register several method→handler pairs at once."""
        for method, handler in handlers.items():
            self.register(method, handler)

    def supports(self, method: str) -> bool:
        """True if a handler is registered for *method*."""
        return method in self._handlers

    def methods(self) -> list[str]:
        """Return all registered method names."""
        return list(self._handlers)

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a request to its handler and return the result.

        Raises:
            MethodNotFoundError: If no handler is registered for *method*.
        """
        handler = self._handlers.get(method)
        if handler is None:
            raise MethodNotFoundError(f"Method not found: {method}")
        return await handler(params)
