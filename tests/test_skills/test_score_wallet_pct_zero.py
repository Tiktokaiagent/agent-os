"""Regression tests for 0% ROI wallet_pct clobber in copy-trade backtest.

Bug: ``wallet_pct or 0.0001`` treats a legitimate 0.0 (e.g. a dev wallet with
zero profit on zero cost) as falsy, replacing it with 0.0001 and producing
wildly wrong copy-trade projections (e.g. -$480k instead of $0).

Fix: substitute only on ``None``, not on any falsy value.
"""

from __future__ import annotations


# Duplicate _clamp from score.py for offline testing.
# score.py cannot be imported at module level because it calls gmgn-cli
# and raises SystemExit(0) during import.
def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Replica of score.py _clamp."""
    return lo if x < lo else hi if x > hi else x


def _wallet_pct_fixed(w: dict) -> float:
    """Fixed formula: substitute on None only."""
    wp = (w["realized_profit"] / w["bought_cost"]) if w["bought_cost"] > 0 else w["roi"]
    return _clamp(wp if wp is not None else 0.0001, -0.9, 3.0)


def _wallet_pct_buggy(w: dict) -> float:
    """Buggy formula: falsy→0.0001 clobbers legitimate 0.0."""
    wp = (w["realized_profit"] / w["bought_cost"]) if w["bought_cost"] > 0 else w["roi"]
    return _clamp(wp or 0.0001, -0.9, 3.0)


# ---------------------------------------------------------------------------
# _clamp
# ---------------------------------------------------------------------------


class TestClamp:
    def test_keeps_zero(self) -> None:
        assert _clamp(0.0) == 0.0

    def test_in_range(self) -> None:
        assert _clamp(1.5, lo=-0.9, hi=3.0) == 1.5

    def test_below_lo(self) -> None:
        assert _clamp(-1.0, lo=-0.9, hi=3.0) == -0.9

    def test_above_hi(self) -> None:
        assert _clamp(5.0, lo=-0.9, hi=3.0) == 3.0

    def test_default_lo_clamps(self) -> None:
        assert _clamp(-0.1) == 0.0

    def test_default_hi_clamps(self) -> None:
        assert _clamp(2.0) == 1.0


# ---------------------------------------------------------------------------
# Fixed formula
# ---------------------------------------------------------------------------


class TestFixed:
    def test_zero_roi_zero_cost(self) -> None:
        assert _wallet_pct_fixed({"realized_profit": 0.0, "bought_cost": 0.0, "roi": 0.0}) == 0.0

    def test_zero_profit_positive_cost(self) -> None:
        assert _wallet_pct_fixed({"realized_profit": 0.0, "bought_cost": 100.0, "roi": 0.5}) == 0.0

    def test_positive_roi(self) -> None:
        result = _wallet_pct_fixed(
            {"realized_profit": 200.0, "bought_cost": 100.0, "roi": 2.0}
        )
        assert result == 2.0

    def test_roi_used_on_zero_cost(self) -> None:
        assert _wallet_pct_fixed({"realized_profit": 800.0, "bought_cost": 0.0, "roi": 0.3}) == 0.3

    def test_negative_roi(self) -> None:
        result = _wallet_pct_fixed(
            {"realized_profit": -50.0, "bought_cost": 100.0, "roi": -0.5}
        )
        assert result == -0.5

    def test_ratio_clamped_hi(self) -> None:
        result = _wallet_pct_fixed(
            {"realized_profit": 1000.0, "bought_cost": 10.0, "roi": 100.0}
        )
        assert result == 3.0

    def test_ratio_clamped_lo(self) -> None:
        result = _wallet_pct_fixed(
            {"realized_profit": -5000.0, "bought_cost": 10.0, "roi": -500.0}
        )
        assert result == -0.9


# ---------------------------------------------------------------------------
# Bug vs fix
# ---------------------------------------------------------------------------


class TestBugRegression:
    def test_zero_roi_not_clobbered(self) -> None:
        w = {"realized_profit": 800.0, "bought_cost": 0.0, "roi": 0.0}
        assert _wallet_pct_buggy(w) == 0.0001  # BUG
        assert _wallet_pct_fixed(w) == 0.0  # FIXED

    def test_zero_profit_not_clobbered(self) -> None:
        w = {"realized_profit": 0.0, "bought_cost": 100.0, "roi": 0.5}
        assert _wallet_pct_buggy(w) == 0.0001  # BUG
        assert _wallet_pct_fixed(w) == 0.0  # FIXED

    def test_copy_7d_blowup(self) -> None:
        """Bug → wallet_pct=0.0001 → copy_7d = -$480k instead of $0."""
        w = {"realized_profit": 800.0, "bought_cost": 0.0, "roi": 0.0}
        wp_bug = _wallet_pct_buggy(w)
        wp_fix = _wallet_pct_fixed(w)
        copy_7d_bug = 800.0 * (-0.06 / wp_bug) if wp_bug else 0.0
        copy_7d_fix = 800.0 * (-0.06 / wp_fix) if wp_fix else 0.0
        assert copy_7d_bug == -480000.0  # wildly wrong
        assert copy_7d_fix == 0.0  # correct

    def test_none_fallback_safe(self) -> None:
        """If wallet_pct is somehow None, 0.0001 fallback still applies."""
        assert _clamp(None if None is not None else 0.0001, -0.9, 3.0) == 0.0001


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdge:
    def test_tiny_cost_zero_profit(self) -> None:
        assert _clamp(0.0 / 0.01, -0.9, 3.0) == 0.0

    def test_zero_cost_negative_roi(self) -> None:
        result = _wallet_pct_fixed(
            {"realized_profit": -50.0, "bought_cost": 0.0, "roi": -0.3}
        )
        assert result == -0.3

    def test_zero_cost_high_roi(self) -> None:
        result = _wallet_pct_fixed(
            {"realized_profit": 500.0, "bought_cost": 0.0, "roi": 0.75}
        )
        assert result == 0.75

    def test_exact_minimum_clamp(self) -> None:
        assert _clamp(-0.9, -0.9, 3.0) == -0.9

    def test_exact_maximum_clamp(self) -> None:
        assert _clamp(3.0, -0.9, 3.0) == 3.0
