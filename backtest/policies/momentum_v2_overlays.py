"""M16.1 — momentum v2's optional overlays: absolute momentum, residual ranking, profitability.

Each is a switch on :class:`~backtest.policies.momentum_v2.MomentumV2Parameters`, off by default,
and each reads its own point-in-time input through its own seam, so a data source that serves
none of them (the daily paper job's, D13's own) is not asked for anything new:

* **A1 — absolute momentum** (``absolute_momentum``). A name the ranking put in the basket is held
  only if its 12-1 return beats what cash would have earned over the same span: the RBI repo rate
  less the 50 bp haircut idle cash already earns (:data:`backtest.cash_interest.HAIRCUT`),
  compounded over :data:`ABSOLUTE_MOMENTUM_HORIZON_DAYS`. A name that fails keeps its slot *in
  cash* — the next-ranked name is not promoted into it, which is the whole point: a market where
  most leaders trail cash is one to be partly out of. The rate is the one in force on the decision
  date (:class:`RepoRateReading`, knowable from its ``effective_from``). The
  ``absolute_momentum_hurdle`` evidence counts the slots the policy *intended* to leave in cash
  (``slots_in_cash`` / ``in_cash``) and the held failing names it *asked* to sell (``selling``) —
  not what the rails let through. Rails clear orders after the decision: A8's minimum-holdings
  floor (``analyst/rails/engine.py`` ``_min_holdings_breach``, eight names) refuses a sell that
  would take a book at or above the floor below it, so a failing name past that point stays held.
  Each refusal is journalled as its own ``RAIL_BLOCK`` entry, which is where to count them.
* **A3 — residual-momentum ranking** (``residual_ranking``). The 12-1 ranking key is replaced by the
  round-2 H1 residual-momentum score, exactly as pre-registered
  (``ops/studies/preregistration-signals-2026-09-29.md`` §3 and §6) and computed by
  :class:`~backtest.policies.residual_momentum.ResidualMomentumPanel` — nothing here redefines it.
  A name the panel excludes (``None``: under 200 valid sessions) cannot be ranked and is left out
  of the ranking; every other part of the decision is unchanged.
* **A4 — profitability filter** (``profitability_filter``). A candidate is eligible only if its
  trailing-twelve-month profit after tax is positive and its newest filing is at most
  :data:`PROFITABILITY_MAX_STALENESS_DAYS` old on the decision date. TTM profit is
  :func:`~dataplatform.query.fundamentals_metrics.compute_metrics`'s ``earnings_ttm``, so A4
  inherits its rules rather than stating new ones:

  - **One nature, never mixed** (``_pick_nature``): consolidated whenever the company has *ever*
    filed a consolidated quarterly ``profit_after_tax`` knowable on the date, else standalone. The
    four quarters are summed within that one nature only.
  - **Owners' share first** (``_earnings_series``): ``profit_attributable_to_owners`` when every
    quarter of the run states it, else ``profit_after_tax`` (the bottom line).
  - **Banks and NBFCs.** The banking taxonomy's ``ProfitLossForThePeriod`` maps to
    ``profit_after_tax`` and its "after minority interest" element to
    ``profit_attributable_to_owners`` (``dataplatform/ingest/xbrl/models.py``
    ``_BANKING_OWNERS_CONCEPTS`` and ``BANKING_CONCEPTS``), so a bank is screened on the same
    keys. NBFCs file the Ind-AS NBFC entry point, which carries the whole Ind-AS P&L spine and
    reads with the Ind-AS vocabulary.
  - **Staleness is measured from the newest filing of the chosen nature.** So a company that has
    stopped filing consolidated results but still files fresh standalone ones stays on the
    consolidated nature and is ineligible once its last consolidated filing is over 200 days old.

  A name with fewer than four consecutive quarters (of that nature), or no filing at all, is
  ineligible while the filter is on.

Point-in-time (invariant #7): each input is a :class:`~dataplatform.query.pit.Dataset` whose
``knowable_date`` is the date it became known — the repo change's effective date, the newest filing
date, the last session the residual window reads — and the policy reads each through
``ctx.pit.admit``, so a future-dated rate, filing or price raises
:class:`~dataplatform.query.pit.PitError` rather than reaching a decision.

What it never does: read a clock or a store, take a float as money or a rate, or rank a name on a
signal it could not compute.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from analyst.journal.evidence import EvidenceItem, EvidenceKind
from backtest.cash_interest import HAIRCUT
from dataplatform.query.pit import Dataset

if TYPE_CHECKING:
    from backtest.policies.momentum_v2 import MomentumV2Record

__all__ = [
    "ABSOLUTE_MOMENTUM_HORIZON_DAYS",
    "PROFITABILITY_MAX_STALENESS_DAYS",
    "AbsoluteMomentumData",
    "ProfitabilityData",
    "ProfitabilityReading",
    "RepoRateReading",
    "ResidualRankingData",
    "ResidualScore",
    "absolute_momentum_evidence",
    "profitability_evidence",
    "profitable",
    "residual_scores",
]

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_DAYS_IN_YEAR: Final = Decimal("365")
_HURDLE_QUANTUM: Final = Decimal("0.00000001")

#: The span the 12-1 return covers: from twelve months back (365 days) to one month back (30 days),
#: the same two calendar steps the L1 source strikes ``momentum_12_1`` on. The cash hurdle is
#: compounded over exactly this span so the comparison is like for like.
ABSOLUTE_MOMENTUM_HORIZON_DAYS: Final = 365 - 30

#: A name whose newest filing is older than this on the decision date is ineligible under A4: two
#: missed quarterly deadlines, the same a-priori limit the M10.6 fundamentals arms use.
PROFITABILITY_MAX_STALENESS_DAYS: Final = 200


# ── A1: the cash hurdle ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RepoRateReading:
    """The policy repo rate in force on a decision date, and when it became knowable.

    ``repo_rate`` is a ratio (6.25 % is ``Decimal("0.0625")``); ``effective_from`` is the date the
    change took effect, which is also ``knowable_date`` — RBI announces a change and it applies
    from that date, so a rate is never known before it is in force.
    """

    repo_rate: Decimal
    effective_from: date
    knowable_date: date

    def __post_init__(self) -> None:
        if not isinstance(self.repo_rate, Decimal):
            raise TypeError("a repo rate is a Decimal — never float (CLAUDE.md)")
        if self.repo_rate <= HAIRCUT:
            raise ValueError(f"repo rate {self.repo_rate} is not above the {HAIRCUT} haircut")

    @property
    def hurdle(self) -> Decimal:
        """``(1 + repo - haircut) ** (335/365) - 1`` — cash's return over the 12-1 span, 8 dp."""
        annual = _ONE + self.repo_rate - HAIRCUT
        years = Decimal(ABSOLUTE_MOMENTUM_HORIZON_DAYS) / _DAYS_IN_YEAR
        return (annual**years - _ONE).quantize(_HURDLE_QUANTUM)


@runtime_checkable
class AbsoluteMomentumData(Protocol):
    """The A1 seam: the repo rate in force on ``as_of`` as a one-element guardable dataset."""

    def repo_rate(self, as_of: date) -> Dataset[RepoRateReading]:
        """The :class:`RepoRateReading` in force on ``as_of``."""


def absolute_momentum_evidence(
    session: date,
    reading: RepoRateReading,
    chosen: Iterable[MomentumV2Record],
    held_in_cash: Iterable[str],
    selling: Iterable[str] = (),
) -> EvidenceItem:
    """The hurdle a rebalance applied, the basket slots it left in cash, and the sells it asked for.

    Both are the policy's *intent*. A sell the minimum-holdings rail refuses still appears in
    ``selling`` here; the refusal is its own ``RAIL_BLOCK`` journal entry.
    """
    cash = sorted(held_in_cash)
    sells = sorted(selling)
    return EvidenceItem(
        kind=EvidenceKind.PRICE,
        source="repo_rates",
        label="absolute_momentum_hurdle",
        as_of=session,
        value=reading.hurdle,
        detail={
            "repo_rate": str(reading.repo_rate),
            "repo_effective_from": reading.effective_from.isoformat(),
            "haircut": str(HAIRCUT),
            "horizon_days": str(ABSOLUTE_MOMENTUM_HORIZON_DAYS),
            "slots": str(len(list(chosen))),
            "slots_in_cash": str(len(cash)),
            "in_cash": ",".join(cash),
            "selling": ",".join(sells),
        },
        text=(
            "12-1 return must beat cash over the same span; a failing slot stays in cash "
            "(intended; rail refusals are journalled as RAIL_BLOCK)"
        ),
    )


# ── A3: the residual-momentum ranking key ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ResidualScore:
    """One name's H1 residual-momentum score as of a decision date (``None`` when excluded).

    ``knowable_date`` is the last session the score's window reads — ``t-1`` — never the decision
    session itself (the panel's window ends before it).
    """

    isin: str
    score: Decimal | None
    knowable_date: date

    def __post_init__(self) -> None:
        if self.score is not None and not isinstance(self.score, Decimal):
            raise TypeError("a residual-momentum score is a Decimal")


@runtime_checkable
class ResidualRankingData(Protocol):
    """The A3 seam: each candidate's residual-momentum score as of ``as_of``."""

    def residual_momentum(self, as_of: date) -> Dataset[ResidualScore]:
        """A :class:`ResidualScore` per candidate of ``signal(as_of)``."""


# ── A4: the profitability screen ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProfitabilityReading:
    """One name's trailing-twelve-month profit as of a decision date, from PIT filings.

    ``earnings_ttm`` is ``None`` when fewer than four consecutive quarters were knowable;
    ``knowable_date`` is the newest filing that entered it — what the PIT guard checks and what the
    staleness limit is measured from.
    """

    isin: str
    earnings_ttm: Decimal | None
    knowable_date: date

    def __post_init__(self) -> None:
        if self.earnings_ttm is not None and not isinstance(self.earnings_ttm, Decimal):
            raise TypeError("TTM earnings are a Decimal — never float (CLAUDE.md)")

    def eligible(self, as_of: date) -> bool:
        """Positive TTM profit and a newest filing at most the staleness limit old on ``as_of``."""
        return (
            self.earnings_ttm is not None
            and self.earnings_ttm > _ZERO
            and (as_of - self.knowable_date).days <= PROFITABILITY_MAX_STALENESS_DAYS
        )


@runtime_checkable
class ProfitabilityData(Protocol):
    """The A4 seam: a :class:`ProfitabilityReading` per candidate with any knowable filing."""

    def profitability(self, as_of: date) -> Dataset[ProfitabilityReading]:
        """Readings as of ``as_of``; a candidate with no reading is ineligible."""


def profitable(
    candidates: Iterable[MomentumV2Record],
    readings: Iterable[ProfitabilityReading],
    *,
    as_of: date,
) -> tuple[MomentumV2Record, ...]:
    """The candidates A4 leaves eligible, in their original order; a missing reading is not one."""
    eligible = {reading.isin for reading in readings if reading.eligible(as_of)}
    return tuple(record for record in candidates if record.isin in eligible)


def profitability_evidence(session: date, offered: int, eligible: int) -> EvidenceItem:
    """How many candidates A4 offered and kept on this rebalance."""
    return EvidenceItem(
        kind=EvidenceKind.POSITION,
        source="pit_fundamentals",
        label="profitability_eligible",
        as_of=session,
        value=Decimal(eligible),
        detail={
            "offered": str(offered),
            "max_staleness_days": str(PROFITABILITY_MAX_STALENESS_DAYS),
        },
        text="TTM profit after tax > 0 with a filing in the last 200 days",
    )


def residual_scores(scores: Iterable[ResidualScore]) -> Mapping[str, Decimal]:
    """The rankable scores by ISIN — an excluded (``None``) name is left out, never zero-filled."""
    return {s.isin: s.score for s in scores if s.score is not None}
