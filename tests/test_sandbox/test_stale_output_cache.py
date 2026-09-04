"""Regression tests for StaleOutputCache TTL expiry (Issue #????).

Validates that cached entries older than _CACHE_TTL_SECONDS are treated
as stale on read, and that record_success evicts expired entries to bound
memory growth.
"""

from __future__ import annotations

import asyncio

import pytest

from agentos.sandbox.stale_output_cache import (
    _CACHE_TTL_SECONDS,
    StaleOutputCache,
    _CacheEntry,
)


def _future_entry(session_id: str = "s1", fingerprint: str = "f1") -> _CacheEntry:
    """Return an entry in the future so it never expires."""
    return _CacheEntry(
        fingerprint=fingerprint,
        session_id=session_id,
        payload={"data": "ok"},
        stored_at_monotonic=asyncio.get_running_loop().time() + 3600,
    )


# ──────────────────────────────────────────────
# 1. get returns None for expired entries
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_returns_none_for_expired_entry() -> None:
    """An entry past TTL returns None on get and is purged."""
    cache = StaleOutputCache()
    loop = asyncio.get_running_loop()
    now = loop.time()

    # Insert an entry that's already expired
    expired = _CacheEntry(
        fingerprint="f1",
        session_id="s1",
        payload={"data": "old"},
        stored_at_monotonic=now - _CACHE_TTL_SECONDS - 10,
    )
    async with cache._lock:
        cache._entries[("s1", "f1")] = expired

    result = await cache.get("s1", "f1")
    assert result is None, "expired entry should return None"
    async with cache._lock:
        assert ("s1", "f1") not in cache._entries, "expired entry should be purged"


# ──────────────────────────────────────────────
# 2. get returns payload for fresh entries
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_returns_payload_for_fresh_entry() -> None:
    """A fresh entry (within TTL) returns its payload."""
    cache = StaleOutputCache()
    obj = {"data": "hello"}
    await cache.record_success("s1", "f1", obj)

    result = await cache.get("s1", "f1")
    assert result is not None
    assert result["data"] == "hello"


# ──────────────────────────────────────────────
# 3. record_success evicts expired entries
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_success_evicts_expired_entries() -> None:
    """record_success evicts stale entries before writing the new one."""
    cache = StaleOutputCache()
    loop = asyncio.get_running_loop()
    now = loop.time()

    # Pre-populate with an expired entry
    expired = _CacheEntry(
        fingerprint="old",
        session_id="s1",
        payload="stale",
        stored_at_monotonic=now - _CACHE_TTL_SECONDS - 10,
    )
    async with cache._lock:
        cache._entries[("s1", "old")] = expired
        assert len(cache._entries) == 1

    # Recording a new entry triggers eviction
    await cache.record_success("s1", "new", "fresh")

    async with cache._lock:
        assert ("s1", "old") not in cache._entries, "expired entry should be evicted"
        assert ("s1", "new") in cache._entries, "new entry should exist"
        assert len(cache._entries) == 1


# ──────────────────────────────────────────────
# 4. _entry_expired static method
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_entry_expired_true_when_past_ttl() -> None:
    """_entry_expired returns True for entries past the default TTL."""
    loop = asyncio.get_running_loop()
    now = loop.time()
    entry = _CacheEntry("f", "s", b"data", stored_at_monotonic=now - _CACHE_TTL_SECONDS - 1)
    assert StaleOutputCache._entry_expired(entry) is True


@pytest.mark.asyncio
async def test_entry_expired_false_when_within_ttl() -> None:
    """_entry_expired returns False for entries within the default TTL."""
    loop = asyncio.get_running_loop()
    now = loop.time()
    entry = _CacheEntry("f", "s", b"data", stored_at_monotonic=now - 10)
    assert StaleOutputCache._entry_expired(entry) is False


@pytest.mark.asyncio
async def test_entry_expired_respects_custom_now() -> None:
    """_entry_expired accepts an optional 'now' argument for testability."""
    entry = _CacheEntry("f", "s", b"data", stored_at_monotonic=100.0)
    assert StaleOutputCache._entry_expired(entry, now=100.0 + _CACHE_TTL_SECONDS - 1) is False
    assert StaleOutputCache._entry_expired(entry, now=100.0 + _CACHE_TTL_SECONDS) is True
    assert StaleOutputCache._entry_expired(entry, now=100.0 + _CACHE_TTL_SECONDS + 1) is True


# ──────────────────────────────────────────────
# 5. clear_session evicts expired and matching entries
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_session_removes_session_entries() -> None:
    """clear_session removes all entries for a session_id."""
    cache = StaleOutputCache()
    await cache.record_success("s1", "f1", "a")
    await cache.record_success("s1", "f2", "b")
    await cache.record_success("s2", "f1", "c")

    count = await cache.clear_session("s1")
    assert count == 2

    assert await cache.get("s1", "f1") is None
    assert await cache.get("s1", "f2") is None
    assert await cache.get("s2", "f1") is not None  # other session survives


# ──────────────────────────────────────────────
# 6. purge removes entries regardless of age
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_removes_entry_regardless_of_age() -> None:
    """purge removes an entry even if it's still fresh."""
    cache = StaleOutputCache()
    await cache.record_success("s1", "f1", "data")
    purged = await cache.purge("s1", "f1")
    assert purged is True
    assert await cache.get("s1", "f1") is None


# ──────────────────────────────────────────────
# 7. concurrent safety (smoke)
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_get_and_record_does_not_crash() -> None:
    """Concurrent record_success and get does not raise."""
    cache = StaleOutputCache()

    async def writer() -> None:
        for i in range(50):
            await cache.record_success("s1", f"f{i}", i)
            await asyncio.sleep(0)

    async def reader() -> None:
        for i in range(50):
            await cache.get("s1", f"f{i}")
            await asyncio.sleep(0)

    await asyncio.gather(writer(), reader())
    # No crash = pass


# ──────────────────────────────────────────────
# 8. snapshot still works
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_works() -> None:
    """snapshot returns a non-stale view of cached keys."""
    cache = StaleOutputCache()
    await cache.record_success("s1", "f1", "data")
    snap = cache.snapshot()
    assert len(snap) == 1
    assert snap[0]["session_id"] == "s1"
    assert snap[0]["fingerprint"] == "f1"
