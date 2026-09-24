"""CoinDCX data facade: instrument validation + execution-reality prices.

Validation contract (master prompt §4): every configured pair must be validated
against CoinDCX Futures instrument metadata. If validation fails -> NO SIGNAL for
that pair, and the pair is NEVER silently replaced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.core.errors import FailClosedError, SymbolMappingError
from app.core.logging_setup import get_logger
from app.core.models import FeedHealth, FeedState, InstrumentSpec, OrderBook
from app.core.timeutils import now_ms
from app.data.coindcx.rest import CoinDCXPoller, CoinDCXRestClient
from app.utils.symbol_mapping import SymbolMap, SymbolPair

log = get_logger(__name__)


@dataclass
class ValidationReport:
    valid: tuple[SymbolPair, ...] = ()
    rejected: tuple[str, ...] = ()
    instruments: dict[str, InstrumentSpec] = field(default_factory=dict)
    active_symbols: set[str] = field(default_factory=set)
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.valid) and not self.errors


class CoinDCXDataClient:
    def __init__(self, exchange_cfg: Any, staleness_ms: int = 6000, *,
                 client: CoinDCXRestClient | None = None):
        self.cfg = exchange_cfg
        self.staleness_ms = staleness_ms
        self.rest = client or CoinDCXRestClient(exchange_cfg.rest_base,
                                             market_data_base=getattr(exchange_cfg, "market_data_base", ""),
                                             poll_sec=float(exchange_cfg.poll_sec))
        self.poller: CoinDCXPoller | None = None
        self.instruments: dict[str, InstrumentSpec] = {}
        self.active_symbols: set[str] = set()
        self._owns_client = client is None
        self.last_error: str = ""

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._owns_client:
            await self.rest.start()

    async def stop(self) -> None:
        if self.poller is not None:
            await self.poller.stop()
        if self._owns_client:
            await self.rest.close()

    async def validate_universe(self, symbol_map: SymbolMap, *, defaults: Mapping[str, Any],
                                binance_symbols: set[str]) -> ValidationReport:
        """Validate every configured pair. Failures are reported, never repaired."""
        errors: list[str] = []
        try:
            self.active_symbols = await self.rest.active_instruments()
        except Exception as exc:  # noqa: BLE001 - without the list nothing is tradable
            self.last_error = str(exc)
            log.error("CoinDCX active_instruments unavailable: %s", exc)
            return ValidationReport(errors=(f"active_instruments unavailable: {exc}",))

        valid, rejected = symbol_map.validate_against_venues(
            coindcx_active=self.active_symbols, binance_symbols=binance_symbols)
        instruments: dict[str, InstrumentSpec] = {}
        still_valid: list[SymbolPair] = []
        for pair in valid:
            try:
                spec = await self.rest.instrument(pair.coindcx, binance_symbol=pair.binance,
                                                  defaults=defaults)
                self._assert_spec_sane(spec)
                instruments[pair.coindcx] = spec
                still_valid.append(pair)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{pair.coindcx}: instrument metadata invalid ({exc})")
                rejected = (*rejected, f"{pair.coindcx}: {exc}")
        self.instruments = instruments
        report = ValidationReport(valid=tuple(still_valid), rejected=tuple(rejected),
                                  instruments=instruments, active_symbols=self.active_symbols,
                                  errors=tuple(errors))
        log.info("CoinDCX validation: %d valid, %d rejected", len(report.valid), len(report.rejected))
        return report

    @staticmethod
    def _assert_spec_sane(spec: InstrumentSpec) -> None:
        if spec.price_increment <= 0:
            raise SymbolMappingError(f"{spec.pair}: price_increment must be positive")
        if spec.quantity_increment <= 0:
            raise SymbolMappingError(f"{spec.pair}: quantity_increment must be positive")
        if spec.min_trade_size <= 0:
            raise SymbolMappingError(f"{spec.pair}: min_trade_size must be positive")
        if spec.min_notional <= 0:
            raise SymbolMappingError(f"{spec.pair}: min_notional must be positive")
        if spec.quote_currency != "USDT":
            raise SymbolMappingError(
                f"{spec.pair}: quote currency {spec.quote_currency} is not USDT - the INR/quanto "
                "normalization path requires a verified FX source and is UNPROVEN"
            )

    async def start_polling(self, pairs: tuple[str, ...]) -> None:
        self.poller = CoinDCXPoller(client=self.rest, pairs=pairs, poll_sec=float(self.cfg.poll_sec), max_concurrency=int(getattr(self.cfg, "poll_concurrency", 5)))
        await self.poller.start()

    # ------------------------------------------------------------------ reads
    async def orderbook(self, pair: str, depth: int = 50) -> OrderBook | None:
        try:
            return await self.rest.orderbook(pair, depth=depth)
        except Exception as exc:  # noqa: BLE001 - missing book => NO TRADE later
            self.last_error = str(exc)
            log.warning("coindcx orderbook %s failed: %s", pair, exc)
            return None

    def cached_book(self, pair: str) -> OrderBook | None:
        return self.poller.book(pair) if self.poller else None

    def instrument(self, pair: str) -> InstrumentSpec:
        spec = self.instruments.get(pair)
        if spec is None:
            raise FailClosedError(f"no validated CoinDCX instrument metadata for {pair} - NO SIGNAL")
        return spec

    def require_tradable(self, pair: str) -> InstrumentSpec:
        if self.active_symbols and pair not in self.active_symbols:
            raise SymbolMappingError(f"{pair} is not in CoinDCX active_instruments - NOT TRADABLE")
        return self.instrument(pair)

    # ------------------------------------------------------------------ health
    def health(self) -> dict[str, FeedHealth]:
        reference = now_ms()
        if self.poller is None:
            return {"coindcx_rest": FeedHealth("coindcx_rest", FeedState.STALE, None, None,
                                               "polling not started")}
        ages = [self.poller.age_ms(p) for p in self.poller.pairs]
        known = [a for a in ages if a is not None]
        if not known:
            return {"coindcx_rest": FeedHealth("coindcx_rest", FeedState.DISCONNECTED, None, None,
                                               self.last_error)}
        worst = max(known)
        if worst > self.staleness_ms * 3:
            state = FeedState.DISCONNECTED
        elif worst > self.staleness_ms:
            state = FeedState.STALE
        elif worst > self.staleness_ms / 2:
            state = FeedState.DEGRADED
        else:
            state = FeedState.HEALTHY
        return {"coindcx_rest": FeedHealth("coindcx_rest", state, reference - worst, worst,
                                          f"poll failures={self.poller.consecutive_failures}")}

    @property
    def available(self) -> bool:
        return bool(self.instruments)


async def validate_pairs(cfg: Any, coindcx: CoinDCXDataClient, symbol_map: SymbolMap,
                         binance_client: Any) -> ValidationReport:
    """Startup gate used by main.py and scripts/validate_config.py."""
    await coindcx.start()
    try:
        binance_symbols: set[str] = set()
        try:
            info, _ = await asyncio.wait_for(binance_client.rest.exchange_info(), timeout=20)
            binance_symbols = set(info)
        except Exception as exc:  # noqa: BLE001
            log.error("Binance exchangeInfo unavailable during validation: %s", exc)
        return await coindcx.validate_universe(
            symbol_map,
            defaults=cfg.pairs.fee_defaults,
            binance_symbols=binance_symbols,
        )
    finally:
        pass


__all__ = ["CoinDCXDataClient", "ValidationReport", "validate_pairs"]
