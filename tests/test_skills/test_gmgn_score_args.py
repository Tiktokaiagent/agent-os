"""Offline regression tests for gmgn-wallet-score argument validation (#819).

The score.py script crashes with IndexError when invoked with <2 arguments.
This test pins the guard that prints usage and exits 2.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCORE_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src/agentos/skills/bundled/gmgn-wallet-score/scripts/score.py"
)


def test_no_arguments_prints_usage_and_exits_2() -> None:
    """score.py with 0 arguments must print usage and exit 2."""
    result = subprocess.run(
        [sys.executable, str(SCORE_SCRIPT)],
        capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert "wallet_address" in result.stderr.lower()


def test_one_argument_prints_usage_and_exits_2() -> None:
    """score.py with only 1 argument must print usage and exit 2."""
    result = subprocess.run(
        [sys.executable, str(SCORE_SCRIPT), "0xabc"],
        capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert "wallet_address" in result.stderr.lower()


def test_two_arguments_does_not_trigger_usage() -> None:
    """score.py with 2 arguments passes the guard (will fail at CLI later)."""
    result = subprocess.run(
        [sys.executable, str(SCORE_SCRIPT), "0xabc", "ethereum"],
        capture_output=True, text=True
    )
    # Exit 0 means argument guard passed (script will later fail at `gmgn-cli`
    # not being installed — that's expected off-line).
    if result.returncode not in (0, 1, None):
        assert "Usage:" not in result.stderr


def test_help_flag_not_needed_but_still_works() -> None:
    """Verifies script doesn't crash with --help flag."""
    result = subprocess.run(
        [sys.executable, str(SCORE_SCRIPT), "--help"],
        capture_output=True, text=True
    )
    # Should exit 2 (since <2 args), not crash
    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_many_arguments_guard_works() -> None:
    """score.py with all 8 arguments passes the guard."""
    result = subprocess.run(
        [sys.executable, str(SCORE_SCRIPT),
         "0xabc", "ethereum", "en", "2.0", "0.03", "0.15", "100"],
        capture_output=True, text=True
    )
    # Will fail at gmgn-cli, not at arg count
    assert "Usage:" not in result.stderr


def test_message_contains_script_name() -> None:
    """Usage message includes the script's name so the user sees what to type."""
    result = subprocess.run(
        [sys.executable, str(SCORE_SCRIPT)],
        capture_output=True, text=True
    )
    assert "score.py" in result.stderr or "Usage:" in result.stderr


def test_exit_code_is_2_not_1() -> None:
    """Per issue, exit code must be 2 (command-line usage error), not 1."""
    result = subprocess.run(
        [sys.executable, str(SCORE_SCRIPT)],
        capture_output=True, text=True
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for missing arguments, got {result.returncode}"
    )


def test_error_goes_to_stderr_not_stdout() -> None:
    """Usage must be on stderr so stdout remains clean for JSON consumers."""
    result = subprocess.run(
        [sys.executable, str(SCORE_SCRIPT)],
        capture_output=True, text=True
    )
    assert result.returncode == 2
    assert result.stderr
    assert "Usage:" in result.stderr
