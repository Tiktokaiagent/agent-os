"""Spend budgets: ledger accumulation, ceiling evaluation, and turn-loop enforcement."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.types import ErrorEvent, WarningEvent
from agentos.engine.usage import UsageTracker
from agentos.gateway.config import BudgetsConfig

SESSION = "agent:main:telegram:acct:peer"


def _spend(tracker: UsageTracker, session_key: str, cost: float) -> None:
    """Record ``cost`` dollars of provider-billed spend on ``session_key``."""
    tracker.add(
        session_key,
        input_tokens=100,
        output_tokens=10,
        model_id="test-model",
        billed_cost=cost,
        provider_id="test",
    )


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# ── Ledger ──────────────────────────────────────────────────────────────


def test_daily_ledger_accumulates_without_a_database() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 1.25)
    _spend(tracker, SESSION, 0.75)

    day = _today()
    assert tracker.get_spend(day, "gateway", "global") == pytest.approx(2.0)
    assert tracker.get_spend(day, "agent", "main") == pytest.approx(2.0)
    assert tracker.get_spend(day, "channel", "telegram") == pytest.approx(2.0)


def test_daily_ledger_survives_a_restart(tmp_path: Path) -> None:
    db = str(tmp_path / "spend_ledger.db")
    first = UsageTracker(ledger_db_path=db)
    _spend(first, SESSION, 3.0)

    # A fresh tracker on the same DB stands in for a gateway restart: the
    # whole point of the ledger is that a crash cannot reset a daily ceiling.
    restarted = UsageTracker(ledger_db_path=db)
    assert restarted.get_spend(_today(), "gateway", "global") == pytest.approx(3.0)
    hard_stop, _ = restarted.check_budget_limits(SESSION, BudgetsConfig(daily_limit=3.0))
    assert hard_stop is True


def test_session_ceiling_survives_a_restart(tmp_path: Path) -> None:
    """A crash-and-respawn must not hand a session a fresh allowance."""
    db = str(tmp_path / "spend_ledger.db")
    first = UsageTracker(ledger_db_path=db)
    _spend(first, SESSION, 4.0)

    restarted = UsageTracker(ledger_db_path=db)
    assert restarted.get_effective_session_cost(SESSION) == pytest.approx(4.0)

    config = BudgetsConfig(session_limit=5.0)
    assert restarted.check_budget_limits(SESSION, config) == (False, None)
    # The post-restart turn's own spend adds to the pre-restart total rather
    # than starting the count over.
    _spend(restarted, SESSION, 1.0)
    assert restarted.check_budget_limits(SESSION, config)[0] is True


def test_a_dropped_ledger_write_cannot_retire_a_ceiling(tmp_path: Path) -> None:
    """The persisted row can only under-report; reads take the larger value."""
    db = str(tmp_path / "spend_ledger.db")
    tracker = UsageTracker(ledger_db_path=db)
    _spend(tracker, SESSION, 6.0)

    # Simulate a write that never landed (sqlite busy, disk full): the row
    # lags behind what this process has actually spent.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE spend_ledger SET cost_usd = 1.0")

    assert tracker.get_spend(_today(), "gateway", "global") == pytest.approx(6.0)
    assert tracker.check_budget_limits(SESSION, BudgetsConfig(daily_limit=5.0))[0] is True


def test_ledger_ignores_zero_cost_usage() -> None:
    tracker = UsageTracker()
    tracker.add(SESSION, input_tokens=0, output_tokens=0, model_id="test-model")

    assert tracker.get_spend(_today(), "gateway", "global") == 0.0


# ── Ceiling evaluation ──────────────────────────────────────────────────


def test_no_config_and_empty_config_enforce_nothing() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 100.0)

    assert tracker.check_budget_limits(SESSION, None) == (False, None)
    assert tracker.check_budget_limits(SESSION, BudgetsConfig()) == (False, None)


def test_disabled_switch_suspends_configured_ceilings() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 10.0)

    config = BudgetsConfig(enabled=False, session_limit=1.0, session_warn=0.5)
    assert tracker.check_budget_limits(SESSION, config) == (False, None)


def test_session_limit_hard_stops_at_the_ceiling() -> None:
    tracker = UsageTracker()
    config = BudgetsConfig(session_limit=2.0)

    _spend(tracker, SESSION, 1.5)
    assert tracker.check_budget_limits(SESSION, config) == (False, None)

    _spend(tracker, SESSION, 0.5)
    hard_stop, message = tracker.check_budget_limits(SESSION, config)
    assert hard_stop is True
    assert message is not None
    assert "Session cost" in message
    assert "$2.0000" in message


def test_session_warning_fires_once_and_does_not_stop_the_turn() -> None:
    tracker = UsageTracker()
    config = BudgetsConfig(session_warn=1.0, session_limit=10.0)

    _spend(tracker, SESSION, 1.0)
    hard_stop, message = tracker.check_budget_limits(SESSION, config)
    assert hard_stop is False
    assert message is not None
    assert "warning threshold" in message

    _spend(tracker, SESSION, 1.0)
    assert tracker.check_budget_limits(SESSION, config) == (False, None)


def test_daily_limit_hard_stops_across_sessions() -> None:
    tracker = UsageTracker()
    config = BudgetsConfig(daily_limit=5.0)

    _spend(tracker, "agent:main:telegram:a:1", 3.0)
    _spend(tracker, "agent:main:webchat:b:2", 2.0)

    hard_stop, message = tracker.check_budget_limits("agent:main:webchat:b:2", config)
    assert hard_stop is True
    assert message is not None
    assert "Daily gateway cost" in message


def test_agent_and_channel_daily_ceilings_are_scoped() -> None:
    tracker = UsageTracker()
    _spend(tracker, "agent:trader:telegram:a:1", 4.0)
    _spend(tracker, "agent:writer:webchat:b:2", 1.0)

    config = BudgetsConfig(agent_daily_limit={"trader": 3.0})
    assert tracker.check_budget_limits("agent:trader:telegram:a:1", config)[0] is True
    assert tracker.check_budget_limits("agent:writer:webchat:b:2", config) == (False, None)

    channel_config = BudgetsConfig(channel_daily_limit={"telegram": 3.0})
    assert tracker.check_budget_limits("agent:trader:telegram:a:1", channel_config)[0] is True
    assert tracker.check_budget_limits("agent:writer:webchat:b:2", channel_config) == (False, None)


def test_subagent_spend_is_attributed_to_the_parent_agent() -> None:
    """A fan-out must count against the spawning agent's daily ceiling."""
    tracker = UsageTracker()
    _spend(tracker, "subagent:agent:trader:subagent:child-1", 2.0)
    _spend(tracker, "subagent:agent:trader:subagent:child-2", 2.0)

    config = BudgetsConfig(agent_daily_limit={"trader": 3.0})
    hard_stop, message = tracker.check_budget_limits("agent:trader:telegram:a:1", config)
    assert hard_stop is True
    assert message is not None
    assert "agent 'trader'" in message


def test_hard_stop_wins_over_an_earlier_scope_warning() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 6.0)

    # Session is only at its warn threshold, but the daily ceiling is breached.
    config = BudgetsConfig(session_warn=5.0, session_limit=20.0, daily_limit=6.0)
    hard_stop, message = tracker.check_budget_limits(SESSION, config)
    assert hard_stop is True
    assert message is not None
    assert "Daily gateway cost" in message


# ── Config validation ───────────────────────────────────────────────────


def test_warn_above_its_own_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="session_warn"):
        BudgetsConfig(session_warn=10.0, session_limit=5.0)
    with pytest.raises(ValueError, match="daily_warn"):
        BudgetsConfig(daily_warn=10.0, daily_limit=5.0)
    with pytest.raises(ValueError, match="agent_daily_warn"):
        BudgetsConfig(agent_daily_warn={"main": 10.0}, agent_daily_limit={"main": 5.0})


def test_negative_ceilings_are_rejected() -> None:
    with pytest.raises(ValueError):
        BudgetsConfig(session_limit=-1.0)
    with pytest.raises(ValueError, match="must be >= 0"):
        BudgetsConfig(agent_daily_limit={"main": -1.0})


def test_colliding_scope_keys_are_rejected() -> None:
    """Two spellings of one scope would silently drop one of the numbers."""
    with pytest.raises(ValueError, match="both refer to"):
        BudgetsConfig(agent_daily_limit={"default": 5.0, "main": 100.0})
    with pytest.raises(ValueError, match="both refer to"):
        BudgetsConfig(channel_daily_limit={"Telegram": 5.0, "telegram": 100.0})


def test_scope_keys_are_normalized() -> None:
    config = BudgetsConfig(
        agent_daily_limit={"Default": 5.0},
        channel_daily_limit={"Telegram": 5.0},
    )
    assert config.agent_daily_limit == {"main": 5.0}
    assert config.channel_daily_limit == {"telegram": 5.0}


def test_unknown_budget_keys_are_rejected() -> None:
    with pytest.raises(ValueError):
        BudgetsConfig(sesion_limit=5.0)  # type: ignore[call-arg]


# ── Turn-loop enforcement ───────────────────────────────────────────────


class _RecordingSessionManager:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def append_message(self, session_key: str, *, role: str, content: str) -> None:
        self.messages.append((session_key, role, content))


def _runner(tracker: Any, budgets: Any, manager: Any) -> Any:
    from agentos.engine.runtime import TurnRunner

    return TurnRunner(
        provider_selector=None,
        session_manager=manager,
        usage_tracker=tracker,
        config=SimpleNamespace(budgets=budgets, context_window_tokens=100_000),
    )


async def _collect(runner: Any) -> list[Any]:
    return [event async for event in runner._run_turn("hi", SESSION, "main", None, [])]


@pytest.mark.asyncio
async def test_turn_is_refused_at_the_hard_limit() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 5.0)
    manager = _RecordingSessionManager()
    runner = _runner(tracker, BudgetsConfig(session_limit=5.0), manager)

    events = await _collect(runner)

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ErrorEvent)
    assert error.code == "budget_exceeded"
    assert "budget limit" in error.message
    # The refusal is auditable in the transcript, not only in the log.
    assert manager.messages and manager.messages[0][1] == "system"


@pytest.mark.asyncio
async def test_turn_warns_but_continues_below_the_hard_limit() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 4.0)
    runner = _runner(tracker, BudgetsConfig(session_warn=4.0, session_limit=10.0), None)

    events = await _collect(runner)

    assert isinstance(events[0], WarningEvent)
    assert events[0].code == "budget_warning"
    # The turn was not refused — it proceeded and failed later on the absent
    # provider, which is the pre-existing behavior for this stub runner.
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "budget_exceeded" for event in events
    )


@pytest.mark.asyncio
async def test_turn_runs_when_no_budgets_are_configured() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 100.0)
    runner = _runner(tracker, BudgetsConfig(), None)

    events = await _collect(runner)

    assert not any(
        isinstance(event, ErrorEvent) and event.code == "budget_exceeded" for event in events
    )


@pytest.mark.asyncio
async def test_hard_stop_without_a_message_still_refuses_the_turn() -> None:
    """The refusal must hinge on the decision, not on the presentation text."""

    class _TerseTracker:
        def check_budget_limits(self, session_key: str, config: Any) -> tuple[bool, str | None]:
            return True, None

    runner = _runner(_TerseTracker(), BudgetsConfig(session_limit=1.0), None)

    events = await _collect(runner)

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "budget_exceeded"
    assert events[0].message


@pytest.mark.asyncio
async def test_budget_check_failure_fails_open() -> None:
    """A broken budget check must never be the reason a turn is refused."""

    class _ExplodingTracker:
        def check_budget_limits(self, session_key: str, config: Any) -> tuple[bool, str | None]:
            raise RuntimeError("ledger unavailable")

    runner = _runner(_ExplodingTracker(), BudgetsConfig(session_limit=0.0), None)

    events = await _collect(runner)

    assert not any(
        isinstance(event, ErrorEvent) and event.code == "budget_exceeded" for event in events
    )


# ── Intra-turn enforcement ──────────────────────────────────────────────


class _LoopingToolProvider:
    """A provider that never stops calling a tool — a runaway turn."""

    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def chat(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        config: Any | None = None,
    ) -> Any:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> Any:
        from agentos.provider import DoneEvent as ProviderDone
        from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
        from agentos.provider import ToolUseStartEvent as ProviderToolUseStart

        tool_id = f"tool-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_id, tool_name="echo")
        yield ProviderToolUseEnd(
            tool_use_id=tool_id, tool_name="echo", arguments={"value": "again"}
        )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)


@pytest.mark.asyncio
async def test_spend_guard_stops_a_runaway_loop_inside_one_turn() -> None:
    """The turn-start gate alone leaves an unbounded tool loop unbounded."""
    from agentos.engine import Agent, AgentConfig, ToolResult
    from agentos.provider import ToolDefinition, ToolInputSchema

    async def _echo(call: Any) -> Any:
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ok")

    seen: list[str] = []

    def guard(session_key: str) -> tuple[bool, str | None]:
        seen.append(session_key)
        # Under the ceiling for the first iteration, over it after that.
        return len(seen) > 1, "Daily gateway cost $50.0000 has reached the $50.0000 budget limit."

    provider = _LoopingToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=0),  # unbounded, as the shipped default is
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(
                    properties={"value": {"type": "string"}}, required=["value"]
                ),
            )
        ],
        tool_handler=_echo,
        session_key=SESSION,
        spend_budget_guard=guard,
    )

    events = [event async for event in agent.run_turn("go")]

    assert any(
        getattr(event, "code", "") == "budget_exceeded" and event.kind == "error"
        for event in events
    )
    # It stopped early rather than looping to exhaustion.
    assert len(provider.calls) <= 3
    assert seen == [SESSION] * len(seen)


@pytest.mark.asyncio
async def test_a_broken_guard_does_not_stop_the_turn() -> None:
    from agentos.engine import Agent, AgentConfig

    def guard(session_key: str) -> tuple[bool, str | None]:
        raise RuntimeError("ledger unavailable")

    provider = _LoopingToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        session_key=SESSION,
        spend_budget_guard=guard,
    )

    events = [event async for event in agent.run_turn("go")]

    assert not any(getattr(event, "code", "") == "budget_exceeded" for event in events)


# ── Budget reservations (concurrent fan-out protection) ────────────────


def test_check_budget_limits_creates_a_reservation() -> None:
    """check_budget_limits must reserve headroom when ceilings pass."""
    tracker = UsageTracker()
    config = SimpleNamespace(
        enabled=True,
        max_turn_billed_cost_usd=1.0,
        session_limit=10.0,
    )
    assert len(tracker._budget_slots) == 0
    tracker.check_budget_limits(SESSION, config)
    assert len(tracker._budget_slots) == 1
    assert tracker._budget_slots[0].session_key == SESSION
    assert tracker._budget_slots[0].amount == 1.0


def test_add_releases_one_reservation_per_call() -> None:
    """add() must release exactly one slot per call (FIFO), not all."""
    tracker = UsageTracker()
    config = SimpleNamespace(
        enabled=True,
        max_turn_billed_cost_usd=1.0,
        session_limit=10.0,
    )

    tracker.check_budget_limits("session:a:1", config)
    tracker.check_budget_limits("session:a:2", config)
    tracker.check_budget_limits("session:a:3", config)
    assert len(tracker._budget_slots) == 3

    tracker.add("session:a:1", input_tokens=10, output_tokens=1, model_id="t", billed_cost=0.5)
    assert len(tracker._budget_slots) == 2

    tracker.add("session:a:2", input_tokens=10, output_tokens=1, model_id="t", billed_cost=0.5)
    assert len(tracker._budget_slots) == 1

    tracker.add("session:a:3", input_tokens=10, output_tokens=1, model_id="t", billed_cost=0.5)
    assert len(tracker._budget_slots) == 0


def test_reservation_prevents_concurrent_fan_out_overshoot() -> None:
    """5 concurrent turns with $9.90/$10 spent must admit exactly 1."""
    tracker = UsageTracker()
    config = SimpleNamespace(
        enabled=True,
        max_turn_billed_cost_usd=0.10,
        session_limit=10.00,
    )

    _spend(tracker, SESSION, 9.90)

    admit_count = 0
    for _ in range(5):
        hard_stop, _ = tracker.check_budget_limits(SESSION, config)
        if not hard_stop:
            admit_count += 1

    assert admit_count == 1, f"Expected 1 admission, got {admit_count}"

    tracker.add(SESSION, input_tokens=10, output_tokens=1, model_id="t", billed_cost=0.05)
    assert len(tracker._budget_slots) == 0


def test_ttl_prevents_eternal_leak_from_broken_turns() -> None:
    """Slots must expire so cancelled turns do not leak."""
    tracker = UsageTracker()
    config = SimpleNamespace(
        enabled=True,
        max_turn_billed_cost_usd=1.0,
        session_limit=10.0,
    )

    tracker.check_budget_limits(SESSION, config)
    assert len(tracker._budget_slots) == 1

    tracker._budget_slots[0].created_at = 0.0  # expire

    hs, _ = tracker.check_budget_limits(SESSION, config)
    assert hs is False  # should admit, old slot purged


def test_reservation_default_amount_works_without_max_turn_cost() -> None:
    """When max_turn_billed_cost_usd is not set, fall back to 0.001."""
    tracker = UsageTracker()
    config = SimpleNamespace(
        enabled=True,
        session_limit=10.0,
    )

    tracker.check_budget_limits(SESSION, config)
    assert len(tracker._budget_slots) == 1
    assert tracker._budget_slots[0].amount == 0.001


def test_reservations_count_across_all_daily_scopes() -> None:
    """Reservations inflate daily/agent/channel spend too."""
    tracker = UsageTracker()
    config = SimpleNamespace(
        enabled=True,
        max_turn_billed_cost_usd=2.0,
        daily_limit=5.0,
    )

    _spend(tracker, SESSION, 4.0)

    hs, _ = tracker.check_budget_limits(SESSION, config)
    assert hs is False, "First turn passes: $4 < $5"

    hs, _ = tracker.check_budget_limits(SESSION, config)
    assert hs is True, "Second turn blocked: $4 + $2 reserve = $6 > $5"


def test_reservation_does_not_break_empty_budgets() -> None:
    """No configured budgets must still work."""
    tracker = UsageTracker()
    config = SimpleNamespace(enabled=True)

    hs, _ = tracker.check_budget_limits(SESSION, config)
    assert hs is False
    assert len(tracker._budget_slots) == 1
