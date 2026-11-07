"""In-memory tool registry for the MCP Gateway.

Aggregates tool schemas from multiple downstream servers, applying a
namespace prefix (``server_name__tool_name``) so that tools with the same
local name on different servers never collide.

Routing a client call back to the correct subprocess is a simple registry
lookup: each :class:`RegisteredTool` stores its owning server name and
original (un-namespaced) tool name, so there is no need to split strings
at request time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Separator between the server namespace prefix and the original tool name.
NAMESPACE_SEP = "__"


@dataclass
class RegisteredTool:
    """A tool registered in the gateway, along with its owning server.

    Attributes:
        name: The original tool name as advertised by the downstream server.
        server: The downstream server name (namespace prefix).
        description: Optional human-readable description.
        input_schema: The tool's JSON Schema for its input parameters.
        raw: The full tool definition dict received from the server, kept
            for reference and forward-compatibility.
    """

    name: str
    server: str
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def namespaced_name(self) -> str:
        """The gateway-facing tool name: ``server_name__tool_name``."""
        return f"{self.server}{NAMESPACE_SEP}{self.name}"

    def to_client_dict(self) -> dict[str, Any]:
        """Return the tool in the format expected by MCP ``tools/list``.

        The name is namespaced; the schema and description are passed through
        unchanged.
        """
        tool_dict: dict[str, Any] = {
            "name": self.namespaced_name,
            "inputSchema": self.input_schema,
        }
        if self.description is not None:
            tool_dict["description"] = self.description
        # Forward any extra fields the downstream server included (annotations,
        # outputSchema, etc.) without overwriting the ones we set above.
        for key, value in self.raw.items():
            if key not in ("name", "inputSchema", "description"):
                tool_dict.setdefault(key, value)
        return tool_dict


class ToolRegistry:
    """Stores tools discovered from multiple downstream servers.

    Tools are keyed by their namespaced name (``server__tool``) to avoid
    collisions when multiple servers define a tool with the same local name.
    """

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        # Track which server names have tools registered, for quick
        # bulk removal and diagnostics.
        self._server_names: set[str] = set()

    # -- Registration ---------------------------------------------------------

    def add(self, tool: RegisteredTool) -> None:
        """Register or replace a single tool."""
        self._tools[tool.namespaced_name] = tool
        self._server_names.add(tool.server)
        logger.info(
            "Registered tool",
            extra={
                "tool": tool.namespaced_name,
                "server": tool.server,
                "local_name": tool.name,
            },
        )

    def register_server_tools(
        self, server_name: str, tools: list[dict[str, Any]]
    ) -> list[RegisteredTool]:
        """Register all tools advertised by a downstream server.

        Existing tools from the same server are replaced (not merged), so
        calling this again after a server's tool list changes performs a
        clean refresh.

        Args:
            server_name: The downstream server's name (namespace prefix).
            tools: The ``tools`` array from that server's ``tools/list``
                response.

        Returns:
            The list of :class:`RegisteredTool` objects that were created.
        """
        # Remove any previously-registered tools for this server first,
        # so a re-discovery doesn't leave stale entries.
        self.unregister_server(server_name)

        registered: list[RegisteredTool] = []
        for tool_def in tools:
            tool = RegisteredTool(
                name=tool_def.get("name", ""),
                server=server_name,
                description=tool_def.get("description"),
                input_schema=tool_def.get("inputSchema", {}),
                raw=tool_def,
            )
            self.add(tool)
            registered.append(tool)

        logger.info(
            "Registered tools from server",
            extra={
                "server": server_name,
                "tool_count": len(registered),
                "tools": [t.namespaced_name for t in registered],
            },
        )
        return registered

    def unregister_server(self, server_name: str) -> int:
        """Remove all tools belonging to a server.

        Returns:
            The number of tools that were removed.
        """
        to_remove = [ns_name for ns_name, tool in self._tools.items() if tool.server == server_name]
        for ns_name in to_remove:
            del self._tools[ns_name]
        if to_remove:
            logger.info(
                "Unregistered server tools",
                extra={"server": server_name, "removed": len(to_remove)},
            )
        # Only drop the server from the tracked set if no tools remain.
        if not any(t.server == server_name for t in self._tools.values()):
            self._server_names.discard(server_name)
        return len(to_remove)

    # -- Lookup & routing ----------------------------------------------------

    def get(self, namespaced_name: str) -> RegisteredTool | None:
        """Look up a tool by its namespaced name."""
        return self._tools.get(namespaced_name)

    def resolve(self, namespaced_name: str) -> tuple[str, str] | None:
        """Resolve a namespaced tool name for routing.

        Returns:
            A ``(server_name, original_tool_name)`` tuple, or ``None`` if
            the tool is not registered.
        """
        tool = self._tools.get(namespaced_name)
        if tool is None:
            return None
        return tool.server, tool.name

    def list_for_client(self) -> list[dict[str, Any]]:
        """Return all tools in the format expected by MCP ``tools/list``.

        Each tool's ``name`` is namespaced; descriptions and schemas are
        passed through from the downstream server.
        """
        return [tool.to_client_dict() for tool in self._tools.values()]

    # -- Introspection -------------------------------------------------------

    def all_tools(self) -> list[RegisteredTool]:
        """Return all registered tools as :class:`RegisteredTool` objects."""
        return list(self._tools.values())

    def tools_for_server(self, server_name: str) -> list[RegisteredTool]:
        """Return all tools registered for a specific server."""
        return [t for t in self._tools.values() if t.server == server_name]

    def servers(self) -> set[str]:
        """Return the set of server names that currently have tools registered."""
        return set(self._server_names)

    def clear(self) -> None:
        """Remove all registered tools."""
        self._tools.clear()
        self._server_names.clear()

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, namespaced_name: str) -> bool:
        return namespaced_name in self._tools
