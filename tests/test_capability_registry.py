"""Tests for capability negotiation and the CapabilityRegistry (Stage 1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_baldr.gateway.server import Gateway
from mcp_baldr.registry.capability_registry import (
    GATEWAY_PROXYABLE_CAPABILITIES,
    CapabilityRegistry,
)
from tests.conftest import write_gateway_config

ROOT = Path(__file__).resolve().parent.parent
FULL_SERVER = str(ROOT / "examples" / "full_mock_server.py")
BASIC_SERVER = str(ROOT / "examples" / "mock_server.py")


# -- Unit tests: CapabilityRegistry -------------------------------------------


class TestCapabilityRegistry:
    def test_register_and_get(self):
        reg = CapabilityRegistry()
        reg.register(
            "srv",
            protocol_version="2026-07-28",
            capabilities={"tools": {}, "resources": {"subscribe": True}},
            server_info={"name": "srv", "version": "1.0"},
        )
        rec = reg.get("srv")
        assert rec is not None
        assert rec.protocol_version == "2026-07-28"
        assert rec.supports("tools")
        assert rec.supports("resources")
        assert not rec.supports("prompts")
        assert rec.last_sync is not None

    def test_servers_supporting(self):
        reg = CapabilityRegistry()
        reg.register("a", protocol_version="x", capabilities={"tools": {}})
        reg.register("b", protocol_version="x", capabilities={"tools": {}, "prompts": {}})
        assert set(reg.servers_supporting("tools")) == {"a", "b"}
        assert reg.servers_supporting("prompts") == ["b"]
        assert reg.servers_supporting("resources") == []

    def test_unhealthy_excluded_from_supporting(self):
        reg = CapabilityRegistry()
        reg.register("a", protocol_version="x", capabilities={"tools": {}})
        reg.mark_unhealthy("a")
        assert reg.servers_supporting("tools") == []
        reg.mark_healthy("a")
        assert reg.servers_supporting("tools") == ["a"]

    def test_aggregate_only_proxyable(self):
        reg = CapabilityRegistry()
        # Server advertises many capabilities...
        reg.register(
            "srv",
            protocol_version="x",
            capabilities={
                "tools": {"listChanged": True},
                "resources": {"subscribe": True},
                "prompts": {},
                "logging": {},
            },
        )
        advertised = {"tools", "resources", "prompts", "logging"}
        agg = reg.aggregate_capabilities()
        # The aggregate is the intersection of what the server advertises and
        # what the gateway can proxy.  The server here does NOT advertise
        # completions, so even though completions is proxyable it must not
        # appear; conversely anything advertised-but-not-proxyable is excluded.
        assert set(agg.keys()) == advertised & set(GATEWAY_PROXYABLE_CAPABILITIES)
        assert "tools" in agg
        assert "completions" not in agg  # not advertised by this server

    def test_aggregate_empty_when_no_servers(self):
        reg = CapabilityRegistry()
        assert reg.aggregate_capabilities() == {}

    def test_aggregate_merges_boolean_subfeatures(self):
        reg = CapabilityRegistry()
        reg.register("a", protocol_version="x", capabilities={"tools": {"listChanged": False}})
        reg.register("b", protocol_version="x", capabilities={"tools": {"listChanged": True}})
        agg = reg.aggregate_capabilities()
        # OR of the two providers' listChanged flags.
        assert agg["tools"]["listChanged"] is True

    def test_aggregate_excludes_unhealthy(self):
        reg = CapabilityRegistry()
        reg.register("a", protocol_version="x", capabilities={"tools": {}})
        reg.mark_unhealthy("a")
        assert reg.aggregate_capabilities() == {}

    def test_remove(self):
        reg = CapabilityRegistry()
        reg.register("a", protocol_version="x", capabilities={"tools": {}})
        assert "a" in reg
        reg.remove("a")
        assert "a" not in reg
        assert len(reg) == 0


# -- Integration: negotiation through the gateway -----------------------------


class TestGatewayNegotiation:
    def _config(self, tmp_path: Path) -> Path:
        return write_gateway_config(
            tmp_path,
            f"""\
            servers:
              - name: full
                command: {sys.executable}
                args: ["{FULL_SERVER}"]
              - name: basic
                command: {sys.executable}
                args: ["{BASIC_SERVER}"]
            logging:
              level: WARNING
              format: text
            """,
        )

    @pytest.mark.asyncio
    async def test_registry_captures_downstream_capabilities(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            full = gw.capabilities.get("full")
            basic = gw.capabilities.get("basic")
            assert full is not None and basic is not None
            # The full mock advertises the broad set.
            assert full.supports("resources")
            assert full.supports("prompts")
            assert full.supports("completions")
            assert full.supports("logging")
            # The basic mock advertises tools only.
            assert basic.supports("tools")
            assert not basic.supports("resources")
            # Protocol version captured.
            assert full.protocol_version == "2026-07-28"

    @pytest.mark.asyncio
    async def test_gateway_advertises_honest_aggregate(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            init = gw._handle_initialize({})
            caps = init["capabilities"]
            # The gateway advertises only capabilities it can proxy.  As of
            # Stage 6 that is tools + resources + prompts + completions +
            # logging (the full mock advertises all of them and the gateway
            # now proxies each one honestly).
            assert "tools" in caps
            assert "resources" in caps
            assert "prompts" in caps
            assert "completions" in caps
            assert "logging" in caps
            assert init["serverInfo"]["name"] == "mcp-baldr"
            assert init["protocolVersion"]

    @pytest.mark.asyncio
    async def test_tools_still_work_after_negotiation(self, tmp_path: Path):
        """Stage 1 must not regress tool routing."""
        async with Gateway(self._config(tmp_path)) as gw:
            result = await gw.call_tool("basic__echo", {"text": "hi"})
            assert result["content"][0]["text"] == "hi"
