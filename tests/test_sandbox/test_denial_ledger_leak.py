"""Tests for DenialLedger dict-leak fix (_sessions never evicted).

DenialLedger._sessions grows with each distinct session that triggers a
sandbox policy check and is never reclaimed.  There is a ``reset_session()``
method but it is never called from any production code path.

Design: ``_evict_stale`` and ``reap()`` never touch active sessions (no
evict-on-read).  Only a batch pass removes entries past TTL, so a session
that is temporarily idle retains its counters.
"""

from __future__ import annotations

import time

import pytest

from agentos.sandbox.governance import DenialLedger
from agentos.sandbox.types import DenialReason

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ledger(**overrides: object) -> DenialLedger:
    return DenialLedger(
        session_ttl=overrides.get("session_ttl", 60.0),
        stale_output_cache=None,
    )


# ======================================================================
# 1.  _evict_stale  — batch eviction
# ======================================================================


class TestEvictStale:
    def test_no_entries_nothing_evicted(self) -> None:
        lgr = _make_ledger()
        assert lgr._evict_stale() == 0

    def test_fresh_entries_survive(self) -> None:
        lgr = _make_ledger()
        lgr._state("s1")
        lgr._state("s2")
        assert lgr._evict_stale() == 0
        assert "s1" in lgr._sessions
        assert "s2" in lgr._sessions

    def test_stale_entries_evicted(self) -> None:
        lgr = _make_ledger(session_ttl=0.01)
        lgr._state("s1")
        time.sleep(0.02)
        assert lgr._evict_stale() == 1
        assert "s1" not in lgr._sessions

    def test_mixed_stale_and_fresh(self) -> None:
        lgr = _make_ledger(session_ttl=0.03)
        lgr._state("old")
        time.sleep(0.04)
        lgr._state("recent")
        evicted = lgr._evict_stale()
        assert evicted == 1
        assert "old" not in lgr._sessions
        assert "recent" in lgr._sessions

    def test_touched_at_deleted_alongside_value(self) -> None:
        lgr = _make_ledger(session_ttl=0.01)
        lgr._state("s1")
        time.sleep(0.02)
        lgr._evict_stale()
        assert "s1" not in lgr._touched_at

    def test_ttl_of_zero_disables_eviction(self) -> None:
        lgr = _make_ledger(session_ttl=0.0)
        lgr._state("s1")
        time.sleep(0.01)
        assert lgr._evict_stale() == 0
        assert "s1" in lgr._sessions


# ======================================================================
# 2.  _state  — records touched_at
# ======================================================================


class TestStateTouches:
    def test_new_entry_touches_timestamp(self) -> None:
        lgr = _make_ledger()
        before = time.time()
        lgr._state("s1")
        assert "s1" in lgr._touched_at
        assert lgr._touched_at["s1"] >= before

    def test_existing_entry_refreshes_timestamp(self) -> None:
        lgr = _make_ledger()
        lgr._state("s1")
        ts1 = lgr._touched_at["s1"]
        time.sleep(0.01)
        lgr._state("s1")  # touch again
        assert lgr._touched_at["s1"] > ts1


# ======================================================================
# 3.  reap  — public async entry point
# ======================================================================


class TestReap:
    @pytest.mark.asyncio
    async def test_reap_empty(self) -> None:
        lgr = _make_ledger()
        assert await lgr.reap() == 0

    @pytest.mark.asyncio
    async def test_reap_stale(self) -> None:
        lgr = _make_ledger(session_ttl=0.01)
        lgr._state("s1")
        time.sleep(0.02)
        evicted = await lgr.reap()
        assert evicted == 1
        assert "s1" not in lgr._sessions

    @pytest.mark.asyncio
    async def test_reap_is_thread_safe(self) -> None:
        lgr = _make_ledger(session_ttl=0.01)
        lgr._state("s1")
        lgr._state("s2")
        time.sleep(0.02)
        evicted = await lgr.reap()
        assert evicted == 2


# ======================================================================
# 4.  reset_session  — explicit cleanup
# ======================================================================


class TestResetSession:
    @pytest.mark.asyncio
    async def test_reset_removes_entry_and_timestamp(self) -> None:
        lgr = _make_ledger()
        lgr._state("s1")
        await lgr.reset_session("s1")
        assert "s1" not in lgr._sessions
        assert "s1" not in lgr._touched_at

    @pytest.mark.asyncio
    async def test_reset_missing_no_error(self) -> None:
        lgr = _make_ledger()
        await lgr.reset_session("ghost")  # should not raise


# ======================================================================
# 5.  Functional — record_denial touches timestamp
# ======================================================================


class TestRecordDenialTouches:
    @pytest.mark.asyncio
    async def test_record_denial_refreshes_timestamp(self) -> None:
        lgr = _make_ledger()
        await lgr.record_denial("s1", "fp1", DenialReason.POLICY_DENIED)
        assert "s1" in lgr._touched_at

    @pytest.mark.asyncio
    async def test_repeated_denial_refreshes_timestamp(self) -> None:
        lgr = _make_ledger()
        await lgr.record_denial("s1", "fp1", DenialReason.POLICY_DENIED)
        ts1 = lgr._touched_at["s1"]
        time.sleep(0.01)
        await lgr.record_denial("s1", "fp2", DenialReason.HUMAN_REJECTED)
        assert lgr._touched_at["s1"] > ts1


# ======================================================================
# 6.  Constructor defaults
# ======================================================================


class TestConstructorDefaults:
    def test_default_ttl_is_positive(self) -> None:
        from agentos.sandbox.governance import _DEFAULT_SESSION_TTL_SECONDS
        assert _DEFAULT_SESSION_TTL_SECONDS > 0

    def test_custom_ttl_accepted(self) -> None:
        lgr = _make_ledger(session_ttl=3600.0)
        assert lgr._session_ttl == 3600.0

    def test_zero_ttl_accepted(self) -> None:
        lgr = _make_ledger(session_ttl=0.0)
        assert lgr._session_ttl == 0.0
        assert lgr._session_ttl == 0.0
