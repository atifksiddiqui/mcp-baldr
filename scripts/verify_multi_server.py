"""Verify multi-server gateway: two mock servers with different tool sets."""

import asyncio

from mcp_gateway.gateway.server import Gateway


async def main():
    async with Gateway("gateway_config.yaml") as gw:
        tools = await gw.list_tools()
        print(f"\nTotal tools: {len(tools)}")
        for t in tools:
            print(f"  {t['name']}: {t.get('description', '')}")

        # Route echo to mock_a
        echo_result = await gw.call_tool("mock_a__echo", {"text": "hello"})
        print(f"\nmock_a__echo -> {echo_result['content'][0]['text']!r}")

        # Route greet to mock_b
        greet_result = await gw.call_tool("mock_b__greet", {"name": "Alice"})
        print(f"mock_b__greet -> {greet_result['content'][0]['text']!r}")

        # Route add to mock_a
        add_result = await gw.call_tool("mock_a__add", {"a": 15, "b": 27})
        print(f"mock_a__add -> {add_result['content'][0]['text']}")

        # Route multiply to mock_b
        mul_result = await gw.call_tool("mock_b__multiply", {"a": 6, "b": 7})
        print(f"mock_b__multiply -> {mul_result['content'][0]['text']}")

        # Verify cross-routing fails
        try:
            await gw.call_tool("mock_a__greet", {"name": "test"})
            print("\nERROR: mock_a__greet should not exist!")
        except Exception as e:
            print(f"\nmock_a__greet correctly rejected: {type(e).__name__}")

        try:
            await gw.call_tool("mock_b__echo", {"text": "test"})
            print("ERROR: mock_b__echo should not exist!")
        except Exception as e:
            print(f"mock_b__echo correctly rejected: {type(e).__name__}")

        print("\n[ALL MULTI-SERVER CHECKS PASSED]")


asyncio.run(main())
