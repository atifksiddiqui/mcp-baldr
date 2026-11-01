#!/usr/bin/env python3
"""Example MCP client that connects to the gateway and calls tools.

Demonstrates the full client-side flow: spawn the gateway, initialize,
list tools, and call them.  This is the same pattern an AI client
(Claude Desktop, Cursor, etc.) would use to interact with the gateway.

Run with::

    python scripts/example_client.py [gateway_config.yaml]
"""

import asyncio
import sys
from pathlib import Path

from mcp_gateway.config.models import StdioServerConfig
from mcp_gateway.logging_utils import setup_logging
from mcp_gateway.transports.stdio import StdioTransport


async def main():
    setup_logging(level="INFO", fmt="text")

    config_path = sys.argv[1] if len(sys.argv) > 1 else "gateway_config.yaml"

    # Spawn the gateway as a subprocess and treat it as an MCP server.
    gateway_config = StdioServerConfig(
        name="gateway",
        command=sys.executable,
        args=["-m", "mcp_gateway.gateway.server", str(Path(config_path).resolve())],
    )

    async with StdioTransport(gateway_config, timeout=30.0) as client:
        # 1. MCP handshake
        init_result = await client.initialize()
        server_info = init_result.get("serverInfo", {})
        print(f"\nConnected to: {server_info.get('name')} v{server_info.get('version')}")

        # 2. List all tools (namespaced)
        tools = await client.list_tools()
        print(f"\n{len(tools)} tool(s) available:")
        for tool in tools:
            print(f"  {tool['name']}: {tool.get('description', '')}")

        # 3. Call tools by their namespaced names
        if any(t["name"] == "mock_a__echo" for t in tools):
            result = await client.call_tool("mock_a__echo", {"text": "hello from client"})
            text = result["content"][0]["text"]
            print(f"\nmock_a__echo -> {text!r}")

        if any(t["name"] == "mock_a__add" for t in tools):
            result = await client.call_tool("mock_a__add", {"a": 17, "b": 25})
            text = result["content"][0]["text"]
            print(f"mock_a__add(17, 25) -> {text}")

        if any(t["name"] == "mock_b__greet" for t in tools):
            result = await client.call_tool("mock_b__greet", {"name": "Developer"})
            text = result["content"][0]["text"]
            print(f"mock_b__greet -> {text}")

        if any(t["name"] == "mock_b__multiply" for t in tools):
            result = await client.call_tool("mock_b__multiply", {"a": 8, "b": 9})
            text = result["content"][0]["text"]
            print(f"mock_b__multiply(8, 9) -> {text}")

    print("\n[Example complete]")


if __name__ == "__main__":
    asyncio.run(main())
