"""Downstream transport interface shared by all transport implementations.

The gateway talks to each downstream MCP server through a transport object.
Historically that was always :class:`~mcp_baldr.transports.stdio.StdioTransport`
(a local subprocess).  To support additional bindings (e.g. Streamable HTTP
for remote servers) without changing the gateway, the concrete surface the
gateway depends on is captured here as an abstract base class.

Any transport the gateway can drive MUST implement :class:`DownstreamTransport`.
The gateway depends only on this interface — never on a concrete transport —
so new bindings are pluggable and the stdio path is unaffected.

The interface is deliberately the *exact* surface the gateway uses today:

- lifecycle: :meth:`start`, :meth:`stop`, :meth:`restart`, :meth:`initialize`
- JSON-RPC send: :meth:`send_request`, :meth:`send_notification`,
  :meth:`send_response`, :meth:`send_error_response`
- convenience: :meth:`list_tools`, :meth:`call_tool`
- negotiated state: :attr:`name`, :attr:`is_running`,
  :attr:`server_capabilities`, :attr:`server_protocol_version`,
  :attr:`server_info`
- callbacks: :meth:`set_crash_callback`, :meth:`set_notification_callback`,
  :meth:`set_request_callback`

Not every binding can honor every method (for example, the 2026-07-28
Streamable HTTP binding carries server→client requests via Multi Round-Trip
Requests rather than as standalone requests, so :meth:`send_response` /
:meth:`send_error_response` may be inapplicable there).  A binding that cannot
support a method should document that and raise :class:`TransportError`; it
MUST still provide the method so the gateway can treat all transports
uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

#: Type for the crash callback: ``(server_name, returncode) -> Awaitable[None]``.
#: For non-process transports, ``returncode`` carries a synthetic code that
#: distinguishes a connection loss from a clean stop.
CrashCallback = Callable[[str, int], Awaitable[None]]

#: Type for the notification callback invoked when the downstream sends a
#: server-originated notification: ``(server_name, method, params) -> Awaitable``.
NotificationCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]

#: Type for the request callback invoked when the downstream sends a
#: server-originated *request*: ``(server_name, request_id, method, params)``.
RequestCallback = Callable[[str, "int | str", str, dict[str, Any]], Awaitable[None]]


class TransportError(Exception):
    """Base error for any downstream transport failure.

    Concrete transports raise a subclass (e.g.
    :class:`~mcp_baldr.transports.stdio.StdioTransportError`).  The gateway
    may catch this base type to handle failures uniformly across bindings.
    """


class DownstreamTransport(ABC):
    """Abstract interface every downstream transport must implement.

    See the module docstring for the rationale and the full surface.  The
    gateway constructs a transport, wires its callbacks, starts it, performs
    the MCP handshake via :meth:`initialize`, and thereafter routes requests
    through :meth:`send_request` / :meth:`call_tool` / :meth:`list_tools`.
    """

    # -- Negotiated state -----------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique downstream server name (used as the namespace prefix)."""

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """True if the transport currently has a live connection."""

    @property
    @abstractmethod
    def server_capabilities(self) -> dict[str, Any]:
        """Capabilities advertised by this downstream in its initialize result."""

    @property
    @abstractmethod
    def server_protocol_version(self) -> str | None:
        """Protocol version this downstream negotiated, or ``None``."""

    @property
    @abstractmethod
    def server_info(self) -> dict[str, Any]:
        """The downstream's ``serverInfo`` (name, version) from initialize."""

    # -- Callbacks ------------------------------------------------------------

    @abstractmethod
    def set_crash_callback(self, callback: CrashCallback) -> None:
        """Register an async callback invoked when the connection is lost."""

    @abstractmethod
    def set_notification_callback(self, callback: NotificationCallback) -> None:
        """Register an async callback for server-originated notifications."""

    @abstractmethod
    def set_request_callback(self, callback: RequestCallback) -> None:
        """Register an async callback for server-originated requests."""

    # -- Lifecycle ------------------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Establish the connection (spawn subprocess / open HTTP session)."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear down the connection cleanly."""

    @abstractmethod
    async def restart(self) -> None:
        """Stop (if needed), start, and re-perform the MCP handshake."""

    @abstractmethod
    async def initialize(self) -> dict[str, Any]:
        """Perform the MCP ``initialize`` handshake; return the result dict."""

    # -- JSON-RPC send --------------------------------------------------------

    @abstractmethod
    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await the matching result."""

    @abstractmethod
    async def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""

    @abstractmethod
    async def send_response(self, request_id: int | str, result: dict[str, Any]) -> None:
        """Send a JSON-RPC response to a server-originated request."""

    @abstractmethod
    async def send_error_response(self, request_id: int | str, code: int, message: str) -> None:
        """Send a JSON-RPC error response to a server-originated request."""

    # -- Convenience MCP methods ----------------------------------------------

    @abstractmethod
    async def list_tools(self) -> list[dict[str, Any]]:
        """Call ``tools/list`` and return the list of tool schemas."""

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call ``tools/call`` with the given name and arguments."""
