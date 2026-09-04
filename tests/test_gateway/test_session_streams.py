from agentos.gateway.session_streams import SessionStreamRegistry


def test_session_stream_registry_records_monotonic_stream_seq() -> None:
    registry = SessionStreamRegistry(max_events_per_session=5)

    first = registry.record("agent:main:test", "session.event.text_delta", {"text": "a"})
    second = registry.record("agent:main:test", "session.event.done", {"reason": "stop"})

    assert first["stream_seq"] == 1
    assert second["stream_seq"] == 2
    assert second["session_key"] == "agent:main:test"
    assert registry.current_seq("agent:main:test") == 2


def test_session_stream_registry_replays_events_after_cursor() -> None:
    registry = SessionStreamRegistry(max_events_per_session=5)
    registry.record("agent:main:test", "session.event.text_delta", {"text": "a"})
    registry.record("agent:main:test", "session.event.text_delta", {"text": "b"})

    replay = registry.replay("agent:main:test", 1)

    assert replay.current_stream_seq == 2
    assert replay.replay_complete is True
    assert [event.payload["text"] for event in replay.events] == ["b"]


def test_session_stream_registry_reports_incomplete_replay() -> None:
    registry = SessionStreamRegistry(max_events_per_session=2)
    registry.record("agent:main:test", "session.event.text_delta", {"text": "a"})
    registry.record("agent:main:test", "session.event.text_delta", {"text": "b"})
    registry.record("agent:main:test", "session.event.text_delta", {"text": "c"})

    replay = registry.replay("agent:main:test", 0)

    assert replay.current_stream_seq == 3
    assert replay.replay_complete is False
    assert [event.stream_seq for event in replay.events] == [2, 3]


def test_session_stream_registry_preserves_meta_step_control_events() -> None:
    registry = SessionStreamRegistry(max_events_per_session=5)
    registry.record(
        "agent:main:test",
        "session.event.tool_use_start",
        {"tool_name": "meta-step:writing_plan", "tool_use_id": "meta_step_writing_plan"},
    )
    registry.record(
        "agent:main:test",
        "session.event.tool_result",
        {
            "tool_name": "meta-step:writing_plan",
            "tool_use_id": "meta_step_writing_plan",
            "result": "ok",
        },
    )
    for index in range(10):
        registry.record(
            "agent:main:test",
            "session.event.text_delta",
            {"text": f"chunk-{index}"},
        )

    replay = registry.replay("agent:main:test", 0)

    tool_events = [
        event
        for event in replay.events
        if event.payload.get("tool_name") == "meta-step:writing_plan"
    ]
    assert [event.event_name for event in tool_events] == [
        "session.event.tool_use_start",
        "session.event.tool_result",
    ]


def test_session_stream_registry_reports_reset_when_client_cursor_is_ahead() -> None:
    registry = SessionStreamRegistry(max_events_per_session=5)

    replay = registry.replay("agent:main:after-restart", 5)

    assert replay.current_stream_seq == 0
    assert replay.replay_complete is False
    assert replay.gap_reason == "stream_buffer_reset"
    assert replay.events == []


def test_discard_clears_session_state() -> None:
    """discard() removes seq and events for a known session."""
    registry = SessionStreamRegistry(max_events_per_session=10)
    registry.record("sess-a", "session.event.text_delta", {"text": "hi"})
    registry.record("sess-b", "session.event.done", {"reason": "stop"})

    registry.discard("sess-a")

    # sess-a is gone
    assert registry.current_seq("sess-a") == 0
    replay = registry.replay("sess-a", 0)
    assert replay.events == []
    assert replay.gap_reason is None

    # sess-b is untouched
    assert registry.current_seq("sess-b") == 1
    replay_b = registry.replay("sess-b", 0)
    assert len(replay_b.events) == 1


def test_discard_is_idempotent() -> None:
    """discard() for unknown or already-discarded keys does not raise."""
    registry = SessionStreamRegistry(max_events_per_session=10)
    registry.discard("never-existed")
    registry.discard("never-existed")
    assert registry.current_seq("never-existed") == 0


def test_discard_after_replay_preserves_new_data() -> None:
    """After discard, fresh records start from stream_seq=1."""
    registry = SessionStreamRegistry(max_events_per_session=10)
    registry.record("sess", "session.event.text_delta", {"text": "old"})
    registry.discard("sess")

    entry = registry.record("sess", "session.event.text_delta", {"text": "new"})
    assert entry["stream_seq"] == 1
    assert registry.current_seq("sess") == 1
