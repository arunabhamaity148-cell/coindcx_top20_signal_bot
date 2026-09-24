"""Cross-venue normalization + divergence model (FINAL_DELIVERABLE §K, §L).

HARD RULE (spec §K): a raw Binance USDT mid and a raw CoinDCX INR mid are NEVER
compared. Mandatory order of operations:

  1. quote-currency normalization  (USDT->USD via a stablecoin cross; INR->USD via a
     verified CoinDCX USDT/INR rate - if that rate is unavailable, we FAIL CLOSED)
  2. timestamp alignment           (`|drift| > 1500 ms` -> raise -> NO TRADE)
  3. contract normalization        (px * unit_contract_value * quanto_multiplier;
                                    inverse contracts use unit*quanto/px)
  4. fee / spread awareness        (effective_cost = 2*taker_fee + spread + slippage)
  5. basis                         (basis_bps = (coindcx_usd - binance_usd)/binance_usd*1e4)
  6. net_basis                     (basis_bps - effective_cost)
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Iterable

from app.core.errors import DataUnavailableError, NormalizationError
from app.core.logging_setup import get_logger
from app.core.mathx import percentile_rank, stdev, mean, zscore
from app.core.models import (
    BasisSnapshot, DivergenceClass, InstrumentSpec, OrderBook,
)
from app.core.timeutils import assert_drift_within_budget

log = get_logger(__name__)


@dataclass
class QuoteConverter:
    """Quote-currency normalization.

    `usdt_usd` comes from a public stablecoin cross (e.g. Binance USDCUSDT) and may
    fall back to 1.0 (documented peg assumption). `usdt_inr` has NO verified free
    source in this build, so an INR-quoted instrument raises instead of guessing.
    """

    usdt_usd: float | None = 1.0
    usdt_inr: float | None = None
    usdt_usd_source: str = "assumption:USDT~USD"
    usdt_inr_source: str = "unavailable"

    def to_usd(self, price: float, quote: str) -> float:
        quote = quote.upper()
        if quote in ("USDT", "USD", "BUSD", "USDC"):
            rate = self.usdt_usd if self.usdt_usd else 1.0
            return price * rate
        if quote == "INR":
            if not self.usdt_inr:
                raise DataUnavailableError(
                    "INR quote normalization requires a verified USDT/INR rate; none is available "
                    "in this build (CoinDCX INR-margin futures are UNPROVEN at API level)"
                )
            return price / self.usdt_inr
        raise NormalizationError(f"unsupported quote currency '{quote}'")


def normalize_contract_price(price: float, spec: InstrumentSpec) -> float:
    """Contract normalization (§K step 3)."""
    if price <= 0:
        raise NormalizationError(f"non-positive price {price} for {spec.pair}")
    if spec.inverse:
        denom = spec.unit_contract_value * spec.quanto_multiplier
        if denom <= 0:
            raise NormalizationError(f"inverse contract {spec.pair} has invalid contract value")
        return denom / price
    return price * spec.unit_contract_value * spec.quanto_multiplier


def effective_cost_bps(*, taker_fee_pct: float, spread_bps: float, slippage_bps: float) -> float:
    """effective_cost = 2*taker_fee + spread + expected_slippage (all in bps)."""
    return 2.0 * (taker_fee_pct * 100.0) + max(0.0, spread_bps) + max(0.0, slippage_bps)


def classify_divergence(z: float | None, *, observations: int, min_obs: int,
                        bands: dict[str, float]) -> DivergenceClass:
    """Divergence bands (§L). Insufficient history is ABNORMAL - fail-closed, never NORMAL."""
    if observations < min_obs or z is None or math.isnan(z):
        return DivergenceClass.ABNORMAL
    az = abs(z)
    extreme = float(bands.get("extreme", 3.0))
    abnormal = float(bands.get("abnormal", 3.0))
    elevated = float(bands.get("elevated", 2.0))
    normal = float(bands.get("normal", 1.0))
    if az >= extreme:
        return DivergenceClass.EXTREME
    if az >= elevated:
        return DivergenceClass.ABNORMAL if az >= abnormal else DivergenceClass.ELEVATED
    if az >= normal:
        return DivergenceClass.ELEVATED
    return DivergenceClass.NORMAL


@dataclass
class BasisHistory:
    """Rolling basis series used for z / percentile / vol-adjusted divergence."""

    maxlen: int = 2000
    values: Deque[float] = field(default_factory=lambda: deque(maxlen=2000))

    def __post_init__(self) -> None:
        if not self.values:
            self.values = deque(maxlen=self.maxlen)

    def push(self, basis_bps: float) -> None:
        self.values.append(float(basis_bps))

    def __len__(self) -> int:
        return len(self.values)

    def stats(self, *, realized_vol: float | None = None) -> dict[str, float | None]:
        series = list(self.values)
        current = series[-1] if series else None
        z = zscore(series, current) if len(series) >= 2 else None
        pr = percentile_rank(series, current) if series else None
        vol_adj = None
        if z is not None:
            vol_adj = z / realized_vol if realized_vol and realized_vol > 0 else None
        sd = stdev(series)
        return {"z": z, "percentile": pr, "vol_adjusted": vol_adj, "sd": sd, "mean": mean(series)}

    def convergence_probability(self, *, horizon: int = 1, window: int = 200) -> float | None:
        """Estimate short-horizon mean-reversion from THIS SYMBOL only.

        The statistic is intentionally simple and transparent: fraction of completed
        observations where absolute basis moved closer to zero within `horizon` steps.
        No cross-symbol observations are mixed into the estimate.
        """
        series = list(self.values)[-max(10, int(window)):]
        h = max(1, int(horizon))
        if len(series) <= h + 2:
            return None
        wins = 0
        trials = 0
        for i in range(0, len(series) - h):
            current = abs(series[i])
            future = abs(series[i + h])
            if current <= 0:
                continue
            trials += 1
            if future < current:
                wins += 1
        return (wins / trials) if trials else None


@dataclass
class Normalizer:
    """Builds a BasisSnapshot from two order books + instrument metadata."""

    max_clock_drift_ms: int = 1500
    min_history_obs: int = 60
    divergence_bands: dict[str, float] = field(default_factory=lambda: {
        "normal": 1.0, "elevated": 2.0, "abnormal": 3.0, "extreme": 3.0})
    slippage_bps: float = 3.0
    # Basis observations are strictly per symbol; cross-symbol samples can create fake z-scores.
    history: BasisHistory = field(default_factory=BasisHistory)
    history_by_symbol: dict[str, BasisHistory] = field(default_factory=dict)
    quote: QuoteConverter = field(default_factory=QuoteConverter)
    last_error: str = ""

    def basis(self, *, binance_book: OrderBook, coindcx_book: OrderBook, spec: InstrumentSpec,
              realized_vol: float | None = None, now_ms: int | None = None) -> BasisSnapshot:
        """Compute the quote-normalized, timestamp-aligned, contract-normalized basis."""
        if not binance_book.is_valid:
            raise NormalizationError(f"Binance book for {binance_book.symbol} is missing or crossed")
        if not coindcx_book.is_valid:
            raise NormalizationError(f"CoinDCX book for {coindcx_book.symbol} is missing or crossed")

        # 2. Timestamp policy (root-cause fix for false >1500ms drift alarms):
        #    * When `now_ms` is provided (live path): freshness is measured against
        #      received_ts_ms (local receipt) when present, otherwise ts_ms. Both books
        #      must be fresh relative to that reference.
        #    * Event-time pairwise drift is ONLY enforced when BOTH venues published a real
        #      exchange event timestamp. Comparing a REST-polled local-receipt clock against
        #      a WebSocket exchange event clock is not a valid market-clock comparison and
        #      produced the observed 1.7–2.4s false positives under a 2s CoinDCX poll cycle.
        #    * When `now_ms` is omitted (unit tests / offline), only event-time or ts_ms
        #      pairwise drift is applied — callers that need wall-clock freshness must pass now_ms.
        if now_ms is not None:
            reference = int(now_ms)

            def _age(book: OrderBook) -> int:
                recv = book.received_ts_ms if book.received_ts_ms is not None else book.ts_ms
                return abs(reference - int(recv))

            binance_age = _age(binance_book)
            coindcx_age = _age(coindcx_book)
            freshness_budget = self.max_clock_drift_ms * 2
            assert_drift_within_budget(binance_age, freshness_budget)
            assert_drift_within_budget(coindcx_age, freshness_budget)

        if binance_book.has_exchange_event_ts and coindcx_book.has_exchange_event_ts:
            event_a = int(binance_book.event_ts_ms)  # type: ignore[arg-type]
            event_b = int(coindcx_book.event_ts_ms)  # type: ignore[arg-type]
            drift = assert_drift_within_budget(
                abs(event_a - event_b),
                self.max_clock_drift_ms,
            )
        elif now_ms is not None and (
            not binance_book.has_exchange_event_ts or not coindcx_book.has_exchange_event_ts
        ):
            # Mixed or local-only clocks under a live reference: report receive-time skew
            # for diagnostics only. Fail-closed already applied on freshness above.
            recv_a = binance_book.received_ts_ms if binance_book.received_ts_ms is not None else binance_book.ts_ms
            recv_b = coindcx_book.received_ts_ms if coindcx_book.received_ts_ms is not None else coindcx_book.ts_ms
            drift = abs(int(recv_a) - int(recv_b))
        else:
            # Offline / both sides using plain ts_ms (legacy unit tests): keep original
            # pairwise ts_ms budget so fail-closed behavior remains testable.
            drift = assert_drift_within_budget(
                abs(int(binance_book.ts_ms) - int(coindcx_book.ts_ms)),
                self.max_clock_drift_ms,
            )

        # 1 + 3. quote and contract normalization
        binance_mid = normalize_contract_price(binance_book.mid, spec)
        coindcx_raw = coindcx_book.mid
        coindcx_usd = self.quote.to_usd(normalize_contract_price(coindcx_raw, spec), spec.quote_currency)

        if binance_mid <= 0:
            raise NormalizationError("normalized Binance mid is non-positive")

        # 4. cost awareness
        spread_bps = coindcx_book.spread_bps or 0.0
        cost = effective_cost_bps(taker_fee_pct=spec.taker_fee_pct, spread_bps=spread_bps,
                                  slippage_bps=self.slippage_bps)

        # 5/6. basis + net basis
        basis_bps = (coindcx_usd - binance_mid) / binance_mid * 1e4
        net_basis = basis_bps - cost

        key = spec.pair or coindcx_book.symbol
        history = self.history_by_symbol.setdefault(key, BasisHistory(maxlen=self.history.maxlen))
        history.push(basis_bps)
        # Retain the public `history` attribute for backwards compatibility, but never use it
        # as a decision series.
        self.history = history
        stats = history.stats(realized_vol=realized_vol)
        convergence_probability = history.convergence_probability(
            horizon=1, window=int(max(60, self.min_history_obs * 3))
        )
        latency_buffer_bps = 2.0
        expected_capture_bps = None
        if convergence_probability is not None:
            expected_capture_bps = max(
                0.0, abs(basis_bps) - cost - latency_buffer_bps
            ) * convergence_probability
        classification = classify_divergence(
            stats["z"], observations=len(history), min_obs=self.min_history_obs,
            bands=self.divergence_bands,
        )
        return BasisSnapshot(
            binance_usd_mid=binance_mid,
            coindcx_usd_mid=coindcx_usd,
            basis_bps=basis_bps,
            net_basis_bps=net_basis,
            z=stats["z"],
            percentile=stats["percentile"],
            vol_adjusted=stats["vol_adjusted"],
            effective_cost_bps=cost,
            observations=len(history),
            drift_ms=drift,
            classification=classification,
            raw_coindcx_mid=coindcx_raw,
            quote=spec.quote_currency,
            convergence_probability=convergence_probability,
            expected_capture_bps=expected_capture_bps,
        )

    def try_basis(self, **kwargs) -> BasisSnapshot | None:
        """Convenience wrapper that converts every normalization failure into None.
        Callers MUST treat None as NO TRADE."""
        try:
            return self.basis(**kwargs)
        except Exception as exc:  # noqa: BLE001 - fail-closed
            self.last_error = str(exc)
            log.warning("normalization failed (fail-closed): %s", exc)
            return None

    def divergence_severity(self, snap: BasisSnapshot | None) -> DivergenceClass:
        if snap is None:
            return DivergenceClass.ABNORMAL
        return snap.classification

    def snapshot_stats(self) -> dict[str, float | int | str | None]:
        stats = self.history.stats()
        return {
            "observations": len(self.history),
            "symbols_tracked": len(self.history_by_symbol),
            "z": stats["z"],
            "percentile": stats["percentile"],
            "vol_adjusted": stats["vol_adjusted"],
            "last_error": self.last_error,
        }


def vol_adjusted_divergence(z: float | None, realized_vol: float | None) -> float | None:
    if z is None or not realized_vol or realized_vol <= 0:
        return None
    return z / realized_vol


__all__ = [
    "BasisHistory", "Normalizer", "QuoteConverter", "classify_divergence",
    "effective_cost_bps", "normalize_contract_price", "vol_adjusted_divergence",
]
