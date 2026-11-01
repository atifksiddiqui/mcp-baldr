"""StdioTransport: subprocess bridge for downstream MCP servers.

Spawns a child process, communicates via newline-delimited JSON-RPC 2.0
over stdin/stdout, and correlates requests with responses by ``id``.

Includes a background process monitor that detects unexpected subprocess
crashes, rejects in-flight requests, and optionally fires a crash callback
so the gateway can auto-restart the server.

Typical usage::

    transport = StdioTransport(server_config)
    await transport.start()
    await transport.initialize()
    tools = await transport.list_tools()
    result = await transport.call_tool("echo", {"text": "hello"})
    await transport.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from mcp import types

from mcp_gateway.config.models import StdioServerConfig

logger = logging.getLogger(__name__)

# Protocol constants -----------------------------------------------------------
PROTOCOL_VERSION = types.LATEST_PROTOCOL_VERSION
CLIENT_INFO: dict[str, str] = {"name": "mcp-gateway", "version": "0.1.0"}
DEFAULT_TIMEOUT: float = 30.0

#: Type for the crash callback: ``(server_name, returncode) -> Awaitable[None]``.
CrashCallback = Callable[[str, int], Awaitable[None]]


class StdioTransportError(Exception):
    """Raised when the stdio transport encounters an unrecoverable error."""


class StdioTransport:
    """Manages a single downstream stdio MCP server subprocess.

    Handles process lifecycle (spawn, monitor, terminate), JSON-RPC 2.0
    request/response correlation by ``id``, and the MCP initialize handshake.

    A background monitor task detects unexpected subprocess exits (crashes),
    rejects in-flight requests, and invokes the registered crash callback if
    one is set.
    """

    def __init__(self, config: StdioServerConfig, timeout: float | None = None):
        self.config = config
        # Prefer explicit timeout arg, fall back to config, then the default.
        self.timeout = timeout if timeout is not None else config.timeout
        self.process: asyncio.subprocess.Process | None = None
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._initialized: bool = False
        self._stderr_lines: list[str] = []
        self._start_lock = asyncio.Lock()

        # Lifecycle / crash tracking
        self._stopping: bool = False
        self._crashed: bool = False
        self._crash_reason: str | None = None
        self._on_crash: CrashCallback | None = None

    # -- Properties -----------------------------------------------------------

    @property
    def name(self) -> str:
        """Server name from config."""
        return self.config.name

    @property
    def is_running(self) -> bool:
        """True if the subprocess is alive."""
        return self.process is not None and self.process.returncode is None

    @property
    def is_initialized(self) -> bool:
        """True if the MCP initialize handshake completed."""
        return self._initialized

    @property
    def returncode(self) -> int | None:
        """Subprocess exit code, or ``None`` if still running / not started."""
        return self.process.returncode if self.process else None

    @property
    def stderr(self) -> list[str]:
        """Lines captured from the subprocess stderr (for debugging)."""
        return list(self._stderr_lines)

    @property
    def crashed(self) -> bool:
        """True if the process exited unexpectedly (not via :meth:`stop`)."""
        return self._crashed

    @property
    def crash_reason(self) -> str | None:
        """Human-readable reason for the last crash, or ``None``."""
        return self._crash_reason

    # -- Crash callback -------------------------------------------------------

    def set_crash_callback(self, callback: CrashCallback) -> None:
        """Register an async callback invoked when the subprocess crashes.

        The callback receives ``(server_name, returncode)``.  It runs in a
        separate task so it can safely call :meth:`restart` (which calls
        :meth:`stop`, which cancels the monitor task) without deadlock.
        """
        self._on_crash = callback

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the subprocess and begin reading stdout/stderr/monitor.

        Raises:
            StdioTransportError: If already started.
            OSError: If the subprocess cannot be spawned.
        """
        async with self._start_lock:
            if self.process is not None:
                raise StdioTransportError(f"Transport '{self.name}' is already started")

            # Reset crash/lifecycle state for the new process.
            self._stopping = False
            self._crashed = False
            self._crash_reason = None

            env = os.environ.copy()
            env.update(self.config.env)

            logger.info(
                "Spawning stdio server",
                extra={
                    "server": self.name,
                    "command": self.config.command,
                    "cmd_args": self.config.args,
                },
            )

            self.process = await asyncio.create_subprocess_exec(
                self.config.command,
                *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            self._reader_task = asyncio.create_task(
                self._read_stdout(), name=f"stdio-reader-{self.name}"
            )
            self._stderr_task = asyncio.create_task(
                self._read_stderr(), name=f"stdio-stderr-{self.name}"
            )
            self._monitor_task = asyncio.create_task(
                self._monitor_process(), name=f"stdio-monitor-{self.name}"
            )

            logger.info(
                "Started stdio server",
                extra={"server": self.name, "pid": self.process.pid},
            )

    async def stop(self) -> None:
        """Terminate the subprocess cleanly.

        Sets an internal ``_stopping`` flag so the monitor task knows this is
        an intentional shutdown, not a crash.  Sends SIGTERM, waits up to 5
        seconds, then SIGKILL if needed.  All pending requests are rejected.
        """
        if self.process is None:
            return

        # Signal to the monitor that this is an intentional shutdown.
        self._stopping = True
        self._reject_pending("Transport shutting down")

        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()

        if self.is_running:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except TimeoutError:
                logger.warning(
                    "Server did not terminate gracefully, sending SIGKILL",
                    extra={"server": self.name},
                )
                self.process.kill()
                await self.process.wait()
            except ProcessLookupError:
                pass  # Already exited

        # Wait for the monitor task to complete (it should finish once
        # the process has exited).
        if self._monitor_task:
            try:
                await asyncio.wait_for(self._monitor_task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                self._monitor_task.cancel()

        for task in (self._reader_task, self._stderr_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._reader_task = None
        self._stderr_task = None
        self._monitor_task = None
        self.process = None
        self._initialized = False
        logger.info("Stopped stdio server", extra={"server": self.name})

    async def restart(self) -> None:
        """Restart the subprocess: stop (if running), start, initialize.

        Resets crash state and spawns a fresh process.  The MCP handshake
        is performed automatically.
        """
        if self.process is not None:
            await self.stop()

        # Clear crash state for the new instance (stop() doesn't reset these).
        self._crashed = False
        self._crash_reason = None

        await self.start()
        await self.initialize()

        logger.info(
            "Restarted stdio server",
            extra={"server": self.name, "pid": self.process.pid if self.process else None},
        )

    # -- MCP handshake --------------------------------------------------------

    async def initialize(self) -> dict[str, Any]:
        """Perform the MCP ``initialize`` handshake.

        Sends the ``initialize`` request, then the ``notifications/initialized``
        notification to complete the handshake.

        Returns:
            The ``initialize`` result dict (contains ``protocolVersion``,
            ``capabilities``, ``serverInfo``).

        Raises:
            StdioTransportError: If not started or already initialized.
        """
        if self.process is None:
            raise StdioTransportError(f"Cannot initialize server '{self.name}': not started")
        if self._initialized:
            raise StdioTransportError(f"Server '{self.name}' is already initialized")

        result = await self.send_request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )

        await self.send_notification("notifications/initialized", {})

        self._initialized = True
        server_info = result.get("serverInfo", {})
        logger.info(
            "Initialized MCP server",
            extra={
                "server": self.name,
                "serverInfo": server_info,
                "protocolVersion": result.get("protocolVersion"),
            },
        )
        return result

    # -- JSON-RPC send --------------------------------------------------------

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC 2.0 request and await the matching response.

        Args:
            method: The JSON-RPC method name (e.g. ``"tools/list"``).
            params: Optional params dict sent as ``"params"``.
            timeout: Seconds to wait for a response (defaults to self.timeout).

        Returns:
            The ``"result"`` field from the JSON-RPC response.

        Raises:
            StdioTransportError: If not started, timed out, or the server
                returned a JSON-RPC error.
        """
        if self.process is None or self.process.stdin is None:
            raise StdioTransportError(f"Transport '{self.name}' is not started")

        request_id = self._next_id
        self._next_id += 1

        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future

        line = json.dumps(message) + "\n"
        logger.debug(
            "→ request",
            extra={"server": self.name, "method": method, "id": request_id},
        )
        self.process.stdin.write(line.encode("utf-8"))
        await self.process.stdin.drain()

        effective_timeout = timeout if timeout is not None else self.timeout
        try:
            return await asyncio.wait_for(future, timeout=effective_timeout)
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise StdioTransportError(
                f"Request '{method}' (id={request_id}) to server '{self.name}' "
                f"timed out after {effective_timeout}s"
            ) from None
        except Exception:
            self._pending.pop(request_id, None)
            raise

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send a JSON-RPC 2.0 notification (no response expected).

        Args:
            method: The JSON-RPC method name.
            params: Optional params dict.
        """
        if self.process is None or self.process.stdin is None:
            raise StdioTransportError(f"Transport '{self.name}' is not started")

        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            message["params"] = params

        line = json.dumps(message) + "\n"
        self.process.stdin.write(line.encode("utf-8"))
        await self.process.stdin.drain()
        logger.debug(
            "→ notification",
            extra={"server": self.name, "method": method},
        )

    # -- Convenience MCP methods ----------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """Call ``tools/list`` and return the list of tool schemas.

        Returns:
            A list of tool definition dicts (each with ``name``,
            ``description``, ``inputSchema``, etc.).
        """
        result = await self.send_request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call ``tools/call`` with the given tool name and arguments.

        Args:
            name: The tool name as advertised by the server.
            arguments: The tool input arguments.

        Returns:
            The ``tools/call`` result dict (contains ``content``, ``isError``).
        """
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        return await self.send_request("tools/call", params)

    # -- Background tasks -----------------------------------------------------

    async def _read_stdout(self) -> None:
        """Background task: read stdout, dispatch responses to waiting futures."""
        assert self.process is not None
        assert self.process.stdout is not None

        while True:
            line = await self.process.stdout.readline()
            if not line:
                break  # EOF — process closed stdout

            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                logger.warning(
                    "Invalid JSON from subprocess",
                    extra={"server": self.name, "line": text[:500]},
                )
                continue

            msg_id = message.get("id")

            if msg_id is not None:
                # This is a response to a pending request.
                future = self._pending.pop(msg_id, None)
                if future is None:
                    logger.warning(
                        "Response for unknown request id",
                        extra={"server": self.name, "id": msg_id},
                    )
                    continue

                if "error" in message:
                    error = message["error"]
                    err_msg = error.get("message", str(error))
                    err_code = error.get("code")
                    logger.error(
                        "Error response from subprocess",
                        extra={
                            "server": self.name,
                            "id": msg_id,
                            "errorCode": err_code,
                            "errorMessage": err_msg,
                        },
                    )
                    if not future.done():
                        future.set_exception(
                            StdioTransportError(
                                f"Server '{self.name}' returned error (code={err_code}): {err_msg}"
                            )
                        )
                else:
                    result = message.get("result", {})
                    logger.debug(
                        "← response",
                        extra={"server": self.name, "id": msg_id},
                    )
                    if not future.done():
                        future.set_result(result)
            else:
                # This is a notification from the server.
                method = message.get("method", "unknown")
                logger.debug(
                    "← notification",
                    extra={"server": self.name, "method": method},
                )

        # stdout closed — reject any still-pending requests.  The monitor
        # task will set the crashed flag if this wasn't an intentional stop.
        if not self._stopping:
            logger.warning(
                "Subprocess closed stdout unexpectedly",
                extra={"server": self.name},
            )
        self._reject_pending(f"Server '{self.name}' closed stdout")

    async def _read_stderr(self) -> None:
        """Background task: capture subprocess stderr for debugging."""
        assert self.process is not None
        assert self.process.stderr is not None

        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr_lines.append(text)
                logger.debug(
                    "stderr from subprocess",
                    extra={"server": self.name, "line": text},
                )

    async def _monitor_process(self) -> None:
        """Background task: wait for process exit and detect crashes.

        When the process exits *without* ``_stopping`` being set, it is
        considered a crash.  The monitor sets the ``crashed`` flag, rejects
        any remaining pending requests, and fires the crash callback in a
        separate task (so it can safely call ``restart`` → ``stop``).
        """
        assert self.process is not None

        returncode = await self.process.wait()

        if self._stopping:
            # Normal shutdown — ``stop()`` initiated the termination.
            return

        # --- Unexpected exit: crash ---
        self._crashed = True
        self._crash_reason = f"Process exited unexpectedly with code {returncode}"

        logger.error(
            "Subprocess crashed",
            extra={
                "server": self.name,
                "returncode": returncode,
                "stderr_tail": self._stderr_lines[-10:],
            },
        )

        # Reject any pending requests that the reader hasn't caught yet.
        self._reject_pending(f"Server '{self.name}' crashed (exit code {returncode})")

        # Fire the crash callback in a separate task so it can safely call
        # restart() → stop() → cancel(monitor) without deadlocking.
        if self._on_crash is not None:
            asyncio.create_task(
                self._run_crash_callback(self.name, returncode),
                name=f"crash-callback-{self.name}",
            )

    async def _run_crash_callback(self, name: str, returncode: int) -> None:
        """Run the crash callback, catching and logging any exceptions."""
        if self._on_crash is None:
            return
        try:
            await self._on_crash(name, returncode)
        except Exception:
            logger.exception(
                "Crash callback failed",
                extra={"server": name, "returncode": returncode},
            )

    # -- Helpers --------------------------------------------------------------

    def _reject_pending(self, reason: str) -> None:
        """Reject all pending request futures with a StdioTransportError."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(StdioTransportError(reason))
        self._pending.clear()

    async def __aenter__(self) -> StdioTransport:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()
