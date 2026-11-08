"""MCP Learning Lab curriculum content (Phase 1: MCP Fundamentals).

This module holds the actual lesson data.  Content is written to match the
real Baldr implementation; source references point to files that exist in
this repository, and capability support status reflects what the gateway
genuinely does today.

Baldr's current protocol surface (verified against
``mcp_baldr/gateway/server.py``):

- ``initialize``      — implemented
- ``tools/list``      — implemented (aggregated + namespaced)
- ``tools/call``      — implemented (routed to downstream server)
- ``notifications/*`` — observed from the client; forwarded lifecycle only
- resources / prompts / sampling / elicitation / completion / logging
  — NOT implemented by the gateway yet (labeled accordingly)
"""

from __future__ import annotations

from mcp_baldr.learning.lessons import (
    DocLink,
    Lesson,
    Level,
    Module,
    SourceRef,
    SupportStatus,
)

# -- Authoritative documentation links ----------------------------------------

_MCP_SPEC = DocLink("MCP Specification", "https://modelcontextprotocol.io/specification")
_MCP_DOCS = DocLink("MCP Documentation", "https://modelcontextprotocol.io")
_MCP_PY_SDK = DocLink("MCP Python SDK", "https://github.com/modelcontextprotocol/python-sdk")
_JSONRPC = DocLink("JSON-RPC 2.0 Specification", "https://www.jsonrpc.org/specification")


# -- MCP Fundamentals module ---------------------------------------------------

_FUNDAMENTALS = Module(
    module_id="fundamentals",
    title="MCP Fundamentals",
    description=(
        "What MCP is, how clients and servers communicate, and how Baldr sits "
        "between them as an aggregating gateway."
    ),
    lessons=(
        Lesson(
            lesson_id="fundamentals.what-is-mcp",
            title="What is MCP?",
            summary="The Model Context Protocol and the roles involved.",
            explanations={
                Level.BEGINNER: (
                    "MCP (Model Context Protocol) is a standard way for AI "
                    "applications to connect to external tools and data. A "
                    "'client' (the AI app) talks to a 'server' (which exposes "
                    "tools, resources, and prompts). Baldr is a gateway that "
                    "sits in the middle and makes several servers look like one."
                ),
                Level.INTERMEDIATE: (
                    "MCP defines a client-host-server architecture over JSON-RPC "
                    "2.0. Servers advertise capabilities (tools, resources, "
                    "prompts) during an initialize handshake. Baldr is itself an "
                    "MCP server to its client, and an MCP client to each "
                    "downstream server it proxies."
                ),
                Level.ADVANCED: (
                    "MCP is a JSON-RPC 2.0 protocol with a capability-negotiation "
                    "handshake (initialize → initialized). Baldr implements the "
                    "server role toward the AI client and the client role toward "
                    "each downstream subprocess, aggregating their tool schemas "
                    "under a namespace prefix. See Gateway._handle_request for the "
                    "server-side dispatch and StdioTransport for the client side."
                ),
            },
            support=SupportStatus.IMPLEMENTED,
            source_refs=(
                SourceRef("mcp_baldr/gateway/server.py", "Gateway", "server + client roles"),
                SourceRef("mcp_baldr/transports/stdio.py", "StdioTransport", "downstream client"),
            ),
            doc_links=(_MCP_SPEC, _MCP_DOCS),
        ),
        Lesson(
            lesson_id="fundamentals.json-rpc",
            title="JSON-RPC in MCP",
            summary="How requests, responses, and notifications are framed.",
            explanations={
                Level.BEGINNER: (
                    "Every MCP message is a small JSON object. A request asks for "
                    "something and has an 'id'; a response answers that id. A "
                    "'notification' is a one-way message with no id and no reply."
                ),
                Level.INTERMEDIATE: (
                    "MCP uses JSON-RPC 2.0 over newline-delimited messages on "
                    "stdio. Requests carry jsonrpc, id, method, params. Responses "
                    "carry the matching id plus result or error. Notifications "
                    "omit id. Baldr correlates responses to requests by id."
                ),
                Level.ADVANCED: (
                    "Framing is one JSON object per line on stdin/stdout. Baldr's "
                    "StdioTransport assigns monotonic integer ids, stores a future "
                    "per in-flight id, and resolves it when a response with that id "
                    "arrives. Error objects follow JSON-RPC codes (-32700 parse, "
                    "-32601 method not found, -32602 invalid params, -32603 internal)."
                ),
            },
            support=SupportStatus.IMPLEMENTED,
            protocol_example=('{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}'),
            source_refs=(
                SourceRef("mcp_baldr/transports/stdio.py", "StdioTransport.send_request"),
                SourceRef("mcp_baldr/gateway/server.py", "Gateway._send_response"),
            ),
            doc_links=(_JSONRPC, _MCP_SPEC),
        ),
        Lesson(
            lesson_id="fundamentals.initialization",
            title="Initialization & Capability Negotiation",
            summary="The initialize handshake that starts every MCP session.",
            explanations={
                Level.BEGINNER: (
                    "Before doing anything, the client and server introduce "
                    "themselves. The client sends 'initialize'; the server replies "
                    "with its name, version, and what it can do. Then the client "
                    "says 'initialized' and real work begins."
                ),
                Level.INTERMEDIATE: (
                    "The handshake is: client → initialize (with protocolVersion, "
                    "capabilities, clientInfo); server → result (protocolVersion, "
                    "capabilities, serverInfo); client → notifications/initialized. "
                    "Baldr performs this handshake with each downstream server at "
                    "startup, then advertises an aggregated capability set upstream."
                ),
                Level.ADVANCED: (
                    "Baldr runs the downstream handshake in "
                    "StdioTransport.initialize (sends initialize, then the "
                    "notifications/initialized notification). Toward its own client "
                    "it answers initialize in Gateway._handle_initialize with a "
                    "tools capability. Protocol version is sourced from the MCP "
                    "SDK's LATEST_PROTOCOL_VERSION."
                ),
            },
            support=SupportStatus.IMPLEMENTED,
            protocol_example=(
                '{"jsonrpc": "2.0", "id": 1, "method": "initialize", '
                '"params": {"protocolVersion": "2026-07-28", "capabilities": {}, '
                '"clientInfo": {"name": "client", "version": "1.0"}}}'
            ),
            source_refs=(
                SourceRef("mcp_baldr/transports/stdio.py", "StdioTransport.initialize"),
                SourceRef("mcp_baldr/gateway/server.py", "Gateway._handle_initialize"),
            ),
            doc_links=(_MCP_SPEC, _MCP_PY_SDK),
        ),
        Lesson(
            lesson_id="fundamentals.aggregation",
            title="How Baldr Aggregates & Routes",
            summary="Namespacing and request routing across many servers.",
            explanations={
                Level.BEGINNER: (
                    "Baldr connects to several MCP servers at once. If two servers "
                    "both have a tool called 'search', Baldr keeps them apart by "
                    "adding the server's name as a prefix, like 'a__search' and "
                    "'b__search'. When you call one, Baldr sends it to the right "
                    "server."
                ),
                Level.INTERMEDIATE: (
                    "Each downstream tool is registered under 'server__tool'. On "
                    "tools/call, Baldr splits the namespace, looks up the owning "
                    "server in its registry, and forwards the call with the "
                    "original (un-prefixed) tool name. Collisions are impossible "
                    "because the prefix is unique per server."
                ),
                Level.ADVANCED: (
                    "ToolRegistry.resolve maps a namespaced name to "
                    "(server_name, original_name) via a dict lookup — no string "
                    "splitting at call time, so server names may contain "
                    "underscores. Gateway.call_tool resolves, selects the "
                    "StdioTransport, forwards tools/call, and relays the result. "
                    "Unknown tools raise ToolNotFoundError (→ JSON-RPC -32602)."
                ),
            },
            support=SupportStatus.IMPLEMENTED,
            source_refs=(
                SourceRef("mcp_baldr/registry/tool_registry.py", "ToolRegistry.resolve"),
                SourceRef("mcp_baldr/gateway/server.py", "Gateway.call_tool"),
            ),
            doc_links=(_MCP_SPEC,),
        ),
    ),
)


# -- Capability support matrix (honest labeling) ------------------------------
# Only capabilities Baldr actually handles are marked IMPLEMENTED.  Everything
# else is explicitly NOT_IMPLEMENTED so the Learning Lab never overclaims.

CAPABILITY_SUPPORT: dict[str, SupportStatus] = {
    "initialize": SupportStatus.IMPLEMENTED,
    "ping": SupportStatus.IMPLEMENTED,
    # Tools (Phase 1 / Stage 2): aggregated + namespaced + routed.
    "tools/list": SupportStatus.IMPLEMENTED,
    "tools/call": SupportStatus.IMPLEMENTED,
    "notifications/initialized": SupportStatus.IMPLEMENTED,
    # Resources (Stage 3) + subscriptions (Stage 5): aggregated with reversible
    # namespaced URIs, read routed to the owning server, subscribe/unsubscribe
    # deduplicated and cleaned up on crash.
    "resources/list": SupportStatus.IMPLEMENTED,
    "resources/read": SupportStatus.IMPLEMENTED,
    "resources/templates/list": SupportStatus.IMPLEMENTED,
    "resources/subscribe": SupportStatus.IMPLEMENTED,
    "resources/unsubscribe": SupportStatus.IMPLEMENTED,
    # Prompts (Stage 4): aggregated + namespaced + routed.
    "prompts/list": SupportStatus.IMPLEMENTED,
    "prompts/get": SupportStatus.IMPLEMENTED,
    # Completion + logging (Stage 6).
    "completion/complete": SupportStatus.IMPLEMENTED,
    "logging/setLevel": SupportStatus.IMPLEMENTED,
    # Server→client requests (Stage 7): the gateway does not *provide* these;
    # it RELAYS them to the upstream client, gated on the client's advertised
    # capability, and never auto-approves.  Marked PARTIAL to reflect that the
    # gateway is a conduit, not a sampling/elicitation/roots provider.
    "sampling/createMessage": SupportStatus.PARTIAL,
    "elicitation/create": SupportStatus.PARTIAL,
    "roots/list": SupportStatus.PARTIAL,
    # Notifications: tools/resources/prompts list_changed refresh+forward,
    # resources/updated translate+forward (if subscribed), message redact+
    # forward, progress/cancelled verbatim passthrough.  PARTIAL because
    # notifications the gateway does not recognize are intentionally dropped
    # rather than blindly forwarded.
    "notifications/*": SupportStatus.PARTIAL,
}


# -- Public accessors ----------------------------------------------------------

MODULES: tuple[Module, ...] = (_FUNDAMENTALS,)


def all_modules() -> tuple[Module, ...]:
    """Return all curriculum modules."""
    return MODULES


def all_lessons() -> list[Lesson]:
    """Return every lesson across all modules, in order."""
    return [lesson for module in MODULES for lesson in module.lessons]


def get_lesson(lesson_id: str) -> Lesson | None:
    """Return a lesson by id, or ``None`` if not found."""
    for lesson in all_lessons():
        if lesson.lesson_id == lesson_id:
            return lesson
    return None


def capability_status(method: str) -> SupportStatus:
    """Return Baldr's implementation status for a JSON-RPC *method*.

    Falls back to ``NOT_IMPLEMENTED`` for anything not explicitly listed, so
    the Learning Lab never implies support that does not exist.
    """
    return CAPABILITY_SUPPORT.get(method, SupportStatus.NOT_IMPLEMENTED)
