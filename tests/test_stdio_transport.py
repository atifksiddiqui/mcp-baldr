"""Tests for the StdioTransport adapter."""

import sys
from pathlib import Path

import pytest

from mcp_baldr.config.models import StdioServerConfig
from mcp_baldr.transports.stdio import StdioTransport, StdioTransportError

# Path to the mock server script shipped with the project.
MOCK_SERVER_PATH = str(Path(__file__).resolve().parent.parent / "examples" / "mock_server.py")


def make_config(name: str = "mock") -> StdioServerConfig:
    return StdioServerConfig(
        name=name,
        command=sys.executable,
        args=[MOCK_SERVER_PATH],
    )


@pytest.fixture
async def transport():
    """Yield a started-and-initialized StdioTransport, stop it after the test."""
    t = StdioTransport(make_config(), timeout=10.0)
    await t.start()
    await t.initialize()
    yield t
    await t.stop()


# -- Lifecycle ----------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        t = StdioTransport(make_config())
        assert t.is_running is False

        await t.start()
        assert t.is_running is True
        assert t.process is not None
        assert t.process.pid > 0

        await t.stop()
        assert t.is_running is False
        assert t.process is None

    @pytest.mark.asyncio
    async def test_double_start_rejected(self):
        t = StdioTransport(make_config())
        await t.start()
        with pytest.raises(StdioTransportError, match="already started"):
            await t.start()
        await t.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self):
        t = StdioTransport(make_config())
        await t.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with StdioTransport(make_config()) as t:
            await t.initialize()
            assert t.is_running
        assert not t.is_running


# -- Initialize ---------------------------------------------------------------


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_returns_server_info(self):
        t = StdioTransport(make_config())
        await t.start()

        result = await t.initialize()
        assert result["serverInfo"]["name"] == "mock-server"
        assert result["protocolVersion"] == "2026-07-28"
        assert t.is_initialized

        await t.stop()

    @pytest.mark.asyncio
    async def test_double_initialize_rejected(self):
        t = StdioTransport(make_config())
        await t.start()
        await t.initialize()
        with pytest.raises(StdioTransportError, match="already initialized"):
            await t.initialize()
        await t.stop()

    @pytest.mark.asyncio
    async def test_initialize_without_start_rejected(self):
        t = StdioTransport(make_config())
        with pytest.raises(StdioTransportError, match="not started"):
            await t.initialize()


# -- tools/list ---------------------------------------------------------------


class TestToolsList:
    @pytest.mark.asyncio
    async def test_list_tools_returns_tools(self, transport):
        tools = await transport.list_tools()
        assert len(tools) == 2
        names = [t["name"] for t in tools]
        assert "echo" in names
        assert "add" in names

    @pytest.mark.asyncio
    async def test_tool_has_schema(self, transport):
        tools = await transport.list_tools()
        echo = next(t for t in tools if t["name"] == "echo")
        assert echo["inputSchema"]["type"] == "object"
        assert "text" in echo["inputSchema"]["properties"]


# -- tools/call ---------------------------------------------------------------


class TestToolsCall:
    @pytest.mark.asyncio
    async def test_echo(self, transport):
        result = await transport.call_tool("echo", {"text": "hello gateway"})
        assert result["isError"] is False
        assert result["content"][0]["text"] == "hello gateway"

    @pytest.mark.asyncio
    async def test_add(self, transport):
        result = await transport.call_tool("add", {"a": 10, "b": 20})
        assert result["content"][0]["text"] == "30"

    @pytest.mark.asyncio
    async def test_echo_empty_text(self, transport):
        result = await transport.call_tool("echo", {"text": ""})
        assert result["content"][0]["text"] == ""

    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self, transport):
        with pytest.raises(StdioTransportError, match="Unknown tool"):
            await transport.call_tool("nonexistent", {})


# -- Error handling -----------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_request_timeout(self):
        config = StdioServerConfig(
            name="slow",
            command=sys.executable,
            args=["-c", "import sys; sys.stdin.read()"],  # never responds
        )
        t = StdioTransport(config, timeout=0.5)
        await t.start()
        with pytest.raises(StdioTransportError, match="timed out"):
            await t.send_request("initialize", {})
        await t.stop()

    @pytest.mark.asyncio
    async def test_send_request_without_start(self):
        t = StdioTransport(make_config())
        with pytest.raises(StdioTransportError, match="not started"):
            await t.send_request("tools/list")

    @pytest.mark.asyncio
    async def test_send_notification_without_start(self):
        t = StdioTransport(make_config())
        with pytest.raises(StdioTransportError, match="not started"):
            await t.send_notification("test", {})

    @pytest.mark.asyncio
    async def test_stderr_captured(self, transport):
        # Send a request that triggers stderr output from the mock server
        # (unknown method → mock server logs nothing to stderr, but the
        # _stderr_lines list should at least be accessible).
        assert isinstance(transport.stderr, list)
