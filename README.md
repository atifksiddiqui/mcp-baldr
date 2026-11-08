# Baldr MCP Gateway (mcp-baldr)

A modular reverse proxy for Model Context Protocol (MCP) servers. The gateway spawns downstream `stdio` MCP server subprocesses, aggregates their full capability surface — tools, resources, prompts, completion, logging, subscriptions — under namespaced identifiers, and exposes a single unified MCP server interface to AI clients. It also relays server→client requests (sampling, elicitation, roots) to the upstream client.

## Features

- **Full-protocol aggregation** — tools, resources, prompts, completion, and logging combined behind one endpoint
- **Honest capability negotiation** — the gateway advertises only what healthy downstreams support *and* what it can actually proxy; it never overclaims
- **Automatic namespacing** — tool/prompt collisions resolved via `server__name` prefixing; resources via a reversible, collision-safe `baldr+<server>://` URI scheme
- **Notifications & subscriptions** — `list_changed` refresh+forward, resource-update subscriptions with per-URI dedup, log-message forwarding with secret redaction
- **Server→client relaying** — sampling / elicitation / roots requests are forwarded to the client **only if the client advertised the capability**; the gateway never auto-approves
- **Opaque, stable pagination** — aggregated `*/list` results are paginated with a tamper-evident cursor that stays correct as downstreams come and go
- **YAML + Pydantic config** — validated configuration that fails fast on errors
- **Crash detection & auto-restart** — monitor subprocess health, restart on crash, clean up subscriptions
- **Clean shutdown** — SIGINT/SIGTERM handled gracefully, no orphaned processes
- **Configurable timeouts** — per-server request timeouts with per-request overrides
- **Structured JSON logging** — all requests and responses logged for observability
- **Pure stdio transport** — no HTTP dependency, works with any MCP host
- **Educational inspection** — optional stderr protocol traces for learning and debugging (see [docs/LEARNING.md](docs/LEARNING.md))

## MCP capability support

The gateway proxies the capabilities below. "Relayed" means the gateway is a conduit, not a provider — it forwards the request to the upstream client and relays the answer back, gated on the client's advertised capability.

| Capability | Methods | Status |
|------------|---------|--------|
| Initialization | `initialize`, `notifications/initialized` | Aggregated / negotiated |
| Tools | `tools/list`, `tools/call` | Aggregated + namespaced + routed |
| Resources | `resources/list`, `resources/read`, `resources/templates/list` | Aggregated with reversible namespaced URIs |
| Subscriptions | `resources/subscribe`, `resources/unsubscribe`, `notifications/resources/updated` | Deduplicated, translated, cleaned up on crash |
| Prompts | `prompts/list`, `prompts/get` | Aggregated + namespaced + routed |
| Completion | `completion/complete` | Routed (prompt-ref and resource-ref) |
| Logging | `logging/setLevel`, `notifications/message` | Fan-out to downstreams; forwarded logs are redacted |
| List changes | `notifications/tools|resources|prompts/list_changed` | Refresh registry + forward upstream |
| Ping | `ping` | Answered directly |
| Sampling / Elicitation / Roots | `sampling/createMessage`, `elicitation/create`, `roots/list` | **Relayed** to the client (capability-gated, never auto-approved) |
| Progress / Cancellation | `notifications/progress`, `notifications/cancelled` | Forwarded verbatim |

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
mcp-baldr gateway_config.yaml
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

# Educational traffic initiation (offline/online)
python scripts/learn_traffic_init.py
```

The gateway exits cleanly on EOF, SIGINT, or SIGTERM. See the [Configuration](#configuration) section below for customizing `gateway_config.yaml`.

## Architecture

```
 AI Client (e.g. Claude)
    │  stdin/stdout (JSON-RPC 2.0)
    ▼
┌──────────────────────────────────────────────────┐
│                Baldr MCP Gateway                   │
│                                                    │
│  Config Loader   ── Pydantic + YAML validation     │
│  CapabilityReg.  ── honest upstream aggregate       │
│  Registries      ── tools / resources / prompts     │
│  RequestRouter   ── method → handler dispatch       │
│  Routing         ── resolve("srv_a__echo")          │
│                     → ("srv_a", "echo")             │
│  Subscriptions   ── resource-update tracking        │
│  Client relay    ── sampling/elicitation/roots      │
│  Transports      ── StdioTransport per server       │
│  Monitoring      ── crash detection + auto-restart  │
│  Server Loop     ── MCP server on own stdin/stdout  │
│  Signal Handle   ── SIGINT/SIGTERM → clean shutdown │
└──────┬──────────────────────┬──────────────────────┘
       │ stdin/stdout         │ stdin/stdout
       ▼                      ▼
   ┌───────────┐          ┌───────────┐
   │  Server A │          │  Server B │
   │ tools     │          │ tools     │
   │ resources │          │ prompts   │
   │ prompts   │          │ resources │
   └───────────┘          └───────────┘
```

The gateway is itself an MCP server. AI clients connect to the gateway's stdin/stdout. The gateway forwards requests to the correct downstream subprocess based on the namespace prefix in tool/prompt names (`server__name`) or the namespaced resource URI scheme (`baldr+<server>://`).

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

pagination:
  page_size: 100
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

Top-level `pagination.page_size` (default 100) bounds how many items each aggregated `tools/list` / `resources/list` / `prompts/list` page returns.

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
    "baldr": {
      "command": "/path/to/mcp-gw/.venv/bin/mcp-baldr",
      "args": ["/path/to/mcp-gw/gateway_config.yaml"]
    }
  }
}
```

### Cursor / VS Code (MCP extension)

Point your MCP client at the gateway command:

```bash
mcp-baldr /path/to/gateway_config.yaml
```

### Programmatic (Python)

```python
import asyncio
from mcp_baldr.gateway.server import Gateway

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

Prompts route the same way (`server__prompt`). Resources use a reversible URI scheme — `resources/read` on `baldr+mock_a://<encoded>` is decoded back to `("mock_a", "<original uri>")` and the original URI is forwarded downstream.

If the tool doesn't exist in the registry, the gateway returns a JSON-RPC error. If the owning server is down, the gateway returns an internal error.

## Server→Client Requests

Some MCP interactions flow the other way: a downstream server asks the *client* to do something (sample from the model, elicit input, list roots). The gateway relays these faithfully and safely:

1. A downstream sends e.g. `sampling/createMessage` to the gateway.
2. The gateway checks whether the **upstream client** advertised the matching capability (`sampling`) in its `initialize` call.
3. If not, the gateway returns a method-not-found error to the downstream — it **never** auto-approves or fabricates a result.
4. If so, the gateway forwards the request to the client, waits for the client's answer, and relays that answer (or error) back to the downstream.

The gateway is strictly a conduit for these requests: it does not generate samples, approve elicitations, or invent roots.

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
3. Re-discovers tools/resources/prompts and re-registers them
4. Drops stale subscriptions so the client can re-subscribe
5. The gateway is ready to route calls again

If the restart count exceeds `max_restarts`, or if `auto_restart` is false, the server's items are removed from the registries and the gateway returns errors for calls to that server.

### Clean Shutdown

The gateway handles SIGINT and SIGTERM by:
1. Stopping the stdin reader (using `asyncio.StreamReader` for clean cancellation)
2. Stopping all downstream subprocesses (SIGTERM with 5s grace, then SIGKILL)
3. Exiting with code 0

### Request Timeouts

Each server has a configurable `timeout` (default 30s). If a downstream server doesn't respond within the timeout, the request fails with a `StdioTransportError` and the client receives a JSON-RPC error response. Per-request timeouts can also override the config default.

## Learning & Protocol Inspection

Baldr includes an educational mode that prints tree-formatted JSON-RPC traces to **stderr** so you can watch the MCP protocol flow in real time — handshakes, tool discovery, namespace routing, and downstream communication — without disrupting the stdout protocol stream. Enable it with the `--learn` flag:

```bash
mcp-baldr gateway_config.yaml --learn
```

There's also a traffic initiation utility (`scripts/learn_traffic_init.py`) that acts as a mock client and visualizes the full packet journey, plus an enhanced mock tool set demonstrating multi-modal output, state persistence, and protocol errors.

**See the full [Learning Guide](docs/LEARNING.md)** for inspection configuration, session tracing, traffic initiation (offline/online), and the enhanced educational tools.

## Docker

```bash
# Build the image
docker build -t mcp-baldr .

# Run with the default config (reads from stdin)
docker run --rm -i mcp-baldr

# Or mount your own config
docker run --rm -i -v /path/to/your_config.yaml:/app/gateway_config.yaml mcp-baldr
```

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Lint
ruff check mcp_baldr/ tests/ examples/ scripts/
```

262 tests across 16 files. Core transport/registry/resiliency coverage plus a test suite per protocol capability and a full end-to-end integration walk:

| File | Covers |
|------|--------|
| `test_config_loader.py` | Config validation, YAML loading, pagination settings, edge cases |
| `test_stdio_transport.py` | Transport lifecycle, MCP handshake, tool ops, errors |
| `test_end_to_end.py` | Single-server config → transport → mock server |
| `test_registry.py` | Namespace registration, collision handling, routing resolution, client format |
| `test_multi_server.py` | Multi-server routing, cross-route rejection, gateway loop via subprocess |
| `test_resiliency.py` | Crash detection, auto-restart, max restarts, SIGINT/SIGTERM, timeouts |
| `test_edge_cases.py` | Concurrent calls, malformed input, unknown methods, registry stress |
| `test_capability_registry.py` | Capability negotiation + honest upstream aggregate |
| `test_router.py` | Unified request-router dispatch |
| `test_resources.py` | Resource aggregation, namespaced URIs, read routing, templates |
| `test_prompts.py` | Prompt aggregation + routing |
| `test_subscriptions.py` | Resource subscriptions, dedup, live update forwarding |
| `test_completion_logging.py` | Completion routing, logging fan-out, redacted log forwarding |
| `test_server_requests.py` | ping, sampling/elicitation/roots relay (capability-gated), progress/cancel |
| `test_pagination.py` | Opaque stable cursors, cross-downstream paginated walks |
| `test_integration.py` | Full-protocol end-to-end walk through two live downstreams |

## Project Structure

```
mcp-gw/
├── .github/
│   └── workflows/
│       └── ci.yml             # GitHub Actions: lint + test on push/PR
├── mcp_baldr/
│   ├── config/
│   │   ├── models.py          # Pydantic config (servers, logging, learning, pagination)
│   │   └── loader.py          # YAML parser + validation
│   ├── protocol/
│   │   ├── uri.py             # Reversible namespaced resource URIs
│   │   └── pagination.py      # Opaque, stable cursor pagination
│   ├── transports/
│   │   └── stdio.py           # StdioTransport: subprocess bridge + crash monitoring
│   ├── routing/
│   │   └── router.py          # RequestRouter: method → handler dispatch
│   ├── registry/
│   │   ├── base_registry.py   # Generic namespaced-registry mechanics
│   │   ├── tool_registry.py   # Tool aggregation
│   │   ├── resource_registry.py  # Resource + template aggregation
│   │   ├── prompt_registry.py    # Prompt aggregation
│   │   └── capability_registry.py # Downstream caps + honest upstream aggregate
│   ├── gateway/
│   │   ├── server.py          # Gateway: MCP server loop + routing + relaying
│   │   └── subscription_manager.py # Resource-subscription tracking
│   ├── educational/
│   │   └── inspector.py       # ProtocolInspector: educational stderr traces
│   ├── learning/              # Learning Lab: events, curriculum, redaction, progress
│   └── logging_utils.py       # Structured JSON logging
├── examples/
│   ├── mock_server.py               # Tools-only mock MCP server
│   ├── full_mock_server.py          # Capability-complete mock (all protocol surfaces)
│   ├── slow_server.py               # Slow MCP server for timeout testing
│   └── learning_network_config.yaml  # Local + live search config
├── tests/                      # 16 test files (see Testing section)
├── scripts/
│   ├── quick_e2e.py            # Single-server verification
│   ├── verify_multi_server.py  # Multi-server routing verification
│   ├── example_client.py      # Example MCP client connecting to the gateway
│   └── learn_traffic_init.py   # Educational traffic initiation (offline/online)
├── docs/
│   └── LEARNING.md             # Educational guide: inspection + traffic initiation
├── .gitignore
├── Dockerfile                  # Containerized deployment
├── gateway_config.yaml         # Default configuration (two mock servers)
├── enhanced_config.yaml        # Enhanced educational tool set config
├── LICENSE
└── pyproject.toml
```

## Contributing

1. Fork the repo and clone your fork
2. Create a venv and install dev dependencies: `pip install -e ".[dev]"`
3. Make your changes
4. Run lint: `ruff check mcp_baldr/ tests/ examples/ scripts/`
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
- [x] Tests across config, transport, registry, multi-server, resiliency, edge cases
- [x] Shared test fixtures in `conftest.py`
- [x] Example client script demonstrating real gateway usage
- [x] Dockerfile for containerized deployment
- [x] GitHub Actions CI (lint + test on push/PR)
- [x] Ruff linting and formatting
- [x] `.gitignore`, `LICENSE`, example configs

### Phase 5: Educational Harness & Visual Inspection
- [x] `ProtocolInspector` module with tree-formatted stderr traces
- [x] `LearningConfig` with `learning_mode` and `verbose_handshake` fields
- [x] `--learn` CLI flag to force-enable inspection from the command line
- [x] Non-destructive: all output to stderr, stdout protocol stream untouched
- [x] Global try/except wrapping for silent failure on unexpected errors

### Phase 6: Live Endpoint Testing & Traffic Initiation
- [x] `learning_network_config.yaml` with local mock + live Brave Search servers
- [x] `learn_traffic_init.py` with auto-detection of `BRAVE_API_KEY`
- [x] Offline fallback with educational text blocks (no stack traces)
- [x] Packet journey visualization on stderr
- [x] Graceful error handling for missing npx, invalid keys, or network failures

### Phase 7: Full MCP Protocol Support
- [x] Capability negotiation + honest upstream capability aggregate (`CapabilityRegistry`)
- [x] Unified request router replacing the ad-hoc method ladder
- [x] Resource aggregation with reversible, collision-safe namespaced URIs
- [x] Prompt aggregation (`server__prompt` namespacing)
- [x] Notifications + resource subscriptions (dedup, translation, crash cleanup)
- [x] Completion routing (prompt-ref + resource-ref) and logging fan-out with redaction
- [x] Server→client relaying (sampling/elicitation/roots), capability-gated, never auto-approved
- [x] ping, progress/cancellation passthrough
- [x] Opaque, stable cursor pagination across downstreams
- [x] Full-protocol integration test + honest capability matrix in the Learning Lab

### Roadmap

- **Future**: SSE / streamable-HTTP transport, persistence layer, interactive architecture explorer

## License

MIT
