"""Typed configuration loading.

Rules:
  * Every tunable lives in `config/*.yaml` - no magic numbers in the engine code.
  * Secrets are read from the environment ONLY, never from YAML and never logged.
  * Loading is fail-closed: a malformed file or a violated invariant raises.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from app.core.errors import ConfigError, SafetyViolation

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# --------------------------------------------------------------------------- helpers


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing configuration file: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def _build(cls: type, data: Mapping[str, Any] | None) -> Any:
    """Instantiate a dataclass from a mapping, ignoring unknown keys loudly."""
    data = dict(data or {})
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{cls.__name__}: unknown configuration keys {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if is_dataclass(f.type) and isinstance(value, Mapping):
            kwargs[f.name] = _build(f.type, value)  # type: ignore[arg-type]
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def _required(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key '{key}'")
    return mapping[key]


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    text = value.strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"invalid boolean environment value: {value!r}")


# --------------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class PairConfig:
    coindcx: str
    binance: str
    tier: str = "MID"

    def __post_init__(self) -> None:
        if not self.coindcx.startswith("B-") or not self.coindcx.endswith("_USDT"):
            raise ConfigError(
                f"pair '{self.coindcx}' is not a CoinDCX USDT-margined futures symbol"
            )
        if self.binance != self.binance.upper():
            raise ConfigError(f"binance symbol '{self.binance}' must be upper-case")

    @property
    def base(self) -> str:
        return self.coindcx[2:].split("_")[0]


@dataclass(frozen=True)
class PairUniverse:
    pairs: tuple[PairConfig, ...]
    validation: Mapping[str, Any] = field(default_factory=dict)
    fee_defaults: Mapping[str, Any] = field(default_factory=dict)
    precision_overrides: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

    def by_coindcx(self, pair: str) -> PairConfig | None:
        return next((p for p in self.pairs if p.coindcx == pair), None)

    def by_binance(self, symbol: str) -> PairConfig | None:
        return next((p for p in self.pairs if p.binance == symbol), None)

    def symbols(self) -> tuple[str, ...]:
        return tuple(p.coindcx for p in self.pairs)

    @property
    def fee_defaults_frac(self) -> tuple[float, float]:
        maker = float(self.fee_defaults.get("maker_fee_pct", 0.0236)) / 100.0
        taker = float(self.fee_defaults.get("taker_fee_pct", 0.059)) / 100.0
        return maker, taker


@dataclass(frozen=True)
class SystemConfig:
    mode: str = "signal_only"
    fail_closed: bool = True
    why_message_budget_sec: int = 10
    boot_assertion: bool = True
    forbidden_capabilities: tuple[str, ...] = ()
    forbidden_credential_env: tuple[str, ...] = ()
    allowed_credential_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExchangeConfig:
    role: str
    rest_base: str = ""
    market_data_base: str = ""
    ws_public_base: str = ""
    ws_routed_paths: tuple[str, ...] = ()
    rate_limit_weight_per_min: int = 2400
    weight_budget_fraction: float = 0.80
    ping_interval_sec: int = 20
    reconnect_backoff_sec: tuple[float, ...] = (1, 2, 5, 10, 30)
    jitter: float = 0.30
    poll_sec: float = 2.0
    poll_concurrency: int = 5
    exposes_derivatives_metrics: bool = False
    public_ws: str = "UNVERIFIED"
    verified_endpoints: tuple[Mapping[str, Any], ...] = ()
    unverified_endpoints: tuple[Mapping[str, Any], ...] = ()

    @property
    def weight_budget(self) -> int:
        return int(self.rate_limit_weight_per_min * self.weight_budget_fraction)


@dataclass(frozen=True)
class ExchangesConfig:
    binance: ExchangeConfig
    coindcx: ExchangeConfig


@dataclass(frozen=True)
class NormalizationConfig:
    max_clock_drift_ms: int = 1500
    min_history_obs: int = 60
    divergence_bands: Mapping[str, float] = field(default_factory=dict)
    stablecoin_cross_symbol: str = "USDCUSDT"
    usdt_usd_fallback: float = 1.0
    quote_currency: str = "USDT"
    expected_slippage_bps: float = 3.0

    @property
    def block_z(self) -> float:
        return float(self.divergence_bands.get("extreme", 3.0))

    @property
    def abnormal_z(self) -> float:
        return float(self.divergence_bands.get("abnormal", 3.0))


@dataclass(frozen=True)
class StalenessConfig:
    binance_rest_ms: int = 3000
    binance_ws_ms: int = 3000
    coindcx_rest_ms: int = 6000
    coindcx_ws_ms: int = 6000
    news_ms: int = 900_000
    min_sources_healthy: int = 2


@dataclass(frozen=True)
class TelegramConfig:
    api_base: str = "https://api.telegram.org"
    max_chars: int = 1024
    per_chat_min_interval_sec: float = 1.2
    bulk_messages_per_sec: int = 25
    retry_backoff_sec: tuple[float, ...] = (2, 5, 15)
    max_retries: int = 4
    dry_run: bool = True
    group_messages_per_min: int = 20
    bot_token: str = ""
    chat_id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass(frozen=True)
class BacktestConfig:
    next_bar_fills_only: bool = True
    limit_fill_probability_model: str = "touch_only"
    limit_fill_prob_max: float = 0.95
    latency_ms: int = 250
    slippage_bps: float = 3.0
    spread_cost_bps: float = 2.0
    maker_fee_pct: float = 0.0236
    taker_fee_pct: float = 0.059
    use_taker_for_stop: bool = True
    warmup_bars: int = 200
    walk_forward_folds: int = 6
    embargo_pct: float = 0.01


@dataclass(frozen=True)
class DatabaseConfig:
    sqlite_path: str = "logs/signal_journal.sqlite"
    jsonl_mirror: bool = True
    jsonl_path: str = "logs"
    retention_days: int = 400


@dataclass(frozen=True)
class StrategyParams:
    id: str
    name: str
    regimes: tuple[str, ...] = ()
    correlation_group: str = "DEFAULT"
    expiry_min: int = 45
    cooldown_min: int = 60
    base_confidence: float = 0.60
    tp1_take_fraction: float = 0.40
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyConfig:
    common: Mapping[str, Any] = field(default_factory=dict)
    strategies: Mapping[str, StrategyParams] = field(default_factory=dict)

    def params(self, strategy_id: str) -> StrategyParams | None:
        return self.strategies.get(strategy_id)

    @property
    def enabled(self) -> tuple[str, ...]:
        return tuple(self.common.get("enabled", ("S1", "S2", "S3", "S4", "S5")))

    @property
    def atr_period(self) -> int:
        return int(self.common.get("atr_period", 14))

    @property
    def atr_percentile_lookback(self) -> int:
        return int(self.common.get("atr_percentile_lookback", 200))

    @property
    def min_candles(self) -> int:
        return int(self.common.get("min_candles", 60))

    @property
    def tp_r_multiples(self) -> tuple[float, ...]:
        return tuple(float(x) for x in self.common.get("tp_r_multiples", (1.0, 2.0, 3.0, 5.0)))

    @property
    def min_rr_tp2(self) -> float:
        return float(self.common.get("min_rr_tp2", 1.8))

    @property
    def risk_floor_atr_mult(self) -> float:
        return float(self.common.get("risk_floor_atr_mult", 0.25))


@dataclass(frozen=True)
class VetoConfig:
    hard_block: bool = True
    override_allowed: bool = False
    fail_closed_on_guard_exception: bool = True
    min_sources_healthy: int = 2
    data_integrity: Mapping[str, Any] = field(default_factory=dict)
    divergence: Mapping[str, Any] = field(default_factory=dict)
    liquidity: Mapping[str, Any] = field(default_factory=dict)
    news_shock: Mapping[str, Any] = field(default_factory=dict)
    crowding: Mapping[str, Any] = field(default_factory=dict)
    degrade_tier: Mapping[str, Any] = field(default_factory=dict)
    escalation: Mapping[str, Any] = field(default_factory=dict)

    @property
    def degrade_penalty(self) -> float:
        return float(self.degrade_tier.get("confidence_penalty", 0.15))

    def guard(self, name: str) -> Mapping[str, Any]:
        return self.degrade_tier.get(name, {})  # type: ignore[return-value]


@dataclass(frozen=True)
class NewsSourceConfig:
    id: str
    tier: int
    type: str
    url: str
    status: str = "VERIFIED"
    enabled: bool = True
    query: str = ""
    mode: str = ""
    format: str = ""
    maxrecords: int = 50

    @property
    def verified(self) -> bool:
        return self.status == "VERIFIED"


@dataclass(frozen=True)
class NewsConfig:
    enabled: bool = True
    min_sources_healthy: int = 2
    poll_sec: int = 60
    lookback_min: int = 720
    max_items_per_source: int = 60
    user_agent: str = "coindcx-top20-signal-bot/1.0"
    sources: tuple[NewsSourceConfig, ...] = ()
    credibility: Mapping[str, Any] = field(default_factory=dict)
    category_weights: Mapping[str, float] = field(default_factory=dict)
    half_life_min: Mapping[str, float] = field(default_factory=dict)
    severity_thresholds: Mapping[str, float] = field(default_factory=dict)
    market_wide_multiplier: float = 1.15
    novelty_lookback_hours: int = 72
    dedupe: Mapping[str, Any] = field(default_factory=dict)
    state: Mapping[str, Any] = field(default_factory=dict)
    categories: tuple[str, ...] = ()

    @property
    def enabled_sources(self) -> tuple[NewsSourceConfig, ...]:
        return tuple(s for s in self.sources if s.enabled)

    @property
    def verified_sources(self) -> tuple[NewsSourceConfig, ...]:
        return tuple(s for s in self.enabled_sources if s.verified)

    def tier_score(self, tier: int) -> float:
        scores = self.credibility.get("tier_scores", {})
        return float(scores.get(tier, scores.get(str(tier), 0.30)))

    def weight(self, category: str) -> float:
        return float(
            self.category_weights.get(category, self.category_weights.get("GENERAL", 0.30))
        )

    def half_life(self, category: str) -> float:
        return float(self.half_life_min.get(category, self.half_life_min.get("DEFAULT", 60)))

    def threshold(self, severity: str) -> float:
        return float(self.severity_thresholds.get(severity, 0.35))


@dataclass(frozen=True)
class BtcRegimeConfig:
    symbol: str = "BTCUSDT"
    coindcx_pair: str = "B-BTC_USDT"
    structure_lookback: int = 60
    ema_fast: int = 21
    ema_slow: int = 55
    vol_percentile_high: float = 0.90
    vol_percentile_low: float = 0.20
    oi_rank_high: float = 0.95
    funding_z_abs_high: float = 2.0
    strong_trend_atr_mult: float = 1.5
    conflict_action: str = "DEGRADE"
    unknown_action: str = "BLOCK"


@dataclass(frozen=True)
class RiskConfig:
    sl_atr_buffer: float = 0.5
    sl_floor_atr_mult: float = 0.25
    tp_r_multiples: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0)
    min_rr_tp2: float = 1.8
    invalidation_recheck_sec: int = 15
    signal_expiry_min: Mapping[str, int] = field(default_factory=dict)
    cooldown_min_per_symbol: int = 60
    duplicate_window_min: int = 60
    account: Mapping[str, Any] = field(default_factory=dict)
    sizing: Mapping[str, Any] = field(default_factory=dict)

    @property
    def max_concurrent_signals(self) -> int:
        return int(self.account.get("max_concurrent_signals", 3))

    @property
    def max_daily_signals(self) -> int:
        return int(self.account.get("max_daily_signals", 6))

    @property
    def max_daily_loss_r(self) -> float:
        return float(self.account.get("max_daily_loss_R", 2.0))

    @property
    def max_per_group(self) -> int:
        return int(self.account.get("max_concurrent_per_correlation_group", 2))

    def expiry_for_grade(self, grade: str) -> int:
        return int(self.signal_expiry_min.get(grade, self.signal_expiry_min.get("default", 45)))


@dataclass(frozen=True)
class GradingConfig:
    a_plus: Mapping[str, Any] = field(default_factory=dict)
    a: Mapping[str, Any] = field(default_factory=dict)
    b: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConsensusConfig:
    correlation_groups: Mapping[str, Sequence[str]] = field(default_factory=dict)
    max_engines_per_group: int = 1
    min_agreeing_engines: int = 2
    conflict_requires_agreement: bool = True
    confidence_weight_by_grade: bool = True
    min_novel_evidence_ratio: float = 0.50


@dataclass(frozen=True)
class AppConfig:
    system: SystemConfig
    pairs: PairUniverse
    exchanges: ExchangesConfig
    normalization: NormalizationConfig
    staleness: StalenessConfig
    telegram: TelegramConfig
    backtest: BacktestConfig
    database: DatabaseConfig
    strategy: StrategyConfig
    veto: VetoConfig
    news: NewsConfig
    risk: RiskConfig
    grading: GradingConfig
    consensus: ConsensusConfig
    btc_regime: BtcRegimeConfig
    costs: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> AppConfig:
        if self.system.mode != "signal_only":
            raise SafetyViolation(f"mode must be 'signal_only', got '{self.system.mode}'")
        if not self.system.fail_closed:
            raise SafetyViolation("fail_closed must be true")
        if not self.veto.hard_block:
            raise SafetyViolation("veto.hard_block must be true")
        if self.veto.override_allowed:
            raise SafetyViolation(
                "veto.override_allowed must be false (a strategy may never override a veto)"
            )
        if not self.pairs.pairs:
            raise ConfigError("no pairs configured")
        if self.strategy.min_rr_tp2 < 1.8:
            raise ConfigError("min_rr_tp2 must be >= 1.8 (master prompt section 14)")
        if self.risk.max_daily_signals > 6:
            raise ConfigError("max_daily_signals must be <= 6")
        if self.risk.max_concurrent_signals > 3:
            raise ConfigError("max_concurrent_signals must be <= 3")
        if self.normalization.max_clock_drift_ms != 1500:
            raise ConfigError("max_clock_drift_ms must be 1500 (spec section K)")
        for cap in self.system.forbidden_capabilities:
            if cap in ("", None):
                raise ConfigError("forbidden_capabilities must not contain empty entries")
        return self


# --------------------------------------------------------------------------- loader


def _load_pairs(config_dir: Path) -> PairUniverse:
    data = _load_yaml(config_dir / "top20_pairs.yaml")
    raw_pairs = data.get("pairs")
    if not isinstance(raw_pairs, list) or not raw_pairs:
        raise ConfigError("top20_pairs.yaml: 'pairs' must be a non-empty list")
    pairs = tuple(_build(PairConfig, entry) for entry in raw_pairs)
    duplicates = {p.coindcx for p in pairs if [q.coindcx for q in pairs].count(p.coindcx) > 1}
    if duplicates:
        raise ConfigError(f"duplicate pairs in top20_pairs.yaml: {sorted(duplicates)}")
    if len(pairs) > 20:
        raise ConfigError(f"at most 20 pairs are permitted, got {len(pairs)}")
    return PairUniverse(
        pairs=pairs,
        validation=data.get("validation", {}) or {},
        fee_defaults=data.get("fee_defaults", {}) or {},
        precision_overrides=data.get("precision_overrides", {}) or {},
    )


def _load_strategies(config_dir: Path) -> StrategyConfig:
    data = _load_yaml(config_dir / "strategy.yaml")
    common = data.get("common", {}) or {}
    strategies: dict[str, StrategyParams] = {}
    for key, value in data.items():
        if key == "common" or not isinstance(value, dict):
            continue
        if "id" not in value:
            raise ConfigError(f"strategy.yaml: block '{key}' must declare an 'id'")
        sid = str(value["id"])
        reserved = {
            "id",
            "name",
            "regimes",
            "correlation_group",
            "expiry_min",
            "cooldown_min",
            "base_confidence",
            "tp1_take_fraction",
        }
        strategies[sid] = StrategyParams(
            id=sid,
            name=str(value.get("name", sid)),
            regimes=tuple(value.get("regimes", ())) or (),
            correlation_group=str(value.get("correlation_group", "DEFAULT")),
            expiry_min=int(value.get("expiry_min", 45)),
            cooldown_min=int(value.get("cooldown_min", 60)),
            base_confidence=float(value.get("base_confidence", 0.60)),
            tp1_take_fraction=float(value.get("tp1_take_fraction", 0.40)),
            extra={k: v for k, v in value.items() if k not in reserved},
        )
    missing = [sid for sid in common.get("enabled", []) if sid not in strategies]
    if missing:
        raise ConfigError(f"strategy.yaml: enabled strategies missing definitions: {missing}")
    return StrategyConfig(common=common, strategies=strategies)


def _load_news(config_dir: Path) -> NewsConfig:
    data = _load_yaml(config_dir / "news.yaml").get("news", {}) or {}
    raw_sources = data.get("sources", []) or []
    sources = tuple(_build(NewsSourceConfig, s) for s in raw_sources)
    kwargs = {k: v for k, v in data.items() if k != "sources"}
    known = {f.name for f in fields(NewsConfig)} - {"sources"}
    unknown = set(kwargs) - known
    if unknown:
        raise ConfigError(f"news.yaml: unknown keys {sorted(unknown)}")
    return NewsConfig(sources=sources, **kwargs)


def load_config(
    config_dir: Path | str | None = None, *, env: Mapping[str, str] | None = None
) -> AppConfig:
    """Load and validate the whole configuration tree."""
    env = dict(os.environ if env is None else env)
    config_dir = Path(config_dir) if config_dir else CONFIG_DIR

    system_data = _load_yaml(config_dir / "system.yaml")
    system = _build(SystemConfig, system_data.get("system", {}))
    exchange_data = system_data.get("exchanges", {}) or {}
    exchanges = ExchangesConfig(
        binance=_build(ExchangeConfig, exchange_data.get("binance", {})),
        coindcx=_build(ExchangeConfig, exchange_data.get("coindcx", {})),
    )

    telegram_data = dict(system_data.get("telegram", {}) or {})
    # Secrets never come from YAML.
    telegram_data["bot_token"] = env.get("TELEGRAM_BOT_TOKEN", "")
    telegram_data["chat_id"] = env.get("TELEGRAM_CHAT_ID", "")
    if "TELEGRAM_DRY_RUN" in env:
        telegram_data["dry_run"] = _env_bool(env.get("TELEGRAM_DRY_RUN"), bool(telegram_data.get("dry_run", True)))
    telegram = _build(TelegramConfig, telegram_data)

    risk_data = _load_yaml(config_dir / "risk.yaml")

    cfg = AppConfig(
        system=system,
        pairs=_load_pairs(config_dir),
        exchanges=exchanges,
        normalization=_build(NormalizationConfig, system_data.get("normalization", {})),
        staleness=_build(StalenessConfig, system_data.get("staleness", {})),
        telegram=telegram,
        backtest=_build(BacktestConfig, system_data.get("backtest", {})),
        database=_build(DatabaseConfig, system_data.get("database", {})),
        strategy=_load_strategies(config_dir),
        veto=_build(VetoConfig, _load_yaml(config_dir / "veto.yaml").get("veto", {})),
        news=_load_news(config_dir),
        risk=_build(RiskConfig, risk_data.get("risk", {})),
        grading=_build(GradingConfig, risk_data.get("grading", {})),
        consensus=_build(ConsensusConfig, risk_data.get("consensus", {})),
        btc_regime=_build(BtcRegimeConfig, risk_data.get("btc_regime", {})),
        costs=risk_data.get("costs", {}) or {},
        raw={**system_data, **risk_data},
    )
    return cfg.validate()


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


__all__ = [
    "AppConfig",
    "BacktestConfig",
    "BtcRegimeConfig",
    "ConsensusConfig",
    "DatabaseConfig",
    "ExchangeConfig",
    "ExchangesConfig",
    "GradingConfig",
    "NewsConfig",
    "NewsSourceConfig",
    "NormalizationConfig",
    "PairConfig",
    "PairUniverse",
    "RiskConfig",
    "StalenessConfig",
    "StrategyConfig",
    "StrategyParams",
    "SystemConfig",
    "TelegramConfig",
    "VetoConfig",
    "load_config",
    "repo_root",
]
