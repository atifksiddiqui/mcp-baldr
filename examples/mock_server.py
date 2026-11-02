#!/usr/bin/env python3
"""Mock stdio MCP server for testing the gateway.

Implements a minimal MCP server that communicates over stdin/stdout using
newline-delimited JSON-RPC 2.0.

The tool set is selected via the ``MOCK_TOOLS`` environment variable:

- ``default`` (unset): ``echo`` and ``add``
- ``alt``:           ``greet`` and ``multiply``
- ``enhanced``:      ``echo``, ``add`` plus three educational tools:

    - ``simulate_visual_report`` — multi-modal content array (text + image)
    - ``stateful_counter`` — persistent counter across calls
    - ``trigger_protocol_error`` — structured MCP error payload (isError: true)

The ``serverInfo.name`` can be overridden with ``MOCK_SERVER_NAME``
(defaults to ``"mock-server"``).

Usage (standalone, for manual testing)::

    python examples/mock_server.py
    MOCK_TOOLS=alt python examples/mock_server.py
    MOCK_TOOLS=enhanced python examples/mock_server.py

When spawned by StdioTransport the gateway sends JSON-RPC messages over
this process's stdin and reads responses from stdout.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# -- Protocol constants --------------------------------------------------------

PROTOCOL_VERSION = "2026-07-28"
SERVER_INFO = {
    "name": os.environ.get("MOCK_SERVER_NAME", "mock-server"),
    "version": "0.1.0",
}

# -- Tool definitions ---------------------------------------------------------

DEFAULT_TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the provided text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to echo back."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two integers and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "First addend."},
                "b": {"type": "integer", "description": "Second addend."},
            },
            "required": ["a", "b"],
        },
    },
]

ALT_TOOLS = [
    {
        "name": "greet",
        "description": "Greet someone by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name to greet."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "multiply",
        "description": "Multiply two integers and return the product.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "First factor."},
                "b": {"type": "integer", "description": "Second factor."},
            },
            "required": ["a", "b"],
        },
    },
]

# -- Enhanced tool definitions (Phase 1 incremental) -------------------------
# Educational tools for protocol exploration. Available via MOCK_TOOLS=enhanced.

ENHANCED_TOOLS = [
    {
        "name": "simulate_visual_report",
        "description": (
            "Educational: returns a multi-modal MCP content array with a text "
            "summary block and a base64-encoded image block. Demonstrates how "
            "AI clients parse and render mixed media content from tools/call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dataset_name": {
                    "type": "string",
                    "description": "Name of the dataset to summarize in the text block.",
                },
            },
        },
    },
    {
        "name": "stateful_counter",
        "description": (
            "Educational: maintains a persistent counter across calls using a "
            "state file. Demonstrates how downstream MCP subprocesses preserve "
            "localized state between independent tools/call invocations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "increment": {
                    "type": "integer",
                    "description": "Amount to add to the counter (default: 1).",
                },
            },
        },
    },
    {
        "name": "trigger_protocol_error",
        "description": (
            "Educational: returns a structured MCP error payload (isError: true) "
            "when crash=true. Demonstrates how AI clients should handle graceful "
            "protocol-level exceptions from tools without system crashes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "crash": {
                    "type": "boolean",
                    "description": "If true, return an MCP error payload (isError: true).",
                },
            },
        },
    },
]

_TOOLS_BY_MODE = {
    "default": DEFAULT_TOOLS,
    "alt": ALT_TOOLS,
    "enhanced": DEFAULT_TOOLS + ENHANCED_TOOLS,
}
TOOLS = _TOOLS_BY_MODE.get(os.environ.get("MOCK_TOOLS", "default"), DEFAULT_TOOLS)


# -- JSON-RPC helpers ----------------------------------------------------------


def make_response(req_id: int | str, result: dict) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})


def make_error(req_id: int | str, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


# -- Method handlers ----------------------------------------------------------


def handle_initialize(req_id: int | str) -> str:
    result = {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": SERVER_INFO,
    }
    return make_response(req_id, result)


def handle_tools_list(req_id: int | str) -> str:
    return make_response(req_id, {"tools": TOOLS})


def handle_tools_call(req_id: int | str, params: dict) -> str:
    name = params.get("name")
    arguments = params.get("arguments", {})

    # Default tool set -------------------------------------------------------
    if name == "echo":
        text = arguments.get("text", "")
        return make_response(
            req_id,
            {"content": [{"type": "text", "text": text}], "isError": False},
        )

    if name == "add":
        a = arguments.get("a", 0)
        b = arguments.get("b", 0)
        return make_response(
            req_id,
            {"content": [{"type": "text", "text": str(a + b)}], "isError": False},
        )

    # Alt tool set ----------------------------------------------------------
    if name == "greet":
        person = arguments.get("name", "stranger")
        return make_response(
            req_id,
            {"content": [{"type": "text", "text": f"Hello, {person}!"}], "isError": False},
        )

    if name == "multiply":
        a = arguments.get("a", 0)
        b = arguments.get("b", 0)
        return make_response(
            req_id,
            {"content": [{"type": "text", "text": str(a * b)}], "isError": False},
        )

    # Enhanced tool set -----------------------------------------------------
    if name == "simulate_visual_report":
        dataset = arguments.get("dataset_name", "sample-dataset")
        # Minimal valid 1x1 PNG as base64 placeholder.
        base64_png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        return make_response(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": f"Visual report for '{dataset}': 42 records, 3 categories.",
                    },
                    {
                        "type": "image",
                        "data": base64_png,
                        "mimeType": "image/png",
                    },
                ],
                "isError": False,
            },
        )

    if name == "stateful_counter":
        increment = arguments.get("increment", 1)
        state_file = Path(tempfile.gettempdir()) / "mock_server_counter.json"
        count = 0
        if state_file.exists():
            try:
                count = json.loads(state_file.read_text()).get("count", 0)
            except (json.JSONDecodeError, KeyError):
                count = 0
        count += increment
        state_file.write_text(json.dumps({"count": count}))
        return make_response(
            req_id,
            {
                "content": [{"type": "text", "text": f"Cumulative count: {count}"}],
                "isError": False,
            },
        )

    if name == "trigger_protocol_error":
        should_crash = arguments.get("crash", False)
        if should_crash:
            return make_response(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "Protocol error triggered: simulated failure for educational purposes.",
                        }
                    ],
                    "isError": True,
                },
            )
        return make_response(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": "No error triggered. Set crash=true to simulate a protocol error.",
                    }
                ],
                "isError": False,
            },
        )

    return make_error(req_id, -32601, f"Unknown tool: {name}")


# -- Main loop ----------------------------------------------------------------


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            sys.stderr.write(f"mock_server: invalid JSON: {line[:200]}\n")
            continue

        msg_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params", {})

        # Notifications have no id and expect no response.
        if msg_id is None:
            continue

        if method == "initialize":
            response = handle_initialize(msg_id)
        elif method == "tools/list":
            response = handle_tools_list(msg_id)
        elif method == "tools/call":
            response = handle_tools_call(msg_id, params)
        else:
            response = make_error(msg_id, -32601, f"Method not found: {method}")

        sys.stdout.write(response + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
