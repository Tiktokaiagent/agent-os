"""Tests for HeartbeatRunner bounded buffer (memory leak fix).

Covers the TTL-based eviction, max-size cap, evict-on-read during poll,
evict-on-write during ingest, purge, and clear — per the Winner Formula.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentos.scheduler.heartbeat import (
    HeartbeatConfig,
    HeartbeatEvent,
    HeartbeatRunner,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _runner(config: HeartbeatConfig | None = None, **kwargs) -> HeartbeatRunner:
    if config is None:
        config = HeartbeatConfig(active_hours=None)
    return HeartbeatRunner(config, **kwargs)


def _event(
    kind: str = "test",
    priority: str = "medium",
    *,
    age_seconds: float = 0.0,
) -> HeartbeatEvent:
    return HeartbeatEvent(
        kind=kind,
        priority=priority,
        emitted_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
    )


# ══════════════════════════════════════════════════════════════════════════════
# TestStaticMethod — _evict_stale_events  (unit)
# ══════════════════════════════════════════════════════════════════════════════


class TestEvictStaleEvents:
    """Exercises the static eviction helper directly."""

    def test_keeps_fresh_events(self):
        now = datetime.now(UTC)
        events = [
            _event(age_seconds=10),
            _event(age_seconds=20),
        ]
        fresh = HeartbeatRunner._evict_stale_events(events, now, ttl=60.0)
        assert len(fresh) == 2

    def test_removes_stale_events(self):
        now = datetime.now(UTC)
        stale = _event(age_seconds=120)
        fresh = _event(age_seconds=10)
        events = [stale, fresh]
        result = HeartbeatRunner._evict_stale_events(events, now, ttl=60.0)
        assert len(result) == 1
        assert result[0].kind == "test"  # the fresh one

    def test_boundary_before_ttl(self):
        """Event emitted exactly at TTL boundary should be evicted (expired)."""
        now = datetime.now(UTC)
        ttl = 60.0
        # emitted_at = now - 60s → cutoff is now - 60s → emitted_at == cutoff
        # which means not > cutoff → stale
        boundary = HeartbeatEvent(
            emitted_at=now - timedelta(seconds=ttl),
        )
        result = HeartbeatRunner._evict_stale_events([boundary], now, ttl)
        assert len(result) == 0

    def test_boundary_just_within_ttl(self):
        """Event emitted 1 microsecond before TTL should be kept."""
        now = datetime.now(UTC)
        ttl = 60.0
        just_within = HeartbeatEvent(
            emitted_at=now - timedelta(seconds=ttl) + timedelta(microseconds=1),
        )
        result = HeartbeatRunner._evict_stale_events([just_within], now, ttl)
        assert len(result) == 1

    def test_empty_list_returns_empty(self):
        assert HeartbeatRunner._evict_stale_events([], datetime.now(UTC), 60.0) == []


# ══════════════════════════════════════════════════════════════════════════════
# TestIngest — evict-on-write  (max buffer size)
# ══════════════════════════════════════════════════════════════════════════════


class TestIngest:
    """Buffer cap enforcement on ingest."""

    def test_trims_oldest_events_when_over_cap(self):
        runner = _runner(max_buffer_size=3)
        runner.ingest(_event(priority="high"))
        runner.ingest(_event(priority="high"))
        runner.ingest(_event(priority="high"))
        runner.ingest(_event(priority="high"))  # 4th → should evict oldest
        assert runner.pending_counts()["high"] == 3

    def test_keeps_events_under_cap(self):
        runner = _runner(max_buffer_size=10)
        for _ in range(5):
            runner.ingest(_event(priority="low"))
        assert runner.pending_counts()["low"] == 5

    def test_different_bands_independent_caps(self):
        runner = _runner(max_buffer_size=3)
        for _ in range(10):
            runner.ingest(_event(priority="high"))
        runner.ingest(_event(priority="low"))
        assert runner.pending_counts()["high"] == 3
        assert runner.pending_counts()["low"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# TestPoll — evict-on-read  (TTL eviction during poll)
# ══════════════════════════════════════════════════════════════════════════════


class TestPoll:
    """TTL eviction during poll()."""

    def test_stale_events_skipped_by_poll(self):
        """Stale events are evicted before emission check."""
        runner = _runner(buffer_ttl_seconds=60.0)
        stale = _event(age_seconds=120)
        runner.ingest(stale)
        ticks = runner.poll(now=datetime.now(UTC))
        # Stale events are evicted, buffer is empty, no tick emitted.
        assert ticks == []

    def test_stale_events_not_emitted(self):
        """Stale events should NOT appear in emitted tick even if cooldown passes."""
        runner = _runner(buffer_ttl_seconds=60.0, config=HeartbeatConfig(coalesce_window_ms=0))
        fresh = _event(age_seconds=5)
        stale = _event(age_seconds=120)
        runner.ingest(fresh)
        runner.ingest(stale)
        now = datetime.now(UTC)
        ticks = runner.poll(now=now)
        # Only fresh event should be emitted.
        assert len(ticks) == 1
        assert ticks[0].event_count == 1

    def test_fresh_events_still_emitted(self):
        """Events within TTL are still emitted (coalescence permitting)."""
        runner = _runner(buffer_ttl_seconds=3600.0, config=HeartbeatConfig(coalesce_window_ms=0))
        runner.ingest(_event(age_seconds=5))
        ticks = runner.poll(now=datetime.now(UTC))
        assert len(ticks) == 1

    def test_active_hours_still_evicts_stale(self):
        """Even outside active hours, stale events are removed from buffers."""
        runner = _runner(
            buffer_ttl_seconds=60.0,
            config=HeartbeatConfig(active_hours=(9, 17)),
        )
        now = datetime(2026, 9, 4, 6, 0, 0, tzinfo=UTC)
        stale = HeartbeatEvent(priority="low", emitted_at=now - timedelta(seconds=120))
        runner.ingest(stale)
        # poll outside active hours
        ticks = runner.poll(now=now)
        assert ticks == []
        # But stale events should be evicted — pending_counts returns 0
        assert runner.pending_counts() == {}


# ══════════════════════════════════════════════════════════════════════════════
# TestPurgeAndClear
# ══════════════════════════════════════════════════════════════════════════════


class TestPurge:
    """purge() clears buffers without affecting tick timestamps."""

    def test_purge_clears_all_buffers(self):
        runner = _runner()
        runner.ingest(_event(priority="high"))
        runner.ingest(_event(priority="low"))
        runner.purge()
        assert runner.pending_counts() == {}

    def test_purge_keeps_tick_history(self):
        runner = _runner(config=HeartbeatConfig(coalesce_window_ms=0))
        runner.ingest(_event())
        runner.poll(now=datetime.now(UTC))  # sets _last_tick
        runner.purge()
        # After purge, a fresh event should still respect cooldown.
        runner.ingest(_event(age_seconds=0.1))
        ticks = runner.poll(now=datetime.now(UTC))
        # Cooldown (5s for medium) hasn't passed, so no tick.
        assert ticks == []


class TestClear:
    """clear() resets both buffers and tick history."""

    def test_clear_resets_everything(self):
        runner = _runner(config=HeartbeatConfig(coalesce_window_ms=0))
        runner.ingest(_event())
        runner.poll(now=datetime.now(UTC))  # sets _last_tick
        runner.clear()
        assert runner.pending_counts() == {}
        # After clear, cooldown history is gone → next poll emits.
        runner.ingest(_event(age_seconds=0.1))
        ticks = runner.poll(now=datetime.now(UTC))
        assert len(ticks) == 1


# ══════════════════════════════════════════════════════════════════════════════
# TestEdgeCase — safety/limits
# ══════════════════════════════════════════════════════════════════════════════


class TestEdgeCase:
    """Boundary conditions and safety."""

    def test_constructor_defaults(self):
        runner = HeartbeatRunner()
        assert runner.buffer_ttl_seconds == 3600.0
        assert runner.max_buffer_size == 500

    def test_custom_ttl_and_cap(self):
        runner = HeartbeatRunner(buffer_ttl_seconds=120.0, max_buffer_size=10)
        assert runner.buffer_ttl_seconds == 120.0
        assert runner.max_buffer_size == 10

    def test_zero_ttl_evicts_everything(self):
        """With TTL=0, every event is stale immediately."""
        runner = _runner(buffer_ttl_seconds=0.0)
        runner.ingest(_event(age_seconds=0))
        ticks = runner.poll(now=datetime.now(UTC))
        assert ticks == []
        assert runner.pending_counts() == {}

    def test_huge_max_buffer_size_does_not_evict(self):
        runner = _runner(max_buffer_size=100_000)
        for _ in range(1000):
            runner.ingest(_event(priority="medium"))
        assert runner.pending_counts()["medium"] == 1000

    def test_evict_on_read_handles_empty_band(self):
        """Band with no events should not crash during evict-on-read."""
        runner = _runner()
        runner.purge()
        assert runner.pending_counts() == {}

    def test_poll_evicts_then_emits_fresh(self):
        """Mixed stale+fresh: stale evicted, only fresh emitted."""
        runner = _runner(
            buffer_ttl_seconds=60.0,
            config=HeartbeatConfig(coalesce_window_ms=0, priority_bands={"test": 0.0}),
        )
        runner.ingest(
            HeartbeatEvent(priority="test", emitted_at=datetime.now(UTC) - timedelta(seconds=120))
        )
        runner.ingest(HeartbeatEvent(priority="test"))
        ticks = runner.poll(now=datetime.now(UTC))
        assert len(ticks) == 1
        assert ticks[0].event_count == 1

    def test_clear_after_purge_is_idempotent(self):
        runner = _runner()
        runner.ingest(_event())
        runner.purge()
        runner.clear()
        assert runner.pending_counts() == {}
        assert runner._last_tick == {}
