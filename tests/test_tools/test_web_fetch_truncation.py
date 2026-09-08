"""Tests for web_fetch max_chars clamping."""

from agentos.tools.builtin.web_fetch import _apply_max_chars, _resolve_effective_max_chars


def test_resolve_effective_max_chars_clamps_below_100() -> None:
    """max_chars < 100 must be clamped to 100, not disabled entirely (#1400)."""
    result = _resolve_effective_max_chars(50)
    assert result is not None, "max_chars=50 should be clamped, not None"
    assert result >= 100, f"Expected >=100, got {result}"

    result = _resolve_effective_max_chars(0)
    assert result is not None
    assert result >= 100


def test_resolve_effective_max_chars_passes_safe_values() -> None:
    """Normal max_chars values pass through unchanged (subject to max_allowed)."""
    result = _resolve_effective_max_chars(100)
    assert result is not None
    assert result == 100

    result = _resolve_effective_max_chars(500)
    assert result is not None
    assert result == 500


def test_apply_max_chars_respects_clamped_limit() -> None:
    """_apply_max_chars must truncate when capped, not return full content."""
    full_text = "A" * 50000
    result = _apply_max_chars({"text": full_text, "url": "https://example.com"}, 100)
    # The wrapped output includes source info, but returned_length must be <= 100
    gotten = result.get("returned_length", 0)
    assert gotten <= 100, f"Expected <=100, got {gotten}"
    assert result.get("original_length", 0) == 50000
    assert result.get("truncated") is True
