"""Tests for the env write-policy gate."""

from __future__ import annotations

import pytest

from agentos import env_policy
from agentos.env_policy import EnvPolicyError


class TestNameValidation:
    @pytest.mark.parametrize("name", ["A", "_A", "OPENAI_API_KEY", "a1", "_", "X_9_Z"])
    def test_accepts_portable_names(self, name: str) -> None:
        env_policy.assert_valid_name(name)

    @pytest.mark.parametrize("name", ["", "1BAD", "A-B", "A B", "A.B", "127_0_0_1", "é"])
    def test_rejects_non_portable_names(self, name: str) -> None:
        with pytest.raises(EnvPolicyError, match="Invalid environment variable name"):
            env_policy.assert_valid_name(name)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(EnvPolicyError):
            env_policy.assert_valid_name(None)  # type: ignore[arg-type]


class TestDenylist:
    @pytest.mark.parametrize(
        "name",
        [
            # loader / linker
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            # interpreter
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "NODE_OPTIONS",
            # shell and implicitly-invoked commands
            "PATH",
            "SHELL",
            "EDITOR",
            "BASH_ENV",
            "GIT_SSH_COMMAND",
            # AgentOS posture and state location
            "AGENTOS_SENSITIVE_PATHS_DISABLED",
            "AGENTOS_SAFE_BIN_ALLOW",
            "AGENTOS_AGENT_PERMISSIONS",
            "AGENTOS_HOOKS",
            "AGENTOS_GATEWAY_TOKEN",
            "AGENTOS_STATE_DIR",
            "AGENTOS_GATEWAY_PORT",
            "AGENTOS_HTTP_DOWNLOAD_LIMIT",
        ],
    )
    def test_execution_and_posture_names_are_refused(self, name: str) -> None:
        assert env_policy.is_writable(name) is False
        with pytest.raises(EnvPolicyError, match="cannot be written through AgentOS"):
            env_policy.assert_writable(name)

    @pytest.mark.parametrize(
        "name",
        [
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "BASE_RPC_URL",
            "TELEGRAM_BOT_TOKEN",
            # AGENTOS_ is NOT blanket-blocked: ordinary credentials use it too.
            "AGENTOS_LLM_API_KEY",
            "AGENTOS_LLM_PROVIDER",
        ],
    )
    def test_ordinary_credentials_stay_writable(self, name: str) -> None:
        assert env_policy.is_writable(name) is True
        env_policy.assert_writable(name)

    @pytest.mark.parametrize(
        "name",
        [
            # proxy vars that must be denied
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "AGENTOS_LLM_PROXY",
            # trust env — gates the proxy injection path
            "AGENTOS_TRUST_ENV",
        ],
    )
    def test_proxy_and_trust_env_names_are_refused(self, name: str) -> None:
        assert env_policy.is_writable(name) is False
        with pytest.raises(EnvPolicyError, match="cannot be written through AgentOS"):
            env_policy.assert_writable(name)

    @pytest.mark.parametrize(
        "name",
        [
            # httpx normalizes proxies to lowercase; mixed-case variants must
            # also be blocked or they bypass the literal denylist check.
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "Http_Proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        ],
    )
    def test_proxy_names_are_refused_case_insensitively(self, name: str) -> None:
        """httpx lowercases proxy var names, so mixed-case variants must be
        blocked to prevent a case-mutation bypass."""
        assert env_policy.is_writable(name) is False
        with pytest.raises(EnvPolicyError, match="cannot be written through AgentOS"):
            env_policy.assert_writable(name)

    def test_invalid_name_is_not_writable_even_when_absent_from_denylist(self) -> None:
        assert env_policy.is_writable("1BAD") is False


class TestSanitizeValue:
    @pytest.mark.parametrize("value", ["", "plain", "with space", "tab\there", "unicode-á", "#h"])
    def test_accepts_single_line_values(self, value: str) -> None:
        assert env_policy.sanitize_value("K", value) == value

    @pytest.mark.parametrize("value", ["a\nb", "a\r\nb", "a\r", "trailing\n"])
    def test_rejects_line_breaks_instead_of_truncating(self, value: str) -> None:
        # Silently stripping would store a mangled credential that fails much
        # later; refusing surfaces the problem at the point of the mistake.
        with pytest.raises(EnvPolicyError, match="line break"):
            env_policy.sanitize_value("K", value)

    @pytest.mark.parametrize("value", ["a\x00b", "a\x07b", "a\x7fb"])
    def test_rejects_control_characters(self, value: str) -> None:
        with pytest.raises(EnvPolicyError, match="control character"):
            env_policy.sanitize_value("K", value)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(EnvPolicyError, match="must be a string"):
            env_policy.sanitize_value("K", 42)  # type: ignore[arg-type]


class TestSecretHeuristics:
    @pytest.mark.parametrize(
        "name",
        ["OPENAI_API_KEY", "SLACK_BOT_TOKEN", "APP_SECRET", "DB_PASSWORD", "VERTEX_CREDENTIALS"],
    )
    def test_credential_shaped_names_are_secret(self, name: str) -> None:
        assert env_policy.is_secret_name(name) is True

    @pytest.mark.parametrize("name", ["BASE_RPC_URL", "OPENAI_BASE_URL", "LOG_LEVEL", "TZ"])
    def test_plain_settings_are_not_secret(self, name: str) -> None:
        assert env_policy.is_secret_name(name) is False


class TestMask:
    def test_none_stays_none(self) -> None:
        assert env_policy.mask(None) is None

    def test_short_values_are_replaced_wholesale(self) -> None:
        # Revealing 4 of 6 characters is not masking.
        masked = env_policy.mask("short")
        assert masked == "•" * 8
        assert "short" not in masked

    def test_long_values_keep_only_recognisable_edges(self) -> None:
        value = "sk-proj-" + "x" * 40 + "wxyz"
        masked = env_policy.mask(value)
        assert masked == "sk-p…wxyz"
        assert value not in masked
