"""Tests for completion + logging aggregation (Stage 6)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp_baldr.gateway.server import (
    Gateway,
    InvalidParamsError,
    PromptNotFoundError,
    ResourceNotFoundError,
)
from mcp_baldr.gateway.server import Gateway as GatewayClass
from mcp_baldr.protocol.uri import encode_namespaced_uri
from tests.conftest import write_gateway_config

ROOT = Path(__file__).resolve().parent.parent
FULL_SERVER = str(ROOT / "examples" / "full_mock_server.py")


def _config(tmp_path: Path, *, two: bool = False) -> Path:
    if two:
        yaml = f"""\
        servers:
          - name: full
            command: {sys.executable}
            args: ["{FULL_SERVER}"]
          - name: second
            command: {sys.executable}
            args: ["{FULL_SERVER}"]
            env:
              MOCK_SERVER_NAME: second-mock
        logging:
          level: WARNING
          format: text
        """
    else:
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


# -- Capability advertising ---------------------------------------------------


class TestCapabilityAdvertising:
    @pytest.mark.asyncio
    async def test_completions_and_logging_advertised(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            caps = gw._handle_initialize({})["capabilities"]
            assert "completions" in caps
            assert "logging" in caps


# -- Completion ---------------------------------------------------------------


class TestCompletion:
    @pytest.mark.asyncio
    async def test_complete_prompt_ref(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            prompts = await gw.list_prompts()
            namespaced = prompts[0]["name"]  # e.g. full__greeting
            result = await gw.complete(
                {
                    "ref": {"type": "ref/prompt", "name": namespaced},
                    "argument": {"name": "name", "value": "Al"},
                }
            )
            # Mock echoes completions derived from the partial value.
            values = result["completion"]["values"]
            assert values == ["Al-alpha", "Al-beta", "Al-gamma"]
            # Downstream saw the de-namespaced ref type.
            assert result["_refType"] == "ref/prompt"

    @pytest.mark.asyncio
    async def test_complete_resource_ref(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            resources = await gw.list_resources()
            uri = resources[0]["uri"]
            result = await gw.complete(
                {
                    "ref": {"type": "ref/resource", "uri": uri},
                    "argument": {"name": "x", "value": "foo"},
                }
            )
            assert result["completion"]["values"] == ["foo-alpha", "foo-beta", "foo-gamma"]
            assert result["_refType"] == "ref/resource"

    @pytest.mark.asyncio
    async def test_complete_unknown_prompt_raises(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            with pytest.raises(PromptNotFoundError):
                await gw.complete(
                    {"ref": {"type": "ref/prompt", "name": "full__nope"}, "argument": {}}
                )

    @pytest.mark.asyncio
    async def test_complete_unknown_resource_raises(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            fake = encode_namespaced_uri("full", "mock://nope")
            with pytest.raises(ResourceNotFoundError):
                await gw.complete({"ref": {"type": "ref/resource", "uri": fake}, "argument": {}})

    @pytest.mark.asyncio
    async def test_complete_bad_ref_type_raises(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            with pytest.raises(InvalidParamsError):
                await gw.complete({"ref": {"type": "ref/bogus"}, "argument": {}})

    @pytest.mark.asyncio
    async def test_complete_missing_ref_raises(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            with pytest.raises(InvalidParamsError):
                await gw.complete({"argument": {}})

    @pytest.mark.asyncio
    async def test_complete_resource_ref_rejects_non_namespaced_uri(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            with pytest.raises(InvalidParamsError):
                await gw.complete(
                    {"ref": {"type": "ref/resource", "uri": "mock://docs/readme.txt"}}
                )


# -- Logging ------------------------------------------------------------------


class TestLogging:
    @pytest.mark.asyncio
    async def test_set_level_records_level(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            await gw.set_logging_level("debug")
            assert gw._log_level == "debug"

    @pytest.mark.asyncio
    async def test_set_level_handler_validates(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            with pytest.raises(InvalidParamsError):
                await gw._handle_logging_set_level({})

    @pytest.mark.asyncio
    async def test_set_level_fans_out_to_all_logging_servers(self, tmp_path: Path):
        async with Gateway(_config(tmp_path, two=True)) as gw:
            targets = gw.capabilities.servers_supporting("logging")
            assert set(targets) == {"full", "second"}
            # Should not raise even with two downstreams.
            await gw.set_logging_level("info")

    @pytest.mark.asyncio
    async def test_log_message_forwarded_and_redacted(self, tmp_path: Path, monkeypatch):
        """Downstream notifications/message is redacted + namespaced upstream."""
        forwarded: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            GatewayClass,
            "_send_notification",
            staticmethod(lambda m, p: forwarded.append((m, p))),
        )

        async with Gateway(_config(tmp_path)) as gw:
            # setLevel makes the mock emit a notifications/message.
            await gw.set_logging_level("warning")
            for _ in range(50):
                if any(m == "notifications/message" for m, _ in forwarded):
                    break
                await asyncio.sleep(0.05)

            msgs = [p for m, p in forwarded if m == "notifications/message"]
            assert msgs, "expected a forwarded log message"
            msg = msgs[0]
            # logger name is namespaced with the originating server.
            assert msg["logger"].startswith("full/")
            assert msg["level"] == "warning"

    @pytest.mark.asyncio
    async def test_log_message_secrets_redacted(self, tmp_path: Path, monkeypatch):
        """Secret-shaped values in a log message's data are masked."""
        from mcp_baldr.learning.redaction import REDACTED

        forwarded: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            GatewayClass,
            "_send_notification",
            staticmethod(lambda m, p: forwarded.append((m, p))),
        )

        async with Gateway(_config(tmp_path)) as gw:
            # Directly exercise the forwarding path with a sensitive payload.
            gw._forward_log_message(
                "full",
                {
                    "level": "info",
                    "logger": "auth",
                    "data": {"api_key": "super-secret-value", "msg": "ok"},
                },
            )
            msgs = [p for m, p in forwarded if m == "notifications/message"]
            assert msgs
            assert msgs[0]["data"]["api_key"] == REDACTED
            assert msgs[0]["data"]["msg"] == "ok"
            assert msgs[0]["logger"] == "full/auth"


# -- Integration: dispatch through the router ---------------------------------


class TestRouterDispatch:
    @pytest.mark.asyncio
    async def test_completion_via_handle_request(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            prompts = await gw.list_prompts()
            namespaced = prompts[0]["name"]
            result = await gw._handle_request(
                "completion/complete",
                {
                    "ref": {"type": "ref/prompt", "name": namespaced},
                    "argument": {"name": "name", "value": "Z"},
                },
            )
            assert result["completion"]["values"][0] == "Z-alpha"

    @pytest.mark.asyncio
    async def test_logging_via_handle_request(self, tmp_path: Path):
        async with Gateway(_config(tmp_path)) as gw:
            result = await gw._handle_request("logging/setLevel", {"level": "error"})
            assert result == {}
            assert gw._log_level == "error"
