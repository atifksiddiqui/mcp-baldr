"""Capability-complete mock stdio MCP server (Stage 0 / extended Stage 7).

Unlike ``mock_server.py`` (tools only), this fixture implements a broad slice
of the MCP protocol so the gateway's aggregation of resources, prompts,
completion, logging, subscriptions, and server→client requests can be tested
honestly against a real downstream that actually performs the operations — no
fabricated traces.

Implemented methods:

- ``initialize``                 — advertises tools/resources/prompts/
                                   completions/logging capabilities
- ``tools/list`` / ``tools/call``
- ``resources/list`` / ``resources/read``
- ``resources/templates/list``
- ``resources/subscribe`` / ``resources/unsubscribe``
- ``prompts/list`` / ``prompts/get``
- ``completion/complete``
- ``logging/setLevel``
- ``ping``

Pagination: ``tools/list``, ``resources/list``, and ``prompts/list`` honor an
optional ``cursor`` param and return a ``nextCursor`` when more items remain,
so the gateway's pagination handling can be exercised.

Test affordances (invoked as tool calls, never automatic):

- ``emit_resource_update`` — send a ``notifications/resources/updated``.
- ``emit_list_changed``    — send a ``notifications/tools/list_changed``.
- ``server_request``       — issue a server→client *request*
  (``sampling/createMessage``, ``elicitation/create``, or ``roots/list``) and
  block until the gateway relays the client's response (or error) back.  The
  tool result reports the outcome so sampling/elicitation/roots relaying can
  be tested end to end.

Environment variables:

- ``MOCK_SERVER_NAME`` — overrides ``serverInfo.name`` (default "full-mock").
- ``MOCK_PAGE_SIZE``   — items per page for list methods (default 100).

All output is newline-delimited JSON-RPC 2.0 on stdout; diagnostics go to
stderr so they never corrupt the protocol stream.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

PROTOCOL_VERSION = "2026-07-28"
SERVER_INFO = {
    "name": os.environ.get("MOCK_SERVER_NAME", "full-mock"),
    "version": "0.1.0",
}
PAGE_SIZE = int(os.environ.get("MOCK_PAGE_SIZE", "100"))

# -- Capability catalog --------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "Echo back the provided text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "emit_resource_update",
        "description": "Test affordance: emit a resources/updated notification for a URI.",
        "inputSchema": {
            "type": "object",
            "properties": {"uri": {"type": "string"}},
            "required": ["uri"],
        },
    },
    {
        "name": "emit_list_changed",
        "description": "Test affordance: emit a tools/list_changed notification.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "server_request",
        "description": (
            "Test affordance: issue a server→client request (sampling/createMessage, "
            "elicitation/create, or roots/list) and wait for the relayed response."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["sampling", "elicitation", "roots"],
                }
            },
            "required": ["kind"],
        },
    },
]

# Resources: one text, one binary (to exercise MIME + base64 handling).
_TEXT_URI = "mock://docs/readme.txt"
_BINARY_URI = "mock://images/pixel.png"

RESOURCES: list[dict[str, Any]] = [
    {
        "uri": _TEXT_URI,
        "name": "readme",
        "title": "Project Readme",
        "description": "A short text document.",
        "mimeType": "text/plain",
    },
    {
        "uri": _BINARY_URI,
        "name": "pixel",
        "title": "1x1 PNG",
        "description": "A tiny binary image.",
        "mimeType": "image/png",
    },
]

RESOURCE_TEMPLATES: list[dict[str, Any]] = [
    {
        "uriTemplate": "mock://docs/{name}.txt",
        "name": "doc-by-name",
        "title": "Document by name",
        "description": "Fetch any mock document by name.",
        "mimeType": "text/plain",
    },
]

# A tiny 1x1 transparent PNG, base64-encoded (for the binary resource).
_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

PROMPTS: list[dict[str, Any]] = [
    {
        "name": "greeting",
        "title": "Greeting prompt",
        "description": "Produce a friendly greeting for a given name.",
        "arguments": [
            {"name": "name", "description": "Person to greet.", "required": True},
            {"name": "formal", "description": "Use a formal tone.", "required": False},
        ],
    },
    {
        "name": "summarize",
        "title": "Summarize prompt",
        "description": "Summarize a block of text.",
        "arguments": [
            {"name": "text", "description": "Text to summarize.", "required": True},
        ],
    },
]

CAPABILITIES: dict[str, Any] = {
    "tools": {"listChanged": True},
    "resources": {"subscribe": True, "listChanged": True},
    "prompts": {"listChanged": True},
    "completions": {},
    "logging": {},
}

# Active subscriptions (resource URIs the client has subscribed to).
_subscriptions: set[str] = set()
_log_level: str | None = None

# Server-originated request bookkeeping.  Because this mock is single-threaded
# and reads one stdin line at a time, ``server_request`` emits a request and
# then pumps stdin until the matching response arrives, so the round-trip can
# be asserted synchronously within the tool call.
_server_req_id_seq = 10_000


# -- JSON-RPC helpers ----------------------------------------------------------


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def make_response(req_id: int | str, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id: int | str, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


# -- Pagination helper ---------------------------------------------------------


def _paginate(items: list[dict[str, Any]], cursor: str | None) -> tuple[list[dict], str | None]:
    """Return (page, next_cursor) using a simple integer-offset cursor.

    The cursor is an opaque string from the client's perspective; here it
    encodes a start offset.  An invalid cursor raises ValueError.
    """
    start = 0
    if cursor is not None:
        try:
            start = int(cursor)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid cursor: {cursor!r}") from None
    end = start + PAGE_SIZE
    page = items[start:end]
    next_cursor = str(end) if end < len(items) else None
    return page, next_cursor


# -- Server→client request payloads --------------------------------------------


def _server_request_payload(kind: str) -> tuple[str, dict[str, Any]]:
    """Return ``(method, params)`` for a server→client request of *kind*."""
    if kind == "sampling":
        return "sampling/createMessage", {
            "messages": [{"role": "user", "content": {"type": "text", "text": "Say hi."}}],
            "maxTokens": 16,
        }
    if kind == "elicitation":
        return "elicitation/create", {
            "message": "Please provide a value.",
            "requestedSchema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
            },
        }
    if kind == "roots":
        return "roots/list", {}
    raise ValueError(f"Unknown server-request kind: {kind}")


def _issue_server_request(kind: str) -> dict[str, Any]:
    """Emit a server→client request and pump stdin until its reply arrives.

    Returns a dict describing the outcome for the tool result.  While waiting
    for the reply, any other requests the gateway sends (e.g. ``ping``) are
    handled so the gateway is never blocked.
    """
    global _server_req_id_seq
    method, params = _server_request_payload(kind)
    _server_req_id_seq += 1
    req_id = _server_req_id_seq

    _emit({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})

    # Pump stdin until we see the response to req_id.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        incoming_id = message.get("id")
        incoming_method = message.get("method")

        # Our awaited response: has our id and no method.
        if incoming_id == req_id and incoming_method is None:
            if "error" in message:
                return {"status": "error", "error": message["error"]}
            return {"status": "ok", "result": message.get("result", {})}

        # Any other request from the gateway while we wait — handle it so the
        # gateway's own request doesn't stall.
        if incoming_id is not None and incoming_method is not None:
            handler = _HANDLERS.get(incoming_method)
            if handler is None:
                _emit(make_error(incoming_id, -32601, f"Method not found: {incoming_method}"))
            else:
                _emit(handler(incoming_id, message.get("params", {}) or {}))
        # Notifications are ignored while waiting.

    return {"status": "no_reply"}


# -- Method handlers -----------------------------------------------------------


def handle_initialize(req_id: int | str) -> dict[str, Any]:
    return make_response(
        req_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": CAPABILITIES,
            "serverInfo": SERVER_INFO,
        },
    )


def handle_tools_list(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        page, next_cursor = _paginate(TOOLS, params.get("cursor"))
    except ValueError as exc:
        return make_error(req_id, -32602, str(exc))
    result: dict[str, Any] = {"tools": page}
    if next_cursor is not None:
        result["nextCursor"] = next_cursor
    return make_response(req_id, result)


def handle_tools_call(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments", {}) or {}

    if name == "echo":
        return make_response(
            req_id,
            {"content": [{"type": "text", "text": arguments.get("text", "")}], "isError": False},
        )

    if name == "emit_resource_update":
        uri = arguments.get("uri", _TEXT_URI)
        # Emit the notification first, then acknowledge via the result.
        _emit(notification("notifications/resources/updated", {"uri": uri}))
        return make_response(
            req_id,
            {"content": [{"type": "text", "text": f"emitted update for {uri}"}], "isError": False},
        )

    if name == "emit_list_changed":
        _emit(notification("notifications/tools/list_changed", {}))
        return make_response(
            req_id,
            {"content": [{"type": "text", "text": "emitted tools/list_changed"}], "isError": False},
        )

    if name == "server_request":
        kind = arguments.get("kind", "sampling")
        try:
            outcome = _issue_server_request(kind)
        except ValueError as exc:
            return make_error(req_id, -32602, str(exc))
        return make_response(
            req_id,
            {
                "content": [{"type": "text", "text": json.dumps(outcome)}],
                "isError": outcome.get("status") != "ok",
                "_serverRequest": outcome,  # echoed for test assertions
            },
        )

    return make_error(req_id, -32602, f"Unknown tool: {name}")


def handle_resources_list(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        page, next_cursor = _paginate(RESOURCES, params.get("cursor"))
    except ValueError as exc:
        return make_error(req_id, -32602, str(exc))
    result: dict[str, Any] = {"resources": page}
    if next_cursor is not None:
        result["nextCursor"] = next_cursor
    return make_response(req_id, result)


def handle_resources_templates_list(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    return make_response(req_id, {"resourceTemplates": RESOURCE_TEMPLATES})


def handle_resources_read(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    uri = params.get("uri")
    if uri == _TEXT_URI:
        return make_response(
            req_id,
            {
                "contents": [
                    {
                        "uri": _TEXT_URI,
                        "mimeType": "text/plain",
                        "text": "Hello from the full mock server.",
                    }
                ]
            },
        )
    if uri == _BINARY_URI:
        return make_response(
            req_id,
            {
                "contents": [
                    {
                        "uri": _BINARY_URI,
                        "mimeType": "image/png",
                        "blob": _PNG_B64,
                    }
                ]
            },
        )
    # Template-style: any mock://docs/<name>.txt resolves to synthetic text.
    if isinstance(uri, str) and uri.startswith("mock://docs/") and uri.endswith(".txt"):
        name = uri[len("mock://docs/") : -len(".txt")]
        return make_response(
            req_id,
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/plain",
                        "text": f"Synthetic document: {name}",
                    }
                ]
            },
        )
    return make_error(req_id, -32002, f"Resource not found: {uri}")


def handle_resources_subscribe(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    uri = params.get("uri")
    if not isinstance(uri, str):
        return make_error(req_id, -32602, "subscribe requires a 'uri'")
    _subscriptions.add(uri)
    return make_response(req_id, {})


def handle_resources_unsubscribe(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    uri = params.get("uri")
    _subscriptions.discard(uri)
    return make_response(req_id, {})


def handle_prompts_list(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        page, next_cursor = _paginate(PROMPTS, params.get("cursor"))
    except ValueError as exc:
        return make_error(req_id, -32602, str(exc))
    result: dict[str, Any] = {"prompts": page}
    if next_cursor is not None:
        result["nextCursor"] = next_cursor
    return make_response(req_id, result)


def handle_prompts_get(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments", {}) or {}

    if name == "greeting":
        person = arguments.get("name", "friend")
        formal = arguments.get("formal")
        greeting = f"Good day, {person}." if formal else f"Hey {person}!"
        return make_response(
            req_id,
            {
                "description": "A friendly greeting.",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": greeting},
                    }
                ],
            },
        )

    if name == "summarize":
        text = arguments.get("text", "")
        return make_response(
            req_id,
            {
                "description": "Summarize the given text.",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": f"Summarize: {text}"},
                    }
                ],
            },
        )

    return make_error(req_id, -32602, f"Unknown prompt: {name}")


def handle_completion_complete(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    ref = params.get("ref", {})
    argument = params.get("argument", {})
    arg_value = argument.get("value", "")
    # Offer a couple of synthetic completions that echo the partial value.
    values = [f"{arg_value}{suffix}" for suffix in ("-alpha", "-beta", "-gamma")]
    return make_response(
        req_id,
        {
            "completion": {
                "values": values,
                "total": len(values),
                "hasMore": False,
            },
            "_refType": ref.get("type"),  # echoed for test assertions
        },
    )


def handle_logging_set_level(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    global _log_level
    level = params.get("level")
    _log_level = level
    # Emit a sanitized log notification to prove the pathway works.
    _emit(
        notification(
            "notifications/message",
            {"level": level or "info", "logger": "full-mock", "data": {"set_level": level}},
        )
    )
    return make_response(req_id, {})


def handle_ping(req_id: int | str, params: dict[str, Any]) -> dict[str, Any]:
    return make_response(req_id, {})


# -- Dispatch + main loop ------------------------------------------------------

_HANDLERS = {
    "initialize": lambda rid, p: handle_initialize(rid),
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
    "resources/list": handle_resources_list,
    "resources/templates/list": handle_resources_templates_list,
    "resources/read": handle_resources_read,
    "resources/subscribe": handle_resources_subscribe,
    "resources/unsubscribe": handle_resources_unsubscribe,
    "prompts/list": handle_prompts_list,
    "prompts/get": handle_prompts_get,
    "completion/complete": handle_completion_complete,
    "logging/setLevel": handle_logging_set_level,
    "ping": handle_ping,
}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            sys.stderr.write(f"full_mock_server: invalid JSON: {line[:200]}\n")
            continue

        msg_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params", {}) or {}

        # Notifications have no id and expect no response.
        if msg_id is None:
            continue

        handler = _HANDLERS.get(method)
        if handler is None:
            _emit(make_error(msg_id, -32601, f"Method not found: {method}"))
            continue

        try:
            _emit(handler(msg_id, params))
        except Exception as exc:  # defensive: never crash the fixture
            _emit(make_error(msg_id, -32603, f"Internal error: {exc}"))


if __name__ == "__main__":
    main()
