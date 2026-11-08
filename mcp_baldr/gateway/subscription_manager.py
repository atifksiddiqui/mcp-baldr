"""Subscription tracking for resource update notifications (Stage 5).

When the upstream client subscribes to a namespaced resource URI, the gateway
subscribes to the corresponding *original* URI on the owning downstream
server.  This manager tracks those subscriptions so the gateway can:

- **deduplicate**: only send one downstream ``resources/subscribe`` per
  (server, original_uri), even if the client subscribes multiple times;
- **route updates**: when a downstream emits ``resources/updated`` for an
  original URI, find the namespaced URI to forward upstream;
- **clean up**: drop all of a server's subscriptions when it crashes or the
  gateway shuts down.

This is single-client (one upstream connection per gateway process), so
ownership is implicitly the sole client.  The structure is kept explicit so
multi-client support can be layered on later without reshaping callers.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SubscriptionManager:
    """Tracks active resource subscriptions keyed by downstream server."""

    def __init__(self) -> None:
        # server_name -> {original_uri -> namespaced_uri}
        self._by_server: dict[str, dict[str, str]] = {}

    def is_subscribed(self, server: str, original_uri: str) -> bool:
        """True if (server, original_uri) already has an active subscription."""
        return original_uri in self._by_server.get(server, {})

    def add(self, server: str, original_uri: str, namespaced_uri: str) -> bool:
        """Record a subscription.

        Returns:
            ``True`` if this is a new subscription (the caller should send a
            downstream ``resources/subscribe``), ``False`` if it already
            existed (dedup — no downstream call needed).
        """
        server_subs = self._by_server.setdefault(server, {})
        if original_uri in server_subs:
            return False
        server_subs[original_uri] = namespaced_uri
        logger.info(
            "Added subscription",
            extra={"server": server, "uri": original_uri},
        )
        return True

    def remove(self, server: str, original_uri: str) -> bool:
        """Remove a subscription.

        Returns:
            ``True`` if a subscription was removed (the caller should send a
            downstream ``resources/unsubscribe``), ``False`` if none existed.
        """
        server_subs = self._by_server.get(server)
        if not server_subs or original_uri not in server_subs:
            return False
        del server_subs[original_uri]
        if not server_subs:
            del self._by_server[server]
        logger.info(
            "Removed subscription",
            extra={"server": server, "uri": original_uri},
        )
        return True

    def namespaced_for(self, server: str, original_uri: str) -> str | None:
        """Return the namespaced URI for a subscribed (server, original_uri).

        Used when a downstream ``resources/updated`` arrives: the gateway
        translates the original URI back to the namespaced URI the client
        knows.  Returns ``None`` if not subscribed.
        """
        return self._by_server.get(server, {}).get(original_uri)

    def drop_server(self, server: str) -> list[str]:
        """Remove all subscriptions for a server (crash / shutdown cleanup).

        Returns:
            The list of original URIs that were subscribed, for logging.
        """
        server_subs = self._by_server.pop(server, {})
        if server_subs:
            logger.info(
                "Dropped server subscriptions",
                extra={"server": server, "count": len(server_subs)},
            )
        return list(server_subs)

    def clear(self) -> None:
        """Remove all subscriptions."""
        self._by_server.clear()

    def active_count(self) -> int:
        """Total number of active subscriptions across all servers."""
        return sum(len(subs) for subs in self._by_server.values())
