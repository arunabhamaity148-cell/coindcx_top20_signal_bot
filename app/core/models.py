"""Canonical in-memory data model shared by every layer.

Every numeric field here is either (a) measured from a verified venue payload or
(b) explicitly None. Nothing is interpolated or guessed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

# --------------------------------------------------------------------------- enums


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    def opposite(self) -> Direction:
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class FeedState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    DISCONNECTED = "DISCONNECTED"

    @property
    def tradeable(self) -> bool:
        return self is FeedState.HEALTHY


class NewsState(str, Enum):
    CLEAR = "CLEAR"
    DEGRADED = "DEGRADED"
    BLOCK = "BLOCK"


class DivergenceClass(str, Enum):
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    ABNORMAL = "ABNORMAL"
    EXTREME = "EXTREME"


class Crowding(str, Enum):
    NEUTRAL = "NEUTRAL"
    ELEVATED = "ELEVATED"
    EXTREME = "EXTREME"


class CascadeRisk(str, Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class OIQuadrant(str, Enum):
    PRICE_UP_OI_UP = "PRICE_UP_OI_UP"
    PRICE_UP_OI_DOWN = "PRICE_UP_OI_DOWN"
    PRICE_DOWN_OI_UP = "PRICE_DOWN_OI_UP"
    PRICE_DOWN_OI_DOWN = "PRICE_DOWN_OI_DOWN"
    UNKNOWN = "UNKNOWN"


class Grade(str, Enum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    NO_TRADE = "NO_TRADE"


class BtcRegime(str, Enum):
    RISK_ON_STRONG = "RISK_ON_STRONG"
    NEUTRAL = "NEUTRAL"
    RISK_OFF_STRONG = "RISK_OFF_STRONG"
    UNKNOWN = "UNKNOWN"


class VetoSeverity(str, Enum):
    PASS = "PASS"
    DEGRADE = "DEGRADE"
    BLOCK = "BLOCK"


class SignalState(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    DANGER = "DANGER"


# --------------------------------------------------------------------------- market


@dataclass(frozen=True)
class Candle:
    """OHLCV bar. `taker_buy_quote` is Binance's taker-buy quote volume when present."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy_quote: float | None = None
    close_time_ms: int | None = None
    is_closed: bool = True

    @property
    def taker_buy_ratio(self) -> float | None:
        if self.taker_buy_quote is None or self.volume <= 0:
            return None
        return self.taker_buy_quote / self.volume

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


@dataclass(frozen=True)
class BookLevel:
    price: float
    qty: float


@dataclass(frozen=True)
class OrderBook:
    """One exchange's top-of-book snapshot. Sorted best-first on both sides.

    Timestamp semantics (intentionally separated):
      * ts_ms          — primary event/observation time used for cross-venue alignment.
                         Prefer the venue-published event time when available.
      * received_ts_ms — local wall-clock time when this snapshot was accepted by the bot.
                         Always set at receipt; used for *freshness* (staleness budgets).
      * event_ts_ms    — venue-published event time when known; None if the venue did not
                         supply a usable timestamp (caller fell back to local clock for ts_ms).

    Cross-venue normalization must NOT treat a REST-polled book's local receipt time and a
    WebSocket book's exchange event time as interchangeable event clocks. Freshness is judged
    against received_ts_ms; event-time drift is only enforced when both sides publish real
    exchange event timestamps.
    """

    venue: str
    symbol: str
    ts_ms: int
    bids: Sequence[BookLevel]
    asks: Sequence[BookLevel]
    received_ts_ms: int | None = None
    event_ts_ms: int | None = None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    @property
    def spread_bps(self) -> float | None:
        bid, ask, mid = self.best_bid, self.best_ask, self.mid
        if bid is None or ask is None or not mid:
            return None
        return (ask - bid) / mid * 1e4

    @property
    def crossed(self) -> bool:
        bid, ask = self.best_bid, self.best_ask
        return bid is not None and ask is not None and bid >= ask

    @property
    def is_valid(self) -> bool:
        return bool(self.bids) and bool(self.asks) and not self.crossed

    @property
    def has_exchange_event_ts(self) -> bool:
        """True when ts_ms was derived from a venue-published event time."""
        return self.event_ts_ms is not None

    def depth_usd(self, side: str, within_bps: float, mid: float | None = None) -> float:
        """Notional resting within `within_bps` of mid on one side."""
        mid = mid if mid is not None else self.mid
        if mid is None:
            return 0.0
        limit = mid * within_bps / 1e4
        levels = self.bids if side == "bid" else self.asks
        total = 0.0
        for lvl in levels:
            if abs(lvl.price - mid) > limit:
                break
            total += lvl.price * lvl.qty
        return total

    @property
    def imbalance(self) -> float:
        """(bid_depth - ask_depth) / total depth, within the configured band."""
        b = sum(l.price * l.qty for l in self.bids)
        a = sum(l.price * l.qty for l in self.asks)
        if b + a <= 0:
            return 0.0
        return (b - a) / (b + a)


@dataclass(frozen=True)
class LiquiditySnapshot:
    spread_bps: float
    depth_bid_usd: float
    depth_ask_usd: float
    imbalance: float
    mid_jump_bps: float
    expected_slippage_bps: float

    @property
    def depth_usd_min(self) -> float:
        return min(self.depth_bid_usd, self.depth_ask_usd)


@dataclass(frozen=True)
class DerivativesSnapshot:
    """Binance-sourced derivatives context.

    `source` is always recorded because CoinDCX publishes NO public OI/funding
    (FINAL_DELIVERABLE §J / DESIGN_SPEC §5) - the system must never imply otherwise.
    """

    symbol: str
    ts_ms: int
    source: str = "BINANCE"
    mark_price: float | None = None
    index_price: float | None = None
    funding_rate: float | None = None
    funding_z: float | None = None
    open_interest: float | None = None
    oi_chg_pct: float | None = None
    oi_pct_rank: float | None = None
    taker_buy_sell_ratio: float | None = None
    price_chg_pct: float | None = None
    quadrant: OIQuadrant = OIQuadrant.UNKNOWN
    crowding: Crowding = Crowding.NEUTRAL
    cascade_risk: CascadeRisk = CascadeRisk.LOW
    oi_ts_ms: int | None = None
    funding_ts_ms: int | None = None
    mark_ts_ms: int | None = None
    index_ts_ms: int | None = None


@dataclass(frozen=True)
class BasisSnapshot:
    binance_usd_mid: float
    coindcx_usd_mid: float
    basis_bps: float
    net_basis_bps: float
    z: float | None
    percentile: float | None
    vol_adjusted: float | None
    effective_cost_bps: float
    observations: int
    drift_ms: int
    classification: DivergenceClass
    raw_coindcx_mid: float | None = None
    quote: str = "USDT"
    convergence_probability: float | None = None
    expected_capture_bps: float | None = None


@dataclass(frozen=True)
class InstrumentSpec:
    """CoinDCX Futures contract metadata (execution reality)."""

    pair: str
    binance_symbol: str
    price_increment: float
    quantity_increment: float
    min_trade_size: float
    min_notional: float
    maker_fee_pct: float
    taker_fee_pct: float
    funding_frequency: int
    quote_currency: str = "USDT"
    settle_currency: str = "USDT"
    kind: str = "perpetual"
    unit_contract_value: float = 1.0
    quanto_multiplier: float = 1.0
    inverse: bool = False

    @property
    def taker_fee_frac(self) -> float:
        return self.taker_fee_pct / 100.0

    @property
    def maker_fee_frac(self) -> float:
        return self.maker_fee_pct / 100.0


@dataclass(frozen=True)
class FeedHealth:
    name: str
    state: FeedState
    last_ts_ms: int | None
    age_ms: int | None
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return self.state is FeedState.HEALTHY


@dataclass(frozen=True)
class StrategyCandidate:
    """A strategy's structured result (master prompt §6: every strategy returns structure)."""

    strategy_id: str
    symbol: str
    direction: Direction
    confidence: float
    entry_price: float
    entry_zone_low: float
    entry_zone_high: float
    invalidation: float
    stop_loss: float
    atr: float
    expiry_min: int
    reasons: Sequence[str] = field(default_factory=tuple)
    correlation_group: str = "DEFAULT"
    metadata: Mapping[str, float] = field(default_factory=dict)
    # The levels the candidate is built on. Populated from LevelPlan by Strategy.finalize;
    # the signal engine / Telegram formatter / tests read them straight off the candidate.
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    tp4: float = 0.0
    rr_tp2: float = 0.0
    atr_timeframe: str = "5m"
    evidence_channels: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything the strategy layer is allowed to see. Immutable by construction."""

    symbol: str
    ts_ms: int
    binance_book: OrderBook
    coindcx_book: OrderBook
    candles: Mapping[str, Sequence[Candle]]
    derivatives: DerivativesSnapshot
    liquidity: LiquiditySnapshot
    basis: BasisSnapshot | None
    instrument: InstrumentSpec
    feed_health: Mapping[str, FeedHealth]
    clock_drift_ms: int
    news_state: NewsState = NewsState.CLEAR
    btc_regime: BtcRegime = BtcRegime.UNKNOWN
    btc_conflict: bool = False
    book_mid_jump_bps: float = 0.0

    def series(self, timeframe: str) -> Sequence[Candle]:
        return self.candles.get(timeframe, ())

    @property
    def last_price(self) -> float:
        return self.coindcx_book.mid or self.binance_book.mid or 0.0
