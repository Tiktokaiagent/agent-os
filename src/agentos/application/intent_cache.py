"""Session-scoped cache of approved action intents.

The per-approval queue treats every tool invocation as a fresh request. That
means approving ``rm /tmp/x`` does nothing for a subsequent
``os.remove("/tmp/x")`` or ``Path("/tmp/x").unlink()`` — the model can paraphrase
its way past approval prompts and the user has to press y repeatedly. This
module normalizes destructive actions to a semantic key (intent kind + target)
and remembers approvals for a short window, so paraphrased retries of the same
intent proceed without another prompt.

Scope: only *delete* intents for now, since that is the bulk of user-observed
pain. Extend ``_extract_intent`` if other classes (write-outside-workspace,
network egress) need intent-level memory.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path, PurePath
from time import time

# ── Shell split config ──────────────────────────────────────────────────────
# Shell command separators. We split on these first so that only ``rm``
# segments are scanned — a bare regex across the whole command could
# capture ``cat``/``grep`` targets from non-rm segments.
_SHELL_SEP_RE = re.compile(r";\s*|&&\s*|\|\|\s*|[|&](?:\s+|$)")


# ── rm target extractor ─────────────────────────────────────────────────────

def _extract_rm_targets(command: str) -> list[str]:
    """Pull every non-flag argument out of every ``rm`` invocation.

    Handles ``rm a b c``, ``rm -rf /a /b``, quoted paths, and splits the
    command into segments first so that ``rm /tmp/safe; rm /root/.ssh/id_rsa``
    yields the second rm's target while ``rm /tmp/safe && cat ~/.ssh/config``
    does **not** scan ``config`` as an rm target.

    The approach: split the full command on shell separators first, then
    keep only segments whose first token is ``rm``. This avoids the
    over-blocking bug in the previous implementation, where the flattened
    ``rm(.*)`` regex captured non-rm commands after an rm segment.

    Does not try to be a full shell parser — falls back to whitespace split
    on shlex errors (unbalanced quotes).
    """
    segments = _SHELL_SEP_RE.split(command)

    targets: list[str] = []
    seen: set[str] = set()

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        if not re.match(r"^\s*rm\b", segment):
            continue

        tail_match = re.match(r"^\s*rm\b\s*(.*)", segment, re.DOTALL)
        if not tail_match:
            continue
        tail = tail_match.group(1).strip()
        if not tail:
            continue

        token_sets: list[list[str]] = []
        try:
            token_sets.append(shlex.split(tail))
        except ValueError:
            token_sets.append(tail.split())
        if "\\" in tail and (os.name == "nt" or re.search(r"(?:^|\s)\\[^\s]", tail)):
            try:
                token_sets.append(shlex.split(tail, posix=False))
            except ValueError:
                token_sets.append(tail.split())

        for tokens in token_sets:
            for token in tokens:
                if not token or token.startswith("-") or token in seen:
                    continue
                seen.add(token)
                targets.append(token)

    return targets


def _extract_intents(
    command: str,
    *,
    base_dir: str | Path | None = None,
) -> list[tuple[str, str]]:
    """Return every recognized destructive intent, deduped and normalized.

    ``rm /a /b /c`` -> three tuples; ``shutil.rmtree('a'); os.remove('b')`` ->
    two tuples; a plain echo returns an empty list.
    """
    if not command:
        return []
    paths: list[str] = []
    paths.extend(_extract_rm_targets(command))
    for pattern in _PY_DELETE_PATTERNS:
        paths.extend(m.group(1) for m in pattern.finditer(command))

    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in paths:
        intent = ("delete", _norm_path(raw, base_dir=base_dir))
        if intent in seen:
            continue
        seen.add(intent)
        result.append(intent)
    return result


def _extract_intent(command: str) -> tuple[str, str] | None:
    """Return the first (kind, target) or None.

    Convenience wrapper for single-intent callers.
    """
    intents = _extract_intents(command)
    return intents[0] if intents else None


# ── Path normalisation ──────────────────────────────────────────────────────

def _norm_path(raw: str, *, base_dir: str | Path | None = None) -> str:
    """Normalize a target path for intent-key comparison.

    Strip trailing whitespace and resolve user-relative (``~/...``) and
    workspace-relative prefixes once, so ``/tmp/a`` vs ``/tmp/a/`` vs
    ``/tmp/a/./`` collapse to the same key.
    """
    raw = raw.strip()
    if not raw:
        return raw
    if raw.startswith("~"):
        raw = str(Path(raw).expanduser())
    return str(PurePath(raw))


# ── Intent approval cache ───────────────────────────────────────────────────

class IntentApprovalCache:
    """Per-session map of pending and approved high-stakes intents.

    An intent is a pair ``(kind, target)`` normalised through ``_norm_path``.
    ``kind`` is always ``"delete"`` for now; ``target`` is a resolved path.

    Caller flow
    -----------
    1.  Extract intents from the tool call with ``_extract_intents``.
    2.  Call ``check`` to see which are cached (approved in the last window).
    3.  Present any uncached intents to the human.
    4.  Call ``approve`` on the approved set.

    Thread-safety
    -------------
    Not thread-safe — call from a single event-loop task.
    """

    def __init__(self, approval_window: float = 30.0) -> None:
        self._approval_window = approval_window
        self._cache: dict[tuple[str, str], float] = {}
        self._pending: list[list[tuple[str, str]]] = []

    # ── Cache API ───────────────────────────────────────────────────────────

    def check(self, intents: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Return the subset of *intents* still in the approval window."""
        now = time()
        stale_keys = [k for k, t in self._cache.items() if now - t > self._approval_window]
        for k in stale_keys:
            del self._cache[k]
        return [i for i in intents if i in self._cache]

    def approve(self, intents: list[tuple[str, str]]) -> None:
        """Record approval timestamp for each intent in *intents*."""
        now = time()
        for intent in intents:
            self._cache[intent] = now

    # ── Pending-approval API ────────────────────────────────────────────────

    def push_pending(self, intents: list[tuple[str, str]]) -> None:
        self._pending.append(intents)

    def pop_pending(self) -> list[tuple[str, str]]:
        if not self._pending:
            return []
        return self._pending.pop(0)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # ── Python delete patterns ──────────────────────────────────────────────

_PY_DELETE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # os.remove, os.unlink
    re.compile(r"(?:^|[\s;(])"
               r"(?:os\.)?(?:remove|unlink|rmdir|removedirs)"
               r"\s*\(\s*(?:rf\"|rf'|f\"|f'|\"|')(.*?)(?:\"|')\s*\)"),
    # shutil.rmtree
    re.compile(r"(?:^|[\s;(])"
               r"shutil\.rmtree\s*\(\s*(?:rf\"|rf'|f\"|f'|\"|')(.*?)(?:\"|')\s*\)"),
    # pathlib Path(...).unlink / .rmdir
    re.compile(r"(?:^|[\s;(])"
               r"path(?:lib)?\..*?\.(?:unlink|rmdir)\s*\(\s*\)"),
)
_cache: IntentApprovalCache | None = None


def get_intent_cache() -> IntentApprovalCache:
    global _cache
    if _cache is None:
        _cache = IntentApprovalCache()
    return _cache


def reset_intent_cache() -> None:
    """Test hook — drop the singleton."""
    global _cache
    _cache = None
