# Changelog

All notable development milestones for Baldr (mcp-baldr) are recorded here,
organized by the build phase in which they landed. The project is
pre-1.0; versions will be tagged as the API stabilizes.

## Phase 8 — Downstream Streamable HTTP Transport

Baldr can now aggregate **remote** MCP servers over the MCP 2026-07-28
Streamable HTTP binding, alongside local stdio subprocesses. The stdio path
and `--learn` educational mode are unchanged.

- `DownstreamTransport` interface extracted; `StdioTransport` implements it and
  the gateway now depends on the interface, not a concrete transport
- Config is a discriminated union on `type`: `stdio` (default, so existing
  configs are untouched) or `http` (`HttpServerConfig` with `url` + static
  `headers` for auth)
- `HttpTransport` built on the SDK's `mcp.client.streamable_http` +
  `ClientSession` (no new top-level dependency); proxies tools, resources,
  prompts, completion, logging, and ping
- The configured `timeout` is honored (HTTP client timeout + SDK session read
  timeout), so a slow/unreachable server fails fast instead of hanging
- `examples/http_mock_server.py` — a real Streamable HTTP mock server
  (`MCPServer`) for honest end-to-end tests
- HTTP transport unit + gateway-with-HTTP-downstream integration tests; mixed
  stdio+http aggregation verified

Deferred for the HTTP path (tracked in the README Roadmap), not regressions —
these exist and work on the stdio transport:

- Server→client requests (sampling / elicitation / roots), which 2026-07-28
  carries via Multi Round-Trip Requests (MRTR, SEP-2322) rather than standalone
  requests
- Live change notifications via a `subscriptions/listen` long-lived SSE stream
- Authentication beyond static headers (e.g. OAuth)
- Backward compatibility with the legacy HTTP+SSE transport and the
  2025-03-26…2025-11-25 Streamable HTTP revisions (session-id, standalone GET
  SSE, resumable streams)
- Proactive connection-health detection: a crashed/unreachable HTTP downstream
  is caught reactively on the next request, not auto-unregistered via a health
  monitor the way a crashed stdio subprocess is
- Per-request timeout override (the configured `timeout` applies session-wide;
  a per-call override is accepted but not yet plumbed through the SDK session)

Also on the roadmap: an **upstream** Streamable HTTP transport (AI clients
connecting to Baldr over HTTP; today Baldr's upstream interface is stdio only).

## Phase 7 — Full MCP Protocol Support

- Capability negotiation + honest upstream capability aggregate (`CapabilityRegistry`)
- Unified request router replacing the ad-hoc method ladder
- Resource aggregation with reversible, collision-safe namespaced URIs
- Prompt aggregation (`server__prompt` namespacing)
- Notifications + resource subscriptions (dedup, translation, crash cleanup)
- Completion routing (prompt-ref + resource-ref) and logging fan-out with redaction
- Server→client relaying (sampling/elicitation/roots), capability-gated, never auto-approved
- ping, progress/cancellation passthrough
- Opaque, stable cursor pagination across downstreams
- Full-protocol integration test + honest capability matrix in the Learning Lab

## Phase 6 — Live Endpoint Testing & Traffic Initiation

- `learning_network_config.yaml` with local mock + live Brave Search servers
- `learn_traffic_init.py` with auto-detection of `BRAVE_API_KEY`
- Offline fallback with educational text blocks (no stack traces)
- Packet journey visualization on stderr
- Graceful error handling for missing npx, invalid keys, or network failures

## Phase 5 — Educational Harness & Visual Inspection

- `ProtocolInspector` module with tree-formatted stderr traces
- `LearningConfig` with `learning_mode` and `verbose_handshake` fields
- `--learn` CLI flag to force-enable inspection from the command line
- Non-destructive: all output to stderr, stdout protocol stream untouched
- Global try/except wrapping for silent failure on unexpected errors

## Phase 4 — Testing & Example Sandbox

- Tests across config, transport, registry, multi-server, resiliency, edge cases
- Shared test fixtures in `conftest.py`
- Example client script demonstrating real gateway usage
- Dockerfile for containerized deployment
- GitHub Actions CI (lint + test on push/PR)
- Ruff linting and formatting
- `.gitignore`, `LICENSE`, example configs

## Phase 3 — Lifecycle Management & Resiliency

- Process monitoring: detect unexpected subprocess crashes, reject in-flight requests
- Auto-restart: configurable per server, with max restart limit and tool re-discovery
- Clean shutdown on SIGINT/SIGTERM using `asyncio.StreamReader` for clean stdin cancellation
- Configurable per-server request timeouts (with per-request override)
- Full test coverage for crash recovery, restart, signal handling, and timeouts

## Phase 2 — Multi-Server Registry & Namespace Router

- Extended registry to load multiple `stdio` servers from YAML config
- Namespace prefixing for tool schemas (`server__tool`)
- Automatic request routing back to the correct subprocess based on prefix
- Gateway acts as MCP server on stdin/stdout for AI clients
- Namespace collision handling (same tool name on different servers)
- Full test coverage for registry routing and multi-server integration

## Phase 1 — Subprocess Stdio Bridge & Config Loader

- Project structure with `gateway/`, `registry/`, `transports/`, `config/`, `tests/`
- `gateway_config.yaml` parser using Pydantic for validation
- `StdioTransport` adapter: spawn subprocess, proxy JSON-RPC, initialize handshake
- End-to-end tool discovery (`tools/list`) and execution (`tools/call`) with mock server
- Structured JSON logging for all requests and responses
- Unit and integration tests with pytest
