# Baldr MCP Gateway — Learning Guide

This guide covers the educational features of the Baldr MCP Gateway (`mcp-baldr`): the protocol inspection harness and the traffic initiation utility. These tools help students and developers observe the Model Context Protocol (MCP) in real time without disrupting the live stdio transport.

For installation and general usage, see the [main README](../README.md).

---

## Educational Harness & Visual Inspection

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

Force-enable learning mode from the command line, regardless of config. This works with any config, including the enhanced tool set:

```bash
mcp-baldr gateway_config.yaml --learn
mcp-baldr enhanced_config.yaml --learn
```

### Tracing During an Active Session

To watch protocol traffic while Claude Desktop (or any MCP host) is connected, redirect stderr to a log file and tail it in a separate terminal:

```bash
# Terminal 1: Run the gateway, pipe stderr to a trace file
mcp-baldr gateway_config.yaml --learn 2> trace.log

# Terminal 2: Watch the trace in real time
tail -f trace.log
```

Or in your Claude Desktop config, set the gateway command with stderr redirected:

```json
{
  "mcpServers": {
    "baldr": {
      "command": "/path/to/mcp-gw/.venv/bin/mcp-baldr",
      "args": ["/path/to/mcp-gw/gateway_config.yaml", "--learn", "2>", "/tmp/mcp-trace.log"]
    }
  }
}
```

Then `tail -f /tmp/mcp-trace.log` in a terminal to see every request, response, routing decision, and error as it happens.

---

## Live Endpoint Testing & Traffic Initiation

Traffic initiation is the act of programmatically generating MCP protocol requests to exercise the full gateway pipeline — from client intent, through namespace routing, to downstream server execution and back. The traffic init utility (`scripts/learn_traffic_init.py`) serves as a mock client that triggers this flow and prints a visual map of every step to stderr.

### Packet Journey

When the traffic init script calls a tool, the request traverses the entire proxy architecture:

```
Client (learn_traffic_init.py)
  │  tools/call via stdin (JSON-RPC 2.0)
  ▼
Gateway (mcp-baldr)
  │  resolve namespace → identify server
  │  forward to subprocess stdin
  ▼
Downstream server (local or search)
  │  execute tool
  │  return result via stdout
  ▼
Gateway → Client (result on stdout)
```

### Configuration

The learning network config (`examples/learning_network_config.yaml`) defines two server namespaces:

```yaml
servers:
  - name: local        # Offline mock server (echo, add)
    command: python3
    args: [examples/mock_server.py]
    enabled: true

  - name: search       # Live Brave Search (requires BRAVE_API_KEY)
    command: npx
    args: ["-y", "@modelcontextprotocol/server-brave-search"]
    enabled: false     # Enabled automatically by the traffic init script
```

### Offline Mode (no API key)

```bash
python scripts/learn_traffic_init.py
```

Without `BRAVE_API_KEY`, the script targets the local mock server (`local__echo`) and prints an educational guide explaining how to obtain and configure the Brave Search API key for the live path.

### Online Mode (with API key)

```bash
export BRAVE_API_KEY="your-api-key"
python scripts/learn_traffic_init.py
```

With the API key detected, the script enables the search server in the config, calls the live `search__brave_web_search` tool, and maps the full packet journey through the external endpoint. If the live server fails to start (missing npx, invalid key, network error), the script catches the error, prints an explanation, and falls back to offline mode — no stack trace.

### Running Against a Specific Config (`--config`)

For a consistent experience across tool sets, point the utility at any gateway config with `--config`. The script discovers whatever tools the config exposes and calls one of them, regardless of the namespace prefix — so the same command works for both the basic and enhanced tool sets.

```bash
# Basic tool set (echo, add) via the default config
python scripts/learn_traffic_init.py --config gateway_config.yaml

# Enhanced educational tool set (simulate_visual_report, stateful_counter,
# trigger_protocol_error) via the enhanced config
python scripts/learn_traffic_init.py --config enhanced_config.yaml
```

When `--config` is supplied, the script uses that file as-is (it is never modified or deleted) and skips the auto-generated config logic. This is the recommended way to explore the enhanced tools added to `examples/mock_server.py`.

---

## Enhanced Mock Tools

The mock server (`examples/mock_server.py`) can expose an extended educational tool set via `MOCK_TOOLS=enhanced` (used by `enhanced_config.yaml`). These tools demonstrate key MCP concepts:

| Tool | Demonstrates |
|------|--------------|
| `simulate_visual_report` | Multi-modal content arrays — a text block plus a base64 image block, showing how clients parse mixed media |
| `stateful_counter` | State persistence across independent `tools/call` invocations within a subprocess |
| `trigger_protocol_error` | Graceful protocol-level error payloads (`isError: true`) instead of system crashes |

Explore them end-to-end with:

```bash
python scripts/learn_traffic_init.py --config enhanced_config.yaml
```

---

## MCP Learning Lab (Backend Foundation)

Beyond the stderr inspector, Baldr ships a structured educational layer in the
`mcp_baldr.learning` package. Where the inspector *renders* protocol traffic to
a terminal, the Learning Lab *captures* it as sanitized, queryable events and
serves structured lesson content. This is the foundation a richer UI can be
built on; today it is consumed by the `scripts/learn.py` terminal viewer.

The Learning Lab is loosely coupled to the gateway. It is created only when
learning mode is on, and the gateway operates identically when it is off — a
property covered by isolation tests in `tests/test_learning.py`.

### Components

| Module | Responsibility |
|--------|----------------|
| `learning/events.py` | `EducationalEvent` (immutable, sanitized) plus `CapabilityCategory` and `ProcessingStage` enums |
| `learning/redaction.py` | `redact()` / `redact_env()` — the security boundary that masks tokens, API keys, passwords, and secret-shaped values, and truncates large blobs |
| `learning/event_collector.py` | `EventCollector` — a bounded ring buffer that records, redacts, correlates (by request id), and filters events |
| `learning/progress.py` | `ProgressTracker` — JSON-backed lessons/exercises/quiz progress, kept separate from gateway operational data |
| `learning/lessons.py` / `learning/curriculum.py` | Lesson content as data, with beginner/intermediate/advanced depth levels, real source references, and an honest capability-support matrix |

### Live vs. simulated events

Every event carries an `is_simulated` flag. Events captured from a running
gateway are always `is_simulated=False`; educational examples are
`is_simulated=True`. The two are never conflated. Live events are produced only
when the gateway runs with learning mode enabled.

### Sensitive-data redaction

All event metadata passes through `redact()` before storage. For example, a
tool call carrying an `api_key` argument is stored as:

```python
{"arguments": {"text": "hi", "api_key": "«redacted»"}}
```

Keys whose names look sensitive are masked, values that look like secrets
(bearer tokens, key prefixes, long opaque strings) are masked even under
innocuous keys, and oversized strings are truncated to avoid leaking large
payloads.

### Honest capability labeling

The curriculum never overclaims. The capability-support matrix marks a method
*implemented* only when the gateway truly serves it: `initialize`, `ping`,
`tools/*`, `resources/*` (including subscribe/unsubscribe), `prompts/*`,
`completion/complete`, and `logging/setLevel`. Server→client requests
(`sampling/createMessage`, `elicitation/create`, `roots/list`) are labeled
*partial* because the gateway only **relays** them to the upstream client,
gated on the client's advertised capability — it is a conduit, not a provider,
and never auto-approves. Notifications are *partial* too: recognized ones are
handled/forwarded, unrecognized ones are intentionally dropped rather than
blindly relayed. Unknown methods default to *not implemented*.

### The `learn` terminal viewer

```bash
# List all lessons (✓ marks completed)
python scripts/learn.py lessons

# Show one lesson at a chosen depth
python scripts/learn.py lesson fundamentals.aggregation --level advanced

# Honest capability-support matrix
python scripts/learn.py capabilities

# Your learning progress
python scripts/learn.py progress

# Mark a lesson complete
python scripts/learn.py complete fundamentals.what-is-mcp
```

Progress is stored at `~/.mcp_baldr_learning_progress.json`, separate from any
gateway operational data.

### Status & scope

This is the Phase 1 backend foundation. Implemented and tested:

- Structured, sanitized event model with live/simulated distinction
- Sensitive-data redaction (the security boundary)
- Bounded event collector with request/response correlation and filtering
- JSON-backed progress tracking
- MCP Fundamentals curriculum (4 lessons, three depth levels)
- Gateway wiring that captures real events, verified isolated when disabled
- `learn` terminal viewer

Not yet built (deferred): the browser UI, the interactive architecture diagram,
guided exercises, knowledge-check quizzes, and the Transport/Security labs. A
web front-end is a separate project; this backend is designed so any UI can be
layered on the same event and curriculum data.
