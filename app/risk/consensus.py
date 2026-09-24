"""Consensus + grading (master prompt §16/§17, FINAL_DELIVERABLE §N).

Rules:
  * strategies sharing an information channel are not blindly counted as independent;
    one engine per correlation group is selected and substantial evidence-channel overlap
    can discount a group, so correlated OI/taker inputs do not masquerade as independent votes;
  * A+ : confidence >= 0.82 AND >= 3 engine-groups AND no degradation
  * A  : confidence >= 0.66 AND >= 2 engine-groups
  * B  : confidence >= 0.55 AND >= 1 engine-group
  * otherwise NO TRADE
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.config import AppConfig
from app.core.models import Direction, Grade, StrategyCandidate


@dataclass
class ConsensusResult:
    direction: Direction | None
    grade: Grade
    confidence: float
    agreeing_groups: tuple[str, ...] = ()
    agreeing_strategies: tuple[str, ...] = ()
    conflicting: tuple[str, ...] = ()
    reason: str = ""
    contributions: Mapping[str, float] = field(default_factory=dict)
    independent_channel_ratio: float = 0.0

    @property
    def tradeable(self) -> bool:
        return self.direction is not None and self.grade is not Grade.NO_TRADE


def group_of(sid: str, cfg: AppConfig) -> str:
    for group, members in cfg.consensus.correlation_groups.items():
        if sid in set(members):
            return group
    return f"UNGROUPED_{sid}"


def _confidence(
    candidates: Sequence[StrategyCandidate], *, weights: Mapping[str, float] | None = None
) -> float:
    """Agreement-weighted confidence: independent groups raise confidence, duplicates do not."""
    if not candidates:
        return 0.0
    groups: dict[str, list[StrategyCandidate]] = {}
    for c in candidates:
        groups.setdefault(c.correlation_group, []).append(c)
    group_scores: list[float] = []
    for _group, members in groups.items():
        best = max(members, key=lambda m: m.confidence)
        extra = 0.03 * (len(members) - 1)  # a second opinion in the SAME channel adds little
        group_scores.append(min(1.0, best.confidence + extra))
    base = sum(group_scores) / len(group_scores)
    agreement_bonus = 0.05 * (len(group_scores) - 1)
    return max(0.0, min(1.0, base + agreement_bonus))


def _independent_group_selection(
    candidates: Sequence[StrategyCandidate], min_novel_ratio: float = 0.50
) -> tuple[dict[str, StrategyCandidate], float]:
    """Select one best candidate per correlation group and discount correlated evidence.

    Groups without declared evidence channels retain legacy group-level independence.
    When channels are present, a group is counted only when at least `min_novel_ratio`
    of its channels are new relative to already-counted groups.
    """
    best: dict[str, StrategyCandidate] = {}
    for c in candidates:
        current = best.get(c.correlation_group)
        if current is None or c.confidence > current.confidence:
            best[c.correlation_group] = c

    ordered = sorted(best.values(), key=lambda c: (-c.confidence, c.correlation_group))
    used: set[str] = set()
    kept: dict[str, StrategyCandidate] = {}
    ratios: list[float] = []
    for c in ordered:
        channels = {str(x) for x in c.evidence_channels if str(x)}
        if not channels:
            kept[c.correlation_group] = c
            ratios.append(1.0)
            continue
        novel = channels - used
        ratio = len(novel) / len(channels)
        if not kept or ratio >= min_novel_ratio:
            kept[c.correlation_group] = c
            used.update(channels)
            ratios.append(ratio)
    independent_ratio = sum(ratios) / len(ratios) if ratios else 0.0
    return kept, independent_ratio


def grade_for(confidence: float, groups: int, *, degraded: bool, cfg: AppConfig) -> Grade:
    a_plus = cfg.grading.a_plus or {}
    a = cfg.grading.a or {}
    b = cfg.grading.b or {}
    if (
        confidence >= float(a_plus.get("min_confidence", 0.82))
        and groups >= int(a_plus.get("min_engines", 3))
        and not (degraded and not bool(a_plus.get("allow_degrade", False)))
    ):
        return Grade.A_PLUS
    if confidence >= float(a.get("min_confidence", 0.66)) and groups >= int(
        a.get("min_engines", 2)
    ):
        return Grade.A
    if confidence >= float(b.get("min_confidence", 0.55)) and groups >= int(
        b.get("min_engines", 1)
    ):
        return Grade.B
    return Grade.NO_TRADE


@dataclass
class ConsensusEngine:
    cfg: AppConfig

    def evaluate(
        self,
        candidates: Sequence[StrategyCandidate],
        *,
        degraded: bool = False,
        degraded_penalty: float = 0.0,
    ) -> ConsensusResult:
        if not candidates:
            return ConsensusResult(
                None, Grade.NO_TRADE, 0.0, reason="no strategy emitted a candidate"
            )

        longs = [c for c in candidates if c.direction is Direction.LONG]
        shorts = [c for c in candidates if c.direction is Direction.SHORT]
        if not longs and not shorts:
            return ConsensusResult(None, Grade.NO_TRADE, 0.0, reason="no directional candidate")

        # direction with the most INDEPENDENT groups wins
        min_novel_ratio = float(getattr(self.cfg.consensus, "min_novel_evidence_ratio", 0.50))

        def grouped(items: Sequence[StrategyCandidate]):
            return _independent_group_selection(items, min_novel_ratio=min_novel_ratio)

        long_best, long_channel_ratio = grouped(longs)
        short_best, short_channel_ratio = grouped(shorts)
        long_groups, short_groups = set(long_best), set(short_best)
        if len(long_groups) == len(short_groups) and long_groups and short_groups:
            return ConsensusResult(
                None,
                Grade.NO_TRADE,
                0.0,
                conflicting=tuple(sorted({c.strategy_id for c in candidates})),
                reason=f"direction conflict with equal independent support "
                f"(long groups {sorted(long_groups)} vs short groups {sorted(short_groups)})",
            )
        if len(long_groups) > len(short_groups):
            direction, agreeing, opposing = Direction.LONG, longs, shorts
        else:
            direction, agreeing, opposing = Direction.SHORT, shorts, longs

        # One strategy per correlation/evidence group is the atomic unit of consensus.
        # Never let multiple correlated engines satisfy the minimum-agreement rule by
        # themselves.
        best_per_group, independent_channel_ratio = _independent_group_selection(
            agreeing, min_novel_ratio=min_novel_ratio
        )
        min_engines = int(self.cfg.consensus.min_agreeing_engines)
        if len(best_per_group) < min_engines:
            return ConsensusResult(
                None,
                Grade.NO_TRADE,
                0.0,
                agreeing_groups=tuple(sorted(best_per_group)),
                agreeing_strategies=tuple(sorted(c.strategy_id for c in best_per_group.values())),
                conflicting=tuple(sorted({c.strategy_id for c in opposing})),
                reason=f"only {len(best_per_group)} independent engine-group(s) agree (minimum {min_engines})",
                independent_channel_ratio=independent_channel_ratio,
            )
        if (
            opposing
            and self.cfg.consensus.conflict_requires_agreement
            and len(best_per_group) <= len(_independent_group_selection(opposing, min_novel_ratio=min_novel_ratio)[0])
        ):
            return ConsensusResult(
                None,
                Grade.NO_TRADE,
                0.0,
                conflicting=tuple(sorted({c.strategy_id for c in opposing})),
                reason="conflicting directions without a clear independent majority",
            )

        # one engine per correlation group counts toward the grade
        counted = list(best_per_group.values())
        confidence = _confidence(agreeing)
        if degraded:
            confidence = max(0.0, confidence - degraded_penalty)

        grade = grade_for(confidence, len(best_per_group), degraded=degraded, cfg=self.cfg)
        return ConsensusResult(
            direction=direction,
            grade=grade,
            confidence=confidence,
            agreeing_groups=tuple(sorted(best_per_group)),
            agreeing_strategies=tuple(sorted(c.strategy_id for c in counted)),
            conflicting=tuple(sorted({c.strategy_id for c in opposing})),
            reason="ok" if grade is not Grade.NO_TRADE else "confidence/grading thresholds not met",
            contributions={c.strategy_id: c.confidence for c in agreeing},
            independent_channel_ratio=independent_channel_ratio,
        )


__all__ = ["ConsensusEngine", "ConsensusResult", "grade_for", "group_of"]
