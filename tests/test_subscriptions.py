"""Tests for notifications + subscriptions (Stage 5)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp_baldr.gateway.server import Gateway, ResourceNotFoundError
from mcp_baldr.gateway.server import Gateway as GatewayClass
from mcp_baldr.gateway.subscription_manager import SubscriptionManager
from mcp_baldr.protocol.uri import decode_namespaced_uri
from tests.conftest import write_gateway_config

ROOT = Path(__file__).resolve().parent.parent
FULL_SERVER = str(ROOT / "examples" / "full_mock_server.py")


# -- SubscriptionManager ------------------------------------------------------


class TestSubscriptionManager:
    def test_add_dedup(self):
        mgr = SubscriptionManager()
        assert mgr.add("srv", "mock://x", "baldr+srv://x") is True  # new
        assert mgr.add("srv", "mock://x", "baldr+srv://x") is False  # dup
        assert mgr.active_count() == 1

    def test_remove(self):
        mgr = SubscriptionManager()
        mgr.add("srv", "mock://x", "ns")
        assert mgr.remove("srv", "mock://x") is True
        assert mgr.remove("srv", "mock://x") is False  # already gone
        assert mgr.active_count() == 0

    def test_namespaced_for(self):
        mgr = SubscriptionManager()
        mgr.add("srv", "mock://x", "baldr+srv://encoded")
        assert mgr.namespaced_for("srv", "mock://x") == "baldr+srv://encoded"
        assert mgr.namespaced_for("srv", "mock://y") is None
        assert mgr.namespaced_for("other", "mock://x") is None

    def test_drop_server(self):
        mgr = SubscriptionManager()
        mgr.add("a", "mock://1", "ns1")
        mgr.add("a", "mock://2", "ns2")
        mgr.add("b", "mock://3", "ns3")
        dropped = mgr.drop_server("a")
        assert set(dropped) == {"mock://1", "mock://2"}
        assert mgr.active_count() == 1
        assert mgr.namespaced_for("b", "mock://3") == "ns3"

    def test_isolation_between_servers(self):
        mgr = SubscriptionManager()
        mgr.add("a", "mock://x", "nsa")
        mgr.add("b", "mock://x", "nsb")  # same original uri, different server
        assert mgr.active_count() == 2
        assert mgr.namespaced_for("a", "mock://x") == "nsa"
        assert mgr.namespaced_for("b", "mock://x") == "nsb"


# -- Gateway integration ------------------------------------------------------


class TestGatewaySubscriptions:
    def _config(self, tmp_path: Path) -> Path:
        return write_gateway_config(
            tmp_path,
            f"""\
            servers:
              - name: full
                command: {sys.executable}
                args: ["{FULL_SERVER}"]
            logging:
              level: WARNING
              format: text
            """,
        )

    @pytest.mark.asyncio
    async def test_subscribe_capability_advertised(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            caps = gw._handle_initialize({})["capabilities"]
            # full mock advertises resources.subscribe: true → surfaced upstream.
            assert caps["resources"].get("subscribe") is True

    @pytest.mark.asyncio
    async def test_subscribe_dedup(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            resources = await gw.list_resources()
            uri = resources[0]["uri"]
            server, original = decode_namespaced_uri(uri)

            await gw.subscribe_resource(uri)
            assert gw.subscriptions.is_subscribed(server, original)
            # Second subscribe is a dedup no-op at the manager level.
            await gw.subscribe_resource(uri)
            assert gw.subscriptions.active_count() == 1

    @pytest.mark.asyncio
    async def test_unsubscribe(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            resources = await gw.list_resources()
            uri = resources[0]["uri"]
            await gw.subscribe_resource(uri)
            await gw.unsubscribe_resource(uri)
            assert gw.subscriptions.active_count() == 0

    @pytest.mark.asyncio
    async def test_subscribe_unknown_resource_raises(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            from mcp_baldr.protocol.uri import encode_namespaced_uri

            fake = encode_namespaced_uri("full", "mock://nope")
            with pytest.raises(ResourceNotFoundError):
                await gw.subscribe_resource(fake)

    @pytest.mark.asyncio
    async def test_resource_update_forwarded_when_subscribed(self, tmp_path: Path, monkeypatch):
        """A downstream resources/updated is translated + forwarded upstream."""
        forwarded: list[tuple[str, dict]] = []

        def capture(method, params):
            forwarded.append((method, params))

        monkeypatch.setattr(GatewayClass, "_send_notification", staticmethod(capture))

        async with Gateway(self._config(tmp_path)) as gw:
            resources = await gw.list_resources()
            text_uri = next(r["uri"] for r in resources if r["mimeType"] == "text/plain")
            _, original = decode_namespaced_uri(text_uri)

            # Subscribe, then trigger the mock to emit resources/updated.
            await gw.subscribe_resource(text_uri)
            await gw.call_tool("full__emit_resource_update", {"uri": original})

            # Give the async notification callback a moment to run.
            for _ in range(50):
                if any(m == "notifications/resources/updated" for m, _ in forwarded):
                    break
                await asyncio.sleep(0.05)

            updates = [p for m, p in forwarded if m == "notifications/resources/updated"]
            assert updates, "expected a forwarded resources/updated notification"
            # The forwarded URI is the NAMESPACED one, not the original.
            assert updates[0]["uri"] == text_uri

    @pytest.mark.asyncio
    async def test_resource_update_dropped_when_not_subscribed(self, tmp_path: Path, monkeypatch):
        """An update for an unsubscribed URI is NOT forwarded upstream."""
        forwarded: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            GatewayClass, "_send_notification", staticmethod(lambda m, p: forwarded.append((m, p)))
        )

        async with Gateway(self._config(tmp_path)) as gw:
            resources = await gw.list_resources()
            text_uri = next(r["uri"] for r in resources if r["mimeType"] == "text/plain")
            _, original = decode_namespaced_uri(text_uri)

            # No subscribe — just trigger the update.
            await gw.call_tool("full__emit_resource_update", {"uri": original})
            await asyncio.sleep(0.3)

            updates = [p for m, p in forwarded if m == "notifications/resources/updated"]
            assert not updates, "update should be dropped when not subscribed"

    @pytest.mark.asyncio
    async def test_list_changed_forwarded_and_refreshes(self, tmp_path: Path, monkeypatch):
        """A downstream tools/list_changed triggers refresh + upstream forward."""
        forwarded: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            GatewayClass, "_send_notification", staticmethod(lambda m, p: forwarded.append((m, p)))
        )

        async with Gateway(self._config(tmp_path)) as gw:
            await gw.call_tool("full__emit_list_changed", {})
            for _ in range(50):
                if any(m == "notifications/tools/list_changed" for m, _ in forwarded):
                    break
                await asyncio.sleep(0.05)
            assert any(m == "notifications/tools/list_changed" for m, _ in forwarded)
