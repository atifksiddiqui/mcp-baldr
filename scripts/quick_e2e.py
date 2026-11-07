"""Quick end-to-end verification of StdioTransport with the mock server.

Run with::

    python scripts/quick_e2e.py
"""

import asyncio
import sys

from mcp_baldr.config.models import StdioServerConfig
from mcp_baldr.logging_utils import setup_logging
from mcp_baldr.transports.stdio import StdioTransport

MOCK_SERVER = str(
    __import__("pathlib").Path(__file__).resolve().parent.parent / "examples" / "mock_server.py"
)


async def main():
    setup_logging(level="DEBUG", fmt="text")

    config = StdioServerConfig(
        name="mock",
        command=sys.executable,
        args=[MOCK_SERVER],
    )

    transport = StdioTransport(config, timeout=10.0)

    try:
        await transport.start()
        print(f"\n[OK] Started server '{transport.name}' (pid={transport.process.pid})")

        init_result = await transport.initialize()
        print(f"[OK] Initialized: {init_result.get('serverInfo')}")

        tools = await transport.list_tools()
        print(f"[OK] tools/list returned {len(tools)} tool(s):")
        for t in tools:
            print(f"       - {t['name']}: {t['description']}")

        echo_result = await transport.call_tool("echo", {"text": "hello world"})
        echo_text = echo_result["content"][0]["text"]
        print(f"[OK] tools/call echo: {echo_text!r}")
        assert echo_text == "hello world", f"Expected 'hello world', got {echo_text!r}"

        add_result = await transport.call_tool("add", {"a": 7, "b": 35})
        add_text = add_result["content"][0]["text"]
        print(f"[OK] tools/call add(7, 35): {add_text}")
        assert add_text == "42", f"Expected '42', got {add_text!r}"

        print("\n[ALL CHECKS PASSED]")
    finally:
        await transport.stop()
        print(f"[OK] Stopped. returncode={transport.returncode}")


if __name__ == "__main__":
    asyncio.run(main())
