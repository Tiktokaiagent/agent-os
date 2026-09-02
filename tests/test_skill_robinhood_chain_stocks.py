"""Offline regression tests for the robinhood-chain-stocks on-chain reader.

No network: every RPC read is stubbed. The cases below pin the two behaviours
that make the skill safe to answer with -- ABI decoding of real return blobs,
and refusing to resolve a company name to a community token that impersonates
it (Robinhood Chain is permissionless, and two entries in the public token list
are both called "GameStop" with symbol GME).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
SKILL_DIR = BUNDLED / "robinhood-chain-stocks"
_SCRIPT = SKILL_DIR / "scripts" / "chain_stocks.py"

_spec = importlib.util.spec_from_file_location("chain_stocks", _SCRIPT)
assert _spec is not None and _spec.loader is not None
chain_stocks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chain_stocks)

REAL_GME = "0x1b0e319c6a659f002271b69db8a7df2f911c153e"
FAKE_GME = "0x7e86381a763f0ecca2bdf27c54eac403ddd48123"
AAPL = "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9"

_TOKENS: list[dict[str, Any]] = [
    {
        "chainId": 4663,
        "address": AAPL,
        "name": "Apple • Robinhood Token",
        "symbol": "AAPL",
        "decimals": 18,
    },
    {
        "chainId": 4663,
        "address": REAL_GME,
        "name": "GameStop • Robinhood Token",
        "symbol": "GME",
        "decimals": 18,
    },
    # Community token impersonating the listing: same name, same symbol, no suffix.
    {"chainId": 4663, "address": FAKE_GME, "name": "GameStop", "symbol": "GME", "decimals": 18},
    {"chainId": 4663, "address": "0xca9c", "name": "NetNet", "symbol": "NET", "decimals": 18},
    {
        "chainId": 4663,
        "address": "0x116f",
        "name": "Cloudflare, Inc. • Robinhood Token",
        "symbol": "NET",
        "decimals": 18,
    },
]

_FEEDS: list[dict[str, Any]] = [
    {
        "name": "Robinhood AAPL / USD",
        "proxyAddress": "0x6B22A786bAa607d76728168703a39Ea9C99f2cD0",
        "heartbeat": 86400,
        "threshold": 0.5,
        "docs": {"baseAsset": "AAPL"},
    },
    # Real feed names are not uniform; these two shapes both occur upstream.
    {"name": "Robinhood DELL-USD", "proxyAddress": "0xdeLL", "docs": {}},
    {"name": "ETH / USD", "proxyAddress": "0xeth", "docs": {"baseAsset": "ETH"}},
]


def _word_hex(value: int) -> str:
    return f"{value & ((1 << 256) - 1):064x}"


class _FakeChain:
    """Minimal eth_call stand-in keyed by (address, selector)."""

    def __init__(self, responses: dict[tuple[str, str], str]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def __call__(self, _rpc: str, to: str, data: str, _timeout: float) -> str:
        key = (to.lower(), data[:10])
        self.calls.append(key)
        try:
            return self.responses[key]
        except KeyError:
            raise chain_stocks.RpcError("execution reverted") from None


# --- ABI decoding -----------------------------------------------------------


def test_decode_string_reads_dynamic_return() -> None:
    blob = "0x" + _word_hex(32) + _word_hex(4) + b"AAPL".hex().ljust(64, "0")
    assert chain_stocks._decode_string(blob) == "AAPL"


def test_word_signed_handles_negative_answers() -> None:
    assert chain_stocks._word_signed("0x" + _word_hex(-1), 0) == -1
    assert chain_stocks._word_signed("0x" + _word_hex(31747461437), 0) == 31747461437


def test_word_rejects_short_result() -> None:
    with pytest.raises(chain_stocks.RpcError):
        chain_stocks._word("0x1234", 0)


def test_encode_address_arg_pads_to_a_full_word() -> None:
    encoded = chain_stocks._encode_address_arg(chain_stocks.SEL_BALANCE_OF, AAPL)
    assert encoded.startswith(chain_stocks.SEL_BALANCE_OF)
    assert len(encoded) == 10 + 64
    assert encoded.endswith(AAPL[2:])


# --- impersonation guard ----------------------------------------------------


def test_is_stock_token_keys_off_the_suffix() -> None:
    assert chain_stocks.is_stock_token(_TOKENS[1]) is True
    assert chain_stocks.is_stock_token(_TOKENS[2]) is False


def test_resolve_token_never_returns_an_impersonator() -> None:
    for query in ("GME", "GameStop", "mã cổ phiếu GME là gì"):
        match = chain_stocks.resolve_token(query, _TOKENS)
        assert match is not None, query
        assert match["address"] == REAL_GME, query


def test_resolve_token_prefers_the_listing_over_a_lookalike_symbol() -> None:
    match = chain_stocks.resolve_token("NET", _TOKENS)
    assert match is not None
    assert match["name"].startswith("Cloudflare")


def test_resolve_token_returns_none_when_nothing_matches() -> None:
    assert chain_stocks.resolve_token("zzz-not-a-company", _TOKENS) is None


# --- Chainlink feed mapping -------------------------------------------------


def test_feed_ticker_prefers_base_asset_then_parses_the_name() -> None:
    assert chain_stocks.feed_ticker(_FEEDS[0]) == "AAPL"
    assert chain_stocks.feed_ticker(_FEEDS[1]) == "DELL"
    assert chain_stocks.feed_ticker({"name": "Robinhood SPY / USD", "docs": {}}) == "SPY"


def test_feed_ticker_ignores_non_robinhood_feeds_without_base_asset() -> None:
    assert chain_stocks.feed_ticker({"name": "USDC / USD", "docs": {}}) == ""


def test_find_feed_matches_by_ticker_only() -> None:
    assert chain_stocks.find_feed("AAPL", _FEEDS) is _FEEDS[0]
    assert chain_stocks.find_feed("TSLA", _FEEDS) is None


# --- on-chain inspection ----------------------------------------------------


def test_inspect_token_reads_price_multiplier_and_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _FEEDS[0]
    proxy = str(feed["proxyAddress"]).lower()
    round_data = "0x" + "".join(_word_hex(v) for v in (1, 31747461437, 1788206000, 1788206389, 1))
    chain = _FakeChain(
        {
            (AAPL, chain_stocks.SEL_SYMBOL): "0x"
            + _word_hex(32)
            + _word_hex(4)
            + b"AAPL".hex().ljust(64, "0"),
            (AAPL, chain_stocks.SEL_DECIMALS): "0x" + _word_hex(18),
            (AAPL, chain_stocks.SEL_TOTAL_SUPPLY): "0x" + _word_hex(10301407498610000000000),
            (AAPL, chain_stocks.SEL_UI_MULTIPLIER): "0x" + _word_hex(1000566080061092436),
            (AAPL, chain_stocks.SEL_ORACLE_PAUSED): "0x" + _word_hex(0),
            (AAPL, chain_stocks.SEL_BALANCE_OF): "0x" + _word_hex(2 * 10**18),
            (proxy, chain_stocks.SEL_LATEST_ROUND_DATA): round_data,
            (proxy, chain_stocks.SEL_DECIMALS): "0x" + _word_hex(8),
        }
    )
    monkeypatch.setattr(chain_stocks, "_eth_call", chain)

    # `now` is pinned so age/staleness never depend on the wall clock.
    state = chain_stocks.inspect_token(
        "rpc", AAPL, 5.0, holder=AAPL, feed=feed, now=1788206389 + 600
    )

    assert state["isStockToken"] is True
    assert state["onchainSymbol"] == "AAPL"
    assert state["oraclePaused"] is False
    assert state["uiMultiplierFormatted"] == pytest.approx(1.000566080061092)
    assert state["price"]["usd"] == pytest.approx(317.47461437)
    assert state["price"]["updatedAt"] == 1788206389
    assert state["price"]["ageSeconds"] == 600
    assert state["price"]["stale"] is False
    assert state["price"]["heartbeatSeconds"] == 86400
    assert state["holding"]["balanceFormatted"] == pytest.approx(2.0)
    assert state["holding"]["valueUsd"] == pytest.approx(634.94922874)
    assert "readErrors" not in state


def test_inspect_token_flags_a_token_that_cannot_answer_ui_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Everything except the ERC-8056 marker resolves -- exactly how an
    # impersonating ERC-20 presents itself.
    chain = _FakeChain(
        {
            (FAKE_GME, chain_stocks.SEL_SYMBOL): "0x"
            + _word_hex(32)
            + _word_hex(3)
            + b"GME".hex().ljust(64, "0"),
            (FAKE_GME, chain_stocks.SEL_DECIMALS): "0x" + _word_hex(18),
            (FAKE_GME, chain_stocks.SEL_TOTAL_SUPPLY): "0x" + _word_hex(10**24),
        }
    )
    monkeypatch.setattr(chain_stocks, "_eth_call", chain)

    state = chain_stocks.inspect_token("rpc", FAKE_GME, 5.0)

    assert state["isStockToken"] is False
    assert "uiMultiplier" not in state
    assert "uiMultiplier" in state["readErrors"]


def test_address_only_run_resolves_its_feed_from_the_onchain_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--address` skips name resolution, so the feed must come from `symbol()`.

    Otherwise a run that never looked up a feed reports "no feed published for
    this token" -- asserting a fact it never checked.
    """
    proxy = str(_FEEDS[0]["proxyAddress"]).lower()
    chain = _FakeChain(
        {
            (AAPL, chain_stocks.SEL_SYMBOL): "0x"
            + _word_hex(32)
            + _word_hex(4)
            + b"AAPL".hex().ljust(64, "0"),
            (AAPL, chain_stocks.SEL_UI_MULTIPLIER): "0x" + _word_hex(10**18),
            (proxy, chain_stocks.SEL_LATEST_ROUND_DATA): "0x"
            + "".join(_word_hex(v) for v in (1, 31747461437, 0, 1788206389, 1)),
            (proxy, chain_stocks.SEL_DECIMALS): "0x" + _word_hex(8),
        }
    )
    monkeypatch.setattr(chain_stocks, "_eth_call", chain)

    # No `feed=` passed, exactly as an --address run does.
    state = chain_stocks.inspect_token("rpc", AAPL, 5.0, feeds=_FEEDS, now=1788206389 + 60)

    assert state["price"]["usd"] == pytest.approx(317.47461437)
    assert state["price"]["feedAddress"] == _FEEDS[0]["proxyAddress"]


def test_feed_lookup_is_skipped_when_the_symbol_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chain_stocks, "_eth_call", _FakeChain({}))
    state = chain_stocks.inspect_token("rpc", AAPL, 5.0, feeds=_FEEDS)
    assert "price" not in state


def _price_chain(answer: int, updated_at: int, paused: int = 0) -> _FakeChain:
    proxy = str(_FEEDS[0]["proxyAddress"]).lower()
    return _FakeChain(
        {
            (AAPL, chain_stocks.SEL_SYMBOL): "0x"
            + _word_hex(32) + _word_hex(4) + b"AAPL".hex().ljust(64, "0"),
            (AAPL, chain_stocks.SEL_DECIMALS): "0x" + _word_hex(18),
            (AAPL, chain_stocks.SEL_TOTAL_SUPPLY): "0x" + _word_hex(10301407498610000000000),
            (AAPL, chain_stocks.SEL_UI_MULTIPLIER): "0x" + _word_hex(1),
            (AAPL, chain_stocks.SEL_ORACLE_PAUSED): "0x" + _word_hex(paused),
            (proxy, chain_stocks.SEL_LATEST_ROUND_DATA): "0x"
            + "".join(_word_hex(v) for v in (1, answer, 0, updated_at, 1)),
            (proxy, chain_stocks.SEL_DECIMALS): "0x" + _word_hex(8),
        }
    )


def test_price_age_is_computed_and_fresh_answers_are_not_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chain_stocks, "_eth_call", _price_chain(31747461437, 1_000_000))
    price = chain_stocks.inspect_token("rpc", AAPL, 5.0, feed=_FEEDS[0], now=1_000_600.0)["price"]
    assert price["ageSeconds"] == 600
    assert price["stale"] is False


def test_answer_older_than_the_heartbeat_is_flagged_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chain_stocks, "_eth_call", _price_chain(31747461437, 1_000_000))
    # heartbeat is 86400; 90000s old is past it.
    price = chain_stocks.inspect_token("rpc", AAPL, 5.0, feed=_FEEDS[0], now=1_090_000.0)["price"]
    assert price["ageSeconds"] == 90_000
    assert price["stale"] is True


def test_paused_oracle_marks_the_price_stale_even_when_recent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chain_stocks, "_eth_call", _price_chain(31747461437, 1_000_000, paused=1))
    state = chain_stocks.inspect_token("rpc", AAPL, 5.0, feed=_FEEDS[0], now=1_000_060.0)
    assert state["oraclePaused"] is True
    assert state["price"]["stale"] is True


def test_non_positive_answer_is_not_reported_as_a_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chain_stocks, "_eth_call", _price_chain(0, 1_000_000))
    price = chain_stocks.inspect_token("rpc", AAPL, 5.0, feed=_FEEDS[0], now=1_000_060.0)["price"]
    assert price["usd"] is None
    assert price["unusableAnswer"] is True


def test_inspect_token_reports_a_paused_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = _FakeChain({(AAPL, chain_stocks.SEL_ORACLE_PAUSED): "0x" + _word_hex(1)})
    monkeypatch.setattr(chain_stocks, "_eth_call", chain)
    assert chain_stocks.inspect_token("rpc", AAPL, 5.0)["oraclePaused"] is True


def test_inspect_token_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chain_stocks, "_eth_call", _FakeChain({}))
    state = chain_stocks.inspect_token("rpc", AAPL, 5.0, feed=_FEEDS[0])
    assert state["isStockToken"] is False
    # Price is not attempted for proven impersonators (#866)
    assert set(state["readErrors"]) >= {"symbol", "decimals", "totalSupply", "uiMultiplier"}


def test_unreachable_node_leaves_stock_status_unknown_not_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead RPC must never be reported as proof that a real token is fake.

    `isStockToken: false` tells the caller to refuse the address, so a network
    fault resolving to `false` would brand a genuine listing an impersonator.
    """

    def _dead(_rpc: str, _to: str, _data: str, _timeout: float) -> str:
        raise OSError("connection refused")

    monkeypatch.setattr(chain_stocks, "_eth_call", _dead)
    state = chain_stocks.inspect_token("rpc", AAPL, 5.0)

    assert state["isStockToken"] is None
    assert "uiMultiplier" in state["readErrors"]


def test_reverting_contract_is_still_reported_as_not_a_stock_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chain_stocks, "_eth_call", _FakeChain({}))
    assert chain_stocks.inspect_token("rpc", FAKE_GME, 5.0)["isStockToken"] is False


# --- skill packaging --------------------------------------------------------


def test_chain_stocks_skill_loads() -> None:
    skill = SkillLoader(bundled_dir=BUNDLED).get_by_name("robinhood-chain-stocks")
    assert skill is not None
    assert skill.provenance.origin == "agentos-original"
    assert skill.provenance.maintained_by == "AgentOS"


def test_skill_documents_the_read_only_boundary() -> None:
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "read-only" in body.lower()
    assert "uiMultiplier()" in body
    assert "4663" in body


# ---------------------------------------------------------------------------
# #815/#816: --rpc-url validation + non-dict RPC error handling
# ---------------------------------------------------------------------------


def test_validate_http_url_rejects_non_http_schemes() -> None:
    """file://, ftp://, gopher://, javascript: are all rejected."""
    for invalid in [
        "file:///etc/passwd",
        "file:///c:/windows/system32/drivers/etc/hosts",
        "ftp://rpc.example.com",
        "gopher://example.com",
        "javascript:alert(1)",
    ]:
        with pytest.raises(ValueError, match="must be http:// or https://"):
            chain_stocks._validate_http_url(invalid)
    for empty in ["", "   "]:
        with pytest.raises(ValueError, match="empty URL"):
            chain_stocks._validate_http_url(empty)
    assert chain_stocks._validate_http_url("http://127.0.0.1:8545") == "http://127.0.0.1:8545"
    assert (
        chain_stocks._validate_http_url("https://rpc.mainnet.chain.robinhood.com")
        == "https://rpc.mainnet.chain.robinhood.com"
    )


def test_validate_http_url_rejects_missing_host() -> None:
    with pytest.raises(ValueError, match="missing host"):
        chain_stocks._validate_http_url("http://")


def test_validate_http_url_accepts_valid_variants() -> None:
    assert chain_stocks._validate_http_url("http://example.com") == "http://example.com"
    assert (
        chain_stocks._validate_http_url("https://user:pass@host.com:8545")
        == "https://user:pass@host.com:8545"
    )
    assert chain_stocks._validate_http_url("http://[::1]:7545") == "http://[::1]:7545"
    assert chain_stocks._validate_http_url(chain_stocks.DEFAULT_RPC_URL)


def test_http_json_rejects_file_scheme() -> None:
    with pytest.raises(ValueError, match="must be http:// or https://"):
        chain_stocks._http_json("file:///etc/passwd", timeout=5.0)


def test_http_json_rejects_empty_url() -> None:
    with pytest.raises(ValueError, match="empty URL"):
        chain_stocks._http_json("", timeout=5.0)


def test_main_rejects_invalid_rpc_url(capsys: pytest.CaptureFixture[str]) -> None:
    import json
    code = chain_stocks.main(["--address",
        "0x0000000000000000000000000000000000000000",
        "--rpc-url", "file:///etc/passwd"])
    assert code == 0
    out, _ = capsys.readouterr()
    payload = json.loads(out)
    assert "invalid rpc-url" in payload.get("error", "")
    assert "must be http:// or https://" in payload.get("error", "")


def test_main_rejects_missing_host_url(capsys: pytest.CaptureFixture[str]) -> None:
    import json
    code = chain_stocks.main(["--address",
        "0x0000000000000000000000000000000000000000",
        "--rpc-url", "http://"])
    assert code == 0
    out, _ = capsys.readouterr()
    payload = json.loads(out)
    assert "missing host" in payload.get("error", "")


def test_eth_call_handle_nondict_error() -> None:
    """#816: a string RPC error no longer crashes _eth_call."""
    from unittest.mock import patch

    def mock_http_json(*args, **kwargs):
        return {"error": "something went wrong"}

    with patch.object(chain_stocks, "_http_json", side_effect=mock_http_json):
        with pytest.raises(chain_stocks.RpcError) as exc:
            chain_stocks._eth_call("http://127.0.0.1:8545",
                "0x0000000000000000000000000000000000000000", "0x", 5.0)
        assert "something went wrong" in str(exc.value)


def test_eth_call_handle_dict_error() -> None:
    """#816: a dict RPC error is handled the same as before."""
    from unittest.mock import patch

    def mock_http_json(*args, **kwargs):
        return {"error": {"message": "execution reverted"}}

    with patch.object(chain_stocks, "_http_json", side_effect=mock_http_json):
        with pytest.raises(chain_stocks.RpcError) as exc:
            chain_stocks._eth_call("http://127.0.0.1:8545",
                "0x0000000000000000000000000000000000000000", "0x", 5.0)
        assert "execution reverted" in str(exc.value)


# ---------------------------------------------------------------------------
# #866: inspect_token must not attach price to proven impersonators
# ---------------------------------------------------------------------------


_FAKE_ADDR = "0xdeadbeef00000000000000000000000000dead"
_FAKE_FEED_PROXY = "0x1111111111111111111111111111111111111111"
_GME_FEEDS = [{
    "name": "Robinhood GME / USD",
    "proxyAddress": _FAKE_FEED_PROXY,
    "heartbeat": 86400,
    "threshold": 0.5,
    "docs": {"baseAsset": "GME"},
}]

# ABI-encoded "GME" string (dynamic bytes):
# offset(32) + length(32) + "GME".hex() padded
_GME_SYMBOL_RESULT = (
    "0x"
    + (32).to_bytes(32, "big").hex()
    + (3).to_bytes(32, "big").hex()
    + "474d4500000000000000000000000000000000000000000000000000000000"
)

# ABI-encoded "AAPL" string
_AAPL_SYMBOL_RESULT = (
    "0x"
    + (32).to_bytes(32, "big").hex()
    + (4).to_bytes(32, "big").hex()
    + "4141504c00000000000000000000000000000000000000000000000000000000"
)

# ABI-encoded uint256(1) for uiMultiplier
_MULTIPLIER_UNIT = "0x" + (1).to_bytes(32, "big").hex()


def _fake_eth_call_impersonator_returns_real_ticker(
    rpc_url: str, to: str, data: str, timeout: float
) -> str:
    """Mock _eth_call for an impersonator that passes symbol()
    but reverts on uiMultiplier().
    """
    if to == _FAKE_ADDR and data == chain_stocks.SEL_SYMBOL:
        return _GME_SYMBOL_RESULT
    if to == _FAKE_ADDR and data == chain_stocks.SEL_DECIMALS:
        return "0x" + (18).to_bytes(32, "big").hex()
    if to == _FAKE_ADDR and data == chain_stocks.SEL_TOTAL_SUPPLY:
        return "0x" + (1_000_000 * 10**18).to_bytes(32, "big").hex()
    if to == _FAKE_ADDR and data == chain_stocks.SEL_UI_MULTIPLIER:
        raise chain_stocks.RpcError("execution reverted")
    if to == _FAKE_ADDR and data == chain_stocks.SEL_ORACLE_PAUSED:
        return "0x" + (0).to_bytes(32, "big").hex()
    if to == _FAKE_FEED_PROXY and data == chain_stocks.SEL_LATEST_ROUND_DATA:
        now = int(__import__("time").time())
        return "0x" + (
            (1).to_bytes(32, "big").hex()
            + (250000000000).to_bytes(32, "big").hex()
            + (1).to_bytes(32, "big").hex()
            + (now).to_bytes(32, "big").hex()
            + (1).to_bytes(32, "big").hex()
        )
    if to == _FAKE_FEED_PROXY and data == chain_stocks.SEL_DECIMALS:
        return "0x" + (8).to_bytes(32, "big").hex()
    raise NotImplementedError(f"unexpected call to={to} data={data}")


def test_inspect_token_no_price_for_impersonator() -> None:
    """#866: An impersonator (isStockToken=False) must not get a price,
    even if its symbol() matches a real company ticker.
    """
    with patch.object(chain_stocks, "_eth_call",
                      side_effect=_fake_eth_call_impersonator_returns_real_ticker):
        result = chain_stocks.inspect_token(
            "http://127.0.0.1:8545", _FAKE_ADDR, 5.0, feeds=_GME_FEEDS)
    assert result["isStockToken"] is False, "impersonator must be flagged"
    assert "price" not in result, (
        "impersonator with isStockToken=False must not get a real price"
    )


def test_inspect_token_has_price_for_genuine_token() -> None:
    """#866: A genuine Stock Token (isStockToken=True) still gets its price."""
    def mock_eth_call(rpc_url, to, data, timeout):
        if to == _FAKE_ADDR and data == chain_stocks.SEL_SYMBOL:
            return _GME_SYMBOL_RESULT
        if to == _FAKE_ADDR and data == chain_stocks.SEL_UI_MULTIPLIER:
            return _MULTIPLIER_UNIT  # proven Stock Token
        if to == _FAKE_ADDR and data == chain_stocks.SEL_DECIMALS:
            return "0x" + (18).to_bytes(32, "big").hex()
        if to == _FAKE_ADDR and data == chain_stocks.SEL_TOTAL_SUPPLY:
            return "0x" + (1_000_000 * 10**18).to_bytes(32, "big").hex()
        if to == _FAKE_ADDR and data == chain_stocks.SEL_ORACLE_PAUSED:
            return "0x" + (0).to_bytes(32, "big").hex()
        if to == _FAKE_FEED_PROXY and data == chain_stocks.SEL_LATEST_ROUND_DATA:
            now = int(__import__("time").time())
            return "0x" + (
                (1).to_bytes(32, "big").hex()
                + (250000000000).to_bytes(32, "big").hex()
                + (1).to_bytes(32, "big").hex()
                + (now).to_bytes(32, "big").hex()
                + (1).to_bytes(32, "big").hex()
            )
        if to == _FAKE_FEED_PROXY and data == chain_stocks.SEL_DECIMALS:
            return "0x" + (8).to_bytes(32, "big").hex()
        raise NotImplementedError(f"unexpected to={to} data={data}")

    with patch.object(chain_stocks, "_eth_call", side_effect=mock_eth_call):
        result = chain_stocks.inspect_token(
            "http://127.0.0.1:8545", _FAKE_ADDR, 5.0, feeds=_GME_FEEDS)
    assert result["isStockToken"] is True
    assert "price" in result, "genuine Stock Token must have price"
    assert result["price"]["usd"] is not None


def test_inspect_token_no_price_when_unverified() -> None:
    """#866: When isStockToken is None (node unreachable), no price
    should be attached — the caller cannot know.
    """
    def mock_eth_call_unreachable(rpc_url, to, data, timeout):
        if to == _FAKE_ADDR and data == chain_stocks.SEL_SYMBOL:
            return _GME_SYMBOL_RESULT
        if to == _FAKE_ADDR and data == chain_stocks.SEL_DECIMALS:
            return "0x" + (18).to_bytes(32, "big").hex()
        if to == _FAKE_ADDR and data == chain_stocks.SEL_TOTAL_SUPPLY:
            return "0x" + (1_000_000 * 10**18).to_bytes(32, "big").hex()
        if to == _FAKE_ADDR and data == chain_stocks.SEL_UI_MULTIPLIER:
            raise chain_stocks.RpcError("execution reverted")
        if to == _FAKE_ADDR and data == chain_stocks.SEL_ORACLE_PAUSED:
            return "0x" + (0).to_bytes(32, "big").hex()
        raise NotImplementedError(f"unexpected to={to} data={data}")

    with patch.object(chain_stocks, "_eth_call", side_effect=mock_eth_call_unreachable):
        result = chain_stocks.inspect_token(
            "http://127.0.0.1:8545", _FAKE_ADDR, 5.0, feeds=_GME_FEEDS)
    assert result["isStockToken"] is False
    assert "price" not in result, "unverified must not have price"


def test_inspect_token_price_skipped_when_feed_explicitly_passed() -> None:
    """#866: When a feed IS passed explicitly AND the token is an
    impersonator, the price must still be suppressed. The guard
    must be in inspect_token itself, not just in the auto-resolve path.
    """
    with patch.object(chain_stocks, "_eth_call",
                      side_effect=_fake_eth_call_impersonator_returns_real_ticker):
        result = chain_stocks.inspect_token(
            "http://127.0.0.1:8545", _FAKE_ADDR, 5.0,
            feed=_GME_FEEDS[0], feeds=_GME_FEEDS)
    assert result["isStockToken"] is False
    assert "price" not in result, "impersonator must not get price even with explicit feed"


def test_inspect_token_price_for_genuine_token_with_explicit_feed() -> None:
    """#866: A genuine token with an explicitly passed feed still gets price."""
    def mock_true_token(rpc_url, to, data, timeout):
        if to == _FAKE_ADDR and data == chain_stocks.SEL_UI_MULTIPLIER:
            return _MULTIPLIER_UNIT
        if to == _FAKE_ADDR and data == chain_stocks.SEL_SYMBOL:
            return _GME_SYMBOL_RESULT
        if to == _FAKE_ADDR and data == chain_stocks.SEL_DECIMALS:
            return "0x" + (18).to_bytes(32, "big").hex()
        if to == _FAKE_ADDR and data == chain_stocks.SEL_TOTAL_SUPPLY:
            return "0x" + (1_000_000 * 10**18).to_bytes(32, "big").hex()
        if to == _FAKE_ADDR and data == chain_stocks.SEL_ORACLE_PAUSED:
            return "0x" + (0).to_bytes(32, "big").hex()
        if to == _FAKE_FEED_PROXY and data == chain_stocks.SEL_LATEST_ROUND_DATA:
            now = int(__import__("time").time())
            return "0x" + (
                (1).to_bytes(32, "big").hex()
                + (250000000000).to_bytes(32, "big").hex()
                + (1).to_bytes(32, "big").hex()
                + (now).to_bytes(32, "big").hex()
                + (1).to_bytes(32, "big").hex()
            )
        if to == _FAKE_FEED_PROXY and data == chain_stocks.SEL_DECIMALS:
            return "0x" + (8).to_bytes(32, "big").hex()
        raise NotImplementedError(f"unexpected to={to} data={data}")

    with patch.object(chain_stocks, "_eth_call", side_effect=mock_true_token):
        result = chain_stocks.inspect_token(
            "http://127.0.0.1:8545", _FAKE_ADDR, 5.0,
            feed=_GME_FEEDS[0], feeds=_GME_FEEDS)
    assert result["isStockToken"] is True
    assert "price" in result, "genuine token with explicit feed must have price"


def test_find_feed_not_interfered_by_guard() -> None:
    """#866: find_feed itself is not affected — only inspect_token.
    This test verifies find_feed still resolves a ticker.
    """
    feed = chain_stocks.find_feed("GME", _GME_FEEDS)
    assert feed is not None
    assert feed["proxyAddress"] == _FAKE_FEED_PROXY
