"""Tests for ping + server→client request relaying (Stage 7).

Covers:
- ``ping`` answered directly by the gateway.
- Upstream client capability gating of sampling/elicitation/roots relays
  (no auto-approve: relayed only if the client advertised the capability).
- The full downstream→gateway→client→gateway→downstream relay round trip,
  driven through the real full mock server's ``server_request`` affordance
  with a simulated upstream client.
- Progress / cancellation notifications forwarded upstream verbatim.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp_baldr.gateway.server import Gateway
from mcp_baldr.gateway.server import Gateway as GatewayClass
from tests.conftest import write_gateway_config

ROOT = Path(__file__).resolve().parent.parent
FULL_SERVER = str(ROOT / "examples" / "full_mock_server.py")


def _config(tmp_path: Path) -> Path:
    yaml = f"""\
    servers:
      - name: full
        command: {sys.executable}
        args: ["{FULL_SERVER}"]
    logging:
      level: WARNING
      format: text
    """
    return write_gateway_config(tmp_path, yaml)


# -- ping ---------------------------------------------------------------------


class TestPing:
    @pytest.mark.asyncio
    async def test_ping_answered_directly(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            result = await gw._handle_request("ping", {})
            assert result == {}


# -- Client capability capture ------------------------------------------------


class TestClientCapabilityCapture:
    @pytest.mark.asyncio
    async def test_initialize_captures_client_capabilities(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            gw._handle_initialize(
                {"capabilities": {"sampling": {}, "roots": {"listChanged": True}}}
            )
            assert "sampling" in gw._client_capabilities
            assert "roots" in gw._client_capabilities
            assert "elicitation" not in gw._client_capabilities

    @pytest.mark.asyncio
    async def test_initialize_without_capabilities_is_empty(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            gw._handle_initialize({})
            assert gw._client_capabilities == {}


# -- Server→client relay gating (no auto-approve) -----------------------------


class TestRelayGating:
    @pytest.mark.asyncio
    async def test_relay_refused_when_client_lacks_capability(self, tmp_path: Path):
        """If the client never advertised sampling, the relay is refused."""
        async with Gateway(_config(tmp_path)) as gw:
            gw._client_capabilities = {}  # client supports nothing
            transport = gw.transports["full"]

            errors: list[tuple] = []

            async def capture_error(req_id, code, message):
                errors.append((req_id, code, message))

            transport.send_error_response = capture_error  # type: ignore[assignment]

            await gw._on_downstream_request("full", 1, "sampling/createMessage", {"messages": []})
            assert errors, "expected an error response to the downstream"
            _, code, message = errors[0]
            assert "sampling" in message

    @pytest.mark.asyncio
    async def test_relay_refused_for_unrelayable_method(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            gw._client_capabilities = {"sampling": {}}
            transport = gw.transports["full"]
            errors: list[tuple] = []

            async def capture_error(req_id, code, message):
                errors.append((req_id, code, message))

            transport.send_error_response = capture_error  # type: ignore[assignment]

            await gw._on_downstream_request("full", 2, "some/other", {})
            assert errors
            assert "does not relay" in errors[0][2]

    @pytest.mark.asyncio
    async def test_relay_forwards_to_client_and_returns_response(self, tmp_path: Path):
        """With capability present, the gateway relays and returns the answer."""
        async with Gateway(_config(tmp_path)) as gw:
            gw._client_capabilities = {"sampling": {}}
            transport = gw.transports["full"]

            responses: list[tuple] = []

            async def capture_response(req_id, result):
                responses.append((req_id, result))

            transport.send_response = capture_response  # type: ignore[assignment]

            client_answer = {
                "role": "assistant",
                "content": {"type": "text", "text": "hi"},
                "model": "test-model",
            }

            # Simulate the upstream client: capture the gateway's outgoing
            # request and immediately feed back a matching response.
            def fake_send_request(req_id, method, params):
                assert method == "sampling/createMessage"
                # Resolve via the same path _serve uses for client responses.
                gw._resolve_client_response(
                    {"jsonrpc": "2.0", "id": req_id, "result": client_answer}
                )

            gw._send_request = staticmethod(fake_send_request)  # type: ignore[assignment]

            await gw._on_downstream_request("full", 42, "sampling/createMessage", {"messages": []})

            assert responses, "expected the gateway to answer the downstream"
            req_id, result = responses[0]
            assert req_id == 42
            assert result == client_answer


# -- Client response correlation ----------------------------------------------


class TestClientResponseCorrelation:
    @pytest.mark.asyncio
    async def test_resolve_client_response_ignores_foreign_ids(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            # A client request (has a method) must NOT be treated as a response.
            consumed = gw._resolve_client_response(
                {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}
            )
            assert consumed is False
            # A response to an id we never issued is ignored.
            consumed = gw._resolve_client_response({"jsonrpc": "2.0", "id": "gw-999", "result": {}})
            assert consumed is False

    @pytest.mark.asyncio
    async def test_send_client_request_times_out(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            # No client will answer; a short timeout must raise.
            gw._send_request = staticmethod(lambda *a, **k: None)  # type: ignore[assignment]
            from mcp_baldr.transports.stdio import StdioTransportError

            with pytest.raises(StdioTransportError):
                await gw._send_client_request("roots/list", {}, timeout=0.2)


# -- Full relay round trip through the real mock ------------------------------


class TestEndToEndRelay:
    @pytest.mark.asyncio
    async def test_sampling_round_trip_via_mock(self, tmp_path: Path):
        """full__server_request(sampling) → relay → simulated client → back.

        The mock emits a real ``sampling/createMessage`` request and blocks for
        the response; the gateway relays it to a simulated client (patched
        ``_send_request``) which answers; the mock reports the relayed result.
        """
        async with Gateway(_config(tmp_path)) as gw:
            gw._client_capabilities = {"sampling": {}}

            client_answer = {"role": "assistant", "content": {"type": "text", "text": "pong"}}

            def fake_send_request(req_id, method, params):
                # Answer on a separate task so the relay's await can proceed.
                asyncio.get_event_loop().call_soon(
                    gw._resolve_client_response,
                    {"jsonrpc": "2.0", "id": req_id, "result": client_answer},
                )

            gw._send_request = staticmethod(fake_send_request)  # type: ignore[assignment]

            result = await gw.call_tool("full__server_request", {"kind": "sampling"})
            outcome = result["_serverRequest"]
            assert outcome["status"] == "ok"
            assert outcome["result"] == client_answer

    @pytest.mark.asyncio
    async def test_roots_refused_round_trip_via_mock(self, tmp_path: Path):
        """If the client lacks roots, the mock receives an error, not a fake."""
        async with Gateway(_config(tmp_path)) as gw:
            gw._client_capabilities = {}  # no roots support

            result = await gw.call_tool("full__server_request", {"kind": "roots"})
            outcome = result["_serverRequest"]
            assert outcome["status"] == "error"
            assert "roots" in outcome["error"]["message"]


# -- Passthrough notifications ------------------------------------------------


class TestPassthroughNotifications:
    @pytest.mark.asyncio
    async def test_progress_forwarded_verbatim(self, tmp_path: Path, monkeypatch):
        forwarded: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            GatewayClass,
            "_send_notification",
            staticmethod(lambda m, p: forwarded.append((m, p))),
        )
        async with Gateway(_config(tmp_path)) as gw:
            params = {"progressToken": "abc", "progress": 0.5, "total": 1.0}
            await gw._on_downstream_notification("full", "notifications/progress", params)
            assert ("notifications/progress", params) in forwarded

    @pytest.mark.asyncio
    async def test_cancelled_forwarded_verbatim(self, tmp_path: Path, monkeypatch):
        forwarded: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            GatewayClass,
            "_send_notification",
            staticmethod(lambda m, p: forwarded.append((m, p))),
        )
        async with Gateway(_config(tmp_path)) as gw:
            params = {"requestId": "r1", "reason": "user cancelled"}
            await gw._on_downstream_notification("full", "notifications/cancelled", params)
            assert ("notifications/cancelled", params) in forwarded
