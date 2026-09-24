"""Integration: DATA -> NORMALIZATION -> STRATEGY -> VETO -> RISK -> SIGNAL -> TELEGRAM.

The end-to-end test injects a stub strategy registry that returns two hand-built
candidates in DIFFERENT correlation groups (so consensus is genuine, per §16) and then
exercises the real consensus, veto, risk, TP/SL, grading, journal and Telegram-format
code paths. Every fail-closed branch gets its own case.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.backtest.harness import SyntheticSnapshotFactory, synthetic_candles
from app.core.models import (
    Direction,
    Grade,
    NewsState,
    SignalState,
    StrategyCandidate,
)
from app.database.repository import JournalRepository
from app.risk.consensus import ConsensusEngine
from app.risk.risk_engine import RiskEngine
from app.risk.veto_engine import VetoEngine
from app.signals.danger import DangerLevel, DangerMonitor
from app.signals.lifecycle import SignalLifecycle
from app.signals.signal_engine import SignalEngine
from app.telegram.formatter import MessageFormatter
from app.telegram.queue import TelegramQueue
from app.telegram.sender import TelegramSender
from tests.conftest import PAIR, SnapshotParts, flat_series, make_basis, make_snapshot


def _candidate(
    strategy_id: str,
    group: str,
    *,
    direction: Direction,
    entry: float,
    invalidation: float,
    stop: float,
    confidence: float,
) -> StrategyCandidate:
    return StrategyCandidate(
        strategy_id=strategy_id,
        symbol=PAIR,
        direction=direction,
        confidence=confidence,
        entry_price=entry,
        entry_zone_low=entry - 0.5,
        entry_zone_high=entry + 0.5,
        invalidation=invalidation,
        stop_loss=stop,
        tp1=entry + 2.0 if direction is Direction.LONG else entry - 2.0,
        tp2=entry + 4.0 if direction is Direction.LONG else entry - 4.0,
        tp3=entry + 6.0 if direction is Direction.LONG else entry - 6.0,
        tp4=entry + 10.0 if direction is Direction.LONG else entry - 10.0,
        rr_tp2=abs((entry + 4.0 if direction is Direction.LONG else entry - 4.0) - entry) / abs(entry - stop),
        atr=1.0,
        expiry_min=45,
        reasons=(f"{strategy_id} measured observation 0.61 > 0.58", "ATR percentile 0.19 <= 0.25"),
        correlation_group=group,
        metadata={"tp2": entry + 4.0 if direction is Direction.LONG else entry - 4.0},
    )


@dataclass
class StubRegistry:
    candidates: tuple
    errors: dict | None = None

    def run(self, snap):
        return self.candidates, (self.errors or {})

    def ids(self):
        return tuple(c.strategy_id for c in self.candidates)


def _engine(cfg, registry, *, risk_engine=None) -> SignalEngine:
    return SignalEngine(
        cfg=cfg,
        registry=registry,
        veto_engine=VetoEngine(cfg),
        consensus_engine=ConsensusEngine(cfg),
        risk_engine=risk_engine or RiskEngine(cfg),
    )


def _two_group_candidates(direction=Direction.LONG, confidence=0.72, entry=100.0):
    return (
        _candidate(
            "S1",
            "MICRO_LIQUIDITY",
            direction=direction,
            entry=entry,
            invalidation=entry - 1.5,
            stop=entry - 2.0,
            confidence=confidence,
        ),
        _candidate(
            "S4",
            "TREND_DERIVATIVES",
            direction=direction,
            entry=entry,
            invalidation=entry - 1.5,
            stop=entry - 2.0,
            confidence=confidence,
        ),
    )


def _snapshot(cfg, instrument, **overrides):
    fields = dict(
        candles=flat_series(price=100.0),
        binance_mid=100.0,
        basis=make_basis(binance_mid=100.0, z=0.3),
    )
    fields.update(overrides)
    return make_snapshot(SnapshotParts(**fields), instrument=instrument)


def test_full_pipeline_produces_a_grade_a_limit_signal(cfg, instrument):
    engine = _engine(cfg, StubRegistry(_two_group_candidates()))
    decision = engine.generate(snap=_snapshot(cfg, instrument), price=100.0)
    assert decision.signal is not None, decision.no_trade
    signal = decision.signal
    assert signal.grade in (Grade.A, Grade.A_PLUS)
    assert signal.direction is Direction.LONG
    assert signal.stop_loss < signal.entry_price < signal.tp1 < signal.tp2 < signal.tp3 < signal.tp4
    assert signal.rr_tp2 >= cfg.risk.min_rr_tp2
    assert signal.expiry_ms > signal.created_ms
    assert signal.signal_id.startswith("CSB-")
    assert signal.state is SignalState.PENDING
    # tick snapping: the printed number must be placeable on CoinDCX
    scale = 1 / instrument.price_increment
    for level in (signal.entry_price, signal.tp1, signal.tp2, signal.stop_loss):
        assert abs(level * scale - round(level * scale)) < 1e-6


def test_why_bullets_are_capped_at_three_and_are_measured_observations(cfg, instrument):
    engine = _engine(cfg, StubRegistry(_two_group_candidates()))
    decision = engine.generate(snap=_snapshot(cfg, instrument), price=100.0)
    bullets = engine.why_bullets(decision.signal)
    assert len(bullets) <= 3
    assert all(any(ch.isdigit() for ch in bullet) for bullet in bullets), (
        "each WHY bullet must contain a measured number"
    )


def test_telegram_signal_and_why_render_within_the_char_ceiling(cfg, instrument):
    engine = _engine(cfg, StubRegistry(_two_group_candidates()))
    decision = engine.generate(snap=_snapshot(cfg, instrument), price=100.0)
    formatter = MessageFormatter(cfg)
    message = formatter.signal(decision.signal)
    why = formatter.why(decision.signal, engine.why_bullets(decision.signal))
    assert "🚨 SIGNAL" in message
    assert decision.signal.signal_id in message
    assert "SIGNAL_ONLY / LIMIT_ORDER" in message
    assert why.startswith("🧠 WHY THIS SIGNAL?")
    assert why.count("•") <= 3
    ok, problems = formatter.validate(message)
    assert ok, problems
    assert len(message) <= cfg.telegram.max_chars


def test_dry_run_delivery_succeeds_and_never_touches_the_network(cfg, instrument):
    import asyncio

    sender = TelegramSender(cfg)

    async def scenario():
        queue = TelegramQueue(cfg=cfg, formatter=MessageFormatter(cfg), sender=sender)
        await queue.start()
        try:
            engine = _engine(cfg, StubRegistry(_two_group_candidates()))
            decision = engine.generate(snap=_snapshot(cfg, instrument), price=100.0)
            await queue.publish_signal(decision.signal, engine.why_bullets(decision.signal))
            await asyncio.sleep(0.05)
        finally:
            await queue.stop()
        return queue

    queue = asyncio.run(scenario())
    assert queue.stats["sent"] >= 2 or queue.stats["queued"] >= 2
    assert sender.dry_run is True


def test_no_trade_when_a_feed_is_unhealthy(cfg, instrument):
    from app.core.models import FeedHealth, FeedState

    feeds = {
        "binance_rest": FeedHealth("binance_rest", FeedState.STALE, 0, 9000),
        "binance_ws": FeedHealth("binance_ws", FeedState.HEALTHY, 0, 100),
        "coindcx_rest": FeedHealth("coindcx_rest", FeedState.HEALTHY, 0, 500),
        "news": FeedHealth("news", FeedState.HEALTHY, 0, 1000),
    }
    engine = _engine(cfg, StubRegistry(_two_group_candidates()))
    decision = engine.generate(snap=_snapshot(cfg, instrument, feeds=feeds), price=100.0)
    assert decision.signal is None
    assert decision.no_trade is not None and "unhealthy feeds" in decision.no_trade.reason


def test_no_trade_when_news_is_blocked(cfg, instrument):
    engine = _engine(cfg, StubRegistry(_two_group_candidates()))
    decision = engine.generate(
        snap=_snapshot(cfg, instrument, news_state=NewsState.BLOCK),
        price=100.0,
        blackout_active=True,
        blocking_headline="exchange hack",
    )
    assert decision.signal is None
    assert "news state BLOCK" in decision.no_trade.reason


def test_no_trade_on_extreme_divergence_even_with_full_consensus(cfg, instrument):
    engine = _engine(cfg, StubRegistry(_two_group_candidates(confidence=0.95)))
    snap = _snapshot(cfg, instrument, basis=make_basis(binance_mid=100.0, coindcx_mid=100.5, z=3.7))
    decision = engine.generate(snap=snap, price=100.0)
    assert decision.signal is None
    assert decision.veto is not None and decision.veto.blocked
    assert "HARD BLOCK" in decision.no_trade.reason


def test_no_trade_when_engines_conflict_with_equal_support(cfg, instrument):
    candidates = (
        _candidate(
            "S1",
            "MICRO_LIQUIDITY",
            direction=Direction.LONG,
            entry=100.0,
            invalidation=98.5,
            stop=98.0,
            confidence=0.7,
        ),
        _candidate(
            "S4",
            "TREND_DERIVATIVES",
            direction=Direction.SHORT,
            entry=100.0,
            invalidation=101.5,
            stop=102.0,
            confidence=0.7,
        ),
    )
    engine = _engine(cfg, StubRegistry(candidates))
    decision = engine.generate(snap=_snapshot(cfg, instrument), price=100.0)
    assert decision.signal is None
    assert "conflict" in decision.no_trade.reason


def test_no_trade_when_only_one_engine_agrees(cfg, instrument):
    candidates = (
        _candidate(
            "S1",
            "MICRO_LIQUIDITY",
            direction=Direction.LONG,
            entry=100.0,
            invalidation=98.5,
            stop=98.0,
            confidence=0.9,
        ),
    )
    engine = _engine(cfg, StubRegistry(candidates))
    decision = engine.generate(snap=_snapshot(cfg, instrument), price=100.0)
    assert decision.signal is None
    assert "minimum" in decision.no_trade.reason or "agree" in decision.no_trade.reason


def test_no_trade_when_the_daily_cap_is_reached(cfg, instrument):
    risk_engine = RiskEngine(cfg)
    for index in range(cfg.risk.max_daily_signals):
        risk_engine.register(symbol=f"B-X{index}_USDT", group="G", now_ms=1_700_000_000_000)
    engine = _engine(cfg, StubRegistry(_two_group_candidates()), risk_engine=risk_engine)
    decision = engine.generate(
        snap=_snapshot(cfg, instrument), price=100.0, reference_ms=1_700_000_000_000
    )
    assert decision.signal is None
    assert "cap" in decision.no_trade.reason or "concurrent" in decision.no_trade.reason


def test_no_trade_when_btc_regime_filter_blocks(cfg, instrument):
    engine = _engine(cfg, StubRegistry(_two_group_candidates()))
    decision = engine.generate(snap=_snapshot(cfg, instrument), price=100.0, btc_block=True)
    assert decision.signal is None
    assert "BTC regime" in decision.no_trade.reason


def test_no_trade_when_normalization_is_unavailable(cfg, instrument):
    engine = _engine(cfg, StubRegistry(_two_group_candidates()))
    decision = engine.generate(snap=_snapshot(cfg, instrument, basis=None), price=100.0)
    assert decision.signal is None
    assert "normalization" in decision.no_trade.reason


def test_risk_engine_blocks_too_small_rr(cfg, instrument):
    risk_engine = RiskEngine(cfg)
    decision = risk_engine.check(
        symbol=PAIR, group="G", grade=Grade.A, rr_tp2=1.1, now_ms=1_700_000_000_000
    )
    assert not decision.allowed and "R:R" in decision.reason


def test_journal_round_trip_allows_full_reconstruction(cfg, instrument, tmp_path):
    repository = JournalRepository(
        sqlite_path=str(tmp_path / "j.sqlite"), jsonl_dir=str(tmp_path), jsonl_mirror=True
    )
    engine = _engine(cfg, StubRegistry(_two_group_candidates()))
    decision = engine.generate(snap=_snapshot(cfg, instrument), price=100.0)
    repository.record_signal(decision.signal.to_row())
    repository.record_vetoes(
        [
            {
                "ts": decision.signal.created_ms,
                "symbol": PAIR,
                "guard": "G6",
                "severity": "DEGRADE",
                "reason": "test",
                "evidence": {"a": 1},
            }
        ]
    )
    record = repository.fetch_signal(decision.signal.signal_id)
    assert record is not None
    reconstructed = repository.reconstruct_signal(decision.signal.signal_id)
    assert reconstructed is not None
    assert reconstructed["signal"]["signal_id"] == decision.signal.signal_id
    assert reconstructed["vetoes"][0]["guard"] == "G6"
    assert reconstructed["signal"]["reason"]
    repository.close()


def test_lifecycle_expires_a_signal_whose_zone_is_never_touched(cfg, instrument):
    engine = _engine(cfg, StubRegistry(_two_group_candidates()))
    decision = engine.generate(snap=_snapshot(cfg, instrument), price=100.0)
    lifecycle = SignalLifecycle(danger_monitor=DangerMonitor(cfg))
    lifecycle.add(decision.signal)
    snap = _snapshot(cfg, instrument)
    updates = lifecycle.update(snap=snap, price=120.0)  # ran away, never fills
    assert updates[0].state in (SignalState.PENDING, SignalState.EXPIRED)
    updates = lifecycle.update(snap=snap, price=120.0, reference_ms=decision.signal.expiry_ms + 1)
    assert updates[0].state is SignalState.EXPIRED
    assert lifecycle.live_signals() == ()


def test_danger_monitor_flags_thesis_invalidation(cfg, instrument):
    engine = _engine(cfg, StubRegistry(_two_group_candidates()))
    decision = engine.generate(snap=_snapshot(cfg, instrument), price=100.0)
    signal = decision.signal
    signal.state = SignalState.ACTIVE
    monitor = DangerMonitor(cfg)
    snap = _snapshot(cfg, instrument)
    assessment = monitor.assess(signal=signal, snap=snap, price=signal.invalidation - 1.0)
    assert assessment.level is DangerLevel.EMERGENCY
    assert assessment.alert is not None
    text = MessageFormatter(cfg).danger(assessment.alert)
    assert "CLOSE / REDUCE / EXIT MANUALLY" in text
    assert "NO AUTO-CLOSE. MANUAL ACTION REQUIRED." in text


def test_synthetic_harness_snapshot_is_well_formed(cfg, instrument):
    series = synthetic_candles(400, seed=5)
    factory = SyntheticSnapshotFactory(cfg=cfg, series=series, instrument=instrument)
    snap = factory(PAIR, 300)
    assert snap is not None
    assert snap.binance_book.is_valid and snap.coindcx_book.is_valid
    assert snap.basis is not None
    assert set(snap.feed_health) == {"binance_rest", "binance_ws", "coindcx_rest", "news"}
