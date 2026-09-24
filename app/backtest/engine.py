"""Event-driven backtest engine (FINAL_DELIVERABLE §Y, master prompt §21).

Guarantees:
  * NEXT-BAR execution only - a signal computed from bar `i` can only fill from bar
    `i+latency` onward, which removes look-ahead by construction;
  * limit fills are simulated (never assumed) with an explicit fill-probability model;
  * the mandatory cost model applies verified CoinDCX fees + spread + slippage + latency;
  * TPs are processed in ascending R order and the SL is checked on the same bar, so an
    ambiguous bar resolves pessimistically (stop first);
  * the strategy layer is injected, so the same objects that run live are exercised here.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.backtest.costs import CostModel
from app.backtest.fills import LimitFillModel
from app.backtest.metrics import Metrics, TradeResult, compute_metrics
from app.config import AppConfig
from app.core.errors import FailClosedError
from app.core.logging_setup import get_logger
from app.core.mathx import atr, realized_vol
from app.core.models import Candle, Direction, MarketSnapshot

log = get_logger(__name__)

SnapshotFactory = Callable[[str, int], MarketSnapshot | None]


@dataclass
class BacktestEngine:
    cfg: AppConfig
    signal_engine: object  # app.signals.signal_engine.SignalEngine
    snapshot_factory: SnapshotFactory
    cost_model: CostModel = field(default_factory=CostModel)
    fill_model: LimitFillModel = field(default_factory=LimitFillModel)
    warmup_bars: int = 200
    seed: int = 11

    def run(
        self,
        *,
        symbol: str,
        bars: Sequence[Candle],
        regime: str = "UNKNOWN",
        bar_minutes: float = 5.0,
        max_signals: int | None = None,
        start_index: int | None = None,
        end_index: int | None = None,
    ) -> tuple[Metrics, list[TradeResult]]:
        """Single-symbol event loop. Returns (metrics, trades)."""
        if len(bars) <= self.warmup_bars + 5:
            raise FailClosedError(f"insufficient bars for a backtest ({len(bars)})")
        rng = random.Random(self.seed)
        trades: list[TradeResult] = []
        considered = 0
        vetoed = 0
        expiries = 0
        misses = 0
        index = max(self.warmup_bars, int(start_index or self.warmup_bars))
        final_index = min(len(bars) - 2, int(end_index) if end_index is not None else len(bars) - 2)
        # Risk slots are held until the simulated trade's actual end bar. This models
        # max-concurrency and daily-loss guards while still allowing legitimate overlap.
        pending_releases: list[tuple[int, str, str, float]] = []
        while index <= final_index:
            for release_index, r_symbol, r_group, realised_r in list(pending_releases):
                if release_index <= index:
                    try:
                        self.signal_engine.risk_engine.close(
                            symbol=r_symbol, group=r_group, realised_r=realised_r,
                            now_ms=bars[min(release_index, len(bars) - 1)].open_time_ms,
                        )
                    finally:
                        pending_releases.remove((release_index, r_symbol, r_group, realised_r))
            snap = self.snapshot_factory(symbol, index)
            if snap is None:
                index += 1
                continue
            decision = self.signal_engine.generate(snap=snap, reference_ms=bars[index].open_time_ms)
            considered += 1
            if decision.veto is not None and decision.veto.blocked:
                vetoed += 1
            if decision.signal is None:
                index += 1
                continue
            trade = self._simulate(
                signal=decision.signal,
                bars=bars,
                index=index,
                regime=regime,
                bar_minutes=bar_minutes,
                rng=rng,
            )
            trades.append(trade)
            if trade.filled:
                group = str(decision.signal.meta.get("correlation_group", ""))
                release_at = min(final_index + 1, index + max(1, trade.bars_held))
                pending_releases.append((release_at, trade.symbol, group, trade.net_r))
            if trade.outcome in ("EXPIRED",):
                expiries += 1
            if trade.outcome in ("MISSED", "INVALIDATED"):
                misses += 1
            # Never jump the event loop past a live trade. Separate signals may overlap
            # until RiskEngine's concurrency/group/cooldown limits are reached.
            index += 1
            if max_signals is not None and len(trades) >= max_signals:
                break
        # Release any still-open simulated risk reservations at the end of the test window.
        for release_index, r_symbol, r_group, realised_r in pending_releases:
            try:
                self.signal_engine.risk_engine.close(
                    symbol=r_symbol, group=r_group, realised_r=realised_r,
                    now_ms=bars[min(release_index, len(bars) - 1)].open_time_ms,
                )
            except Exception as exc:
                log.warning("backtest risk-release cleanup failed for %s: %s", r_symbol, exc)
        metrics = compute_metrics(
            trades,
            signals_considered=considered,
            vetoed=vetoed,
            expiries=expiries,
            misses=misses,
            days=len(bars) * bar_minutes / (60 * 24),
        )
        return metrics, trades

    # ------------------------------------------------------------------ internals
    def _simulate(
        self,
        *,
        signal,
        bars: Sequence[Candle],
        index: int,
        regime: str,
        bar_minutes: float,
        rng: random.Random,
    ) -> TradeResult:
        expiry_bars = max(1, int((signal.expiry_ms - signal.created_ms) / (bar_minutes * 60_000)))
        expiry_index = min(len(bars), index + 1 + expiry_bars)
        fill = self.fill_model.simulate(
            direction=signal.direction,
            entry=signal.entry_price,
            zone_low=signal.entry_zone_low,
            zone_high=signal.entry_zone_high,
            invalidation=signal.invalidation,
            atr=signal.atr,
            bars=bars,
            start_index=index,
            expiry_index=expiry_index,
            rng=rng,
        )
        if not fill.filled:
            return TradeResult(
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                strategy=str(signal.meta.get("strategy", "?")),
                regime=regime,
                direction=signal.direction.value,
                filled=False,
                outcome=fill.outcome.value,
            )

        entry = self.cost_model.apply_entry(
            fill.fill_price or signal.entry_price,
            side="buy" if signal.direction is Direction.LONG else "sell",
        )
        risk = abs(entry - signal.stop_loss)
        if risk <= 0:
            raise FailClosedError("non-positive risk in the backtest simulation")

        # Position management is explicitly partial: TP1 40%, TP2 30%, TP3 20%,
        # TP4 10%. After TP1, the remaining position's stop is moved to breakeven.
        tp_sizes = (0.40, 0.30, 0.20, 0.10)
        tp_prices = (signal.tp1, signal.tp2, signal.tp3, signal.tp4)
        remaining = 1.0
        realized_gross_r = 0.0
        fee_cost_r = self.cost_model.maker_fee * entry / risk
        mfe = 0.0
        mae = 0.0
        tp_hits: list[int] = []
        current_stop = signal.stop_loss
        last_event_offset = 0

        def price_r(price: float) -> float:
            return ((price - entry) / risk) if signal.direction is Direction.LONG else ((entry - price) / risk)

        for offset, bar in enumerate(bars[fill.bar_index : expiry_index], start=1):
            high_r = price_r(bar.high)
            low_r = price_r(bar.low)
            mfe = max(mfe, high_r)
            mae = min(mae, low_r)

            # Ambiguous candle: test the currently active stop first.
            stop_breached = (
                bar.low <= current_stop if signal.direction is Direction.LONG
                else bar.high >= current_stop
            )
            if stop_breached and remaining > 0:
                exit_price = self.cost_model.apply_exit(
                    current_stop,
                    side="sell" if signal.direction is Direction.LONG else "buy",
                    is_stop=True,
                )
                leg_r = price_r(exit_price)
                realized_gross_r += remaining * leg_r
                fee_cost_r += remaining * self.cost_model.taker_fee * exit_price / risk
                outcome = "SL" if not tp_hits else "SL_AFTER_TP"
                return self._result(
                    signal, entry, exit_price, risk, outcome, offset, regime, mfe, mae,
                    gross_r=realized_gross_r, net_r=realized_gross_r - fee_cost_r,
                    tp_hits=tuple(tp_hits),
                )

            for level_index, (tp, fraction) in enumerate(zip(tp_prices, tp_sizes, strict=True), start=1):
                if level_index in tp_hits or remaining <= 0:
                    continue
                reached = bar.high >= tp if signal.direction is Direction.LONG else bar.low <= tp
                if not reached:
                    continue
                take = min(fraction, remaining)
                exit_price = self.cost_model.apply_exit(
                    tp, side="sell" if signal.direction is Direction.LONG else "buy"
                )
                leg_r = price_r(exit_price)
                realized_gross_r += take * leg_r
                fee_cost_r += take * self.cost_model.maker_fee * exit_price / risk
                remaining -= take
                tp_hits.append(level_index)
                last_event_offset = offset
                if remaining > 0 and level_index == 1:
                    current_stop = entry  # deterministic model of the manual BE rule
                elif remaining > 0 and level_index in (2, 3):
                    # Deterministic approximation of the documented manual structure trail:
                    # after TP2 trail under the prior 5m swing; after TP3 the remaining 10%
                    # runner trails 15m structure (three completed 5m bars). Never loosen a
                    # previously protected stop.
                    current_idx = (fill.bar_index or index) + offset - 1
                    prior = list(bars[max(0, current_idx - 3):current_idx])
                    if len(prior) >= 3:
                        if signal.direction is Direction.LONG:
                            structural_stop = min(c.low for c in prior)
                            current_stop = max(current_stop, structural_stop)
                        else:
                            structural_stop = max(c.high for c in prior)
                            current_stop = min(current_stop, structural_stop)

            if remaining <= 0:
                return self._result(
                    signal, entry, self.cost_model.apply_exit(tp_prices[-1], side="sell" if signal.direction is Direction.LONG else "buy"),
                    risk, "TP4", offset, regime, mfe, mae,
                    gross_r=realized_gross_r, net_r=realized_gross_r - fee_cost_r,
                    tp_hits=tuple(tp_hits),
                )

        # Expiry: close whatever remains at the expiry/last observed close.
        last = bars[min(len(bars) - 1, max(fill.bar_index or index, expiry_index - 1))]
        if remaining > 0:
            exit_price = self.cost_model.apply_exit(
                last.close, side="sell" if signal.direction is Direction.LONG else "buy"
            )
            leg_r = price_r(exit_price)
            realized_gross_r += remaining * leg_r
            fee_cost_r += remaining * self.cost_model.maker_fee * exit_price / risk
        else:
            exit_price = last.close
        outcome = "EXPIRED" if not tp_hits else f"TP{tp_hits[-1]}+EXPIRED"
        return self._result(
            signal, entry, exit_price, risk, outcome, max(last_event_offset, 0), regime, mfe, mae,
            gross_r=realized_gross_r, net_r=realized_gross_r - fee_cost_r,
            tp_hits=tuple(tp_hits),
        )

    def _result(
        self,
        signal,
        entry: float,
        exit_price: float,
        risk: float,
        outcome: str,
        bars_held: int,
        regime: str,
        mfe: float,
        mae: float,
        *,
        gross_r: float,
        net_r: float,
        tp_hits: tuple[int, ...] = (),
    ) -> TradeResult:
        return TradeResult(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            strategy=str(signal.meta.get("strategy", "?")),
            regime=regime,
            direction=signal.direction.value,
            filled=True,
            outcome=outcome,
            gross_r=gross_r,
            net_r=net_r,
            mfe_r=mfe,
            mae_r=mae,
            bars_held=bars_held,
            entry=entry,
            exit_price=exit_price,
            tp_hits=tp_hits,
        )



def regime_label(candles: Sequence[Candle], atr_period: int = 14) -> str:
    """Simple, transparent regime tag used for per-regime reporting (never as a signal)."""
    if len(candles) < 60:
        return "UNKNOWN"
    closes = [c.close for c in candles]
    atr_value = atr(list(candles), atr_period) or 0.0
    rv = realized_vol(closes, periods_per_year=365, window=min(60, len(closes) - 1)) or 0.0
    window = candles[-60:]
    width = max(c.high for c in window) - min(c.low for c in window)
    if rv > 1.5:
        return "HIGH_VOL"
    if atr_value and width <= 6 * atr_value:
        return "COMPRESSION"
    if closes[-1] > closes[0] and (closes[-1] - closes[0]) > 2 * atr_value:
        return "TREND_UP"
    if closes[-1] < closes[0] and (closes[0] - closes[-1]) > 2 * atr_value:
        return "TREND_DOWN"
    return "RANGE"


__all__ = ["BacktestEngine", "SnapshotFactory", "regime_label"]
