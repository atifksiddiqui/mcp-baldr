"""Generic namespaced registry mechanics shared by capability registries.

Tools, resources, and prompts all share the same aggregation pattern:

- items are discovered from multiple downstream servers,
- each item is exposed upstream under a namespaced identifier,
- a client-facing identifier resolves back to ``(server, original_id)``
  for routing,
- a server's items can be refreshed or removed as a unit.

:class:`NamespacedRegistry` factors out that mechanic so the tool, resource,
and prompt registries only specialize the parts that differ (how an item's
identity is derived and how it is rendered for the client).

The namespacing uses the same ``server__name`` separator the gateway has
always used for tools, so existing behavior is preserved exactly.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Separator between the server namespace prefix and the original identifier.
NAMESPACE_SEP = "__"


class NamespacedRegistry[T]:
    """Base class for registries that aggregate namespaced items per server.

    Subclasses implement :meth:`_make_item` (build a stored item from a raw
    downstream definition), :meth:`_namespaced_id` (the client-facing id for
    a stored item), :meth:`_server_of` (which server owns a stored item), and
    :meth:`_original_id` (the un-namespaced id to send downstream).
    """

    def __init__(self) -> None:
        self._items: dict[str, T] = {}
        self._server_names: set[str] = set()

    # -- Hooks subclasses implement -------------------------------------------

    def _make_item(self, server_name: str, raw: dict) -> T:
        raise NotImplementedError

    def _namespaced_id(self, item: T) -> str:
        raise NotImplementedError

    def _server_of(self, item: T) -> str:
        raise NotImplementedError

    def _original_id(self, item: T) -> str:
        raise NotImplementedError

    # -- Registration ---------------------------------------------------------

    def add(self, item: T) -> None:
        """Register or replace a single item."""
        self._items[self._namespaced_id(item)] = item
        self._server_names.add(self._server_of(item))

    def register_server_items(self, server_name: str, raw_items: list[dict]) -> list[T]:
        """Register all items advertised by a downstream server.

        Existing items from the same server are replaced (clean refresh).

        Returns:
            The list of stored items created.
        """
        self.unregister_server(server_name)
        created: list[T] = []
        for raw in raw_items:
            item = self._make_item(server_name, raw)
            self.add(item)
            created.append(item)
        logger.info(
            "Registered items from server",
            extra={
                "registry": type(self).__name__,
                "server": server_name,
                "count": len(created),
            },
        )
        return created

    def unregister_server(self, server_name: str) -> int:
        """Remove all items belonging to a server. Returns the count removed."""
        to_remove = [
            key for key, item in self._items.items() if self._server_of(item) == server_name
        ]
        for key in to_remove:
            del self._items[key]
        if not any(self._server_of(i) == server_name for i in self._items.values()):
            self._server_names.discard(server_name)
        return len(to_remove)

    # -- Lookup & routing -----------------------------------------------------

    def get(self, namespaced_id: str) -> T | None:
        """Return the stored item for a namespaced id, or ``None``."""
        return self._items.get(namespaced_id)

    def resolve(self, namespaced_id: str) -> tuple[str, str] | None:
        """Resolve a namespaced id to ``(server_name, original_id)``.

        Returns ``None`` if the id is not registered.
        """
        item = self._items.get(namespaced_id)
        if item is None:
            return None
        return self._server_of(item), self._original_id(item)

    # -- Introspection --------------------------------------------------------

    def all_items(self) -> list[T]:
        """Return all stored items."""
        return list(self._items.values())

    def items_for_server(self, server_name: str) -> list[T]:
        """Return all stored items owned by a server."""
        return [i for i in self._items.values() if self._server_of(i) == server_name]

    def servers(self) -> set[str]:
        """Return the set of server names with items registered."""
        return set(self._server_names)

    def clear(self) -> None:
        """Remove all items."""
        self._items.clear()
        self._server_names.clear()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, namespaced_id: str) -> bool:
        return namespaced_id in self._items
