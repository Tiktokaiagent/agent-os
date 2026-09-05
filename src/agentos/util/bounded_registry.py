"""A bounded, LRU-evicting, optionally TTL-backed registry.

Replaces the twenty unbounded dicts catalogued in
https://github.com/use-agent-os/agent-os/issues/1131 with a single primitive
that can be configured for either lifetime shape:

- **Session-scoped state (Shape A):** entries are dropped deterministically
  via ``discard()`` on session terminal events, with a max-size backstop for
  sessions that never emit an event.
- **Time-scoped caches (Shape B):** entries expire after a configurable TTL,
  with a max-size backstop as overflow protection.

Both shapes share the same LRU eviction policy — when the cap is exceeded the
oldest entries are evicted first.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import TypeVar

KT = TypeVar("KT")
VT = TypeVar("VT")

_DEFAULT_MAX_ENTRIES = 500


class BoundedRegistry[KT, VT]:
    """A dict-like bounded registry with LRU eviction and optional TTL.

    Thread-safe for both sync and async callers (fast dict ops under a
    ``threading.Lock``).

    Parameters
    ----------
    max_entries:
        Hard cap on the number of entries. When exceeded, oldest entries
        (by insertion or last-set order) are evicted first. ``0`` disables
        the cap (use with caution).
    ttl_seconds:
        Entries past this age (measured by ``time.monotonic``) are evicted
        on the next mutating operation. ``0.0`` disables TTL eviction.
    """

    def __init__(
        self, *, max_entries: int = _DEFAULT_MAX_ENTRIES, ttl_seconds: float = 0.0
    ) -> None:
        if max_entries < 0:
            raise ValueError(f"max_entries must be >= 0, got {max_entries}")
        if ttl_seconds < 0:
            raise ValueError(f"ttl_seconds must be >= 0, got {ttl_seconds}")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._data: OrderedDict[KT, VT] = OrderedDict()
        self._timestamps: dict[KT, float] = {}
        self._lock = Lock()
        self._eviction_count = 0

    # ── public API ────────────────────────────────────────────────

    def get(self, key: KT, default: VT | None = None) -> VT | None:
        """Return the value for *key*, or *default*."""
        with self._lock:
            self._evict_stale()
            raw = self._data.get(key, _MISSING)
            if raw is _MISSING:
                return default
            self._data.move_to_end(key)
            return raw

    def set(self, key: KT, value: VT) -> None:
        """Insert or overwrite *key* -> *value*."""
        with self._lock:
            self._evict_stale()
            self._data[key] = value
            self._data.move_to_end(key)
            self._timestamps[key] = time.monotonic()
            self._trim_to_fit()

    def discard(self, key: KT) -> bool:
        """Remove *key* if present. Returns True when it existed."""
        with self._lock:
            was_present = key in self._data
            self._data.pop(key, None)
            self._timestamps.pop(key, None)
            return was_present

    def get_or_create(self, key: KT, factory: type[VT] | None = None) -> VT:
        """Return the existing entry or create+store a new one."""
        with self._lock:
            self._evict_stale()
            try:
                self._data.move_to_end(key)
                return self._data[key]
            except KeyError:
                pass
            value = (factory or type(None))() if factory is not None else None
            self._data[key] = value
            self._data.move_to_end(key)
            self._timestamps[key] = time.monotonic()
            self._trim_to_fit()
            return value

    def clear(self) -> int:
        """Remove all entries. Returns the count of removed entries."""
        with self._lock:
            count = len(self._data)
            self._data.clear()
            self._timestamps.clear()
            self._eviction_count += count
            return count

    def __getitem__(self, key: KT) -> VT:
        with self._lock:
            self._evict_stale()
            self._data.move_to_end(key)
            return self._data[key]

    def __setitem__(self, key: KT, value: VT) -> None:
        self.set(key, value)

    def __delitem__(self, key: KT) -> None:
        with self._lock:
            del self._data[key]
            self._timestamps.pop(key, None)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            self._evict_stale()
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __iter__(self):
        with self._lock:
            return iter(list(self._data.keys()))

    def items(self) -> list[tuple[KT, VT]]:
        """Return a snapshot of all (key, value) pairs."""
        with self._lock:
            return list(self._data.items())

    def pop(self, key: KT, default: VT | None = None) -> VT | None:
        """Remove *key* and return its value, or *default*."""
        with self._lock:
            self._timestamps.pop(key, None)
            raw = self._data.pop(key, _MISSING)
            return raw if raw is not _MISSING else default

    def values(self) -> list[VT]:
        """Return a snapshot of all values (safe to iterate outside lock)."""
        with self._lock:
            return list(self._data.values())

    # ── properties ────────────────────────────────────────────────

    @property
    def eviction_count(self) -> int:
        """Total entries evicted (TTL + cap) over the lifetime."""
        return self._eviction_count

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    # ── internal ──────────────────────────────────────────────────

    def _evict_stale(self) -> None:
        """Remove entries whose TTL has expired (no-op when TTL is 0)."""
        if self._ttl_seconds <= 0 or not self._timestamps:
            return
        now = time.monotonic()
        deadline = now - self._ttl_seconds
        stale = [k for k, ts in self._timestamps.items() if ts < deadline]
        for k in stale:
            del self._data[k]
            del self._timestamps[k]
            self._eviction_count += 1

    def _trim_to_fit(self) -> None:
        """Evict oldest entries when over the cap (no-op when cap is 0)."""
        if self._max_entries <= 0:
            return
        while len(self._data) > self._max_entries:
            oldest, _ = self._data.popitem(last=False)
            self._timestamps.pop(oldest, None)
            self._eviction_count += 1


class _MissingSentinel:
    pass


_MISSING = _MissingSentinel()
