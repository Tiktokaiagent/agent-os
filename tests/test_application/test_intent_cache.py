"""Regression tests for IntentApprovalCache compound-command bypass fix.

These test the `_extract_rm_targets` fix for issue #563 (bare root wipe,
trailing targets in compound rm commands) and the over-block regression
introduced by an earlier attempt (PR #564).

See https://github.com/use-agent-os/agent-os/issues/563
"""

from __future__ import annotations

from agentos.application.intent_cache import (
    IntentApprovalCache,
    _extract_intents,
    _extract_rm_targets,
)


class TestExtractRmTargets:
    """Direct tests for _extract_rm_targets — the core fix."""

    def test_basic_rm(self) -> None:
        assert _extract_rm_targets("rm -rf /a /b") == ["/a", "/b"]

    def test_compound_second_rm_found(self) -> None:
        """Issue #563: second rm in compound command must be scanned."""
        targets = _extract_rm_targets("rm /tmp/safe; rm /root/.ssh/id_rsa")
        assert "/root/.ssh/id_rsa" in targets

    def test_non_rm_segment_not_scanned(self) -> None:
        """PR #564 regression: non-rm segments must not be scanned."""
        targets = _extract_rm_targets("rm -rf build && cat ~/.ssh/config")
        assert "build" in targets
        assert "~/.ssh" not in targets
        assert ".ssh" not in targets

    def test_root_wipe(self) -> None:
        """Issue #563: bare / must be extracted as a target."""
        targets = _extract_rm_targets("rm -rf /")
        assert "/" in targets

    def test_compound_root_wipe(self) -> None:
        """Issue #563: root wipe in a compound command must be caught."""
        targets = _extract_rm_targets("rm /tmp/safe; rm -rf /")
        assert "/" in targets

    def test_no_rm_returns_empty(self) -> None:
        assert _extract_rm_targets("echo hello") == []

    def test_empty_command(self) -> None:
        assert _extract_rm_targets("") == []

    def test_pipe_with_rm(self) -> None:
        targets = _extract_rm_targets("rm /tmp/safe | grep foo")
        assert "/tmp/safe" in targets

    def test_or_or_with_non_rm_tail_not_leaked(self) -> None:
        targets = _extract_rm_targets("rm /tmp/a || echo ~/.ssh/id_rsa")
        assert "/tmp/a" in targets
        assert ".ssh" not in targets

    def test_rm_over_root_finds_root(self) -> None:
        """rm -rf / and rm -rf /* both produce root."""
        assert "/" in _extract_rm_targets("rm -rf /")
        assert "/*" in _extract_rm_targets("rm -rf /*")


class TestCompoundCommandSeparatorBypass:
    """Every shell separator must split rm segments for permission checking."""

    def _check_separator(self, separator: str) -> None:
        cache = IntentApprovalCache()
        command = f"rm /a{separator} rm /b"
        intents = _extract_intents(command)
        cache.approve([i for i in intents if i[1] == "/a"])
        checked = cache.check(intents)
        assert ("delete", "/a") in checked, "/a should be approved"
        assert ("delete", "/b") not in checked, "/b should NOT be approved"

    def test_semicolon(self) -> None:
        self._check_separator(";")

    def test_and_and(self) -> None:
        self._check_separator(" && ")

    def test_or_or(self) -> None:
        self._check_separator(" || ")

    def test_pipe(self) -> None:
        self._check_separator(" | ")

    def test_ampersand(self) -> None:
        self._check_separator(" & ")

    def test_newline(self) -> None:
        self._check_separator("\n")


class TestMultiTargetApproval:
    """Multi-target commands must require approval for all targets."""

    def test_all_targets_approved_passes(self) -> None:
        cache = IntentApprovalCache()
        intents = _extract_intents("rm /a /b")
        cache.approve(intents)
        checked = cache.check(intents)
        assert len(checked) == 2

    def test_extra_target_not_approved_fails(self) -> None:
        cache = IntentApprovalCache()
        cache.approve(_extract_intents("rm /a /b"))
        with_extra = _extract_intents("rm /a /b /c")
        checked = cache.check(with_extra)
        assert ("delete", "/c") not in checked


class TestRecordAndCheck:
    """Basic record/check lifecycle."""

    def test_empty_command_returns_empty(self) -> None:
        assert _extract_intents("") == []

    def test_non_rm_command_returns_empty(self) -> None:
        assert _extract_intents("echo hello") == []

    def test_approve_and_check(self) -> None:
        cache = IntentApprovalCache()
        intents = _extract_intents("rm /a")
        cache.approve(intents)
        assert cache.check(intents) == intents

    def test_unapproved_returns_empty(self) -> None:
        cache = IntentApprovalCache()
        assert cache.check(_extract_intents("rm /a")) == []


class TestSensitivePathRoot:
    """Issue #563: is_sensitive_path must block /, /*, etc."""

    def test_root_is_sensitive(self) -> None:
        from agentos.sandbox.sensitive_paths import is_sensitive_path

        assert is_sensitive_path("/") is not None
        assert is_sensitive_path("/.") is not None
        assert is_sensitive_path("//") is not None
        assert is_sensitive_path("/*") is not None


class TestSensitiveTargetInCommandIntegration:
    """Integration: sensitive_target_in_command with root + compound."""

    def test_root_wipe_blocked(self) -> None:
        from agentos.sandbox.sensitive_paths import sensitive_target_in_command

        assert sensitive_target_in_command("rm -rf /") is not None

    def test_compound_root_wipe_blocked(self) -> None:
        from agentos.sandbox.sensitive_paths import sensitive_target_in_command

        assert sensitive_target_in_command("rm /tmp/safe; rm -rf /") is not None

    def test_compound_root_wipe_via_and_and(self) -> None:
        from agentos.sandbox.sensitive_paths import sensitive_target_in_command

        assert sensitive_target_in_command("rm /tmp/safe && rm -rf /") is not None

    def test_compound_trailing_sensitive_target(self) -> None:
        from agentos.sandbox.sensitive_paths import sensitive_target_in_command

        assert (
            sensitive_target_in_command("rm /tmp/safe; rm /root/.ssh/id_rsa")
            is not None
        )
