"""Central capability registry for the MCP aggregation gateway.

Tracks, per downstream server, the facts the gateway needs to negotiate an
honest upstream capability set and to route requests safely:

- advertised capabilities (the ``capabilities`` object from initialize)
- negotiated protocol version
- server info (name/version)
- connection health
- last successful synchronization time

The registry also computes the **aggregate** capability object the gateway
advertises upstream.  That aggregate is the intersection of what downstream
servers advertise and what the gateway can actually proxy — the gateway never
claims a capability it cannot honor (a hard requirement).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Capability keys the gateway is able to proxy.  A downstream capability is
#: only surfaced upstream if it appears here.  As new stages land, entries are
#: added — this set is the single source of truth for "what Baldr can proxy".
#:
#: Stage 1 ships with ``tools`` only (the gateway's existing, verified
#: capability).  Resources/prompts/completions/logging are added in their
#: respective stages once genuinely implemented — never before.
GATEWAY_PROXYABLE_CAPABILITIES: frozenset[str] = frozenset(
    {"tools", "resources", "prompts", "completions", "logging"}
)


@dataclass
class ServerCapabilityRecord:
    """Everything the gateway knows about one downstream server's capabilities."""

    name: str
    protocol_version: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    server_info: dict[str, Any] = field(default_factory=dict)
    healthy: bool = True
    last_sync: float | None = None

    def supports(self, capability: str) -> bool:
        """True if this server advertises *capability* (e.g. ``"resources"``)."""
        return capability in self.capabilities


class CapabilityRegistry:
    """Tracks downstream capabilities and derives the upstream aggregate."""

    def __init__(self) -> None:
        self._records: dict[str, ServerCapabilityRecord] = {}

    # -- Registration / updates ----------------------------------------------

    def register(
        self,
        name: str,
        *,
        protocol_version: str | None,
        capabilities: dict[str, Any],
        server_info: dict[str, Any] | None = None,
    ) -> ServerCapabilityRecord:
        """Record (or refresh) a downstream server's negotiated capabilities."""
        record = ServerCapabilityRecord(
            name=name,
            protocol_version=protocol_version,
            capabilities=dict(capabilities or {}),
            server_info=dict(server_info or {}),
            healthy=True,
            last_sync=time.time(),
        )
        self._records[name] = record
        logger.info(
            "Registered server capabilities",
            extra={
                "server": name,
                "protocolVersion": protocol_version,
                "capabilities": sorted(record.capabilities.keys()),
            },
        )
        return record

    def mark_unhealthy(self, name: str) -> None:
        """Flag a server as unhealthy (e.g. after a crash) without dropping it."""
        record = self._records.get(name)
        if record is not None:
            record.healthy = False

    def mark_healthy(self, name: str) -> None:
        """Flag a server as healthy (e.g. after a successful restart)."""
        record = self._records.get(name)
        if record is not None:
            record.healthy = True
            record.last_sync = time.time()

    def remove(self, name: str) -> None:
        """Remove a server from the registry entirely."""
        self._records.pop(name, None)

    # -- Queries --------------------------------------------------------------

    def get(self, name: str) -> ServerCapabilityRecord | None:
        """Return the record for *name*, or ``None``."""
        return self._records.get(name)

    def servers(self) -> list[str]:
        """Return all tracked server names."""
        return list(self._records)

    def servers_supporting(self, capability: str) -> list[str]:
        """Return healthy servers that advertise *capability*."""
        return [
            name for name, rec in self._records.items() if rec.healthy and rec.supports(capability)
        ]

    def aggregate_capabilities(self) -> dict[str, Any]:
        """Compute the capability object the gateway advertises upstream.

        A capability is advertised only when (a) the gateway can proxy it
        (``GATEWAY_PROXYABLE_CAPABILITIES``) **and** (b) at least one healthy
        downstream server advertises it.  This guarantees the gateway never
        overclaims.

        Sub-features (e.g. ``resources.subscribe``) are surfaced only if at
        least one healthy downstream advertises that sub-feature.
        """
        aggregate: dict[str, Any] = {}
        for capability in GATEWAY_PROXYABLE_CAPABILITIES:
            providers = [
                rec for rec in self._records.values() if rec.healthy and rec.supports(capability)
            ]
            if not providers:
                continue
            aggregate[capability] = self._merge_subfeatures(capability, providers)
        return aggregate

    @staticmethod
    def _merge_subfeatures(
        capability: str, providers: list[ServerCapabilityRecord]
    ) -> dict[str, Any]:
        """Union the boolean sub-features advertised for *capability*.

        For example, if any provider advertises ``resources.subscribe: true``
        the aggregate advertises it too, because the gateway can route a
        subscribe to that provider.  Non-boolean sub-feature values are passed
        through from the first provider that defines them.
        """
        merged: dict[str, Any] = {}
        for rec in providers:
            sub = rec.capabilities.get(capability)
            if not isinstance(sub, dict):
                continue
            for key, value in sub.items():
                if isinstance(value, bool):
                    merged[key] = merged.get(key, False) or value
                else:
                    merged.setdefault(key, value)
        return merged

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, name: str) -> bool:
        return name in self._records
