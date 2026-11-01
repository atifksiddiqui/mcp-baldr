#!/usr/bin/env python3
"""Slow mock MCP server for timeout testing.

Responds to ``initialize`` and ``tools/list`` quickly, but sleeps for
``MOCK_DELAY`` seconds (default 5) before responding to ``tools/call``.

Used by tests/test_resiliency.py to verify request timeout handling.
"""

from __future__ import annotations

import json
import os
import sys
import time

PROTOCOL_VERSION = "2026-07-28"
DELAY = float(os.environ.get("MOCK_DELAY", "5.0"))


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        if msg_id is None:
            continue  # notification

        method = msg.get("method", "")

        if method == "tools/call":
            time.sleep(DELAY)

        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "slow-server", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "slow_tool",
                        "description": "Responds slowly.",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        else:
            result = {}

        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
