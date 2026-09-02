"""Tests for the 25 MB size ceiling in send_file across all channels.

The ``_check_file_size`` helper is in ``_util.py`` (shared across all channel
adapters), so a single test suite covers every adapter.  Each channel adapter
also calls ``_check_file_size`` in its own ``send_file`` — this module tests
the shared logic, plus an import check that every adapter uses it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agentos.channels._util import _SEND_FILE_MAX_BYTES, _check_file_size


class TestSendFileConstants:
    """The constant must be exactly 25 MiB."""

    def test_constant_value(self) -> None:
        assert _SEND_FILE_MAX_BYTES == 25 * 1024 * 1024

    def test_constant_imported_by_email(self) -> None:
        from agentos.channels._util import _SEND_FILE_MAX_BYTES

        assert _SEND_FILE_MAX_BYTES == 25 * 1024 * 1024

    def test_constant_imported_by_discord(self) -> None:
        from agentos.channels._util import _SEND_FILE_MAX_BYTES

        assert _SEND_FILE_MAX_BYTES == 25 * 1024 * 1024

    def test_constant_imported_by_telegram(self) -> None:
        from agentos.channels._util import _SEND_FILE_MAX_BYTES

        assert _SEND_FILE_MAX_BYTES == 25 * 1024 * 1024


class TestCheckFileSize:
    """_check_file_size rejects files above the limit."""

    def test_small_file_accepted(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"x" * 1024)
            p = Path(f.name)
        _check_file_size(p)  # must not raise
        p.unlink()

    def test_large_file_raises(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".big") as f:
            f.write(b"x" * 30 * 1024 * 1024)  # 30 MB > 25 MB limit
            p = Path(f.name)
        try:
            _check_file_size(p)
            assert False, "Expected ValueError"
        except ValueError as e:
            msg = str(e)
            assert "send_file refused" in msg
            assert "30.0 MB" in msg
            assert "25 MB" in msg
        finally:
            p.unlink()

    def test_exact_limit_accepted(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".exact") as f:
            f.write(b"x" * (25 * 1024 * 1024))  # exactly 25 MB
            p = Path(f.name)
        _check_file_size(p)  # must not raise (at limit, not over)
        p.unlink()

    def test_file_not_found_raises(self) -> None:
        """Non-existent file is rejected."""
        from agentos.channels._util import _check_file_size

        try:
            _check_file_size(Path("/tmp/nonexistent_file_for_test_xyz"))
            assert False, "Expected OSError or ValueError"
        except (OSError, ValueError):
            pass

    def test_zero_byte_file_accepted(self) -> None:
        """Zero-byte file (edge case) is accepted."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".empty") as f:
            p = Path(f.name)
        _check_file_size(p)  # must not raise
        p.unlink()
