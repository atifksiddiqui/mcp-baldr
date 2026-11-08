# Baldr MCP Gateway (mcp-baldr)

A modular reverse proxy for Model Context Protocol (MCP) servers. The gateway spawns downstream `stdio` MCP server subprocesses (and/or connects to remote Streamable HTTP MCP servers), aggregates their full capability surface — tools, resources, prompts, completion, logging, subscriptions — under namespaced identifiers, and exposes a single unified MCP server interface to AI clients. It also relays server→client requests (sampling, elicitation, roots) to the upstream client.

## Features

- **Full-protocol aggregation** — tools, resources, prompts, completion, logging, and subscriptions combined behind one endpoint, with server→client requests relayed to the client
- **Honest capability negotiation** — advertises only what healthy downstreams support *and* what it can actually proxy; never overclaims
- **Automatic namespacing** — tool/prompt collisions resolved via `server__name` prefixing; resources via a reversible, collision-safe `baldr+<server>://` URI scheme
- **Notifications & subscriptions** — `list_changed` refresh+forward, resource-update subscriptions with per-URI dedup, log-message forwarding with secret redaction
- **Server→client relaying** — sampling / elicitation / roots forwarded to the client **only if the client advertised the capability**; never auto-approved
- **Opaque, stable pagination** — aggregated `*/list` results paginated with a tamper-evident cursor that stays correct as downstreams come and go
- **Resilient** — crash detection, optional auto-restart, configurable timeouts, clean SIGINT/SIGTERM shutdown
- **Pluggable downstream transports** — aggregate local `stdio` subprocesses and/or remote Streamable HTTP servers (MCP 2026-07-28), selected per server by a `type` field
- **YAML + Pydantic config** and **structured JSON logging**
- **Educational inspection** — optional stderr protocol traces (see [docs/LEARNING.md](docs/LEARNING.md))

## MCP capability support

"Relayed" means the gateway is a conduit, not a provider — it forwards the request to the upstream client and relays the answer back, gated on the client's advertised capability.

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

Capabilities per transport: **stdio** downstreams support the full surface above. **Streamable HTTP** downstreams support the request/response capabilities (tools, resources, prompts, completion, logging, ping); HTTP-side server→client requests, live subscriptions, and legacy fallback are on the [Roadmap](#roadmap).

## Prerequisites

- Python 3.12 or later
- The downstream MCP servers you want to proxy (e.g. npx, uvx, any stdio MCP server, or a remote Streamable HTTP MCP server)

## Quickstart

```bash
# 1. Clone and install
git clone https://github.com/atifksiddiqui/mcp-baldr.git && cd mcp-baldr
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"      # dev (pytest, ruff); use `pip install .` for runtime only

# 2. Verify it works with the built-in mock servers
python scripts/quick_e2e.py

# 3. Run the gateway (connects an AI client via stdin/stdout)
mcp-baldr gateway_config.yaml
```

Expected output from step 2:

```
[OK] Started server 'mock' (pid=12345)
[OK] Initialized: {'name': 'mock-server', 'version': '0.1.0'}
[OK] tools/list returned 2 tool(s): echo, add
[OK] tools/call echo: 'hello world'
[OK] tools/call add(7, 35): 42
[ALL CHECKS PASSED]
```

More verification scripts: `scripts/verify_multi_server.py` (namespace routing), `scripts/example_client.py gateway_config.yaml` (example client as subprocess), `scripts/learn_traffic_init.py` (educational traffic). The gateway exits cleanly on EOF, SIGINT, or SIGTERM.

## Architecture

```
 AI Client (e.g. Claude)
    │  stdin/stdout (JSON-RPC 2.0)
    ▼
┌──────────────────────────────────────────────────┐
│                Baldr MCP Gateway                   │
│  Config Loader   ── Pydantic + YAML validation     │
│  CapabilityReg.  ── honest upstream aggregate       │
│  Registries      ── tools / resources / prompts     │
│  RequestRouter   ── method → handler dispatch       │
│  Routing         ── resolve("srv_a__echo")          │
│                     → ("srv_a", "echo")             │
│  Subscriptions   ── resource-update tracking        │
│  Client relay    ── sampling/elicitation/roots      │
│  Transports      ── stdio (subprocess) / http       │
│  Monitoring      ── crash detection + auto-restart  │
│  Server Loop     ── MCP server on own stdin/stdout  │
└──────┬──────────────────────┬──────────────────────┘
       │ stdio                │ Streamable HTTP
       ▼                      ▼
   ┌───────────┐          ┌───────────┐
   │  Server A │          │  Server B │
   │ (local)   │          │ (remote)  │
   └───────────┘          └───────────┘
```

The gateway is itself an MCP server: AI clients connect to its stdin/stdout, and it routes each request to the owning downstream (a local subprocess or a remote HTTP endpoint).

**Routing.** A namespaced tool/prompt name like `mock_a__echo` resolves to `("mock_a", "echo")`; the gateway forwards the request to that server's transport using the *original* name. Resources use a reversible URI scheme — `baldr+mock_a://<encoded>` decodes back to `("mock_a", "<original uri>")`. Unknown items return a JSON-RPC error; a down server returns an internal error.

**Server→client requests.** When a downstream asks the *client* to act (`sampling/createMessage`, `elicitation/create`, `roots/list`), the gateway relays it **only if the client advertised that capability** in its `initialize` call. Otherwise it returns a method-not-found error — it never auto-approves or fabricates a result. The gateway is strictly a conduit: it does not generate samples, approve elicitations, or invent roots.

## Configuration

Define your downstream servers in `gateway_config.yaml`:

```yaml
servers:
  - name: mock_a
    command: python3
    args: [examples/mock_server.py]

  - name: mock_b
    command: python3
    args: [examples/mock_server.py]
    env:
      MOCK_TOOLS: "alt"

logging:
  level: INFO
  format: json

pagination:
  page_size: 100
```

Tools are namespaced per server (`mock_a__echo`, `mock_b__echo`), so identical local names never collide. Every server entry shares these fields:

| Field | Default | Description |
|-------|---------|-------------|
| `type` | `stdio` | Transport: `stdio` (local subprocess) or `http` (remote Streamable HTTP). Omit to get `stdio`. |
| `name` | required | Unique identifier + namespace prefix (lowercase letters, digits, underscores) |
| `enabled` | `true` | Set to `false` to skip at startup |
| `timeout` | `30` | Seconds to wait for a response before timing out (per-request override available) |

Stdio servers (`type: stdio`) additionally take:

| Field | Default | Description |
|-------|---------|-------------|
| `command` | required | The executable to run |
| `args` | `[]` | Arguments passed to the command |
| `env` | `{}` | Environment variables merged with the current env |
| `auto_restart` | `false` | Restart the server if it crashes unexpectedly |
| `max_restarts` | `3` | Maximum restart attempts before giving up |

Top-level `pagination.page_size` (default 100) bounds how many items each aggregated `*/list` page returns.

A real-world config mixing filesystem, GitHub, and SQLite servers with resiliency enabled:

```yaml
servers:
  - name: fs
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/docs"]
    auto_restart: true
  - name: github
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env: { GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_..." }
    timeout: 60
  - name: sqlite
    command: uvx
    args: ["mcp-server-sqlite", "--db-path", "/data/app.db"]
    auto_restart: true
```

An AI client then sees `fs__read_file`, `github__create_issue`, `sqlite__query`, etc. — all through one connection, no collisions.

### Remote servers over Streamable HTTP

Set `type: http` to aggregate a remote MCP server speaking the 2026-07-28 Streamable HTTP binding, alongside (or instead of) local stdio servers:

```yaml
servers:
  - name: local                       # stdio (the default type)
    command: python3
    args: [examples/mock_server.py]

  - type: http
    name: remote
    url: https://mcp.example.com/mcp   # the server's single MCP endpoint
    headers:                           # static headers, e.g. bearer auth
      Authorization: "Bearer your-token"
    timeout: 60
```

HTTP servers (`type: http`) take `url` (required) and `headers` (static per-request headers — the supported form of auth) in place of the stdio process fields. Header values are redacted in `--learn` output.

The HTTP transport proxies the request/response capabilities (tools, resources, prompts, completion, logging, ping). Server→client requests (which 2026-07-28 carries via Multi Round-Trip Requests), live `subscriptions/listen` change streams, OAuth, and legacy HTTP+SSE fallback are not yet implemented over HTTP — see the [Roadmap](#roadmap). The stdio transport supports the full protocol surface.

## Usage with MCP Clients

The gateway is a standard MCP stdio server; any MCP-compatible client can connect.

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "baldr": {
      "command": "/path/to/mcp-baldr/.venv/bin/mcp-baldr",
      "args": ["/path/to/mcp-baldr/gateway_config.yaml"]
    }
  }
}
```

**Cursor / VS Code (MCP extension)** — point the client at `mcp-baldr /path/to/gateway_config.yaml`.

**Programmatic (Python):**

```python
import asyncio
from mcp_baldr.gateway.server import Gateway

async def main():
    async with Gateway("gateway_config.yaml") as gw:
        tools = await gw.list_tools()
        print(f"{len(tools)} tools available")
        result = await gw.call_tool("mock_a__echo", {"text": "hello"})
        print(result["content"][0]["text"])

asyncio.run(main())
```

## Resiliency

- **Process monitoring** — a background task watches each subprocess; an unexpected exit flags the server as crashed, rejects in-flight requests, and fires the crash callback.
- **Auto-restart** (opt-in per stdio server) — on crash, the gateway restarts the subprocess, re-discovers its tools/resources/prompts, and drops stale subscriptions so the client can re-subscribe. After `max_restarts` (or when `auto_restart` is off) the server's items are removed and calls to it return errors.
- **Timeouts** — per-server `timeout` (default 30s), overridable per request; a slow downstream fails with a JSON-RPC error rather than hanging.
- **Clean shutdown** — SIGINT/SIGTERM stops the stdin reader (via `asyncio.StreamReader`), terminates downstreams (SIGTERM → 5s grace → SIGKILL), and exits 0.

## Learning & Protocol Inspection

An educational mode prints tree-formatted JSON-RPC traces to **stderr** (stdout protocol stream untouched) so you can watch handshakes, discovery, namespace routing, and downstream communication in real time:

```bash
mcp-baldr gateway_config.yaml --learn
```

See the **[Learning Guide](docs/LEARNING.md)** for inspection configuration, session tracing, the `scripts/learn_traffic_init.py` traffic utility, and the honest capability matrix.

## Docker

```bash
docker build -t mcp-baldr .
docker run --rm -i mcp-baldr                                           # default config (stdin)
docker run --rm -i -v /path/to/your_config.yaml:/app/gateway_config.yaml mcp-baldr  # custom config
```

## Testing

```bash
python -m pytest tests/ -v                               # full suite (stdio + http)
ruff check mcp_baldr/ tests/ examples/ scripts/          # lint
```

Coverage includes core transport/registry/resiliency, a suite per protocol capability (capability negotiation, resources, prompts, subscriptions, completion/logging, server→client requests, pagination), the transport abstraction and HTTP transport (`tests/test_http_transport.py`, driven against a real Streamable HTTP mock server), and a full end-to-end integration walk through two live downstreams (`tests/test_integration.py`).

## Contributing

See **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** for environment setup, the daily workflow, the project structure, and the release checklist. In short: fork, `pip install -e ".[dev]"`, make changes with tests, run `ruff check` + `pytest`, and open a PR.

## Roadmap

Project history by phase lives in **[CHANGELOG.md](CHANGELOG.md)**. Not yet built — tracked as independent future workstreams:

- **Streamable HTTP — full parity with stdio** — the downstream HTTP transport lands the request/response surface; still to do: server→client requests over HTTP (sampling/elicitation/roots via Multi Round-Trip Requests, SEP-2322), live change notifications via `subscriptions/listen`, authentication beyond static headers (OAuth), backward-compat with the legacy HTTP+SSE / pre-2026-07-28 Streamable HTTP revisions, **proactive connection-health detection** (a crashed/unreachable HTTP downstream is currently caught reactively on the next request, not auto-unregistered via a health monitor like stdio crashes), and **per-request timeout override** (the configured `timeout` is applied session-wide; a per-call override is accepted but not yet plumbed through the SDK session).
- **Upstream Streamable HTTP** — let AI clients connect to Baldr itself over HTTP (today Baldr serves its upstream interface over stdio only).
- **Persistence layer** — gateway state is currently in-memory (the only on-disk state is the Learning Lab's progress file).
- **Interactive architecture diagram (web UI)** — a browser-based explorer over the Learning Lab's event and curriculum data.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
