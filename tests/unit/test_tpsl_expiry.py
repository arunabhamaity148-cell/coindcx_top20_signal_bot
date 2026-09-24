"""TP/SL, expiry and rounding tests (acceptance tests 8 and 9)."""

from __future__ import annotations

import pytest

from app.core.errors import FailClosedError
from app.core.models import Direction, SignalState
from app.signals.expiry import EntryVerdict, evaluate_entry, expiry_timestamp, next_state
from app.signals.tpsl import apply_tp1_clamp, build_tp_sl, management_plan
from app.utils.rounding import floor_price, qty_for_notional, round_price, round_qty


def test_round_price_snaps_to_the_venue_tick():
    assert round_price(86_123.4567, 0.01) == pytest.approx(86_123.46)
    assert round_price(86_123.4567, 0.1) == pytest.approx(86_123.5)
    assert round_price(86_123.4567, 1.0) == pytest.approx(86_123.0)
    assert floor_price(86_123.4567, 0.01) == pytest.approx(86_123.45)


def test_round_qty_never_rounds_up():
    assert round_qty(3.9, 1.0) == pytest.approx(3.0)


def test_advisory_qty_respects_min_notional_and_step():
    qty = qty_for_notional(1.0, price=86_000.0, increment=1.0, min_trade_size=1.0, min_notional=6.0)
    assert qty * 86_000.0 >= 6.0


def test_long_ladder_is_monotonic_and_snapped():
    plan = build_tp_sl(
        direction=Direction.LONG,
        entry=86_120.0,
        invalidation=85_900.0,
        atr=200.0,
        tick=0.01,
        sl_atr_buffer=0.5,
        risk_floor_atr_mult=0.25,
    )
    assert plan.stop_loss < plan.entry < plan.tps[0] < plan.tps[1] < plan.tps[2] < plan.tps[3]
    assert plan.rr_tp2 == pytest.approx(2.0, abs=1e-6)


def test_short_ladder_is_monotonic():
    plan = build_tp_sl(
        direction=Direction.SHORT,
        entry=86_120.0,
        invalidation=86_340.0,
        atr=200.0,
        tick=0.01,
        sl_atr_buffer=0.5,
        risk_floor_atr_mult=0.25,
    )
    assert plan.stop_loss > plan.entry > plan.tps[0] > plan.tps[1] > plan.tps[2] > plan.tps[3]


def test_risk_floor_prevents_absurd_r_multiples():
    # a 5-tick stop would otherwise manufacture a 100R TP2
    plan = build_tp_sl(
        direction=Direction.LONG,
        entry=100.0,
        invalidation=99.95,
        atr=4.0,
        tick=0.01,
        sl_atr_buffer=0.0,
        risk_floor_atr_mult=0.25,
    )
    assert plan.atr_floor_applied is True
    assert plan.risk == pytest.approx(1.0, abs=0.02)
    assert plan.rr_tp2 == pytest.approx(2.0, abs=0.02)


def test_impossible_ladder_raises():
    with pytest.raises(FailClosedError):
        build_tp_sl(
            direction=Direction.LONG,
            entry=100.0,
            invalidation=200.0,
            atr=1.0,
            tick=0.01,
            sl_atr_buffer=0.0,
            risk_floor_atr_mult=0.25,
        )
    with pytest.raises(FailClosedError):
        build_tp_sl(
            direction=Direction.LONG,
            entry=100.0,
            invalidation=99.0,
            atr=0.0,
            tick=0.01,
            sl_atr_buffer=0.5,
            risk_floor_atr_mult=0.25,
        )


def test_tp1_clamp_keeps_r_positive():
    plan = build_tp_sl(
        direction=Direction.LONG,
        entry=100.0,
        invalidation=98.0,
        atr=2.0,
        tick=0.01,
        sl_atr_buffer=0.5,
        risk_floor_atr_mult=0.25,
    )
    clamped = apply_tp1_clamp(plan, direction=Direction.LONG, clamp_level=101.0, tick=0.01)
    assert clamped.tps[0] == pytest.approx(101.0)
    with pytest.raises(FailClosedError):
        apply_tp1_clamp(plan, direction=Direction.LONG, clamp_level=99.0, tick=0.01)


def test_expiry_and_entry_verdicts():
    now = 1_700_000_000_000
    expiry = expiry_timestamp(now, 45)
    assert expiry == now + 45 * 60_000

    fillable = evaluate_entry(
        direction=Direction.LONG,
        price=100.0,
        zone_low=99.5,
        zone_high=100.5,
        invalidation=98.0,
        created_ms=now,
        expiry_ms=expiry,
        now_ms=now,
        atr=2.0,
    )
    assert fillable.verdict is EntryVerdict.FILLABLE
    assert not fillable.must_expire

    expired = evaluate_entry(
        direction=Direction.LONG,
        price=100.0,
        zone_low=99.5,
        zone_high=100.5,
        invalidation=98.0,
        created_ms=now,
        expiry_ms=expiry,
        now_ms=expiry + 1,
        atr=2.0,
    )
    assert expired.verdict is EntryVerdict.EXPIRED and expired.must_expire

    invalidated = evaluate_entry(
        direction=Direction.LONG,
        price=97.0,
        zone_low=99.5,
        zone_high=100.5,
        invalidation=98.0,
        created_ms=now,
        expiry_ms=expiry,
        now_ms=now,
        atr=2.0,
    )
    assert invalidated.verdict is EntryVerdict.INVALIDATED

    ran_away = evaluate_entry(
        direction=Direction.LONG,
        price=105.0,
        zone_low=99.5,
        zone_high=100.5,
        invalidation=98.0,
        created_ms=now,
        expiry_ms=expiry,
        now_ms=now,
        atr=2.0,
    )
    assert ran_away.verdict is EntryVerdict.WAITING
    assert not ran_away.fillable  # no fill beyond the zone, ever


def test_state_transitions():
    now = 1_700_000_000_000
    expiry = expiry_timestamp(now, 45)
    check = evaluate_entry(
        direction=Direction.LONG,
        price=100.0,
        zone_low=99.5,
        zone_high=100.5,
        invalidation=98.0,
        created_ms=now,
        expiry_ms=expiry,
        now_ms=now,
        atr=2.0,
    )
    assert next_state(SignalState.PENDING, check) is SignalState.ACTIVE
    expired = evaluate_entry(
        direction=Direction.LONG,
        price=100.0,
        zone_low=99.5,
        zone_high=100.5,
        invalidation=98.0,
        created_ms=now,
        expiry_ms=expiry,
        now_ms=expiry + 1,
        atr=2.0,
    )
    assert next_state(SignalState.PENDING, expired) is SignalState.EXPIRED


def test_management_plan_is_manual_only():
    plan = " ".join(management_plan()).lower()
    assert "no auto-close" in plan
