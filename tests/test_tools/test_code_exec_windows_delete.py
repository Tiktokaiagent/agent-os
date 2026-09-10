"""Tests for Windows delete command detection (issue #1466)."""

from agentos.tools.builtin.code_exec import _check_code_destructive


def test_os_system_rm_still_detected() -> None:
    assert _check_code_destructive('import os; os.system("rm -rf /tmp")') is not None


def test_os_system_del_detected() -> None:
    assert _check_code_destructive('import os; os.system("del file.txt")') is not None


def test_os_system_erase_detected() -> None:
    assert _check_code_destructive('import os; os.system("erase file.txt")') is not None


def test_os_system_rd_detected() -> None:
    assert _check_code_destructive('import os; os.system("rd /s /q dir"') is not None


def test_subprocess_cmd_del_detected() -> None:
    cmd = 'import subprocess; subprocess.run(["cmd", "/c", "del", "file.txt"])'
    assert _check_code_destructive(cmd) is not None


def test_subprocess_powershell_remove_item_detected() -> None:
    cmd = 'import subprocess; subprocess.run(["powershell", "-c", "Remove-Item", "file.txt"])'
    assert _check_code_destructive(cmd) is not None


def test_echo_del_not_false_positive() -> None:
    assert _check_code_destructive('import os; os.system("echo del")') is None
