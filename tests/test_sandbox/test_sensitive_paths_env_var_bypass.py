"""Regression tests for the env-var-sensitive-path bypass fix (#985).

``sensitive_path_in_text`` and ``sensitive_path_marker`` previously failed
to detect sensitive paths written with ``$HOME/...`` or ``${HOME}/...``
instead of ``~/...``, allowing prompt-injected agents to silently read or
exfiltrate every credential directory the denylist is supposed to guard.

The fix operates at two layers:

1. ``_restore_env_var_shape`` re-attaches ``$`` / ``${\"\"\" stripped by
   ``_TOKEN_EDGE_CHARS`` during token extraction in ``sensitive_path_in_text``.
2. ``_expand_env_vars`` runs ``os.path.expandvars()`` in both
   ``sensitive_path_marker()`` and ``is_sensitive_path()`` so tokens like
   ``$HOME/.ssh/config`` resolve to the real absolute path before the
   prefix/suffix matchers run.
"""

from __future__ import annotations

from pathlib import Path

from agentos.sandbox.sensitive_paths import (
    _TOKEN_EDGE_CHARS,
    _expand_env_vars,
    _restore_env_var_shape,
    is_sensitive_path,
    sensitive_path_in_text,
    sensitive_path_marker,
    sensitive_target_in_command,
)

# ---------------------------------------------------------------------------
# _expand_env_vars
# ---------------------------------------------------------------------------


def test_expand_simple_home() -> None:
    """``$HOME/.ssh/config`` expands to the real home-relative path."""
    result = _expand_env_vars("$HOME/.ssh/config")
    assert result == str(Path.home() / ".ssh" / "config")


def test_expand_brace_home() -> None:
    """``${HOME}/.ssh/config`` expands correctly."""
    result = _expand_env_vars("${HOME}/.ssh/config")
    assert result == str(Path.home() / ".ssh" / "config")


def test_expand_brace_with_no_closing() -> None:
    """A trailing ``${HOME`` without ``}`` is left alone (not a valid ref)."""
    result = _expand_env_vars("${HOME/.ssh/config")
    # os.path.expandvars leaves invalid syntax mostly as-is
    assert "$" in result


def test_expand_no_var_is_noop() -> None:
    """Plain text without ``$`` returns unchanged."""
    assert _expand_env_vars("/etc/passwd") == "/etc/passwd"
    assert _expand_env_vars("~/.ssh/config") == "~/.ssh/config"


def test_expand_nonexistent_var() -> None:
    """An unset variable name is left as-is or becomes empty string depending
    on the OS — either way should not crash."""
    result = _expand_env_vars("$NONEXISTENT_VAR_XYZ/.ssh/config")
    # Should not raise and should not produce a valid absolute path
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _restore_env_var_shape
# ---------------------------------------------------------------------------


def test_restore_simple_dollar() -> None:
    """``$HOME/.ssh/config`` stripped to ``HOME/.ssh/config`` is restored."""
    raw = "$HOME/.ssh/config"
    stripped = raw.strip(_TOKEN_EDGE_CHARS)
    assert stripped == "HOME/.ssh/config"
    assert _restore_env_var_shape(raw, stripped) == "$HOME/.ssh/config"


def test_restore_brace_form() -> None:
    """``${HOME}/.aws`` stripped to ``HOME}/.aws`` is restored to
    ``${HOME}/.aws``."""
    raw = "${HOME}/.aws"
    stripped = raw.strip(_TOKEN_EDGE_CHARS)
    assert stripped == "HOME}/.aws"
    assert _restore_env_var_shape(raw, stripped) == "${HOME}/.aws"


def test_restore_non_var_is_noop() -> None:
    """A token without env-var prefix is unchanged."""
    assert _restore_env_var_shape("cat", "cat") == "cat"
    assert _restore_env_var_shape("/etc", "/etc") == "/etc"


def test_restore_dollar_but_no_path() -> None:
    """``$ALONE`` (no slashes) stripped to ``ALONE`` is restored."""
    raw = "$ALONE"
    stripped = raw.strip(_TOKEN_EDGE_CHARS)
    assert _restore_env_var_shape(raw, stripped) == "$ALONE"


def test_restore_brace_long_var() -> None:
    """Longer var name like ``${XDG_CONFIG_HOME}`` works."""
    raw = "${XDG_CONFIG_HOME}/git/config"
    stripped = raw.strip(_TOKEN_EDGE_CHARS)
    assert _restore_env_var_shape(raw, stripped) == "${XDG_CONFIG_HOME}/git/config"


# ---------------------------------------------------------------------------
# is_sensitive_path — env-var form
# ---------------------------------------------------------------------------


def test_is_sensitive_path_home_ssh() -> None:
    """``$HOME/.ssh/config`` is recognized as ``~/.ssh``."""
    assert is_sensitive_path("$HOME/.ssh/config") == "~/.ssh"


def test_is_sensitive_path_home_aws() -> None:
    assert is_sensitive_path("$HOME/.aws/credentials") == "~/.aws"


def test_is_sensitive_path_home_kube() -> None:
    assert is_sensitive_path("$HOME/.kube/config") == "~/.kube"


def test_is_sensitive_path_home_gnupg() -> None:
    assert is_sensitive_path("$HOME/.gnupg/secring.gpg") == "~/.gnupg"


def test_is_sensitive_path_home_password_store() -> None:
    assert is_sensitive_path("$HOME/.password-store/something") == "~/.password-store"


def test_is_sensitive_path_home_gcloud() -> None:
    assert is_sensitive_path("$HOME/.config/gcloud/creds.json") == "~/.config/gcloud"


def test_is_sensitive_path_brace_home_ssh() -> None:
    """``${HOME}/.ssh/config`` is recognized as ``~/.ssh``."""
    assert is_sensitive_path("${HOME}/.ssh/config") == "~/.ssh"


def test_is_sensitive_path_user_dot_ssh() -> None:
    """``$USER/.ssh/authorized_keys`` — ``$USER`` is a relative path so the
    system falls back to leaf matching, but leaf markers include
    ``/authorized_keys``."""
    result = is_sensitive_path("$USER/.ssh/authorized_keys")
    assert result is not None, "should block via leaf marker"


def test_is_sensitive_path_docker_config() -> None:
    """$HOME/.docker/config resolves correctly."""
    result = is_sensitive_path("$HOME/.docker/config")
    assert result == "~/.docker/config"

    result2 = sensitive_path_in_text("cat $HOME/.docker/config")
    assert result2 == "~/.docker/config"


def test_is_sensitive_path_etc_via_home_remains_blocked() -> None:
    """Absolute sensitive prefixes like ``/etc`` are unaffected by env-var
    expansion."""
    assert is_sensitive_path("/etc/passwd") == "/etc"


def test_is_sensitive_path_non_sensitive_via_home_is_none() -> None:
    """A non-sensitive path under ``$HOME`` should not become sensitive just
    because ``$HOME`` was expanded."""
    assert is_sensitive_path("$HOME/notes.txt") is None


# ---------------------------------------------------------------------------
# sensitive_path_in_text — complete bypass scenarios from the issue
# ---------------------------------------------------------------------------


def test_bypass_tilde_ssh_baseline() -> None:
    """The ``~/.ssh/config`` form was already caught before the fix."""
    assert sensitive_path_in_text("cat ~/.ssh/config") == "~/.ssh"


def test_bypass_home_ssh() -> None:
    """``cat $HOME/.ssh/config`` — the primary bypass from the issue."""
    assert sensitive_path_in_text("cat $HOME/.ssh/config") == "~/.ssh"


def test_bypass_home_aws() -> None:
    """``cp $HOME/.aws/credentials /tmp/leak.txt`` — exfiltration vector."""
    assert sensitive_path_in_text("cp $HOME/.aws/credentials /tmp/leak.txt") == "~/.aws"


def test_bypass_home_kube() -> None:
    """``cat $HOME/.kube/config`` — Kubernetes credential theft."""
    assert sensitive_path_in_text("cat $HOME/.kube/config") == "~/.kube"


def test_bypass_home_gnupg() -> None:
    """``cat $HOME/.gnupg/secring.gpg`` — GPG key theft."""
    assert sensitive_path_in_text("cat $HOME/.gnupg/secring.gpg") == "~/.gnupg"


def test_bypass_home_password_store() -> None:
    """``cat $HOME/.password-store/file`` — password-store access."""
    assert sensitive_path_in_text("cat $HOME/.password-store/file") == "~/.password-store"


def test_bypass_home_gcloud() -> None:
    """``cat $HOME/.config/gcloud/creds.json`` — GCP credential access."""
    assert sensitive_path_in_text("cat $HOME/.config/gcloud/creds.json") == "~/.config/gcloud"


def test_bypass_brace_home_aws() -> None:
    """``cat ${HOME}/.aws/credentials`` — brace-form bypass."""
    assert sensitive_path_in_text("cat ${HOME}/.aws/credentials") == "~/.aws"


def test_bypass_brace_home_kube() -> None:
    """``cp ${HOME}/.kube/config /tmp/leak.txt`` — brace-form exfil."""
    assert sensitive_path_in_text("cp ${HOME}/.kube/config /tmp/leak.txt") == "~/.kube"


def test_bypass_home_rm_ssh() -> None:
    """``rm $HOME/.ssh`` — destructive bypass."""
    assert sensitive_path_in_text("rm $HOME/.ssh") == "~/.ssh"


def test_bypass_home_rm_aws() -> None:
    """``rm -rf $HOME/.aws`` — destructive bypass."""
    assert sensitive_path_in_text("rm -rf $HOME/.aws") == "~/.aws"


def test_bypass_home_netrc() -> None:
    """``cat $HOME/.netrc`` — credential access (leaf suffix still works)."""
    assert sensitive_path_in_text("cat $HOME/.netrc") is not None


# ---------------------------------------------------------------------------
# sensitive_path_marker — direct env-var path
# ---------------------------------------------------------------------------


def test_marker_home_expanded() -> None:
    """``$HOME/.ssh/config`` blocked at the marker level."""
    assert sensitive_path_marker("$HOME/.ssh/config") == "~/.ssh"


def test_marker_brace_home_expanded() -> None:
    assert sensitive_path_marker("${HOME}/.ssh/config") == "~/.ssh"


def test_marker_home_azure() -> None:
    """``$HOME/.azure`` is blocked."""
    assert sensitive_path_marker("$HOME/.azure") is not None


# ---------------------------------------------------------------------------
# sensitive_target_in_command — destructive operations
# ---------------------------------------------------------------------------


def test_target_rm_home_ssh() -> None:
    """``rm -rf $HOME/.ssh`` — destructive, must block."""
    assert sensitive_target_in_command("rm -rf $HOME/.ssh") == "~/.ssh"


def test_target_rm_brace_home_aws() -> None:
    """``rm -rf ${HOME}/.aws`` — destructive brace form."""
    assert sensitive_target_in_command("rm -rf ${HOME}/.aws") == "~/.aws"


def test_target_rm_root_via_home_no_false_positive() -> None:
    """A destructive command toward a non-sensitive path stays allowed."""
    assert sensitive_target_in_command("rm -rf $HOME/scratch") is None


# ---------------------------------------------------------------------------
# Combined / compound commands
# ---------------------------------------------------------------------------


def test_compound_with_home_bypass() -> None:
    """A compound command with a benign first segment and a home-bypass
    second segment must still block."""
    workspace = Path("/workspace")
    result = sensitive_path_in_text(
        "ls /tmp; cat $HOME/.ssh/config",
        workspace=workspace,
    )
    assert result == "~/.ssh"


def test_compound_destructive_home() -> None:
    """``rm /tmp/ok; rm -rf $HOME/.aws`` blocks at the tool boundary."""
    workspace = Path("/workspace")
    result = sensitive_target_in_command(
        "rm /tmp/ok; rm -rf $HOME/.aws",
        workspace=workspace,
    )
    assert result == "~/.aws"


# ---------------------------------------------------------------------------
# Regression: non-env-var paths must still work
# ---------------------------------------------------------------------------


def test_tilde_paths_still_work() -> None:
    """Existing tilde-path detection is not broken."""
    assert sensitive_path_in_text("cat ~/.ssh/config") == "~/.ssh"
    assert sensitive_path_in_text("cat ~/.aws/credentials") == "~/.aws"
    assert sensitive_path_in_text("cat ~/.kube/config") == "~/.kube"


def test_absolute_paths_still_work() -> None:
    """Existing absolute-path detection is not broken."""
    assert sensitive_path_in_text("cat /etc/passwd") == "/etc"
    assert sensitive_path_in_text("cat /proc/cpuinfo") == "/proc"
    assert sensitive_path_in_text("cat /boot/config") == "/boot"


def test_combined_tilde_and_expanded() -> None:
    """Mixed forms all resolve to the same marker."""
    assert sensitive_path_in_text("cat ~/.ssh/config") == "~/.ssh"
    assert sensitive_path_in_text("cat $HOME/.ssh/config") == "~/.ssh"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_path() -> None:
    """Empty strings are handled without error."""
    assert sensitive_path_in_text("") is None
    assert sensitive_path_marker("") is None


def test_path_with_only_dollar() -> None:
    """Standalone ``$`` does not crash."""
    assert sensitive_path_in_text("echo $") is None
    assert sensitive_path_in_text("$") is None


def test_many_env_vars_in_one_path() -> None:
    """Multiple env-var references in one command."""
    result = sensitive_path_in_text("cat $HOME/.ssh/config $HOME/notes")
    assert result == "~/.ssh"


def test_path_with_subprocess_like_syntax() -> None:
    """Python subprocess-like syntax (``['cat', '$HOME/.ssh/config']``)
    should still detect the env-var path."""
    result = sensitive_path_in_text("['cat', '$HOME/.ssh/config']")
    assert result == "~/.ssh"
