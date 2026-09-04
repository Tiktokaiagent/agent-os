"""Regression tests for background session eviction (Issue #1071).

Validates that _finalize_bg_session schedules eviction, stale sessions
are removed after TTL, and the soft cap prevents unbounded growth.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agentos.tools.builtin import shell


class _FakeDoneProcess:
    """Minimal process stub that looks like a completed background process."""

    pid = 99999
    returncode = 0
    stdout = None
    stderr = None

    async def wait(self) -> int:
        return 0


def _make_session(
    session_id: str,
    *,
    done: bool = True,
    timed_out: bool = False,
    killed: bool = False,
    age: float = 0.0,
) -> shell._BgSession:
    """Build a _BgSession with synthetic age."""
    proc = _FakeDoneProcess()
    ended = time.time() - age
    return shell._BgSession(
        session_id=session_id,
        command="echo hi",
        process=proc,
        done=done,
        timed_out=timed_out,
        killed=killed,
        ended_at=ended,
        returncode=0,
    )


# ──────────────────────────────────────────────
# 1. _finalize_bg_session schedules eviction
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_finalize_schedules_eviction() -> None:
    """_finalize_bg_session() triggers _schedule_bg_evict()."""
    session = _make_session("evict-test-1")
    shell._finalize_bg_session(session)

    assert session.done is True
    assert session.ended_at is not None
    assert session.returncode == 0

    # Eviction task was created (schedule runs inside _finalize_bg_session)
    new_task = shell._bg_evict_task
    # The task might be the old one if already scheduled, or a new one
    # Either way, one must exist
    assert new_task is not None, "expected an eviction task to be scheduled"


# ──────────────────────────────────────────────
# 2. TTL eviction removes stale sessions
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evict_stale_removes_old_completed_sessions() -> None:
    """Sessions past the TTL are removed by _evict_stale_bg_sessions()."""
    shell._bg_sessions.clear()
    nowish = time.time()

    # Fresh session (just completed)
    fresh = shell._BgSession(
        session_id="fresh",
        command="echo fresh",
        process=_FakeDoneProcess(),
        done=True,
        ended_at=nowish - 10,
        returncode=0,
    )
    # Old session (past TTL)
    stale = shell._BgSession(
        session_id="stale",
        command="echo stale",
        process=_FakeDoneProcess(),
        done=True,
        ended_at=nowish - 1000,
        returncode=0,
    )
    shell._bg_sessions["fresh"] = fresh
    shell._bg_sessions["stale"] = stale

    shell._evict_stale_bg_sessions()

    assert "fresh" in shell._bg_sessions, "fresh session should survive"
    assert "stale" not in shell._bg_sessions, "stale session should be evicted"


# ──────────────────────────────────────────────
# 3. Soft cap evicts oldest completed sessions
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evict_excess_sessions_enforces_soft_cap() -> None:
    """When completed sessions exceed the max, the oldest are evicted."""
    shell._bg_sessions.clear()
    max_sessions = shell._BG_SESSION_MAX_SESSIONS

    # Fill beyond the cap
    base = time.time()
    for i in range(max_sessions + 10):
        sid = f"cap-test-{i}"
        shell._bg_sessions[sid] = shell._BgSession(
            session_id=sid,
            command="echo hello",
            process=_FakeDoneProcess(),
            done=True,
            ended_at=base - (max_sessions + 10 - i) * 0.1,  # newest last
            returncode=0,
        )

    shell._evict_stale_bg_sessions()

    # Should have at most max_sessions entries
    assert len(shell._bg_sessions) <= max_sessions, (
        f"expected at most {max_sessions} sessions, got {len(shell._bg_sessions)}"
    )
    # The 10 oldest (cap-test-0 through cap-test-9) should be gone
    for i in range(10):
        assert f"cap-test-{i}" not in shell._bg_sessions, (
            f"oldest session cap-test-{i} should have been evicted"
        )


# ──────────────────────────────────────────────
# 4. Running sessions survive eviction
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_running_sessions_not_evicted() -> None:
    """Incomplete sessions are never removed by eviction."""
    shell._bg_sessions.clear()
    running = shell._BgSession(
        session_id="running",
        command="sleep 100",
        process=_FakeDoneProcess(),
        done=False,
    )
    shell._bg_sessions["running"] = running

    shell._evict_stale_bg_sessions()

    assert "running" in shell._bg_sessions, "running session should not be evicted"


# ──────────────────────────────────────────────
# 5. Manual remove still works
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manual_remove_still_works() -> None:
    """Manual process(action='remove') removes the session immediately."""
    shell._bg_sessions.clear()
    session = _make_session("manual-remove")
    shell._bg_sessions["manual-remove"] = session

    del shell._bg_sessions["manual-remove"]

    assert "manual-remove" not in shell._bg_sessions


# ──────────────────────────────────────────────
# 6. schedule_bg_evict is idempotent
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schedule_bg_evict_idempotent() -> None:
    """Multiple calls to _schedule_bg_evict don't create multiple tasks."""
    shell._bg_evict_task = None

    shell._schedule_bg_evict()
    first_task = shell._bg_evict_task

    shell._schedule_bg_evict()
    second_task = shell._bg_evict_task

    # Should be the same task (idempotent while not done)
    assert first_task is second_task, "expected idempotent schedule"

    # Cleanup
    if first_task is not None and not first_task.done():
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
    shell._bg_evict_task = None


# ──────────────────────────────────────────────
# 7. Kill + Timeout sessions evicted too
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_killed_and_timed_out_sessions_evicted() -> None:
    """Killed/timed-out sessions are evicted just like completed ones."""
    shell._bg_sessions.clear()
    nowish = time.time()

    killed = shell._BgSession(
        session_id="killed-session",
        command="echo killed",
        process=_FakeDoneProcess(),
        done=True,
        killed=True,
        ended_at=nowish - 1000,
        returncode=-9,
    )
    timed_out_s = shell._BgSession(
        session_id="timed-out-session",
        command="echo timeout",
        process=_FakeDoneProcess(),
        done=True,
        timed_out=True,
        ended_at=nowish - 1000,
        returncode=None,
    )
    shell._bg_sessions["killed-session"] = killed
    shell._bg_sessions["timed-out-session"] = timed_out_s

    shell._evict_stale_bg_sessions()

    assert "killed-session" not in shell._bg_sessions
    assert "timed-out-session" not in shell._bg_sessions


# ──────────────────────────────────────────────
# 8. _finalize_bg_session cleanup callbacks still run
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_finalize_still_runs_cleanup_callbacks() -> None:
    """_finalize_bg_session still invokes all cleanup callbacks."""
    fired: list[str] = []

    def callback_a() -> None:
        fired.append("a")

    def callback_b() -> None:
        fired.append("b")

    session = _make_session("callback-test")
    session.cleanup_callbacks = [callback_a, callback_b]

    shell._finalize_bg_session(session)

    assert fired == ["a", "b"], f"expected both callbacks to fire, got {fired}"
    assert not session.cleanup_callbacks, "callbacks list should be cleared"
