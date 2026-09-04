"""Denial-aware stale-output cache.

When the approval gate denies an action, we must prevent the agent from
simply reusing the cached output of a *previous* successful run of the same
dangerous action. This module owns the agentos-side hooks that implement
that hygiene rule.

Scope — read carefully before extending:

* This cache covers **agentos-owned caches** only: the last successful
  tool-envelope payload keyed by the action fingerprint. It is the concrete
  mechanism behind §8.3 for agentos-produced artefacts.
* It does **not** reach into the LLM's in-context memory. If a prior turn
  of the conversation already delivered the successful output to the model,
  the model may still recall it. The only way to fully enforce §8.3 at
  that boundary is history scrubbing in the session layer, which is
  tracked as follow-up work in :mod:`agentos.session` and is intentionally
  out of scope for this slice.
* Purging removes the entry unconditionally: we prefer a false negative
  (losing a legitimate cached result) over a false positive (letting a
  denied command's stale output influence the next turn).

The cache is in-memory and per-process. A session key scopes entries so
concurrent sessions do not leak output across each other.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _CacheEntry:
    fingerprint: str
    session_id: str
    payload: Any
    stored_at_monotonic: float


_CACHE_TTL_SECONDS = 600.0  # 10 min — entries older than this are treated as stale


class StaleOutputCache:
    """In-memory cache of last-successful payload per (session, fingerprint).

    The cache is intentionally small and session-scoped. It is updated by
    :func:`agentos.sandbox.governance.on_successful_exec` after any
    sandboxed execution whose result the agent might subsequently recall,
    and purged on denial by :class:`DenialLedger`.

    Entries older than ``_CACHE_TTL_SECONDS`` (10 min) are considered stale
    and silently dropped on read or write, preventing unbounded memory growth
    on long-running sessions.
    """

    def __init__(self, ttl: float = _CACHE_TTL_SECONDS) -> None:
        self._entries: dict[tuple[str, str], _CacheEntry] = {}
        self._ttl = ttl
        self._lock = asyncio.Lock()

    async def record_success(self, session_id: str, fingerprint: str, payload: Any) -> None:
        """Store ``payload`` keyed by ``(session_id, fingerprint)``.

        Called after a sandboxed tool produces output the agent might refer
        back to. Overwrites any prior entry for the same key. Also evicts
        expired entries to bound memory growth.
        """
        async with self._lock:
            self._evict_expired_locked()
            loop = asyncio.get_running_loop()
            self._entries[(session_id, fingerprint)] = _CacheEntry(
                fingerprint=fingerprint,
                session_id=session_id,
                payload=payload,
                stored_at_monotonic=loop.time(),
            )

    async def purge(self, session_id: str, fingerprint: str) -> bool:
        """Remove any cached payload for ``(session_id, fingerprint)``.

        Returns ``True`` if an entry was removed, else ``False``. Callers
        should not rely on the return value for correctness — this hook is
        idempotent by design.
        """
        async with self._lock:
            return self._entries.pop((session_id, fingerprint), None) is not None

    async def get(self, session_id: str, fingerprint: str) -> Any | None:
        """Return the stored payload or ``None`` if not cached or stale."""
        async with self._lock:
            entry = self._entries.get((session_id, fingerprint))
            if entry is None:
                return None
            if self._entry_expired(entry):
                self._entries.pop((session_id, fingerprint), None)
                return None
            return entry.payload

    @staticmethod
    def _entry_expired(entry: _CacheEntry, now: float | None = None) -> bool:
        if now is None:
            now = asyncio.get_running_loop().time()
        return now - entry.stored_at_monotonic >= _CACHE_TTL_SECONDS

    def _evict_expired_locked(self) -> int:
        """Remove all expired entries. Caller must hold ``_lock``."""
        now = asyncio.get_running_loop().time()
        stale_keys = [
            k for k, e in self._entries.items() if now - e.stored_at_monotonic >= self._ttl
        ]
        for k in stale_keys:
            del self._entries[k]
        return len(stale_keys)

    async def clear_session(self, session_id: str) -> int:
        """Remove every entry for ``session_id``. Returns the count removed."""
        async with self._lock:
            self._evict_expired_locked()
            keys = [k for k in self._entries if k[0] == session_id]
            for k in keys:
                del self._entries[k]
            return len(keys)

    def snapshot(self) -> list[dict[str, object]]:
        """Return a plain-data view of cached keys (debug/test helper).

        The payload is intentionally omitted so the snapshot is safe to log.
        """
        return [
            {
                "session_id": e.session_id,
                "fingerprint": e.fingerprint,
                "stored_at": e.stored_at_monotonic,
            }
            for e in self._entries.values()
        ]


_default_cache: StaleOutputCache | None = None


def get_stale_output_cache() -> StaleOutputCache:
    """Return the process-wide default cache, creating it lazily."""
    global _default_cache
    if _default_cache is None:
        _default_cache = StaleOutputCache()
    return _default_cache


def reset_stale_output_cache() -> None:
    """Drop the process-wide cache. Test helper."""
    global _default_cache
    _default_cache = None


@dataclass
class NullStaleOutputCache:
    """No-op variant for tests that don't care about the §8.3 pathway."""

    calls: list[tuple[str, str]] = field(default_factory=list)

    async def record_success(self, session_id: str, fingerprint: str, payload: Any) -> None:
        self.calls.append(("record", session_id, fingerprint))  # type: ignore[arg-type]

    async def purge(self, session_id: str, fingerprint: str) -> bool:
        self.calls.append(("purge", session_id, fingerprint))  # type: ignore[arg-type]
        return False

    async def get(self, session_id: str, fingerprint: str) -> Any | None:
        return None

    async def clear_session(self, session_id: str) -> int:
        return 0

    @staticmethod
    def _entry_expired(entry: _CacheEntry, now: float | None = None) -> bool:
        return False

    def snapshot(self) -> list[dict[str, object]]:
        return []


__all__ = [
    "NullStaleOutputCache",
    "StaleOutputCache",
    "get_stale_output_cache",
    "reset_stale_output_cache",
]
