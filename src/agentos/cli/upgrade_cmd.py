"""``agentos upgrade`` — upgrade the package and restart the managed gateway.

Design derived from the OpenClaw / Nous Hermes daemon+CLI case studies. Two
regrets drive the whole command:

* **Silent version skew** — a "successful" upgrade that leaves the daemon on
  old code. So a successful package upgrade restarts the managed gateway by
  default and then VERIFIES the running version equals the new one before
  reporting success.
* **Upgrade subprocess hangs on macOS** (Hermes) — PATH gaps and no timeout.
  So the delegated tool is resolved to an absolute path against a hardened
  PATH, the subprocess runs under a bounded timeout, and on timeout the whole
  process group is killed with recovery guidance. Never a half-state.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import typer

from agentos import __version__
from agentos.cli.install_method import (
    build_upgrade_plan,
    hardened_path_env,
    installed_from_directory,
)
from agentos.cli.ui import console, markup_escape

# Default upgrade-subprocess timeout (seconds). Overridable via --timeout.
_DEFAULT_TIMEOUT_S = 600.0
# Bounded wait for the restarted gateway to report the new version.
_VERIFY_TIMEOUT_S = 30.0
_VERIFY_POLL_S = 0.5


@dataclass
class UpgradeRunResult:
    ok: bool
    timed_out: bool
    returncode: int | None
    stdout: str
    stderr: str


def _installed_version_via(executable: str, *, env: dict[str, str]) -> str | None:
    """Read the installed dist version using a FRESH subprocess of ``executable``.

    Running in a fresh process of the (possibly upgraded) interpreter avoids
    the stale ``importlib.metadata`` cache of the currently-running process.
    """

    code = "import importlib.metadata as m; print(m.version('use-agent-os'))"
    try:
        proc = subprocess.run(  # noqa: S603 - argv built internally
            [executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=15.0,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    return out or None


def _run_upgrade_subprocess(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: float,
) -> UpgradeRunResult:
    """Run the delegated upgrade command under a hard timeout.

    On timeout the whole process group is killed (SIGKILL after SIGTERM) so no
    half-finished child survives — a hung ``uv``/``pipx`` invocation must never
    leave a background zombie.
    """

    start_new_session = os.name != "nt"
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv built internally
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=start_new_session,
        )
    except OSError as exc:
        stderr_msg = str(exc)
        # On Windows, retry once after killing AgentOS/uv processes (#1365)
        if sys.platform == "win32" and "Access is denied" in stderr_msg:
            _kill_agentos_processes_windows()
            try:
                proc = subprocess.Popen(  # noqa: S603
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    start_new_session=start_new_session,
                )
            except OSError as exc2:
                return UpgradeRunResult(
                    ok=False, timed_out=False, returncode=None,
                    stdout="", stderr=f"{stderr_msg}; retry: {exc2}",
                )
        else:
            return UpgradeRunResult(
                ok=False, timed_out=False, returncode=None, stdout="", stderr=stderr_msg,
            )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            # The tree is dead, but orphaned grandchildren can keep the
            # inherited stdout/stderr pipe handles open, so communicate() would
            # block past the timeout it was supposed to enforce. Don't let a
            # stuck handle hang the CLI — give up on the streams.
            stdout, stderr = "", ""
        return UpgradeRunResult(
            ok=False,
            timed_out=True,
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
        )

    return UpgradeRunResult(
        ok=proc.returncode == 0,
        timed_out=False,
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
        _kill_agentos_processes_windows()
        return
    try:
        pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined]
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):  # type: ignore[attr-defined]
        try:
            os.killpg(pgid, sig)  # type: ignore[attr-defined]
        except ProcessLookupError:
            return
        except OSError:
            return
        time.sleep(0.2)
        if proc.poll() is not None:
            return


def _kill_agentos_processes_windows() -> None:
    """Kill running AgentOS/uv processes to release package file locks on Windows."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "agentos.exe"],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "uv.exe"],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def _query_gateway_version(config_path: str | None) -> str | None:
    """Return the gateway's handshake-reported version (or ``None``)."""

    from agentos.cli.gateway_cmd import gateway_handshake_version

    return gateway_handshake_version(config_path=config_path)


def _restart_and_verify(
    *,
    config_path: str | None,
    expected_version: str,
    json_output: bool,
) -> bool:
    """Restart the managed gateway (if running) and verify the new version.

    Returns True on verified restart (or nothing-to-restart), False on failure.
    """

    from agentos.cli.gateway_cmd import _lifecycle_manager

    manager = _lifecycle_manager(port=None, bind=None, listen="", config_path=config_path)
    status = manager.status()
    if not (status.state == "running" and status.managed):
        console.print(
            "[dim]Gateway is not running (managed) — nothing to restart. "
            "It will pick up the new version on next start.[/dim]"
        )
        return True

    console.print("Restarting managed gateway…")
    result = manager.restart()
    if result.exit_code != 0:
        console.print(
            f"[red]Gateway restart failed:[/red] {result.message or result.code or result.state}"
        )
        return False

    deadline = time.monotonic() + _VERIFY_TIMEOUT_S
    observed: str | None = None
    while time.monotonic() <= deadline:
        observed = _query_gateway_version(config_path)
        if observed == expected_version:
            console.print(f"Gateway: restarted and verified ({expected_version}).")
            return True
        time.sleep(_VERIFY_POLL_S)

    console.print(
        f"[red]Gateway restart could not be verified.[/red] Expected "
        f"{expected_version}, gateway reports {observed or 'unreachable'}.\n"
        "Recovery: run 'agentos gateway status' to inspect, then "
        "'agentos gateway restart' to retry.",
    )
    return False


def _emit_source_install_notice(source_dir: Path) -> None:
    """Tell a source installer that this command installs the RELEASE instead.

    ``agentos upgrade`` deliberately does not build from a checkout — only
    ``scripts/install_source.sh`` rebuilds the Control UI before installing. A
    source installer who is not told that would find their local code replaced
    with no idea how to get it back, so name the way back explicitly. Purely
    informational: it never blocks, prompts, or changes the exit code.
    """

    path = markup_escape(str(source_dir))
    console.print(
        f"[dim]This install was built from {path}.\n"
        "'agentos upgrade' installs the published PyPI release instead.\n"
        f"To install that checkout again: cd {path} && "
        "bash scripts/install_source.sh[/dim]"
    )


def _emit_no_restart_warning(new_version: str) -> None:
    old = __version__
    print(
        f"⚠ Gateway still running OLD version {old} — run 'agentos gateway restart' "
        f"to apply {new_version}.",
        file=sys.stderr,
    )


def upgrade_command(
    check: bool = typer.Option(
        False, "--check", help="Only report whether a newer version exists; change nothing."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would run and touch nothing."
    ),
    no_restart: bool = typer.Option(
        False, "--no-restart", help="Do not restart the managed gateway after upgrading."
    ),
    timeout: float = typer.Option(
        _DEFAULT_TIMEOUT_S, "--timeout", help="Upgrade subprocess timeout in seconds."
    ),
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Upgrade AgentOS to the published release and restart the gateway to match.

    Detects the install method (uv-tool / pipx / pip / editable) and delegates
    to the right installer, always targeting the PyPI release of
    ``use-agent-os[recommended]`` — including when the current install was built
    from a local checkout. To install a checkout instead, run
    ``bash scripts/install_source.sh``, which rebuilds the Control UI first.
    Config migrations (with an automatic timestamped backup) run at gateway
    start.
    """

    from agentos.cli.output import print_json

    plan = build_upgrade_plan()
    env = hardened_path_env()

    # --check: query PyPI without changing anything.
    if check:
        from agentos.compat.pypi_client import latest_version
        from agentos.compat.version_utils import is_newer

        latest = latest_version(timeout=5.0)
        if latest is None:
            msg = "could not check (offline)"
            if json_output:
                print_json({"current": __version__, "latest": None, "status": "offline"})
            else:
                console.print(msg)
            raise typer.Exit(0)
        newer = is_newer(latest, __version__)
        if json_output:
            print_json(
                {
                    "current": __version__,
                    "latest": latest,
                    "status": "outdated" if newer else "up-to-date",
                }
            )
        elif newer:
            console.print(f"A newer version is available: {__version__} → {latest}")
        else:
            console.print(f"Up to date ({__version__}).")
        raise typer.Exit(0)

    # Non-delegated installs: print the exact manual command, exit 3.
    if not plan.delegated:
        # The hint carries a ``dist[extras]`` spec, whose brackets Rich would
        # otherwise eat as markup — printing a command that silently drops the
        # extras is exactly the failure this whole change is about.
        message = (
            f"AgentOS was installed via {plan.method.value}; automatic upgrade is not "
            f"available for this method.\nRun this to upgrade:\n    "
            f"{markup_escape(plan.manual_hint)}"
        )
        if json_output:
            print_json(
                {
                    "method": plan.method.value,
                    "delegated": False,
                    "manualCommand": plan.manual_hint,
                }
            )
        else:
            console.print(message)
        raise typer.Exit(3)

    # This command always installs the published release. When the current
    # install was built from a local checkout, say so before touching anything.
    source_dir = installed_from_directory()

    # --dry-run: print what would run, touch nothing.
    if dry_run:
        printable = " ".join(plan.command)
        if json_output:
            print_json(
                {
                    "method": plan.method.value,
                    "command": plan.command,
                    "wouldRestart": not no_restart,
                    "dryRun": True,
                    "sourceDirectory": str(source_dir) if source_dir else None,
                }
            )
        else:
            if source_dir is not None:
                _emit_source_install_notice(source_dir)
            console.print(f"Would run: {markup_escape(printable)}")
            console.print(
                "Would then restart and verify the managed gateway."
                if not no_restart
                else "Would NOT restart the gateway (--no-restart)."
            )
        raise typer.Exit(0)

    # Execute the delegated upgrade under a bounded timeout.
    if source_dir is not None:
        _emit_source_install_notice(source_dir)
    console.print(f"Upgrading use-agent-os via {plan.method.value}…")
    result = _run_upgrade_subprocess(plan.command, env=env, timeout=timeout)
    if result.stdout.strip():
        console.print(markup_escape(result.stdout.strip()))

    if result.timed_out:
        console.print(
            f"[red]Upgrade timed out after {timeout:.0f}s and was terminated.[/red]\n"
            "The upgrade tool was killed (process group), so no half-finished child "
            "is left running.\nRecovery: re-run 'agentos upgrade' (optionally with a "
            f"larger --timeout), or run '{markup_escape(plan.manual_hint)}' manually.",
        )
        raise typer.Exit(1)

    if not result.ok:
        console.print(
            f"[red]Upgrade failed (exit {result.returncode}).[/red]\n"
            f"{markup_escape(result.stderr.strip())}"
        )
        raise typer.Exit(1)

    # Report old → new by reading the version from a fresh subprocess of the
    # (now upgraded) executable, avoiding this process's stale metadata cache.
    new_version = _installed_version_via(sys.executable, env=env) or "unknown"
    console.print(f"Upgraded: {__version__} → {new_version}")
    console.print(
        "[dim]Config migrations (with an automatic timestamped backup) run at "
        "gateway start.[/dim]"
    )

    if no_restart:
        _emit_no_restart_warning(new_version)
        if json_output:
            print_json(
                {
                    "old": __version__,
                    "new": new_version,
                    "restarted": False,
                    "gatewayVersionApplied": False,
                    "sourceDirectory": str(source_dir) if source_dir else None,
                }
            )
        raise typer.Exit(0)

    verified = _restart_and_verify(
        config_path=config_path,
        expected_version=new_version,
        json_output=json_output,
    )
    if json_output:
        print_json(
            {
                "old": __version__,
                "new": new_version,
                "restarted": True,
                "verified": verified,
                "sourceDirectory": str(source_dir) if source_dir else None,
            }
        )
    if not verified:
        raise typer.Exit(1)
    raise typer.Exit(0)
