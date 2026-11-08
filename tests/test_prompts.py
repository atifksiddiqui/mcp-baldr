"""Tests for prompt aggregation (Stage 4): registry + routing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_baldr.gateway.server import Gateway, InvalidParamsError, PromptNotFoundError
from mcp_baldr.registry.prompt_registry import PromptRegistry
from tests.conftest import write_gateway_config

ROOT = Path(__file__).resolve().parent.parent
FULL_SERVER = str(ROOT / "examples" / "full_mock_server.py")
BASIC_SERVER = str(ROOT / "examples" / "mock_server.py")


# -- PromptRegistry -----------------------------------------------------------


class TestPromptRegistry:
    def test_register_and_resolve(self):
        reg = PromptRegistry()
        reg.register_server_prompts(
            "srv",
            [
                {
                    "name": "greeting",
                    "description": "Greet someone.",
                    "arguments": [{"name": "name", "required": True}],
                }
            ],
        )
        client = reg.list_for_client()
        assert len(client) == 1
        assert client[0]["name"] == "srv__greeting"
        # Arguments + description preserved verbatim.
        assert client[0]["description"] == "Greet someone."
        assert client[0]["arguments"] == [{"name": "name", "required": True}]
        assert reg.resolve("srv__greeting") == ("srv", "greeting")

    def test_collision_across_servers(self):
        reg = PromptRegistry()
        reg.register_server_prompts("a", [{"name": "greeting"}])
        reg.register_server_prompts("b", [{"name": "greeting"}])
        assert len(reg) == 2
        assert reg.resolve("a__greeting") == ("a", "greeting")
        assert reg.resolve("b__greeting") == ("b", "greeting")

    def test_refresh_replaces(self):
        reg = PromptRegistry()
        reg.register_server_prompts("srv", [{"name": "a"}, {"name": "b"}])
        reg.register_server_prompts("srv", [{"name": "c"}])
        assert len(reg) == 1
        assert reg.resolve("srv__c") == ("srv", "c")
        assert reg.resolve("srv__a") is None

    def test_unregister(self):
        reg = PromptRegistry()
        reg.register_server_prompts("a", [{"name": "x"}])
        reg.register_server_prompts("b", [{"name": "y"}])
        reg.unregister_server("a")
        assert reg.resolve("a__x") is None
        assert reg.resolve("b__y") == ("b", "y")


# -- Gateway integration ------------------------------------------------------


class TestGatewayPrompts:
    def _config(self, tmp_path: Path) -> Path:
        return write_gateway_config(
            tmp_path,
            f"""\
            servers:
              - name: full
                command: {sys.executable}
                args: ["{FULL_SERVER}"]
              - name: basic
                command: {sys.executable}
                args: ["{BASIC_SERVER}"]
            logging:
              level: WARNING
              format: text
            """,
        )

    @pytest.mark.asyncio
    async def test_prompts_capability_advertised(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            caps = gw._handle_initialize({})["capabilities"]
            assert "prompts" in caps  # full advertises prompts
            assert "tools" in caps
            assert "resources" in caps

    @pytest.mark.asyncio
    async def test_list_prompts_namespaced(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            prompts = await gw.list_prompts()
            names = {p["name"] for p in prompts}
            # full mock exposes greeting + summarize; basic exposes none.
            assert names == {"full__greeting", "full__summarize"}

    @pytest.mark.asyncio
    async def test_get_prompt_routes_and_preserves_messages(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            result = await gw.get_prompt("full__greeting", {"name": "Sid"})
            # Message structure + content blocks preserved verbatim.
            assert result["messages"][0]["role"] == "user"
            assert result["messages"][0]["content"]["type"] == "text"
            assert result["messages"][0]["content"]["text"] == "Hey Sid!"

    @pytest.mark.asyncio
    async def test_get_prompt_with_formal_argument(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            result = await gw.get_prompt("full__greeting", {"name": "Sid", "formal": True})
            assert result["messages"][0]["content"]["text"] == "Good day, Sid."

    @pytest.mark.asyncio
    async def test_get_unknown_prompt_raises(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            with pytest.raises(PromptNotFoundError):
                await gw.get_prompt("full__does_not_exist", {})

    @pytest.mark.asyncio
    async def test_get_prompt_missing_name_raises(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            with pytest.raises(InvalidParamsError):
                await gw._handle_prompts_get({"arguments": {}})

    @pytest.mark.asyncio
    async def test_tools_and_resources_still_work(self, tmp_path: Path):
        async with Gateway(self._config(tmp_path)) as gw:
            tool_result = await gw.call_tool("basic__echo", {"text": "hi"})
            assert tool_result["content"][0]["text"] == "hi"
            resources = await gw.list_resources()
            assert len(resources) == 2
