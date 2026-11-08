"""Tests for resource aggregation (Stage 3): URI codec, registry, routing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_baldr.gateway.server import Gateway, InvalidParamsError, ResourceNotFoundError
from mcp_baldr.protocol.uri import (
    NamespacedURIError,
    decode_namespaced_uri,
    encode_namespaced_uri,
    is_namespaced_uri,
)
from mcp_baldr.registry.resource_registry import ResourceRegistry
from tests.conftest import write_gateway_config

ROOT = Path(__file__).resolve().parent.parent
FULL_SERVER = str(ROOT / "examples" / "full_mock_server.py")
BASIC_SERVER = str(ROOT / "examples" / "mock_server.py")


# -- URI codec ----------------------------------------------------------------


class TestURICodec:
    @pytest.mark.parametrize(
        "uri",
        [
            "file:///home/user/doc.txt",
            "mock://docs/readme.txt",
            "https://example.com/a?b=c&d=e#frag",
            "weird:uri/with spaces/and:colons",
            "s3://bucket/key__with__underscores",
        ],
    )
    def test_round_trip(self, uri):
        ns = encode_namespaced_uri("srv", uri)
        assert is_namespaced_uri(ns)
        server, original = decode_namespaced_uri(ns)
        assert server == "srv"
        assert original == uri  # exact, lossless

    def test_collision_safety(self):
        # Same original URI from two servers → distinct namespaced URIs.
        a = encode_namespaced_uri("alpha", "mock://x")
        b = encode_namespaced_uri("beta", "mock://x")
        assert a != b
        assert decode_namespaced_uri(a) == ("alpha", "mock://x")
        assert decode_namespaced_uri(b) == ("beta", "mock://x")

    def test_original_uri_cannot_inject_structure(self):
        # An original URI that itself looks like a namespaced URI must still
        # round-trip to its exact self, not be misparsed.
        tricky = "baldr+evil://mock%3A%2F%2Fx"
        ns = encode_namespaced_uri("srv", tricky)
        server, original = decode_namespaced_uri(ns)
        assert server == "srv"
        assert original == tricky

    def test_empty_server_rejected(self):
        with pytest.raises(NamespacedURIError):
            encode_namespaced_uri("", "mock://x")

    def test_unsafe_server_rejected(self):
        with pytest.raises(NamespacedURIError):
            encode_namespaced_uri("bad/name", "mock://x")

    def test_decode_non_namespaced_rejected(self):
        with pytest.raises(NamespacedURIError):
            decode_namespaced_uri("file:///not/namespaced")

    def test_is_namespaced_uri(self):
        assert is_namespaced_uri("baldr+srv://encoded")
        assert not is_namespaced_uri("file:///x")


# -- ResourceRegistry ---------------------------------------------------------


class TestResourceRegistry:
    def test_register_and_resolve(self):
        reg = ResourceRegistry()
        reg.register_server_resources(
            "srv",
            [{"uri": "mock://a", "name": "a", "mimeType": "text/plain"}],
        )
        client = reg.list_resources_for_client()
        assert len(client) == 1
        ns_uri = client[0]["uri"]
        assert is_namespaced_uri(ns_uri)
        # Original metadata preserved.
        assert client[0]["mimeType"] == "text/plain"
        assert client[0]["name"] == "a"
        # Resolvable back to server + original URI.
        assert reg.resolve(ns_uri) == ("srv", "mock://a")

    def test_collision_across_servers(self):
        reg = ResourceRegistry()
        reg.register_server_resources("a", [{"uri": "mock://x"}])
        reg.register_server_resources("b", [{"uri": "mock://x"}])
        assert len(reg) == 2
        uris = {r["uri"] for r in reg.list_resources_for_client()}
        assert len(uris) == 2  # distinct namespaced URIs

    def test_templates_registered_separately(self):
        reg = ResourceRegistry()
        reg.register_server_templates(
            "srv", [{"uriTemplate": "mock://docs/{name}.txt", "name": "doc"}]
        )
        templates = reg.list_templates_for_client()
        assert len(templates) == 1
        assert is_namespaced_uri(templates[0]["uriTemplate"])

    def test_unregister_removes_resources_and_templates(self):
        reg = ResourceRegistry()
        reg.register_server_resources("srv", [{"uri": "mock://a"}])
        reg.register_server_templates("srv", [{"uriTemplate": "mock://{x}"}])
        reg.unregister_server("srv")
        assert len(reg) == 0
        assert reg.list_templates_for_client() == []

    def test_refresh_replaces(self):
        reg = ResourceRegistry()
        reg.register_server_resources("srv", [{"uri": "mock://a"}, {"uri": "mock://b"}])
        reg.register_server_resources("srv", [{"uri": "mock://c"}])
        assert len(reg) == 1
        uris = [decode_namespaced_uri(r["uri"])[1] for r in reg.list_resources_for_client()]
        assert uris == ["mock://c"]


# -- Gateway integration ------------------------------------------------------


class TestGatewayResources:
    def _config(self, tmp_path: Path, two_full: bool = False) -> Path:
        if two_full:
            servers = f"""\
              - name: full_a
                command: {sys.executable}
                args: ["{FULL_SERVER}"]
              - name: full_b
                command: {sys.executable}
                args: ["{FULL_SERVER}"]"""
        else:
            servers = f"""\
              - name: full
                command: {sys.executable}
                args: ["{FULL_SERVER}"]
              - name: basic
                command: {sys.executable}
                args: ["{BASIC_SERVER}"]"""
        return write_gateway_config(
            tmp_path,
            f"""\
            servers:
{servers}
            logging:
              level: WARNING
              format: text
            """,
        )

    @pytest.mark.asyncio
    async def test_resources_capability_now_advertised(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            caps = gw._handle_initialize({})["capabilities"]
            # full advertises resources; basic does not. Gateway proxies both
            # tools and resources now.
            assert "tools" in caps
            assert "resources" in caps

    @pytest.mark.asyncio
    async def test_list_resources_namespaced(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            resources = await gw.list_resources()
            assert len(resources) == 2  # text + binary from full mock
            for r in resources:
                assert is_namespaced_uri(r["uri"])
            mimes = {r["mimeType"] for r in resources}
            assert "text/plain" in mimes
            assert "image/png" in mimes

    @pytest.mark.asyncio
    async def test_read_text_resource(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            resources = await gw.list_resources()
            text_uri = next(r["uri"] for r in resources if r["mimeType"] == "text/plain")
            result = await gw.read_resource(text_uri)
            content = result["contents"][0]
            assert content["text"] == "Hello from the full mock server."
            # Downstream content carries the ORIGINAL uri, not the namespaced one.
            assert content["uri"] == "mock://docs/readme.txt"

    @pytest.mark.asyncio
    async def test_read_binary_resource(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            resources = await gw.list_resources()
            bin_uri = next(r["uri"] for r in resources if r["mimeType"] == "image/png")
            result = await gw.read_resource(bin_uri)
            content = result["contents"][0]
            assert "blob" in content
            assert content["mimeType"] == "image/png"

    @pytest.mark.asyncio
    async def test_templates_listed(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            templates = await gw.list_resource_templates()
            assert len(templates) == 1
            assert is_namespaced_uri(templates[0]["uriTemplate"])

    @pytest.mark.asyncio
    async def test_read_unknown_resource_raises(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            with pytest.raises(InvalidParamsError):
                await gw.read_resource("file:///not/namespaced")

    @pytest.mark.asyncio
    async def test_read_unregistered_namespaced_uri_raises(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            fake = encode_namespaced_uri("full", "mock://does/not/exist")
            with pytest.raises(ResourceNotFoundError):
                await gw.read_resource(fake)

    @pytest.mark.asyncio
    async def test_collision_two_full_servers(self, tmp_path: Path):
        """Two identical servers expose identical URIs; routing stays correct."""
        async with Gateway(self._config(tmp_path, two_full=True)) as gw:
            resources = await gw.list_resources()
            # 2 resources per server × 2 servers = 4, all distinct namespaced.
            assert len(resources) == 4
            uris = {r["uri"] for r in resources}
            assert len(uris) == 4
            # Each reads correctly from its owning server.
            for r in resources:
                if r["mimeType"] == "text/plain":
                    result = await gw.read_resource(r["uri"])
                    assert result["contents"][0]["text"] == "Hello from the full mock server."

    @pytest.mark.asyncio
    async def test_tools_still_work(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            result = await gw.call_tool("basic__echo", {"text": "hi"})
            assert result["content"][0]["text"] == "hi"
