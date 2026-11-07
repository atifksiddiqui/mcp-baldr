"""Tests for Phase 3 resiliency: crash detection, auto-restart, signals, timeouts."""

import asyncio
import json
import signal
import sys
import textwrap
from pathlib import Path

import pytest

from mcp_baldr.config.models import StdioServerConfig
from mcp_baldr.gateway.server import Gateway
from mcp_baldr.transports.stdio import StdioTransport, StdioTransportError

MOCK_SERVER_PATH = str(Path(__file__).resolve().parent.parent / "examples" / "mock_server.py")
SLOW_SERVER_PATH = str(Path(__file__).resolve().parent.parent / "examples" / "slow_server.py")


def _write_config(
    tmp_path: Path,
    *,
    auto_restart: bool = False,
    max_restarts: int = 3,
    timeout: float = 30.0,
    server_name: str = "mock",
    command: str | None = None,
    args: list[str] | None = None,
) -> Path:
    """Write a gateway config YAML and return its path."""
    cmd = command or sys.executable
    server_args = args or [MOCK_SERVER_PATH]
    yaml = textwrap.dedent(f"""\
        servers:
          - name: {server_name}
            command: {cmd}
            args: {json.dumps(server_args)}
            auto_restart: {str(auto_restart).lower()}
            max_restarts: {max_restarts}
            timeout: {timeout}
        logging:
          level: WARNING
          format: text
    """)
    path = tmp_path / "gateway_config.yaml"
    path.write_text(yaml)
    return path


# -- Crash detection (transport level) ----------------------------------------


class TestCrashDetection:
    @pytest.mark.asyncio
    async def test_crash_sets_flag(self):
        """Killing the subprocess sets crashed=True with a reason."""
        config = StdioServerConfig(name="mock", command=sys.executable, args=[MOCK_SERVER_PATH])
        transport = StdioTransport(config, timeout=10.0)
        await transport.start()
        await transport.initialize()

        assert not transport.crashed

        # Kill the subprocess
        transport.process.kill()

        # Wait for the monitor to detect the crash
        for _ in range(30):
            if transport.crashed:
                break
            await asyncio.sleep(0.1)

        assert transport.crashed
        assert transport.crash_reason is not None
        assert "code" in transport.crash_reason.lower()

        await transport.stop()

    @pytest.mark.asyncio
    async def test_crash_rejects_pending_request(self):
        """In-flight requests are rejected when the subprocess crashes.

        Uses the slow server so the request stays pending long enough to
        kill the process mid-request.
        """
        config = StdioServerConfig(
            name="slow",
            command=sys.executable,
            args=[SLOW_SERVER_PATH],
            env={"MOCK_DELAY": "10"},
        )
        transport = StdioTransport(config, timeout=30.0)
        await transport.start()
        await transport.initialize()

        # Send a tools/call request (slow server sleeps 10s before responding)
        request_task = asyncio.create_task(transport.call_tool("slow_tool", {}))

        # Give it a moment to send the request
        await asyncio.sleep(0.1)

        # Kill the subprocess mid-request
        transport.process.kill()

        # The pending request should be rejected
        with pytest.raises(StdioTransportError):
            await request_task

        assert transport.crashed
        await transport.stop()

    @pytest.mark.asyncio
    async def test_normal_stop_does_not_set_crashed(self):
        """Calling stop() should not set the crashed flag."""
        config = StdioServerConfig(name="mock", command=sys.executable, args=[MOCK_SERVER_PATH])
        transport = StdioTransport(config, timeout=10.0)
        await transport.start()
        await transport.initialize()

        await transport.stop()

        assert not transport.crashed
        assert transport.crash_reason is None

    @pytest.mark.asyncio
    async def test_crash_callback_invoked(self):
        """The crash callback is called when the subprocess crashes."""
        config = StdioServerConfig(name="mock", command=sys.executable, args=[MOCK_SERVER_PATH])
        transport = StdioTransport(config, timeout=10.0)

        callback_called = asyncio.Event()
        callback_returncode: list[int] = []

        async def on_crash(name: str, returncode: int) -> None:
            callback_returncode.append(returncode)
            callback_called.set()

        transport.set_crash_callback(on_crash)
        await transport.start()
        await transport.initialize()

        transport.process.kill()

        # Wait for the callback to fire
        await asyncio.wait_for(callback_called.wait(), timeout=3.0)

        assert len(callback_returncode) == 1
        assert callback_returncode[0] != 0  # killed process has non-zero exit

        await transport.stop()


# -- Restart (transport level) ------------------------------------------------


class TestTransportRestart:
    @pytest.mark.asyncio
    async def test_restart_creates_new_process(self):
        """restart() stops the old process and spawns a new one."""
        config = StdioServerConfig(name="mock", command=sys.executable, args=[MOCK_SERVER_PATH])
        transport = StdioTransport(config, timeout=10.0)
        await transport.start()
        await transport.initialize()

        old_pid = transport.process.pid

        await transport.restart()

        assert transport.is_running
        assert transport.is_initialized
        assert transport.process.pid != old_pid  # New process

        # Verify the new process works
        tools = await transport.list_tools()
        assert len(tools) == 2

        await transport.stop()

    @pytest.mark.asyncio
    async def test_restart_after_crash(self):
        """restart() clears the crashed flag and starts fresh."""
        config = StdioServerConfig(name="mock", command=sys.executable, args=[MOCK_SERVER_PATH])
        transport = StdioTransport(config, timeout=10.0)
        await transport.start()
        await transport.initialize()

        # Crash the process
        transport.process.kill()
        await asyncio.sleep(0.3)
        assert transport.crashed

        # Restart
        await transport.restart()

        assert not transport.crashed
        assert transport.is_running
        assert transport.is_initialized

        # Verify it works
        result = await transport.call_tool("echo", {"text": "after-restart"})
        assert result["content"][0]["text"] == "after-restart"

        await transport.stop()


# -- Auto-restart (gateway level) ----------------------------------------------


class TestAutoRestart:
    @pytest.mark.asyncio
    async def test_auto_restart_recovers(self, tmp_path: Path):
        """A crashed server with auto_restart=true is restarted and tools re-registered."""
        config_path = _write_config(tmp_path, auto_restart=True, max_restarts=3)
        async with Gateway(config_path) as gw:
            transport = gw.transports["mock"]
            assert transport.is_running
            assert len(gw.registry) > 0

            # Kill the subprocess
            transport.process.kill()

            # Wait for the crash callback to restart the server.
            # The crash is detected, restart() runs (clearing crashed flag),
            # and _restart_counts is incremented — so we wait for that.
            for _ in range(60):  # up to 6 seconds
                if (
                    gw._restart_counts.get("mock", 0) > 0
                    and transport.is_running
                    and transport.is_initialized
                ):
                    break
                await asyncio.sleep(0.1)

            assert transport.is_running
            assert transport.is_initialized
            assert len(gw.registry) > 0  # Tools re-registered
            assert gw._restart_counts.get("mock") == 1

    @pytest.mark.asyncio
    async def test_auto_restart_max_exceeded(self, tmp_path: Path):
        """After max_restarts crashes, the gateway gives up and removes tools."""
        config_path = _write_config(tmp_path, auto_restart=True, max_restarts=1)
        async with Gateway(config_path) as gw:
            transport = gw.transports["mock"]

            # First crash — should restart
            transport.process.kill()

            # Wait for crash detection
            for _ in range(30):
                if transport.crashed:
                    break
                await asyncio.sleep(0.1)

            # Wait for restart to complete
            for _ in range(50):
                if transport.is_running and transport.is_initialized and not transport.crashed:
                    break
                await asyncio.sleep(0.1)

            assert transport.is_running
            assert gw._restart_counts.get("mock") == 1

            # Second crash — should give up (max_restarts=1)
            transport.process.kill()

            # Wait for crash detection
            for _ in range(30):
                if transport.crashed:
                    break
                await asyncio.sleep(0.1)

            # Wait for crash callback to run and give up (remove tools)
            for _ in range(50):
                if len(gw.registry) == 0:
                    break
                await asyncio.sleep(0.1)

            assert not transport.is_running
            assert len(gw.registry) == 0  # Tools removed


class TestNoAutoRestart:
    @pytest.mark.asyncio
    async def test_crash_without_auto_restart_removes_tools(self, tmp_path: Path):
        """Without auto_restart, a crash removes the server's tools from the registry."""
        config_path = _write_config(tmp_path, auto_restart=False)
        async with Gateway(config_path) as gw:
            transport = gw.transports["mock"]
            assert len(gw.registry) > 0

            transport.process.kill()

            # Wait for crash callback
            for _ in range(50):
                if len(gw.registry) == 0:
                    break
                await asyncio.sleep(0.1)

            assert transport.crashed
            assert not transport.is_running
            assert len(gw.registry) == 0  # Tools removed

    @pytest.mark.asyncio
    async def test_crashed_server_returns_error(self, tmp_path: Path):
        """Calling a tool on a crashed server returns a proper error."""
        config_path = _write_config(tmp_path, auto_restart=False)
        async with Gateway(config_path) as gw:
            transport = gw.transports["mock"]

            transport.process.kill()
            for _ in range(50):
                if len(gw.registry) == 0:
                    break
                await asyncio.sleep(0.1)

            # Registry is empty, so tool not found
            from mcp_baldr.gateway.server import ToolNotFoundError

            with pytest.raises(ToolNotFoundError):
                await gw.call_tool("mock__echo", {"text": "test"})


# -- Signal handling (subprocess level) ---------------------------------------


class TestSignalShutdown:
    @pytest.mark.asyncio
    async def test_sigint_clean_exit(self, tmp_path: Path):
        """SIGINT causes the gateway to shut down cleanly (exit code 0)."""
        config_path = _write_config(tmp_path)

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mcp_baldr.gateway.server",
            str(config_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            # Write initialize immediately — data buffers in the pipe until
            # the gateway enters its serve loop.
            init_req = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2026-07-28",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                }
            )
            proc.stdin.write(init_req.encode() + b"\n")
            await proc.stdin.drain()

            # Read initialize response (gateway startup takes a few seconds).
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=15.0)
            resp = json.loads(line)
            assert resp["result"]["serverInfo"]["name"] == "mcp-baldr"

            # Send SIGINT
            proc.send_signal(signal.SIGINT)

            # Wait for clean exit
            returncode = await asyncio.wait_for(proc.wait(), timeout=10.0)
            assert returncode == 0
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    @pytest.mark.asyncio
    async def test_sigterm_clean_exit(self, tmp_path: Path):
        """SIGTERM causes the gateway to shut down cleanly (exit code 0)."""
        config_path = _write_config(tmp_path)

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mcp_baldr.gateway.server",
            str(config_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            # Write initialize immediately — data buffers in the pipe.
            init_req = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2026-07-28",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                }
            )
            proc.stdin.write(init_req.encode() + b"\n")
            await proc.stdin.drain()

            # Read initialize response (gateway startup takes a few seconds).
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=15.0)
            resp = json.loads(line)
            assert resp["result"]["serverInfo"]["name"] == "mcp-baldr"

            # Send SIGTERM (now that handlers are registered)
            proc.send_signal(signal.SIGTERM)

            # Wait for clean exit
            returncode = await asyncio.wait_for(proc.wait(), timeout=10.0)
            assert returncode == 0
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


# -- Config-level timeout (transport level) -----------------------------------


class TestConfigTimeout:
    @pytest.mark.asyncio
    async def test_config_timeout_used_by_transport(self):
        """The timeout from StdioServerConfig is used by the transport."""
        config = StdioServerConfig(
            name="slow",
            command=sys.executable,
            args=[SLOW_SERVER_PATH],
            timeout=1.0,  # 1 second timeout
        )
        # Transport should pick up timeout from config
        transport = StdioTransport(config)
        assert transport.timeout == 1.0

        await transport.start()
        await transport.initialize()  # initialize is fast

        # tools/call sleeps for 5 seconds, but timeout is 1s
        with pytest.raises(StdioTransportError, match="timed out"):
            await transport.call_tool("slow_tool", {})

        await transport.stop()

    @pytest.mark.asyncio
    async def test_per_request_timeout_overrides_config(self):
        """An explicit timeout on send_request overrides the config timeout."""
        config = StdioServerConfig(
            name="slow",
            command=sys.executable,
            args=[SLOW_SERVER_PATH],
            timeout=10.0,  # 10 seconds (would not time out)
        )
        transport = StdioTransport(config)
        assert transport.timeout == 10.0

        await transport.start()
        await transport.initialize()

        # Override with a short timeout for this request
        with pytest.raises(StdioTransportError, match="timed out"):
            await transport.send_request(
                "tools/call", {"name": "slow_tool", "arguments": {}}, timeout=0.5
            )

        await transport.stop()
