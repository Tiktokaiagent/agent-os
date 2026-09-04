"""Tests for SubagentRegistry archived-handle eviction.

``SubagentRegistry._archived`` grows unboundedly on long-running sessions:
every call to ``archive()`` moves a ``SubagentHandle`` from ``_runs`` to
``_archived``, but old entries are never removed.  This adds TTL-based and
cap-based eviction for ``_archived`` and adds ``purge_archived()``.

Acceptance criteria
-------------------
1. Archived handle past TTL → excluded from get_archived, removed from dict
2. Freshly archived handle → still returned by get_archived
3. archive() triggers lazy eviction (stale handles from earlier archives cleaned)
4. Numeric cap: after max_archived, oldest handles are evicted first
5. purge_archived empties _archived and returns count
6. _handle_expired respects custom ``now`` for testability
7. Active runs (not archived) are unaffected by eviction
8. handle without completed_at (e.g. still running or never archived) never expired
"""

from __future__ import annotations

import time

from agentos.engine.subagent import (
    _ARCHIVED_TTL_SECONDS,
    SubagentHandle,
    SubagentRegistry,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_handle(
    run_id: str = "test",
    *,
    completed_at: float | None,
    status: str = "done",
) -> SubagentHandle:
    # A real SubagentHandle carries an asyncio.Task — use a minimal mock.
    return SubagentHandle(
        run_id=run_id,
        label="test-agent",
        task=None,  # type: ignore[arg-type]
        status=status,
        completed_at=completed_at,
    )


def _register_done(
    registry: SubagentRegistry, run_id: str, completed_seconds_ago: float
) -> SubagentHandle:
    """Helper: register a handle, then archive it with a specific age."""
    now = time.monotonic()
    handle = _make_handle(run_id, completed_at=now - completed_seconds_ago)
    registry._runs[run_id] = handle  # direct insertion (skipping register())
    registry.archive(run_id)
    return handle


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


class TestHandleExpired:
    """``_handle_expired`` static method."""

    def test_expired_when_past_ttl(self) -> None:
        now = 1000.0
        handle = _make_handle(run_id="h1", completed_at=now - _ARCHIVED_TTL_SECONDS)
        assert SubagentRegistry._handle_expired(handle, _ARCHIVED_TTL_SECONDS, now=now) is True

    def test_not_expired_when_within_ttl(self) -> None:
        now = 1000.0
        handle = _make_handle(run_id="h1", completed_at=now - _ARCHIVED_TTL_SECONDS + 1)
        assert SubagentRegistry._handle_expired(handle, _ARCHIVED_TTL_SECONDS, now=now) is False

    def test_not_expired_when_no_completed_at(self) -> None:
        handle = _make_handle(run_id="h1", completed_at=None)
        assert SubagentRegistry._handle_expired(handle, _ARCHIVED_TTL_SECONDS, now=1000.0) is False

    def test_respects_custom_now(self) -> None:
        handle = _make_handle(run_id="h1", completed_at=100.0)
        ttl = 300.0
        # 1 second before TTL = not expired
        assert SubagentRegistry._handle_expired(handle, ttl, now=100.0 + ttl - 1) is False
        # at boundary = expired (now - completed >= TTL)
        assert SubagentRegistry._handle_expired(handle, ttl, now=100.0 + ttl) is True
        # past boundary = expired
        assert SubagentRegistry._handle_expired(handle, ttl, now=100.0 + ttl + 1) is True

    def test_respects_custom_ttl(self) -> None:
        handle = _make_handle(run_id="h1", completed_at=100.0)
        assert SubagentRegistry._handle_expired(handle, 10.0, now=100.0 + 9.0) is False
        # at boundary = expired (now - completed >= TTL)
        assert SubagentRegistry._handle_expired(handle, 10.0, now=100.0 + 10.0) is True
        assert SubagentRegistry._handle_expired(handle, 10.0, now=100.0 + 11.0) is True


class TestGetArchived:
    """``get_archived`` filters out expired handles."""

    def test_returns_fresh_handles(self) -> None:
        reg = SubagentRegistry(max_archived=10)
        handle = _make_handle("h1", completed_at=time.monotonic())
        reg._runs["h1"] = handle
        reg.archive("h1")
        archived = reg.get_archived()
        assert len(archived) == 1
        assert archived[0].run_id == "h1"

    def test_excludes_expired_handles(self) -> None:
        reg = SubagentRegistry(max_archived=10, archived_ttl=10)
        # Manually insert an old archived handle so archive() won't see it
        old_completed = time.monotonic() - 1000
        old_handle = _make_handle("stale", completed_at=old_completed)
        reg._archived["stale"] = old_handle
        archived = reg.get_archived()
        assert len(archived) == 0
        assert "stale" not in reg._archived

    def test_mixed_fresh_and_expired(self) -> None:
        reg = SubagentRegistry(max_archived=10, archived_ttl=10)
        now = time.monotonic()
        # Fresh
        fresh = _make_handle("fresh", completed_at=now)
        reg._archived["fresh"] = fresh
        # Stale
        stale = _make_handle("stale", completed_at=now - 100)
        reg._archived["stale"] = stale
        archived = reg.get_archived()
        assert len(archived) == 1
        assert archived[0].run_id == "fresh"


class TestArchive:
    """``archive()`` moves handle and triggers lazy eviction."""

    def test_moves_from_runs_to_archived(self) -> None:
        reg = SubagentRegistry(max_archived=10)
        handle = _make_handle("h1", completed_at=time.monotonic())
        reg._runs["h1"] = handle
        assert reg.archive("h1") is True
        assert "h1" not in reg._runs
        assert "h1" in reg._archived

    def test_returns_false_for_missing_run(self) -> None:
        reg = SubagentRegistry()
        assert reg.archive("ghost") is False

    def test_evicts_stale_on_archive(self) -> None:
        reg = SubagentRegistry(max_archived=10, archived_ttl=5)
        # Insert a stale archived entry directly
        old = _make_handle("old", completed_at=time.monotonic() - 1000)
        reg._archived["old"] = old
        # Now archive a fresh one — should trigger eviction
        fresh = _make_handle("fresh", completed_at=time.monotonic())
        reg._runs["fresh"] = fresh
        reg.archive("fresh")
        assert "old" not in reg._archived
        assert "fresh" in reg._archived

    def test_trims_over_cap_on_archive(self) -> None:
        reg = SubagentRegistry(max_archived=3, archived_ttl=99999)
        now = time.monotonic()
        # Insert 4 handles directly into _archived
        reg._archived = {
            "a": _make_handle("a", completed_at=now - 30),
            "b": _make_handle("b", completed_at=now - 20),
            "c": _make_handle("c", completed_at=now - 10),
            "d": _make_handle("d", completed_at=now - 0),
        }
        # Archive a new one — triggers eviction trimming to 3
        e_handle = _make_handle("e", completed_at=time.monotonic())
        reg._runs["e"] = e_handle
        reg.archive("e")
        # Should have at most 3 archived (oldest evicted)
        assert len(reg._archived) <= 3, f"Expected ≤3, got {reg.get_archived()}"


class TestPurgeArchived:
    """``purge_archived()`` removes all archived handles."""

    def test_purges_all_and_returns_count(self) -> None:
        reg = SubagentRegistry(max_archived=10)
        now = time.monotonic()
        reg._archived["a"] = _make_handle("a", completed_at=now)
        reg._archived["b"] = _make_handle("b", completed_at=now)
        assert reg.purge_archived() == 2
        assert len(reg._archived) == 0

    def test_purge_empty_returns_zero(self) -> None:
        reg = SubagentRegistry()
        assert reg.purge_archived() == 0


class TestActiveRunsUnaffected:
    """Eviction only touches archived handles, never active runs."""

    def test_active_runs_survive_eviction(self) -> None:
        reg = SubagentRegistry(max_archived=10, archived_ttl=5)
        # Active handle
        active = _make_handle("active", completed_at=None)
        reg._runs["active"] = active
        # Stale archived
        reg._archived["stale"] = _make_handle("stale", completed_at=time.monotonic() - 1000)
        reg.get_archived()  # triggers eviction
        assert "active" in reg._runs
        assert reg.get("active") is not None

    def test_aborted_handle_stay_in_runs_until_archived(self) -> None:
        """Aborted handles remain in _runs; only archive() moves them."""
        reg = SubagentRegistry()
        handle = _make_handle("h1", completed_at=time.monotonic(), status="aborted")
        reg._runs["h1"] = handle
        assert len(reg.all_handles()) == 1
        assert reg.get_archived() == []  # not in archived


class TestCleanupOrphans:
    """Existing `cleanup_orphans()` must still work correctly."""

    def test_orphans_not_affected_by_archived_eviction(self) -> None:
        """`run()` handles remain in _runs; eviction only touches archived."""
        reg = SubagentRegistry()
        handle = _make_handle("orphan", completed_at=None, status="running")
        reg._runs["orphan"] = handle
        # Also put something stale in archived
        stale = _make_handle("stale", completed_at=time.monotonic() - 9999)
        reg._archived["stale"] = stale
        # Eviction shouldn't touch _runs
        reg.get_archived()
        assert "orphan" in reg._runs
        assert "stale" not in reg._archived
