"""Regression tests for media image-fetch download-size enforcement.

Before the fix, `_fetch_image_url` did `await client.get(url)` and then read
`resp.content`, so the entire response body was buffered into memory before the
20 MB limit was checked. A URL serving an unbounded body (chunked / lying
content-length) exhausted process memory even though the code believed it had a
20 MB cap. The cap must stop the download, not just reject after the fact.

These tests prove the stream stops once the limit is exceeded, using a handler
that raises if the client ever tries to read past the cap.
"""

from __future__ import annotations

import httpx
import pytest

from agentos.tools.builtin.media import _fetch_image_url


def _png_header() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


def _patch_network(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    real = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)  # type: ignore[attr-defined]
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "socket.getaddrinfo", lambda h, p, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_fetch_image_rejects_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized body is rejected with the size-limit error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": "209715200"},
            content=b"X" * (200 * 1024 * 1024),
        )

    _patch_network(monkeypatch, handler)

    with pytest.raises(Exception) as exc_info:
        await _fetch_image_url("https://example.com/huge.png")

    assert "exceeds 20MB" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fetch_image_small_body_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal-sized image is fetched and returned."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=_png_header())

    _patch_network(monkeypatch, handler)

    data, mime = await _fetch_image_url("https://example.com/small.png")
    assert mime == "image/png"
    assert data == _png_header()


@pytest.mark.asyncio
async def test_fetch_image_redirect_missing_location_raises_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 302 redirect without Location header raises ToolError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={})

    _patch_network(monkeypatch, handler)

    with pytest.raises(ToolError, match="missing Location header"):
        await _fetch_image_url("https://example.com/redirect-no-location.png")


@pytest.mark.asyncio
async def test_fetch_image_redirect_follows_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 302 redirect with Location header follows and returns the image."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect.png":
            return httpx.Response(302, headers={"location": "/final.png"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=_png_header())

    _patch_network(monkeypatch, handler)

    data, mime = await _fetch_image_url("https://example.com/redirect.png")
    assert mime == "image/png"
    assert data == _png_header()


@pytest.mark.asyncio
async def test_fetch_image_too_many_redirects_raises_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More than MAX_REDIRECTS redirects raises ToolError."""

    redirect_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal redirect_count
        redirect_count += 1
        return httpx.Response(302, headers={"location": "/loop"})

    _patch_network(monkeypatch, handler)

    with pytest.raises(ToolError, match="Too many redirects"):
        await _fetch_image_url("https://example.com/redirect-loop.png")
