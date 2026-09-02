"""Small shared output helpers for scriptable CLI commands."""

from __future__ import annotations

import json
from typing import Any

import typer


def print_json(payload: Any) -> None:
    """Print JSON payload to stdout using the AgentOS CLI contract.

    Writes UTF-8 bytes directly to avoid UnicodeEncodeError on terminals
    with non-UTF-8 encoding (e.g. Windows cp1252, cp437).
    """
    text = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        typer.echo(text)
    except UnicodeEncodeError:
        # Fallback: write UTF-8 bytes to stdout buffer
        import sys
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))


def error_payload(
    message: str,
    *,
    code: str | None = None,
    details: Any | None = None,
) -> dict[str, Any]:
    """Build the small AgentOS-owned JSON error envelope."""

    error: dict[str, Any] = {"message": message}
    if code:
        error["code"] = code
    if details is not None:
        error["details"] = details
    return {"error": error}


def emit_error(
    message: str,
    *,
    json_output: bool = False,
    code: str | None = None,
    details: Any | None = None,
) -> None:
    """Emit an error to stderr without polluting JSON stdout."""

    if json_output:
        text = json.dumps(
            error_payload(message, code=code, details=details),
            ensure_ascii=False,
            default=str,
        )
        try:
            typer.echo(text, err=True)
        except UnicodeEncodeError:
            import sys
            sys.stderr.buffer.write((text + "\n").encode("utf-8"))
    else:
        typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
