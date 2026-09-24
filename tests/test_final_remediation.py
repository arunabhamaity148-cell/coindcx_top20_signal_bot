from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.backtest.engine import BacktestEngine
from app.backtest.fills import FillOutcome, FillResult
from app.backtest.costs import CostModel
from app.bot import SignalBot
from app.core.models import Candle, Direction
from app.data.binance.client import BinanceDataClient
from app.data.binance.websocket import StreamRoute, build_default_streams
from app.data.coindcx.rest import CoinDCXPoller
from app.strategies.s2_volatility_compression import VolatilityCompressionBreakout
from app.strategies.s3_funding_crowding import FundingCrowdingExhaustion
from app.strategies.s4_oi_trend import OIConfirmedTrendContinuation
from tests.conftest import SnapshotParts, candle, make_basis, make_candles, make_snapshot
from tests.strategies.test_strategies import _deriv


def test_strategy_level_plan_and_expiry_survive_engine(cfg, instrument):
    from app.core.models import Direction, StrategyCandidate
    from app.risk.consensus import ConsensusEngine
    from app.risk.risk_engine import RiskEngine
    from app.risk.veto_engine import VetoEngine
    from app.signals.signal_engine import SignalEngine

    candidate = StrategyCandidate(
        strategy_id="S1", symbol="B-BTC_USDT", direction=Direction.LONG, confidence=0.90,
        entry_price=100.0, entry_zone_low=99.5, entry_zone_high=100.5,
        invalidation=98.5, stop_loss=98.0, tp1=104.0, tp2=106.0, tp3=108.0, tp4=110.0,
        rr_tp2=3.0, atr=2.0, expiry_min=45,
        reasons=("measured observation 1",), correlation_group="G1",
    )
    # Use two candidates in distinct consensus groups.
    c2 = StrategyCandidate(**{**candidate.__dict__, "strategy_id":"S4", "correlation_group":"G2"})
    class Registry2:
        def run(self, snap): return (candidate, c2), {}
        def ids(self): return ("S1", "S4")
    engine = SignalEngine(cfg, Registry2(), VetoEngine(cfg), ConsensusEngine(cfg), RiskEngine(cfg))
    snap = make_snapshot(SnapshotParts(candles={"5m": make_candles([100.0]*100)}, binance_mid=100.0, basis=make_basis(binance_mid=100.0)), instrument=instrument)
    d = engine.generate(snap=snap, reference_ms=1_700_000_000_000)
    assert d.signal is not None
    s = d.signal
    assert (s.entry_price, s.entry_zone_low, s.entry_zone_high) == (100.0, 99.5, 100.5)
    assert (s.stop_loss, s.tp1, s.tp2, s.tp3, s.tp4) == (98.0, 104.0, 106.0, 108.0, 110.0)
    assert s.expiry_ms - s.created_ms == 45 * 60_000


def test_s2_missing_taker_flow_is_rejected(cfg, instrument):
    compression = make_candles([100.0] * 200, spread=0.2)
    trigger = make_candles([100.0] * 199, spread=0.2, taker_ratio=0.5)
    expansion = candle(open=100.0, high=100.9, low=100.0, close=100.9, taker_ratio=None,
                        open_time_ms=trigger[-1].open_time_ms + 60_000)
    snap = make_snapshot(SnapshotParts(candles={"15m": compression, "5m": [*trigger, expansion]},
                                       binance_mid=100.9, derivatives=_deriv(oi_chg_pct=1.5, mark_price=100.9),
                                       basis=make_basis(binance_mid=100.9)), instrument=instrument)
    assert VolatilityCompressionBreakout(cfg).analyze(snap) is None


def test_s3_1h_context_blocks_countertrend_trigger(cfg, instrument):
    context = make_candles([100.0 + i * 0.3 for i in range(80)], spread=0.2, bar_ms=60 * 60_000)
    trigger = make_candles([124.0] * 79, spread=0.2)
    failure = candle(open=124.0, high=124.2, low=123.0, close=123.1, taker_ratio=0.40,
                      open_time_ms=trigger[-1].open_time_ms + 60_000)
    snap = make_snapshot(SnapshotParts(candles={"1h": context, "5m": [*trigger, failure]},
                                       binance_mid=123.1,
                                       derivatives=_deriv(funding_z=2.6, oi_pct_rank=0.92, mark_price=123.1),
                                       basis=make_basis(binance_mid=123.1)), instrument=instrument)
    assert FundingCrowdingExhaustion(cfg).analyze(snap) is None


def test_s4_missing_taker_flow_is_rejected(cfg, instrument):
    closes, trend = ([100.0] * 60 + [100.0 + 0.1 * i for i in range(1, 41)], None)
    trend = make_candles(closes, spread=0.4, bar_ms=4 * 60 * 60_000)
    pullback = make_candles([103.0] * 56 + [102.8, 102.9, 103.0, 103.2], spread=0.2, bar_ms=15 * 60_000)
    trigger = make_candles([103.1] * 80, spread=0.2)
    snap = make_snapshot(SnapshotParts(candles={"4h": trend, "15m": pullback, "5m": trigger},
                                       binance_mid=103.1, derivatives=_deriv(oi_chg_pct=1.0, taker_buy_sell_ratio=None),
                                       basis=make_basis(binance_mid=103.1)), instrument=instrument)
    assert OIConfirmedTrendContinuation(cfg).analyze(snap) is None


def test_s4_requires_15m_pullback(cfg, instrument):
    closes = [100.0] * 60 + [100.0 + 0.1 * i for i in range(1, 41)]
    trend = make_candles(closes, spread=0.4, bar_ms=4 * 60 * 60_000)
    pullback = make_candles([103.0 + 0.01 * i for i in range(60)], spread=0.01, bar_ms=15 * 60_000)
    trigger = make_candles([103.10] * 80, spread=0.2)
    snap = make_snapshot(SnapshotParts(candles={"4h": trend, "15m": pullback, "5m": trigger},
                                       binance_mid=103.05, derivatives=_deriv(oi_chg_pct=1.0, taker_buy_sell_ratio=0.58),
                                       basis=make_basis(binance_mid=103.05)), instrument=instrument)
    assert OIConfirmedTrendContinuation(cfg).analyze(snap) is None


def test_s5_requires_expected_convergence_edge_to_exceed_cost_buffer(cfg, instrument):
    from app.strategies.s5_basis_convergence import CrossVenueBasisConvergence
    # Net basis is positive, but the raw convergence edge is only 2.7 bps while
    # the configured effective cost is 2.6 bps plus the 2 bps adverse-selection buffer.
    basis = make_basis(binance_mid=100.0, coindcx_mid=100.027, z=2.5, net=0.1)
    trigger = make_candles([100.027] * 60, spread=0.01)
    snap = make_snapshot(SnapshotParts(candles={"1m": trigger, "5m": trigger},
                                       binance_mid=100.0, coindcx_mid=100.027, basis=basis),
                         instrument=instrument)
    assert CrossVenueBasisConvergence(cfg).analyze(snap) is None


def test_binance_ws_message_updates_book_and_kline_state(cfg):
    client = BinanceDataClient(cfg.exchanges.binance)
    client._on_ws_message("btcusdt@depth20@100ms", {"E": 1700000000123, "s":"BTCUSDT",
        "b":[["100.0","10"]], "a":[["100.2","10"]]})
    book = client.cached_book("BTCUSDT")
    assert book is not None and book.mid == pytest.approx(100.1)
    for i in range(241):
        open_ts = 1699992000000 + i * 60000
        close_ts = open_ts + 59999
        client._on_ws_message("btcusdt@kline_1m", {
            "E": close_ts,
            "s": "BTCUSDT",
            "k": {
                "t": open_ts,
                "o": "100",
                "h": "101",
                "l": "99",
                "c": "100.5",
                "q": "1000",
                "v": "10",
                "Q": "520",
                "x": True,
                "T": close_ts,
            },
        })

    assert client.cached_klines("BTCUSDT", "1m")
    assert client.cached_klines("BTCUSDT", "5m")
    assert client.cached_klines("BTCUSDT", "15m")
    assert client.cached_klines("BTCUSDT", "1h")
    assert client.cached_klines("BTCUSDT", "4h")
    assert client.cached_klines("BTCUSDT", "1m")[-1].taker_buy_ratio == pytest.approx(0.52)


def test_default_ws_subscription_covers_all_20_pairs():
    symbols = [f"COIN{i}USDT" for i in range(20)]
    routes = build_default_streams(symbols)
    assert len(routes[StreamRoute.PUBLIC]) == 20
    assert len(routes[StreamRoute.MARKET]) == 60
    assert {s.split("@")[0].upper() for s in routes[StreamRoute.PUBLIC]} == set(symbols)
    assert {s.split("@")[0].upper() for s in routes[StreamRoute.MARKET]} == set(symbols)


@pytest.mark.asyncio
async def test_boot_is_idempotent_without_second_startup_side_effect(cfg, monkeypatch):
    bot = SignalBot(cfg)
    sentinel = object()
    bot._booted = True
    bot._boot_report = sentinel
    async def should_not_run():
        raise AssertionError("boot side effects repeated")
    monkeypatch.setattr(bot.binance, "start", should_not_run)
    assert await bot.boot() is sentinel


@pytest.mark.asyncio
async def test_coindcx_polling_respects_bounded_concurrency():
    from app.core.models import BookLevel, OrderBook
    class FakeClient:
        def __init__(self): self.active = 0; self.max_active = 0
        async def orderbook(self, pair):
            self.active += 1; self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return OrderBook("COINDCX", pair, 1700000000000,
                             [BookLevel(100, 10)], [BookLevel(100.2, 10)])
    fake = FakeClient()
    pairs = tuple(f"B-C{i}_USDT" for i in range(20))
    poller = CoinDCXPoller(fake, pairs, max_concurrency=5)
    await poller.poll_once()
    assert fake.max_active <= 5
    assert len(poller.books) == 20


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_backtest_models_partial_tp_ladder_and_be(cfg, instrument, direction):
    class Fill:
        def simulate(self, **kwargs):
            return FillResult(FillOutcome.FILLED, bar_index=0, fill_price=100.0, probability=1.0)
    engine = BacktestEngine(cfg, signal_engine=object(), snapshot_factory=lambda *_: None,
                            cost_model=CostModel(maker_fee_pct=0, taker_fee_pct=0, slippage_bps=0, spread_cost_bps=0),
                            fill_model=Fill(), warmup_bars=0)
    if direction is Direction.LONG:
        sig = SimpleNamespace(signal_id="T", symbol="B-BTC_USDT", direction=direction, entry_price=100,
                              entry_zone_low=99, entry_zone_high=101, invalidation=99, stop_loss=99,
                              tp1=101, tp2=102, tp3=103, tp4=105, atr=1, expiry_ms=900000, created_ms=0,
                              meta={"strategy":"S1"})
        bars = [Candle(i, 100, 100 + r, 100.2, 100 + r - 0.1, 1000, 500) for i,r in enumerate([1.1,2.1,3.1,5.1])]
    else:
        sig = SimpleNamespace(signal_id="T", symbol="B-BTC_USDT", direction=direction, entry_price=100,
                              entry_zone_low=99, entry_zone_high=101, invalidation=101, stop_loss=101,
                              tp1=99, tp2=98, tp3=97, tp4=95, atr=1, expiry_ms=900000, created_ms=0,
                              meta={"strategy":"S1"})
        bars = [Candle(i, 100, 99.8, 100 - r + 0.1, 100 - r, 1000, 500) for i,r in enumerate([1.1,2.1,3.1,5.1])]
    result = engine._simulate(signal=sig, bars=bars, index=0, regime="RANGE", bar_minutes=5, rng=__import__('random').Random(1))
    assert result.tp_hits == (1,2,3,4)
    assert result.gross_r == pytest.approx(2.1)


def test_structured_logger_preserves_multi_argument_formatting(caplog):
    import logging
    from app.core.logging_setup import _RedactingFilter
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "soak cycle %s failed: %s", (7, RuntimeError("boom")), None)
    assert _RedactingFilter().filter(record)
    assert record.getMessage() == "soak cycle 7 failed: boom"


def test_soak_script_uses_existing_journal_api_for_no_trade_rows():
    from pathlib import Path
    source = Path("scripts/soak_test.py").read_text(encoding="utf-8")
    assert "record_no_trade" not in source
    assert 'record_error("no_trade"' in source


def test_s4_level_plan_uses_slow_ema_invalidation_and_documented_stop(cfg, instrument):
    closes = [100.0] * 60 + [100.0 + 0.1 * i for i in range(1, 41)]
    trend = make_candles(closes, spread=0.4, bar_ms=4 * 60 * 60_000)
    context = make_candles([102.0] * 60 + [102.2 + 0.05 * i for i in range(1, 21)], spread=0.4, bar_ms=60 * 60_000)
    pullback = make_candles([103.0] * 56 + [102.8, 102.9, 103.0, 103.2], spread=0.3, bar_ms=15 * 60_000)
    trigger = make_candles([103.1] * 79, spread=0.3)
    snap = make_snapshot(SnapshotParts(candles={"4h": trend, "1h": context, "15m": pullback, "5m": trigger},
                                       binance_mid=103.1, derivatives=_deriv(oi_chg_pct=1.0, taker_buy_sell_ratio=0.58),
                                       basis=make_basis(binance_mid=103.1)), instrument=instrument)
    cand = OIConfirmedTrendContinuation(cfg).analyze(snap)
    assert cand is not None
    e_fast = __import__("app.core.mathx", fromlist=["ema_last"]).ema_last([c.close for c in trend], 21)
    e_slow = __import__("app.core.mathx", fromlist=["ema_last"]).ema_last([c.close for c in trend], 55)
    atr_value = OIConfirmedTrendContinuation(cfg).atr_of(trigger)
    assert e_fast is not None and e_slow is not None and atr_value is not None
    expected_stop = min(trigger[-1].low, e_fast - 0.6 * atr_value) - 0.2 * atr_value
    assert cand.invalidation == __import__("app.utils.rounding", fromlist=["round_price"]).round_price(e_slow, instrument.price_increment)
    assert cand.stop_loss == __import__("app.utils.rounding", fromlist=["round_price"]).round_price(expected_stop, instrument.price_increment)


def test_closed_candle_parser_rejects_forming_rest_kline():
    from app.data.binance.models import parse_kline
    row = [1_700_000_000_000, "100", "101", "99", "100.5", "0", 1_700_000_060_000, "1000", "0", "10", "520", "0"]
    candle = parse_kline(row, reference_ms=1_700_000_059_999)
    assert candle.is_closed is False
    closed = parse_kline(row, reference_ms=1_700_000_060_000)
    assert closed.is_closed is True


def test_consensus_requires_independent_groups_for_minimum_agreement(cfg):
    from app.core.models import Direction, StrategyCandidate
    from app.risk.consensus import ConsensusEngine
    base = dict(symbol="B-BTC_USDT", direction=Direction.LONG, confidence=0.8, entry_price=100, entry_zone_low=99, entry_zone_high=101,
                invalidation=98, stop_loss=97, tp1=103, tp2=105, tp3=107, tp4=110, rr_tp2=2.5, atr=1, expiry_min=45, reasons=("x",))
    a = StrategyCandidate(strategy_id="S1", correlation_group="MICRO_LIQUIDITY", evidence_channels=("PRICE_STRUCTURE",), **base)
    b = StrategyCandidate(strategy_id="S2", correlation_group="MICRO_LIQUIDITY", evidence_channels=("TAKER_FLOW",), **base)
    result = ConsensusEngine(cfg).evaluate([a,b])
    assert result.grade.value == "NO_TRADE"


@pytest.mark.asyncio
async def test_telegram_signal_inline_delivery_unblocks_why(cfg, instrument):
    from app.telegram.queue import TelegramQueue
    from app.telegram.sender import SendResult
    from app.telegram.formatter import MessageFormatter
    from types import SimpleNamespace
    class Sender:
        sent_hashes=set()
        async def send(self, text, **kwargs): return SendResult(ok=True, message_id=1)
    queue = TelegramQueue(cfg=cfg, formatter=MessageFormatter(cfg), sender=Sender())
    signal = SimpleNamespace(signal_id="SIG", symbol="B-BTC_USDT", direction=Direction.LONG, grade=__import__("app.core.models", fromlist=["Grade"]).Grade.B,
                             confidence=0.6, entry_price=100, stop_loss=98, tp1=102, tp2=104, tp3=106, tp4=110, expiry_ms=120000,
                             rr_tp2=2, entry_zone_low=99, entry_zone_high=101, invalidation=98, reasons=("measured",), strategy_votes={},
                             veto_status=__import__("app.core.models", fromlist=["VetoSeverity"]).VetoSeverity.PASS, veto_detail="ok", news_state=__import__("app.core.models", fromlist=["NewsState"]).NewsState.CLEAR,
                             news_source="none", state=__import__("app.core.models", fromlist=["SignalState"]).SignalState.PENDING, advisory_qty=0, advisory_notional=0, meta={}, latency_ms=0,
                             binance_price=100, coindcx_price=100, spread_bps=1, basis_bps=0)
    await queue.publish_signal(signal, ["flow measured"])
    if queue._why_tasks:
        await asyncio.gather(*list(queue._why_tasks))
    assert queue._signal_events == {}
    assert queue.stats["failed"] == 0


def test_binance_current_routed_urls_and_stream_classification():
    from app.data.binance.websocket import COMBINED_ROUTE_MAP, ROUTE_MAP, required_route
    assert ROUTE_MAP[StreamRoute.PUBLIC].endswith("/public/ws")
    assert ROUTE_MAP[StreamRoute.MARKET].endswith("/market/ws")
    assert COMBINED_ROUTE_MAP[StreamRoute.PUBLIC].endswith("/public/stream")
    assert COMBINED_ROUTE_MAP[StreamRoute.MARKET].endswith("/market/stream")
    assert required_route("btcusdt@depth20@100ms") is StreamRoute.PUBLIC
    assert required_route("btcusdt@bookTicker") is StreamRoute.PUBLIC
    assert required_route("btcusdt@aggTrade") is StreamRoute.MARKET
    assert required_route("btcusdt@kline_1m") is StreamRoute.MARKET
    assert required_route("btcusdt@forceOrder") is StreamRoute.MARKET
    assert required_route("btcusdt@markPrice@1s") is StreamRoute.MARKET


def test_binance_default_ws_puts_kline_on_market_and_depth_on_public():
    routes = build_default_streams(["BTCUSDT"])
    assert routes[StreamRoute.PUBLIC] == ["btcusdt@depth20@100ms"]
    assert routes[StreamRoute.MARKET] == ["btcusdt@kline_1m", "btcusdt@markPrice@1s", "btcusdt@ticker"]


def test_coindcx_public_futures_parsers_match_documented_shapes():
    from app.data.coindcx.models import parse_instrument, parse_orderbook
    instrument_payload = {
        "instrument": {
            "symbol": "B-BTC_USDT",
            "price_increment": "0.1",
            "quantity_increment": "0.001",
            "min_trade_size": "0.001",
            "min_notional": "5",
            "maker_fee": "0.0236",
            "taker_fee": "0.059",
            "funding_frequency": 8,
            "quote_currency_short_name": "USDT",
            "settle_currency_short_name": "USDT",
            "kind": "perpetual",
            "quanto_to_settle_multiplier": "1",
            "is_inverse": False,
        }
    }
    spec = parse_instrument(instrument_payload, pair="B-BTC_USDT", binance_symbol="BTCUSDT")
    assert spec.price_increment == pytest.approx(0.1)
    assert spec.quote_currency == "USDT"
    assert spec.inverse is False

    book = parse_orderbook({"ts": 1700000000123,
                            "bids": {"100.0": "10", "99.9": "5"},
                            "asks": {"100.2": "4", "100.3": "8"}}, "B-BTC_USDT")
    assert book.best_bid == pytest.approx(100.0)
    assert book.best_ask == pytest.approx(100.2)
    assert book.mid == pytest.approx(100.1)


def test_walkforward_combined_drawdown_spans_fold_boundaries():
    from app.backtest.metrics import Metrics
    from app.backtest.walk_forward import _combine
    first = Metrics(fills=2, trades=2, net_r=1.0, equity_curve=(1.0, 1.0))
    first.gross_profit_r = 1.0
    first.gross_loss_r = 0.0
    second = Metrics(fills=2, trades=2, net_r=-1.4, equity_curve=(-0.7, -1.4))
    second.gross_profit_r = 0.0
    second.gross_loss_r = 1.4
    combined = _combine([first, second])
    assert combined.equity_curve == pytest.approx((1.0, 1.0, 0.3, -0.4))
    assert combined.max_dd_r == pytest.approx(1.4)

