"""Tests for opaque cursor pagination of aggregated lists (Stage 8)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_baldr.gateway.server import Gateway
from mcp_baldr.protocol.pagination import (
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
    paginate,
)
from tests.conftest import write_gateway_config

ROOT = Path(__file__).resolve().parent.parent
FULL_SERVER = str(ROOT / "examples" / "full_mock_server.py")


def _config(tmp_path: Path, *, page_size: int, two: bool = False) -> Path:
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
        pagination:
          page_size: {page_size}
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
        pagination:
          page_size: {page_size}
        logging:
          level: WARNING
          format: text
        """
    return write_gateway_config(tmp_path, yaml)


# -- Cursor encode/decode -----------------------------------------------------


class TestCursor:
    def test_round_trip(self):
        cur = encode_cursor("full__echo")
        assert decode_cursor(cur) == "full__echo"

    def test_cursor_is_opaque_base64url(self):
        cur = encode_cursor("full__echo")
        # No raw id leaking in the clear; decodes only via decode_cursor.
        assert "full__echo" not in cur

    def test_rejects_garbage(self):
        with pytest.raises(InvalidCursorError):
            decode_cursor("!!!not base64!!!")

    def test_rejects_empty(self):
        with pytest.raises(InvalidCursorError):
            decode_cursor("")

    def test_rejects_valid_b64_wrong_shape(self):
        import base64

        bad = base64.urlsafe_b64encode(b'{"nope": 1}').decode()
        with pytest.raises(InvalidCursorError):
            decode_cursor(bad)

    def test_rejects_wrong_version(self):
        import base64
        import json

        raw = json.dumps({"v": 999, "after": "x"}).encode()
        bad = base64.urlsafe_b64encode(raw).decode()
        with pytest.raises(InvalidCursorError):
            decode_cursor(bad)


# -- paginate() ---------------------------------------------------------------


class TestPaginate:
    def _items(self, n: int) -> list[dict]:
        # Intentionally unsorted input to prove paginate sorts stably.
        return [{"name": f"s__t{i:02d}"} for i in reversed(range(n))]

    def test_single_page_no_cursor(self):
        page, nxt = paginate(self._items(3), "name", None, 10)
        assert [p["name"] for p in page] == ["s__t00", "s__t01", "s__t02"]
        assert nxt is None

    def test_walks_all_pages_without_gaps_or_dupes(self):
        items = self._items(10)
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            page, cursor = paginate(items, "name", cursor, 3)
            seen.extend(p["name"] for p in page)
            pages += 1
            if cursor is None:
                break
            assert pages < 100  # guard against infinite loop
        assert seen == sorted(it["name"] for it in items)
        assert len(seen) == len(set(seen)) == 10

    def test_stable_when_items_added_between_pages(self):
        items = self._items(6)  # s__t00..s__t05
        page1, cursor = paginate(items, "name", None, 3)
        assert [p["name"] for p in page1] == ["s__t00", "s__t01", "s__t02"]
        # A new item sorting *before* the cursor appears; it must NOT reappear
        # on the next page (cursor is id-based, not offset-based).
        items.append({"name": "s__t00b"})
        page2, _ = paginate(items, "name", cursor, 3)
        names = [p["name"] for p in page2]
        assert "s__t00b" not in names
        assert names[0] == "s__t03"

    def test_invalid_page_size(self):
        with pytest.raises(ValueError):
            paginate([], "name", None, 0)


# -- Gateway integration ------------------------------------------------------


class TestGatewayPagination:
    @pytest.mark.asyncio
    async def test_tools_list_paginates(self, tmp_path: Path):
        async with Gateway(_config(tmp_path, page_size=2)) as gw:
            total = len(await gw.list_tools())
            seen: list[str] = []
            cursor = None
            while True:
                params = {"cursor": cursor} if cursor else {}
                result = await gw._handle_request("tools/list", params)
                assert len(result["tools"]) <= 2
                seen.extend(t["name"] for t in result["tools"])
                cursor = result.get("nextCursor")
                if cursor is None:
                    break
            assert len(seen) == total
            assert len(set(seen)) == total  # no duplicates

    @pytest.mark.asyncio
    async def test_cross_downstream_aggregated_pages(self, tmp_path: Path):
        async with Gateway(_config(tmp_path, page_size=3, two=True)) as gw:
            total = len(await gw.list_tools())
            # Two full mocks → tools from both servers in one aggregated set.
            assert total == 8  # 4 tools x 2 servers
            seen: list[str] = []
            cursor = None
            while True:
                params = {"cursor": cursor} if cursor else {}
                result = await gw._handle_request("tools/list", params)
                seen.extend(t["name"] for t in result["tools"])
                cursor = result.get("nextCursor")
                if cursor is None:
                    break
            assert len(seen) == 8
            # Both servers' namespaces are represented.
            assert any(n.startswith("full__") for n in seen)
            assert any(n.startswith("second__") for n in seen)

    @pytest.mark.asyncio
    async def test_resources_and_prompts_list_paginate(self, tmp_path: Path):
        async with Gateway(_config(tmp_path, page_size=1)) as gw:
            r = await gw._handle_request("resources/list", {})
            assert len(r["resources"]) == 1
            assert "nextCursor" in r  # 2 resources, page_size 1

            p = await gw._handle_request("prompts/list", {})
            assert len(p["prompts"]) == 1
            assert "nextCursor" in p  # 2 prompts, page_size 1

    @pytest.mark.asyncio
    async def test_invalid_cursor_raises(self, tmp_path: Path):
        from mcp_baldr.gateway.server import InvalidCursorError as GwInvalidCursor

        async with Gateway(_config(tmp_path, page_size=2)) as gw:
            with pytest.raises(GwInvalidCursor):
                await gw._handle_request("tools/list", {"cursor": "garbage!!!"})

    @pytest.mark.asyncio
    async def test_default_page_size_from_config(self, tmp_path: Path):
        async with Gateway(_config(tmp_path, page_size=50)) as gw:
            assert gw._page_size == 50
