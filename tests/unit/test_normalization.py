"""Normalization tests (FINAL_DELIVERABLE §K, §L; acceptance test 3).

Covers: quote/contract normalization, the 1500 ms drift budget, cost model, basis and
net-basis arithmetic, and the fail-closed classification when history is insufficient.
"""

from __future__ import annotations

import pytest

from app.core.errors import ClockDriftError, DataUnavailableError, NormalizationError
from app.core.models import DivergenceClass, InstrumentSpec, OrderBook
from app.data.normalization import (
    BasisHistory,
    Normalizer,
    QuoteConverter,
    classify_divergence,
    effective_cost_bps,
    normalize_contract_price,
)
from tests.conftest import TICK, book


def _spec(**overrides) -> InstrumentSpec:
    base = dict(
        pair="B-BTC_USDT",
        binance_symbol="BTCUSDT",
        price_increment=TICK,
        quantity_increment=1.0,
        min_trade_size=1.0,
        min_notional=6.0,
        maker_fee_pct=0.0236,
        taker_fee_pct=0.059,
        funding_frequency=4,
    )
    base.update(overrides)
    return InstrumentSpec(**base)  # type: ignore[arg-type]


def test_contract_normalization_linear_and_inverse():
    assert normalize_contract_price(100.0, _spec()) == pytest.approx(100.0)
    assert normalize_contract_price(100.0, _spec(unit_contract_value=10.0)) == pytest.approx(1000.0)
    inverse = _spec(inverse=True, unit_contract_value=100.0, quanto_multiplier=1.0)
    assert normalize_contract_price(50.0, inverse) == pytest.approx(2.0)


def test_effective_cost_matches_verified_coindcx_fees():
    # 2 x taker 0.059 % = 11.8 bps, plus 1.5 bps spread plus 3 bps slippage
    cost = effective_cost_bps(taker_fee_pct=0.059, spread_bps=1.5, slippage_bps=3.0)
    assert cost == pytest.approx(16.3, abs=1e-9)


def test_inr_quote_fails_closed_without_a_verified_fx_source():
    with pytest.raises(DataUnavailableError):
        QuoteConverter(usdt_inr=None).to_usd(1000.0, "INR")


def test_unsupported_quote_raises():
    with pytest.raises(NormalizationError):
        QuoteConverter().to_usd(100.0, "EUR")


def test_basis_and_net_basis_arithmetic():
    normalizer = Normalizer(min_history_obs=1)
    binance = book(86_000.0, venue="BINANCE", ts_ms=1_700_000_000_000)
    coindcx = book(86_043.0, venue="COINDCX", ts_ms=1_700_000_000_010)
    snap = normalizer.basis(binance_book=binance, coindcx_book=coindcx, spec=_spec())
    # mid includes the book's own bid/ask offset identically on both venues, so the
    # basis is driven by the price difference only
    expected_bps = (coindcx.mid - binance.mid) / binance.mid * 1e4
    assert snap.basis_bps == pytest.approx(expected_bps, rel=1e-9)
    assert snap.net_basis_bps == pytest.approx(snap.basis_bps - snap.effective_cost_bps)
    assert snap.drift_ms == 10


def test_clock_drift_beyond_budget_raises():
    normalizer = Normalizer(max_clock_drift_ms=1500, min_history_obs=1)
    binance = book(86_000.0, venue="BINANCE", ts_ms=1_700_000_000_000)
    coindcx = book(86_000.0, venue="COINDCX", ts_ms=1_700_000_002_000)  # 2 s later
    with pytest.raises(ClockDriftError):
        normalizer.basis(binance_book=binance, coindcx_book=coindcx, spec=_spec())


def test_try_basis_returns_none_instead_of_raising():
    normalizer = Normalizer(max_clock_drift_ms=1500, min_history_obs=1)
    binance = book(86_000.0, venue="BINANCE", ts_ms=1_700_000_000_000)
    coindcx = book(86_000.0, venue="COINDCX", ts_ms=1_700_000_005_000)
    assert normalizer.try_basis(binance_book=binance, coindcx_book=coindcx, spec=_spec()) is None
    assert normalizer.last_error


def test_insufficient_history_is_abnormal_never_normal():
    classified = classify_divergence(
        0.1,
        observations=10,
        min_obs=60,
        bands={"normal": 1.0, "elevated": 2.0, "abnormal": 3.0, "extreme": 3.0},
    )
    assert classified is DivergenceClass.ABNORMAL
    assert (
        classify_divergence(
            None,
            observations=500,
            min_obs=60,
            bands={"normal": 1.0, "elevated": 2.0, "abnormal": 3.0, "extreme": 3.0},
        )
        is DivergenceClass.ABNORMAL
    )


@pytest.mark.parametrize(
    "z,expected",
    [
        (0.4, DivergenceClass.NORMAL),
        (1.5, DivergenceClass.ELEVATED),
        (2.5, DivergenceClass.ABNORMAL),
        (3.4, DivergenceClass.EXTREME),
        (-3.4, DivergenceClass.EXTREME),
    ],
)
def test_divergence_bands(z, expected, cfg):
    """Spec §L bands: NORMAL < 1.0, ELEVATED 1-2, ABNORMAL 2-3, EXTREME >= 3.0."""
    bands = {
        "normal": float(cfg.normalization.divergence_bands.get("normal", 1.0)),
        "elevated": float(cfg.normalization.divergence_bands.get("elevated", 1.0)),
        "abnormal": float(cfg.normalization.divergence_bands.get("abnormal", 2.0)),
        "extreme": float(cfg.normalization.divergence_bands.get("extreme", 3.0)),
    }
    assert classify_divergence(z, observations=500, min_obs=60, bands=bands) is expected


def test_basis_history_reports_z_percentile_and_vol_adjustment():
    history = BasisHistory()
    for value in range(1, 101):
        history.push(float(value % 7))
    history.push(50.0)
    stats = history.stats(realized_vol=0.4)
    assert stats["z"] is not None and stats["percentile"] is not None
    assert stats["vol_adjusted"] == pytest.approx(stats["z"] / 0.4)


def test_crossed_or_empty_book_rejected():
    normalizer = Normalizer(min_history_obs=1)
    good: OrderBook = book(100.0, venue="COINDCX")
    empty = OrderBook(venue="BINANCE", symbol="BTCUSDT", ts_ms=good.ts_ms, bids=[], asks=[])
    with pytest.raises(NormalizationError):
        normalizer.basis(binance_book=empty, coindcx_book=good, spec=_spec())
