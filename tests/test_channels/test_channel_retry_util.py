"""Direct regression tests for ``retry_request`` (``agentos.channels._util``).

Pins the two fixes in this PR:
1. Non-numeric Retry-After (HTTP-date) falls back to exponential backoff
2. httpx.TimeoutException subclasses are caught and retried
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agentos.channels._util import retry_request


_REQUEST = httpx.Request("POST", "https://test.example/api")


def _resp(
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(status_code, headers=headers or {}, request=_REQUEST)


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("agentos.channels._util.asyncio.sleep", new=AsyncMock()) as sleep:
        yield sleep


async def test_retry_429_with_numeric_retry_after() -> None:
    """A 429 with a numeric Retry-After header uses that value for backoff."""
    func = AsyncMock(side_effect=[_resp(429, headers={"Retry-After": "2.5"}), _resp(200)])

    result = await retry_request(func, max_retries=3, base_delay=1.0)

    assert result.status_code == 200
    assert func.await_count == 2


async def test_retry_429_with_http_date_retry_after_falls_back() -> None:
    """A 429 with an HTTP-date Retry-After (RFC 7231) falls back to default backoff."""
    func = AsyncMock(
        side_effect=[
            _resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            _resp(200),
        ]
    )

    result = await retry_request(func, max_retries=3, base_delay=1.0)

    assert result.status_code == 200
    assert func.await_count == 2


async def test_retry_429_with_empty_retry_after_falls_back() -> None:
    """A 429 with an empty Retry-After header falls back to default backoff."""
    func = AsyncMock(side_effect=[_resp(429, headers={"Retry-After": ""}), _resp(200)])

    result = await retry_request(func, max_retries=3, base_delay=1.0)

    assert result.status_code == 200
    assert func.await_count == 2


async def test_retry_connect_timeout() -> None:
    """httpx.ConnectTimeout is retried like ConnectError."""
    func = AsyncMock(
        side_effect=[
            httpx.ConnectTimeout("dns timeout", request=_REQUEST),
            _resp(200),
        ]
    )

    result = await retry_request(func, max_retries=3, base_delay=1.0)

    assert result.status_code == 200
    assert func.await_count == 2


async def test_retry_generic_timeout_exception() -> None:
    """Any httpx.TimeoutException subclass is retried."""
    func = AsyncMock(
        side_effect=[
            httpx.ReadTimeout("read timed out", request=_REQUEST),
            _resp(200),
        ]
    )

    result = await retry_request(func, max_retries=3, base_delay=1.0)

    assert result.status_code == 200
    assert func.await_count == 2


async def test_retry_exhausted_raises_last_exception() -> None:
    """After exhausting retries, the last exception is re-raised."""
    exc = httpx.ConnectTimeout("always fails", request=_REQUEST)
    func = AsyncMock(side_effect=exc)

    with pytest.raises(httpx.ConnectTimeout):
        await retry_request(func, max_retries=2, base_delay=0.1)

    assert func.await_count == 3  # initial + 2 retries