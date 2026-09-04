"""Unit tests for the side-effect-free plan-mode helpers (agentos.plan_mode)."""

from __future__ import annotations

import json
import time

import pytest

from agentos.plan_mode import (
    PLAN_MODE_TOOL_ALLOW,
    PLAN_STATUS_PRESENTED,
    PlanModeStore,
    build_plan_presented_payload,
    exit_plan_payload_terminates_turn,
    format_plan_as_text,
    plan_from_tool_result,
    validate_plan,
)


class TestPlanModeStore:
    def test_enable_disable_roundtrip(self) -> None:
        store = PlanModeStore()
        key = "agent:main:t"
        assert store.is_enabled(key) is False
        store.enable(key)
        assert store.is_enabled(key) is True
        # No TTL: still on until an explicit disable.
        assert store.get(key) is not None
        assert store.disable(key) is True
        assert store.is_enabled(key) is False
        assert store.disable(key) is False

    def test_enable_requires_a_key(self) -> None:
        with pytest.raises(ValueError, match="session_key"):
            PlanModeStore().enable("  ")

    def test_sessions_are_independent(self) -> None:
        store = PlanModeStore()
        store.enable("agent:main:a")
        assert store.is_enabled("agent:main:b") is False


class TestAllowlist:
    def test_contains_only_read_research_tools(self) -> None:
        forbidden = {
            "write_file",
            "edit_file",
            "apply_patch",
            "exec_command",
            "execute_code",
            "background_process",
            "git_commit",
            "message",
            "publish_artifact",
            "cron",
            "gateway",
            "memory",
            "memory_save",
            "sessions_send",
            "sessions_spawn",
            "skill_create",
            "skill_edit",
            "skill_delete",
            "image_generate",
        }
        assert not (PLAN_MODE_TOOL_ALLOW & forbidden)

    def test_contains_the_exit_door_and_the_question_tool(self) -> None:
        assert "exit_plan_mode" in PLAN_MODE_TOOL_ALLOW
        assert "ask_user" in PLAN_MODE_TOOL_ALLOW


class TestPayloadHelpers:
    def test_validate_plan_normalizes_and_rejects(self) -> None:
        assert validate_plan("  do X  ") == "do X"
        with pytest.raises(ValueError, match="non-empty"):
            validate_plan("   ")
        with pytest.raises(ValueError, match="exceeds"):
            validate_plan("x" * 40_001)

    def test_presented_payload_terminates_turn(self) -> None:
        payload = build_plan_presented_payload("## Plan\n1. Do X")
        assert payload["status"] == PLAN_STATUS_PRESENTED
        assert exit_plan_payload_terminates_turn(json.dumps(payload)) is True
        assert exit_plan_payload_terminates_turn(payload) is True
        assert exit_plan_payload_terminates_turn("not json") is False
        assert exit_plan_payload_terminates_turn(json.dumps({"status": "error"})) is False

    def test_plan_from_tool_result_gates_on_tool_and_status(self) -> None:
        payload = json.dumps(build_plan_presented_payload("Do X"))
        assert plan_from_tool_result("exit_plan_mode", payload) == "Do X"
        assert plan_from_tool_result("ask_user", payload) is None
        assert plan_from_tool_result("exit_plan_mode", '{"status": "error"}') is None

    def test_format_plan_as_text_carries_the_approval_hint(self) -> None:
        text = format_plan_as_text("1. Do X")
        assert "1. Do X" in text
        assert "/plan off" in text


# ======================================================================
# PlanModeStore memory-leak tests (_sessions never evicted)
# ======================================================================


class TestPlanModeStoreEviction:
    def test_evict_stale_empty(self) -> None:
        store = PlanModeStore()
        assert store._evict_stale() == 0

    def test_fresh_entries_survive_eviction(self) -> None:
        store = PlanModeStore(session_ttl=60.0)
        store.enable("k1")
        store.enable("k2")
        assert store._evict_stale() == 0
        assert store.is_enabled("k1")
        assert store.is_enabled("k2")

    def test_stale_entries_evicted(self) -> None:
        store = PlanModeStore(session_ttl=0.01)
        store.enable("k1")
        time
        time.sleep(0.02)
        assert store._evict_stale() == 1
        assert not store.is_enabled("k1")

    def test_mixed_stale_and_fresh(self) -> None:
        store = PlanModeStore(session_ttl=0.03)
        store.enable("old")
        time
        time.sleep(0.04)
        store.enable("recent")
        evicted = store._evict_stale()
        assert evicted == 1
        assert not store.is_enabled("old")
        assert store.is_enabled("recent")

    def test_reap_public_entry_point(self) -> None:
        store = PlanModeStore(session_ttl=0.01)
        store.enable("k1")
        time
        time.sleep(0.02)
        assert store.reap() == 1
        assert not store.is_enabled("k1")

    def test_reap_no_stale(self) -> None:
        store = PlanModeStore()
        assert store.reap() == 0

    def test_disable_clears_timestamp(self) -> None:
        store = PlanModeStore()
        store.enable("k1")
        store.disable("k1")
        assert "k1" not in store._enabled_at

    def test_disable_then_re_enable(self) -> None:
        store = PlanModeStore()
        store.enable("k1")
        store.disable("k1")
        store.enable("k1")
        assert store.is_enabled("k1")

    def test_constructor_default_ttl(self) -> None:
        from agentos.plan_mode import _DEFAULT_SESSION_TTL_SECONDS
        assert _DEFAULT_SESSION_TTL_SECONDS > 0

    def test_custom_ttl_accepted(self) -> None:
        store = PlanModeStore(session_ttl=3600.0)
        assert store._session_ttl == 3600.0

    def test_zero_ttl_disables_eviction(self) -> None:
        store = PlanModeStore(session_ttl=0.0)
        store.enable("k1")
        time
        time.sleep(0.02)
        assert store._evict_stale() == 0
        assert store.is_enabled("k1")
