"""Tests for Prettier CRLF fix on Windows (#825)."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_prettierrc_has_end_of_line():
    """Verify .prettierrc.json configures endOfLine to prevent CRLF failures."""
    prettierrc = REPO_ROOT / "frontend" / ".prettierrc.json"
    assert prettierrc.exists(), ".prettierrc.json not found"

    with open(prettierrc) as f:
        config = json.load(f)

    assert "endOfLine" in config, "endOfLine must be set in .prettierrc.json"
    assert config["endOfLine"] == "lf", (
        f"endOfLine should be 'lf' (got '{config['endOfLine']}'); "
        "'auto' still normalises to CRLF on Windows checkouts"
    )


def test_gitattributes_has_frontend_patterns():
    """Verify .gitattributes enforces LF for frontend file types."""
    gitattributes = REPO_ROOT / ".gitattributes"
    assert gitattributes.exists(), ".gitattributes not found"

    text = gitattributes.read_text()

    required_patterns = [
        "frontend/**/*.ts text eol=lf",
        "frontend/**/*.tsx text eol=lf",
        "frontend/**/*.js text eol=lf",
        "frontend/**/*.jsx text eol=lf",
        "frontend/**/*.json text eol=lf",
        "frontend/**/*.css text eol=lf",
        "frontend/**/*.html text eol=lf",
        "frontend/**/*.md text eol=lf",
    ]

    for pattern in required_patterns:
        assert pattern in text, f"Missing .gitattributes pattern: {pattern}"
