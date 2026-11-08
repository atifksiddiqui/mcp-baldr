"""HttpTransport: Streamable HTTP bridge for remote downstream MCP servers.

A pluggable alternative to :class:`~mcp_baldr.transports.stdio.StdioTransport`
that lets the gateway aggregate **remote** MCP servers reached over the MCP
``2026-07-28`` Streamable HTTP binding (a single MCP endpoint accepting
``POST``, with per-request JSON or SSE responses).

It is built on the official SDK client (:func:`mcp.client.streamable_http.
streamable_http_client` + :class:`mcp.client.session.ClientSession`), so the
wire-level details — required request-metadata headers, SSE framing, protocol
version negotiation — are handled by the SDK rather than reimplemented here.
No new top-level dependency is introduced: the HTTP stack ships transitively
with ``mcp``.

Scope (first cut)
-----------------
This transport proxies the request/response capabilities the gateway needs:
``initialize``, ``tools/list``, ``tools/call``, ``resources/*``,
``prompts/*``, ``completion/complete``, ``logging/setLevel``.  The interface's
raw JSON-RPC surface (:meth:`send_request`) is implemented by dispatching to
the SDK session's typed methods and converting the typed result back to a
plain dict shaped like the MCP wire format.

The configured ``timeout`` is honored: it is applied both as the HTTP client
timeout and as the SDK session's read timeout, so a slow or unresponsive
remote server fails with a :class:`HttpTransportError` rather than hanging.

Deliberately **not** supported yet (tracked as roadmap items):

- Server→client requests (sampling / elicitation / roots).  Under 2026-07-28
  Streamable HTTP these are carried via Multi Round-Trip Requests (MRTR,
  SEP-2322) embedded in results, not as standalone requests, so
  :meth:`send_response` / :meth:`send_error_response` are inapplicable and
  raise :class:`HttpTransportError`.
- Live change notifications via ``subscriptions/listen`` (long-lived SSE).
- Authentication beyond static per-request headers.
- Proactive connection-health detection.  A dropped session is surfaced
  reactively — the next request fails with :class:`HttpTransportError`, which
  the gateway reports as an error — but there is no background monitor that
  fires the crash callback to auto-unregister a silently-gone remote server
  the way the stdio transport does for a crashed subprocess.

The stdio transport and the gateway's own stdio server loop are entirely
unaffected by this module.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

import httpx2
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from mcp_baldr.config.models import HttpServerConfig
from mcp_baldr.transports.base import (
    CrashCallback,
    DownstreamTransport,
    NotificationCallback,
    RequestCallback,
    TransportError,
)

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = types.LATEST_PROTOCOL_VERSION
CLIENT_INFO = types.Implementation(name="mcp-baldr", version="0.1.0")

#: Synthetic "returncode" reported to the crash callback when an HTTP session
#: is lost unexpectedly (there is no process exit code for a remote server).
CONNECTION_LOST_CODE = -1

#: How result models are rendered back to plain MCP-wire dicts: alias keys
#: (``inputSchema`` not ``input_schema``), JSON-mode scalars, drop ``None``.
_DUMP_KW = {"by_alias": True, "mode": "json", "exclude_none": True}


class HttpTransportError(TransportError):
    """Raised when the Streamable HTTP transport encounters an error."""


class HttpTransport(DownstreamTransport):
    """Drives a remote downstream MCP server over Streamable HTTP.

    Lifecycle mirrors :class:`StdioTransport`: :meth:`start` opens the HTTP
    session, :meth:`initialize` performs the MCP handshake, requests are routed
    through :meth:`send_request` / :meth:`call_tool` / :meth:`list_tools`, and
    :meth:`stop` tears the session down.

    Health model (differs from stdio): there is no background crash monitor.
    A lost or unresponsive remote server is detected *reactively* — the request
    raises :class:`HttpTransportError`, which the gateway surfaces as an error
    — rather than proactively via the crash callback.  Proactive health
    detection for HTTP is a roadmap item.
    """

    def __init__(self, config: HttpServerConfig, timeout: float | None = None):
        self.config = config
        self.timeout = timeout if timeout is not None else config.timeout
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._initialized = False

        # Negotiated state (populated by initialize()).
        self._server_capabilities: dict[str, Any] = {}
        self._server_protocol_version: str | None = None
        self._server_info: dict[str, Any] = {}

        # Callbacks.  The notification/request callbacks are accepted for
        # interface parity but not driven until subscriptions/listen and MRTR
        # land (roadmap); the crash callback is likewise not fired because HTTP
        # health is detected reactively (see the class docstring).
        self._on_crash: CrashCallback | None = None
        self._on_notification: NotificationCallback | None = None
        self._on_request: RequestCallback | None = None

    # -- Negotiated state -----------------------------------------------------

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_running(self) -> bool:
        return self._session is not None

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return dict(self._server_capabilities)

    @property
    def server_protocol_version(self) -> str | None:
        return self._server_protocol_version

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    # -- Callbacks ------------------------------------------------------------

    def set_crash_callback(self, callback: CrashCallback) -> None:
        # Accepted for interface parity.  HTTP health is currently detected
        # reactively (a failing request raises), so this is not fired yet;
        # proactive health detection is a roadmap item.
        self._on_crash = callback

    def set_notification_callback(self, callback: NotificationCallback) -> None:
        # Accepted for interface parity.  Live change notifications over HTTP
        # require a subscriptions/listen stream, which is a roadmap item.
        self._on_notification = callback

    def set_request_callback(self, callback: RequestCallback) -> None:
        # Accepted for interface parity.  Server→client requests over 2026-07-28
        # Streamable HTTP use MRTR (embedded in results), not standalone
        # requests, so this is never invoked in the current implementation.
        self._on_request = callback

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Open the Streamable HTTP session to the remote server.

        Builds an ``httpx`` client carrying the configured static headers and
        timeout, enters the SDK's ``streamable_http_client`` and
        ``ClientSession`` context managers, and keeps them alive on an
        :class:`AsyncExitStack` until :meth:`stop`.
        """
        if self._session is not None:
            raise HttpTransportError(f"Transport '{self.name}' is already started")

        logger.info(
            "Opening Streamable HTTP session",
            extra={"server": self.name, "url": self.config.url},
        )

        stack = AsyncExitStack()
        try:
            http_client = create_mcp_http_client(
                headers=dict(self.config.headers) or None,
                timeout=httpx2.Timeout(self.timeout),
            )
            read, write, *_ = await stack.enter_async_context(
                streamable_http_client(self.config.url, http_client=http_client)
            )
            session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=self.timeout,
                    client_info=CLIENT_INFO,
                )
            )
        except Exception as exc:
            await stack.aclose()
            raise HttpTransportError(
                f"Failed to open HTTP session to '{self.name}' at {self.config.url}: {exc}"
            ) from exc

        self._stack = stack
        self._session = session
        logger.info("Opened Streamable HTTP session", extra={"server": self.name})

    async def stop(self) -> None:
        """Close the HTTP session and release the SDK context managers."""
        self._initialized = False
        self._session = None
        stack, self._stack = self._stack, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                logger.debug(
                    "Error while closing HTTP session (ignored)",
                    extra={"server": self.name},
                )
        logger.info("Closed Streamable HTTP session", extra={"server": self.name})

    async def restart(self) -> None:
        """Close (if open), reopen, and re-perform the MCP handshake."""
        if self._session is not None:
            await self.stop()
        await self.start()
        await self.initialize()
        logger.info("Restarted Streamable HTTP session", extra={"server": self.name})

    # -- MCP handshake --------------------------------------------------------

    async def initialize(self) -> dict[str, Any]:
        """Perform the MCP ``initialize`` handshake via the SDK session."""
        session = self._require_session()
        if self._initialized:
            raise HttpTransportError(f"Server '{self.name}' is already initialized")

        try:
            result = await session.initialize()
        except Exception as exc:
            raise HttpTransportError(f"initialize failed for '{self.name}': {exc}") from exc

        data = result.model_dump(**_DUMP_KW)
        self._initialized = True
        self._server_capabilities = data.get("capabilities", {}) or {}
        self._server_protocol_version = data.get("protocolVersion")
        self._server_info = data.get("serverInfo", {}) or {}
        logger.info(
            "Initialized MCP server (HTTP)",
            extra={
                "server": self.name,
                "serverInfo": self._server_info,
                "protocolVersion": self._server_protocol_version,
            },
        )
        return data

    # -- JSON-RPC send --------------------------------------------------------

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,  # noqa: ARG002 — session read timeout applies
    ) -> dict[str, Any]:
        """Dispatch a JSON-RPC request to the SDK session's typed method.

        The typed result is converted back to a plain MCP-wire dict so the
        gateway's registries and routing treat HTTP and stdio identically.
        The effective timeout is the session read timeout configured at
        :meth:`start` (from the server's ``timeout`` setting); a per-call
        override is accepted for interface parity but not yet plumbed through
        the SDK session.
        """
        session = self._require_session()
        params = params or {}
        try:
            result = await self._dispatch(session, method, params)
        except HttpTransportError:
            raise
        except Exception as exc:
            raise HttpTransportError(f"Request '{method}' to '{self.name}' failed: {exc}") from exc
        return result

    async def _dispatch(
        self, session: ClientSession, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Map a JSON-RPC method to the matching typed ``ClientSession`` call."""
        if method == "tools/list":
            return (await session.list_tools()).model_dump(**_DUMP_KW)
        if method == "tools/call":
            res = await session.call_tool(params["name"], params.get("arguments") or {})
            return res.model_dump(**_DUMP_KW)
        if method == "resources/list":
            return (await session.list_resources()).model_dump(**_DUMP_KW)
        if method == "resources/templates/list":
            return (await session.list_resource_templates()).model_dump(**_DUMP_KW)
        if method == "resources/read":
            return (await session.read_resource(params["uri"])).model_dump(**_DUMP_KW)
        if method == "resources/subscribe":
            return (await session.subscribe_resource(params["uri"])).model_dump(**_DUMP_KW)
        if method == "resources/unsubscribe":
            return (await session.unsubscribe_resource(params["uri"])).model_dump(**_DUMP_KW)
        if method == "prompts/list":
            return (await session.list_prompts()).model_dump(**_DUMP_KW)
        if method == "prompts/get":
            res = await session.get_prompt(params["name"], params.get("arguments"))
            return res.model_dump(**_DUMP_KW)
        if method == "logging/setLevel":
            return (await session.set_logging_level(params["level"])).model_dump(**_DUMP_KW)
        if method == "completion/complete":
            return await self._complete(session, params)
        if method == "ping":
            return (await session.send_ping()).model_dump(**_DUMP_KW)
        raise HttpTransportError(f"HTTP transport does not support request method: {method}")

    async def _complete(self, session: ClientSession, params: dict[str, Any]) -> dict[str, Any]:
        """Translate a raw ``completion/complete`` params dict to the typed call."""
        ref = params.get("ref") or {}
        ref_type = ref.get("type")
        if ref_type == "ref/prompt":
            typed_ref: Any = types.PromptReference(type="ref/prompt", name=ref.get("name", ""))
        elif ref_type == "ref/resource":
            typed_ref = types.ResourceTemplateReference(type="ref/resource", uri=ref.get("uri", ""))
        else:
            raise HttpTransportError(f"Unsupported completion ref type: {ref_type!r}")
        argument = params.get("argument") or {}
        context = params.get("context") or {}
        context_arguments = context.get("arguments") if isinstance(context, dict) else None
        res = await session.complete(typed_ref, argument, context_arguments)
        return res.model_dump(**_DUMP_KW)

    async def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Notifications toward the server are not used on this path.

        The gateway does not send client→server notifications to downstreams
        beyond the initialize handshake (which the SDK session performs
        internally), so this is a no-op logged for visibility.
        """
        logger.debug(
            "Ignoring outbound notification on HTTP transport",
            extra={"server": self.name, "method": method},
        )

    async def send_response(self, request_id: int | str, result: dict[str, Any]) -> None:
        """Not supported: 2026-07-28 Streamable HTTP uses MRTR, not server requests."""
        raise HttpTransportError(
            "Streamable HTTP (2026-07-28) does not carry server→client requests; "
            "sampling/elicitation/roots relaying over HTTP is a roadmap item (MRTR)."
        )

    async def send_error_response(self, request_id: int | str, code: int, message: str) -> None:
        """Not supported: see :meth:`send_response`."""
        raise HttpTransportError(
            "Streamable HTTP (2026-07-28) does not carry server→client requests; "
            "sampling/elicitation/roots relaying over HTTP is a roadmap item (MRTR)."
        )

    # -- Convenience MCP methods ----------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.send_request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.send_request("tools/call", {"name": name, "arguments": arguments or {}})

    # -- Helpers --------------------------------------------------------------

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise HttpTransportError(f"Transport '{self.name}' is not started")
        return self._session
