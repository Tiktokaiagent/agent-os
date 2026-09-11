"""Tests for _wait_exec_process event-driven replacement (issue #1470)."""

import asyncio
from agentos.tools.builtin.shell import _wait_exec_process


class _FakeProc:
    def __init__(self, exit_delay: float = 0):
        self._exit_delay = exit_delay
        self._returncode = None

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        await asyncio.sleep(self._exit_delay)
        self._returncode = 0
        return 0


async def test_exits_immediately():
    p = _FakeProc(exit_delay=0)
    assert await _wait_exec_process(p, timeout=5) is True
    assert p.returncode == 0


async def test_timeout_returns_false():
    p = _FakeProc(exit_delay=10)
    assert await _wait_exec_process(p, timeout=0.05) is False
    assert p.returncode is None


async def test_already_exited():
    p = _FakeProc(exit_delay=0)
    await p.wait()
    assert await _wait_exec_process(p, timeout=1) is True
