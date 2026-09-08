"""Tests for identity/parser.py — IDENTITY.md parsing and inline stripping."""

from agentos.identity.parser import parse_identity


def test_identity_preserves_underscores_in_values() -> None:
    """Underscores inside words must be kept; only emphasis markers stripped.

    Per CommonMark, _ underscores are only emphasis delimiters when they
    are flanked by word-boundary characters. A bare _ inside
    snake_case, my_agent_name, etc. is literal (issue #1428).
    """
    doc = """# Identity
- name: my_agent_name
- creature: snake_case_bot
- theme: _cyberpunk_
"""
    result = parse_identity(doc)
    assert result.name == "my_agent_name"
    assert result.creature == "snake_case_bot"
    assert result.theme == "cyberpunk"


def test_identity_bold_and_code_are_stripped() -> None:
    """Bold, italic, and code markers are still stripped normally."""
    doc = """# Identity
- name: **Alice**
- creature: `robot`
- emoji: ~~nah~~
"""
    result = parse_identity(doc)
    assert result.name == "Alice"
    assert result.creature == "robot"


def test_identity_single_underscore_unaffected() -> None:
    """A single underscore between two words is preserved."""
    doc = """# Identity
- name: x_y
"""
    result = parse_identity(doc)
    assert result.name == "x_y"


def test_identity_strips_surrounding_emphasis() -> None:
    """Emphasis markers around a word are still stripped."""
    doc = """# Identity
- name: _hello_
- creature: __hello__
"""
    result = parse_identity(doc)
    assert result.name == "hello"
    assert result.creature == "hello"
