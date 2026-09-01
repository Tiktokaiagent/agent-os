"""Issue #678: sessions export path traversal.

The CLI surface is stubbed at ``run_gateway_sync`` — these test the
argument sanitisation (session ID injection, output path traversal),
not the RPC layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.cli import sessions_cmd

runner = CliRunner()


class TestSafeExportFilename:
    def test_replaces_path_separators(self) -> None:
        """Slashes and backslashes must be stripped, not passed through."""
        safe = sessions_cmd._safe_export_filename("../../etc/pwned")
        assert "/" not in safe
        assert "\\" not in safe

    def test_replaces_colons(self) -> None:
        """The original session_id.replace(':', '-') only handled colons."""
        safe = sessions_cmd._safe_export_filename("agent:main:cli:abc")
        assert ":" not in safe
        assert safe == "agent-main-cli-abc"

    def test_rejects_traversal_with_mixed_separators(self) -> None:
        safe = sessions_cmd._safe_export_filename("..\\..\\secret\\key")
        assert "\\" not in safe
        assert "/" not in safe

    def test_empty_after_sanitize_falls_back(self) -> None:
        safe = sessions_cmd._safe_export_filename("///")
        assert safe == "session"

    def test_all_dots_falls_back(self) -> None:
        safe = sessions_cmd._safe_export_filename("...")
        assert safe == "session"

    def test_leading_dot_removed_to_prevent_hidden_file(self) -> None:
        """Leading dots are stripped so the filename is not hidden on Unix."""
        safe = sessions_cmd._safe_export_filename(".hidden")
        assert not safe.startswith(".")

    def test_normal_session_key_passes_through(self) -> None:
        safe = sessions_cmd._safe_export_filename("agent-main-cli-abc")
        assert safe == "agent-main-cli-abc"

    def test_inner_dots_are_preserved(self) -> None:
        """Dots in the middle of a safe name (e.g. version suffixes) stay."""
        safe = sessions_cmd._safe_export_filename("session.v2")
        assert safe == "session.v2"

    def test_trailing_dot_stripped(self) -> None:
        safe = sessions_cmd._safe_export_filename("name.")
        assert not safe.endswith(".")

    def test_dot_only_segments_inside_are_harmless(self) -> None:
        """A ``..`` segment that survives (after slash→hyphen replacement)
        is just part of the filename, not a directory reference — path
        separators are already removed."""
        safe = sessions_cmd._safe_export_filename("../../etc/pwned")
        # ``/`` becomes ``-`` → ``-..-etc-pwned`` — harmless filename.
        assert safe.startswith("-")
        assert ".." in safe  # part of filename, not a traversal


class TestResolveExportTarget:
    def test_no_output_argument_uses_safe_filename(self) -> None:
        target = sessions_cmd._resolve_export_target(
            "agent:main:cli:abc",
            None,
            "json",
        )
        assert str(target) == "agent-main-cli-abc.json"
        assert ":" not in str(target)

    def test_traversal_session_id_gets_path_separators_removed(self) -> None:
        target = sessions_cmd._resolve_export_target(
            "../../etc/pwned",
            None,
            "md",
        )
        assert "/" not in str(target)
        assert "\\" not in str(target)
        # The filename can contain ``..`` (harmless once separators are gone),
        # but it must not be a hidden dotfile.
        assert not Path(str(target)).name.startswith(".")

    def test_explicit_output_within_cwd_passes(self) -> None:
        """A file in the current working directory is accepted."""
        cwd = Path.cwd()
        target = sessions_cmd._resolve_export_target(
            "any",
            cwd / "test-export-tmp.md",
            "md",
        )
        assert str(target) == str(cwd / "test-export-tmp.md")

    def test_explicit_output_outside_cwd_is_rejected(self) -> None:
        """Path traversal via --output must be blocked."""
        from click.exceptions import BadParameter

        with pytest.raises(BadParameter, match="outside the working directory"):
            sessions_cmd._resolve_export_target(
                "any",
                Path("/tmp/outside.md"),
                "md",
            )

    def test_traversal_path_in_explicit_output_is_rejected(self) -> None:
        """--output with ../ traversal must be rejected."""
        from click.exceptions import BadParameter

        traversal = Path("../../etc/pwned.json").resolve()
        with pytest.raises(BadParameter, match="outside the working directory"):
            sessions_cmd._resolve_export_target(
                "any",
                traversal,
                "json",
            )
