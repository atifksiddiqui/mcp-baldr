"""Unit tests for the multi-server ToolRegistry and namespace routing."""

import pytest

from mcp_gateway.registry.tool_registry import (
    NAMESPACE_SEP,
    RegisteredTool,
    ToolRegistry,
)

# -- Fixtures ------------------------------------------------------------------


def make_tool(name: str, description: str = "", schema: dict | None = None) -> dict:
    """Build a tool definition dict as a downstream server would return it."""
    return {
        "name": name,
        "description": description,
        "inputSchema": schema or {"type": "object", "properties": {}},
    }


@pytest.fixture
def registry():
    return ToolRegistry()


# -- register_server_tools ----------------------------------------------------


class TestRegisterServerTools:
    def test_register_single_server(self, registry):
        tools = [make_tool("echo", "Echo back"), make_tool("add", "Add numbers")]
        registered = registry.register_server_tools("srv_a", tools)

        assert len(registered) == 2
        assert registry.get("srv_a__echo") is not None
        assert registry.get("srv_a__add") is not None

    def test_register_multiple_servers(self, registry):
        registry.register_server_tools("srv_a", [make_tool("echo")])
        registry.register_server_tools("srv_b", [make_tool("echo")])

        assert len(registry) == 2
        assert registry.get("srv_a__echo") is not None
        assert registry.get("srv_b__echo") is not None

    def test_namespace_collision_handled(self, registry):
        """Two servers with the same tool name get distinct namespaced names."""
        registry.register_server_tools("alpha", [make_tool("echo", "Alpha echo")])
        registry.register_server_tools("beta", [make_tool("echo", "Beta echo")])

        alpha_tool = registry.get("alpha__echo")
        beta_tool = registry.get("beta__echo")
        assert alpha_tool is not None
        assert beta_tool is not None
        assert alpha_tool.description == "Alpha echo"
        assert beta_tool.description == "Beta echo"
        assert alpha_tool.server == "alpha"
        assert beta_tool.server == "beta"

    def test_re_register_replaces_tools(self, registry):
        """Re-registering a server replaces its old tools cleanly."""
        registry.register_server_tools("srv", [make_tool("echo"), make_tool("add")])
        assert len(registry) == 2

        # Re-register with different tools
        registry.register_server_tools("srv", [make_tool("greet")])
        assert len(registry) == 1
        assert registry.get("srv__echo") is None
        assert registry.get("srv__add") is None
        assert registry.get("srv__greet") is not None

    def test_empty_tool_list(self, registry):
        registered = registry.register_server_tools("srv", [])
        assert registered == []
        assert len(registry) == 0


# -- resolve (routing) --------------------------------------------------------


class TestResolve:
    def test_resolve_returns_server_and_tool_name(self, registry):
        registry.register_server_tools("srv_a", [make_tool("echo")])
        result = registry.resolve("srv_a__echo")
        assert result == ("srv_a", "echo")

    def test_resolve_unknown_tool_returns_none(self, registry):
        assert registry.resolve("nonexistent__tool") is None

    def test_resolve_empty_registry(self, registry):
        assert registry.resolve("anything") is None

    def test_resolve_disambiguates_same_tool_name(self, registry):
        """Two servers both have 'echo' — resolve returns the right one."""
        registry.register_server_tools("alpha", [make_tool("echo")])
        registry.register_server_tools("beta", [make_tool("echo")])

        assert registry.resolve("alpha__echo") == ("alpha", "echo")
        assert registry.resolve("beta__echo") == ("beta", "echo")


# -- list_for_client ----------------------------------------------------------


class TestListForClient:
    def test_returns_namespaced_names(self, registry):
        registry.register_server_tools("srv", [make_tool("echo", "Echo back")])
        tools = registry.list_for_client()
        assert len(tools) == 1
        assert tools[0]["name"] == "srv__echo"
        assert tools[0]["description"] == "Echo back"
        assert "inputSchema" in tools[0]

    def test_multiple_servers_aggregated(self, registry):
        registry.register_server_tools("a", [make_tool("echo")])
        registry.register_server_tools("b", [make_tool("greet"), make_tool("multiply")])
        tools = registry.list_for_client()
        names = sorted(t["name"] for t in tools)
        assert names == ["a__echo", "b__greet", "b__multiply"]

    def test_passes_through_extra_fields(self, registry):
        tool_def = make_tool("echo")
        tool_def["annotations"] = {"title": "Echo Tool"}
        registry.register_server_tools("srv", [tool_def])
        tools = registry.list_for_client()
        assert tools[0].get("annotations") == {"title": "Echo Tool"}


# -- unregister_server --------------------------------------------------------


class TestUnregisterServer:
    def test_removes_all_server_tools(self, registry):
        registry.register_server_tools("srv_a", [make_tool("echo"), make_tool("add")])
        registry.register_server_tools("srv_b", [make_tool("greet")])

        removed = registry.unregister_server("srv_a")
        assert removed == 2
        assert registry.get("srv_a__echo") is None
        assert registry.get("srv_a__add") is None
        assert registry.get("srv_b__greet") is not None

    def test_unregister_nonexistent_server(self, registry):
        removed = registry.unregister_server("nonexistent")
        assert removed == 0

    def test_server_dropped_from_tracked_set(self, registry):
        registry.register_server_tools("srv", [make_tool("echo")])
        assert "srv" in registry.servers()
        registry.unregister_server("srv")
        assert "srv" not in registry.servers()


# -- Introspection ------------------------------------------------------------


class TestIntrospection:
    def test_servers_returns_set(self, registry):
        registry.register_server_tools("a", [make_tool("x")])
        registry.register_server_tools("b", [make_tool("y")])
        assert registry.servers() == {"a", "b"}

    def test_tools_for_server(self, registry):
        registry.register_server_tools("a", [make_tool("echo"), make_tool("add")])
        registry.register_server_tools("b", [make_tool("greet")])
        a_tools = registry.tools_for_server("a")
        assert len(a_tools) == 2
        assert all(t.server == "a" for t in a_tools)

    def test_contains(self, registry):
        registry.register_server_tools("srv", [make_tool("echo")])
        assert "srv__echo" in registry
        assert "srv__nonexistent" not in registry

    def test_clear(self, registry):
        registry.register_server_tools("a", [make_tool("x")])
        registry.register_server_tools("b", [make_tool("y")])
        registry.clear()
        assert len(registry) == 0
        assert registry.servers() == set()


# -- RegisteredTool dataclass -------------------------------------------------


class TestRegisteredTool:
    def test_namespaced_name(self):
        tool = RegisteredTool(name="echo", server="srv")
        assert tool.namespaced_name == "srv__echo"

    def test_to_client_dict(self):
        tool = RegisteredTool(
            name="echo",
            server="srv",
            description="Echo back",
            input_schema={"type": "object"},
            raw={"name": "echo", "annotations": {"title": "Echo"}},
        )
        d = tool.to_client_dict()
        assert d["name"] == "srv__echo"
        assert d["description"] == "Echo back"
        assert d["inputSchema"] == {"type": "object"}
        assert d["annotations"] == {"title": "Echo"}

    def test_namespace_sep_constant(self):
        assert NAMESPACE_SEP == "__"
