"""In-memory resource registry for the MCP Gateway.

Aggregates resources from multiple downstream servers.  Unlike tools (whose
names are prefixed with ``server__``), resources are identified by URIs, so
the gateway namespaces them with a reversible, collision-safe scheme (see
:mod:`mcp_baldr.protocol.uri`) rather than mangling the URI string.

Each :class:`RegisteredResource` stores its owning server and original URI,
so routing a ``resources/read`` back to the correct subprocess is a registry
lookup followed by URI decoding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from mcp_baldr.protocol.uri import encode_namespaced_uri
from mcp_baldr.registry.base_registry import NamespacedRegistry

logger = logging.getLogger(__name__)

__all__ = ["RegisteredResource", "RegisteredResourceTemplate", "ResourceRegistry"]


@dataclass
class RegisteredResource:
    """A resource registered in the gateway, with its owning server."""

    uri: str
    server: str
    name: str | None = None
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def namespaced_uri(self) -> str:
        """The gateway-facing URI (reversible, collision-safe)."""
        return encode_namespaced_uri(self.server, self.uri)

    def to_client_dict(self) -> dict[str, Any]:
        """Return the resource in MCP ``resources/list`` format.

        The URI is the namespaced one; all other fields pass through from the
        downstream definition unchanged.
        """
        out: dict[str, Any] = dict(self.raw)
        out["uri"] = self.namespaced_uri
        return out


@dataclass
class RegisteredResourceTemplate:
    """A resource template registered in the gateway, with its owning server."""

    uri_template: str
    server: str
    name: str | None = None
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def namespaced_uri_template(self) -> str:
        """The gateway-facing URI template (reversible, collision-safe)."""
        return encode_namespaced_uri(self.server, self.uri_template)

    def to_client_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.raw)
        out["uriTemplate"] = self.namespaced_uri_template
        return out


class ResourceRegistry(NamespacedRegistry[RegisteredResource]):
    """Stores resources aggregated from multiple downstream servers.

    Resources are keyed by their namespaced URI.  Templates are tracked
    separately (per server) since they are listed via a distinct method.
    """

    def __init__(self) -> None:
        super().__init__()
        # Templates are a parallel collection keyed by namespaced template.
        self._templates: dict[str, RegisteredResourceTemplate] = {}

    # -- NamespacedRegistry hooks (resources) ---------------------------------

    def _make_item(self, server_name: str, raw: dict) -> RegisteredResource:
        return RegisteredResource(
            uri=raw.get("uri", ""),
            server=server_name,
            name=raw.get("name"),
            title=raw.get("title"),
            description=raw.get("description"),
            mime_type=raw.get("mimeType"),
            raw=raw,
        )

    def _namespaced_id(self, item: RegisteredResource) -> str:
        return item.namespaced_uri

    def _server_of(self, item: RegisteredResource) -> str:
        return item.server

    def _original_id(self, item: RegisteredResource) -> str:
        return item.uri

    # -- Resource-specific public API -----------------------------------------

    def register_server_resources(
        self, server_name: str, resources: list[dict[str, Any]]
    ) -> list[RegisteredResource]:
        """Register all resources advertised by a downstream server."""
        return self.register_server_items(server_name, resources)

    def register_server_templates(
        self, server_name: str, templates: list[dict[str, Any]]
    ) -> list[RegisteredResourceTemplate]:
        """Register all resource templates advertised by a downstream server."""
        # Drop existing templates for this server first (clean refresh).
        to_remove = [key for key, tpl in self._templates.items() if tpl.server == server_name]
        for key in to_remove:
            del self._templates[key]

        created: list[RegisteredResourceTemplate] = []
        for raw in templates:
            tpl = RegisteredResourceTemplate(
                uri_template=raw.get("uriTemplate", ""),
                server=server_name,
                name=raw.get("name"),
                title=raw.get("title"),
                description=raw.get("description"),
                mime_type=raw.get("mimeType"),
                raw=raw,
            )
            self._templates[tpl.namespaced_uri_template] = tpl
            created.append(tpl)
        return created

    def unregister_server(self, server_name: str) -> int:
        """Remove all resources and templates belonging to a server."""
        removed = super().unregister_server(server_name)
        tpl_keys = [key for key, tpl in self._templates.items() if tpl.server == server_name]
        for key in tpl_keys:
            del self._templates[key]
        return removed

    def list_resources_for_client(self) -> list[dict[str, Any]]:
        """Return all resources in MCP ``resources/list`` format (namespaced)."""
        return [r.to_client_dict() for r in self.all_items()]

    def list_templates_for_client(self) -> list[dict[str, Any]]:
        """Return all templates in ``resources/templates/list`` format."""
        return [t.to_client_dict() for t in self._templates.values()]

    def clear(self) -> None:
        """Remove all resources and templates."""
        super().clear()
        self._templates.clear()
