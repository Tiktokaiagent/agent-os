from __future__ import annotations

from typing import Any

import pytest

from agentos.channels._telegram_formatting import render_telegram_html
from agentos.channels.telegram import TelegramApiError, TelegramChannel, TelegramChannelConfig
from agentos.channels.types import OutgoingMessage


def test_telegram_markdown_renders_bold_code_and_two_column_table() -> None:
    markdown = """Skill dùng `agentos channels list`.

AgentOS có **1 channel**:

| Thông tin | Giá trị |
| --- | --- |
| **Tên** | `telegram-test` |
| **Trạng thái** | ✅ Enabled |
"""

    rendered = render_telegram_html(markdown)

    assert "<code>agentos channels list</code>" in rendered
    assert "AgentOS có <b>1 channel</b>:" in rendered
    assert "<b>Thông tin — Giá trị</b>" in rendered
    assert "<b>Tên:</b> <code>telegram-test</code>" in rendered
    assert "<b>Trạng thái:</b> ✅ Enabled" in rendered
    assert "| --- |" not in rendered
    assert "**" not in rendered
    assert "`" not in rendered


def test_telegram_markdown_escapes_html_and_preserves_code_blocks() -> None:
    markdown = """# Result <safe>

Use **care & caution** with `x < 2`.

```python
if x < 2:
    print("&")
```
"""

    rendered = render_telegram_html(markdown)

    assert "<b>Result &lt;safe&gt;</b>" in rendered
    assert "Use <b>care &amp; caution</b> with <code>x &lt; 2</code>." in rendered
    assert (
        '<pre><code class="language-python">'
        "if x &lt; 2:\n    print(&quot;&amp;&quot;)</code></pre>"
    ) in rendered


def test_telegram_send_payload_auto_renders_html() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(content="**Ready**: `agentos status`", reply_to="42")
    )

    assert payload == {
        "chat_id": "42",
        "text": "<b>Ready</b>: <code>agentos status</code>",
        "parse_mode": "HTML",
    }


def test_telegram_send_payload_respects_explicit_parse_mode_override() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(
            content="*caller-owned*",
            reply_to="42",
            metadata={"parse_mode": "MarkdownV2"},
        )
    )

    assert payload["text"] == "*caller-owned*"
    assert payload["parse_mode"] == "MarkdownV2"


def test_telegram_send_payload_can_explicitly_disable_rendering() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(content="**literal**", reply_to="42", metadata={"parse_mode": ""})
    )

    assert payload["text"] == "**literal**"
    assert "parse_mode" not in payload


@pytest.mark.asyncio
async def test_telegram_send_falls_back_to_plain_text_on_entity_parse_error() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> dict[str, int]:
        calls.append((method, dict(payload or {})))
        if len(calls) == 1:
            raise TelegramApiError("Bad Request: can't parse entities")
        return {"message_id": 7}

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001

    result = await channel.send(
        OutgoingMessage(content="**Ready**: `agentos status`", reply_to="42")
    )

    assert result == {"message_id": 7}
    assert calls[0][1] == {
        "chat_id": "42",
        "text": "<b>Ready</b>: <code>agentos status</code>",
        "parse_mode": "HTML",
    }
    assert calls[1][1] == {
        "chat_id": "42",
        "text": "**Ready**: `agentos status`",
    }


# ── Issue #1031: ragged table rows ──────────────────────────────────────


def test_two_column_table_with_short_row_pads_missing_cell() -> None:
    """A 2-column table row with only 1 cell should be padded, not dropped."""
    markdown = (
        "| Header A | Header B |\n"
        "| --- | --- |\n"
        "| Row 1 Only |\n"
        "| x | y |\n"
    )
    rendered = render_telegram_html(markdown)

    # Both rows must appear — the old `break` dropped "| x | y |".
    assert "<b>Header A — Header B</b>" in rendered
    assert "<b>Row 1 Only:</b>" in rendered
    assert "<b>x:</b> y" in rendered
    # No raw pipe characters should leak through.
    assert "| x | y |" not in rendered
    assert "| Row 1 Only |" not in rendered


def test_three_column_table_with_short_row_pads_missing_cells() -> None:
    """A 3-column table row missing trailing cells should be padded."""
    markdown = (
        "| A | B | C |\n"
        "| --- | --- | --- |\n"
        "| only-a |\n"
        "| x | y | z |\n"
    )
    rendered = render_telegram_html(markdown)

    assert "<b>A · B · C</b>" in rendered
    # The short row has only column A filled; B and C are empty → filtered out.
    assert "<b>A:</b> only-a" in rendered
    # The well-formed row after the ragged one must also render.
    assert "<b>A:</b> x" in rendered
    assert "<b>B:</b> y" in rendered
    assert "<b>C:</b> z" in rendered
    assert "| x | y | z |" not in rendered


def test_table_row_with_extra_columns_is_truncated() -> None:
    """A row with more cells than headers should be truncated, not break."""
    markdown = (
        "| A | B |\n"
        "| --- | --- |\n"
        "| 1 | 2 | 3 | 4 |\n"
        "| x | y |\n"
    )
    rendered = render_telegram_html(markdown)

    assert "<b>A — B</b>" in rendered
    # Extra columns (3, 4) should be silently truncated.
    assert "<b>1:</b> 2" in rendered
    assert "<b>x:</b> y" in rendered
    assert "3" not in rendered
    assert "4" not in rendered


def test_mixed_ragged_rows_all_render_without_raw_pipes() -> None:
    """Mix of short, exact, and long rows — none should leak raw Markdown."""
    markdown = (
        "| Name | Status | Notes |\n"
        "| --- | --- | --- |\n"
        "| alpha | ok | fine |\n"
        "| beta |\n"
        "| gamma | fail | bad | extra |\n"
        "| delta | ok | good |\n"
    )
    rendered = render_telegram_html(markdown)

    # All four data rows must be rendered (no break/abort).
    assert "<b>Name:</b> alpha" in rendered
    assert "<b>Status:</b> ok" in rendered
    assert "<b>Notes:</b> fine" in rendered
    assert "<b>Name:</b> beta" in rendered
    assert "<b>Name:</b> gamma" in rendered
    assert "<b>Status:</b> fail" in rendered
    assert "<b>Notes:</b> bad" in rendered
    assert "<b>Name:</b> delta" in rendered
    # "extra" from the long row should be truncated.
    assert "extra" not in rendered
    # No raw pipe characters.
    assert "|" not in rendered


def test_telegram_markdown_does_not_format_markers_inside_link_urls() -> None:
    """Markdown markers inside link URLs must not become HTML tags (#1435).

    A URL containing double underscores, asterisks, or tildes was being
    wrapped in <b>/<i>/<s> tags when the inline formatting regexes ran
    over the already-rendered <a href="..."> tag, producing invalid HTML
    that Telegram rejects with "Can't parse entities".
    """
    rendered = render_telegram_html("[test](https://example.com/foo__bar__baz)")
    assert rendered == '<a href="https://example.com/foo__bar__baz">test</a>'

    rendered = render_telegram_html("[test](https://example.com/foo*bar*baz)")
    assert rendered == '<a href="https://example.com/foo*bar*baz">test</a>'

    rendered = render_telegram_html("[test](https://example.com/foo~~bar~~baz)")
    assert rendered == '<a href="https://example.com/foo~~bar~~baz">test</a>'


def test_telegram_markdown_still_formats_link_text_and_bold_outside_links() -> None:
    """Link text keeps inline formatting while URLs stay untouched (#1435)."""
    rendered = render_telegram_html("[**bold**](https://example.com/x__y)")
    assert rendered == '<a href="https://example.com/x__y"><b>bold</b></a>'

    rendered = render_telegram_html(
        "[test](https://example.com/foo__bar__baz) and **bold** text"
    )
    assert rendered == (
        '<a href="https://example.com/foo__bar__baz">test</a> and <b>bold</b> text'
    )
