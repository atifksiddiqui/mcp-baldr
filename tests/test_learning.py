"""Tests for the MCP Learning Lab educational module.

Covers event collection, sensitive-data redaction, event correlation,
progress persistence, curriculum navigation, and — most importantly — proof
that the educational layer is fully isolated from gateway protocol
processing: with learning mode off, no events are produced and the gateway
behaves identically.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_baldr.gateway.server import Gateway
from mcp_baldr.learning import (
    CapabilityCategory,
    EducationalEvent,
    EventCollector,
    ProcessingStage,
    ProgressTracker,
    all_lessons,
    capability_status,
    get_lesson,
    redact,
    redact_env,
)
from mcp_baldr.learning.lessons import Level, SupportStatus
from mcp_baldr.learning.redaction import REDACTED
from tests.conftest import write_two_server_config

# -- Redaction (security-critical) --------------------------------------------


class TestRedaction:
    def test_masks_sensitive_key_names(self):
        data = {
            "api_key": "whatever",
            "token": "abc",
            "password": "hunter2",
            "authorization": "Bearer x",
            "note": "safe",
        }
        out = redact(data)
        assert out["api_key"] == REDACTED
        assert out["token"] == REDACTED
        assert out["password"] == REDACTED
        assert out["authorization"] == REDACTED
        assert out["note"] == "safe"

    def test_masks_secret_shaped_values_under_innocuous_keys(self):
        out = redact({"value": "Bearer sk-abcdefghijklmnopqrstuvwxyz0123456789"})
        assert out["value"] == REDACTED

    def test_masks_long_opaque_tokens(self):
        out = redact({"blob": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q7R8S9T0"})
        assert out["blob"] == REDACTED

    def test_recurses_into_nested_structures(self):
        data = {"outer": {"api_key": "secret", "ok": "fine"}, "list": [{"password": "p"}]}
        out = redact(data)
        assert out["outer"]["api_key"] == REDACTED
        assert out["outer"]["ok"] == "fine"
        assert out["list"][0]["password"] == REDACTED

    def test_truncates_long_strings(self):
        # A long, prose-like string (with spaces) is not token-shaped, so it
        # is truncated rather than fully redacted.
        long = "word " * 300
        out = redact({"content": long})
        assert out["content"].endswith("…(truncated)")
        assert len(out["content"]) < 300

    def test_does_not_mutate_input(self):
        data = {"api_key": "secret"}
        redact(data)
        assert data["api_key"] == "secret"  # original untouched

    def test_preserves_non_sensitive_scalars(self):
        out = redact({"count": 42, "ratio": 3.14, "flag": True, "none": None})
        assert out == {"count": 42, "ratio": 3.14, "flag": True, "none": None}

    def test_redact_env_masks_by_key_and_value(self):
        env = {
            "BRAVE_API_KEY": "sk-live-value",
            "PATH": "/usr/bin",
            "WEIRD": "Bearer sometokenvalue1234567890",
        }
        out = redact_env(env)
        assert out["BRAVE_API_KEY"] == REDACTED
        assert out["PATH"] == "/usr/bin"
        assert out["WEIRD"] == REDACTED


# -- Event model --------------------------------------------------------------


class TestEducationalEvent:
    def test_category_from_method(self):
        assert CapabilityCategory.from_method("initialize") is CapabilityCategory.INITIALIZATION
        assert CapabilityCategory.from_method("tools/call") is CapabilityCategory.TOOLS
        assert CapabilityCategory.from_method("resources/read") is CapabilityCategory.RESOURCES
        assert CapabilityCategory.from_method("prompts/get") is CapabilityCategory.PROMPTS
        assert (
            CapabilityCategory.from_method("notifications/initialized")
            is CapabilityCategory.NOTIFICATIONS
        )
        assert CapabilityCategory.from_method("ping") is CapabilityCategory.PING
        assert CapabilityCategory.from_method("unknown/thing") is CapabilityCategory.OTHER
        assert CapabilityCategory.from_method("") is CapabilityCategory.OTHER

    def test_to_dict_round_trip_fields(self):
        ev = EducationalEvent(
            event_id="e1",
            timestamp=1.0,
            session_id="s1",
            stage=ProcessingStage.CLIENT_REQUEST,
            category=CapabilityCategory.TOOLS,
            method="tools/call",
            request_id=5,
            is_simulated=False,
        )
        d = ev.to_dict()
        assert d["event_id"] == "e1"
        assert d["stage"] == "client_request"
        assert d["category"] == "tools"
        assert d["method"] == "tools/call"
        assert d["is_simulated"] is False

    def test_events_are_immutable(self):
        ev = EducationalEvent(
            event_id="e1",
            timestamp=1.0,
            session_id="s1",
            stage=ProcessingStage.ERROR,
            category=CapabilityCategory.ERROR,
        )
        with pytest.raises((AttributeError, TypeError)):
            ev.event_id = "mutated"  # type: ignore[misc]


# -- Event collector ----------------------------------------------------------


class TestEventCollector:
    def test_record_redacts_metadata(self):
        c = EventCollector("s1")
        c.record(
            ProcessingStage.DOWNSTREAM_REQUEST,
            method="tools/call",
            metadata={"arguments": {"api_key": "sk-secret1234567890abcd"}},
        )
        stored = c.all_events()[0]
        assert stored.metadata["arguments"]["api_key"] == REDACTED

    def test_correlation_measures_duration(self):
        c = EventCollector("s1")
        c.record(ProcessingStage.CLIENT_REQUEST, method="tools/call", request_id=1)
        ev = c.record(ProcessingStage.CLIENT_RESPONSE, method="tools/call", request_id=1)
        assert ev.duration_ms is not None
        assert ev.duration_ms >= 0

    def test_correlated_returns_lifecycle(self):
        c = EventCollector("s1")
        c.record(ProcessingStage.CLIENT_REQUEST, method="tools/call", request_id=7)
        c.record(ProcessingStage.NAMESPACE_RESOLUTION, request_id=7)
        c.record(ProcessingStage.CLIENT_RESPONSE, method="tools/call", request_id=7)
        # Unrelated event with different id must not appear.
        c.record(ProcessingStage.CLIENT_REQUEST, method="tools/list", request_id=8)
        lifecycle = c.correlated(7)
        assert len(lifecycle) == 3
        assert all(e.request_id == 7 for e in lifecycle)

    def test_filter_by_category(self):
        c = EventCollector("s1")
        c.record(ProcessingStage.CLIENT_REQUEST, method="tools/call", request_id=1)
        c.record(ProcessingStage.CLIENT_REQUEST, method="resources/read", request_id=2)
        tools = c.filter(category=CapabilityCategory.TOOLS)
        assert len(tools) == 1
        assert tools[0].method == "tools/call"

    def test_filter_by_simulated(self):
        c = EventCollector("s1")
        c.record(ProcessingStage.CLIENT_REQUEST, method="ping", request_id=1, is_simulated=True)
        c.record(ProcessingStage.CLIENT_REQUEST, method="ping", request_id=2, is_simulated=False)
        assert len(c.filter(simulated=True)) == 1
        assert len(c.filter(simulated=False)) == 1

    def test_error_stage_forces_error_category(self):
        c = EventCollector("s1")
        ev = c.record(ProcessingStage.ERROR, method="tools/call", request_id=1)
        assert ev.category is CapabilityCategory.ERROR

    def test_bounded_history(self):
        c = EventCollector("s1", max_events=3)
        for i in range(10):
            c.record(ProcessingStage.CLIENT_REQUEST, method="ping", request_id=i)
        assert len(c) == 3
        # Only the last 3 survive.
        ids = [e.request_id for e in c.all_events()]
        assert ids == [7, 8, 9]

    def test_get_by_id(self):
        c = EventCollector("s1")
        ev = c.record(ProcessingStage.GATEWAY_READY)
        assert c.get(ev.event_id) is ev
        assert c.get("nonexistent") is None

    def test_clear(self):
        c = EventCollector("s1")
        c.record(ProcessingStage.GATEWAY_READY)
        c.clear()
        assert len(c) == 0


# -- Progress tracking --------------------------------------------------------


class TestProgress:
    def test_complete_lesson_persists(self, tmp_path: Path):
        path = tmp_path / "progress.json"
        t = ProgressTracker(path)
        t.complete_lesson("fundamentals.what-is-mcp")
        # Reload from disk to prove persistence.
        t2 = ProgressTracker(path)
        assert t2.is_lesson_complete("fundamentals.what-is-mcp")

    def test_quiz_keeps_best_score(self, tmp_path: Path):
        path = tmp_path / "progress.json"
        t = ProgressTracker(path)
        t.record_quiz("q1", 0.5)
        t.record_quiz("q1", 0.8)
        t.record_quiz("q1", 0.6)
        assert t.state.quiz_scores["q1"] == 0.8

    def test_corrupt_file_tolerated(self, tmp_path: Path):
        path = tmp_path / "progress.json"
        path.write_text("{not valid json")
        t = ProgressTracker(path)  # must not raise
        assert t.summary()["lessons_completed"] == 0

    def test_missing_file_starts_empty(self, tmp_path: Path):
        t = ProgressTracker(tmp_path / "does_not_exist.json")
        assert t.summary()["lessons_completed"] == 0

    def test_summary_average(self, tmp_path: Path):
        t = ProgressTracker(tmp_path / "p.json")
        t.record_quiz("q1", 0.6)
        t.record_quiz("q2", 0.8)
        assert t.summary()["average_quiz_score"] == 0.7


# -- Curriculum ---------------------------------------------------------------


class TestCurriculum:
    def test_lessons_exist(self):
        lessons = all_lessons()
        assert len(lessons) >= 4
        ids = {lesson.lesson_id for lesson in lessons}
        assert "fundamentals.what-is-mcp" in ids

    def test_get_lesson(self):
        lesson = get_lesson("fundamentals.json-rpc")
        assert lesson is not None
        assert lesson.title
        assert get_lesson("nope") is None

    def test_lesson_has_all_levels(self):
        for lesson in all_lessons():
            for level in Level:
                assert lesson.explanation(level), f"{lesson.lesson_id} missing {level}"

    def test_capability_status_is_honest(self):
        # Fully implemented capabilities (Stages 1-6).
        assert capability_status("tools/call") is SupportStatus.IMPLEMENTED
        assert capability_status("initialize") is SupportStatus.IMPLEMENTED
        assert capability_status("resources/read") is SupportStatus.IMPLEMENTED
        assert capability_status("prompts/get") is SupportStatus.IMPLEMENTED
        assert capability_status("completion/complete") is SupportStatus.IMPLEMENTED
        assert capability_status("logging/setLevel") is SupportStatus.IMPLEMENTED
        assert capability_status("ping") is SupportStatus.IMPLEMENTED
        # Server→client requests are RELAYED, not provided: honestly PARTIAL,
        # never claimed as fully implemented (the gateway is a conduit).
        assert capability_status("sampling/createMessage") is SupportStatus.PARTIAL
        assert capability_status("elicitation/create") is SupportStatus.PARTIAL
        assert capability_status("roots/list") is SupportStatus.PARTIAL
        # Unknown methods default to not-implemented (never overclaim).
        assert capability_status("made/up") is SupportStatus.NOT_IMPLEMENTED

    def test_source_refs_point_to_real_files(self):
        project_root = Path(__file__).resolve().parent.parent
        for lesson in all_lessons():
            for ref in lesson.source_refs:
                assert (project_root / ref.path).exists(), f"missing source file: {ref.path}"


# -- Gateway integration & isolation ------------------------------------------


class TestGatewayIntegration:
    @pytest.mark.asyncio
    async def test_collector_captures_live_events(self, tmp_path: Path):
        config_path = write_two_server_config(tmp_path)
        async with Gateway(config_path, force_learning=True) as gw:
            assert gw.collector is not None
            await gw.call_tool("mock_a__echo", {"text": "hi"})
            events = gw.collector.all_events()
            stages = {e.stage for e in events}
            assert ProcessingStage.GATEWAY_READY in stages
            assert ProcessingStage.NAMESPACE_RESOLUTION in stages
            assert ProcessingStage.DOWNSTREAM_REQUEST in stages
            assert ProcessingStage.DOWNSTREAM_RESPONSE in stages
            # Every live event is marked non-simulated.
            assert all(not e.is_simulated for e in events)

    @pytest.mark.asyncio
    async def test_live_events_redact_secrets(self, tmp_path: Path):
        config_path = write_two_server_config(tmp_path)
        async with Gateway(config_path, force_learning=True) as gw:
            await gw.call_tool("mock_a__echo", {"text": "hi", "api_key": "sk-secret1234567890abcd"})
            dreq = gw.collector.filter(stage=ProcessingStage.DOWNSTREAM_REQUEST)
            assert dreq
            assert dreq[0].metadata["arguments"]["api_key"] == REDACTED

    @pytest.mark.asyncio
    async def test_disabled_produces_no_collector(self, tmp_path: Path):
        config_path = write_two_server_config(tmp_path)
        # No force_learning, and the config's learning_mode defaults to false.
        async with Gateway(config_path) as gw:
            assert gw.collector is None
            assert gw.inspector is None
            # Gateway still works identically.
            result = await gw.call_tool("mock_a__echo", {"text": "works"})
            assert result["content"][0]["text"] == "works"

    @pytest.mark.asyncio
    async def test_disabled_routing_identical(self, tmp_path: Path):
        """With learning off, routing behavior is unchanged."""
        config_path = write_two_server_config(tmp_path)
        async with Gateway(config_path) as gw:
            tools = await gw.list_tools()
            names = sorted(t["name"] for t in tools)
            assert names == [
                "mock_a__add",
                "mock_a__echo",
                "mock_b__greet",
                "mock_b__multiply",
            ]
            add = await gw.call_tool("mock_a__add", {"a": 2, "b": 3})
            assert add["content"][0]["text"] == "5"


# -- learn CLI (smoke) --------------------------------------------------------


class TestLearnCLI:
    def _run(self, argv: list[str]) -> int:
        import importlib.util

        script = Path(__file__).resolve().parent.parent / "scripts" / "learn.py"
        spec = importlib.util.spec_from_file_location("_learn_cli", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["_learn_cli"] = module
        spec.loader.exec_module(module)
        return module.main(argv)

    def test_lessons_command(self, capsys):
        assert self._run(["lessons"]) == 0
        out = capsys.readouterr().out
        assert "MCP Fundamentals" in out

    def test_capabilities_command(self, capsys):
        assert self._run(["capabilities"]) == 0
        out = capsys.readouterr().out
        assert "tools/call" in out
        # The matrix shows real statuses; relayed server→client requests are
        # honestly labeled "partial", fully-served methods "implemented".
        assert "implemented" in out
        assert "partial" in out

    def test_lesson_command(self, capsys):
        assert self._run(["lesson", "fundamentals.what-is-mcp", "--level", "advanced"]) == 0
        out = capsys.readouterr().out
        assert "What is MCP?" in out

    def test_unknown_lesson_returns_error(self, capsys):
        assert self._run(["lesson", "nope"]) == 1
