"""In-memory tool registry for the MCP Gateway.

Aggregates tool schemas from multiple downstream servers, applying a
namespace prefix (``server_name__tool_name``) so that tools with the same
local name on different servers never collide.

Routing a client call back to the correct subprocess is a simple registry
lookup: each :class:`RegisteredTool` stores its owning server name and
original (un-namespaced) tool name, so there is no need to split strings
at request time.

The generic namespacing/resolve/refresh mechanics live in
:class:`~mcp_baldr.registry.base_registry.NamespacedRegistry`; this module
specializes them for tools (JSON-Schema input, client rendering).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from mcp_baldr.registry.base_registry import NAMESPACE_SEP, NamespacedRegistry

logger = logging.getLogger(__name__)

__all__ = ["NAMESPACE_SEP", "RegisteredTool", "ToolRegistry"]


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


class ToolRegistry(NamespacedRegistry[RegisteredTool]):
    """Stores tools discovered from multiple downstream servers.

    Tools are keyed by their namespaced name (``server__tool``) to avoid
    collisions when multiple servers define a tool with the same local name.
    """

    # -- NamespacedRegistry hooks ---------------------------------------------

    def _make_item(self, server_name: str, raw: dict) -> RegisteredTool:
        return RegisteredTool(
            name=raw.get("name", ""),
            server=server_name,
            description=raw.get("description"),
            input_schema=raw.get("inputSchema", {}),
            raw=raw,
        )

    def _namespaced_id(self, item: RegisteredTool) -> str:
        return item.namespaced_name

    def _server_of(self, item: RegisteredTool) -> str:
        return item.server

    def _original_id(self, item: RegisteredTool) -> str:
        return item.name

    # -- Tool-specific public API (preserved for backward compatibility) ------

    def add(self, tool: RegisteredTool) -> None:
        """Register or replace a single tool."""
        super().add(tool)
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
        """
        return self.register_server_items(server_name, tools)

    def list_for_client(self) -> list[dict[str, Any]]:
        """Return all tools in the format expected by MCP ``tools/list``.

        Each tool's ``name`` is namespaced; descriptions and schemas are
        passed through from the downstream server.
        """
        return [tool.to_client_dict() for tool in self.all_items()]

    def all_tools(self) -> list[RegisteredTool]:
        """Return all registered tools as :class:`RegisteredTool` objects."""
        return self.all_items()

    def tools_for_server(self, server_name: str) -> list[RegisteredTool]:
        """Return all tools registered for a specific server."""
        return self.items_for_server(server_name)
