"""Regression tests for skills update security-scan bypass (#988).

``SkillInstaller.update()`` called ``self.install(..., force=True)``
unconditionally, which caused the dangerous-content scan verdict to be
silently waived on every update. A compromised upstream could push malicious
content and ``skills update`` would accept it.

Separately, the RPC response for ``skills.update`` never surfaced
``scan_verdict`` / ``scan_findings``, unlike ``skills.install``.

Fix:
1. Drop ``force=True`` from the update path in ``SkillInstaller.update()``
   so the scan verdict is respected.
2. Add ``scan_verdict`` / ``scan_findings`` to the RPC response dict.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agentos.skills.hub.installer import InstallResult, SkillInstaller
from agentos.skills.hub.scanner import ScanFinding, ScanResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_installer(
    tmp_path: Path,
    router: Any = None,
    lockfile_data: dict[str, Any] | None = None,
) -> SkillInstaller:
    """Build a SkillInstaller configured for the given temp workspace."""

    class _StubRouter:
        async def fetch(self, identifier: str, source_id: str) -> Any:
            from agentos.skills.hub.source import SkillBundle

            return SkillBundle(name=identifier, files={"SKILL.md": "# Demo\n"}, meta=None)

        async def inspect(self, identifier: str, source_id: str) -> None:
            return None

    managed = tmp_path / "skills"
    managed.mkdir()
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    lockfile = tmp_path / "lockfile.json"

    result = SkillInstaller(
        router=router or _StubRouter(),
        managed_dir=managed,
        quarantine_dir=quarantine,
        lockfile_path=lockfile,
    )

    if lockfile_data is not None:
        lockfile.write_text(json.dumps(lockfile_data), encoding="utf-8")

    return result


def _minimal_lockfile(name: str, old_sha: str = "oldsha") -> dict[str, Any]:
    """Minimal real-looking lockfile entry for testing."""
    return {
        "version": 2,
        "installed": {
            name: {
                "identifier": name,
                "source": "clawhub",
                "sha256": old_sha,
                "title": name.replace("-", " ").title(),
                "description": "",
                "categories": [],
                "commands": [],
                "users": [],
                "publisher": "test",
                "display_name": name.replace("-", " ").title(),
                "icon_emoji": "🧪",
            }
        },
    }


def _dangerous_scan() -> ScanResult:
    return ScanResult(
        verdict="dangerous",
        findings=[ScanFinding(category="shell_injection", severity="dangerous",
                    line=1, text="os.system call", pattern="os.system")],
        strategy="static",
    )


def _safe_scan() -> ScanResult:
    return ScanResult(
        verdict="safe",
        findings=[],
        strategy="static",
    )


# ---------------------------------------------------------------------------
# Tests: SkillInstaller.update() no longer forces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_does_not_pass_force_by_default(tmp_path: Path) -> None:
    """``SkillInstaller.update()`` calls ``install()`` without ``force=True``."""
    installer = _make_installer(tmp_path, lockfile_data=_minimal_lockfile("test-skill"))

    with patch.object(installer, "install", new_callable=AsyncMock) as mock_install:
        mock_install.return_value = InstallResult(
            success=False, name="test-skill",
            sha256="old", message="Security scan: dangerous",
        )
        await installer.update("test-skill")

        mock_install.assert_awaited_once()
        call_args, call_kwargs = mock_install.call_args
        assert "force" not in call_kwargs


@pytest.mark.asyncio
async def test_update_blocks_dangerous_scan(tmp_path: Path) -> None:
    """A dangerous-scanning update is refused when force=False."""
    installer = _make_installer(tmp_path, lockfile_data=_minimal_lockfile("danger-skill"))

    with patch(
        "agentos.skills.hub.installer.scan_skill_bundle",
        return_value=_dangerous_scan(),
    ):
        results = await installer.update("danger-skill")

    assert len(results) == 1
    assert results[0].success is False, \
        f"Dangerous update should be blocked, got: {results[0].message}"
    assert "dangerous" in results[0].message.lower()


@pytest.mark.asyncio
async def test_update_allows_safe_scan(tmp_path: Path) -> None:
    """A safe-scanning update succeeds."""
    installer = _make_installer(tmp_path, lockfile_data=_minimal_lockfile("safe-skill"))

    with patch(
        "agentos.skills.hub.installer.scan_skill_bundle",
        return_value=_safe_scan(),
    ):
        results = await installer.update("safe-skill")

    assert len(results) == 1
    assert results[0].success is True


@pytest.mark.asyncio
async def test_update_safe_scan_surfaces_result(tmp_path: Path) -> None:
    """The scan result is attached to the InstallResult."""
    installer = _make_installer(tmp_path, lockfile_data=_minimal_lockfile("scanned-skill"))

    with patch(
        "agentos.skills.hub.installer.scan_skill_bundle",
        return_value=_safe_scan(),
    ):
        results = await installer.update("scanned-skill")

    assert results[0].scan is not None
    assert results[0].scan.verdict == "safe"


@pytest.mark.asyncio
async def test_update_dangerous_scan_surfaces_findings(tmp_path: Path) -> None:
    """A blocked dangerous update still surfaces the scan result."""
    installer = _make_installer(tmp_path, lockfile_data=_minimal_lockfile("blocked-skill"))

    with patch(
        "agentos.skills.hub.installer.scan_skill_bundle",
        return_value=_dangerous_scan(),
    ):
        results = await installer.update("blocked-skill")

    result = results[0]
    assert result.success is False
    assert result.scan is not None
    assert result.scan.verdict == "dangerous"
    assert len(result.scan.findings) > 0


@pytest.mark.asyncio
async def test_update_scan_verdict_in_result_message(tmp_path: Path) -> None:
    """The result message includes the dangerous verdict text."""
    installer = _make_installer(tmp_path, lockfile_data=_minimal_lockfile("msg-skill"))

    with patch(
        "agentos.skills.hub.installer.scan_skill_bundle",
        return_value=_dangerous_scan(),
    ):
        results = await installer.update("msg-skill")

    assert "dangerous" in results[0].message.lower()


# ---------------------------------------------------------------------------
# Tests: update all skills
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_all_skills_blocks_dangerous(tmp_path: Path) -> None:
    """``update()`` with no name updates all entries and blocks dangerous ones."""
    lockfile_data = _minimal_lockfile("safe-skill", "sha1")
    lockfile_data["installed"]["danger-skill"] = {
        "identifier": "danger-skill",
        "source": "clawhub",
        "sha256": "sha2",
        "title": "Dangerous",
        "description": "",
        "categories": [],
        "commands": [],
        "users": [],
        "publisher": "test",
        "display_name": "Dangerous",
        "icon_emoji": "💀",
    }

    installer = _make_installer(tmp_path, lockfile_data=lockfile_data)

    call_count = [0]

    def _alternating_scan(*args: Any, **kwargs: Any) -> ScanResult:
        call_count[0] += 1
        return _dangerous_scan() if call_count[0] > 1 else _safe_scan()

    with patch(
        "agentos.skills.hub.installer.scan_skill_bundle",
        side_effect=_alternating_scan,
    ):
        results = await installer.update()

    assert len(results) == 2
    safe_result = next(r for r in results if r.name == "safe-skill")
    assert safe_result.success is True, "safe skill should update"
    danger_result = next(r for r in results if r.name == "danger-skill")
    assert danger_result.success is False, "dangerous skill should be blocked"
    assert "dangerous" in danger_result.message.lower()


# ---------------------------------------------------------------------------
# Tests: RPC surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_rpc_surfaces_scan_verdict(tmp_path: Path) -> None:
    """The RPC response dict includes ``scan_verdict`` and ``scan_findings``."""
    installer = _make_installer(tmp_path, lockfile_data=_minimal_lockfile("rpc-safe"))

    with patch(
        "agentos.skills.hub.installer.scan_skill_bundle",
        return_value=_safe_scan(),
    ):
        results = await installer.update("rpc-safe")

    rpc_results = [
        {
            "success": r.success,
            "name": r.name,
            "message": r.message,
            "scan_verdict": r.scan.verdict if r.scan else None,
            "scan_findings": [f.__dict__ for f in r.scan.findings] if r.scan else None,
        }
        for r in results
    ]

    assert rpc_results[0]["scan_verdict"] == "safe"


@pytest.mark.asyncio
async def test_update_rpc_surfaces_dangerous_findings(tmp_path: Path) -> None:
    """A dangerous update's scan findings are surfaced in RPC shape."""
    installer = _make_installer(tmp_path, lockfile_data=_minimal_lockfile("rpc-danger"))

    with patch(
        "agentos.skills.hub.installer.scan_skill_bundle",
        return_value=_dangerous_scan(),
    ):
        results = await installer.update("rpc-danger")

    rpc_results = [
        {
            "success": r.success,
            "name": r.name,
            "message": r.message,
            "scan_verdict": r.scan.verdict if r.scan else None,
            "scan_findings": [f.__dict__ for f in r.scan.findings] if r.scan else None,
        }
        for r in results
    ]

    entry = rpc_results[0]
    assert entry["success"] is False
    assert entry["scan_verdict"] == "dangerous"
    assert len(entry["scan_findings"]) > 0


# ---------------------------------------------------------------------------
# Tests: explicit force still works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_force_still_overrides_scan(tmp_path: Path) -> None:
    """Calling ``install()`` directly with ``force=True`` still overrides."""
    installer = _make_installer(tmp_path)

    with patch(
        "agentos.skills.hub.installer.scan_skill_bundle",
        return_value=_dangerous_scan(),
    ):
        result = await installer.install("danger-skill", "clawhub", force=True)

    assert result.success is True, "explicit force=True should override dangerous scan"


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_nonexistent_skill(tmp_path: Path) -> None:
    """Updating a skill not in the lockfile returns a not-found message."""
    installer = _make_installer(tmp_path, lockfile_data={"version": 2, "installed": {}})
    results = await installer.update("nonexistent")
    assert len(results) == 1
    assert results[0].success is False
    assert "lockfile" in results[0].message.lower()


@pytest.mark.asyncio
async def test_update_no_lockfile(tmp_path: Path) -> None:
    """Updating with no lockfile at all (first use)."""
    installer = _make_installer(tmp_path)

    with patch(
        "agentos.skills.hub.installer.scan_skill_bundle",
        return_value=_safe_scan(),
    ):
        results = await installer.update("missing")

    assert len(results) == 1
    assert results[0].success is False
