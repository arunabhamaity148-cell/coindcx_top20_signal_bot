"""Signal engine: consensus -> veto -> risk -> grade -> limit signal (master §12).

Order of operations is fixed by the specification:
    NEWS -> MACRO -> BTC -> DATA -> NORMALIZATION -> LIQUIDITY -> DERIVATIVES ->
    STRUCTURE -> STRATEGIES -> CONSENSUS -> VETO (hard block) -> RISK -> GRADING ->
    LIMIT ENTRY(SL/TP1-4) -> TELEGRAM
Anything that fails on the way becomes NO TRADE, and that outcome is journalled.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.config import AppConfig
from app.core.errors import FailClosedError
from app.core.mathx import atr
from app.core.models import (
    Direction,
    FeedState,
    Grade,
    MarketSnapshot,
    NewsState,
    SignalState,
    StrategyCandidate,
    VetoSeverity,
)
from app.core.timeutils import now_ms
from app.risk.consensus import ConsensusEngine, ConsensusResult
from app.risk.risk_engine import RiskEngine
from app.risk.veto_engine import VetoEngine, VetoOutcome
from app.signals.models import NoTradeRecord, Signal
from app.signals.tpsl import TpSlPlan, plan_from_candidate
from app.strategies.registry import StrategyRegistry


@dataclass
class SignalDecision:
    signal: Signal | None
    consensus: ConsensusResult
    veto: VetoOutcome | None
    no_trade: NoTradeRecord | None = None
    plan: TpSlPlan | None = None
    strategy_failures: dict[str, str] = field(default_factory=dict)
    candidates: tuple[StrategyCandidate, ...] = ()


@dataclass
class SignalEngine:
    cfg: AppConfig
    registry: StrategyRegistry
    veto_engine: VetoEngine
    consensus_engine: ConsensusEngine
    risk_engine: RiskEngine

    # ------------------------------------------------------------------ entry point
    def generate(
        self,
        *,
        snap: MarketSnapshot,
        price: float | None = None,
        realized_vol: float | None = None,
        atr_pct_rank: float | None = None,
        blackout_active: bool = False,
        blocking_headline: str = "",
        news_blocked_for_pair: bool = False,
        live_signals: Sequence[str] = (),
        btc_block: bool = False,
        btc_degrade: bool = False,
        reference_ms: int | None = None,
    ) -> SignalDecision:
        started = reference_ms or now_ms()
        price = price if price is not None else snap.last_price

        def no_trade(reason: str) -> SignalDecision:
            return SignalDecision(
                signal=None,
                consensus=ConsensusResult(None, Grade.NO_TRADE, 0.0, reason=reason),
                veto=None,
                no_trade=NoTradeRecord(
                    symbol=snap.symbol,
                    reason=reason,
                    feed_status={n: h.state.value for n, h in snap.feed_health.items()},
                    ts_ms=started,
                ),
            )

        # fail-closed: unhealthy feeds, BLOCK news, BTC veto
        unhealthy = [n for n, h in snap.feed_health.items() if h.state is not FeedState.HEALTHY]
        if unhealthy:
            return no_trade(f"unhealthy feeds {unhealthy} - NO TRADE (fail-closed)")
        if snap.news_state is NewsState.BLOCK:
            return no_trade(f"news state BLOCK - NO TRADE ({blocking_headline[:80]})")
        if news_blocked_for_pair:
            return no_trade(f"news block - NO TRADE ({blocking_headline[:80]})")
        if btc_block:
            return no_trade(f"BTC regime filter blocked ({snap.btc_regime.value}) - NO TRADE")
        if snap.basis is None:
            return no_trade("cross-venue normalization unavailable - NO TRADE (fail-closed)")

        candidates, failures = self.registry.run(snap)
        consensus = self.consensus_engine.evaluate(candidates, degraded=btc_degrade)
        if not consensus.tradeable or consensus.direction is None:
            return SignalDecision(
                signal=None,
                consensus=consensus,
                veto=None,
                no_trade=NoTradeRecord(symbol=snap.symbol, reason=consensus.reason, ts_ms=started),
                strategy_failures=failures,
                candidates=candidates,
            )

        representative = _representative(
            candidates, consensus.direction, consensus.agreeing_strategies
        )
        if representative is None:
            return SignalDecision(
                signal=None,
                consensus=consensus,
                veto=None,
                no_trade=NoTradeRecord(
                    symbol=snap.symbol, reason="no representative plan", ts_ms=started
                ),
                strategy_failures=failures,
                candidates=candidates,
            )

        veto = self.veto_engine.run(
            snap,
            candidate=representative,
            realized_vol=realized_vol,
            atr_pct_rank=atr_pct_rank,
            blackout_active=blackout_active,
            blocking_headline=blocking_headline,
            live_signals=live_signals,
            price=price,
        )
        if veto.blocked:
            return SignalDecision(
                signal=None,
                consensus=consensus,
                veto=veto,
                no_trade=NoTradeRecord(
                    symbol=snap.symbol, reason=f"HARD BLOCK {veto.summary()}", ts_ms=started
                ),
                strategy_failures=failures,
                candidates=candidates,
            )

        # ---- Preserve the selected strategy's own LevelPlan verbatim.
        # The previous implementation rebuilt TP/SL generically here, which could
        # silently erase S1/S2/S3/S4/S5-specific entries, invalidations and targets.
        series = list(snap.series("5m")) or list(snap.series("1m"))
        atr_value = representative.atr or (atr(series, self.cfg.strategy.atr_period) if series else None)
        if not atr_value:
            return no_trade("ATR unavailable - NO TRADE")
        try:
            plan = plan_from_candidate(
                representative,
                tick=snap.instrument.price_increment,
            )
        except FailClosedError as exc:
            return no_trade(f"strategy level plan invalid: {exc}")

        # Preserve the strategy-selected expiry, while refusing anything beyond the
        # risk-tier maximum. This prevents grading from silently replacing S1/S2/S3/S4/S5
        # decay assumptions.
        risk_decision = self.risk_engine.check(
            symbol=snap.symbol,
            group=representative.correlation_group,
            grade=consensus.grade,
            rr_tp2=plan.rr_tp2,
            now_ms=started,
            requested_expiry_min=representative.expiry_min,
        )
        if not risk_decision.allowed:
            return SignalDecision(
                signal=None,
                consensus=consensus,
                veto=veto,
                no_trade=NoTradeRecord(
                    symbol=snap.symbol,
                    reason=f"risk check failed: {risk_decision.reason}",
                    ts_ms=started,
                ),
                plan=plan,
                strategy_failures=failures,
                candidates=candidates,
            )
        expiry_min = risk_decision.expiry_min

        confidence = consensus.confidence
        if veto.degraded:
            confidence = max(0.0, confidence - veto.confidence_penalty)

        sizing = self.risk_engine.advisory_size(
            price=plan.entry,
            stop_distance=plan.risk,
            instrument=snap.instrument,
            multiplier=risk_decision.position_multiplier,
        )
        if not sizing["honours_minimums"]:
            return no_trade(
                "advisory size would violate CoinDCX minimums without exceeding the configured "
                "cash-at-stop risk budget - NO TRADE"
            )
        signal = Signal(
            signal_id=Signal.new_id(started),
            symbol=snap.symbol,
            direction=representative.direction,
            grade=consensus.grade,
            confidence=confidence,
            entry_price=plan.entry,
            entry_zone_low=representative.entry_zone_low,
            entry_zone_high=representative.entry_zone_high,
            stop_loss=plan.stop_loss,
            tp1=plan.tps[0],
            tp2=plan.tps[1],
            tp3=plan.tps[2],
            tp4=plan.tps[3],
            invalidation=plan.invalidation,
            risk=plan.risk,
            rr_tp2=plan.rr_tp2,
            expiry_ms=started + expiry_min * 60_000,
            created_ms=started,
            strategy_votes=dict(consensus.contributions),
            veto_status=VetoSeverity.DEGRADE if veto.degraded else VetoSeverity.PASS,
            veto_detail=veto.summary(),
            news_state=snap.news_state,
            news_source=(blocking_headline or "none"),
            reasons=representative.reasons,
            atr=atr_value,
            binance_price=snap.binance_book.mid or 0.0,
            coindcx_price=snap.coindcx_book.mid or 0.0,
            spread_bps=snap.liquidity.spread_bps,
            basis_bps=snap.basis.basis_bps if snap.basis else 0.0,
            state=SignalState.PENDING,
            advisory_qty=sizing["advisory_qty"],
            advisory_notional=sizing["advisory_notional_usdt"],
            meta={
                "agreeing_groups": list(consensus.agreeing_groups),
                "strategy": representative.strategy_id,
                "expiry_min": expiry_min,
                "position_multiplier": risk_decision.position_multiplier,
                "sizing_model": sizing["sizing_model"],
                "cash_at_stop_risk_usdt": sizing["cash_at_stop_risk_usdt"],
                "evidence_channels": list(representative.evidence_channels),
            },
            latency_ms=now_ms() - started,
        )
        self.risk_engine.register(
            symbol=snap.symbol, group=representative.correlation_group, now_ms=started
        )
        return SignalDecision(
            signal=signal,
            consensus=consensus,
            veto=veto,
            plan=plan,
            strategy_failures=failures,
            candidates=candidates,
        )

    def why_bullets(self, signal: Signal, limit: int = 3) -> list[str]:
        """WHY bullets: measured observations only, never adjectives (spec §T)."""
        bullets: list[str] = []
        for reason in signal.reasons:
            if len(bullets) >= limit:
                break
            text = str(reason).strip()
            if text and text not in bullets:
                bullets.append(text)
        if not bullets:
            bullets.append(
                f"confidence {signal.confidence:.2f} from {len(signal.strategy_votes)} engine(s)"
            )
        return bullets[:limit]


def _representative(
    candidates: Sequence[StrategyCandidate], direction: Direction, agreeing: Sequence[str]
) -> StrategyCandidate | None:
    pool = [c for c in candidates if c.direction is direction and c.strategy_id in set(agreeing)]
    if not pool:
        pool = [c for c in candidates if c.direction is direction]
    if not pool:
        return None
    return max(pool, key=lambda c: c.confidence)


__all__ = ["SignalDecision", "SignalEngine"]
