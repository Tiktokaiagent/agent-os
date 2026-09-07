"""Durable file writes for memory state.

``Path.write_text`` opens with ``"w"``, which truncates the file *before* the
new content is written. If the process dies in that window -- SIGKILL, OOM,
container stop, power loss -- the old content is already gone and the new
content never landed. For a file that is rewritten wholesale on every turn,
that window is hit on every turn.

Writing to a temporary file in the same directory and renaming it into place
removes the window: the rename is atomic, so a reader sees either the old
file or the new one, never a truncated one.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def fsync_directory(path: Path) -> None:
    """Flush *path*'s directory entry so a rename survives a power loss.

    Best-effort: a filesystem that refuses the open or the fsync (some
    network mounts) is not a reason to fail the write that already landed.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY

    try:
        fd = os.open(path, flags)
    except OSError:
        return

    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)


def atomic_write_text(target: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write *content* to *target* so the file is never observed truncated.

    The temporary file is created in the target's own directory, because
    ``os.replace`` is only atomic within a single filesystem. On failure the
    temporary file is removed and the original is left exactly as it was.
    """
    encoded = content.encode(encoding)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            existing_mode = stat.S_IMODE(os.stat(target).st_mode)
        except OSError:
            existing_mode = None
        os.replace(tmp_name, target)
        if existing_mode is not None:
            os.chmod(target, existing_mode)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:  # pragma: no cover - already gone
            pass
        raise
    fsync_directory(target.parent)
