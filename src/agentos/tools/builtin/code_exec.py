"""Code execution tool — sandboxed Python execution via subprocess."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from agentos.sandbox.integration import (
    escalate_backend_denial,
    gate_action,
    get_runtime,
    run_under_backend,
)
from agentos.sandbox.types import DenialResult, SandboxRequest
from agentos.tools.registry import tool
from agentos.tools.types import ToolError, current_tool_context

# Destructive Python patterns that must go through the same approval flow as
# shell warnlist hits. Matches via AST (not regex) so obfuscation techniques
# like getattr() with concatenated names, __import__(), importlib imports,
# and exec/eval with destructive literals are caught.
_DESTRUCTIVE_MODULE_ATTRS: dict[str, frozenset[str]] = {
    "os": frozenset(["remove", "unlink", "rmdir", "removedirs", "system"]),
    "shutil": frozenset(["rmtree", "rmdir", "remove"]),
    "pathlib": frozenset(["unlink", "rmdir"]),
}
_DESTRUCTIVE_FUNCTIONS: frozenset[str] = frozenset([
    "remove", "unlink", "rmdir", "removedirs", "rmtree",
])
_DESTRUCTIVE_STRING_MARKERS: frozenset[str] = frozenset([
    "remove", "unlink", "rmdir", "rmtree", "removedirs",
])


_DESTRUCTIVE_PY_PATTERNS: list[tuple[str, str]] = [
    (r"\bos\.remove\s*\(", "os.remove()"),
    (r"\bos\.unlink\s*\(", "os.unlink()"),
    (r"\bos\.rmdir\s*\(", "os.rmdir()"),
    (r"\bos\.removedirs\s*\(", "os.removedirs()"),
    (r"\bshutil\.rmtree\s*\(", "shutil.rmtree()"),
    (r"\.unlink\s*\(", "Path.unlink()"),
    (r"\.rmdir\s*\(", "Path.rmdir()"),
    (r"\bos\.system\s*\([^)]*\brm\b", "os.system with rm"),
    (
        r"\bsubprocess\.(run|call|Popen|check_output|check_call)[^\n;]{0,200}\brm\b",
        "subprocess invoking rm",
    ),
    (
        r"\bsubprocess\.(run|call|Popen|check_output|check_call)[^\n;]{0,200}\brmdir\b",
        "subprocess invoking rmdir",
    ),
]


# Destructive module names that should be flagged when imported directly
_DESTRUCTIVE_IMPORT_MODULES: frozenset[str] = frozenset([
    "os", "shutil",
])

# Functions that can invoke arbitrary code with string arguments
_EVAL_LIKE_FUNCTIONS: frozenset[str] = frozenset([
    "exec", "eval", "compile",
])

# Functions used for dynamic attribute access
_ATTR_ACCESS_FUNCTIONS: frozenset[str] = frozenset([
    "getattr", "setattr", "delattr",
])


def _check_code_destructive(code: str) -> str | None:
    """Return a human-readable warning if *code* triggers a destructive pattern, else None.

    Uses AST analysis (supplemented by regex for non-parseable code) to detect:
    - Direct attribute access: os.remove(), shutil.rmtree(), Path.unlink()
    - getattr() with stacked args: getattr(os, "remove")()
    - __import__().something(): __import__("os").remove()
    - exec/eval with destructive keywords
    - importlib.import_module().something()
    - Combined/multiple destructive calls
    """
    # First: regex-based scan for obvious patterns (fast path)
    for pattern, label in _DESTRUCTIVE_PY_PATTERNS:
        if re.search(pattern, code):
            return f"blocked: {label}"

    # Second: AST analysis for obfuscated bypasses
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    imported_names: dict[str, str] = {}
    # Track aliases: from os import remove as rm -> rm -> os
    alias_map: dict[str, tuple[str, str]] = {}
    module_imports: set[str] = set()

    for node in ast.walk(tree):
        # Track imports to resolve attribute chains
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names[alias.asname or alias.name] = alias.name
                if alias.name in _DESTRUCTIVE_MODULE_ATTRS:
                    module_imports.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module in _DESTRUCTIVE_IMPORT_MODULES:
                module_imports.add(node.module)
                for alias in node.names:
                    attrs = _DESTRUCTIVE_MODULE_ATTRS.get(node.module, frozenset())
                    if alias.name in attrs:
                        return f"blocked: {node.module}.{alias.name}()"
                    # Wildcard imports from destructive modules
                    if alias.name == "*":
                        return f"blocked: from {node.module} import *"
                    alias_map[alias.asname or alias.name] = (node.module, alias.name)
            elif node.module in ["importlib", "builtins"]:
                for alias in node.names:
                    alias_map[alias.asname or alias.name] = (node.module, alias.name)

        # Check attribute access chains: os.remove, shutil.rmtree, etc.
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if isinstance(node.ctx, ast.Load):
                module = node.value.id
                resolved = imported_names.get(module, module)
                attr = node.attr
                if resolved in _DESTRUCTIVE_MODULE_ATTRS:
                    d_attrs = _DESTRUCTIVE_MODULE_ATTRS[resolved]
                    if attr in d_attrs:
                        return f"blocked: {module}.{attr}()"
                if attr in ("unlink", "rmdir"):
                    return f"blocked: Path.{attr}()"

        # Check getattr() with destructive string args
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _ATTR_ACCESS_FUNCTIONS and len(node.args) >= 2:
                attr_arg = node.args[1]
                if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str):
                    if attr_arg.value in _DESTRUCTIVE_FUNCTIONS:
                        v = attr_arg.value
                        return f"blocked: getattr(..., {v})()"
                # Concatenated strings: "rem" + "ove" -> check all parts
                if isinstance(attr_arg, ast.BinOp) and isinstance(attr_arg.op, ast.Add):
                    parts = _collect_add_parts(attr_arg)
                    reconstructed = "".join(p for p in parts if isinstance(p, str))
                    if reconstructed in _DESTRUCTIVE_FUNCTIONS:
                        return "blocked: getattr(..., reconstructed destructive name)"

        # Check __import__().attr()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # __import__("os").remove()
            inner_call = node.func.value
            if isinstance(inner_call, ast.Call) and isinstance(inner_call.func, ast.Name):
                if inner_call.func.id == "__import__" and inner_call.args:
                    mod_arg = inner_call.args[0]
                    if isinstance(mod_arg, ast.Constant) and isinstance(mod_arg.value, str):
                        if mod_arg.value in _DESTRUCTIVE_MODULE_ATTRS:
                            attr = node.func.attr
                            d_attrs = _DESTRUCTIVE_MODULE_ATTRS[mod_arg.value]
                            if attr in d_attrs:
                                mv = mod_arg.value
                                return f"blocked: __import__('{mv}').{attr}()"
            # importlib.import_module("os").remove()
            elif isinstance(inner_call, ast.Call) and isinstance(inner_call.func, ast.Attribute):
                is_import_lib = (
                    isinstance(inner_call.func.value, ast.Name)
                    and inner_call.func.value.id in ["importlib", "__import__"]
                    and inner_call.func.attr == "import_module"
                )
                if is_import_lib:
                    for a in inner_call.args:
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            if a.value in _DESTRUCTIVE_MODULE_ATTRS:
                                attr = node.func.attr
                                d_attrs = _DESTRUCTIVE_MODULE_ATTRS[a.value]
                                if attr in d_attrs:
                                    return f"blocked: importlib.import_module('{a.value}').{attr}()"

        # Check exec/eval with destructive string literals
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _EVAL_LIKE_FUNCTIONS and node.args:
                code_arg = node.args[0]
                if isinstance(code_arg, ast.Constant) and isinstance(code_arg.value, str):
                    # Scan the embedded string for destructive markers
                    for marker in _DESTRUCTIVE_STRING_MARKERS:
                        if marker in code_arg.value.lower():
                            fn = node.func.id
                            return f"blocked: {fn}() with destructive content"

        # Check alias_map: from os import remove as rm; rm("/tmp/x")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in alias_map:
                module_name, attr = alias_map[func_name]
                if module_name in _DESTRUCTIVE_MODULE_ATTRS:
                    d_attrs = _DESTRUCTIVE_MODULE_ATTRS[module_name]
                    if attr in d_attrs:
                        return f"blocked: {module_name}.{attr}() (via import alias)"
                if module_name == "builtins" and attr == "getattr":
                    # from builtins import getattr is the same as getattr
                    pass

        # Check chained imports: import os as X; getattr(X, "remove")()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _ATTR_ACCESS_FUNCTIONS and len(node.args) >= 2:
                obj_arg = node.args[0]
                attr_arg = node.args[1]
                obj_name = _get_name(obj_arg)
                if obj_name and obj_name in imported_names:
                    imported_module = imported_names[obj_name]
                    if imported_module in _DESTRUCTIVE_MODULE_ATTRS:
                        if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str):
                            d_attrs = _DESTRUCTIVE_MODULE_ATTRS[imported_module]
                            if attr_arg.value in d_attrs:
                                im = imported_module
                                return f"blocked: getattr({im}, ...) via alias"

        # Check subprocess/__import__ calls targeting destructive modules
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "__import__" and node.args:
                mod_arg = node.args[0]
                if isinstance(mod_arg, ast.Constant) and isinstance(mod_arg.value, str):
                    if mod_arg.value in _DESTRUCTIVE_IMPORT_MODULES:
                        mv = mod_arg.value
                        return f"blocked: dynamic __import__('{mv}')"

    return None


def _collect_add_parts(node: ast.AST) -> list[str | ast.AST]:
    """Recursively collect string parts from addition expressions."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _collect_add_parts(node.left)
        right = _collect_add_parts(node.right)
        return left + right
    return [node]


def _get_name(node: ast.AST) -> str | None:
    """Extract the name from a Name or Attribute expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _get_name(node.value)
    return None


_CODE_SENSITIVE_READ_TOKENS = (
    "open(",
    ".open(",
    ".read_text(",
    ".read_bytes(",
    "listdir(",
    "scandir(",
    "walk(",
    ".glob(",
    ".rglob(",
    "copyfile(",
    "copy2(",
    "copy(",
    "subprocess.",
    "os.system(",
    "os.popen(",
)
_CODE_NETWORK_TOKENS = (
    "httpx.",
    "requests.",
    "urllib.request",
    "http.client",
    "socket.",
    ".post(",
    ".put(",
    ".patch(",
)


def _iter_code_string_literals(code: str) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return re.findall(r"""["']([^"']{1,500})["']""", code)

    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
            if parts:
                values.append("".join(parts))
    return values


def _check_code_sensitive_access(code: str) -> tuple[str, str] | None:
    """Return (reason, marker) if Python code is trying to touch sensitive data."""
    lowered = code.lower()
    has_read_or_shell = any(token in lowered for token in _CODE_SENSITIVE_READ_TOKENS)

    ctx = current_tool_context.get()
    workspace = ctx.workspace_dir if ctx is not None else None

    from agentos.sandbox.sensitive_paths import sensitive_path_in_text, sensitive_path_marker

    for literal in _iter_code_string_literals(code):
        marker = sensitive_path_marker(literal, workspace=workspace) or sensitive_path_in_text(
            literal,
            workspace=workspace,
        )
        path_like_literal = literal.strip().startswith(("/", "~", "."))
        if marker is not None and (has_read_or_shell or path_like_literal):
            return "sensitive_path", marker

    from agentos.tools.builtin.web import _sensitive_body_marker, _sensitive_url_marker

    has_network = any(token in lowered for token in _CODE_NETWORK_TOKENS)
    if has_network:
        for literal in _iter_code_string_literals(code):
            marker = _sensitive_url_marker(literal)
            if marker is not None:
                return "sensitive_payload", marker
        marker = _sensitive_body_marker(code)
        if marker is not None:
            return "sensitive_payload", marker

    return None


_MAX_TIMEOUT = 120
_DEFAULT_TIMEOUT = 30
_MAX_OUTPUT_CHARS = 50_000
_SANDBOX_PYTHON_CANDIDATES: tuple[Path, ...] = (
    Path("/usr/bin/python3"),
    Path("/bin/python3"),
    Path("/usr/bin/python"),
    Path("/bin/python"),
)

# Only these env vars are forwarded to the sandbox subprocess.
# Secrets (API keys, tokens) are explicitly excluded.
_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "USER",
        "SHELL",
        "TERM",
        "PYTHONPATH",
    }
)


def _build_safe_env() -> dict[str, str]:
    """Return the sandbox environment: the safe base plus skill declarations.

    The allowlist above is what any code can see. A skill that declares
    ``metadata.requires.env`` adds its own names on top for the session that
    loaded it — that is the supported way for a skill to reach a third-party
    API from sandboxed code, and it is why the guard on the way out no longer
    has to guess whether a payload is a credential. A skill AgentOS did not
    ship is refused AgentOS's own credentials at registration, so this cannot
    widen past them.
    """
    from agentos.tools.env_passthrough import is_env_passthrough

    return {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_ENV_KEYS or is_env_passthrough(key)
    }


def _execution_result_json(
    *,
    returncode: int,
    stdout: str,
    stderr: str,
    timed_out: bool,
    elapsed_ms: int,
) -> str:
    # Redact secrets from captured output before it reaches the model.
    # shell.py does this on every output surface; execute_code must match the
    # same egress policy (see redact.redact_terminal_output). A script that
    # prints os.environ / a credential file would otherwise leak it raw.
    #
    # Redact BEFORE truncating: a credential straddling the output cap would
    # otherwise be cut in half first, and the surviving prefix no longer
    # matches the shape pattern, leaking a partial key.
    #
    # code_file=False is a deliberate divergence from redact_terminal_output,
    # which passes code_file=not assignments (assignment pass only for env
    # dumps / credential-file reads). execute_code output is arbitrary script
    # output that routinely prints os.environ and credential files, so the
    # assignment pass must run unconditionally here. The cost is that
    # `api_key=*** in printed source becomes `api_key=*** — acceptable
    # for a code-execution surface where real secrets are the norm.
    from agentos.redact import redact_sensitive_text

    redacted_stdout = redact_sensitive_text(stdout, force=True, code_file=False)
    redacted_stderr = redact_sensitive_text(stderr, force=True, code_file=False)
    return json.dumps(
        {
            "exit_code": returncode,
            "stdout": (redacted_stdout if redacted_stdout is not None else stdout)[
                :_MAX_OUTPUT_CHARS
            ],
            "stderr": (redacted_stderr if redacted_stderr is not None else stderr)[
                :_MAX_OUTPUT_CHARS
            ],
            "timed_out": timed_out,
            "elapsed_ms": elapsed_ms,
        },
        ensure_ascii=False,
    )


def _append_code_exec_sandbox_network_hint(*, stdout: str, stderr: str) -> str:
    from agentos.tools.builtin.shell import (
        _SANDBOX_NETWORK_HINT,
        _append_sandbox_network_hint,
        _looks_like_sandbox_network_failure,
    )

    if not _looks_like_sandbox_network_failure(stdout + "\n" + stderr):
        return stderr
    if stderr:
        return _append_sandbox_network_hint(stderr, force=True)
    return _SANDBOX_NETWORK_HINT


def _resolve_python_bin(*, sandbox_enabled: bool) -> str:
    """Resolve a Python executable that is visible from the execution mode."""
    if sandbox_enabled:
        # The bubblewrap backend exposes host /usr and /bin inside the sandbox,
        # but not the caller's project venv. `uv run` commonly puts
        # .venv/bin/python3 first on PATH, which is invisible after isolation.
        for candidate in _SANDBOX_PYTHON_CANDIDATES:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    else:
        current_python = Path(sys.executable)
        if current_python.is_file():
            return str(current_python)

    python_bin = shutil.which("python3") or shutil.which("python")
    if python_bin is None:
        raise ToolError("Python interpreter not found on PATH")
    return python_bin


@tool(
    name="execute_code",
    description=(
        "Execute Python code in an isolated subprocess and return stdout/stderr. "
        "When an active workspace is configured, code runs with that workspace "
        "as cwd; otherwise each invocation runs in a fresh temporary directory. "
        "Use for calculations, data processing, and validation."
    ),
    params={
        "code": {
            "type": "string",
            "description": "Python code to execute.",
        },
        "timeout": {
            "type": "number",
            "description": (
                f"Execution timeout in seconds (1-{_MAX_TIMEOUT}, default {_DEFAULT_TIMEOUT})."
            ),
        },
        "approval_id": {
            "type": "string",
            "description": "Approval record to consume for destructive Python operations.",
        },
    },
    required=["code"],
)
async def execute_code(
    code: str,
    timeout: float = _DEFAULT_TIMEOUT,
    approval_id: str | None = None,
) -> str:
    if not code.strip():
        raise ToolError("Code must not be empty")

    from agentos.tools.builtin.shell import _context_elevated_mode

    sensitive_access = _check_code_sensitive_access(code)
    if sensitive_access is not None and _context_elevated_mode() != "full":
        reason, marker = sensitive_access
        if reason == "sensitive_payload":
            from agentos.tools.builtin.web import _sensitive_body_block

            return _sensitive_body_block("execute_code", marker)

        from agentos.sandbox.sensitive_paths import build_block_envelope

        return json.dumps(
            build_block_envelope(
                "execute_code <python>",
                marker,
                tool_name="execute_code",
            ),
            ensure_ascii=False,
        )

    # Destructive-Python gate — mirrors the shell warnlist approval flow.
    warning = _check_code_destructive(code)
    if warning is not None:
        from agentos.tools.builtin.shell import (
            _approval_elevation_state,
            _check_exec_approval,
            _restore_approval_elevation,
        )

        prior_elevation = _approval_elevation_state()
        approval_response: dict[str, object] | None = None
        approval_granted = False
        try:
            approval_response = await _check_exec_approval(
                tool_name="execute_code",
                command=code[:200],
                workdir=None,
                warning=warning,
                approval_id=approval_id,
                background=False,
            )
            approval_granted = approval_response is None and _approval_elevation_state()
        finally:
            if not approval_granted:
                _restore_approval_elevation(prior_elevation)
        if approval_response is not None:
            return json.dumps(approval_response)

    timeout = max(1.0, min(float(timeout), _MAX_TIMEOUT))

    ctx = current_tool_context.get()
    runtime = get_runtime()
    sandbox_enabled = bool(runtime is not None and runtime.effective.sandbox_enabled)
    python_bin = _resolve_python_bin(sandbox_enabled=sandbox_enabled)
    workspace = (
        Path(ctx.workspace_dir).expanduser().resolve() if ctx and ctx.workspace_dir else None
    )
    cleanup_dir: str | None = None
    if workspace is not None:
        workspace.mkdir(parents=True, exist_ok=True)
        workdir_path = workspace
    elif runtime is not None and runtime.effective.sandbox_enabled:
        workdir_path = runtime.workspace.expanduser().resolve()
        workdir_path.mkdir(parents=True, exist_ok=True)
    else:
        workdir = tempfile.mkdtemp(prefix="agentos_exec_")
        workdir_path = Path(workdir)
        cleanup_dir = workdir
    start_ns = time.monotonic_ns()

    safe_env = _build_safe_env()

    from agentos.tools.builtin.shell import _elevated_mode

    elevated_bypass = _elevated_mode() in ("on", "bypass", "full")
    if runtime is None or (runtime.effective.sandbox_enabled and not elevated_bypass):
        decision, _policy, request = await gate_action(
            action_kind="code.exec",
            argv=(python_bin, "-c", code),
            cwd=workdir_path,
            env=safe_env,
        )
        if isinstance(decision, DenialResult):
            return json.dumps(decision.to_dict())
        backend_request = SandboxRequest(
            argv=(python_bin, "-c", code),
            cwd=request.cwd,
            action_kind=request.action_kind,
            policy=request.policy,
            env=safe_env,
        )
        try:
            sandbox_result = await run_under_backend(backend_request, runtime=runtime)
        except Exception as exc:
            return _execution_result_json(
                returncode=-1,
                stdout="",
                stderr=f"Execution error: {exc}",
                timed_out=False,
                elapsed_ms=0,
            )
        if sandbox_result.backend_notes:
            escalation = await escalate_backend_denial(
                sandbox_result, request, _policy, runtime=runtime
            )
            if isinstance(escalation, DenialResult):
                return json.dumps(escalation.to_dict())
            try:
                proc = await asyncio.create_subprocess_exec(
                    python_bin,
                    "-c",
                    code,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workdir_path),
                    env=safe_env,
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
                    return _execution_result_json(
                        returncode=-1,
                        stdout="",
                        stderr=f"Execution timed out after {timeout}s",
                        timed_out=True,
                        elapsed_ms=elapsed_ms,
                    )
                elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
                return _execution_result_json(
                    returncode=proc.returncode if proc.returncode is not None else -1,
                    stdout=stdout_bytes.decode("utf-8", errors="replace"),
                    stderr=stderr_bytes.decode("utf-8", errors="replace"),
                    timed_out=False,
                    elapsed_ms=elapsed_ms,
                )
            except Exception as exc:
                return _execution_result_json(
                    returncode=-1,
                    stdout="",
                    stderr=f"Execution error: {exc}",
                    timed_out=False,
                    elapsed_ms=0,
                )
        elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        stdout = sandbox_result.stdout
        stderr = sandbox_result.stderr
        stderr = _append_code_exec_sandbox_network_hint(stdout=stdout, stderr=stderr)
        return _execution_result_json(
            returncode=sandbox_result.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=sandbox_result.timed_out,
            elapsed_ms=elapsed_ms,
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            python_bin,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir_path),
            env=safe_env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
            return _execution_result_json(
                returncode=-1,
                stdout="",
                stderr=f"Execution timed out after {timeout}s",
                timed_out=True,
                elapsed_ms=elapsed_ms,
            )

        elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        return _execution_result_json(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            elapsed_ms=elapsed_ms,
        )
    except Exception as exc:
        return _execution_result_json(
            returncode=-1,
            stdout="",
            stderr=f"Execution error: {exc}",
            timed_out=False,
            elapsed_ms=0,
        )
    finally:
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
