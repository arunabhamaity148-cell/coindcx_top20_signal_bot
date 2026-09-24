"""Shared wiring for the operator scripts (kept in one place: no duplicated logic).

Nothing in this module can trade. Every object it constructs is read-only with respect to
the exchanges: public REST/WS data collection, formatting, journalling.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import AppConfig  # noqa: E402
from app.core.models import InstrumentSpec  # noqa: E402
from app.risk.consensus import ConsensusEngine  # noqa: E402
from app.risk.risk_engine import RiskEngine  # noqa: E402
from app.risk.veto_engine import VetoEngine  # noqa: E402
from app.signals.signal_engine import SignalEngine  # noqa: E402
from app.strategies.registry import StrategyRegistry  # noqa: E402
from app.utils.symbol_mapping import SymbolMap  # noqa: E402


def instrument_for(cfg: AppConfig, pair: str) -> InstrumentSpec:
    """Build an InstrumentSpec from the configured pair + precision overrides."""
    pair_cfg = cfg.pairs.by_coindcx(pair)
    if pair_cfg is None:
        raise KeyError(f"{pair} is not part of the configured TOP-20 universe")
    overrides = dict(cfg.pairs.precision_overrides.get(pair, {}))
    maker, taker = cfg.pairs.fee_defaults_frac
    return InstrumentSpec(
        funding_frequency=int(
            getattr(getattr(cfg, "derivatives", None), "funding_interval_hours", 8)
        ),  # CoinDCX perpetual funding interval, hours
        pair=pair,
        binance_symbol=pair_cfg.binance,
        price_increment=float(overrides.get("price_increment", 0.01)),
        quantity_increment=float(overrides.get("quantity_increment", 1.0)),
        min_trade_size=float(overrides.get("min_trade_size", 1.0)),
        min_notional=float(overrides.get("min_notional", 6.0)),
        maker_fee_pct=maker * 100.0,
        taker_fee_pct=taker * 100.0,
    )


def build_signal_engine(cfg: AppConfig) -> tuple[SignalEngine, RiskEngine]:
    risk_engine = RiskEngine(cfg)
    engine = SignalEngine(
        cfg=cfg,
        registry=StrategyRegistry(cfg),
        veto_engine=VetoEngine(cfg),
        consensus_engine=ConsensusEngine(cfg),
        risk_engine=risk_engine,
    )
    return engine, risk_engine


async def live_validation(cfg: AppConfig) -> tuple[bool, str]:
    """Probe both public venues once, including representative market + derivatives reads.

    Returns ``(ok, detail)`` and never raises to the CLI.  The function intentionally
    uses the same public clients as the live bot, so a renamed class cannot leave the
    operator's ``--live`` gate silently broken.
    """
    import asyncio

    from app.data.binance.client import BinanceDataClient
    from app.data.coindcx.client import CoinDCXDataClient

    binance = BinanceDataClient(cfg.exchanges.binance, staleness_ms=cfg.staleness.binance_rest_ms)
    coindcx = CoinDCXDataClient(cfg.exchanges.coindcx, staleness_ms=cfg.staleness.coindcx_rest_ms)
    try:
        await binance.start([])
        await coindcx.start()
        info, _ = await binance.rest.exchange_info()
        binance_symbols = set(info)
        report = await coindcx.validate_universe(
            SymbolMap.from_config(cfg.pairs),
            defaults=cfg.pairs.fee_defaults,
            binance_symbols=binance_symbols,
        )
        if not report.ok:
            return False, f"pair validation failed: {report.errors or report.rejected[:5]}"
        sample = report.valid[0]
        # Representative market reads on both venues; these remain public/read-only.
        b_symbol = sample.binance
        c_pair = sample.coindcx
        book_b, premium, oi, funding = await asyncio.gather(
            binance.rest.depth(b_symbol),
            binance.rest.premium_index(b_symbol),
            binance.rest.open_interest(b_symbol),
            binance.rest.funding_rate(b_symbol, limit=1),
        )
        book_c = await coindcx.rest.orderbook(c_pair)
        if not book_b.is_valid or not book_c.is_valid:
            return False, f"market-data probe returned invalid books for {c_pair}/{b_symbol}"
        return (
            True,
            f"validated {len(report.valid)}/{len(cfg.pairs.pairs)} pairs; "
            f"Binance depth/premium/OI/funding + CoinDCX futures orderbook probe passed for {c_pair}",
        )
    except Exception as exc:  # noqa: BLE001 - CLI should report unavailable, not crash
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        await coindcx.stop()
        await binance.stop()
