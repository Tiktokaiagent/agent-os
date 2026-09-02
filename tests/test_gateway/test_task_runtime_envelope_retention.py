"""TaskRuntime retains cached route envelope when same-session work remains (#930).

``_mark_terminal`` must not discard ``_last_envelope_by_session[session_key]``
when another task for that session is still queued or running. If it does,
``TaskRuntime.send()`` sees no cached envelope and builds a generic
``SourceKind.SYSTEM`` envelope instead of reusing the channel routing metadata.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import TaskRuntime


def _make_envelope(session_key: str = "agent-1::sess-1") -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="agent-1",
        session_key=session_key,
        input_provenance={"kind": "test"},
    )


def _make_storage() -> Any:
    from unittest.mock import MagicMock

    storage = MagicMock()
    task_db: dict[str, Any] = {}

    async def create(record: Any) -> None:
        task_db[record.task_id] = record

    async def update(task_id: str, **kwargs: Any) -> None:
        rec = task_db.get(task_id)
        if rec is None:
            return
        for k, v in kwargs.items():
            if hasattr(rec, k):
                object.__setattr__(rec, k, v)

    async def get(task_id: str) -> Any | None:
        return task_db.get(task_id)

    async def list_tasks(**_: Any) -> list:
        return list(task_db.values())

    storage.create_agent_task = create
    storage.update_agent_task = update
    storage.get_agent_task = get
    storage.list_tasks = list_tasks
    return storage


@pytest.mark.asyncio
async def test_envelope_retained_when_pending_work_remains() -> None:
    """Cached route envelope is kept while the session still has queued work."""
    gate = asyncio.Event()

    async def _blocking_handler(_run: Any) -> None:
        await gate.wait()

    rt = TaskRuntime(
        storage=_make_storage(),
        turn_handler=_blocking_handler,
        max_concurrency=1,
        max_pending_per_session=64,
    )

    env = _make_envelope("agent-1::sess-env-ret-1")

    # Task A runs (holds the single concurrency slot)
    h_a = await rt.enqueue(env, "task-a")
    await asyncio.sleep(0.05)
    assert h_a is not None

    # Task B is queued for the same session (remains pending)
    h_b = await rt.enqueue(env, "task-b")
    assert h_b is not None

    # Envelope should be cached for this session
    cached_before = rt._last_envelope_by_session.get("agent-1::sess-env-ret-1")
    assert cached_before is not None, (
        "Envelope should be cached after first enqueue"
    )
    assert cached_before.source_kind == SourceKind.WEB
    assert cached_before.input_provenance == {"kind": "test"}

    # Complete task A — this triggers _mark_terminal
    gate.set()
    await rt.wait(h_a.task_id, timeout=5.0)

    # Now task A is done, but task B is still pending.
    # Envelope MUST still be cached.
    cached_after_a = rt._last_envelope_by_session.get("agent-1::sess-env-ret-1")
    assert cached_after_a is not None, (
        "Envelope was evicted when task A completed, but task B "
        "is still pending!  TaskRuntime.send() will build a generic "
        "System envelope and lose channel routing metadata."
    )

    # send() still works — the envelope in the cache is preserved
    send_handle = await rt.send("agent-1::sess-env-ret-1", "follow-up message")
    assert send_handle is not None
    # Envelope should still be in the dict (not evicted)
    cached_still = rt._last_envelope_by_session.get("agent-1::sess-env-ret-1")
    assert cached_still is not None, "envelope evicted despite pending task B"
    assert cached_still.source_kind == SourceKind.WEB

    # Complete task B — now the envelope should be evicted
    await rt.wait(h_b.task_id, timeout=5.0)

    # Give event loop a tick for _mark_terminal to run
    await asyncio.sleep(0.02)

    cached_after_b = rt._last_envelope_by_session.get("agent-1::sess-env-ret-1")
    assert cached_after_b is None, (
        "Envelope was not evicted after the last task completed"
    )


@pytest.mark.asyncio
async def test_envelope_evicted_when_no_more_work() -> None:
    """Single-task completion (no pending work) evicts the envelope as before."""
    rt = TaskRuntime(
        storage=_make_storage(),
        turn_handler=None,
        max_concurrency=1,
        max_pending_per_session=64,
    )

    async def _handler(_run: Any) -> None:
        pass

    rt._turn_handler = _handler  # type: ignore[assignment]

    env = _make_envelope("agent-1::sess-env-evict-1")
    h = await rt.enqueue(env, "single-task")
    await rt.wait(h.task_id, timeout=5.0)
    await asyncio.sleep(0.02)

    cached = rt._last_envelope_by_session.get("agent-1::sess-env-evict-1")
    assert cached is None, (
        "Envelope should be evicted when the only task completes"
    )


@pytest.mark.asyncio
async def test_send_uses_cached_envelope_for_pending_session() -> None:
    """send() reuses cached route envelope while session has queued work."""
    gate = asyncio.Event()

    async def _blocking_handler(_run: Any) -> None:
        await gate.wait()

    rt = TaskRuntime(
        storage=_make_storage(),
        turn_handler=_blocking_handler,
        max_concurrency=1,
    )

    env = _make_envelope("agent-1::sess-send-2")
    h_a = await rt.enqueue(env, "task-a")
    await asyncio.sleep(0.05)
    h_b = await rt.enqueue(env, "task-b")

    # Complete task A
    gate.set()
    await rt.wait(h_a.task_id, timeout=5.0)

    # send() should still work — the envelope dict should be preserved
    result = await rt.send("agent-1::sess-send-2", "hello after task-a")
    assert result is not None
    # Check the internal dict for cached envelope
    cached = rt._last_envelope_by_session.get("agent-1::sess-send-2")
    assert cached is not None, "envelope evicted despite pending work"
    assert cached.source_kind == SourceKind.WEB

    await rt.wait(h_b.task_id, timeout=5.0)
