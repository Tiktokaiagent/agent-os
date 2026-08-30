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


def test_git_diff_argv_unstaged_without_path() -> None:
    from agentos.tools.builtin.git import _git_diff_argv

    assert _git_diff_argv({}) == ("git", "diff")
    assert _git_diff_argv({"staged": False, "path": None}) == ("git", "diff")


def test_git_diff_argv_staged_without_path() -> None:
    from agentos.tools.builtin.git import _git_diff_argv

    assert _git_diff_argv({"staged": True}) == ("git", "diff", "--cached")
    assert _git_diff_argv({"staged": True, "path": None}) == ("git", "diff", "--cached")


def test_git_diff_argv_unstaged_with_path() -> None:
    from agentos.tools.builtin.git import _git_diff_argv

    assert _git_diff_argv({"path": "src/main.py"}) == (
        "git", "diff", "--", "src/main.py",
    )
    assert _git_diff_argv({"staged": False, "path": "src/"}) == (
        "git", "diff", "--", "src/",
    )


def test_git_diff_argv_staged_with_path() -> None:
    from agentos.tools.builtin.git import _git_diff_argv

    assert _git_diff_argv({"staged": True, "path": "src/main.py"}) == (
        "git", "diff", "--cached", "--", "src/main.py",
    )
