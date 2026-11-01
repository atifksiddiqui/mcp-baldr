"""End-to-end integration test: config → transport → mock server round-trip.

This test loads a real YAML config, starts the configured mock server
subprocess, performs the MCP handshake, lists tools, and calls them.
"""

import sys
import textwrap
from pathlib import Path

import pytest

from mcp_gateway.config import load_config
from mcp_gateway.transports.stdio import StdioTransport

MOCK_SERVER_PATH = str(Path(__file__).resolve().parent.parent / "examples" / "mock_server.py")


@pytest.fixture
def gateway_yaml(tmp_path: Path) -> Path:
    """Write a minimal gateway_config.yaml pointing at the mock server."""
    yaml_text = textwrap.dedent(f"""\
        servers:
          - name: mock
            command: {sys.executable}
            args: ["{MOCK_SERVER_PATH}"]
        logging:
          level: INFO
          format: json
    """)
    path = tmp_path / "gateway_config.yaml"
    path.write_text(yaml_text)
    return path


@pytest.mark.asyncio
async def test_full_round_trip(gateway_yaml: Path):
    """Load config → start transport → initialize → list → call → stop."""
    # 1. Load and validate config.
    config = load_config(gateway_yaml)
    assert len(config.servers) == 1
    assert config.servers[0].name == "mock"

    server_config = config.enabled_servers()[0]
    transport = StdioTransport(server_config, timeout=10.0)

    try:
        # 2. Start subprocess.
        await transport.start()
        assert transport.is_running

        # 3. MCP handshake.
        init_result = await transport.initialize()
        assert init_result["serverInfo"]["name"] == "mock-server"

        # 4. tools/list.
        tools = await transport.list_tools()
        assert len(tools) == 2
        tool_names = sorted(t["name"] for t in tools)
        assert tool_names == ["add", "echo"]

        # 5. tools/call — echo.
        echo_result = await transport.call_tool("echo", {"text": "gateway works"})
        assert echo_result["content"][0]["text"] == "gateway works"
        assert echo_result["isError"] is False

        # 6. tools/call — add.
        add_result = await transport.call_tool("add", {"a": 100, "b": 200})
        assert add_result["content"][0]["text"] == "300"
        assert add_result["isError"] is False
    finally:
        # 7. Clean shutdown.
        await transport.stop()
        assert not transport.is_running
        assert transport.process is None


@pytest.mark.asyncio
async def test_disabled_server_not_started(gateway_yaml: Path):
    """Servers with enabled: false are filtered out by enabled_servers()."""
    config = load_config(gateway_yaml)
    # Add a disabled server to the config.
    from mcp_gateway.config.models import StdioServerConfig

    config.servers.append(StdioServerConfig(name="disabled_one", command="echo", enabled=False))
    enabled = config.enabled_servers()
    assert len(enabled) == 1
    assert enabled[0].name == "mock"


@pytest.mark.asyncio
async def test_multiple_sequential_requests(gateway_yaml: Path):
    """Send several requests in sequence to verify id correlation works."""
    config = load_config(gateway_yaml)
    transport = StdioTransport(config.enabled_servers()[0], timeout=10.0)

    try:
        await transport.start()
        await transport.initialize()

        # Send 5 echo calls in a row.
        for i in range(5):
            result = await transport.call_tool("echo", {"text": f"msg-{i}"})
            assert result["content"][0]["text"] == f"msg-{i}"

        # A tools/list after calls still works.
        tools = await transport.list_tools()
        assert len(tools) == 2
    finally:
        await transport.stop()
