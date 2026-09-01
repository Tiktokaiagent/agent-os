from __future__ import annotations

import os

import pytest

from agentos.memory.sync_manager import MemorySyncManager


class NoopStore:
    def __init__(self) -> None:
        self.indexed: list[str] = []
        self.removed: list[str] = []

    async def index_file(
        self,
        *,
        path: str,
        content: str,
        source: object,
        mtime: float | None = None,
    ) -> int:
        self.indexed.append(path)
        return 1

    async def remove_file(self, path: str) -> None:
        self.removed.append(path)
        return None


class MtimeStore(NoopStore):
    def __init__(self) -> None:
        super().__init__()
        self.mtimes: dict[str, float | None] = {}

    async def index_file(
        self,
        *,
        path: str,
        content: str,
        source: object,
        mtime: float | None = None,
    ) -> int:
        self.indexed.append(path)
        self.mtimes[path] = mtime
        return 1


def test_sync_manager_scans_archive_as_curated_memory_subdir(tmp_path):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    archive = memory / "archive"
    hidden = memory / ".private"
    archive.mkdir(parents=True)
    hidden.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("root\n", encoding="utf-8")
    (memory / "a.md").write_text("a\n", encoding="utf-8")
    (memory / ".hidden.md").write_text("hidden file\n", encoding="utf-8")
    (archive / "x.md").write_text("archive is curated if user-created\n", encoding="utf-8")
    (hidden / "x.md").write_text("hidden\n", encoding="utf-8")

    manager = MemorySyncManager(
        store=NoopStore(),
        workspace_dir=workspace,
        memory_dir=memory,
    )

    assert sorted(manager._scan_files()) == [
        "MEMORY.md",
        "memory/a.md",
        "memory/archive/x.md",
    ]


@pytest.mark.asyncio
async def test_sync_force_rescans_unchanged_memory_sources(tmp_path):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("root\n", encoding="utf-8")
    (memory / "a.md").write_text("a\n", encoding="utf-8")
    store = NoopStore()
    manager = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory)

    await manager.sync(reason="manual")
    first_indexed = list(store.indexed)
    await manager.sync(reason="manual")
    second_indexed = store.indexed[len(first_indexed) :]
    await manager.sync(reason="manual", force=True)
    forced_indexed = store.indexed[len(first_indexed) + len(second_indexed) :]

    assert sorted(first_indexed) == ["MEMORY.md", "memory/a.md"]
    assert second_indexed == []
    assert sorted(forced_indexed) == ["MEMORY.md", "memory/a.md"]


@pytest.mark.asyncio
async def test_sync_force_overrides_search_clean_fast_path(tmp_path):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("root\n", encoding="utf-8")
    store = NoopStore()
    manager = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory)

    await manager.sync(reason="manual")
    first_count = len(store.indexed)
    sync_calls: list[dict[str, object]] = []

    async def fake_do_file_sync(**kwargs: object) -> set[str]:
        sync_calls.append(kwargs)
        return set()

    manager._do_file_sync = fake_do_file_sync  # type: ignore[method-assign]
    await manager.sync(reason="search")
    await manager.sync(reason="search:tool")
    await manager.sync(reason="search:control")
    search_count = len(store.indexed)
    await manager.sync(reason="search:tool", force=True)

    assert first_count == 1
    assert search_count == first_count
    assert sync_calls == [{"force": True}]


@pytest.mark.asyncio
async def test_sync_passes_source_mtime_for_memory_and_knowledge_base_files(tmp_path):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    knowledge_base = workspace / "knowledge_base"
    knowledge_base.mkdir()
    memory_file = workspace / "MEMORY.md"
    memory_file.write_text("Durable preference.\n", encoding="utf-8")
    document = knowledge_base / "guide.md"
    document.write_text("Deployment runbook.\n", encoding="utf-8")
    expected_mtimes = {
        "MEMORY.md": 1_700_000_000.0,
        "knowledge_base/guide.md": 1_600_000_000.0,
    }
    os.utime(memory_file, (expected_mtimes["MEMORY.md"], expected_mtimes["MEMORY.md"]))
    os.utime(
        document,
        (
            expected_mtimes["knowledge_base/guide.md"],
            expected_mtimes["knowledge_base/guide.md"],
        ),
    )

    store = MtimeStore()
    manager = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory)

    await manager.sync(reason="manual")

    assert sorted(store.indexed) == ["MEMORY.md", "knowledge_base/guide.md"]
    assert store.mtimes == expected_mtimes
class _FailingStore:
    """Store that fails on first index_file call, succeeds on subsequent."""

    def __init__(self) -> None:
        self.indexed: list[str] = []
        self._fail_count = 0

    async def index_file(self, *, path: str, content: str, source: object, mtime: float = 0) -> int:
        if self._fail_count < 1:
            self._fail_count += 1
            raise RuntimeError("transient indexing failure")
        self.indexed.append(path)
        return 1

    async def remove_file(self, path: str) -> None:
        return None


@pytest.mark.asyncio
async def test_failed_index_file_is_requeued_for_retry(tmp_path):
    """When index_file raises, the path is removed from _mtimes so the next
    sync retries it instead of skipping it as unchanged."""
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("root", encoding="utf-8")
    store = _FailingStore()
    manager = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory)

    # First sync: index_file fails, but the path should be requeued
    await manager.sync(reason="manual")

    # First call failed, so nothing was indexed
    assert store.indexed == []

    # Second sync: mtime was popped, so the file is seen as new and re-indexed
    await manager.sync(reason="manual")

    assert store.indexed == ["MEMORY.md"]

