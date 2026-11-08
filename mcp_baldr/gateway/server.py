"""Gateway server: aggregates multiple stdio MCP servers behind one interface.

The gateway is itself an MCP server (AI clients connect to it over stdio).
It spawns downstream MCP server subprocesses, discovers their capabilities,
registers tools/prompts under namespaced names (``server__name``) and
resources under reversible namespaced URIs, and routes client requests to the
correct subprocess.

Resiliency features (Phase 3):

- **Process monitoring**: Each downstream subprocess is watched by a
  monitor task.  When a subprocess crashes unexpectedly, in-flight
  requests are rejected and a crash callback is invoked.
- **Auto-restart**: If ``auto_restart: true`` is set on a server config,
  the gateway restarts the subprocess (up to ``max_restarts`` times) and
  re-discovers its capabilities.
- **Clean shutdown**: SIGINT / SIGTERM triggers a graceful shutdown that
  stops all downstream servers and exits cleanly.

Capability negotiation (Stage 1):

- Each downstream's ``initialize`` result (capabilities + protocol version)
  is recorded in a :class:`CapabilityRegistry`.
- The gateway advertises upstream the honest aggregate of what healthy
  downstreams support intersected with what the gateway can actually proxy.

Unified routing (Stage 2):

- Incoming MCP requests are dispatched through a single
  :class:`~mcp_baldr.routing.router.RequestRouter`.

Resource aggregation (Stage 3):

- Resources and resource templates are aggregated from servers advertising
  the ``resources`` capability, namespaced with a reversible, collision-safe
  URI scheme (see :mod:`mcp_baldr.protocol.uri`).

Prompt aggregation (Stage 4):

- Prompts are aggregated from servers advertising the ``prompts`` capability,
  namespaced as ``server__prompt`` (like tools).

Notifications & subscriptions (Stage 5):

- Server-originated notifications are surfaced by the transport and handled
  by the gateway:
  - ``notifications/*/list_changed`` → re-discover that capability from the
    owning server, then forward the list-changed notification upstream;
  - ``notifications/resources/updated`` → translate the original URI to its
    namespaced form and forward upstream (only if subscribed).
- ``resources/subscribe`` / ``resources/unsubscribe`` route to the owning
  server, deduplicated via a :class:`SubscriptionManager`.  Subscriptions are
  cleaned up on downstream crash and gateway shutdown.

Completion & logging (Stage 6):

- ``completion/complete`` resolves the namespaced prompt name (``ref/prompt``)
  or namespaced resource URI (``ref/resource``) back to the owning server and
  forwards the request with the de-namespaced reference.
- ``logging/setLevel`` is forwarded to every healthy downstream advertising
  the ``logging`` capability.
- Downstream ``notifications/message`` are redacted (secrets masked) and
  forwarded upstream, tagged with the originating server.

Server→client requests, ping (Stage 7):

- ``ping`` is answered directly by the gateway (a liveness check).
- Downstream-originated requests ``sampling/createMessage``,
  ``elicitation/create``, and ``roots/list`` are relayed to the upstream
  client **only if the client advertised the matching capability** in its
  ``initialize`` call.  The gateway never auto-approves or fabricates a
  result — it is a pure conduit: it forwards the request, waits for the
  client's answer, and sends that answer (or error) back to the downstream.
  If the client did not advertise the capability, the gateway rejects the
  downstream request with a method-not-found error rather than silently
  succeeding.
- ``notifications/progress`` and ``notifications/cancelled`` emitted by a
  downstream are forwarded upstream verbatim (their tokens are client-chosen
  and opaque to the gateway).

Pagination (Stage 8):

- ``tools/list`` / ``resources/list`` / ``prompts/list`` paginate the
  *aggregate* of all downstream items with an opaque, stable cursor (see
  :mod:`mcp_baldr.protocol.pagination`).  The page size comes from config.
  A malformed or stale cursor is rejected with an invalid-params error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

from mcp import types

from mcp_baldr.config import load_config
from mcp_baldr.educational.inspector import ProtocolInspector
from mcp_baldr.gateway.subscription_manager import SubscriptionManager
from mcp_baldr.learning.event_collector import EventCollector
from mcp_baldr.learning.events import CapabilityCategory, ProcessingStage
from mcp_baldr.learning.redaction import redact
from mcp_baldr.logging_utils import setup_logging
from mcp_baldr.protocol.pagination import InvalidCursorError, paginate
from mcp_baldr.protocol.uri import (
    NamespacedURIError,
    decode_namespaced_uri,
    is_namespaced_uri,
)
from mcp_baldr.registry.capability_registry import CapabilityRegistry
from mcp_baldr.registry.prompt_registry import PromptRegistry
from mcp_baldr.registry.resource_registry import ResourceRegistry
from mcp_baldr.registry.tool_registry import ToolRegistry
from mcp_baldr.routing.router import MethodNotFoundError as RouterMethodNotFound
from mcp_baldr.routing.router import RequestRouter
from mcp_baldr.transports.stdio import StdioTransport, StdioTransportError

logger = logging.getLogger(__name__)

# -- Protocol constants --------------------------------------------------------

PROTOCOL_VERSION = types.LATEST_PROTOCOL_VERSION
GATEWAY_INFO: dict[str, str] = {"name": "mcp-baldr", "version": "0.1.0"}

#: Server→client request methods the gateway can relay, mapped to the upstream
#: client capability that must be present for the relay to proceed.  The
#: gateway is a conduit only — it never answers these itself.
CLIENT_RELAYABLE_REQUESTS: dict[str, str] = {
    "sampling/createMessage": "sampling",
    "elicitation/create": "elicitation",
    "roots/list": "roots",
}

#: Downstream notifications that are forwarded upstream verbatim.  Their tokens
#: (progress tokens, request ids) are client-chosen and opaque to the gateway.
PASSTHROUGH_NOTIFICATIONS: frozenset[str] = frozenset(
    {"notifications/progress", "notifications/cancelled"}
)


# -- Exceptions ----------------------------------------------------------------


class GatewayError(Exception):
    """Base error for gateway-level routing failures."""


class ToolNotFoundError(GatewayError):
    """The requested tool is not in the registry."""


class ResourceNotFoundError(GatewayError):
    """The requested resource URI is not in the registry."""


class PromptNotFoundError(GatewayError):
    """The requested prompt is not in the registry."""


class ServerNotAvailableError(GatewayError):
    """The owning server for a tool, resource, or prompt is not running."""


class MethodNotFoundError(GatewayError):
    """The MCP method is not supported by the gateway."""


class InvalidParamsError(GatewayError):
    """A request parameter was missing or malformed."""


# -- JSON-RPC error codes ------------------------------------------------------

ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603


# -- Gateway -------------------------------------------------------------------


class Gateway:
    """Multi-server MCP gateway.

    Manages downstream :class:`StdioTransport` instances, aggregates their
    tools, resources, and prompts, tracks each downstream's negotiated
    capabilities, dispatches requests through a unified :class:`RequestRouter`,
    propagates server-originated notifications upstream, relays server→client
    requests to the upstream client, and exposes a unified MCP server interface
    to AI clients via stdio.

    Crash recovery and clean shutdown are handled automatically.
    """

    def __init__(self, config_path: str | Path, *, force_learning: bool = False):
        self.config = load_config(config_path)
        if force_learning:
            self.config.learning.learning_mode = True
        self.registry = ToolRegistry()
        self.resources = ResourceRegistry()
        self.prompts = PromptRegistry()
        self.capabilities = CapabilityRegistry()
        self.subscriptions = SubscriptionManager()
        self.transports: dict[str, StdioTransport] = {}
        self._restart_counts: dict[str, int] = {}
        # Last log level requested by the upstream client (forwarded to new
        # downstreams on restart so they match the client's chosen verbosity).
        self._log_level: str | None = None
        # Capabilities the upstream client advertised in its initialize call.
        # Used to gate server→client relaying (sampling/elicitation/roots):
        # the gateway only relays a request if the client actually supports it.
        self._client_capabilities: dict[str, Any] = {}
        # Gateway-initiated requests to the upstream client, awaiting the
        # client's response.  Keyed by the gateway-generated request id.
        self._pending_client_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._client_req_seq: int = 0
        # Page size for aggregated list results (tools/resources/prompts).
        self._page_size: int = self.config.pagination.page_size
        # Unified request router — capabilities register their handlers here.
        self.router = RequestRouter()
        self._register_handlers()
        # Educational layer (Learning Lab).  The inspector renders to stderr;
        # the collector stores sanitized, queryable events.  They are
        # independent siblings, both created only when learning mode is on and
        # both ``None`` otherwise, so the protocol path does zero educational
        # work when disabled.
        learning_on = self.config.learning.learning_mode
        self.inspector: ProtocolInspector | None = (
            ProtocolInspector(self.config.learning) if learning_on else None
        )
        self.collector: EventCollector | None = (
            EventCollector(session_id=uuid.uuid4().hex) if learning_on else None
        )

    def _register_handlers(self) -> None:
        """Register this gateway's MCP method handlers with the router.

        Later stages add more methods here; the dispatch path in
        ``_handle_request`` does not change as methods are added.
        """
        self.router.register_many(
            {
                "initialize": self._handle_initialize_async,
                "tools/list": self._handle_tools_list,
                "tools/call": self._handle_tools_call,
                "resources/list": self._handle_resources_list,
                "resources/read": self._handle_resources_read,
                "resources/templates/list": self._handle_resources_templates_list,
                "resources/subscribe": self._handle_resources_subscribe,
                "resources/unsubscribe": self._handle_resources_unsubscribe,
                "prompts/list": self._handle_prompts_list,
                "prompts/get": self._handle_prompts_get,
                "completion/complete": self._handle_completion_complete,
                "logging/setLevel": self._handle_logging_set_level,
                "ping": self._handle_ping,
            }
        )

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Start all enabled downstream servers and discover their capabilities."""
        setup_logging(
            level=self.config.logging.level,
            fmt=self.config.logging.format,
        )

        for server_config in self.config.enabled_servers():
            transport = StdioTransport(server_config)
            transport.set_crash_callback(self._on_transport_crash)
            transport.set_notification_callback(self._on_downstream_notification)
            transport.set_request_callback(self._on_downstream_request)
            self.transports[server_config.name] = transport

        for name, transport in self.transports.items():
            await transport.start()
            await transport.initialize()
            # Record the downstream's negotiated capabilities so the gateway
            # can advertise an honest aggregate upstream.
            self.capabilities.register(
                name,
                protocol_version=transport.server_protocol_version,
                capabilities=transport.server_capabilities,
                server_info=transport.server_info,
            )
            await self._discover_server(name, transport)

        logger.info(
            "Gateway ready",
            extra={
                "servers": list(self.transports),
                "total_tools": len(self.registry),
                "total_resources": len(self.resources),
                "total_prompts": len(self.prompts),
                "capabilities": sorted(self.capabilities.aggregate_capabilities().keys()),
            },
        )

        if self.inspector:
            self.inspector.gateway_ready(list(self.transports), len(self.registry))
        if self.collector is not None:
            self.collector.record(
                ProcessingStage.GATEWAY_READY,
                metadata={
                    "servers": list(self.transports),
                    "tool_count": len(self.registry),
                    "resource_count": len(self.resources),
                    "prompt_count": len(self.prompts),
                },
            )

    async def _discover_server(self, name: str, transport: StdioTransport) -> None:
        """Discover and register a downstream server's capabilities.

        Only capabilities the server actually advertises are queried, so a
        tools-only server is never asked for resources or prompts.
        """
        caps = transport.server_capabilities

        if "tools" in caps:
            tools = await transport.list_tools()
            self.registry.register_server_tools(name, tools)
            logger.info(
                "Discovered tools from server",
                extra={"server": name, "tool_count": len(tools)},
            )

        if "resources" in caps:
            res_result = await transport.send_request("resources/list", {})
            resources = res_result.get("resources", [])
            self.resources.register_server_resources(name, resources)

            templates: list[dict[str, Any]] = []
            # templates/list is optional even when resources is advertised;
            # tolerate servers that don't implement it.
            try:
                tmpl_result = await transport.send_request("resources/templates/list", {})
                templates = tmpl_result.get("resourceTemplates", [])
                self.resources.register_server_templates(name, templates)
            except StdioTransportError:
                logger.debug(
                    "Server does not implement resources/templates/list",
                    extra={"server": name},
                )
            logger.info(
                "Discovered resources from server",
                extra={
                    "server": name,
                    "resource_count": len(resources),
                    "template_count": len(templates),
                },
            )

        if "prompts" in caps:
            prompt_result = await transport.send_request("prompts/list", {})
            prompts = prompt_result.get("prompts", [])
            self.prompts.register_server_prompts(name, prompts)
            logger.info(
                "Discovered prompts from server",
                extra={"server": name, "prompt_count": len(prompts)},
            )

        # If the client already chose a log level, replay it to the freshly
        # (re)discovered server so its verbosity matches the client's choice.
        if self._log_level is not None and "logging" in caps:
            try:
                await transport.send_request("logging/setLevel", {"level": self._log_level})
            except StdioTransportError:
                logger.debug(
                    "Server rejected replayed logging/setLevel",
                    extra={"server": name, "level": self._log_level},
                )

    async def stop(self) -> None:
        """Stop all downstream servers and clear the registries."""
        # Reject any in-flight client relays so their awaiters don't hang.
        self._reject_pending_client_requests("Gateway shutting down")
        for transport in reversed(list(self.transports.values())):
            await transport.stop()
        self.transports.clear()
        self.registry.clear()
        self.resources.clear()
        self.prompts.clear()
        self.subscriptions.clear()
        for name in list(self.capabilities.servers()):
            self.capabilities.remove(name)
        self._restart_counts.clear()

    async def __aenter__(self) -> Gateway:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # -- Crash handling -------------------------------------------------------

    async def _on_transport_crash(self, server_name: str, returncode: int) -> None:
        """Crash callback: restart the server or remove its items.

        This runs in a separate task spawned by the transport's monitor,
        so it can safely call ``restart()`` (which calls ``stop()`` →
        cancels the monitor task) without deadlock.
        """
        transport = self.transports.get(server_name)
        if transport is None:
            return

        server_config = next((s for s in self.config.servers if s.name == server_name), None)

        if (
            server_config
            and server_config.auto_restart
            and self._restart_counts.get(server_name, 0) < server_config.max_restarts
        ):
            self._restart_counts[server_name] = self._restart_counts.get(server_name, 0) + 1
            attempt = self._restart_counts[server_name]

            logger.warning(
                "Attempting server restart",
                extra={
                    "server": server_name,
                    "returncode": returncode,
                    "restart_attempt": attempt,
                    "max_restarts": server_config.max_restarts,
                },
            )

            try:
                await transport.restart()
                # Refresh negotiated capabilities + items after the restart.
                self.capabilities.register(
                    server_name,
                    protocol_version=transport.server_protocol_version,
                    capabilities=transport.server_capabilities,
                    server_info=transport.server_info,
                )
                # Subscriptions do not survive a crash; drop stale ones so the
                # client can re-subscribe against the fresh process.
                self.subscriptions.drop_server(server_name)
                await self._discover_server(server_name, transport)
                logger.info(
                    "Server restarted successfully",
                    extra={"server": server_name, "restart_attempt": attempt},
                )
            except Exception as exc:
                logger.error(
                    "Server restart failed, removing items",
                    extra={
                        "server": server_name,
                        "error": str(exc),
                        "restart_attempt": attempt,
                    },
                )
                self._unregister_everywhere(server_name)
                self.capabilities.mark_unhealthy(server_name)
        else:
            # Auto-restart is disabled or max restarts exceeded.
            logger.warning(
                "Server down, removing items from registries",
                extra={
                    "server": server_name,
                    "returncode": returncode,
                    "restarts": self._restart_counts.get(server_name, 0),
                    "auto_restart": server_config.auto_restart if server_config else False,
                },
            )
            self._unregister_everywhere(server_name)
            self.capabilities.mark_unhealthy(server_name)

    def _unregister_everywhere(self, server_name: str) -> None:
        """Remove a server's items and subscriptions from all registries."""
        self.registry.unregister_server(server_name)
        self.resources.unregister_server(server_name)
        self.prompts.unregister_server(server_name)
        self.subscriptions.drop_server(server_name)

    # -- Server-originated notifications --------------------------------------

    async def _on_downstream_notification(
        self, server_name: str, method: str, params: dict[str, Any]
    ) -> None:
        """Handle a notification emitted by a downstream server.

        Supported notifications:

        - ``notifications/tools/list_changed`` → re-discover tools, forward up.
        - ``notifications/resources/list_changed`` → re-discover resources, up.
        - ``notifications/prompts/list_changed`` → re-discover prompts, up.
        - ``notifications/resources/updated`` → translate URI, forward up (only
          if the client is subscribed to that resource).
        - ``notifications/message`` → redact, tag with server, forward up.
        - ``notifications/progress`` / ``notifications/cancelled`` → forward up
          verbatim (opaque, client-chosen tokens).

        Unknown notifications are logged and dropped (never forwarded blindly).
        """
        transport = self.transports.get(server_name)
        if transport is None or not transport.is_running:
            return

        try:
            if method == "notifications/tools/list_changed":
                tools = await transport.list_tools()
                self.registry.register_server_tools(server_name, tools)
                self._send_notification("notifications/tools/list_changed", {})

            elif method == "notifications/resources/list_changed":
                res = await transport.send_request("resources/list", {})
                self.resources.register_server_resources(server_name, res.get("resources", []))
                self._send_notification("notifications/resources/list_changed", {})

            elif method == "notifications/prompts/list_changed":
                pr = await transport.send_request("prompts/list", {})
                self.prompts.register_server_prompts(server_name, pr.get("prompts", []))
                self._send_notification("notifications/prompts/list_changed", {})

            elif method == "notifications/resources/updated":
                original_uri = params.get("uri")
                if not isinstance(original_uri, str):
                    return
                # Only forward if the client actually subscribed, and translate
                # the original URI back to the namespaced URI the client knows.
                namespaced = self.subscriptions.namespaced_for(server_name, original_uri)
                if namespaced is None:
                    logger.debug(
                        "Dropping resources/updated for unsubscribed URI",
                        extra={"server": server_name, "uri": original_uri},
                    )
                    return
                self._send_notification("notifications/resources/updated", {"uri": namespaced})

            elif method == "notifications/message":
                self._forward_log_message(server_name, params)

            elif method in PASSTHROUGH_NOTIFICATIONS:
                # Progress / cancellation: tokens are client-chosen and opaque
                # to the gateway, so forward verbatim.
                self._send_notification(method, params)

            else:
                logger.debug(
                    "Ignoring unsupported downstream notification",
                    extra={"server": server_name, "method": method},
                )
        except StdioTransportError as exc:
            logger.warning(
                "Failed to handle downstream notification",
                extra={"server": server_name, "method": method, "error": str(exc)},
            )

    def _forward_log_message(self, server_name: str, params: dict[str, Any]) -> None:
        """Redact and forward a downstream ``notifications/message`` upstream.

        The ``data`` payload is passed through :func:`redact` so secrets in
        log records never reach the client.  The ``logger`` name is namespaced
        with the originating server so the client can tell logs apart.  The
        ``level`` is preserved verbatim.
        """
        forwarded: dict[str, Any] = {}
        if "level" in params:
            forwarded["level"] = params["level"]
        # Namespace the logger name with the server so multiplexed logs are
        # attributable to their source downstream.
        downstream_logger = params.get("logger")
        forwarded["logger"] = (
            f"{server_name}/{downstream_logger}" if downstream_logger else server_name
        )
        data = params.get("data")
        if isinstance(data, dict):
            forwarded["data"] = redact(data)
        elif data is not None:
            # Non-dict data: wrap so redaction's string handling still applies.
            forwarded["data"] = redact({"value": data})["value"]
        self._send_notification("notifications/message", forwarded)

    # -- Server→client requests (Stage 7) -------------------------------------

    async def _on_downstream_request(
        self, server_name: str, request_id: int | str, method: str, params: dict[str, Any]
    ) -> None:
        """Relay a downstream server→client request to the upstream client.

        The gateway is a pure conduit.  It relays ``sampling/createMessage``,
        ``elicitation/create``, and ``roots/list`` to the upstream client only
        if the client advertised the matching capability; it never fabricates
        or auto-approves a result.  The client's response (or error) is sent
        back to the originating downstream.
        """
        transport = self.transports.get(server_name)
        if transport is None or not transport.is_running:
            return

        capability = CLIENT_RELAYABLE_REQUESTS.get(method)
        if capability is None:
            # The gateway does not relay this server→client request.
            await transport.send_error_response(
                request_id, ERR_METHOD_NOT_FOUND, f"Gateway does not relay request: {method}"
            )
            return

        if capability not in self._client_capabilities:
            # Honest refusal: the upstream client never advertised this
            # capability, so there is nobody to answer.  Do NOT auto-approve.
            logger.info(
                "Refusing server→client request: client lacks capability",
                extra={"server": server_name, "method": method, "capability": capability},
            )
            await transport.send_error_response(
                request_id,
                ERR_METHOD_NOT_FOUND,
                f"Upstream client does not support '{capability}'",
            )
            return

        if self.collector is not None:
            self.collector.record(
                ProcessingStage.DOWNSTREAM_REQUEST,
                method=method,
                downstream_server=server_name,
                metadata={"relayed_to_client": True, "capability": capability},
            )

        logger.info(
            "Relaying server→client request",
            extra={"server": server_name, "method": method, "capability": capability},
        )

        try:
            result = await self._send_client_request(method, params)
        except StdioTransportError as exc:
            # Relay failed (client disconnected / timed out).  Report back.
            await transport.send_error_response(request_id, ERR_INTERNAL, str(exc))
            return

        await transport.send_response(request_id, result)

    async def _send_client_request(
        self, method: str, params: dict[str, Any], timeout: float = 120.0
    ) -> dict[str, Any]:
        """Send a request to the upstream client and await its response.

        The gateway picks a distinct ``gw-<n>`` id so the client's response
        can be correlated in :meth:`_serve` without colliding with the
        client's own request id space.
        """
        self._client_req_seq += 1
        req_id = f"gw-{self._client_req_seq}"

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_client_requests[req_id] = future

        self._send_request(req_id, method, params)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending_client_requests.pop(req_id, None)
            raise StdioTransportError(
                f"Upstream client did not answer '{method}' within {timeout}s"
            ) from None
        finally:
            self._pending_client_requests.pop(req_id, None)

    def _resolve_client_response(self, message: dict[str, Any]) -> bool:
        """If *message* is a response to a gateway→client request, resolve it.

        Returns ``True`` if the message was consumed as such a response (and
        must not be dispatched as a client request), ``False`` otherwise.
        """
        msg_id = message.get("id")
        if msg_id is None or message.get("method") is not None:
            return False  # not a response (notifications/requests have method)
        future = self._pending_client_requests.get(msg_id)
        if future is None:
            return False  # not one of ours
        if not future.done():
            if "error" in message:
                err = message["error"]
                future.set_exception(
                    StdioTransportError(
                        f"Client returned error (code={err.get('code')}): {err.get('message')}"
                    )
                )
            else:
                future.set_result(message.get("result", {}))
        return True

    def _reject_pending_client_requests(self, reason: str) -> None:
        """Fail all in-flight gateway→client request futures."""
        for fut in self._pending_client_requests.values():
            if not fut.done():
                fut.set_exception(StdioTransportError(reason))
        self._pending_client_requests.clear()

    # -- Client-facing operations: tools --------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return all tools with namespaced names for MCP ``tools/list``."""
        return self.registry.list_for_client()

    async def call_tool(
        self, namespaced_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Route a tool call to the correct downstream server.

        Raises:
            ToolNotFoundError: If the tool is not registered.
            ServerNotAvailableError: If the owning server is not running.
            StdioTransportError: If the downstream server returns an error
                or the request times out.
        """
        resolved = self.registry.resolve(namespaced_name)
        if resolved is None:
            raise ToolNotFoundError(f"Tool not found: {namespaced_name}")

        server_name, original_name = resolved
        transport = self._require_running(server_name)

        if self.inspector:
            self.inspector.routing(namespaced_name, server_name, original_name)
        if self.collector is not None:
            self.collector.record(
                ProcessingStage.NAMESPACE_RESOLUTION,
                downstream_server=server_name,
                category=CapabilityCategory.TOOLS,
                metadata={
                    "requested": namespaced_name,
                    "resolved_server": server_name,
                    "resolved_tool": original_name,
                },
            )

        logger.info(
            "Routing tool call",
            extra={
                "namespaced_name": namespaced_name,
                "server": server_name,
                "tool": original_name,
            },
        )
        if self.inspector:
            self.inspector.downstream_request(
                server_name,
                "tools/call",
                {"name": original_name, "arguments": arguments or {}},
            )
        if self.collector is not None:
            self.collector.record(
                ProcessingStage.DOWNSTREAM_REQUEST,
                method="tools/call",
                downstream_server=server_name,
                metadata={"name": original_name, "arguments": arguments or {}},
            )

        result = await transport.call_tool(original_name, arguments)

        if self.inspector:
            self.inspector.downstream_response(server_name, "tools/call", result)
        if self.collector is not None:
            self.collector.record(
                ProcessingStage.DOWNSTREAM_RESPONSE,
                method="tools/call",
                downstream_server=server_name,
                success=not result.get("isError", False),
                metadata={"result": result},
            )

        return result

    # -- Client-facing operations: resources ----------------------------------

    async def list_resources(self) -> list[dict[str, Any]]:
        """Return all resources with namespaced URIs for ``resources/list``."""
        return self.resources.list_resources_for_client()

    async def list_resource_templates(self) -> list[dict[str, Any]]:
        """Return all resource templates for ``resources/templates/list``."""
        return self.resources.list_templates_for_client()

    def _resolve_resource(self, namespaced_uri: str) -> tuple[str, str]:
        """Decode and validate a namespaced resource URI.

        Returns ``(server_name, original_uri)``.

        Raises:
            InvalidParamsError: If the URI is not a valid namespaced URI.
            ResourceNotFoundError: If the resource is not registered.
        """
        try:
            server_name, original_uri = decode_namespaced_uri(namespaced_uri)
        except NamespacedURIError as exc:
            raise InvalidParamsError(f"Invalid resource URI: {exc}") from exc
        if self.resources.resolve(namespaced_uri) is None:
            raise ResourceNotFoundError(f"Resource not found: {namespaced_uri}")
        return server_name, original_uri

    async def read_resource(self, namespaced_uri: str) -> dict[str, Any]:
        """Route a ``resources/read`` to the owning downstream server."""
        server_name, original_uri = self._resolve_resource(namespaced_uri)
        transport = self._require_running(server_name)

        if self.collector is not None:
            self.collector.record(
                ProcessingStage.NAMESPACE_RESOLUTION,
                downstream_server=server_name,
                category=CapabilityCategory.RESOURCES,
                metadata={"requested": namespaced_uri, "resolved_uri": original_uri},
            )
        logger.info(
            "Routing resource read",
            extra={"server": server_name, "uri": original_uri},
        )

        return await transport.send_request("resources/read", {"uri": original_uri})

    async def subscribe_resource(self, namespaced_uri: str) -> dict[str, Any]:
        """Subscribe the client to a resource, deduplicating downstream calls.

        Only the first subscription for a given (server, original_uri) sends a
        downstream ``resources/subscribe``; later duplicates are tracked but
        not re-sent.
        """
        server_name, original_uri = self._resolve_resource(namespaced_uri)
        transport = self._require_running(server_name)

        is_new = self.subscriptions.add(server_name, original_uri, namespaced_uri)
        if is_new:
            await transport.send_request("resources/subscribe", {"uri": original_uri})
            logger.info(
                "Subscribed to resource",
                extra={"server": server_name, "uri": original_uri},
            )
        return {}

    async def unsubscribe_resource(self, namespaced_uri: str) -> dict[str, Any]:
        """Unsubscribe the client from a resource.

        Only sends a downstream ``resources/unsubscribe`` if a subscription
        actually existed.
        """
        server_name, original_uri = self._resolve_resource(namespaced_uri)
        transport = self._require_running(server_name)

        existed = self.subscriptions.remove(server_name, original_uri)
        if existed:
            await transport.send_request("resources/unsubscribe", {"uri": original_uri})
            logger.info(
                "Unsubscribed from resource",
                extra={"server": server_name, "uri": original_uri},
            )
        return {}

    # -- Client-facing operations: prompts ------------------------------------

    async def list_prompts(self) -> list[dict[str, Any]]:
        """Return all prompts with namespaced names for MCP ``prompts/list``."""
        return self.prompts.list_for_client()

    async def get_prompt(
        self, namespaced_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Route a ``prompts/get`` to the owning downstream server.

        The namespaced prompt name is resolved to ``(server, original_name)``
        and the original name is forwarded downstream.  Prompt message
        structures and content blocks are passed through verbatim.

        Raises:
            PromptNotFoundError: If the prompt is not registered.
            ServerNotAvailableError: If the owning server is not running.
        """
        resolved = self.prompts.resolve(namespaced_name)
        if resolved is None:
            raise PromptNotFoundError(f"Prompt not found: {namespaced_name}")

        server_name, original_name = resolved
        transport = self._require_running(server_name)

        if self.collector is not None:
            self.collector.record(
                ProcessingStage.NAMESPACE_RESOLUTION,
                downstream_server=server_name,
                category=CapabilityCategory.PROMPTS,
                metadata={
                    "requested": namespaced_name,
                    "resolved_server": server_name,
                    "resolved_prompt": original_name,
                },
            )
        logger.info(
            "Routing prompt get",
            extra={"server": server_name, "prompt": original_name},
        )

        downstream_params: dict[str, Any] = {"name": original_name}
        if arguments is not None:
            downstream_params["arguments"] = arguments
        return await transport.send_request("prompts/get", downstream_params)

    # -- Client-facing operations: completion ---------------------------------

    async def complete(self, params: dict[str, Any]) -> dict[str, Any]:
        """Route a ``completion/complete`` to the owning downstream server.

        The ``ref`` identifies what is being completed:

        - ``ref/prompt`` carries a namespaced prompt ``name`` — resolved back
          to ``(server, original_name)``;
        - ``ref/resource`` carries a namespaced resource ``uri`` — decoded back
          to ``(server, original_uri)``.

        The reference is de-namespaced before forwarding so the downstream
        sees its own prompt/resource identifier.  The ``argument`` (and any
        ``context``) is forwarded verbatim and the downstream result is
        returned unchanged.

        Raises:
            InvalidParamsError: If the ref is missing/malformed.
            PromptNotFoundError / ResourceNotFoundError: If the referenced
                item is not registered.
            ServerNotAvailableError: If the owning server is not running.
        """
        ref = params.get("ref")
        if not isinstance(ref, dict):
            raise InvalidParamsError("completion/complete requires a 'ref' object")

        ref_type = ref.get("type")
        server_name, downstream_ref = self._resolve_completion_ref(ref_type, ref)
        transport = self._require_running(server_name)

        downstream_params: dict[str, Any] = {"ref": downstream_ref}
        if "argument" in params:
            downstream_params["argument"] = params["argument"]
        if "context" in params:
            downstream_params["context"] = params["context"]

        if self.collector is not None:
            self.collector.record(
                ProcessingStage.NAMESPACE_RESOLUTION,
                downstream_server=server_name,
                metadata={"ref_type": ref_type, "resolved_ref": downstream_ref},
            )
        logger.info(
            "Routing completion",
            extra={"server": server_name, "ref_type": ref_type},
        )

        return await transport.send_request("completion/complete", downstream_params)

    def _resolve_completion_ref(
        self, ref_type: Any, ref: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Resolve a completion ``ref`` to ``(server, de-namespaced ref)``."""
        if ref_type == "ref/prompt":
            namespaced_name = ref.get("name")
            if not isinstance(namespaced_name, str) or not namespaced_name:
                raise InvalidParamsError("ref/prompt requires a 'name'")
            resolved = self.prompts.resolve(namespaced_name)
            if resolved is None:
                raise PromptNotFoundError(f"Prompt not found: {namespaced_name}")
            _server, original_name = resolved
            new_ref = dict(ref)
            new_ref["name"] = original_name
            return _server, new_ref

        if ref_type == "ref/resource":
            namespaced_uri = ref.get("uri")
            if not isinstance(namespaced_uri, str) or not namespaced_uri:
                raise InvalidParamsError("ref/resource requires a 'uri'")
            if not is_namespaced_uri(namespaced_uri):
                raise InvalidParamsError(f"Invalid resource URI: {namespaced_uri}")
            server_name, original_uri = self._resolve_resource(namespaced_uri)
            new_ref = dict(ref)
            new_ref["uri"] = original_uri
            return server_name, new_ref

        raise InvalidParamsError(f"Unsupported completion ref type: {ref_type!r}")

    # -- Client-facing operations: logging ------------------------------------

    async def set_logging_level(self, level: str) -> dict[str, Any]:
        """Forward ``logging/setLevel`` to every healthy ``logging`` server.

        The level is remembered so servers that (re)start later can be brought
        to the same verbosity.  Per-server failures are logged but do not fail
        the overall request, since logging is best-effort.
        """
        self._log_level = level
        targets = self.capabilities.servers_supporting("logging")
        for server_name in targets:
            transport = self.transports.get(server_name)
            if transport is None or not transport.is_running:
                continue
            try:
                await transport.send_request("logging/setLevel", {"level": level})
            except StdioTransportError as exc:
                logger.warning(
                    "Downstream rejected logging/setLevel",
                    extra={"server": server_name, "level": level, "error": str(exc)},
                )
        logger.info(
            "Set logging level on downstreams",
            extra={"level": level, "servers": targets},
        )
        return {}

    # -- Routing helpers ------------------------------------------------------

    def _require_running(self, server_name: str) -> StdioTransport:
        """Return the transport for *server_name* or raise if unavailable."""
        transport = self.transports.get(server_name)
        if transport is None or not transport.is_running:
            raise ServerNotAvailableError(f"Server '{server_name}' is not available")
        return transport

    # -- MCP server loop ------------------------------------------------------

    async def run(self) -> None:
        """Start the gateway and serve MCP requests on stdin/stdout.

        Reads newline-delimited JSON-RPC 2.0 from stdin, handles each
        request, and writes responses to stdout.  Exits cleanly on
        EOF (client disconnected), SIGINT, or SIGTERM.
        """
        await self.start()

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()

        def request_shutdown() -> None:
            logger.info("Shutdown signal received")
            shutdown.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_shutdown)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler; the gateway
                # will still exit on EOF (client disconnect).
                pass

        try:
            await self._serve(shutdown)
        finally:
            await self.stop()

    async def _serve(self, shutdown: asyncio.Event) -> None:
        """Main loop: read JSON-RPC from stdin, dispatch, write to stdout.

        Uses :class:`asyncio.StreamReader` for stdin so that signal-based
        shutdown can cleanly cancel a pending read without leaving a
        blocked thread.
        """
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while not shutdown.is_set():
            readline_task = asyncio.ensure_future(reader.readline())
            shutdown_task = asyncio.ensure_future(shutdown.wait())

            done, pending = await asyncio.wait(
                {readline_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            if shutdown.is_set():
                break

            line_bytes = readline_task.result()
            if not line_bytes:
                break  # EOF — client disconnected

            text = line_bytes.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                self._send_error(None, ERR_PARSE, "Parse error")
                continue

            # A response to a gateway→client request (sampling/elicitation/
            # roots relay) has an id but no method — resolve it and move on.
            if self._resolve_client_response(message):
                continue

            msg_id = message.get("id")
            method = message.get("method", "")
            params = message.get("params", {})

            # Notifications (no id) — no response expected.
            if msg_id is None:
                if self.inspector:
                    self.inspector.client_notification(method, params)
                if self.collector is not None:
                    self.collector.record(
                        ProcessingStage.CLIENT_NOTIFICATION,
                        method=method,
                        metadata={"params": params},
                    )
                logger.debug(
                    "← notification from client",
                    extra={"method": method},
                )
                continue

            if self.inspector:
                self.inspector.client_request(method, params, msg_id)
            if self.collector is not None:
                self.collector.record(
                    ProcessingStage.CLIENT_REQUEST,
                    method=method,
                    request_id=msg_id,
                    metadata={"params": params},
                )
            logger.debug(
                "← request from client",
                extra={"method": method, "id": msg_id},
            )

            try:
                result = await self._handle_request(method, params)
                if self.inspector:
                    self.inspector.client_response(method, result, msg_id)
                if self.collector is not None:
                    self.collector.record(
                        ProcessingStage.CLIENT_RESPONSE,
                        method=method,
                        request_id=msg_id,
                        success=True,
                        metadata={"result": result},
                    )
                self._send_response(msg_id, result)
            except MethodNotFoundError as exc:
                self._record_error(msg_id, ERR_METHOD_NOT_FOUND, str(exc))
                self._send_error(msg_id, ERR_METHOD_NOT_FOUND, str(exc))
            except (
                ToolNotFoundError,
                ResourceNotFoundError,
                PromptNotFoundError,
                InvalidParamsError,
                InvalidCursorError,
            ) as exc:
                self._record_error(msg_id, ERR_INVALID_PARAMS, str(exc))
                self._send_error(msg_id, ERR_INVALID_PARAMS, str(exc))
            except ServerNotAvailableError as exc:
                self._record_error(msg_id, ERR_INTERNAL, str(exc))
                self._send_error(msg_id, ERR_INTERNAL, str(exc))
            except StdioTransportError as exc:
                self._record_error(msg_id, ERR_INTERNAL, str(exc))
                self._send_error(msg_id, ERR_INTERNAL, str(exc))
            except Exception as exc:
                logger.exception(
                    "Unhandled error handling request",
                    extra={"method": method},
                )
                self._record_error(msg_id, ERR_INTERNAL, f"Internal error: {exc}")
                self._send_error(msg_id, ERR_INTERNAL, f"Internal error: {exc}")

    async def _handle_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Dispatch an MCP request through the unified router.

        The router owns method→handler mapping.  An unknown method surfaces
        as the gateway's :class:`MethodNotFoundError` (JSON-RPC ``-32601``).
        """
        try:
            return await self.router.dispatch(method, params)
        except RouterMethodNotFound as exc:
            raise MethodNotFoundError(str(exc)) from exc

    # -- Method handlers (registered with the router) -------------------------

    async def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``tools/list`` with cursor pagination over the aggregate."""
        page, next_cursor = paginate(
            await self.list_tools(), "name", params.get("cursor"), self._page_size
        )
        result: dict[str, Any] = {"tools": page}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return result

    async def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``tools/call``."""
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        return await self.call_tool(name, arguments)

    async def _handle_resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``resources/list`` with cursor pagination over the aggregate."""
        page, next_cursor = paginate(
            await self.list_resources(), "uri", params.get("cursor"), self._page_size
        )
        result: dict[str, Any] = {"resources": page}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return result

    async def _handle_resources_templates_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``resources/templates/list``."""
        return {"resourceTemplates": await self.list_resource_templates()}

    async def _handle_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``resources/read``."""
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise InvalidParamsError("resources/read requires a 'uri' string")
        return await self.read_resource(uri)

    async def _handle_resources_subscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``resources/subscribe``."""
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise InvalidParamsError("resources/subscribe requires a 'uri' string")
        return await self.subscribe_resource(uri)

    async def _handle_resources_unsubscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``resources/unsubscribe``."""
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise InvalidParamsError("resources/unsubscribe requires a 'uri' string")
        return await self.unsubscribe_resource(uri)

    async def _handle_prompts_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``prompts/list`` with cursor pagination over the aggregate."""
        page, next_cursor = paginate(
            await self.list_prompts(), "name", params.get("cursor"), self._page_size
        )
        result: dict[str, Any] = {"prompts": page}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return result

    async def _handle_prompts_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``prompts/get``."""
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidParamsError("prompts/get requires a 'name' string")
        arguments = params.get("arguments")
        return await self.get_prompt(name, arguments)

    async def _handle_completion_complete(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``completion/complete``."""
        return await self.complete(params)

    async def _handle_logging_set_level(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``logging/setLevel``."""
        level = params.get("level")
        if not isinstance(level, str) or not level:
            raise InvalidParamsError("logging/setLevel requires a 'level' string")
        return await self.set_logging_level(level)

    async def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``ping`` — a liveness check answered directly by the gateway.

        MCP ``ping`` expects an empty result.  The gateway answers on its own
        behalf; it does not fan the ping out to downstreams (their health is
        tracked separately by the crash monitor).
        """
        return {}

    async def _handle_initialize_async(self, params: dict[str, Any]) -> dict[str, Any]:
        """Async wrapper so ``initialize`` fits the router's handler type."""
        return self._handle_initialize(params)

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Respond to the MCP ``initialize`` request.

        The advertised capabilities are the honest aggregate of what healthy
        downstream servers support intersected with what the gateway can
        actually proxy (see :class:`CapabilityRegistry`).  The gateway never
        claims a capability it cannot honor.

        The upstream client's advertised capabilities are captured here so the
        gateway can gate server→client relaying (sampling/elicitation/roots):
        a downstream request is only relayed if the client supports it.
        """
        client_caps = params.get("capabilities")
        if isinstance(client_caps, dict):
            self._client_capabilities = dict(client_caps)
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": self.capabilities.aggregate_capabilities(),
            "serverInfo": GATEWAY_INFO,
        }

    # -- Educational error recording ------------------------------------------

    def _record_error(self, msg_id: int | str | None, code: int, message: str) -> None:
        """Emit an error to the inspector and collector (if learning is on).

        Keeps the educational concerns in one place so the error handlers in
        ``_serve`` stay readable.  A no-op when learning mode is disabled.
        """
        if self.inspector:
            self.inspector.error(msg_id, code, message)
        if self.collector is not None:
            self.collector.record(
                ProcessingStage.ERROR,
                request_id=msg_id,
                success=False,
                metadata={"code": code, "message": message},
            )

    # -- JSON-RPC output helpers ----------------------------------------------

    @staticmethod
    def _send_response(msg_id: int | str | None, result: dict[str, Any]) -> None:
        """Write a JSON-RPC success response to stdout."""
        response = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})
        sys.stdout.write(response + "\n")
        sys.stdout.flush()

    @staticmethod
    def _send_error(msg_id: int | str | None, code: int, message: str) -> None:
        """Write a JSON-RPC error response to stdout."""
        response = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": code, "message": message},
            }
        )
        sys.stdout.write(response + "\n")
        sys.stdout.flush()

    @staticmethod
    def _send_request(req_id: int | str, method: str, params: dict[str, Any]) -> None:
        """Write a JSON-RPC request (gateway→client) to stdout."""
        message = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        sys.stdout.write(message + "\n")
        sys.stdout.flush()

    @staticmethod
    def _send_notification(method: str, params: dict[str, Any]) -> None:
        """Write a JSON-RPC notification (no id) to the upstream client."""
        message = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        sys.stdout.write(message + "\n")
        sys.stdout.flush()


# -- CLI entry point -----------------------------------------------------------


def main() -> None:
    """Entry point for the ``mcp-baldr`` CLI command.

    Runs the gateway as an MCP stdio server.  An AI client (or another MCP
    host) connects to this process's stdin/stdout.  The gateway exits
    cleanly on EOF, SIGINT, or SIGTERM.

    Flags:
        config: Path to gateway_config.yaml (default: gateway_config.yaml)
        --learn: Force-enable educational protocol inspection on stderr
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="MCP Gateway: stdio reverse proxy for MCP servers.",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="gateway_config.yaml",
        help="Path to gateway_config.yaml (default: gateway_config.yaml)",
    )
    parser.add_argument(
        "--learn",
        action="store_true",
        help="Force-enable educational protocol inspection (stderr output).",
    )
    args = parser.parse_args()

    asyncio.run(Gateway(args.config, force_learning=args.learn).run())


if __name__ == "__main__":
    main()
