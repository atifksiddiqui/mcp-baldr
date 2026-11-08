# Baldr MCP Gateway — Learning Guide

Baldr is also a place to **learn the Model Context Protocol (MCP) by watching it work**. This guide covers three complementary tools for studying the protocol — a structured curriculum, a live traffic inspector, and a traffic generator — none of which disrupt the gateway's real stdio transport.

For installation and general usage, see the [main README](../README.md).

## Three ways to learn with Baldr

| I want to… | Use | Command |
|------------|-----|---------|
| **Study** MCP concepts at my own pace (lessons, capability matrix) | Learning Lab + `learn.py` viewer | `python scripts/learn.py lessons` |
| **Watch** live protocol traffic as a client talks to the gateway | stderr inspector (`--learn`) | `mcp-baldr gateway_config.yaml --learn` |
| **Generate** traffic to watch, without a real client | Traffic-init utility | `python scripts/learn_traffic_init.py` |

A good path for a newcomer: read a couple of lessons, then run the traffic-init utility *with* inspection on so you can read a real trace of the requests you just triggered.

All three are strictly non-destructive to the protocol: every byte of educational output goes to **stderr**, so the gateway's `stdout` JSON-RPC stream stays a clean pipe that AI clients (Claude Desktop, Cursor, etc.) are never disturbed by.

---

## 1. The Learning Lab & `learn.py` viewer

Baldr ships a structured educational layer in the `mcp_baldr.learning` package. It serves lesson content as data (so any UI can render it) and, when the gateway is running with learning mode on, captures real protocol activity as sanitized, queryable events. Today it is consumed by the `scripts/learn.py` terminal viewer.

### Curriculum

The **MCP Fundamentals** module contains four lessons, each available at three depth levels (`beginner`, `intermediate`, `advanced`):

| Lesson id | Topic |
|-----------|-------|
| `fundamentals.what-is-mcp` | What MCP is and the client/server/host model |
| `fundamentals.json-rpc` | The JSON-RPC 2.0 message shapes MCP is built on |
| `fundamentals.initialization` | The `initialize` handshake and capability negotiation |
| `fundamentals.aggregation` | How Baldr namespaces and routes across many servers |

Each lesson cites **real source files** in this repo, so you can read the explanation and then jump straight to the code that implements it.

### Viewer commands

| Command | Purpose |
|---------|---------|
| `python scripts/learn.py lessons` | List all lessons (`✓` marks completed) |
| `python scripts/learn.py lesson <id> [--level L]` | Show one lesson at a chosen depth |
| `python scripts/learn.py capabilities` | Show the honest capability-support matrix |
| `python scripts/learn.py progress` | Show your learning progress |
| `python scripts/learn.py complete <id>` | Mark a lesson complete |

```bash
# e.g. read the aggregation lesson at full depth
python scripts/learn.py lesson fundamentals.aggregation --level advanced
```

Progress is stored at `~/.mcp_baldr_learning_progress.json`, completely separate from any gateway operational data.

### Honest capability labeling

The curriculum never overclaims. A method is labeled **implemented** only when the gateway truly serves it — `initialize`, `ping`, `tools/*`, `resources/*` (including subscribe/unsubscribe), `prompts/*`, `completion/complete`, and `logging/setLevel`.

Server→client requests (`sampling/createMessage`, `elicitation/create`, `roots/list`) are labeled **partial**: the gateway only *relays* them to the upstream client, gated on the client's advertised capability — it is a conduit, not a provider, and never auto-approves. Notifications are **partial** too: recognized ones are handled or forwarded, unrecognized ones are intentionally dropped rather than blindly relayed. Unknown methods default to **not implemented**.

---

## 2. Live protocol inspection (`--learn`)

Run the gateway with `--learn` (or set `learning.learning_mode: true` in config) and it prints richly formatted, tree-like traces of every JSON-RPC message to **stderr** — handshakes, `tools/list` aggregation, namespace resolution, downstream communication, and errors — as they happen.

```bash
mcp-baldr gateway_config.yaml --learn
```

### What a trace looks like

Here is a real excerpt (colors are shown only on a TTY; piping to a file yields plain text). This is the journey of a single `tools/call` for `mock_a__echo` — note how the gateway resolves the namespaced name, forwards the *original* name downstream, and relays the result back:

```
┌─ [AI CLIENT ──► GATEWAY]  tools/call  (id=3) ─────────────────────────
├───────────────────────────────────────────────────────────────────────
│ params:
│   name: "mock_a__echo"
│   arguments:
│     text: "hello from a trace"
└───────────────────────────────────────────────────────────────────────
┌─ [GATEWAY]  Namespace Resolution ─────────────────────────────────────
├───────────────────────────────────────────────────────────────────────
│ requested: 'mock_a__echo'
│   └─► server: 'mock_a'   tool: 'echo'
└───────────────────────────────────────────────────────────────────────
┌─ [GATEWAY ──► mock_a]  tools/call ────────────────────────────────────
├───────────────────────────────────────────────────────────────────────
│ name: "echo"
│ arguments:
│   text: "hello from a trace"
└───────────────────────────────────────────────────────────────────────
┌─ [mock_a ──► GATEWAY]  tools/call result ─────────────────────────────
├───────────────────────────────────────────────────────────────────────
│ content:
│   [0]:
│     type: "text"
│     text: "hello from a trace"
│ isError: false
└───────────────────────────────────────────────────────────────────────
┌─ [GATEWAY ──► AI CLIENT]  tools/call result  (id=3) ──────────────────
├───────────────────────────────────────────────────────────────────────
│ content:
│   [0]:
│     type: "text"
│     text: "hello from a trace"
│ isError: false
└───────────────────────────────────────────────────────────────────────
```

The direction label in each box header (`AI CLIENT ──► GATEWAY`, `GATEWAY ──► mock_a`, etc.) makes the four hops of the round trip explicit. Earlier in the same session you would also see `[GATEWAY READY]`, the `initialize` handshake, and the full `tools/list` aggregation.

### Configuration

```yaml
learning:
  learning_mode: true        # Enable protocol inspection (default: false)
  verbose_handshake: true    # Show detailed initialize breakdown (default: true)
```

Omit the `learning` block entirely and the gateway runs in raw, high-performance proxy mode with zero inspection overhead. The `--learn` flag force-enables inspection regardless of config, and works with any config including the enhanced tool set (`mcp-baldr enhanced_config.yaml --learn`).

### Tracing an active Claude Desktop / host session

Redirect stderr to a file and tail it in another terminal:

```bash
# Terminal 1 — run the gateway, send the trace to a file
mcp-baldr gateway_config.yaml --learn 2> trace.log

# Terminal 2 — watch it live
tail -f trace.log
```

Or redirect stderr from within your host's MCP config, e.g. Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "baldr": {
      "command": "/path/to/mcp-baldr/.venv/bin/mcp-baldr",
      "args": ["/path/to/mcp-baldr/gateway_config.yaml", "--learn", "2>", "/tmp/mcp-trace.log"]
    }
  }
}
```

Then `tail -f /tmp/mcp-trace.log` to watch every request, response, routing decision, and error as it happens.

---

## 3. Generating traffic (`learn_traffic_init.py`)

When you don't have a real client handy, the traffic-init utility acts as a mock client: it spawns the gateway, triggers tool calls, and prints a visual map of the full packet journey to stderr. Pair it with `--learn` on the gateway and you can read the inspector trace of traffic you generated yourself.

### Packet journey

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

### Offline mode (no API key)

```bash
python scripts/learn_traffic_init.py
```

Without `BRAVE_API_KEY`, the script targets the local mock server (`local__echo`) and explains how to obtain and configure a Brave Search API key for the live path.

### Online mode (with API key)

```bash
export BRAVE_API_KEY="your-api-key"
python scripts/learn_traffic_init.py
```

With the key detected, the script enables the live Brave Search server, calls `search__brave_web_search`, and maps the full journey through the external endpoint. If the live server fails to start (missing npx, invalid key, network error), it catches the error, explains it, and falls back to offline mode — no stack trace.

### Against a specific config (`--config`)

Point the utility at any gateway config; it discovers whatever tools the config exposes and calls one, regardless of namespace prefix. The config is used as-is and never modified or deleted.

```bash
python scripts/learn_traffic_init.py --config gateway_config.yaml   # basic tool set
python scripts/learn_traffic_init.py --config enhanced_config.yaml  # enhanced tool set
```

The learning network config (`examples/learning_network_config.yaml`) defines a `local` namespace (offline mock) and a `search` namespace (live Brave Search, enabled automatically only when a key is present).

---

## Enhanced mock tools

The mock server (`examples/mock_server.py`) exposes an extended educational tool set via `MOCK_TOOLS=enhanced` (used by `enhanced_config.yaml`). Each tool demonstrates a key MCP concept:

| Tool | Demonstrates |
|------|--------------|
| `simulate_visual_report` | Multi-modal content arrays — a text block plus a base64 image block, showing how clients parse mixed media |
| `stateful_counter` | State persistence across independent `tools/call` invocations within a subprocess |
| `trigger_protocol_error` | Graceful protocol-level error payloads (`isError: true`) instead of system crashes |

Explore them end to end:

```bash
python scripts/learn_traffic_init.py --config enhanced_config.yaml
```

---

## Under the hood

The Learning Lab is **loosely coupled** to the gateway: it is created only when learning mode is on, and the gateway behaves identically when it is off — a property covered by isolation tests in `tests/test_learning.py`.

### Components

| Module | Responsibility |
|--------|----------------|
| `learning/events.py` | `EducationalEvent` (immutable, sanitized) plus `CapabilityCategory` and `ProcessingStage` enums |
| `learning/redaction.py` | `redact()` / `redact_env()` — the security boundary that masks tokens, API keys, passwords, and secret-shaped values, and truncates large blobs |
| `learning/event_collector.py` | `EventCollector` — a bounded ring buffer that records, redacts, correlates (by request id), and filters events |
| `learning/progress.py` | `ProgressTracker` — JSON-backed lessons/exercises/quiz progress, kept separate from gateway operational data |
| `learning/lessons.py` / `learning/curriculum.py` | Lesson content as data, with depth levels, real source references, and the capability-support matrix |

### Live vs. simulated events

Every event carries an `is_simulated` flag. Events captured from a running gateway are always `is_simulated=False`; educational examples are `is_simulated=True`. The two are never conflated, and live events are produced only when the gateway runs with learning mode enabled.

### Sensitive-data redaction

All event metadata passes through `redact()` before storage. A tool call carrying an `api_key` argument is stored as:

```python
{"arguments": {"text": "hi", "api_key": "«redacted»"}}
```

Keys whose names look sensitive are masked; values that *look* like secrets (bearer tokens, key prefixes, long opaque strings) are masked even under innocuous keys; oversized strings are truncated to avoid leaking large payloads.

---

## What's included / not yet built

**Included and tested today:**

- Structured, sanitized event model with a live/simulated distinction
- Sensitive-data redaction (the security boundary)
- Bounded event collector with request/response correlation and filtering
- JSON-backed progress tracking
- MCP Fundamentals curriculum (4 lessons × 3 depth levels) with real source references
- Gateway wiring that captures real events, verified isolated when disabled
- The `learn.py` terminal viewer and the honest capability matrix

**Not yet built (deferred):** the browser UI and interactive architecture diagram, guided exercises, knowledge-check quizzes, and the Transport/Security labs. A web front-end is a separate project; this layer is designed so any UI can be built on the same event and curriculum data.
