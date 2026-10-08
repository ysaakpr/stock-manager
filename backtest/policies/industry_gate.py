"""M16.2 — the point-in-time industry-momentum gate for momentum v2 (arm A2).

Each rebalance, rank the NSE sectoral indices of :mod:`backtest.sector_indices` by their 6-1 month
return (the level one month back over the level six months back, from published levels only), keep
the top :data:`TOP_K` (5), and drop from the momentum ranking every *classified* name whose industry
is not mapped to one of those five (an unmapped industry included). A name the classification does
not name at all is gate-neutral and passes: the classification is a 2026 snapshot that omits every
name which died before it, so excluding the unclassified would keep only survivors. Every parameter
is fixed — K=5, 6-1 months, the monthly rebalance — and none is exposed as an option, so the gate
cannot be tuned through its configuration.

Why it replaces M10.3's sector test: M10.3 scored sectors from a static 42-name current-day map and
the members' own momentum, so the sector signal was the stock signal regrouped. Here the sector
signal is the published index level, read through the PIT guard: each :class:`IndexMomentum` is
dated by the later publication date of its two levels and admitted via ``ctx.pit``, so a level not
yet published on the decision date trips :class:`~dataplatform.query.pit.PitError`.

What it never does: backfill an index before its first published level (the data side reports no
reading for it), key on a symbol (ISIN in, index name out), or read anything but its data seam.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from backtest.replay import SessionContext
from dataplatform.query.pit import Dataset

__all__ = [
    "SKIP_MONTHS",
    "TOP_K",
    "TOTAL_MONTHS",
    "IndexMomentum",
    "IndustryGateData",
    "IndustryGateOutcome",
    "apply_industry_gate",
    "rank_indices",
]

#: Sectoral indices kept each rebalance. Fixed (M16.2) — not a parameter.
TOP_K: Final = 5
#: The 6-1 return: from six months back to one month back, skipping the most recent month for the
#: same short-term-reversal reason the 12-1 stock signal skips it. Fixed (M16.2).
TOTAL_MONTHS: Final = 6
SKIP_MONTHS: Final = 1

_LABEL: Final = "sector_index_momentum_6_1"


@dataclass(frozen=True, slots=True)
class IndexMomentum:
    """One sectoral index's 6-1 return as of a decision date, from two published levels.

    ``start_level`` is the last level published on or before the six-month reference date,
    ``end_level`` the last on or before the one-month reference; ``momentum`` is
    ``end_level / start_level - 1``. ``knowable_date`` is the later of the two levels' publication
    dates — the guard refuses the reading if it is after the decision date.
    """

    index: str
    momentum: Decimal
    start_session: date
    start_level: Decimal
    end_session: date
    end_level: Decimal
    knowable_date: date

    def __post_init__(self) -> None:
        for name in ("momentum", "start_level", "end_level"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be a Decimal — never float (CLAUDE.md)")
        if self.start_level <= 0 or self.end_level <= 0:
            raise ValueError(f"index levels must be positive for {self.index}")
        if self.start_session > self.end_session:
            raise ValueError(f"{self.index}: the 6m level is dated after the 1m level")


@runtime_checkable
class IndustryGateData(Protocol):
    """What a momentum v2 data source adds to serve the gate.

    * ``sector_index_momentum(as_of)`` — one :class:`IndexMomentum` per mapped index that has
      published levels at both reference dates, as a guardable dataset;
    * ``is_classified(isin)`` — whether the classification names the ISIN at all. An unclassified
      name is *gate-neutral* (it passes): the classification is a 2026 snapshot that omits every
      name which died before it, so excluding the unclassified would drop the losers and keep the
      survivors (PR #97 review, B1);
    * ``sector_index_of(isin)`` — for a *classified* ISIN, the index its industry maps to, or
      ``None`` when that industry is unmapped (then the name is ineligible).
    """

    def is_classified(self, isin: str) -> bool:
        """Whether the classification names ``isin``."""

    def sector_index_momentum(self, as_of: date) -> Dataset[IndexMomentum]:
        """The admitted-to-be 6-1 readings as of ``as_of``."""

    def sector_index_of(self, isin: str) -> str | None:
        """The sectoral index a classified ``isin``'s industry maps to, or ``None`` if unmapped."""


class _Named(Protocol):
    @property
    def isin(self) -> str: ...


def rank_indices(readings: Sequence[IndexMomentum]) -> tuple[IndexMomentum, ...]:
    """Readings by 6-1 return, strongest first; ties broken by index name (deterministic)."""
    return tuple(sorted(readings, key=lambda r: (-r.momentum, r.index)))


@dataclass(frozen=True, slots=True)
class IndustryGateOutcome:
    """The session's ranking and the indices kept — what the gate decided, for the evidence."""

    ranked: tuple[IndexMomentum, ...]
    chosen: frozenset[str]
    unclassified_admitted: int = 0

    def evidence_items(self, session: date) -> tuple[EvidenceItem, ...]:
        """One item per ranked index, plus how many names passed only by being unclassified."""
        neutral = EvidenceItem(
            kind=EvidenceKind.POSITION,
            source="policy",
            label="industry_gate_unclassified_admitted",
            as_of=session,
            value=Decimal(self.unclassified_admitted),
            text="candidates not in the industry classification, passed gate-neutral",
        )
        return (*self._ranking_items(session), neutral)

    def _ranking_items(self, session: date) -> tuple[EvidenceItem, ...]:
        if not self.ranked:
            return (
                EvidenceItem(
                    kind=EvidenceKind.PRICE,
                    source="L1",
                    label=_LABEL,
                    as_of=session,
                    text="industry gate: no index has 6-1 levels; only unclassified names pass",
                ),
            )
        return tuple(
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="L1",
                label=_LABEL,
                as_of=session,
                value=reading.momentum,
                detail={
                    "index": reading.index,
                    "rank": str(rank),
                    "kept": "true" if reading.index in self.chosen else "false",
                    "start_session": reading.start_session.isoformat(),
                    "start_level": str(reading.start_level),
                    "end_session": reading.end_session.isoformat(),
                    "end_level": str(reading.end_level),
                },
            )
            for rank, reading in enumerate(self.ranked, start=1)
        )

    def annotate(self, evidence: EvidenceBundle) -> EvidenceBundle:
        """``evidence`` plus this gate's items, so the journal shows which sectors ruled."""
        return evidence.model_copy(
            update={"items": (*evidence.items, *self.evidence_items(evidence.trading_date))}
        )


def apply_industry_gate[R: _Named](
    data: object, ctx: SessionContext, candidates: Sequence[R]
) -> tuple[tuple[R, ...], IndustryGateOutcome]:
    """Drop the classified candidates whose industry is not mapped to a top-:data:`TOP_K` index.

    Reads the readings through ``ctx.pit.admit`` (a future-published level raises ``PitError``),
    ranks them and keeps the top five — fewer when fewer indices have six months of published
    history, none when none do. A candidate passes when its industry maps to a kept index, or when
    the classification does not name it at all (gate-neutral); a classified name in an unmapped
    industry or a non-top-5 index is dropped. Returns the survivors in their original order, with
    the outcome for the evidence. Refuses a data source that does not serve the gate.
    """
    if not isinstance(data, IndustryGateData):
        raise TypeError(
            "industry_gate is on but the momentum v2 data source does not serve sector-index "
            "readings (IndustryGateData) — wire backtest.sector_indices into it"
        )
    ranked = rank_indices(ctx.pit.admit(data.sector_index_momentum(ctx.session)))
    chosen = frozenset(reading.index for reading in ranked[:TOP_K])
    neutral = {c.isin for c in candidates if not data.is_classified(c.isin)}
    admitted = tuple(
        c for c in candidates if c.isin in neutral or data.sector_index_of(c.isin) in chosen
    )
    return admitted, IndustryGateOutcome(
        ranked=ranked, chosen=chosen, unclassified_admitted=len(neutral)
    )
