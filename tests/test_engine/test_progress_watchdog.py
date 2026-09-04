from __future__ import annotations

from agentos.engine.progress_watchdog import (
    ProgressObservation,
    ProgressWatchdog,
    canonical_arguments,
    guidance_for,
    tool_call_signature,
)


def _call(tool: str, arguments: dict, result: str, *, is_error: bool = False):
    return tool_call_signature(tool, arguments, result, is_error=is_error)


def test_progress_watchdog_observes_progress_and_resets_repeated_errors() -> None:
    watchdog = ProgressWatchdog(repeated_tool_error_threshold=2)

    first = watchdog.observe(ProgressObservation(iteration=1, tool_error_signature="tool:error"))
    progress = watchdog.observe(ProgressObservation(iteration=2, successful_tool_result=True))
    after_reset = watchdog.observe(
        ProgressObservation(iteration=3, tool_error_signature="tool:error")
    )

    assert first.action == "observe"
    assert progress.reason == "progress"
    assert after_reset.action == "observe"


def test_progress_watchdog_warns_in_observe_only_mode() -> None:
    watchdog = ProgressWatchdog(repeated_tool_error_threshold=2, observe_only=True)

    watchdog.observe(ProgressObservation(iteration=1, tool_error_signature="same"))
    decision = watchdog.observe(ProgressObservation(iteration=2, tool_error_signature="same"))

    assert decision.action == "warn"
    assert decision.reason == "repeated_tool_error"
    assert decision.details["count"] == 2
    assert decision.details["iteration"] == 2
    assert decision.details["provider_call_count"] == 0


def test_progress_watchdog_blocks_only_when_enabled() -> None:
    watchdog = ProgressWatchdog(
        repeated_provider_failure_threshold=2,
        observe_only=False,
    )

    watchdog.observe(ProgressObservation(iteration=1, provider_failure_signature="timeout"))
    decision = watchdog.observe(
        ProgressObservation(iteration=2, provider_failure_signature="timeout")
    )

    assert decision.action == "block"
    assert decision.reason == "repeated_provider_failure"


def test_a_succeeding_call_repeated_with_the_same_result_is_flagged() -> None:
    # Every one of these "succeeds", so successful_tool_result is True each
    # time. Without the call signature the turn looks productive.
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3)
    call = _call("read_file", {"path": "/a.py"}, "contents")

    decisions = [
        watchdog.observe(
            ProgressObservation(iteration=i, successful_tool_result=True, tool_calls=(call,))
        )
        for i in range(1, 4)
    ]

    assert [d.reason for d in decisions[:2]] == ["progress", "progress"]
    assert decisions[2].reason == "repeated_tool_call"
    assert decisions[2].details["tool"] == "read_file"
    assert decisions[2].details["count"] == 3


def test_a_changed_result_resets_the_repeat_count() -> None:
    # Re-reading a file that changed is real work, not a loop.
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3)
    args = {"path": "/a.py"}

    watchdog.observe(ProgressObservation(iteration=1, tool_calls=(_call("read_file", args, "v1"),)))
    watchdog.observe(ProgressObservation(iteration=2, tool_calls=(_call("read_file", args, "v1"),)))
    watchdog.observe(ProgressObservation(iteration=3, tool_calls=(_call("read_file", args, "v2"),)))
    decision = watchdog.observe(
        ProgressObservation(iteration=4, tool_calls=(_call("read_file", args, "v2"),))
    )

    assert decision.reason != "repeated_tool_call"


def test_different_arguments_are_tracked_separately() -> None:
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=2)

    watchdog.observe(
        ProgressObservation(iteration=1, tool_calls=(_call("read_file", {"path": "/a"}, "x"),))
    )
    decision = watchdog.observe(
        ProgressObservation(iteration=2, tool_calls=(_call("read_file", {"path": "/b"}, "x"),))
    )

    assert decision.reason != "repeated_tool_call"


def test_argument_key_order_does_not_hide_a_repeat() -> None:
    first = tool_call_signature("grep_search", {"a": 1, "b": 2}, "hit")
    second = tool_call_signature("grep_search", {"b": 2, "a": 1}, "hit")

    assert first.arguments_hash == second.arguments_hash


def test_unserializable_arguments_still_produce_a_signature() -> None:
    signature = tool_call_signature("x", {"fn": object()}, "result")

    assert signature.arguments_hash
    assert canonical_arguments({"fn": object()})


def test_repeat_guidance_names_the_tool_and_the_count() -> None:
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=2)
    call = _call("web_search", {"q": "x"}, "same answer")

    watchdog.observe(ProgressObservation(iteration=1, tool_calls=(call,)))
    decision = watchdog.observe(ProgressObservation(iteration=2, tool_calls=(call,)))

    guidance = guidance_for(decision)
    assert "web_search" in guidance
    assert "2 times" in guidance


def test_guidance_is_empty_for_an_ordinary_observation() -> None:
    watchdog = ProgressWatchdog()

    decision = watchdog.observe(ProgressObservation(iteration=1, successful_tool_result=True))

    assert guidance_for(decision) == ""


# ======================================================================
# ProgressWatchdog dict-leak tests (_repeat_counts / _repeat_results)
# ======================================================================


def _calln(tool: str, args: dict, result: str) -> object:
    return tool_call_signature(tool, args, result)


class TestProgressWatchdogEviction:
    def test_trim_to_fit_under_cap_no_eviction(self) -> None:
        dog = ProgressWatchdog(max_repeat_entries=10)
        dog._repeat_counts[("a", "1")] = 1
        dog._repeat_results[("a", "1")] = "r"
        dog._trim_to_fit()
        assert len(dog._repeat_counts) == 1

    def test_trim_to_fit_evicts_oldest(self) -> None:
        dog = ProgressWatchdog(max_repeat_entries=2)
        dog._repeat_counts[("a", "1")] = 1
        dog._repeat_results[("a", "1")] = "r1"
        dog._repeat_counts[("b", "2")] = 1
        dog._repeat_results[("b", "2")] = "r2"
        dog._repeat_counts[("c", "3")] = 1  # triggers evict
        dog._repeat_results[("c", "3")] = "r3"
        dog._trim_to_fit()
        assert ("a", "1") not in dog._repeat_counts
        assert ("b", "2") in dog._repeat_counts
        assert ("c", "3") in dog._repeat_counts

    def test_trim_to_fit_syncs_both_dicts(self) -> None:
        dog = ProgressWatchdog(max_repeat_entries=1)
        dog._repeat_counts[("a", "1")] = 1
        dog._repeat_results[("a", "1")] = "r1"
        dog._repeat_counts[("b", "2")] = 1  # triggers evict of a
        dog._repeat_results[("b", "2")] = "r2"
        dog._trim_to_fit()
        assert ("a", "1") not in dog._repeat_counts
        assert ("a", "1") not in dog._repeat_results

    def test_observe_triggers_trim_when_over_cap(self) -> None:
        dog = ProgressWatchdog(repeated_tool_call_threshold=2, max_repeat_entries=2)
        call1 = _calln("a", {"p": "1"}, "r1")
        call2 = _calln("b", {"p": "2"}, "r2")
        call3 = _calln("c", {"p": "3"}, "r3")

        dog.observe(ProgressObservation(iteration=1, tool_calls=(call1,)))
        dog.observe(ProgressObservation(iteration=2, tool_calls=(call2,)))
        dog.observe(ProgressObservation(iteration=3, tool_calls=(call3,)))

        assert len(dog._repeat_counts) <= 2

    def test_default_max_repeat_entries_positive(self) -> None:
        from agentos.engine.progress_watchdog import _DEFAULT_MAX_REPEAT_ENTRIES
        assert _DEFAULT_MAX_REPEAT_ENTRIES > 0

    def test_custom_max_repeat_entries_accepted(self) -> None:
        dog = ProgressWatchdog(max_repeat_entries=50)
        assert dog._max_repeat_entries == 50

    def test_clear_wipes_both_dicts(self) -> None:
        dog = ProgressWatchdog()
        dog._repeat_counts[("a", "1")] = 1
        dog._repeat_results[("a", "1")] = "r"
        dog.clear()
        assert len(dog._repeat_counts) == 0
        assert len(dog._repeat_results) == 0
