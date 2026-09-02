"""SearchResult.source is populated by each search provider for injection defense (#688).

Each provider (brave, tavily, duckduckgo) must tag every result with its origin so
the tool layer can surface the provider identity alongside each result.  The
``SearchResult`` dataclass default for ``source`` is ``""`` (backward compat for
code that constructs results directly), but the tool's ``_search_payload`` must
serialise that to ``""``, not ``None``, so downstream injection guards have a
consistent type.
"""

from __future__ import annotations

from agentos.search.types import SearchResult
from agentos.tools.builtin.web import _search_error_payload, _search_payload


class TestSearchResultSourceField:
    """SearchResult.source is populated correctly in payloads."""

    def test_source_defaults_to_empty_string(self) -> None:
        """Legacy SearchResult without source should have empty string."""
        result = SearchResult(title="T", url="https://x.com", snippet="s")
        assert result.source == ""

    def test_source_is_empty_string_in_payload(self) -> None:
        """Search payload serializes missing source as empty string."""
        result = SearchResult(title="T", url="https://x.com", snippet="s")
        payload = _search_payload("test", "duckduckgo", [result])
        assert payload["results"][0]["source"] == ""

    def test_source_in_payload(self) -> None:
        """Search payload carries the source field."""
        result = SearchResult(title="T", url="https://x.com", snippet="s", source="brave")
        payload = _search_payload("test", "brave", [result])
        assert payload["results"][0]["source"] == "brave"

    def test_source_isolation(self) -> None:
        """Each result keeps its own source tag when mixed providers."""
        results = [
            SearchResult(title="A", url="https://a.com", snippet="a", source="brave"),
            SearchResult(title="B", url="https://b.com", snippet="b", source="tavily"),
            SearchResult(title="C", url="https://c.com", snippet="c", source="duckduckgo"),
        ]
        payload = _search_payload("test", "mixed", results)
        sources = {r["source"] for r in payload["results"]}
        assert sources == {"brave", "tavily", "duckduckgo"}

    def test_error_payload_no_source(self) -> None:
        """Error payload returns empty results, no source field leak."""
        payload = _search_error_payload("test", "brave", Exception("fail"))
        assert payload["results"] == []

    def test_source_includes_all_providers(self) -> None:
        """Test with all three known provider origin tags."""
        for origin in ("brave", "tavily", "duckduckgo"):
            result = SearchResult(title="T", url="https://x.com", snippet="s", source=origin)
            payload = _search_payload("test", origin, [result])
            assert payload["results"][0]["source"] == origin
