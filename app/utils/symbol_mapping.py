"""Symbol mapping between CoinDCX Futures and Binance USDⓈ-M Futures.

FAILURE MODE MATRIX (spec §8/§AA): "symbol mapping failure -> suppress signal
(not tradable)". A mapping is therefore never guessed: it comes from
`config/top20_pairs.yaml` AND must be confirmed against BOTH venues' live
instrument lists.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import SymbolMappingError


@dataclass(frozen=True)
class SymbolPair:
    coindcx: str
    binance: str
    tier: str = "MID"

    @property
    def base(self) -> str:
        return self.coindcx[2:].split("_")[0]


class SymbolMap:
    def __init__(self, pairs: tuple[SymbolPair, ...]):
        if not pairs:
            raise SymbolMappingError(
                "symbol map is empty - refusing to run without a configured universe"
            )
        self._pairs = pairs
        self._by_coindcx = {p.coindcx: p for p in pairs}
        self._by_binance = {p.binance: p for p in pairs}

    @classmethod
    def from_config(cls, universe) -> SymbolMap:
        return cls(tuple(SymbolPair(p.coindcx, p.binance, p.tier) for p in universe.pairs))

    def __len__(self) -> int:
        return len(self._pairs)

    def all(self) -> tuple[SymbolPair, ...]:
        return self._pairs

    def binance_symbols(self) -> tuple[str, ...]:
        return tuple(p.binance for p in self._pairs)

    def coindcx_pairs(self) -> tuple[str, ...]:
        return tuple(p.coindcx for p in self._pairs)

    def by_coindcx(self, pair: str) -> SymbolPair:
        pair = pair.strip().upper()
        if pair not in self._by_coindcx:
            raise SymbolMappingError(f"{pair} is not in the configured TOP-20 universe")
        return self._by_coindcx[pair]

    def by_binance(self, symbol: str) -> SymbolPair:
        symbol = symbol.strip().upper()
        if symbol not in self._by_binance:
            raise SymbolMappingError(f"{symbol} is not in the configured TOP-20 universe")
        return self._by_binance[symbol]

    def validate_against_venues(
        self,
        *,
        coindcx_active: set[str],
        binance_symbols: set[str],
        require_coindcx: bool = True,
        require_binance: bool = True,
        never_substitute: bool = True,
    ) -> tuple[tuple[SymbolPair, ...], tuple[str, ...]]:
        """Return (valid pairs, rejected pair names with reasons).

        Rejection is never repaired by substitution - master prompt §4.
        """
        valid: list[SymbolPair] = []
        rejected: list[str] = []
        for pair in self._pairs:
            reasons: list[str] = []
            if require_coindcx and pair.coindcx not in coindcx_active:
                reasons.append("not in CoinDCX active_instruments")
            if require_binance and pair.binance not in binance_symbols:
                reasons.append("not in Binance exchangeInfo")
            if reasons:
                rejected.append(f"{pair.coindcx}: " + "; ".join(reasons))
                continue
            valid.append(pair)
        if never_substitute and not valid:
            raise SymbolMappingError("no pair survived venue validation - NO SIGNAL (fail-closed)")
        return tuple(valid), tuple(rejected)


__all__ = ["SymbolMap", "SymbolPair"]
