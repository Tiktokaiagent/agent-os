from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.tools.builtin import git
from agentos.tools.types import ToolContext, ToolError, current_tool_context


def test_git_effective_workdir_resolves_context_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        assert git._effective_workdir(None) == str(workspace.resolve())
    finally:
        current_tool_context.reset(token)


def test_git_effective_workdir_rejects_foreign_posix_absolute_path_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(git, "os", SimpleNamespace(name="nt"), raising=False)
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        with pytest.raises(ToolError, match="foreign_host_path"):
            git._effective_workdir("/Users/a1/Desktop/repo")
    finally:
        current_tool_context.reset(token)


def test_git_rejects_foreign_diff_path_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git, "os", SimpleNamespace(name="nt"), raising=False)

    with pytest.raises(ToolError, match="foreign_host_path"):
        git._reject_foreign_git_path("/Users/a1/Desktop/repo/file.py")


def test_git_rejects_foreign_commit_file_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git, "os", SimpleNamespace(name="nt"), raising=False)

    with pytest.raises(ToolError, match="foreign_host_path"):
        git._reject_foreign_git_path("/Users/a1/Desktop/repo/file.py")


def test_git_log_argv_without_path() -> None:
    from agentos.tools.builtin.git import _git_log_argv

    assert _git_log_argv({}) == ("git", "log", "--max-count=10")
    assert _git_log_argv({"count": 5, "path": None}) == ("git", "log", "--max-count=5")


def test_git_log_argv_with_path() -> None:
    from agentos.tools.builtin.git import _git_log_argv

    assert _git_log_argv({"count": 5, "path": "src/main.py"}) == (
        "git", "log", "--max-count=5", "--", "src/main.py",
    )
    assert _git_log_argv({"count": 10, "path": "src/"}) == (
        "git", "log", "--max-count=10", "--", "src/",
    )
