from __future__ import annotations

import json

import typer

from agentos.cli.output import _safe_echo, emit_error, print_json


def test_print_json_uses_stdout(capsys):
    print_json({"text": "héllo", "value": object()})

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["text"] == "héllo"
    assert captured.err == ""


def test_emit_error_json_uses_stderr(capsys):
    emit_error("bad input", json_output=True, code="INVALID_REQUEST", details={"field": "x"})

    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload == {
        "error": {
            "message": "bad input",
            "code": "INVALID_REQUEST",
            "details": {"field": "x"},
        }
    }


def test_safe_echo_falls_back_when_typer_echo_raises_unicode_error(monkeypatch):
    """When typer.echo raises UnicodeEncodeError, _safe_echo does not crash."""

    def fake_echo(text, err=False):
        raise UnicodeEncodeError("utf-8", "", 0, 1, "test encoding error")

    monkeypatch.setattr(typer, "echo", fake_echo)

    _safe_echo("✅ done")

    assert True  # no exception


def test_safe_echo_normal_case(capsys):
    _safe_echo("héllo world")

    captured = capsys.readouterr()
    assert captured.out == "héllo world\n"
