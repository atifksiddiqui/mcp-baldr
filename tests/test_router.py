"""Tests for the unified RequestRouter and NamespacedRegistry base (Stage 2)."""

from __future__ import annotations

import pytest

from mcp_baldr.registry.base_registry import NamespacedRegistry
from mcp_baldr.routing.router import MethodNotFoundError, RequestRouter

# -- RequestRouter ------------------------------------------------------------


class TestRequestRouter:
    @pytest.mark.asyncio
    async def test_dispatch_invokes_registered_handler(self):
        router = RequestRouter()

        async def handler(params):
            return {"echoed": params.get("x")}

        router.register("test/method", handler)
        result = await router.dispatch("test/method", {"x": 42})
        assert result == {"echoed": 42}

    @pytest.mark.asyncio
    async def test_unknown_method_raises(self):
        router = RequestRouter()
        with pytest.raises(MethodNotFoundError, match="nope"):
            await router.dispatch("nope", {})

    def test_supports_and_methods(self):
        router = RequestRouter()

        async def h(params):
            return {}

        router.register_many({"a/x": h, "b/y": h})
        assert router.supports("a/x")
        assert router.supports("b/y")
        assert not router.supports("c/z")
        assert set(router.methods()) == {"a/x", "b/y"}

    def test_register_override_warns_but_replaces(self):
        router = RequestRouter()

        async def h1(params):
            return {"v": 1}

        async def h2(params):
            return {"v": 2}

        router.register("m", h1)
        router.register("m", h2)  # override
        assert router._handlers["m"] is h2


# -- NamespacedRegistry base --------------------------------------------------


class _Item:
    def __init__(self, server: str, name: str):
        self.server = server
        self.name = name


class _Reg(NamespacedRegistry[_Item]):
    def _make_item(self, server_name, raw):
        return _Item(server_name, raw["name"])

    def _namespaced_id(self, item):
        return f"{item.server}__{item.name}"

    def _server_of(self, item):
        return item.server

    def _original_id(self, item):
        return item.name


class TestNamespacedRegistryBase:
    def test_register_and_resolve(self):
        reg = _Reg()
        reg.register_server_items("srv", [{"name": "x"}, {"name": "y"}])
        assert len(reg) == 2
        assert reg.resolve("srv__x") == ("srv", "x")
        assert reg.resolve("srv__y") == ("srv", "y")
        assert reg.resolve("srv__missing") is None

    def test_collision_across_servers(self):
        reg = _Reg()
        reg.register_server_items("a", [{"name": "dup"}])
        reg.register_server_items("b", [{"name": "dup"}])
        assert reg.resolve("a__dup") == ("a", "dup")
        assert reg.resolve("b__dup") == ("b", "dup")
        assert len(reg) == 2

    def test_refresh_replaces_server_items(self):
        reg = _Reg()
        reg.register_server_items("srv", [{"name": "x"}, {"name": "y"}])
        reg.register_server_items("srv", [{"name": "z"}])
        assert len(reg) == 1
        assert reg.resolve("srv__z") == ("srv", "z")
        assert reg.resolve("srv__x") is None

    def test_unregister_server(self):
        reg = _Reg()
        reg.register_server_items("a", [{"name": "x"}])
        reg.register_server_items("b", [{"name": "y"}])
        removed = reg.unregister_server("a")
        assert removed == 1
        assert "a" not in reg.servers()
        assert reg.resolve("b__y") == ("b", "y")

    def test_items_for_server_and_clear(self):
        reg = _Reg()
        reg.register_server_items("a", [{"name": "x"}, {"name": "y"}])
        assert len(reg.items_for_server("a")) == 2
        reg.clear()
        assert len(reg) == 0
        assert reg.servers() == set()
