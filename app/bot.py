"""Orchestration: the live signal-only loop.

Boot sequence (master prompt §30):
  1 configuration validation            6 news health
  2 TOP-20 validation                   7 database health
  3 CoinDCX instrument validation       8 Telegram health
  4 Binance market validation           9 safety invariant check
  5 feed health
If any critical check fails, SIGNAL GENERATION DOES NOT START.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from app.backtest.costs import CostModel
from app.config import AppConfig
from app.core.errors import FailClosedError
from app.core.logging_setup import get_logger
from app.core.mathx import atr_series, percentile_rank, realized_vol
from app.core.models import NewsState, SignalState
from app.core.timeutils import now_ms
from app.data.binance.client import BinanceDataClient
from app.data.coindcx.client import CoinDCXDataClient
from app.data.derivatives import DerivativesEngine
from app.data.health import FeedHealthRegistry
from app.data.normalization import Normalizer
from app.data.orderbook import OrderBookEngine
from app.data.snapshot import SnapshotBuilder
from app.database.repository import JournalRepository
from app.monitoring.health import HealthReport, startup_health
from app.monitoring.metrics import Metrics as RunMetrics
from app.news.collectors import NewsCollector
from app.news.engine import NewsEngine
from app.news.correlation import NewsCorrelationEngine
from app.risk.btc_regime import BtcRegimeEngine
from app.risk.consensus import ConsensusEngine
from app.risk.risk_engine import RiskEngine
from app.risk.veto_engine import VetoEngine
from app.safety import assert_signal_only
from app.signals.lifecycle import SignalLifecycle
from app.signals.signal_engine import SignalEngine
from app.strategies.registry import StrategyRegistry
from app.telegram.formatter import MessageFormatter
from app.telegram.queue import Priority, TelegramQueue
from app.telegram.sender import TelegramSender
from app.utils.symbol_mapping import SymbolMap

log = get_logger(__name__)


@dataclass
class SignalBot:
    cfg: AppConfig
    scan_interval_sec: float = 30.0
    paper_mode: bool = True

    symbol_map: SymbolMap = field(init=False)
    binance: BinanceDataClient = field(init=False)
    coindcx: CoinDCXDataClient = field(init=False)
    normalizer: Normalizer = field(init=False)
    book_engine: OrderBookEngine = field(init=False)
    derivatives_engine: DerivativesEngine = field(init=False)
    btc_engine: BtcRegimeEngine = field(init=False)
    snapshot_builder: SnapshotBuilder | None = field(default=None, init=False)
    news_engine: NewsEngine | None = field(default=None, init=False)
    signals: SignalEngine = field(init=False)
    lifecycle: SignalLifecycle = field(init=False)
    telegram_sender: TelegramSender = field(init=False)
    telegram_queue: TelegramQueue = field(init=False)
    journal: JournalRepository = field(init=False)
    metrics: RunMetrics = field(default_factory=RunMetrics, init=False)
    health: FeedHealthRegistry = field(default_factory=FeedHealthRegistry, init=False)
    validation_report: object | None = field(default=None, init=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _tasks: list[asyncio.Task] = field(default_factory=list, init=False)
    started_ms: int | None = field(default=None, init=False)
    last_error: str = field(default="", init=False)
    last_regime: object | None = field(default=None, init=False)
    _booted: bool = field(default=False, init=False)
    _boot_report: HealthReport | None = field(default=None, init=False)

    # ------------------------------------------------------------------ construction
    def __post_init__(self) -> None:
        cfg = self.cfg
        self.symbol_map = SymbolMap.from_config(cfg.pairs)
        self.binance = BinanceDataClient(
            cfg.exchanges.binance,
            staleness_ms=cfg.staleness.binance_rest_ms,
            timeframes=tuple(
                cfg.strategy.common.get("timeframes", ("1m", "5m", "15m", "1h", "4h"))
            ),
        )
        self.coindcx = CoinDCXDataClient(
            cfg.exchanges.coindcx, staleness_ms=cfg.staleness.coindcx_rest_ms
        )
        self.normalizer = Normalizer(
            max_clock_drift_ms=cfg.normalization.max_clock_drift_ms,
            min_history_obs=cfg.normalization.min_history_obs,
            divergence_bands=dict(cfg.normalization.divergence_bands),
            slippage_bps=float(cfg.normalization.expected_slippage_bps),
        )
        self.book_engine = OrderBookEngine(
            depth_band_bps=float(cfg.veto.liquidity.get("depth_band_bps", 50.0))
        )
        self.derivatives_engine = DerivativesEngine(
            crowding_extreme_z=float(cfg.veto.crowding.get("max_funding_z", 2.5))
        )
        self.btc_engine = BtcRegimeEngine(cfg)

        registry = StrategyRegistry(cfg)
        self.signals = SignalEngine(
            cfg=cfg,
            registry=registry,
            veto_engine=VetoEngine(cfg),
            consensus_engine=ConsensusEngine(cfg),
            risk_engine=RiskEngine(cfg, state_path=Path(cfg.database.jsonl_path) / "risk_state.json"),
        )
        self.lifecycle = SignalLifecycle(
            danger_monitor=__import__(
                "app.signals.danger", fromlist=["DangerMonitor"]
            ).DangerMonitor(cfg),
            on_state_change=self._on_lifecycle_state_change,
        )
        self.telegram_sender = TelegramSender(cfg)
        self.telegram_queue = TelegramQueue(
            cfg=cfg, formatter=MessageFormatter(cfg), sender=self.telegram_sender
        )
        self.journal = JournalRepository(
            sqlite_path=cfg.database.sqlite_path,
            jsonl_dir=cfg.database.jsonl_path,
            jsonl_mirror=cfg.database.jsonl_mirror,
        )
        self.health = FeedHealthRegistry(
            staleness={
                "binance_rest": cfg.staleness.binance_rest_ms,
                "binance_ws": cfg.staleness.binance_ws_ms,
                "coindcx_rest": cfg.staleness.coindcx_rest_ms,
                "news": cfg.staleness.news_ms,
            },
            min_sources_healthy=cfg.staleness.min_sources_healthy,
            max_clock_drift_ms=cfg.normalization.max_clock_drift_ms,
        )

    # ------------------------------------------------------------------ lifecycle
    async def boot(self) -> HealthReport:
        """Run startup safety checks exactly once; failed boot is transactional."""
        if self._booted and self._boot_report is not None:
            log.info("boot: already completed; reusing startup health report")
            return self._boot_report
        log.info("boot: asserting signal-only invariants")
        try:
            assert_signal_only(self.cfg)
            self.journal.connect()

            await self.binance.start([])
            await self.coindcx.start()
            # `BinanceDataClient.start()` already performs the exchangeInfo load. Reuse
            # its validated metadata to avoid a redundant startup-weight request.
            binance_symbols: set[str] = set(self.binance._symbol_meta)
            if not binance_symbols:
                self.last_error = self.binance._last_error or "Binance exchangeInfo returned no symbols"
                log.error("Binance exchangeInfo unavailable at boot: %s", self.last_error)
            self.validation_report = await self.coindcx.validate_universe(
                self.symbol_map, defaults=self.cfg.pairs.fee_defaults, binance_symbols=binance_symbols
            )

            valid_pairs = tuple(p.coindcx for p in self.validation_report.valid)
            if not valid_pairs:
                raise FailClosedError(
                    "no TOP-20 pair passed venue validation - SIGNAL GENERATION WILL NOT START"
                )

            if self.cfg.news.enabled:
                self.news_engine = NewsEngine(
                    self.cfg.news,
                    NewsCollector(self.cfg.news),
                    correlation=NewsCorrelationEngine.from_symbol_map(self.symbol_map),
                )
                try:
                    await self.news_engine.run_once()
                except Exception as exc:
                    log.error("initial news sweep failed: %s", exc)

            self.snapshot_builder = SnapshotBuilder(
                cfg=self.cfg,
                symbol_map=self.symbol_map,
                binance=self.binance,
                coindcx=self.coindcx,
                normalizer=self.normalizer,
                book_engine=self.book_engine,
                derivatives_engine=self.derivatives_engine,
                btc_engine=self.btc_engine,
                news_engine=self.news_engine,
                health=self.health,
            )

            valid_binance_symbols = [p.binance for p in self.validation_report.valid]
            await self.binance.start_ws(valid_binance_symbols or ["BTCUSDT"])
            await self.coindcx.start_polling(valid_pairs)
            await self.binance.warm_cache(
                valid_binance_symbols,
                limit=400,
                max_concurrency=int(self.cfg.exchanges.binance.poll_concurrency),
            )
            await self.telegram_sender.start()
            await self.telegram_queue.start()

            report = startup_health(
                cfg=self.cfg,
                binance=self.binance,
                coindcx=self.coindcx,
                news=self.news_engine,
                telegram=self.telegram_sender,
                repository=self.journal,
                validation_report=self.validation_report,
            )
            log.info("startup health: %s", report.status)
            for blocker in report.blockers:
                log.error("startup blocker: %s", blocker)
            if not report.ok:
                raise FailClosedError(f"startup health check failed: {report.render()}")

            self.started_ms = now_ms()
            self._stop.clear()
            self._boot_report = report
            self._booted = True
            return report
        except Exception:
            # Startup is transactional: close every resource that may already have been
            # opened before allowing the failure to escape.
            try:
                await self.shutdown()
            except Exception as cleanup_exc:  # noqa: BLE001
                log.error("boot rollback cleanup failed: %s", cleanup_exc)
            raise

    async def shutdown(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        async def _safe_async(label: str, fn) -> None:
            try:
                await fn()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                log.warning("shutdown %s failed: %s", label, exc)

        await _safe_async("telegram queue", self.telegram_queue.stop)
        await _safe_async("telegram sender", self.telegram_sender.close)
        if self.news_engine is not None:
            await _safe_async("news engine", self.news_engine.close)
        await _safe_async("CoinDCX", self.coindcx.stop)
        await _safe_async("Binance", self.binance.stop)
        try:
            self.journal.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown journal close failed: %s", exc)
        self._booted = False
        self._boot_report = None
        self.news_engine = None
        self.snapshot_builder = None
        log.info("shutdown complete")

    async def run(self, *, cycles: int | None = None) -> None:
        """Main scan loop. Boots only when the caller has not already booted the bot."""
        if not self._booted:
            await self.boot()
        cycles_done = 0
        while not self._stop.is_set():
            try:
                await self.scan_once()
            except Exception as exc:
                self.last_error = str(exc)
                self.metrics.inc("scan_errors")
                log.error("scan cycle failed: %s", exc)
                self.journal.record_error("scan", exc)
            cycles_done += 1
            if cycles is not None and cycles_done >= cycles:
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.scan_interval_sec)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------ one cycle
    async def scan_once(self) -> list[object]:
        assert self.snapshot_builder is not None
        started = now_ms()
        emitted: list[object] = []
        if self.news_engine is not None:
            try:
                await self.news_engine.run_once()
            except Exception as exc:
                log.error("news sweep failed: %s", exc)
        news_snapshot = self.news_engine.state() if self.news_engine is not None else None
        news_state = news_snapshot.state if news_snapshot else NewsState.CLEAR
        global_blackout = (
            self.news_engine.is_blackout(news_snapshot)
            if self.news_engine and news_snapshot
            else False
        )
        blocking_headline = news_snapshot.blocking_headline if news_snapshot else ""

        btc = await self.snapshot_builder.btc_context()
        self.last_regime = btc
        btc_block, btc_degrade = self.btc_engine.apply(btc)
        self.metrics.inc(f"btc_regime_{btc.regime.value.lower()}")

        pairs = tuple(p.coindcx for p in self.validation_report.valid)  # type: ignore[union-attr]
        for pair in pairs:
            try:
                snap = await self.snapshot_builder.build(
                    pair, news_state=news_state, btc_regime=btc.regime, btc_conflict=btc.conflict
                )
            except Exception as exc:
                self.metrics.inc("snapshot_errors")
                self.journal.record_error("snapshot", exc, {"pair": pair})
                continue

            # DANGER re-evaluation for live signals BEFORE considering new ones
            for update in self.lifecycle.update(snap=snap, price=snap.last_price):
                if update.alert is not None and update.state in (
                    SignalState.DANGER,
                    SignalState.INVALIDATED,
                ):
                    self.metrics.inc("danger_alerts")
                    await self.telegram_queue.publish_danger(update.alert)
                    self.journal.record_error("danger", update.alert.to_row(), {"pair": pair})

            series = list(snap.series("5m"))
            rv = (
                realized_vol(
                    [c.close for c in series],
                    periods_per_year=365 * 24 * 12,
                    window=min(200, len(series) - 1),
                )
                if len(series) > 3
                else None
            )
            atr_series_values = atr_series(series, self.cfg.strategy.atr_period)
            atr_rank = (
                percentile_rank(atr_series_values, atr_series_values[-1])
                if atr_series_values
                else None
            )

            pair_block_items = (
                self.news_engine.blocking_for_pair(news_snapshot, pair)
                if self.news_engine and news_snapshot
                else []
            )
            pair_blocked = bool(pair_block_items)
            pair_headline = pair_block_items[0].headline if pair_block_items else blocking_headline
            decision = self.signals.generate(
                snap=snap,
                realized_vol=rv,
                atr_pct_rank=atr_rank,
                blackout_active=global_blackout,
                blocking_headline=pair_headline,
                news_blocked_for_pair=pair_blocked,
                live_signals=self.lifecycle.active_symbols(),
                btc_block=btc_block,
                btc_degrade=btc_degrade,
            )
            if decision.veto is not None and decision.veto.results:
                rows = decision.veto.rows(snap.symbol)
                if rows:
                    self.journal.record_vetoes(rows)
                    for row in rows:
                        self.metrics.inc(f"veto_{row['guard']}")
            if decision.signal is None:
                self.metrics.inc("no_trade")
                if decision.no_trade is not None:
                    self.journal.record_error("no_trade", decision.no_trade.reason, {"pair": pair})
                continue

            signal = decision.signal
            self.journal.record_signal(signal.to_row())
            self.lifecycle.add(signal)
            bullets = self.signals.why_bullets(signal, limit=3)
            await self.telegram_queue.enqueue(
                self.telegram_queue.formatter.management(signal),
                priority=Priority.INFO,
                dedupe_key=f"mgmt:{signal.signal_id}",
            )
            await self.telegram_queue.publish_signal(signal, bullets)
            self.metrics.inc("signals_emitted")
            why_latency = self.telegram_queue.max_why_latency_ms()
            if why_latency is not None:
                self.metrics.latency(
                    "why_block", int(self.cfg.system.why_message_budget_sec) * 1000
                ).record(why_latency)
            emitted.append(signal)
        self.metrics.inc("scan_cycles")
        self.metrics.latency("scan_cycle", int(self.scan_interval_sec * 1000)).record(
            now_ms() - started
        )
        return emitted

    def _on_lifecycle_state_change(self, signal, state: SignalState) -> None:
        if state not in (SignalState.EXPIRED, SignalState.INVALIDATED):
            return
        group = str(signal.meta.get("correlation_group", signal.meta.get("strategy_group", "")))
        if not group:
            # SignalEngine stores the strategy itself; derive its group from the live candidate is
            # not safe, so the registration map is authoritative. RiskEngine.close is idempotent
            # and retrieves the stored group by symbol.
            group = ""
        try:
            self.signals.risk_engine.close(symbol=signal.symbol, group=group, realised_r=None, now_ms=signal.created_ms)
        except Exception as exc:  # fail-safe telemetry; do not hide lifecycle state
            log.error("risk lifecycle release failed for %s: %s", signal.signal_id, exc)
            self.last_error = str(exc)

    # ------------------------------------------------------------------ reporting
    def status(self) -> dict[str, object]:
        return {
            "started_ms": self.started_ms,
            "uptime_sec": self.metrics.uptime_sec,
            "valid_pairs": len(getattr(self.validation_report, "valid", ()) or ()),
            "rejected_pairs": list(getattr(self.validation_report, "rejected", ()) or ()),
            "active_signals": [s.signal_id for s in self.lifecycle.live_signals()],
            "btc_regime": getattr(self.last_regime, "regime", None).value
            if self.last_regime
            else None,
            "metrics": self.metrics.snapshot(),
            "normalizer": self.normalizer.snapshot_stats(),
            "rate_limit": self.binance.rest.budget.snapshot(),
            "telegram": {**self.telegram_sender.snapshot(), **self.telegram_queue.snapshot()},
            "last_error": self.last_error,
            "cost_model": CostModel(
                self.cfg.backtest.maker_fee_pct,
                self.cfg.backtest.taker_fee_pct,
                self.cfg.backtest.slippage_bps,
                self.cfg.backtest.spread_cost_bps,
                self.cfg.backtest.latency_ms,
            ).describe(),
        }


__all__ = ["SignalBot"]
