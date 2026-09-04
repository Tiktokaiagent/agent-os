"""Tests for ApprovalQueue dict-leak fix (_session_elevated_modes, _node_settings).

Two unbounded dicts in ApprovalQueue grow with distinct sessions/nodes and are
never evicted on production gateways.  This test suite verifies TTL-based
eviction, explicit remove methods, and evict-on-read semantics — the same
conditional-safety pattern used in Carlys17's split-brain lock fix (#1080).

Design: each dict leak is independent but the eviction machinery is shared
(``_is_entry_stale``, ``_evict_stale_*``, ``reap()``).  Tests are grouped by
layer to make the coverage gap obvious.
"""

from __future__ import annotations

import time

from agentos.application.approval_queue import ApprovalQueue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_queue(**overrides: object) -> ApprovalQueue:
    """Create a queue with a non-default TTL so tests don't wait 24 h."""
    return ApprovalQueue(
        db_path=":memory:",
        session_elevated_ttl=overrides.get("session_elevated_ttl", 60.0),
        node_settings_ttl=overrides.get("node_settings_ttl", 60.0),
    )


# ======================================================================
# 1.  _evict_stale_session_modes  — batch eviction
# ======================================================================


class TestEvictStaleSessionModes:
    def test_no_entries_nothing_evicted(self) -> None:
        q = _make_queue()
        assert q._evict_stale_session_modes() == 0

    def test_fresh_entries_survive(self) -> None:
        q = _make_queue()
        q.set_elevated_mode("s1", "on")
        q.set_elevated_mode("s2", "full")
        assert q._evict_stale_session_modes() == 0
        assert q._session_elevated_modes == {"s1": "on", "s2": "full"}

    def test_stale_entries_evicted(self) -> None:
        q = _make_queue(session_elevated_ttl=0.01)
        q.set_elevated_mode("s1", "on")
        time.sleep(0.02)
        assert q._evict_stale_session_modes() == 1
        assert "s1" not in q._session_elevated_modes

    def test_mixed_stale_and_fresh(self) -> None:
        q = _make_queue(session_elevated_ttl=0.03)
        q.set_elevated_mode("old", "on")
        time.sleep(0.04)
        q.set_elevated_mode("recent", "bypass")
        evicted = q._evict_stale_session_modes()
        assert evicted == 1
        assert "old" not in q._session_elevated_modes
        assert q._session_elevated_modes["recent"] == "bypass"

    def test_set_at_deleted_alongside_value(self) -> None:
        q = _make_queue(session_elevated_ttl=0.01)
        q.set_elevated_mode("s1", "on")
        time.sleep(0.02)
        q._evict_stale_session_modes()
        assert "s1" not in q._session_elevated_set_at

    def test_ttl_of_zero_disables_eviction(self) -> None:
        q = _make_queue(session_elevated_ttl=0.0)
        q.set_elevated_mode("s1", "on")
        time.sleep(0.01)
        assert q._evict_stale_session_modes() == 0
        assert "s1" in q._session_elevated_modes


# ======================================================================
# 2.  _evict_stale_node_settings  — batch eviction
# ======================================================================


class TestEvictStaleNodeSettings:
    def test_no_entries_nothing_evicted(self) -> None:
        q = _make_queue()
        assert q._evict_stale_node_settings() == 0

    def test_fresh_settings_survive(self) -> None:
        q = _make_queue()
        q.set_settings("auto-approve", node_id="node-a")
        q.set_settings("auto-deny", node_id="node-b")
        assert q._evict_stale_node_settings() == 0
        assert "node-a" in q._node_settings
        assert "node-b" in q._node_settings

    def test_stale_settings_evicted(self) -> None:
        q = _make_queue(node_settings_ttl=0.01)
        q.set_settings("auto-approve", node_id="node-a")
        time.sleep(0.02)
        assert q._evict_stale_node_settings() == 1
        assert "node-a" not in q._node_settings

    def test_mixed_stale_and_fresh_nodes(self) -> None:
        q = _make_queue(node_settings_ttl=0.03)
        q.set_settings("auto-approve", node_id="old-node")
        time.sleep(0.04)
        q.set_settings("prompt", node_id="recent-node")
        evicted = q._evict_stale_node_settings()
        assert evicted == 1
        assert "old-node" not in q._node_settings
        assert "recent-node" in q._node_settings

    def test_set_at_timestamp_evicted_alongside_value(self) -> None:
        q = _make_queue(node_settings_ttl=0.01)
        q.set_settings("auto-approve", node_id="node-a")
        time.sleep(0.02)
        q._evict_stale_node_settings()
        assert "node-a" not in q._node_settings_set_at

    def test_ttl_of_zero_disables_node_eviction(self) -> None:
        q = _make_queue(node_settings_ttl=0.0)
        q.set_settings("auto-approve", node_id="node-a")
        time.sleep(0.01)
        assert q._evict_stale_node_settings() == 0
        assert "node-a" in q._node_settings


# ======================================================================
# 3.  reap() — public batch entry point
# ======================================================================


class TestReap:
    def test_reap_empty(self) -> None:
        q = _make_queue()
        assert q.reap() == (0, 0)

    def test_reap_only_stale_modes(self) -> None:
        q = _make_queue(session_elevated_ttl=0.01, node_settings_ttl=999.0)
        q.set_elevated_mode("s1", "on")
        time.sleep(0.02)
        mode_count, node_count = q.reap()
        assert mode_count == 1
        assert node_count == 0
        assert "s1" not in q._session_elevated_modes

    def test_reap_only_stale_nodes(self) -> None:
        q = _make_queue(session_elevated_ttl=999.0, node_settings_ttl=0.01)
        q.set_settings("auto-approve", node_id="node-a")
        time.sleep(0.02)
        mode_count, node_count = q.reap()
        assert mode_count == 0
        assert node_count == 1
        assert "node-a" not in q._node_settings

    def test_reap_both_stale(self) -> None:
        q = _make_queue(session_elevated_ttl=0.01, node_settings_ttl=0.01)
        q.set_elevated_mode("s1", "on")
        q.set_settings("auto-approve", node_id="node-a")
        time.sleep(0.02)
        mode_count, node_count = q.reap()
        assert mode_count == 1
        assert node_count == 1


# ======================================================================
# 4.  clear_elevated_mode  — explicit remove
# ======================================================================


class TestClearElevatedMode:
    def test_clear_existing_entry(self) -> None:
        q = _make_queue()
        q.set_elevated_mode("s1", "on")
        q.clear_elevated_mode("s1")
        assert "s1" not in q._session_elevated_modes
        assert "s1" not in q._session_elevated_set_at

    def test_clear_missing_entry_no_error(self) -> None:
        q = _make_queue()
        q.clear_elevated_mode("unknown")  # should not raise

    def test_clear_then_recreate(self) -> None:
        q = _make_queue()
        q.set_elevated_mode("s1", "on")
        q.clear_elevated_mode("s1")
        q.set_elevated_mode("s1", "bypass")
        assert q._session_elevated_modes["s1"] == "bypass"
        assert "s1" in q._session_elevated_set_at


# ======================================================================
# 5.  remove_node_settings  — explicit remove
# ======================================================================


class TestRemoveNodeSettings:
    def test_remove_existing_node(self) -> None:
        q = _make_queue()
        q.set_settings("auto-deny", node_id="node-a")
        q.remove_node_settings("node-a")
        assert "node-a" not in q._node_settings
        assert "node-a" not in q._node_settings_set_at

    def test_remove_missing_node_no_error(self) -> None:
        q = _make_queue()
        q.remove_node_settings("ghost")  # should not raise

    def test_remove_node_does_not_affect_global(self) -> None:
        q = _make_queue()
        q.set_settings("auto-approve", node_id="node-a")
        q.set_settings("prompt")  # global
        q.remove_node_settings("node-a")
        assert q._global_settings.mode == "prompt"
        assert "node-a" not in q._node_settings


# ======================================================================
# 6.  set_elevated_mode with mode=off
# ======================================================================


class TestSetElevatedOff:
    def test_off_removes_value_and_timestamp(self) -> None:
        q = _make_queue()
        q.set_elevated_mode("s1", "on")
        q.set_elevated_mode("s1", "off")
        assert "s1" not in q._session_elevated_modes
        assert "s1" not in q._session_elevated_set_at

    def test_off_for_never_set_no_error(self) -> None:
        q = _make_queue()
        q.set_elevated_mode("ghost", "off")  # should not raise


# ======================================================================
# 7.  get_elevated_mode  — evict-on-read
# ======================================================================


class TestGetElevatedModeEvictOnRead:
    def test_fresh_entry_returned(self) -> None:
        q = _make_queue()
        q.set_elevated_mode("s1", "full")
        assert q.get_elevated_mode("s1") == "full"

    def test_stale_entry_evicted_on_read(self) -> None:
        q = _make_queue(session_elevated_ttl=0.01)
        q.set_elevated_mode("s1", "full")
        time.sleep(0.02)
        assert q.get_elevated_mode("s1") is None
        assert "s1" not in q._session_elevated_modes

    def test_stale_entry_preserved_for_different_key(self) -> None:
        q = _make_queue(session_elevated_ttl=0.01)
        q.set_elevated_mode("s1", "full")
        q.set_elevated_mode("s2", "bypass")
        time.sleep(0.02)
        q.get_elevated_mode("s1")  # triggers evict of s1
        assert "s1" not in q._session_elevated_modes
        assert q._session_elevated_modes["s2"] == "bypass"

    def test_none_key_returns_none(self) -> None:
        q = _make_queue()
        assert q.get_elevated_mode(None) is None
        assert q.get_elevated_mode("") is None


# ======================================================================
# 8.  has_node_settings  — evict-on-read
# ======================================================================


class TestHasNodeSettingsEvictOnRead:
    def test_fresh_node_returns_true(self) -> None:
        q = _make_queue()
        q.set_settings("auto-approve", node_id="node-a")
        assert q.has_node_settings("node-a") is True

    def test_unknown_node_returns_false(self) -> None:
        q = _make_queue()
        assert q.has_node_settings("ghost") is False

    def test_stale_node_evicted_on_read(self) -> None:
        q = _make_queue(node_settings_ttl=0.01)
        q.set_settings("auto-approve", node_id="node-a")
        time.sleep(0.02)
        assert q.has_node_settings("node-a") is False
        assert "node-a" not in q._node_settings

    def test_stale_node_evict_different_node_preserved(self) -> None:
        q = _make_queue(node_settings_ttl=0.01)
        q.set_settings("auto-approve", node_id="node-a")
        q.set_settings("prompt", node_id="node-b")
        time.sleep(0.02)
        q.has_node_settings("node-a")  # triggers evict of node-a
        assert "node-a" not in q._node_settings
        assert "node-b" in q._node_settings


# ======================================================================
# 9.  get_settings  — evict-on-read
# ======================================================================


class TestGetSettingsEvictOnRead:
    def test_fresh_node_settings_returned(self) -> None:
        q = _make_queue()
        q.set_settings("auto-approve", node_id="node-a")
        s = q.get_settings("node-a")
        assert s.mode == "auto-approve"

    def test_stale_node_settings_evicted_and_falls_back_to_global(self) -> None:
        q = _make_queue(node_settings_ttl=0.01)
        q.set_settings("auto-deny", node_id="node-a")
        q.set_settings("prompt")  # global
        time.sleep(0.02)
        s = q.get_settings("node-a")
        # Should evict stale node-a and fall back to global
        assert s.mode == "prompt"
        assert "node-a" not in q._node_settings

    def test_global_settings_no_eviction(self) -> None:
        q = _make_queue()
        s = q.get_settings()
        assert s.mode == "prompt"

    def test_stale_node_returns_fresh_copy_not_reference(self) -> None:
        q = _make_queue()
        q.set_settings("auto-approve", node_id="node-a")
        s = q.get_settings("node-a")
        s.mode = "auto-deny"
        # original should be unchanged
        assert q._node_settings["node-a"].mode == "auto-approve"


# ======================================================================
# 10.  set_settings with node_id records timestamp
# ======================================================================


class TestSetSettingsTimestamp:
    def test_global_settings_no_timestamp_recorded(self) -> None:
        q = _make_queue()
        q.set_settings("auto-approve")  # no node_id
        # global doesn't use node_settings_set_at
        assert len(q._node_settings_set_at) == 0

    def test_node_settings_timestamp_recorded(self) -> None:
        q = _make_queue()
        before = time.time()
        q.set_settings("auto-approve", node_id="node-a")
        after = time.time()
        ts = q._node_settings_set_at["node-a"]
        assert before <= ts <= after


# ======================================================================
# 11.  _is_entry_stale boundary
# ======================================================================


class TestIsEntryStale:
    def test_none_set_at_never_stale(self) -> None:
        assert ApprovalQueue._is_entry_stale(None, 60.0, 100.0) is False

    def test_exactly_at_ttl_not_stale(self) -> None:
        # now - set_at == ttl  →  not stale (strict >)
        assert ApprovalQueue._is_entry_stale(40.0, 60.0, 100.0) is False

    def test_one_past_ttl_stale(self) -> None:
        assert ApprovalQueue._is_entry_stale(39.0, 60.0, 100.0) is True

    def test_zero_ttl_stale_by_math(self) -> None:
        # Static math: when ttl=0, any elapsed time > 0 is stale.
        # Callers guard ttl <= 0 before calling batch eviction.
        assert ApprovalQueue._is_entry_stale(10.0, 0.0, 100.0) is True

    def test_negative_ttl_stale_by_math(self) -> None:
        assert ApprovalQueue._is_entry_stale(10.0, -5.0, 100.0) is True


# ======================================================================
# 12.  Configurable constructor defaults
# ======================================================================


class TestConstructorDefaults:
    def test_default_ttl_is_positive(self) -> None:
        from agentos.application import approval_queue as aq

        assert aq._DEFAULT_SESSION_ELEVATED_TTL_SECONDS > 0
        assert aq._DEFAULT_NODE_SETTINGS_TTL_SECONDS > 0

    def test_custom_ttl_accepted(self) -> None:
        q = _make_queue(
            session_elevated_ttl=300.0,
            node_settings_ttl=600.0,
        )
        assert q._session_elevated_ttl == 300.0
        assert q._node_settings_ttl == 600.0

    def test_float_zero_ttl_accepted(self) -> None:
        q = _make_queue(session_elevated_ttl=0.0, node_settings_ttl=0.0)
        assert q._session_elevated_ttl == 0.0
        assert q._node_settings_ttl == 0.0
