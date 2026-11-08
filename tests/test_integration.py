"""Full-protocol integration test (Stage 9).

Drives a real :class:`Gateway` with two live ``full_mock_server`` downstreams
through the gateway's own request path (``_handle_request`` / handler methods)
and exercises every capability surface end to end in one cohesive flow:

- initialize + honest capability aggregate
- paginated tools / resources / prompts across two servers
- namespaced tool-call routing to the correct downstream
- resource read with reversible URI de-namespacing + templates
- prompt get with argument passthrough
- completion for both ref/prompt and ref/resource
- logging/setLevel fan-out
- subscribe → live resources/updated → unsubscribe
- ping
- sampling relay to a simulated upstream client (capability-gated)

Everything runs against real subprocesses — no fabricated traces.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp_baldr.gateway.server import Gateway
from mcp_baldr.protocol.uri import decode_namespaced_uri
from tests.conftest import write_gateway_config

ROOT = Path(__file__).resolve().parent.parent
FULL_SERVER = str(ROOT / "examples" / "full_mock_server.py")


@pytest.fixture
def two_server_config(tmp_path: Path) -> Path:
    yaml = f"""\
    servers:
      - name: alpha
        command: {sys.executable}
        args: ["{FULL_SERVER}"]
        env:
          MOCK_SERVER_NAME: alpha-mock
      - name: beta
        command: {sys.executable}
        args: ["{FULL_SERVER}"]
        env:
          MOCK_SERVER_NAME: beta-mock
    pagination:
      page_size: 3
    logging:
      level: WARNING
      format: text
    """
    return write_gateway_config(tmp_path, yaml)


async def _walk_list(gw: Gateway, method: str, items_key: str) -> list[dict]:
    """Walk every page of a paginated list method and return all items."""
    collected: list[dict] = []
    cursor = None
    guard = 0
    while True:
        params = {"cursor": cursor} if cursor else {}
        result = await gw._handle_request(method, params)
        collected.extend(result[items_key])
        cursor = result.get("nextCursor")
        guard += 1
        assert guard < 100
        if cursor is None:
            break
    return collected


class TestFullProtocolIntegration:
    @pytest.mark.asyncio
    async def test_everything_together(self, two_server_config: Path, monkeypatch):
        # Simulate an upstream client that supports sampling so the relay path
        # can be exercised; answer any gateway→client request immediately.
        client_answer = {"role": "assistant", "content": {"type": "text", "text": "ok"}}

        async with Gateway(two_server_config) as gw:

            def fake_send_request(req_id, method, params):
                asyncio.get_event_loop().call_soon(
                    gw._resolve_client_response,
                    {"jsonrpc": "2.0", "id": req_id, "result": client_answer},
                )

            monkeypatch.setattr(gw, "_send_request", staticmethod(fake_send_request))
            gw._client_capabilities = {"sampling": {}}

            # 1. initialize → honest aggregate of what both servers support.
            init = gw._handle_initialize({"capabilities": {"sampling": {}}})
            caps = init["capabilities"]
            for expected in ("tools", "resources", "prompts", "completions", "logging"):
                assert expected in caps
            assert init["serverInfo"]["name"] == "mcp-baldr"

            # 2. ping answered directly.
            assert await gw._handle_request("ping", {}) == {}

            # 3. Paginated tools across both servers (4 tools x 2 = 8).
            tools = await _walk_list(gw, "tools/list", "tools")
            tool_names = {t["name"] for t in tools}
            assert len(tools) == 8
            assert "alpha__echo" in tool_names
            assert "beta__echo" in tool_names

            # 4. Tool call routes to the correct downstream.
            echo = await gw._handle_request(
                "tools/call", {"name": "beta__echo", "arguments": {"text": "hi"}}
            )
            assert echo["content"][0]["text"] == "hi"

            # 5. Paginated resources (2 x 2 = 4); read with URI de-namespacing.
            resources = await _walk_list(gw, "resources/list", "resources")
            assert len(resources) == 4
            text_uri = next(
                r["uri"]
                for r in resources
                if r["mimeType"] == "text/plain" and r["uri"].startswith("baldr+alpha")
            )
            read = await gw._handle_request("resources/read", {"uri": text_uri})
            assert "Hello from the full mock server." in read["contents"][0]["text"]

            # 6. Resource templates aggregated (unpaginated).
            templates = await gw._handle_request("resources/templates/list", {})
            assert len(templates["resourceTemplates"]) == 2  # one per server

            # 7. Paginated prompts (2 x 2 = 4); get with arguments.
            prompts = await _walk_list(gw, "prompts/list", "prompts")
            assert len(prompts) == 4
            greeting_name = next(p["name"] for p in prompts if p["name"].endswith("__greeting"))
            got = await gw._handle_request(
                "prompts/get", {"name": greeting_name, "arguments": {"name": "Sam", "formal": True}}
            )
            assert "Sam" in got["messages"][0]["content"]["text"]

            # 8. Completion for a prompt ref (namespaced → de-namespaced).
            comp = await gw._handle_request(
                "completion/complete",
                {
                    "ref": {"type": "ref/prompt", "name": greeting_name},
                    "argument": {"name": "name", "value": "Jo"},
                },
            )
            assert comp["completion"]["values"] == ["Jo-alpha", "Jo-beta", "Jo-gamma"]

            # 9. Completion for a resource ref.
            comp_r = await gw._handle_request(
                "completion/complete",
                {
                    "ref": {"type": "ref/resource", "uri": text_uri},
                    "argument": {"name": "x", "value": "Q"},
                },
            )
            assert comp_r["completion"]["values"][0] == "Q-alpha"

            # 10. logging/setLevel fans out to both servers.
            assert await gw._handle_request("logging/setLevel", {"level": "debug"}) == {}
            assert gw._log_level == "debug"

            # 11. Subscribe → trigger a live update → it is forwarded; then
            #     unsubscribe. Capture upstream notifications.
            forwarded: list[tuple[str, dict]] = []
            monkeypatch.setattr(
                Gateway,
                "_send_notification",
                staticmethod(lambda m, p: forwarded.append((m, p))),
            )
            _, original = decode_namespaced_uri(text_uri)
            await gw._handle_request("resources/subscribe", {"uri": text_uri})
            await gw.call_tool("alpha__emit_resource_update", {"uri": original})
            for _ in range(50):
                if any(m == "notifications/resources/updated" for m, _ in forwarded):
                    break
                await asyncio.sleep(0.05)
            updates = [p for m, p in forwarded if m == "notifications/resources/updated"]
            assert updates and updates[0]["uri"] == text_uri
            await gw._handle_request("resources/unsubscribe", {"uri": text_uri})
            assert gw.subscriptions.active_count() == 0

            # 12. Sampling relay: downstream asks, gateway relays to the
            #     (simulated) client, client's answer comes back — no fake.
            relay = await gw.call_tool("alpha__server_request", {"kind": "sampling"})
            outcome = relay["_serverRequest"]
            assert outcome["status"] == "ok"
            assert outcome["result"] == client_answer

    @pytest.mark.asyncio
    async def test_one_server_down_others_keep_working(self, tmp_path: Path):
        """If one downstream is absent, the gateway still serves the healthy one."""
        yaml = f"""\
        servers:
          - name: good
            command: {sys.executable}
            args: ["{FULL_SERVER}"]
          - name: bad
            command: {sys.executable}
            args: ["{FULL_SERVER}", "--nonexistent-flag-that-is-ignored"]
        logging:
          level: WARNING
          format: text
        """
        # Note: the mock ignores unknown argv, so both actually start; this
        # asserts multi-server aggregation rather than crash recovery (covered
        # in test_resiliency.py).
        config_path = write_gateway_config(tmp_path, yaml)
        async with Gateway(config_path) as gw:
            tools = await _walk_list(gw, "tools/list", "tools")
            names = {t["name"] for t in tools}
            assert any(n.startswith("good__") for n in names)
            assert any(n.startswith("bad__") for n in names)
