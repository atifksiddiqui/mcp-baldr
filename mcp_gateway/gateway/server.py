"""Gateway server: aggregates multiple stdio MCP servers behind one interface.

The gateway is itself an MCP server (AI clients connect to it over stdio).
It spawns downstream MCP server subprocesses, discovers their tools,
registers them under namespaced names (``server__tool``), and routes
client ``tools/call`` requests to the correct subprocess based on the
namespace prefix.

Resiliency features (Phase 3):

- **Process monitoring**: Each downstream subprocess is watched by a
  monitor task.  When a subprocess crashes unexpectedly, in-flight
  requests are rejected and a crash callback is invoked.
- **Auto-restart**: If ``auto_restart: true`` is set on a server config,
  the gateway restarts the subprocess (up to ``max_restarts`` times) and
  re-discovers its tools.
- **Clean shutdown**: SIGINT / SIGTERM triggers a graceful shutdown that
  stops all downstream servers and exits cleanly.

JSON-RPC protocol flow::

    AI Client          Gateway            Downstream Servers
    │                  │                    │
    │── initialize ──→ │                    │
    │←── result ──────│                    │
    │── notif/init ──→ │                    │
    │                  │                    │
    │── tools/list ──→ │                    │
    │                  │ (registry lookup)  │
    │←── tools[] ─────│                    │
    │                  │                    │
    │── tools/call ──→ │                    │
    │                  │── resolve ns ──→   │
    │                  │── tools/call ────→ │ (correct subprocess)
    │                  │←── result ───────│
    │←── result ──────│                    │
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from mcp import types

from mcp_gateway.config import load_config
from mcp_gateway.logging_utils import setup_logging
from mcp_gateway.registry.tool_registry import ToolRegistry
from mcp_gateway.transports.stdio import StdioTransport, StdioTransportError

logger = logging.getLogger(__name__)

# -- Protocol constants --------------------------------------------------------

PROTOCOL_VERSION = types.LATEST_PROTOCOL_VERSION
GATEWAY_INFO: dict[str, str] = {"name": "mcp-gateway", "version": "0.1.0"}


# -- Exceptions ----------------------------------------------------------------


class GatewayError(Exception):
    """Base error for gateway-level routing failures."""


class ToolNotFoundError(GatewayError):
    """The requested tool is not in the registry."""


class ServerNotAvailableError(GatewayError):
    """The owning server for a tool is not running."""


class MethodNotFoundError(GatewayError):
    """The MCP method is not supported by the gateway."""


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
    tools under namespaced names in a :class:`ToolRegistry`, and exposes a
    unified MCP server interface to AI clients via stdio.

    Crash recovery and clean shutdown are handled automatically.
    """

    def __init__(self, config_path: str | Path):
        self.config = load_config(config_path)
        self.registry = ToolRegistry()
        self.transports: dict[str, StdioTransport] = {}
        self._restart_counts: dict[str, int] = {}

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Start all enabled downstream servers and discover their tools."""
        setup_logging(
            level=self.config.logging.level,
            fmt=self.config.logging.format,
        )

        for server_config in self.config.enabled_servers():
            transport = StdioTransport(server_config)
            transport.set_crash_callback(self._on_transport_crash)
            self.transports[server_config.name] = transport

        for name, transport in self.transports.items():
            await transport.start()
            await transport.initialize()
            tools = await transport.list_tools()
            self.registry.register_server_tools(name, tools)
            logger.info(
                "Discovered tools from server",
                extra={
                    "server": name,
                    "tool_count": len(tools),
                    "tools": [t["name"] for t in tools],
                },
            )

        logger.info(
            "Gateway ready",
            extra={
                "servers": list(self.transports),
                "total_tools": len(self.registry),
            },
        )

    async def stop(self) -> None:
        """Stop all downstream servers and clear the registry."""
        for transport in reversed(list(self.transports.values())):
            await transport.stop()
        self.transports.clear()
        self.registry.clear()
        self._restart_counts.clear()

    async def __aenter__(self) -> Gateway:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # -- Crash handling -------------------------------------------------------

    async def _on_transport_crash(self, server_name: str, returncode: int) -> None:
        """Crash callback: restart the server or remove its tools.

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
                tools = await transport.list_tools()
                self.registry.register_server_tools(server_name, tools)
                logger.info(
                    "Server restarted successfully",
                    extra={
                        "server": server_name,
                        "tool_count": len(tools),
                        "restart_attempt": attempt,
                    },
                )
            except Exception as exc:
                logger.error(
                    "Server restart failed, removing tools",
                    extra={
                        "server": server_name,
                        "error": str(exc),
                        "restart_attempt": attempt,
                    },
                )
                self.registry.unregister_server(server_name)
        else:
            # Auto-restart is disabled or max restarts exceeded.
            logger.warning(
                "Server down, removing tools from registry",
                extra={
                    "server": server_name,
                    "returncode": returncode,
                    "restarts": self._restart_counts.get(server_name, 0),
                    "auto_restart": server_config.auto_restart if server_config else False,
                },
            )
            self.registry.unregister_server(server_name)

    # -- Client-facing operations ---------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return all tools with namespaced names for MCP ``tools/list``."""
        return self.registry.list_for_client()

    async def call_tool(
        self, namespaced_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Route a tool call to the correct downstream server.

        Args:
            namespaced_name: The namespaced tool name (``server__tool``).
            arguments: The tool input arguments.

        Returns:
            The downstream ``tools/call`` result (contains ``content``,
            ``isError``).

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
        transport = self.transports.get(server_name)
        if transport is None or not transport.is_running:
            raise ServerNotAvailableError(f"Server '{server_name}' is not available")

        logger.info(
            "Routing tool call",
            extra={
                "namespaced_name": namespaced_name,
                "server": server_name,
                "tool": original_name,
            },
        )
        return await transport.call_tool(original_name, arguments)

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

            msg_id = message.get("id")
            method = message.get("method", "")
            params = message.get("params", {})

            # Notifications (no id) — no response expected.
            if msg_id is None:
                logger.debug(
                    "← notification from client",
                    extra={"method": method},
                )
                continue

            logger.debug(
                "← request from client",
                extra={"method": method, "id": msg_id},
            )

            try:
                result = await self._handle_request(method, params)
                self._send_response(msg_id, result)
            except MethodNotFoundError as exc:
                self._send_error(msg_id, ERR_METHOD_NOT_FOUND, str(exc))
            except ToolNotFoundError as exc:
                self._send_error(msg_id, ERR_INVALID_PARAMS, str(exc))
            except ServerNotAvailableError as exc:
                self._send_error(msg_id, ERR_INTERNAL, str(exc))
            except StdioTransportError as exc:
                self._send_error(msg_id, ERR_INTERNAL, str(exc))
            except Exception as exc:
                logger.exception(
                    "Unhandled error handling request",
                    extra={"method": method},
                )
                self._send_error(msg_id, ERR_INTERNAL, f"Internal error: {exc}")

    async def _handle_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Dispatch an MCP request to the appropriate handler."""
        if method == "initialize":
            return self._handle_initialize(params)
        if method == "tools/list":
            return {"tools": await self.list_tools()}
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            return await self.call_tool(name, arguments)
        raise MethodNotFoundError(f"Method not found: {method}")

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Respond to the MCP ``initialize`` request."""
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": GATEWAY_INFO,
        }

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


# -- CLI entry point -----------------------------------------------------------


def main() -> None:
    """Entry point for the ``mcp-gateway`` CLI command.

    Runs the gateway as an MCP stdio server.  An AI client (or another MCP
    host) connects to this process's stdin/stdout.  The gateway exits
    cleanly on EOF, SIGINT, or SIGTERM.
    """
    config_path = sys.argv[1] if len(sys.argv) > 1 else "gateway_config.yaml"
    asyncio.run(Gateway(config_path).run())


if __name__ == "__main__":
    main()
