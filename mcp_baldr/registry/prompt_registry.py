"""In-memory prompt registry for the MCP Gateway.

Aggregates prompts from multiple downstream servers.  Prompts are identified
by name (like tools), so they use the same ``server__prompt`` namespacing via
:class:`~mcp_baldr.registry.base_registry.NamespacedRegistry`.

Each :class:`RegisteredPrompt` stores its owning server and original prompt
name, so routing a ``prompts/get`` back to the correct subprocess is a
registry lookup.  Prompt arguments and descriptions are preserved verbatim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from mcp_baldr.registry.base_registry import NAMESPACE_SEP, NamespacedRegistry

logger = logging.getLogger(__name__)

__all__ = ["RegisteredPrompt", "PromptRegistry"]


@dataclass
class RegisteredPrompt:
    """A prompt registered in the gateway, with its owning server.

    Attributes:
        name: The original prompt name as advertised by the downstream server.
        server: The downstream server name (namespace prefix).
        title: Optional human-readable title.
        description: Optional human-readable description.
        arguments: The prompt's argument definitions (preserved verbatim).
        raw: The full prompt definition dict from the server.
    """

    name: str
    server: str
    title: str | None = None
    description: str | None = None
    arguments: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def namespaced_name(self) -> str:
        """The gateway-facing prompt name: ``server_name__prompt_name``."""
        return f"{self.server}{NAMESPACE_SEP}{self.name}"

    def to_client_dict(self) -> dict[str, Any]:
        """Return the prompt in MCP ``prompts/list`` format.

        The name is namespaced; all other fields (title, description,
        arguments) pass through from the downstream definition unchanged.
        """
        out: dict[str, Any] = dict(self.raw)
        out["name"] = self.namespaced_name
        return out


class PromptRegistry(NamespacedRegistry[RegisteredPrompt]):
    """Stores prompts aggregated from multiple downstream servers.

    Prompts are keyed by their namespaced name (``server__prompt``) to avoid
    collisions when multiple servers define a prompt with the same local name.
    """

    # -- NamespacedRegistry hooks ---------------------------------------------

    def _make_item(self, server_name: str, raw: dict) -> RegisteredPrompt:
        return RegisteredPrompt(
            name=raw.get("name", ""),
            server=server_name,
            title=raw.get("title"),
            description=raw.get("description"),
            arguments=raw.get("arguments", []),
            raw=raw,
        )

    def _namespaced_id(self, item: RegisteredPrompt) -> str:
        return item.namespaced_name

    def _server_of(self, item: RegisteredPrompt) -> str:
        return item.server

    def _original_id(self, item: RegisteredPrompt) -> str:
        return item.name

    # -- Prompt-specific public API -------------------------------------------

    def register_server_prompts(
        self, server_name: str, prompts: list[dict[str, Any]]
    ) -> list[RegisteredPrompt]:
        """Register all prompts advertised by a downstream server."""
        return self.register_server_items(server_name, prompts)

    def list_for_client(self) -> list[dict[str, Any]]:
        """Return all prompts in MCP ``prompts/list`` format (namespaced)."""
        return [p.to_client_dict() for p in self.all_items()]
