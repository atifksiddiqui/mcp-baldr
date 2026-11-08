"""Tests for the downstream Streamable HTTP transport (MCP 2026-07-28).

Unit tests cover the transport's behavior without a server (lifecycle guards,
unsupported server→client requests).  Live tests spawn the real
``http_mock_server.py`` (via the ``HttpMockServer`` helper) and exercise
``HttpTransport`` end to end, plus a full gateway aggregating an HTTP
downstream.  The stdio path is untouched by any of this.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_baldr.config.models import HttpServerConfig
from mcp_baldr.gateway.server import Gateway
from mcp_baldr.transports import HttpTransport, HttpTransportError
from tests.conftest import HttpMockServer, write_gateway_config

# -- Unit: lifecycle + unsupported operations (no server) ---------------------


class TestHttpTransportUnit:
    def test_not_started_raises(self):
        t = HttpTransport(HttpServerConfig(type="http", name="x", url="http://127.0.0.1:1/mcp"))
        assert t.is_running is False

    @pytest.mark.asyncio
    async def test_send_request_before_start_raises(self):
        t = HttpTransport(HttpServerConfig(type="http", name="x", url="http://127.0.0.1:1/mcp"))
        with pytest.raises(HttpTransportError, match="not started"):
            await t.send_request("tools/list", {})

    @pytest.mark.asyncio
    async def test_server_to_client_requests_unsupported(self):
        """2026-07-28 Streamable HTTP uses MRTR, not server→client requests."""
        t = HttpTransport(HttpServerConfig(type="http", name="x", url="http://127.0.0.1:1/mcp"))
        with pytest.raises(HttpTransportError, match="MRTR|does not carry"):
            await t.send_response(1, {})
        with pytest.raises(HttpTransportError, match="MRTR|does not carry"):
            await t.send_error_response(1, -32601, "nope")

    def test_name_from_config(self):
        t = HttpTransport(HttpServerConfig(type="http", name="remote", url="https://a/mcp"))
        assert t.name == "remote"


# -- Live: HttpTransport against a real Streamable HTTP server ----------------


class TestHttpTransportLive:
    @pytest.mark.asyncio
    async def test_initialize_and_capabilities(self):
        with HttpMockServer() as srv:
            t = HttpTransport(HttpServerConfig(type="http", name="remote", url=srv.url))
            await t.start()
            try:
                init = await t.initialize()
                assert init["serverInfo"]["name"] == "http-mock"
                assert t.is_running
                caps = t.server_capabilities
                assert "tools" in caps
                assert "resources" in caps
                assert "prompts" in caps
                # The SDK server negotiates a protocol version; the transport
                # records whatever it is rather than assuming.
                assert t.server_protocol_version is not None
            finally:
                await t.stop()
            assert t.is_running is False

    @pytest.mark.asyncio
    async def test_tools_list_and_call(self):
        with HttpMockServer() as srv:
            t = HttpTransport(HttpServerConfig(type="http", name="remote", url=srv.url))
            await t.start()
            try:
                await t.initialize()
                tools = await t.list_tools()
                names = sorted(x["name"] for x in tools)
                assert names == ["add", "echo"]

                echo = await t.call_tool("echo", {"text": "hi over http"})
                assert echo["content"][0]["text"] == "hi over http"
                assert echo["isError"] is False

                add = await t.call_tool("add", {"a": 3, "b": 4})
                # Structured content carries the typed result.
                assert add["structuredContent"]["result"] == 7
            finally:
                await t.stop()

    @pytest.mark.asyncio
    async def test_resources_and_prompts(self):
        with HttpMockServer() as srv:
            t = HttpTransport(HttpServerConfig(type="http", name="remote", url=srv.url))
            await t.start()
            try:
                await t.initialize()
                resources = await t.send_request("resources/list", {})
                uris = [r["uri"] for r in resources["resources"]]
                assert "mock://docs/readme.txt" in uris

                read = await t.send_request("resources/read", {"uri": "mock://docs/readme.txt"})
                assert "Hello from the HTTP mock server." in read["contents"][0]["text"]

                prompts = await t.send_request("prompts/list", {})
                pnames = sorted(p["name"] for p in prompts["prompts"])
                assert pnames == ["greeting", "summarize"]

                got = await t.send_request(
                    "prompts/get", {"name": "greeting", "arguments": {"name": "Al"}}
                )
                assert "Al" in got["messages"][0]["content"]["text"]
            finally:
                await t.stop()

    @pytest.mark.asyncio
    async def test_ping(self):
        with HttpMockServer() as srv:
            t = HttpTransport(HttpServerConfig(type="http", name="remote", url=srv.url))
            await t.start()
            try:
                await t.initialize()
                assert await t.send_request("ping", {}) == {}
            finally:
                await t.stop()

    @pytest.mark.asyncio
    async def test_restart(self):
        with HttpMockServer() as srv:
            t = HttpTransport(HttpServerConfig(type="http", name="remote", url=srv.url))
            await t.start()
            await t.initialize()
            await t.restart()
            try:
                assert t.is_running
                tools = await t.list_tools()
                assert len(tools) == 2
            finally:
                await t.stop()


# -- Live: gateway aggregating an HTTP downstream -----------------------------


class TestGatewayWithHttpDownstream:
    def _config(self, tmp_path: Path, url: str) -> Path:
        yaml = f"""\
        servers:
          - type: http
            name: remote
            url: {url}
        logging:
          level: WARNING
          format: text
        """
        return write_gateway_config(tmp_path, yaml)

    @pytest.mark.asyncio
    async def test_gateway_aggregates_http_server(self, tmp_path: Path):
        with HttpMockServer() as srv:
            async with Gateway(self._config(tmp_path, srv.url)) as gw:
                # Tools from the HTTP downstream are namespaced + routed.
                tools = await gw._handle_request("tools/list", {})
                names = {t["name"] for t in tools["tools"]}
                assert "remote__echo" in names
                assert "remote__add" in names

                result = await gw._handle_request(
                    "tools/call", {"name": "remote__echo", "arguments": {"text": "yo"}}
                )
                assert result["content"][0]["text"] == "yo"

                # Resources aggregate with namespaced URIs; read routes back.
                resources = await gw._handle_request("resources/list", {})
                assert resources["resources"], "expected aggregated resources"
                ns_uri = resources["resources"][0]["uri"]
                assert ns_uri.startswith("baldr+remote://")
                read = await gw._handle_request("resources/read", {"uri": ns_uri})
                assert read["contents"]

                # Prompts aggregate + route.
                prompts = await gw._handle_request("prompts/list", {})
                pnames = {p["name"] for p in prompts["prompts"]}
                assert "remote__greeting" in pnames

    @pytest.mark.asyncio
    async def test_gateway_mixed_stdio_and_http(self, tmp_path: Path):
        """A config with both a stdio and an http downstream aggregates both."""
        import sys

        from tests.conftest import MOCK_SERVER_PATH

        with HttpMockServer() as srv:
            yaml = f"""\
            servers:
              - name: local
                command: {sys.executable}
                args: ["{MOCK_SERVER_PATH}"]
              - type: http
                name: remote
                url: {srv.url}
            logging:
              level: WARNING
              format: text
            """
            config_path = write_gateway_config(tmp_path, yaml)
            async with Gateway(config_path) as gw:
                tools = await gw._handle_request("tools/list", {})
                names = {t["name"] for t in tools["tools"]}
                assert any(n.startswith("local__") for n in names)
                assert any(n.startswith("remote__") for n in names)


# -- Timeout is honored -------------------------------------------------------


class TestHttpTransportTimeout:
    @pytest.mark.asyncio
    async def test_connect_timeout_fails_fast(self):
        """A short timeout to an unroutable address fails fast, not hangs.

        198.51.100.0/24 (TEST-NET-2, RFC 5737) is reserved for documentation
        and is not routable, so the connection attempt stalls until the
        configured timeout trips.
        """
        import time

        cfg = HttpServerConfig(
            type="http",
            name="dead",
            url="http://198.51.100.1:9/mcp",
            timeout=1.0,
        )
        t = HttpTransport(cfg)
        start = time.monotonic()
        with pytest.raises(HttpTransportError):
            await t.start()
            await t.initialize()
        elapsed = time.monotonic() - start
        # Must fail well before any default multi-second SDK timeout.
        assert elapsed < 10.0
        await t.stop()
