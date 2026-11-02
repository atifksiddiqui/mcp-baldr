#!/usr/bin/env python3
"""Educational traffic initiation utility (Phase 6).

Spawns the MCP gateway with the learning network config, connects as a
mock client, and triggers tool calls through both local and live
namespaces.  All diagnostic output goes to **stderr** to keep the
gateway's stdout JSON-RPC channel pure.

Behaviour:
  - If ``BRAVE_API_KEY`` is set in the environment, the script enables
    the live search server and calls it.
  - If the key is absent (or the search server fails to start), the
    script falls back to the local mock server and prints an
    educational text block explaining how to obtain and configure the
    missing requirements.

Run::

    python scripts/learn_traffic_init.py
    BRAVE_API_KEY="your-key" python scripts/learn_traffic_init.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp_gateway.transports.stdio import StdioTransportError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "examples" / "learning_network_config.yaml"
MOCK_SERVER = str(PROJECT_ROOT / "examples" / "mock_server.py")


# -- Stderr helpers ------------------------------------------------------------

_LEARN = "\033[36m[LEARN]\033[0m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RED = "\033[31m"


def _log(msg: str) -> None:
    """Print a milestone line to stderr."""
    print(f"{_LEARN} {msg}", file=sys.stderr, flush=True)


def _log_block(lines: list[str]) -> None:
    """Print an indented block to stderr."""
    for line in lines:
        print(f"{_LEARN} {line}", file=sys.stderr, flush=True)


# -- Config generation ---------------------------------------------------------


def _build_config(online: bool) -> str:
    """Build a YAML config string for the gateway subprocess.

    Args:
        online: If True, include the brave search server (enabled).
    """
    lines = [
        "servers:",
        "  - name: local",
        "    command: python3",
        f'    args: ["{MOCK_SERVER}"]',
        "    enabled: true",
    ]
    if online:
        api_key = os.environ.get("BRAVE_API_KEY", "")
        lines.extend(
            [
                "  - name: search",
                "    command: npx",
                '    args: ["-y", "@modelcontextprotocol/server-brave-search"]',
                "    env:",
                f'      BRAVE_API_KEY: "{api_key}"',
                "    enabled: true",
            ]
        )
    lines.extend(
        [
            "learning:",
            "  learning_mode: true",
            "  verbose_handshake: true",
            "logging:",
            "  level: WARNING",
            "  format: text",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_temp_config(yaml_text: str) -> Path:
    """Write YAML to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="mcpgw_learn_")
    with os.fdopen(fd, "w") as f:
        f.write(yaml_text)
    return Path(path)


# -- Gateway subprocess management ---------------------------------------------


async def _spawn_gateway(config_path: Path) -> asyncio.subprocess.Process:
    """Spawn the gateway as a subprocess."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mcp_gateway.gateway.server",
        str(config_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _gateway_alive(proc: asyncio.subprocess.Process) -> bool:
    """Check if the gateway process is still running after startup."""
    await asyncio.sleep(2.0)
    return proc.returncode is None


# -- Educational output --------------------------------------------------------


def _print_offline_fallback(error_detail: str) -> None:
    """Print an educational block explaining why the search server failed."""
    _log(f"{_RED}Live search server failed to start{_RESET}")
    _log(f"{_DIM}Error detail: {error_detail[:200]}{_RESET}")
    _log("")
    _log("Falling back to offline mode (local mock server only).")
    _log("")
    _log(f"{_YELLOW}To enable the live search path:{_RESET}")
    _log_block(
        [
            "1. Install Node.js (includes npx):",
            "   https://nodejs.org/",
            "",
            "2. Obtain a Brave Search API key:",
            "   https://api.search.brave.com/",
            "",
            "3. Export the key in your environment:",
            '   export BRAVE_API_KEY="your-api-key-here"',
            "",
            "4. Re-run this script:",
            "   python scripts/learn_traffic_init.py",
        ]
    )


def _print_no_api_key_guide() -> None:
    """Print an educational block when BRAVE_API_KEY is not set."""
    _log(f"{_YELLOW}BRAVE_API_KEY not detected in environment{_RESET}")
    _log("Running in OFFLINE mode — only the local mock server will be used.")
    _log("")
    _log("To test the live search path:")
    _log_block(
        [
            "1. Obtain a Brave Search API key:",
            "   https://api.search.brave.com/",
            "",
            "2. Export it:",
            '   export BRAVE_API_KEY="your-api-key-here"',
            "",
            "3. Re-run:",
            "   python scripts/learn_traffic_init.py",
        ]
    )


# -- Main flow -----------------------------------------------------------------


async def main(config_path: Path | None = None) -> None:
    _log("=" * 60)
    _log("Phase 6: Traffic Initiation & Endpoint Bridging")
    _log("=" * 60)

    has_api_key = bool(os.environ.get("BRAVE_API_KEY"))
    mode = "ONLINE" if has_api_key else "OFFLINE"
    _log(
        f"Mode: {_GREEN}{mode}{_RESET}"
        f"{' (BRAVE_API_KEY detected)' if has_api_key else ' (BRAVE_API_KEY not set)'}"
    )
    _log("")

    if not has_api_key and config_path is None:
        _print_no_api_key_guide()
        _log("")

    # Step 1: Build or use config and spawn gateway.
    if config_path is not None:
        _log(f"Step 1: Using config: {config_path}")
    else:
        _log("Step 1: Building gateway configuration...")
        yaml_text = _build_config(online=has_api_key)
        config_path = _write_temp_config(yaml_text)
        _log(f"  Config written to: {config_path.name}")

    _log("Step 2: Spawning gateway subprocess...")
    proc = await _spawn_gateway(config_path)

    if not await _gateway_alive(proc):
        # Gateway crashed during startup.
        stderr_bytes = await proc.stderr.read() if proc.stderr else b""
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if has_api_key:
            _log("")
            _print_offline_fallback(stderr_text)
            _log("")
            _log("Retrying in OFFLINE mode...")
            config_path.unlink(missing_ok=True)
            yaml_text = _build_config(online=False)
            config_path = _write_temp_config(yaml_text)
            proc = await _spawn_gateway(config_path)
            if not await _gateway_alive(proc):
                _log(f"{_RED}Gateway failed to start even in offline mode{_RESET}")
                _log(f"{_DIM}{stderr_text[:500]}{_RESET}")
                return
        else:
            _log(f"{_RED}Gateway failed to start{_RESET}")
            _log(f"{_DIM}{stderr_text[:500]}{_RESET}")
            return

    _log(f"  Gateway process: PID {proc.pid}")
    _log("")

    # Step 3: Connect as MCP client.
    _log("Step 3: Connecting as MCP client...")
    # We already have the process — read/write directly to its pipes.
    try:
        await _run_client(proc, has_api_key)
    except StdioTransportError as exc:
        _log(f"{_RED}Transport error: {exc}{_RESET}")
    except Exception as exc:
        _log(f"{_RED}Unexpected error: {exc}{_RESET}")
        _log(
            f"{_DIM}This is an educational utility — errors are expected when external dependencies are missing.{_RESET}"
        )
    finally:
        _log("")
        _log("Cleaning up...")
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        config_path.unlink(missing_ok=True)
        _log("Done.")


async def _run_client(proc: asyncio.subprocess.Process, online: bool) -> None:
    """Run the mock client flow: initialize → list tools → call tool."""
    assert proc.stdin is not None
    assert proc.stdout is not None

    _next_id = 0

    async def _send(msg: dict) -> None:
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

    async def _recv() -> dict:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
        if not line:
            raise StdioTransportError("Gateway closed stdout")
        return json.loads(line)

    async def _request(method: str, params: dict | None = None) -> dict:
        nonlocal _next_id
        _next_id += 1
        msg_id = _next_id
        await _send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        resp = await _recv()
        if "error" in resp:
            raise StdioTransportError(
                f"Error (code={resp['error']['code']}): {resp['error']['message']}"
            )
        return resp["result"]

    # Step 4: Initialize handshake.
    _log("Step 4: MCP initialize handshake...")
    _log('  → Client sends initialize {"protocolVersion": "2026-07-28", ...}')
    init_result = await _request(
        "initialize",
        {
            "protocolVersion": "2026-07-28",
            "capabilities": {},
            "clientInfo": {"name": "learn-traffic-init", "version": "0.1.0"},
        },
    )
    server_info = init_result.get("serverInfo", {})
    _log(f"  ← Gateway responds: {server_info.get('name')} v{server_info.get('version')}")
    _log(f"  Protocol version: {init_result.get('protocolVersion')}")

    # Send initialized notification.
    await _send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    _log("  → Client sends notifications/initialized (handshake complete)")
    _log("")

    # Step 5: List tools.
    _log("Step 5: Discovering tools (tools/list)...")
    _log("  → Client sends tools/list")
    list_result = await _request("tools/list")
    tools = list_result.get("tools", [])
    _log(f"  ← Gateway returns {len(tools)} tool(s):")
    for t in tools:
        _log(f"     {t['name']}: {t.get('description', '')}")
    _log("")

    # Step 6: Select and call the target tool.
    search_tools = [t for t in tools if t["name"].startswith("search__")]
    local_tools = [t for t in tools if t["name"].startswith("local__")]

    if online and search_tools:
        target = search_tools[0]["name"]
        args = {"query": "Model Context Protocol Python"}
        _log(f"Step 6: Calling live search tool: {_GREEN}{target}{_RESET}")
        _log(f'  → Client sends tools/call (name: "{target}", arguments: {json.dumps(args)})')
    elif local_tools:
        target = "local__echo"
        args = {"text": "hello from traffic init"}
        if online and not search_tools:
            _log(f"{_YELLOW}Search server did not register any tools{_RESET}")
            _log("Falling back to local mock server.")
            _log("")
        _log(f"Step 6: Calling local mock tool: {_GREEN}{target}{_RESET}")
        _log(f'  → Client sends tools/call (name: "{target}", arguments: {json.dumps(args)})')
    else:
        _log(f"{_RED}No tools available{_RESET}")
        return

    # Step 7: Show namespace resolution (handled by gateway inspector on stderr).
    _log("  → Gateway resolves namespace:")
    parts = target.split("__", 1)
    if len(parts) == 2:
        _log(f'     "{target}" → server: "{parts[0]}", tool: "{parts[1]}"')
        _log(f'  → Gateway forwards tools/call to subprocess "{parts[0]}"')

    # Call the tool.
    result = await _request("tools/call", {"name": target, "arguments": args})

    _log("  ← Subprocess responds with result")
    _log("  ← Gateway returns result to client")
    _log("")

    # Step 8: Display the result.
    _log("Step 7: Result received:")
    content = result.get("content", [])
    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "")
            # Truncate long search results for readability.
            if len(text) > 200:
                text = text[:200] + "..."
            _log(f'  content: "{text}"')
    _log(f"  isError: {result.get('isError', False)}")
    _log("")

    # Educational summary.
    _log("=" * 60)
    _log("Packet journey complete:")
    _log_block(
        [
            "  Client (learn_traffic_init.py)",
            "    │",
            "    │  tools/call via stdin (JSON-RPC 2.0)",
            "    ▼",
            "  Gateway (mcp-gateway)",
            "    │  resolve namespace → identify server",
            "    │  forward to subprocess stdin",
            "    ▼",
            f"  Downstream server ({parts[0] if len(parts) == 2 else 'unknown'})",
            "    │  execute tool",
            "    │  return result via stdout",
            "    ▼",
            "  Gateway → Client (result on stdout)",
        ]
    )
    _log("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _log("Interrupted.")
    except Exception as exc:
        _log(f"{_RED}Fatal error: {exc}{_RESET}")
        _log(f"{_DIM}This is an educational utility — check your environment and retry.{_RESET}")
