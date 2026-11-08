#!/usr/bin/env python3
"""Streamable HTTP mock MCP server (MCP 2026-07-28), for honest HTTP tests.

The HTTP analogue of ``full_mock_server.py``: a real MCP server that speaks the
``2026-07-28`` Streamable HTTP binding (single ``POST`` MCP endpoint at
``/mcp``, per-request JSON or SSE responses) so :class:`HttpTransport` can be
exercised end to end against a genuine downstream rather than a stub.

It is built on the SDK's :class:`mcp.server.mcpserver.MCPServer` (the mcp 2.x
successor to FastMCP), which implements the wire-level transport, session, and
security handling.  By default the server binds to ``127.0.0.1`` per the
spec's localhost recommendation.

Capability surface (mirrors the stdio full mock where it makes sense):

- tools:     ``echo``, ``add``
- resources: ``mock://docs/readme.txt`` (text) + a ``mock://docs/{name}.txt``
             template
- prompts:   ``greeting``, ``summarize``
- completion for prompt/resource references

Server→client requests (sampling/elicitation/roots) and ``subscriptions/listen``
change streams are intentionally absent — those are deferred roadmap items for
the HTTP path, so the mock does not advertise or exercise them.

Run directly::

    python examples/http_mock_server.py --host 127.0.0.1 --port 9000
    # endpoint: http://127.0.0.1:9000/mcp

Environment:
    MOCK_SERVER_NAME  overrides the advertised server name (default "http-mock").
"""

from __future__ import annotations

import argparse
import os

from mcp.server.mcpserver import MCPServer

SERVER_NAME = os.environ.get("MOCK_SERVER_NAME", "http-mock")

mcp = MCPServer(name=SERVER_NAME, version="0.1.0")


# -- Tools --------------------------------------------------------------------


@mcp.tool()
def echo(text: str) -> str:
    """Echo back the provided text."""
    return text


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


# -- Resources ----------------------------------------------------------------


@mcp.resource(
    "mock://docs/readme.txt",
    name="readme",
    title="Project Readme",
    description="A short text document.",
    mime_type="text/plain",
)
def readme() -> str:
    return "Hello from the HTTP mock server."


@mcp.resource(
    "mock://docs/{name}.txt",
    name="doc-by-name",
    title="Document by name",
    description="Fetch any mock document by name.",
    mime_type="text/plain",
)
def doc_by_name(name: str) -> str:
    return f"Synthetic document: {name}"


# -- Prompts ------------------------------------------------------------------


@mcp.prompt()
def greeting(name: str, formal: bool = False) -> str:
    """Produce a friendly greeting for a given name."""
    return f"Good day, {name}." if formal else f"Hey {name}!"


@mcp.prompt()
def summarize(text: str) -> str:
    """Summarize a block of text."""
    return f"Summarize: {text}"


# -- Entry point --------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Streamable HTTP mock MCP server.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=9000, help="Bind port (default 9000).")
    args = parser.parse_args()

    # Serves the 2026-07-28 Streamable HTTP binding at /mcp, bound to localhost.
    mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
