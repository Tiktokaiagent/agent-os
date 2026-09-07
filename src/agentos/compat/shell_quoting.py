"""Cross-platform shell quoting for CLI hint messages.

Uses shlex.quote on all platforms. These are display hints for CLI
recovery messages, not executed shell commands, so single-quote style
is correct everywhere.
"""
import shlex


def shell_quote(value: str) -> str:
    """Return *value* quoted for display in CLI hints."""
    return shlex.quote(value)

