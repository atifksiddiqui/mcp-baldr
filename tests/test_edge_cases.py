"""Edge-case tests: concurrent calls, malformed input, logging, registry stress."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from mcp_baldr.gateway.server import Gateway
from mcp_baldr.logging_utils import _JsonFormatter, _TextFormatter, setup_logging
from mcp_baldr.registry.tool_registry import ToolRegistry
from mcp_baldr.transports.stdio import StdioTransport
from tests.conftest import make_mock_server_config, write_two_server_config

# -- Concurrent tool calls ----------------------------------------------------


class TestConcurrentCalls:
    @pytest.mark.asyncio
    async def test_concurrent_calls_same_server(self):
        """Multiple concurrent calls to the same server work correctly."""
        transport = StdioTransport(make_mock_server_config(), timeout=10.0)
        await transport.start()
        await transport.initialize()

        try:
            # Fire 5 echo calls concurrently
            tasks = [transport.call_tool("echo", {"text": f"msg-{i}"}) for i in range(5)]
            results = await asyncio.gather(*tasks)

            texts = sorted(r["content"][0]["text"] for r in results)
            assert texts == ["msg-0", "msg-1", "msg-2", "msg-3", "msg-4"]
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_concurrent_calls_mixed_tools(self):
        """Concurrent calls to different tools on the same server."""
        transport = StdioTransport(make_mock_server_config(), timeout=10.0)
        await transport.start()
        await transport.initialize()

        try:
            tasks = [
                transport.call_tool("echo", {"text": "a"}),
                transport.call_tool("add", {"a": 1, "b": 2}),
                transport.call_tool("echo", {"text": "b"}),
                transport.call_tool("add", {"a": 10, "b": 20}),
            ]
            results = await asyncio.gather(*tasks)
            assert results[0]["content"][0]["text"] == "a"
            assert results[1]["content"][0]["text"] == "3"
            assert results[2]["content"][0]["text"] == "b"
            assert results[3]["content"][0]["text"] == "30"
        finally:
            await transport.stop()

    @pytest.mark.asyncio
    async def test_concurrent_calls_through_gateway(self, tmp_path: Path):
        """Concurrent routed calls through the gateway to different servers."""
        config_path = write_two_server_config(tmp_path)
        async with Gateway(config_path) as gw:
            tasks = [
                gw.call_tool("mock_a__echo", {"text": "from-a"}),
                gw.call_tool("mock_b__greet", {"name": "Alice"}),
                gw.call_tool("mock_a__add", {"a": 5, "b": 10}),
                gw.call_tool("mock_b__multiply", {"a": 3, "b": 4}),
            ]
            results = await asyncio.gather(*tasks)
            assert results[0]["content"][0]["text"] == "from-a"
            assert results[1]["content"][0]["text"] == "Hello, Alice!"
            assert results[2]["content"][0]["text"] == "15"
            assert results[3]["content"][0]["text"] == "12"


# -- Malformed input to gateway ------------------------------------------------


class TestMalformedInput:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_parse_error(self, tmp_path: Path):
        """Malformed JSON on the gateway stdin returns a parse error response."""
        config_path = write_two_server_config(tmp_path)

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
            # Send initialize first (needed before other methods)
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
            await asyncio.wait_for(proc.stdout.readline(), timeout=15.0)

            # Send malformed JSON
            proc.stdin.write(b"this is not json\n")
            await proc.stdin.drain()

            # Should get a parse error response
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
            resp = json.loads(line)
            assert resp["error"]["code"] == -32700
            assert "parse" in resp["error"]["message"].lower()
        finally:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=10.0)

    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(self, tmp_path: Path):
        """Unknown MCP method returns a method-not-found error."""
        config_path = write_two_server_config(tmp_path)

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
            # Initialize first
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
            await asyncio.wait_for(proc.stdout.readline(), timeout=15.0)

            # Send unknown method
            unknown_req = json.dumps(
                {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {}}
            )
            proc.stdin.write(unknown_req.encode() + b"\n")
            await proc.stdin.drain()

            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
            resp = json.loads(line)
            assert resp["error"]["code"] == -32601
            assert "method not found" in resp["error"]["message"].lower()
        finally:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=10.0)

    @pytest.mark.asyncio
    async def test_empty_line_ignored(self, tmp_path: Path):
        """Empty lines on stdin are silently ignored."""
        config_path = write_two_server_config(tmp_path)

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
            # Send an empty line followed by a valid initialize
            proc.stdin.write(b"\n")
            await proc.stdin.drain()

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

            # Should get the initialize response (empty line ignored)
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=15.0)
            resp = json.loads(line)
            assert resp["result"]["serverInfo"]["name"] == "mcp-baldr"
        finally:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=10.0)


# -- Registry stress ----------------------------------------------------------


class TestRegistryStress:
    def test_many_servers(self):
        """Registry handles many servers with overlapping tool names."""
        registry = ToolRegistry()
        for i in range(20):
            tools = [
                {
                    "name": f"tool_{j}",
                    "description": f"Tool {j} from server {i}",
                    "inputSchema": {"type": "object"},
                }
                for j in range(5)
            ]
            registry.register_server_tools(f"srv_{i:02d}", tools)

        assert len(registry) == 100
        assert len(registry.servers()) == 20

        # Resolve works for each
        for i in range(20):
            for j in range(5):
                result = registry.resolve(f"srv_{i:02d}__tool_{j}")
                assert result == (f"srv_{i:02d}", f"tool_{j}")

        # List returns all 100 tools with namespaced names
        client_tools = registry.list_for_client()
        assert len(client_tools) == 100

    def test_register_unregister_all_servers(self):
        """Unregistering all servers leaves registry empty."""
        registry = ToolRegistry()
        for i in range(10):
            registry.register_server_tools(f"srv_{i}", [{"name": "tool", "inputSchema": {}}])

        assert len(registry) == 10

        for i in range(10):
            removed = registry.unregister_server(f"srv_{i}")
            assert removed == 1

        assert len(registry) == 0
        assert len(registry.servers()) == 0


# -- Logging utilities --------------------------------------------------------


class TestLoggingUtils:
    def test_json_formatter_outputs_valid_json(self):
        """The JSON formatter produces parseable JSON with expected fields."""
        import logging

        formatter = _JsonFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        record.server = "test_server"
        record.method = "tools/list"

        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "Test message"
        assert parsed["server"] == "test_server"
        assert parsed["method"] == "tools/list"
        assert "timestamp" in parsed

    def test_text_formatter_includes_extras(self):
        """The text formatter includes extra fields in the output."""
        import logging

        formatter = _TextFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.WARNING,
            pathname="test.py",
            lineno=1,
            msg="Warning message",
            args=(),
            exc_info=None,
        )
        record.server = "srv_1"

        output = formatter.format(record)
        assert "WARNING" in output
        assert "Warning message" in output
        assert "srv_1" in output

    def test_setup_logging_json(self, capsys):
        """setup_logging with json format configures the root logger."""
        import logging

        setup_logging(level="DEBUG", fmt="json")
        root = logging.getLogger()
        assert root.level == logging.DEBUG

        logging.getLogger("test").info("hello", extra={"server": "x"})
        captured = capsys.readouterr()
        # Log goes to stderr
        assert "hello" in captured.err
        assert '"server": "x"' in captured.err

    def test_setup_logging_text(self, capsys):
        """setup_logging with text format produces human-readable output."""
        import logging

        setup_logging(level="WARNING", fmt="text")
        root = logging.getLogger()
        assert root.level == logging.WARNING

        logging.getLogger("test").warning("test warning", extra={"server": "y"})
        captured = capsys.readouterr()
        assert "test warning" in captured.err
        assert "y" in captured.err
