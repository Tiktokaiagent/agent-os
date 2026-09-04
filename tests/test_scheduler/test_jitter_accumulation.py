"""Jitter-accumulation regression tests for :issue:`1059`.

``_next_run`` adds ``job.jitter_seconds`` only when ``apply_jitter=True``.
All post-execution call sites (``_apply_result_state``, ``ops.update``,
``ops.resume``, ``startup_catchup``) pass the default ``False`` so the
one-time startup stagger never accumulates on re-runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentos.scheduler.jobs import _next_run
from agentos.scheduler.ops import SchedulerOps
from agentos.scheduler.payloads import make_agent_turn_payload
from agentos.scheduler.persistence import JobStore
from agentos.scheduler.types import CronJob, ScheduleKind, SessionTarget


def _cron_job(
    cron_expr: str = "*/5 * * * *",
    jitter_seconds: float = 0.0,
    tz: str = "",
) -> CronJob:
    return CronJob(
        id="jitter-test",
        cron_expr=cron_expr,
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "x", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.CRON,
        jitter_seconds=jitter_seconds,
        tz=tz,
    )


# ---------------------------------------------------------------------------
# Pure _next_run unit tests
# ---------------------------------------------------------------------------


def test_next_run_apply_jitter_true_adds_seconds() -> None:
    """When apply_jitter=True the returned datetime includes jitter_seconds."""
    job = _cron_job(cron_expr="*/5 * * * *", jitter_seconds=17.0)
    after = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    # The first cron match after 12:00:00 is 12:05:00.
    result = _next_run(job, after, apply_jitter=True)
    assert result == datetime(2026, 9, 4, 12, 5, 17, tzinfo=UTC)


def test_next_run_apply_jitter_false_omits_seconds() -> None:
    """Default (apply_jitter=False) returns the raw cron match without jitter."""
    job = _cron_job(cron_expr="*/5 * * * *", jitter_seconds=17.0)
    after = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    result = _next_run(job, after)  # default False
    assert result == datetime(2026, 9, 4, 12, 5, 0, tzinfo=UTC)


def test_next_run_with_zero_jitter_apply_true() -> None:
    """apply_jitter=True with 0 jitter is a no-op (same as False)."""
    job = _cron_job(cron_expr="*/5 * * * *", jitter_seconds=0.0)
    after = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    result_on = _next_run(job, after, apply_jitter=True)
    result_off = _next_run(job, after, apply_jitter=False)
    assert result_on == result_off == datetime(2026, 9, 4, 12, 5, 0, tzinfo=UTC)


def test_next_run_jitter_does_not_accumulate_across_calls() -> None:
    """Calling _next_run multiple times with apply_jitter=True on the result
    of a previous non-jittered call does NOT compound jitter.

    This simulates the post-execution reschedule path: the stored
    ``next_run_at`` has no jitter (save from the initial creation), and the
    next call recomputes from that point without jitter.
    """
    job = _cron_job(cron_expr="*/5 * * * *", jitter_seconds=10.0)
    after = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)

    # Initial creation: apply jitter once
    first = _next_run(job, after, apply_jitter=True)
    assert first == datetime(2026, 9, 4, 12, 5, 10, tzinfo=UTC)

    # Post-exec reschedule: no jitter, computed from the *previous return*
    # which already includes jitter. The next cron slot is still 12:10:00.
    second = _next_run(job, first, apply_jitter=False)
    assert second == datetime(2026, 9, 4, 12, 10, 0, tzinfo=UTC)

    # Third call from the non-jittered point: still 12:15:00 (no drift)
    third = _next_run(job, second, apply_jitter=False)
    assert third == datetime(2026, 9, 4, 12, 15, 0, tzinfo=UTC)


def test_next_run_jitter_on_midnight_boundary() -> None:
    """Jitter works correctly across midnight / day boundary."""
    job = _cron_job(cron_expr="0 0 * * *", jitter_seconds=30.0)
    after = datetime(2026, 9, 4, 23, 30, 0, tzinfo=UTC)
    result = _next_run(job, after, apply_jitter=True)
    assert result == datetime(2026, 9, 5, 0, 0, 30, tzinfo=UTC)


def test_next_run_jitter_with_tz() -> None:
    """Jitter works correctly with a timezone."""
    job = _cron_job(cron_expr="*/5 * * * *", jitter_seconds=7.0, tz="Asia/Jakarta")
    after = datetime(2026, 9, 4, 5, 0, 0, tzinfo=UTC)  # 12:00 WIB
    result = _next_run(job, after, apply_jitter=True)
    # Next match at 12:05 WIB = 05:05 UTC + 7s jitter
    assert result == datetime(2026, 9, 4, 5, 5, 7, tzinfo=UTC)


# ---------------------------------------------------------------------------
# SchedulerOps integration tests
# ---------------------------------------------------------------------------


async def test_ops_add_applies_jitter_to_initial_next_run(tmp_path: Path) -> None:
    """ops.add passes apply_jitter=True so the first next_run_at includes jitter."""
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store, max_jitter=10.0)
        job = await ops.add(
            name="jitter-check",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="* * * * *",
            handler_key="agent_run",
            payload=make_agent_turn_payload("test"),
            session_target=SessionTarget.ISOLATED,
        )
        assert job.jitter_seconds > 0  # auto-computed jitter
        now = ops._now()
        # next_run_at should be now+1min + jitter, not just now+1min
        expected_base = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        assert job.next_run_at > expected_base  # jitter pushed it forward
        assert job.next_run_at <= expected_base + timedelta(seconds=job.jitter_seconds)
    finally:
        await store.close()


async def test_ops_add_zero_jitter_produces_exact_next_run(tmp_path: Path) -> None:
    """Explicit jitter_seconds=0 produces exact-minute next_run_at."""
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store)
        job = await ops.add(
            name="exact",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
            handler_key="agent_run",
            payload=make_agent_turn_payload("test"),
            session_target=SessionTarget.ISOLATED,
            jitter_seconds=0.0,
        )
        now = ops._now()
        expected = now.replace(second=0, microsecond=0)
        # Find the next */5 minute boundary
        remainder = expected.minute % 5
        if remainder != 0 or expected <= now:
            expected += timedelta(minutes=5 - remainder if remainder else 5)
        # No jitter — should be exactly on the 5-minute boundary
        assert job.next_run_at == expected.replace(second=0, microsecond=0)
    finally:
        await store.close()


async def test_ops_update_schedule_no_jitter_reeval(tmp_path: Path) -> None:
    """ops.update with schedule change recomputes next_run_at WITHOUT jitter.

    Changing only the name keeps the original (jittered) next_run_at from
    ops.add — that is correct, the one-time stagger set at creation should
    not be removed. Changing the schedule expression triggers a fresh
    _next_run call with apply_jitter=False.
    """
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store, max_jitter=5.0)
        job = await ops.add(
            name="update-test",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
            handler_key="agent_run",
            payload=make_agent_turn_payload("test"),
            session_target=SessionTarget.ISOLATED,
        )
        assert job.jitter_seconds > 0

        # Name-only update: preserves original jittered next_run (no recalc)
        renamed = await ops.update(job.id, name="renamed")
        assert renamed is not None
        assert renamed.next_run_at == job.next_run_at

        # Schedule-changing update: recalc WITHOUT re-applying jitter
        updated = await ops.update(
            job.id,
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/3 * * * *",
        )
        assert updated is not None
        assert updated.cron_expr == "*/3 * * * *"

        # Recalculated next_run_at must be on exact 3-minute boundary
        # (no jitter sub-minute offset)
        assert updated.next_run_at.second == 0
        assert updated.next_run_at.microsecond == 0
        assert updated.next_run_at.minute % 3 == 0
    finally:
        await store.close()


async def test_ops_resume_does_not_re_apply_jitter(tmp_path: Path) -> None:
    """Resuming a job recomputes next_run_at without jitter."""
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store, max_jitter=10.0)
        job = await ops.add(
            name="pause-resume",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/3 * * * *",
            handler_key="agent_run",
            payload=make_agent_turn_payload("test"),
            session_target=SessionTarget.ISOLATED,
        )
        # Disable then resume
        job.enabled = False
        await store.save(job)
        resumed = await ops.resume(job.id)
        assert resumed is not None

        # Resumed next_run_at should be on exact 3-minute boundary (no jitter)
        assert resumed.next_run_at.second == 0
        assert resumed.next_run_at.microsecond == 0
        assert resumed.next_run_at.minute % 3 == 0
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Boundary: explicit jitter_seconds on EVERY+anchor path
# ---------------------------------------------------------------------------


async def test_ops_add_explicit_jitter_on_every_interval(tmp_path: Path) -> None:
    """EVERY+seconds path respects jitter on initial add but not on reschedule.

    EVERY+interval uses anchor math, not the cron-scan jitter code path, so
    jitter is stored on the job but not applied in the anchor branch of
    _next_run. This behaviour is correct — jitter is a cron stagger concept.
    """
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store)
        job = await ops.add(
            name="every-jitter",
            schedule_kind=ScheduleKind.EVERY,
            schedule_value="60",
            handler_key="agent_run",
            payload=make_agent_turn_payload("test"),
            session_target=SessionTarget.ISOLATED,
            jitter_seconds=15.0,
        )
        # EVERY path doesn't add jitter to next_run_at (anchor + interval math)
        assert job.jitter_seconds == 15.0
        # The jitter is *stored* but EVERY anchor doesn't apply it
        # (the cron-scan branch does). This documents the existing behaviour.
        # next_run_at is computed from anchor_at, so compare against that.
        assert job.anchor_at is not None
        assert job.next_run_at == job.anchor_at + timedelta(seconds=60)
    finally:
        await store.close()
