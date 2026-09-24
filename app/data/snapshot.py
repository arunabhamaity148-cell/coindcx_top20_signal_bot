"""Snapshot builder: assembles the immutable `MarketSnapshot` the strategy layer sees.

Every field is either measured from a verified venue payload or explicitly None/UNKNOWN.
A missing input is NEVER interpolated - it becomes a fail-closed condition downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from app.config import AppConfig
from app.core.errors import FailClosedError, StaleDataError, SymbolMappingError
from app.core.logging_setup import get_logger
from app.core.models import (
    BtcRegime, Candle, DerivativesSnapshot, FeedHealth, InstrumentSpec,
    MarketSnapshot, NewsState,
)
from app.core.timeutils import age_ms, drift_ms, now_ms
from app.data.binance.client import BinanceDataClient
from app.data.coindcx.client import CoinDCXDataClient
from app.data.derivatives import DerivativesEngine
from app.data.normalization import Normalizer
from app.data.orderbook import OrderBookEngine
from app.data.health import FeedHealthRegistry
from app.news.engine import NewsEngine
from app.risk.btc_regime import BtcRegimeEngine
from app.utils.symbol_mapping import SymbolMap

log = get_logger(__name__)


@dataclass
class SnapshotBuilder:
    cfg: AppConfig
    symbol_map: SymbolMap
    binance: BinanceDataClient
    coindcx: CoinDCXDataClient
    normalizer: Normalizer
    book_engine: OrderBookEngine
    derivatives_engine: DerivativesEngine
    btc_engine: BtcRegimeEngine
    news_engine: NewsEngine | None = None
    health: FeedHealthRegistry = field(default_factory=lambda: FeedHealthRegistry())
    timeframes: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h")

    async def build(self, pair: str, *, news_state: NewsState = NewsState.CLEAR,
                    btc_regime: BtcRegime = BtcRegime.UNKNOWN, btc_conflict: bool = False,
                    reference_ms: int | None = None) -> MarketSnapshot:
        """Assemble one pair's snapshot. Raises FailClosedError when a hard input is missing."""
        reference = reference_ms or now_ms()
        configured = self.symbol_map.by_coindcx(pair)
        instrument: InstrumentSpec = self.coindcx.require_tradable(pair)
        binance_symbol = configured.binance

        binance_meta = self.binance.require_metadata(binance_symbol)
        binance_book = await self.binance.orderbook(binance_symbol)
        if binance_book is None:
            raise StaleDataError(f"Binance order book unavailable for {binance_symbol} - NO TRADE")
        self.book_engine.observe(binance_book)

        coindcx_book = self.coindcx.cached_book(pair) or await self.coindcx.orderbook(pair)
        if coindcx_book is None or not coindcx_book.is_valid:
            raise StaleDataError(f"CoinDCX order book unavailable for {pair} - NO TRADE")
        self.book_engine.observe(coindcx_book)

        candles: dict[str, Sequence[Candle]] = {}
        for timeframe in self.timeframes:
            series = self.binance.cached_klines(binance_symbol, timeframe)
            if not series:
                try:
                    series = await self.binance.klines(binance_symbol, timeframe)
                except Exception as exc:  # noqa: BLE001 - a missing timeframe is fatal for signals
                    raise FailClosedError(f"klines {binance_symbol} {timeframe} unavailable: {exc}") from exc
            if not series or not self.binance.candle_health(binance_symbol, timeframe, reference_ms=reference):
                last_ts = series[-1].close_time_ms if series else None
                raise StaleDataError(
                    f"Binance {timeframe} candles stale for {binance_symbol}: last={last_ts} ref={reference} - NO TRADE"
                )
            candles[timeframe] = tuple(series)

        deriv_raw = await self.binance.derivatives_context(binance_symbol)
        funding_history = deriv_raw.get("funding_history")
        if isinstance(funding_history, Sequence) and not isinstance(funding_history, (str, bytes)):
            self.derivatives_engine.observe_funding(binance_symbol, list(funding_history))
        oi = deriv_raw.get("open_interest")
        oi_chg = None
        series_1h = list(candles.get("1h", ()))
        if len(series_1h) >= 2 and series_1h[-2].open is not None and series_1h[-1].volume:
            price_chg = ((series_1h[-1].close - series_1h[-2].close) / series_1h[-2].close * 100.0
                         if series_1h[-2].close else None)
        else:
            price_chg = None
        derivatives = self.derivatives_engine.build(
            symbol=binance_symbol,
            candles=list(candles.get("5m", ())),
            funding_rate=deriv_raw.get("funding_rate"),
            mark_price=deriv_raw.get("mark_price"),
            index_price=deriv_raw.get("index_price"),
            open_interest=float(oi) if oi is not None else None,
            oi_ts_ms=int(float(deriv_raw["open_interest_ts"])) if deriv_raw.get("open_interest_ts") is not None else None,
            funding_ts_ms=(deriv_raw.get("funding_ts_ms") if deriv_raw.get("funding_ts_ms") is not None else None),
            mark_ts_ms=(deriv_raw.get("mark_ts_ms") if deriv_raw.get("mark_ts_ms") is not None else None),
            index_ts_ms=(deriv_raw.get("index_ts_ms") if deriv_raw.get("index_ts_ms") is not None else None),
            ts_ms=reference,
        )

        liquidity = self.book_engine.liquidity(
            coindcx_book, side_for_slippage="bid" if coindcx_book.mid and binance_book.mid
            and coindcx_book.mid > binance_book.mid else "ask")

        basis = self.normalizer.try_basis(binance_book=binance_book, coindcx_book=coindcx_book,
                                         spec=instrument, realized_vol=None, now_ms=reference)

        health: dict[str, FeedHealth] = {}
        health.update(self.binance.health())
        health.update(self.coindcx.health())
        for timeframe, series in candles.items():
            last_close = series[-1].close_time_ms if series else None
            tf_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[timeframe]
            age = (reference - last_close) if last_close is not None else None
            budget = max(self.cfg.staleness.binance_rest_ms, tf_ms)
            state = (FeedHealth(
                f"binance_candle_{timeframe}",
                __import__("app.core.models", fromlist=["FeedState"]).FeedState.HEALTHY
                if age is not None and 0 <= age <= budget
                else __import__("app.core.models", fromlist=["FeedState"]).FeedState.STALE,
                last_close, age, f"timeframe={timeframe}"))
            health[state.name] = state
        # Derivatives are independently health-checked; strategies that require them cannot
        # silently consume an old OI/funding observation.
        for name, ts, budget in (
            ("binance_oi", derivatives.oi_ts_ms, self.derivatives_engine.oi_max_age_ms),
            ("binance_mark", derivatives.mark_ts_ms, self.cfg.staleness.binance_rest_ms),
        ):
            age = (reference - ts) if ts is not None else None
            feed_state = __import__("app.core.models", fromlist=["FeedState"]).FeedState
            state = feed_state.HEALTHY if age is not None and 0 <= age <= budget else feed_state.STALE
            health[name] = FeedHealth(name, state, ts, age)
        if self.news_engine is not None:
            health["news"] = self.news_engine.health()
        self.health.update(health)

        drift = drift_ms(binance_book.ts_ms, coindcx_book.ts_ms)
        book_jump = self.book_engine.tracker(coindcx_book.symbol).mid_jump_bps(coindcx_book.mid)

        return MarketSnapshot(
            symbol=pair,
            ts_ms=reference,
            binance_book=binance_book,
            coindcx_book=coindcx_book,
            candles=candles,
            derivatives=derivatives,
            liquidity=liquidity,
            basis=basis,
            instrument=instrument,
            feed_health=health,
            clock_drift_ms=drift,
            news_state=news_state,
            btc_regime=btc_regime,
            btc_conflict=btc_conflict,
            book_mid_jump_bps=book_jump,
        )

    async def btc_context(self, reference_ms: int | None = None):
        """Classify the BTC regime used as the market-wide filter."""
        pair = self.cfg.btc_regime.coindcx_pair
        symbol = self.cfg.btc_regime.symbol
        candles = self.binance.cached_klines(symbol, "1h")
        if not candles:
            try:
                candles = await self.binance.klines(symbol, "1h")
            except Exception as exc:  # noqa: BLE001
                log.warning("BTC regime candles unavailable: %s", exc)
                return self.btc_engine.evaluate(candles_1h=[], derivatives=None)
        deriv = self.derivatives_engine.build(symbol=symbol, candles=list(self.binance.cached_klines(symbol, "5m")))
        basis = None
        try:
            binance_book = self.binance.cached_book(symbol) or await self.binance.orderbook(symbol)
            coindcx_book = self.coindcx.cached_book(pair) or await self.coindcx.orderbook(pair)
            if binance_book and coindcx_book:
                basis = self.normalizer.try_basis(binance_book=binance_book, coindcx_book=coindcx_book,
                                                  spec=self.coindcx.instrument(pair))
        except Exception as exc:  # noqa: BLE001
            log.debug("BTC basis unavailable: %s", exc)
        return self.btc_engine.evaluate(candles_1h=list(candles), derivatives=deriv,
                                        basis_z=basis.z if basis else None)


__all__ = ["SnapshotBuilder"]
