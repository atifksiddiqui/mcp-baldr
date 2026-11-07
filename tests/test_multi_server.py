"""Integration tests for multi-server namespace routing.

These tests spawn two mock servers with different tool sets behind the
gateway, then connect to the gateway as an MCP client (using StdioTransport)
to verify namespace prefixing and routing.

Topology::

    StdioTransport (test client)
        │ stdin/stdout
        ▼
    Gateway subprocess
        │ stdin/stdout          │ stdin/stdout
        ▼                       ▼
    mock_server (default)    mock_server (alt)
    tools: echo, add          tools: greet, multiply
"""

import sys
import textwrap
from pathlib import Path

import pytest

from mcp_baldr.config.models import StdioServerConfig
from mcp_baldr.gateway.server import Gateway, ToolNotFoundError
from mcp_baldr.transports.stdio import StdioTransport, StdioTransportError

MOCK_SERVER_PATH = str(Path(__file__).resolve().parent.parent / "examples" / "mock_server.py")


def _make_config_path(tmp_path: Path, servers_yaml: str) -> Path:
    """Write a gateway_config.yaml to a temp file and return its path."""
    path = tmp_path / "gateway_config.yaml"
    path.write_text(textwrap.dedent(servers_yaml))
    return path


# -- Gateway-level routing (direct, no subprocess) ----------------------------


class TestGatewayRouting:
    @pytest.fixture
    def config_path(self, tmp_path: Path) -> Path:
        return _make_config_path(
            tmp_path,
            f"""\
            servers:
              - name: mock_a
                command: {sys.executable}
                args: ["{MOCK_SERVER_PATH}"]
              - name: mock_b
                command: {sys.executable}
                args: ["{MOCK_SERVER_PATH}"]
                env:
                  MOCK_TOOLS: "alt"
                  MOCK_SERVER_NAME: "mock-server-b"
            logging:
              level: WARNING
              format: text
            """,
        )

    @pytest.mark.asyncio
    async def test_two_servers_discovered(self, config_path: Path):
        async with Gateway(config_path) as gw:
            tools = await gw.list_tools()
            names = sorted(t["name"] for t in tools)
            assert names == ["mock_a__add", "mock_a__echo", "mock_b__greet", "mock_b__multiply"]

    @pytest.mark.asyncio
    async def test_route_to_server_a(self, config_path: Path):
        async with Gateway(config_path) as gw:
            result = await gw.call_tool("mock_a__echo", {"text": "from-a"})
            assert result["content"][0]["text"] == "from-a"

            result = await gw.call_tool("mock_a__add", {"a": 3, "b": 4})
            assert result["content"][0]["text"] == "7"

    @pytest.mark.asyncio
    async def test_route_to_server_b(self, config_path: Path):
        async with Gateway(config_path) as gw:
            result = await gw.call_tool("mock_b__greet", {"name": "World"})
            assert result["content"][0]["text"] == "Hello, World!"

            result = await gw.call_tool("mock_b__multiply", {"a": 6, "b": 7})
            assert result["content"][0]["text"] == "42"

    @pytest.mark.asyncio
    async def test_cross_route_rejected(self, config_path: Path):
        """mock_a__greet should not exist (greet is on mock_b)."""
        async with Gateway(config_path) as gw:
            with pytest.raises(ToolNotFoundError, match="mock_a__greet"):
                await gw.call_tool("mock_a__greet", {"name": "test"})

    @pytest.mark.asyncio
    async def test_unknown_tool_rejected(self, config_path: Path):
        async with Gateway(config_path) as gw:
            with pytest.raises(ToolNotFoundError, match="nonexistent__tool"):
                await gw.call_tool("nonexistent__tool", {})

    @pytest.mark.asyncio
    async def test_same_tool_name_different_servers(self, tmp_path: Path):
        """Both servers expose 'echo' — namespace keeps them distinct."""
        config_path = _make_config_path(
            tmp_path,
            f"""\
            servers:
              - name: srv_a
                command: {sys.executable}
                args: ["{MOCK_SERVER_PATH}"]
              - name: srv_b
                command: {sys.executable}
                args: ["{MOCK_SERVER_PATH}"]
            logging:
              level: WARNING
              format: text
            """,
        )
        async with Gateway(config_path) as gw:
            tools = await gw.list_tools()
            # Both have echo and add, namespaced distinctly
            names = sorted(t["name"] for t in tools)
            assert names == ["srv_a__add", "srv_a__echo", "srv_b__add", "srv_b__echo"]

            # Route to srv_a
            r_a = await gw.call_tool("srv_a__echo", {"text": "from-a"})
            assert r_a["content"][0]["text"] == "from-a"

            # Route to srv_b (same tool name, different server)
            r_b = await gw.call_tool("srv_b__echo", {"text": "from-b"})
            assert r_b["content"][0]["text"] == "from-b"


# -- Full gateway subprocess (MCP server loop) --------------------------------


class TestGatewayServerLoop:
    """Spawn the gateway as a subprocess and talk to it via StdioTransport.

    This tests the full MCP server loop (initialize → tools/list → tools/call)
    through the gateway's stdin/stdout, exercising the routing end-to-end.
    """

    @pytest.fixture
    def config_path(self, tmp_path: Path) -> Path:
        return _make_config_path(
            tmp_path,
            f"""\
            servers:
              - name: mock_a
                command: {sys.executable}
                args: ["{MOCK_SERVER_PATH}"]
              - name: mock_b
                command: {sys.executable}
                args: ["{MOCK_SERVER_PATH}"]
                env:
                  MOCK_TOOLS: "alt"
                  MOCK_SERVER_NAME: "mock-server-b"
            logging:
              level: WARNING
              format: text
            """,
        )

    @pytest.fixture
    async def client(self, config_path: Path):
        """StdioTransport connected to the gateway subprocess."""
        gateway_config = StdioServerConfig(
            name="gateway",
            command=sys.executable,
            args=["-m", "mcp_baldr.gateway.server", str(config_path)],
            env={"PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
        )
        transport = StdioTransport(gateway_config, timeout=30.0)
        await transport.start()
        await transport.initialize()
        yield transport
        await transport.stop()

    @pytest.mark.asyncio
    async def test_initialize_handshake(self, client):
        """Gateway responds to initialize with gateway server info."""
        # initialize already happened in the fixture; just verify it's initialized
        assert client.is_initialized

    @pytest.mark.asyncio
    async def test_tools_list_namespaced(self, client):
        tools = await client.list_tools()
        names = sorted(t["name"] for t in tools)
        assert names == ["mock_a__add", "mock_a__echo", "mock_b__greet", "mock_b__multiply"]

    @pytest.mark.asyncio
    async def test_call_tool_routes_to_a(self, client):
        result = await client.call_tool("mock_a__echo", {"text": "via-gateway"})
        assert result["content"][0]["text"] == "via-gateway"

    @pytest.mark.asyncio
    async def test_call_tool_routes_to_b(self, client):
        result = await client.call_tool("mock_b__greet", {"name": "Gateway"})
        assert result["content"][0]["text"] == "Hello, Gateway!"

    @pytest.mark.asyncio
    async def test_call_unknown_tool_error(self, client):
        with pytest.raises(StdioTransportError, match="Tool not found"):
            await client.call_tool("nonexistent__tool", {})

    @pytest.mark.asyncio
    async def test_cross_route_error(self, client):
        """mock_a__greet doesn't exist — should get an error."""
        with pytest.raises(StdioTransportError, match="Tool not found"):
            await client.call_tool("mock_a__greet", {"name": "test"})

    @pytest.mark.asyncio
    async def test_sequential_calls_mixed_routing(self, client):
        """Interleave calls to both servers to verify routing stability."""
        for i in range(3):
            r = await client.call_tool("mock_a__echo", {"text": f"a-{i}"})
            assert r["content"][0]["text"] == f"a-{i}"

            r = await client.call_tool("mock_b__multiply", {"a": i + 1, "b": 2})
            assert r["content"][0]["text"] == str((i + 1) * 2)
