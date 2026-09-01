"""Small shared output helpers for scriptable CLI commands."""

from __future__ import annotations

import json
import sys
from typing import Any

import typer


def _safe_echo(text: str, err: bool = False) -> None:
    """Echo text, safe for terminals with non-UTF-8 encoding.

    Falls back to writing UTF-8 bytes to the underlying binary stream
    when the terminal encoding cannot represent the text (e.g. Windows
    cp1252/cp437 with emoji or non-Latin characters).
    """
    try:
        typer.echo(text, err=err)
    except UnicodeEncodeError:
        out = sys.stderr if err else sys.stdout
        buffer = getattr(out, "buffer", None)
        if buffer is not None:
            buffer.write(text.encode("utf-8") + b"\n")
            buffer.flush()
        else:
            out.write(text + "\n")
            out.flush()


def print_json(payload: Any) -> None:
    """Print JSON payload to stdout using the AgentOS CLI contract."""

    _safe_echo(json.dumps(payload, ensure_ascii=False, default=str))


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
        _safe_echo(
            json.dumps(
                error_payload(message, code=code, details=details),
                ensure_ascii=False,
                default=str,
            ),
            err=True,
        )
    else:
        typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
