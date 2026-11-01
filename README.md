# Enterprise MCP Gateway

A modular reverse proxy for Model Context Protocol (MCP) servers. The gateway spawns downstream `stdio` MCP server subprocesses, aggregates their tools under namespaced names, and exposes a unified MCP server interface to AI clients.

## Features

- **Multi-server aggregation** — combine many MCP servers behind a single endpoint
- **Automatic namespacing** — tool collisions resolved via `server__tool` prefixing
- **YAML + Pydantic config** — validated configuration that fails fast on errors
- **Crash detection & auto-restart** — monitor subprocess health, restart on crash
- **Clean shutdown** — SIGINT/SIGTERM handled gracefully, no orphaned processes
- **Configurable timeouts** — per-server request timeouts with per-request overrides
- **Structured JSON logging** — all requests and responses logged for observability
- **Pure stdio transport** — no HTTP dependency, works with any MCP host

## Prerequisites

- Python 3.12 or later
- The downstream MCP servers you want to proxy (e.g. npx, uvx, or any stdio MCP server)

## Quickstart

```bash
# 1. Clone and install
git clone https://github.com/atifksiddiqui/mcp-gw.git && cd mcp-gw
python3 -m venv .venv && source .venv/bin/activate

# Development (includes pytest, pytest-asyncio, ruff):
pip install -e ".[dev]"

# Production (runtime dependencies only, no editable mode):
pip install .

# 2. Verify it works with the built-in mock servers
python scripts/quick_e2e.py

# 3. Run the gateway (connects an AI client via stdin/stdout)
mcp-gateway gateway_config.yaml
```

Expected output from step 2:

```
[OK] Started server 'mock' (pid=12345)
[OK] Initialized: {'name': 'mock-server', 'version': '0.1.0'}
[OK] tools/list returned 2 tool(s):
       - echo: Echo back the provided text.
       - add: Add two integers and return the sum.
[OK] tools/call echo: 'hello world'
[OK] tools/call add(7, 35): 42
[ALL CHECKS PASSED]
```

Additional verification scripts:

```bash
# Multi-server namespace routing
python scripts/verify_multi_server.py

# Example MCP client connecting to the gateway as a subprocess
python scripts/example_client.py gateway_config.yaml
```

The gateway exits cleanly on EOF, SIGINT, or SIGTERM. See the [Configuration](#configuration) section below for customizing `gateway_config.yaml`.

## Architecture

```
 AI Client (e.g. Claude)
    │  stdin/stdout (JSON-RPC 2.0)
    ▼
┌──────────────────────────────────────────────────┐
│                   MCP Gateway                      │
│                                                    │
│  Config Loader ── Pydantic + YAML validation       │
│  ToolRegistry  ── namespaced tool aggregation     │
│  Routing       ── resolve("srv_a__echo")          │
│                    → ("srv_a", "echo")             │
│  Transports    ── StdioTransport per server        │
│  Monitoring    ── crash detection + auto-restart   │
│  Server Loop   ── MCP server on own stdin/stdout   │
│  Signal Handle ── SIGINT/SIGTERM → clean shutdown  │
└──────┬──────────────────────┬──────────────────────┘
       │ stdin/stdout         │ stdin/stdout
       ▼                      ▼
   ┌───────────┐          ┌───────────┐
   │  Server A │          │  Server B │
   │           │          │           │
   │ echo      │          │ greet     │
   │ add       │          │ multiply  │
   └───────────┘          └───────────┘
```

The gateway is itself an MCP server. AI clients connect to the gateway's stdin/stdout. The gateway forwards requests to the correct downstream subprocess based on the namespace prefix in tool names (`server__tool`).

## Configuration

Edit `gateway_config.yaml` to define your downstream servers:

```yaml
servers:
  - name: mock_a
    command: python3
    args:
      - examples/mock_server.py

  - name: mock_b
    command: python3
    args:
      - examples/mock_server.py
    env:
      MOCK_TOOLS: "alt"
      MOCK_SERVER_NAME: "mock-server-b"

logging:
  level: INFO
  format: json
```

This config creates two downstream servers. Tools are namespaced as:
- `mock_a__echo`, `mock_a__add` (from server A)
- `mock_b__greet`, `mock_b__multiply` (from server B)

If both servers define a tool with the same local name (e.g. `echo`), they remain distinct: `mock_a__echo` and `mock_b__echo`.

Each server entry supports:

| Field | Default | Description |
|-------|---------|-------------|
| `name` | required | Unique identifier, used as the tool namespace prefix (lowercase letters, digits, underscores) |
| `command` | required | The executable to run |
| `args` | `[]` | Arguments passed to the command |
| `env` | `{}` | Environment variables merged with the current env |
| `enabled` | `true` | Set to `false` to skip at startup |
| `timeout` | `30` | Seconds to wait for a response before timing out |
| `auto_restart` | `false` | Restart the server if it crashes unexpectedly |
| `max_restarts` | `3` | Maximum restart attempts before giving up |

Real-world example with resiliency enabled:

```yaml
servers:
  # Filesystem access
  - name: fs
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/docs"]
    auto_restart: true
    max_restarts: 3

  # GitHub integration
  - name: github
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_..."
    timeout: 60

  # SQLite database
  - name: sqlite
    command: uvx
    args: ["mcp-server-sqlite", "--db-path", "/data/app.db"]
    auto_restart: true
```

With this config, an AI client sees: `fs__read_file`, `fs__write_file`, `github__create_issue`, `sqlite__query`, etc. — all through one connection, no tool name collisions.

## Usage with MCP Clients

The gateway is a standard MCP stdio server. Any MCP-compatible client can connect to it.

### Claude Desktop

Add the gateway to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gateway": {
      "command": "/path/to/mcp-gw/.venv/bin/mcp-gateway",
      "args": ["/path/to/mcp-gw/gateway_config.yaml"]
    }
  }
}
```

### Cursor / VS Code (MCP extension)

Point your MCP client at the gateway command:

```bash
mcp-gateway /path/to/gateway_config.yaml
```

### Programmatic (Python)

```python
import asyncio
from mcp_gateway.gateway.server import Gateway

async def main():
    async with Gateway("gateway_config.yaml") as gw:
        tools = await gw.list_tools()
        print(f"{len(tools)} tools available")
        for t in tools:
            print(f"  {t['name']}: {t.get('description', '')}")

        result = await gw.call_tool("mock_a__echo", {"text": "hello"})
        print(result["content"][0]["text"])

asyncio.run(main())
```

## How Routing Works

When a client calls `tools/call` with a namespaced name like `mock_a__echo`:

1. The gateway looks up `mock_a__echo` in the registry
2. The registry resolves it to `("mock_a", "echo")` — the server name and original tool name
3. The gateway finds the `StdioTransport` for `mock_a`
4. It forwards `tools/call` with the original name `echo` (not the namespaced one)
5. The result is passed back to the client

If the tool doesn't exist in the registry, the gateway returns a JSON-RPC error. If the owning server is down, the gateway returns an internal error.

## Resiliency

### Process Monitoring

Each downstream subprocess is watched by a background monitor task. When a subprocess exits unexpectedly (not via a normal `stop()` call):

1. The monitor sets a `crashed` flag with the exit code
2. Any in-flight requests are rejected with a `StdioTransportError`
3. The gateway's crash callback is invoked

### Auto-Restart

If `auto_restart: true` is set, the crash callback:
1. Increments the restart counter for that server
2. Calls `transport.restart()` (stop + start + initialize)
3. Re-discovers tools and re-registers them in the registry
4. The gateway is ready to route calls again

If the restart count exceeds `max_restarts`, or if `auto_restart` is false, the server's tools are removed from the registry and the gateway returns errors for calls to that server.

### Clean Shutdown

The gateway handles SIGINT and SIGTERM by:
1. Stopping the stdin reader (using `asyncio.StreamReader` for clean cancellation)
2. Stopping all downstream subprocesses (SIGTERM with 5s grace, then SIGKILL)
3. Exiting with code 0

### Request Timeouts

Each server has a configurable `timeout` (default 30s). If a downstream server doesn't respond within the timeout, the request fails with a `StdioTransportError` and the client receives a JSON-RPC error response. Per-request timeouts can also override the config default.

## Docker

```bash
# Build the image
docker build -t mcp-gateway .

# Run with the default config (reads from stdin)
docker run --rm -i mcp-gateway

# Or mount your own config
docker run --rm -i -v /path/to/your_config.yaml:/app/gateway_config.yaml mcp-gateway
```

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Lint
.venv/bin/ruff check mcp_gateway/ tests/ examples/ scripts/
```

104 tests across 7 files:

| File | Tests | Covers |
|------|-------|--------|
| `test_config_loader.py` | 23 | Config validation, YAML loading, edge cases |
| `test_stdio_transport.py` | 20 | Transport lifecycle, MCP handshake, tool ops, errors |
| `test_end_to_end.py` | 3 | Single-server config → transport → mock server |
| `test_registry.py` | 18 | Namespace registration, collision handling, routing resolution, client format, unregister, introspection |
| `test_multi_server.py` | 14 | Multi-server routing, cross-route rejection, gateway server loop via subprocess |
| `test_resiliency.py` | 14 | Crash detection, crash callback, transport restart, auto-restart recovery, max restarts, no-restart tool removal, SIGINT/SIGTERM clean exit, config/per-request timeouts |
| `test_edge_cases.py` | 12 | Concurrent tool calls, malformed JSON-RPC input, unknown methods, empty lines, registry stress (20 servers), logging utilities |

## Project Structure

```
mcp-gw/
├── .github/
│   └── workflows/
│       └── ci.yml             # GitHub Actions: lint + test on push/PR
├── mcp_gateway/
│   ├── config/
│   │   ├── models.py          # Pydantic config models (with resiliency fields)
│   │   └── loader.py          # YAML parser + validation
│   ├── transports/
│   │   └── stdio.py           # StdioTransport: subprocess bridge + crash monitoring
│   ├── registry/
│   │   └── tool_registry.py   # Multi-server registry with namespace routing
│   ├── gateway/
│   │   └── server.py          # Gateway: MCP server loop + routing + crash handling
│   ├── educational/
│   │   └── inspector.py       # ProtocolInspector: educational stderr traces
│   └── logging_utils.py       # Structured JSON logging
├── examples/
│   ├── mock_server.py         # Mock MCP server (default + alt tool sets)
│   └── slow_server.py         # Slow MCP server for timeout testing
├── tests/
│   ├── conftest.py            # Shared fixtures (paths, config helpers)
│   ├── test_config_loader.py
│   ├── test_stdio_transport.py
│   ├── test_end_to_end.py
│   ├── test_registry.py
│   ├── test_multi_server.py
│   ├── test_resiliency.py
│   └── test_edge_cases.py
├── scripts/
│   ├── quick_e2e.py            # Single-server verification
│   ├── verify_multi_server.py  # Multi-server routing verification
│   └── example_client.py      # Example MCP client connecting to the gateway
├── .gitignore
├── Dockerfile                  # Containerized deployment
├── gateway_config.yaml         # Default configuration (two mock servers)
├── LICENSE
└── pyproject.toml
```

## Contributing

1. Fork the repo and clone your fork
2. Create a venv and install dev dependencies: `pip install -e ".[dev]"`
3. Make your changes
4. Run lint: `ruff check mcp_gateway/ tests/ examples/ scripts/`
5. Run tests: `python -m pytest tests/ -v`
6. Add tests for any new functionality
7. Submit a pull request

## Build Status

### Phase 1: Subprocess Stdio Bridge & Config Loader
- [x] Project structure with `gateway/`, `registry/`, `transports/`, `config/`, `tests/`
- [x] `gateway_config.yaml` parser using Pydantic for validation
- [x] `StdioTransport` adapter: spawn subprocess, proxy JSON-RPC, initialize handshake
- [x] End-to-end tool discovery (`tools/list`) and execution (`tools/call`) with mock server
- [x] Structured JSON logging for all requests and responses
- [x] Unit and integration tests with pytest

### Phase 2: Multi-Server Registry & Namespace Router
- [x] Extended registry to load multiple `stdio` servers from YAML config
- [x] Namespace prefixing for tool schemas (`server__tool`)
- [x] Automatic request routing back to the correct subprocess based on prefix
- [x] Gateway acts as MCP server on stdin/stdout for AI clients
- [x] Namespace collision handling (same tool name on different servers)
- [x] Full test coverage for registry routing and multi-server integration

### Phase 3: Lifecycle Management & Resiliency
- [x] Process monitoring: detect unexpected subprocess crashes, reject in-flight requests
- [x] Auto-restart: configurable per server, with max restart limit and tool re-discovery
- [x] Clean shutdown on SIGINT/SIGTERM using asyncio.StreamReader for clean stdin cancellation
- [x] Configurable per-server request timeouts (with per-request override)
- [x] Full test coverage for crash recovery, restart, signal handling, and timeouts

### Phase 4: Testing & Example Sandbox
- [x] 104 tests across 7 files (config, transport, registry, multi-server, resiliency, edge cases)
- [x] Shared test fixtures in `conftest.py`
- [x] Example client script demonstrating real gateway usage
- [x] Dockerfile for containerized deployment
- [x] GitHub Actions CI (lint + test on push/PR)
- [x] Ruff linting and formatting
- [x] `.gitignore`, `LICENSE`, example configs

### Roadmap

- **Future**: Resource proxying, prompt aggregation, SSE transport, persistence layer

## Phase 5: Educational Harness & Visual Inspection

The gateway includes an optional educational mode that prints richly formatted, tree-like traces of all JSON-RPC traffic to **stderr**. This lets students and developers observe the MCP protocol in real time — `initialize` handshakes, `tools/list` aggregation, `tools/call` routing, namespace resolution, and downstream server communication — without affecting the protocol stream on stdout.

All inspection output goes exclusively to `sys.stderr`. The gateway's `sys.stdout` remains a clean JSON-RPC pipe, so AI clients (Claude Desktop, Cursor, etc.) are never disrupted.

### Configuration

Add a `learning` block to `gateway_config.yaml`:

```yaml
learning:
  learning_mode: true        # Enable protocol inspection (default: false)
  verbose_handshake: true    # Show detailed initialize breakdown (default: true)
```

If the `learning` block is omitted entirely, learning mode defaults to `false` and the gateway runs in its raw, high-performance proxy mode with zero overhead.

### CLI Flag

Force-enable learning mode from the command line, regardless of config:

```bash
mcp-gateway gateway_config.yaml --learn
```

### Tracing During an Active Session

To watch protocol traffic while Claude Desktop (or any MCP host) is connected, redirect stderr to a log file and tail it in a separate terminal:

```bash
# Terminal 1: Run the gateway, pipe stderr to a trace file
mcp-gateway gateway_config.yaml --learn 2> trace.log

# Terminal 2: Watch the trace in real time
tail -f trace.log
```

Or in your Claude Desktop config, set the gateway command with stderr redirected:

```json
{
  "mcpServers": {
    "gateway": {
      "command": "/path/to/mcp-gw/.venv/bin/mcp-gateway",
      "args": ["/path/to/mcp-gw/gateway_config.yaml", "--learn", "2>", "/tmp/mcp-trace.log"]
    }
  }
}
```

Then `tail -f /tmp/mcp-trace.log` in a terminal to see every request, response, routing decision, and error as it happens.

## License

MIT
