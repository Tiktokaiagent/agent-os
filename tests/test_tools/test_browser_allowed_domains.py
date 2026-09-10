"""Tests for browser allowed_domains normalization (issue #1478)."""

from agentos.tools.builtin.browser import _domain_allowed, configure_browser
from types import SimpleNamespace


def test_bare_hostname_matches() -> None:
    configure_browser(SimpleNamespace(allowed_domains=["example.com"]))
    assert _domain_allowed("https://example.com/p") is True
    assert _domain_allowed("https://www.example.com/p") is True
    assert _domain_allowed("https://evil-example.com/p") is False


def test_dot_prefix_normalized() -> None:
    configure_browser(SimpleNamespace(allowed_domains=[".example.com"]))
    assert _domain_allowed("https://example.com/p") is True
    assert _domain_allowed("https://www.example.com/p") is True


def test_wildcard_prefix_normalized() -> None:
    configure_browser(SimpleNamespace(allowed_domains=["*.example.com"]))
    assert _domain_allowed("https://example.com/p") is True
    assert _domain_allowed("https://sub.example.com/p") is True


def test_scheme_stripped() -> None:
    configure_browser(SimpleNamespace(allowed_domains=["https://example.com"]))
    assert _domain_allowed("https://example.com/p") is True


def test_trailing_slash_stripped() -> None:
    configure_browser(SimpleNamespace(allowed_domains=["example.com/"]))
    assert _domain_allowed("https://example.com/p") is True


def test_case_insensitive() -> None:
    configure_browser(SimpleNamespace(allowed_domains=["EXAMPLE.COM"]))
    assert _domain_allowed("https://example.com/p") is True


def test_multiple_spellings_all_work() -> None:
    domain = "example.com"
    for spelling in [domain, ".example.com", "*.example.com",
                     "https://example.com", "example.com/"]:
        configure_browser(SimpleNamespace(allowed_domains=[spelling]))
        assert _domain_allowed("https://example.com/p") is True, f"{spelling} failed"
        assert _domain_allowed("https://www.example.com/p") is True, f"{spelling} sub failed"
