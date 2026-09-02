"""A9: the §5.7 evidence pack — the graduation evidence, assembled from the journal.

The plan (§5.7, decision #8) makes graduation from paper to real money *your* discretionary call,
and this pack is what you read to make it: returns against both benchmarks, the drawdown profile,
the rail-breach count, a review of the monitoring tiers' verdicts against what actually happened,
turnover and tax events by sleeve, the token/cost burn, and the days data quality forced a skip.
It is auto-generated monthly and on a graduation request, and two properties are load-bearing:

* **It reads in ten minutes.** One document, one section per §5.7 clause, the numbers that decide
  the question at the top and the supporting detail below. `render_markdown` produces exactly that.

* **It reconstructs from the journal alone.** Every figure the pack attributes to the case's own
  activity — turnover, tax events, cost burn, rail breaches, data-quality skips, the monitoring
  verdicts, and the cashflow stream that drives the return — is derived here from
  `decision_journal` rows, not from a parallel ledger this module keeps. There *is* no parallel
  ledger: §0 says the journal is the product, and a pack that agreed with a second book the journal
  did not know about would be evidence for that book, not for the record graduation rests on. The
  two figures that are irreducibly *marks* rather than decisions — the portfolio's terminal value
  and the benchmarks' index levels — arrive as an explicit `PackValuation` input and are labelled
  as marks wherever they appear, because a price is a market fact, not something the agent decided
  and journaled.

The rail-breach count is not a neutral metric and the pack does not present it as one. Rails block
by construction (invariant #6), so a breach that reached the book is a bug in the rails, not a risk
event that was correctly handled: the section's verdict is CLEAN at zero and BUGS_PRESENT above it,
and the rendered pack says so in those words. A reader skimming for a number must not mistake "one
breach" for "one close call".

Money is `Decimal` throughout (CLAUDE.md); XIRR is delegated to `backtest.xirr` (the M4.6 engine,
one implementation); the only join key is the ISIN (invariant #2); and nothing here reads a clock —
`generated_at` is supplied by the caller from an injected `Clock` (B10) so a regenerated pack over
the same journal window is byte-identical but for that stamp.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from string import Template
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from analyst.journal.models import Actor, Decision, Money, RecordedEntry, Sleeve
from analyst.journal.writer import Journal, JournalFilter
from backtest.xirr import Cashflow, XIRRError, xirr
from dataplatform.ingest.indices import TriSeries
from dataplatform.logging import get_logger

__all__ = [
    "CostBurnSection",
    "DataQualitySection",
    "DecisionReview",
    "DecisionReviewSection",
    "DrawdownSection",
    "EvidencePack",
    "EvidencePackError",
    "NavPoint",
    "PackTrigger",
    "PackValuation",
    "RailBreachSection",
    "ReturnsSection",
    "SleeveTurnover",
    "TaxEvent",
    "TierCost",
    "TurnoverTaxSection",
    "generate_pack",
    "render_markdown",
]

_LOG = get_logger(__name__)

_ZERO: Final = Decimal("0")

#: The document skeleton lives in a template file so the wording and section order a human reads is
#: editable without touching the arithmetic. The section bodies (which need loops over journal rows)
#: are composed here and substituted in.
_TEMPLATE_PATH: Final = Path(__file__).resolve().parent / "templates" / "pack.md.tmpl"

#: The monitoring tiers whose verdicts the decision review covers (§5.7). T0 is mechanical and
#: renders no thesis verdict; the human and the platform are not tiers under review.
_REVIEW_ACTORS: Final[frozenset[Actor]] = frozenset({Actor.T1, Actor.T2})

#: Cash sources that are *external* money into the account (a return's pay-in), as opposed to
#: internally recycled proceeds. Only these enter the XIRR cashflow stream — recycling exit
#: proceeds through the parking ETF is a move inside the account, not a fresh deposit.
_EXTERNAL_PARK_SOURCE_KEYS: Final[tuple[str, ...]] = ("source_SIP_INSTALMENT", "source_TOP_UP")

#: How a trade entry's gross rupee value is read from its payload, in the order tried. A park BUY
#: (A7) records `shares`+`price`; a deployment records `deployed_inr`; a `value_inr` is honoured for
#: any writer that adopts the canonical key. A trade whose value none of these can supply is counted
#: as untraceable and surfaced, never silently treated as zero.
_DEPLOYED_KEY: Final = "deployed_inr"
_VALUE_KEY: Final = "value_inr"
_SHARES_KEY: Final = "shares"
_PRICE_KEY: Final = "price"

#: Tax-event payload keys a SELL entry carries when the exit path records its realized outcome.
_REALIZED_GAIN_KEY: Final = "realized_gain_inr"
_CHARGES_KEY: Final = "charges_inr"
_HOLDING_PERIOD_KEY: Final = "holding_period"


class EvidencePackError(Exception):
    """The pack cannot be assembled from what it was given — a loud failure (CLAUDE.md)."""


class PackTrigger(StrEnum):
    """Why the pack was generated (§5.7: monthly, or on a graduation request)."""

    MONTHLY = "MONTHLY"
    """The scheduled monthly generation."""

    GRADUATION = "GRADUATION"
    """A graduation request — the evidence for decision #8."""


class NavPoint(BaseModel):
    """The portfolio's marked value on one session — a mark, not a decision.

    The NAV series is the one input the drawdown profile and the return's terminal value cannot come
    from the journal: it is what the holdings were *worth*, which is a market fact. It is carried as
    an explicit input for exactly that reason, and the pack labels every figure derived from it as a
    mark so a reader never mistakes it for something the agent decided.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: date = Field(description="The session this mark is for, in IST.")
    nav_inr: Money = Field(description="Net asset value: free cash plus marked holdings. Decimal.")


class PackValuation(BaseModel):
    """The market marks the return and drawdown sections need, alongside the journal.

    Everything else in the pack is journal-derived; these three are not, and are grouped here so the
    boundary is a single, visible argument rather than a scattering of optional prices. The NAV
    series dates the drawdown profile and supplies the terminal value; the two TRI series are the
    benchmark legs the return is measured against (§5.2, "NIFTY-TRI + theme proxy").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    nav_series: tuple[NavPoint, ...] = Field(
        min_length=1,
        description="Portfolio NAV per session, ascending; the last point is terminal.",
    )
    benchmark: TriSeries = Field(description="The broad-market total-return series (NIFTY-TRI).")
    theme: TriSeries = Field(description="The theme-proxy total-return series.")


# ── sections ─────────────────────────────────────────────────────────────────────────────────────


class ReturnsSection(BaseModel):
    """Money-weighted return since the first SIP, beside both benchmarks (§5.7).

    The cashflow stream is the case's actual SIP instalments, read from the journal's park entries;
    the terminal value is the last NAV mark. Each benchmark XIRR replays the *same* cashflows into
    that index, so the excess figures are like-for-like — same money, same dates, a different
    destination (§5.2). `benchmark_xirr`/`theme_xirr` are the one place a market mark (the index
    level) enters a return number, and the section names that in `basis`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    first_sip_date: date = Field(description="The earliest SIP instalment in the window.")
    as_of: date = Field(description="The terminal date the return is struck on.")
    total_invested_inr: Money = Field(
        description="Sum of SIP instalments paid in, from the journal."
    )
    terminal_value_inr: Money = Field(description="Terminal NAV mark — a market value, not a flow.")
    portfolio_xirr: Decimal = Field(description="Money-weighted return of the case's cashflows.")
    benchmark_xirr: Decimal = Field(description="Same cashflows into NIFTY-TRI.")
    theme_xirr: Decimal = Field(description="Same cashflows into the theme proxy.")
    cashflow_count: int = Field(ge=1, description="How many SIP pay-ins fed the return.")

    @property
    def excess_over_benchmark(self) -> Decimal:
        """Portfolio XIRR minus the broad-market benchmark's — the value added over the index."""
        return self.portfolio_xirr - self.benchmark_xirr

    @property
    def excess_over_theme(self) -> Decimal:
        """Portfolio XIRR minus the theme proxy's."""
        return self.portfolio_xirr - self.theme_xirr


class DrawdownSection(BaseModel):
    """The worst peak-to-trough fall in the NAV series, and where the case sits now (§5.7).

    Drawdown is measured on the NAV marks (a market series), so it is labelled a mark-derived
    figure; the dates that bound it are real sessions, which lets a reader line the trough up
    against the journal to see what the agent did on the way down.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    peak_value_inr: Money = Field(description="The highest NAV reached before the worst trough.")
    peak_date: date = Field(description="The session of that peak.")
    trough_value_inr: Money = Field(description="The lowest NAV after that peak.")
    trough_date: date = Field(description="The session of that trough.")
    max_drawdown_pct: Decimal = Field(description="Worst peak-to-trough fall, as a percentage.")
    current_drawdown_pct: Decimal = Field(
        description="Fall from the running peak to the last mark, as a percentage."
    )


class RailBreachSection(BaseModel):
    """Rail breaches that reached the book — a bug count, not a risk metric (§5.7).

    Rails are unbypassable (invariant #6): every A5/A6/A7 order clears A8, and A8 blocks
    deterministically. So the target is zero, and a non-zero count does not mean risk was managed —
    it means the rails let something through that they exist to stop, which is a defect. The verdict
    is CLEAN only at zero; anything above it is BUGS_PRESENT, and the renderer says so in words.
    A `RAIL_BLOCK` journal line is a rail doing its job (an order refused) and is *not* a breach; a
    breach is an order that violated a cap and was filled anyway, which the journal would show as a
    trade the rails should have stopped. This section counts what the journal marks as breaches.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    breach_count: int = Field(
        ge=0, description="Rail breaches that reached the book. Target: zero."
    )
    rail_blocks: int = Field(
        ge=0, description="Orders A8 correctly refused (RAIL_BLOCK lines) — context, not breaches."
    )
    breaches: tuple[Mapping[str, str], ...] = Field(
        default=(), description="One record per breach: date, isin, the caps violated."
    )

    @property
    def is_clean(self) -> bool:
        """True only when no breach reached the book — the sole acceptable state (invariant #6)."""
        return self.breach_count == 0

    @property
    def verdict(self) -> str:
        """CLEAN at zero breaches, BUGS_PRESENT above it — never a neutral label."""
        return "CLEAN" if self.is_clean else "BUGS_PRESENT"


class DecisionReview(BaseModel):
    """One monitoring verdict, paired with what the agent did about it afterwards (§5.7).

    The verdict is what a T1/T2 review concluded about a break condition; the outcome is whether a
    trade on that instrument followed. A `BROKEN` verdict with no subsequent exit is the pairing
    this review exists to surface — a thesis the agent called broken but did not act on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trading_date: date = Field(description="The session the review was made about.")
    actor: Actor = Field(description="Which tier reviewed — T1 or T2.")
    isin: str | None = Field(default=None, description="The instrument reviewed, if about one.")
    break_condition_id: str = Field(description="The break-condition id evaluated.")
    verdict: str = Field(description="INTACT / WEAKENED / BROKEN.")
    observed: str | None = Field(default=None, description="What was observed, in one line.")
    subsequent_action: str = Field(
        description="The outcome: a later BUY/SELL on the ISIN, or NONE if nothing followed."
    )
    action_date: date | None = Field(
        default=None, description="When that action was taken, if any."
    )


class DecisionReviewSection(BaseModel):
    """The monitoring tiers' verdicts against subsequent outcomes (§5.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviews: tuple[DecisionReview, ...] = Field(
        default=(), description="Every T1/T2 break-condition verdict in the window."
    )
    broken_unactioned: int = Field(
        ge=0, description="BROKEN verdicts with no subsequent trade — the review's headline risk."
    )


class SleeveTurnover(BaseModel):
    """Turnover in one sleeve: what was traded, on how many trades (§5.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sleeve: Sleeve = Field(description="CORE, TACTICAL or CASH (§5.5).")
    buy_value_inr: Money = Field(description="Gross rupee value bought in the sleeve.")
    sell_value_inr: Money = Field(description="Gross rupee value sold in the sleeve.")
    trade_count: int = Field(ge=0, description="Buys plus sells in the sleeve.")

    @property
    def turnover_inr(self) -> Money:
        """Total two-way turnover in the sleeve — buys plus sells."""
        return self.buy_value_inr + self.sell_value_inr


class TaxEvent(BaseModel):
    """A realized, tax-relevant outcome — a sell's gain and its holding-period class (§5.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trading_date: date = Field(description="The session the sell settled on.")
    isin: str = Field(description="The instrument sold. ISIN only (invariant #2).")
    sleeve: Sleeve = Field(description="The sleeve the sell belonged to.")
    realized_gain_inr: Money = Field(description="Realized gain (or loss) booked on the sell.")
    charges_inr: Money = Field(
        description="Sell-side charges (STT, DP, GST) — the tax already paid."
    )
    holding_period: str = Field(description="LONG or SHORT, as the exit recorded it.")


class TurnoverTaxSection(BaseModel):
    """Turnover and tax events by sleeve (§5.7).

    `untraceable_trades` is not padding: a trade whose rupee value cannot be read from its journal
    entry breaks the "reconstructable from the journal alone" property, so it is counted and named
    rather than dropped — a zero here is part of what the pack asserts, not an absence of trouble.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_sleeve: tuple[SleeveTurnover, ...] = Field(
        default=(), description="Per-sleeve turnover, ordered CORE, TACTICAL, CASH."
    )
    tax_events: tuple[TaxEvent, ...] = Field(
        default=(), description="Realized tax events in the window, earliest first."
    )
    untraceable_trades: tuple[Mapping[str, str], ...] = Field(
        default=(),
        description="Trades whose value the journal did not carry — a reconstructability defect.",
    )

    @property
    def total_turnover_inr(self) -> Money:
        """Two-way turnover across every sleeve."""
        return sum((sleeve.turnover_inr for sleeve in self.by_sleeve), _ZERO)


class TierCost(BaseModel):
    """Token and rupee burn for one actor tier (§5.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    actor: Actor = Field(description="The tier that spent — T1, T2, or any model-calling actor.")
    tokens_in: int = Field(ge=0, description="Prompt tokens billed to the tier.")
    tokens_out: int = Field(ge=0, description="Completion tokens billed to the tier.")
    cost_inr: Money = Field(description="Settled rupee cost for the tier.")
    call_count: int = Field(ge=0, description="How many model calls the tier made.")


class CostBurnSection(BaseModel):
    """Token/cost burn, by tier and in total (§5.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_tier: tuple[TierCost, ...] = Field(
        default=(), description="Per-tier burn, only for tiers that spent anything."
    )
    total_cost_inr: Money = Field(description="Total rupee burn across all tiers.")
    total_tokens_in: int = Field(ge=0, description="Total prompt tokens.")
    total_tokens_out: int = Field(ge=0, description="Total completion tokens.")


class DataQualitySection(BaseModel):
    """Days data quality forced the loop to skip trading (§5.7, invariant #10).

    A `SKIPPED_DATA_RED` is the loop reading `/status/sync`, finding it not green, and declining to
    trade — exactly the behaviour invariant #10 requires. It belongs in the pack because a case that
    skipped many sessions was flying blind on those days, which bears on whether its record is a
    fair basis for graduation. Auth-required skips (a lapsed broker session) are a precondition
    and are counted separately so the two are not conflated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    data_red_skips: int = Field(ge=0, description="Sessions skipped because data was not green.")
    auth_required_skips: int = Field(
        ge=0, description="Sessions skipped because broker auth lapsed."
    )
    skipped_dates: tuple[date, ...] = Field(
        default=(), description="The sessions a data-red skip occurred on, earliest first."
    )


# ── the pack ───────────────────────────────────────────────────────────────────────────────────


class EvidencePack(BaseModel):
    """The whole §5.7 pack: header, then one section per clause.

    Frozen and self-describing: a stored pack is the evidence, and it carries which case, which
    window, and why it was generated so a reader years on needs nothing else to place it. Rendering
    is separate (`render_markdown`) — the model is the data, the template is the presentation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1, description="The case this pack is about.")
    trigger: PackTrigger = Field(description="Monthly or graduation request.")
    window_start: date = Field(description="First session the pack covers (inclusive).")
    window_end: date = Field(description="Last session the pack covers (inclusive).")
    generated_at: datetime = Field(
        description="When the pack was generated, from an injected Clock."
    )
    entries_reviewed: int = Field(ge=0, description="Journal entries the pack was built from.")

    returns: ReturnsSection
    drawdown: DrawdownSection
    rail_breaches: RailBreachSection
    decision_review: DecisionReviewSection
    turnover_tax: TurnoverTaxSection
    cost_burn: CostBurnSection
    data_quality: DataQualitySection


# ── generation ─────────────────────────────────────────────────────────────────────────────────


def generate_pack(
    journal: Journal,
    *,
    case_id: str,
    window_start: date,
    window_end: date,
    valuation: PackValuation,
    generated_at: datetime,
    trigger: PackTrigger = PackTrigger.MONTHLY,
) -> EvidencePack:
    """Assemble the §5.7 evidence pack for one case over one window, from the journal.

    What it does: reads every journal entry for `case_id` between `window_start` and `window_end`
    (inclusive, on trading date), and derives each §5.7 section from those rows — combining them
    with the `valuation` marks only where a return or a drawdown needs a market value. Returns
    a frozen `EvidencePack`; `render_markdown` turns it into the ten-minute read.

    What it assumes: the window's SIP instalments are journaled as A7 park entries carrying their
    source amounts, trades carry their rupee value in the payload, and `valuation.nav_series` marks
    the case's value across the window with a terminal point. `generated_at` came from an injected
    clock (B10).

    What it never does: keep a second ledger. Every case-activity figure traces to a journal entry;
    the only non-journal numbers are the NAV and index marks, which are labelled as marks. It raises
    `EvidencePackError` rather than emitting a pack whose return has no defined rate or whose window
    holds no entries — a silent, empty pack is worse than a loud refusal (CLAUDE.md).
    """
    if window_start > window_end:
        raise EvidencePackError(
            f"empty pack window: start {window_start.isoformat()} is after end "
            f"{window_end.isoformat()}"
        )
    if generated_at.tzinfo is None or generated_at.tzinfo.utcoffset(generated_at) is None:
        raise EvidencePackError(
            f"generated_at must be tz-aware, got naive {generated_at.isoformat()!r}; take it from "
            "an injected Clock"
        )

    entries = journal.entries(JournalFilter(case_id=case_id, start=window_start, end=window_end))
    if not entries:
        raise EvidencePackError(
            f"no journal entries for case {case_id!r} between {window_start.isoformat()} and "
            f"{window_end.isoformat()}: there is nothing to build a pack from"
        )
    # Oldest first — every section reasons forward in time (a verdict then its outcome, a peak then
    # its trough). `Journal.entries` returns newest first, so reverse once here.
    ordered = tuple(reversed(entries))

    returns = _returns_section(ordered, valuation)
    pack = EvidencePack(
        case_id=case_id,
        trigger=trigger,
        window_start=window_start,
        window_end=window_end,
        generated_at=generated_at,
        entries_reviewed=len(ordered),
        returns=returns,
        drawdown=_drawdown_section(valuation),
        rail_breaches=_rail_breach_section(ordered),
        decision_review=_decision_review_section(ordered),
        turnover_tax=_turnover_tax_section(ordered),
        cost_burn=_cost_burn_section(ordered),
        data_quality=_data_quality_section(ordered),
    )
    _LOG.info(
        "journal.evidence_pack.generated",
        case_id=case_id,
        trigger=trigger.value,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        entries=len(ordered),
        portfolio_xirr=str(returns.portfolio_xirr),
        rail_breaches=pack.rail_breaches.breach_count,
        rail_verdict=pack.rail_breaches.verdict,
        total_cost_inr=str(pack.cost_burn.total_cost_inr),
        data_red_skips=pack.data_quality.data_red_skips,
    )
    return pack


def _sip_cashflows(entries: Sequence[RecordedEntry]) -> list[Cashflow]:
    """The external SIP pay-ins in the window, from the journal's park entries.

    An A7 park of a SIP instalment records the instalment amount under a `source_*` payload key
    (`analyst.cash.manager`), which is the money that entered the account — the pay-in a return is
    struck on. Exit proceeds recycled through the same parking are internal and are excluded (only
    `_EXTERNAL_PARK_SOURCE_KEYS` count). Each pay-in is negative, in XIRR's sign convention.
    """
    flows: list[Cashflow] = []
    for entry in entries:
        if entry.decision is not Decision.BUY or entry.sleeve is not Sleeve.CASH:
            continue
        paid_in = _ZERO
        for key in _EXTERNAL_PARK_SOURCE_KEYS:
            raw = entry.payload.get(key)
            if raw is not None:
                paid_in += Decimal(raw)
        if paid_in > _ZERO:
            flows.append(Cashflow(entry.trading_date, -paid_in))
    return flows


def _returns_section(entries: Sequence[RecordedEntry], valuation: PackValuation) -> ReturnsSection:
    """Portfolio and benchmark XIRR from journal cashflows and the terminal NAV mark."""
    pay_ins = _sip_cashflows(entries)
    if not pay_ins:
        raise EvidencePackError(
            "no SIP instalments found in the journal window: a return since SIP needs at least one "
            "pay-in, and none of the CASH-sleeve BUY entries carried a source amount"
        )
    terminal = valuation.nav_series[-1]
    stream = [*pay_ins, Cashflow(terminal.as_of, terminal.nav_inr)]
    try:
        portfolio_xirr = xirr(stream)
        benchmark_xirr = _benchmark_xirr(pay_ins, valuation.benchmark, terminal.as_of)
        theme_xirr = _benchmark_xirr(pay_ins, valuation.theme, terminal.as_of)
    except XIRRError as exc:
        raise EvidencePackError(f"the case's return has no defined rate: {exc}") from exc
    return ReturnsSection(
        first_sip_date=min(flow.when for flow in pay_ins),
        as_of=terminal.as_of,
        total_invested_inr=-sum((flow.amount for flow in pay_ins), _ZERO),
        terminal_value_inr=terminal.nav_inr,
        portfolio_xirr=portfolio_xirr,
        benchmark_xirr=benchmark_xirr,
        theme_xirr=theme_xirr,
        cashflow_count=len(pay_ins),
    )


def _benchmark_xirr(pay_ins: Sequence[Cashflow], series: TriSeries, as_of: date) -> Decimal:
    """XIRR of the same pay-ins replayed into one total-return series (the §5.2 comparison).

    Each pay-in buys units at the index level on its date; the accumulated units are marked at the
    terminal level. Same money, same dates, a different destination — so the excess is a true read
    of what the strategy added over the index.
    """
    units = _ZERO
    for flow in pay_ins:
        level = _tri_level_asof(series, flow.when)
        units -= flow.amount / level  # flow.amount < 0 (a pay-in), so units grow
    terminal_value = units * _tri_level_asof(series, as_of)
    return xirr([*pay_ins, Cashflow(as_of, terminal_value)])


def _tri_level_asof(series: TriSeries, on: date) -> Decimal:
    """The TRI level in force on `on` — the last point dated on or before it (a PIT step)."""
    level: Decimal | None = None
    for point in series.points:  # ascending by construction (TriSeries validates)
        if point.as_of <= on:
            level = point.tri_value
        else:
            break
    if level is None:
        raise EvidencePackError(
            f"no benchmark level for {on.isoformat()}: it precedes {series.index_slug}'s history"
        )
    return level


def _drawdown_section(valuation: PackValuation) -> DrawdownSection:
    """Worst peak-to-trough fall over the NAV marks, and the fall to the last mark."""
    points = valuation.nav_series
    peak_value = points[0].nav_inr
    peak_date = points[0].as_of
    worst_peak_value = peak_value
    worst_peak_date = peak_date
    worst_trough_value = peak_value
    worst_trough_date = peak_date
    max_dd = _ZERO
    for point in points:
        if point.nav_inr > peak_value:
            peak_value = point.nav_inr
            peak_date = point.as_of
        if peak_value > _ZERO:
            drawdown = (peak_value - point.nav_inr) / peak_value * Decimal("100")
            if drawdown > max_dd:
                max_dd = drawdown
                worst_peak_value = peak_value
                worst_peak_date = peak_date
                worst_trough_value = point.nav_inr
                worst_trough_date = point.as_of
    running_peak = max(point.nav_inr for point in points)
    last = points[-1].nav_inr
    current_dd = (
        (running_peak - last) / running_peak * Decimal("100") if running_peak > _ZERO else _ZERO
    )
    return DrawdownSection(
        peak_value_inr=worst_peak_value,
        peak_date=worst_peak_date,
        trough_value_inr=worst_trough_value,
        trough_date=worst_trough_date,
        max_drawdown_pct=max_dd,
        current_drawdown_pct=current_dd,
    )


def _rail_breach_section(entries: Sequence[RecordedEntry]) -> RailBreachSection:
    """Rail blocks (rails working) counted for context; breaches (rails failing) counted as bugs.

    A `RAIL_BLOCK` is an order A8 refused — the rail did its job, so it is context, not a breach. A
    breach is a filled trade that violated a cap the rails should have caught; the journal flags
    such a trade with a `rail_breach` payload marker, the only thing counted into `breach_count`.
    In a correct system that count is always zero (invariant #6), which is exactly why a non-zero
    one is a bug and the section says so.
    """
    rail_blocks = 0
    breaches: list[Mapping[str, str]] = []
    for entry in entries:
        if entry.decision is Decision.RAIL_BLOCK:
            rail_blocks += 1
        if entry.payload.get("rail_breach") == "true":
            breaches.append(
                {
                    "trading_date": entry.trading_date.isoformat(),
                    "isin": entry.isin or "—",
                    "rails": entry.payload.get("rails", "unspecified"),
                    "decision": entry.decision.value,
                }
            )
    return RailBreachSection(
        breach_count=len(breaches), rail_blocks=rail_blocks, breaches=tuple(breaches)
    )


def _decision_review_section(entries: Sequence[RecordedEntry]) -> DecisionReviewSection:
    """T1/T2 verdicts paired with the trade (if any) that followed on the same instrument."""
    reviews: list[DecisionReview] = []
    broken_unactioned = 0
    for entry in entries:
        if entry.actor not in _REVIEW_ACTORS or not entry.break_conditions_evaluated:
            continue
        # A trade (BUY/SELL) is the *outcome* of a review, not a review — it may carry the verdict
        # that justified it, but counting it here would double-count that verdict and, worse, flag
        # the exit itself as a BROKEN-with-no-action. The review is the non-trade entry (a HOLD, an
        # ESCALATE) that concluded the verdict; the trade is what `_subsequent_action` pairs to it.
        if entry.decision in (Decision.BUY, Decision.SELL):
            continue
        action, action_date = _subsequent_action(entries, entry)
        for evaluation in entry.break_conditions_evaluated:
            reviews.append(
                DecisionReview(
                    trading_date=entry.trading_date,
                    actor=entry.actor,
                    isin=entry.isin,
                    break_condition_id=evaluation.id,
                    verdict=evaluation.verdict.value,
                    observed=evaluation.observed,
                    subsequent_action=action,
                    action_date=action_date,
                )
            )
            if evaluation.verdict.value == "BROKEN" and action == "NONE":
                broken_unactioned += 1
    return DecisionReviewSection(reviews=tuple(reviews), broken_unactioned=broken_unactioned)


def _subsequent_action(
    entries: Sequence[RecordedEntry], review: RecordedEntry
) -> tuple[str, date | None]:
    """The first BUY/SELL on the reviewed instrument at or after the review — its outcome.

    "At or after" on the trading date, because a review and the exit it triggers can land on one
    session. Only trades on the *same* ISIN count: an exit of a different holding is not this
    review's outcome. Returns ``("NONE", None)`` when nothing followed.
    """
    if review.isin is None:
        return "NONE", None
    for entry in entries:
        if (
            entry.isin == review.isin
            and entry.decision in (Decision.BUY, Decision.SELL)
            and entry.trading_date >= review.trading_date
            and entry.id > review.id
        ):
            return entry.decision.value, entry.trading_date
    return "NONE", None


def _trade_value_inr(entry: RecordedEntry) -> Decimal | None:
    """The gross rupee value of a trade entry, read from its journal payload, or None.

    Tries the writers' known keys in turn: a deployment's `deployed_inr`, the canonical `value_inr`,
    then a park's `shares` x `price`. Returns None — not zero — when none is present, so a caller
    can count the trade as untraceable rather than silently understate turnover.
    """
    deployed = entry.payload.get(_DEPLOYED_KEY)
    if deployed is not None:
        return Decimal(deployed)
    value = entry.payload.get(_VALUE_KEY)
    if value is not None:
        return Decimal(value)
    shares = entry.payload.get(_SHARES_KEY)
    price = entry.payload.get(_PRICE_KEY)
    if shares is not None and price is not None:
        return Decimal(shares) * Decimal(price)
    return None


def _turnover_tax_section(entries: Sequence[RecordedEntry]) -> TurnoverTaxSection:
    """Per-sleeve turnover and realized tax events, both from journal trade entries."""
    buys: dict[Sleeve, Decimal] = defaultdict(lambda: _ZERO)
    sells: dict[Sleeve, Decimal] = defaultdict(lambda: _ZERO)
    counts: dict[Sleeve, int] = defaultdict(int)
    tax_events: list[TaxEvent] = []
    untraceable: list[Mapping[str, str]] = []

    for entry in entries:
        if entry.decision not in (Decision.BUY, Decision.SELL) or entry.sleeve is None:
            continue
        value = _trade_value_inr(entry)
        if value is None:
            untraceable.append(
                {
                    "trading_date": entry.trading_date.isoformat(),
                    "isin": entry.isin or "—",
                    "decision": entry.decision.value,
                    "sleeve": entry.sleeve.value,
                }
            )
            continue
        counts[entry.sleeve] += 1
        if entry.decision is Decision.BUY:
            buys[entry.sleeve] += value
        else:
            sells[entry.sleeve] += value
            tax_event = _tax_event(entry)
            if tax_event is not None:
                tax_events.append(tax_event)

    by_sleeve = tuple(
        SleeveTurnover(
            sleeve=sleeve,
            buy_value_inr=buys[sleeve],
            sell_value_inr=sells[sleeve],
            trade_count=counts[sleeve],
        )
        for sleeve in (Sleeve.CORE, Sleeve.TACTICAL, Sleeve.CASH)
        if counts[sleeve] > 0
    )
    return TurnoverTaxSection(
        by_sleeve=by_sleeve,
        tax_events=tuple(tax_events),
        untraceable_trades=tuple(untraceable),
    )


def _tax_event(entry: RecordedEntry) -> TaxEvent | None:
    """A SELL's realized tax outcome, when it carried one, or None.

    A sell records its realized gain, sell-side charges and holding-period class in the payload when
    the exit path computes them (the STCG/LTCG-relevant facts). A sell without them is turnover but
    not (yet) a reportable tax event, so it is left out of the tax list rather than invented.
    """
    gain = entry.payload.get(_REALIZED_GAIN_KEY)
    holding_period = entry.payload.get(_HOLDING_PERIOD_KEY)
    if gain is None or holding_period is None or entry.isin is None or entry.sleeve is None:
        return None
    return TaxEvent(
        trading_date=entry.trading_date,
        isin=entry.isin,
        sleeve=entry.sleeve,
        realized_gain_inr=Decimal(gain),
        charges_inr=Decimal(entry.payload.get(_CHARGES_KEY, "0")),
        holding_period=holding_period,
    )


def _cost_burn_section(entries: Sequence[RecordedEntry]) -> CostBurnSection:
    """Token and rupee burn per tier and in total, from the entries that carried a `TokenSpend`."""
    tokens_in: dict[Actor, int] = defaultdict(int)
    tokens_out: dict[Actor, int] = defaultdict(int)
    cost: dict[Actor, Decimal] = defaultdict(lambda: _ZERO)
    calls: dict[Actor, int] = defaultdict(int)

    for entry in entries:
        if entry.tokens is None:
            continue
        tokens_in[entry.actor] += entry.tokens.tokens_in
        tokens_out[entry.actor] += entry.tokens.tokens_out
        cost[entry.actor] += entry.tokens.cost_inr
        calls[entry.actor] += 1

    by_tier = tuple(
        TierCost(
            actor=actor,
            tokens_in=tokens_in[actor],
            tokens_out=tokens_out[actor],
            cost_inr=cost[actor],
            call_count=calls[actor],
        )
        for actor in sorted(calls, key=lambda a: a.value)
    )
    return CostBurnSection(
        by_tier=by_tier,
        total_cost_inr=sum(cost.values(), _ZERO),
        total_tokens_in=sum(tokens_in.values()),
        total_tokens_out=sum(tokens_out.values()),
    )


def _data_quality_section(entries: Sequence[RecordedEntry]) -> DataQualitySection:
    """Data-red and auth-required skips in the window (§5.7, invariant #10)."""
    skipped_dates: list[date] = []
    auth_skips = 0
    for entry in entries:
        if entry.decision is Decision.SKIPPED_DATA_RED:
            skipped_dates.append(entry.trading_date)
        elif entry.decision is Decision.AUTH_REQUIRED:
            auth_skips += 1
    return DataQualitySection(
        data_red_skips=len(skipped_dates),
        auth_required_skips=auth_skips,
        skipped_dates=tuple(skipped_dates),
    )


# ── rendering ──────────────────────────────────────────────────────────────────────────────────


def render_markdown(pack: EvidencePack) -> str:
    """Render the pack as the ten-minute human read (§5.7).

    The document skeleton comes from `templates/pack.md.tmpl`; the section bodies — which need loops
    over rows — are composed here and substituted in. The order puts the graduation-deciding numbers
    (return vs benchmarks, then the rail-breach verdict) first, and every figure a reader might act
    on is spelled with its units.
    """
    template = Template(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    return template.substitute(
        case_id=pack.case_id,
        trigger=pack.trigger.value,
        window_start=pack.window_start.isoformat(),
        window_end=pack.window_end.isoformat(),
        generated_at=pack.generated_at.isoformat(),
        entries_reviewed=pack.entries_reviewed,
        returns_body=_render_returns(pack.returns),
        drawdown_body=_render_drawdown(pack.drawdown),
        rail_body=_render_rails(pack.rail_breaches),
        decision_body=_render_decisions(pack.decision_review),
        turnover_body=_render_turnover(pack.turnover_tax),
        cost_body=_render_cost(pack.cost_burn),
        data_quality_body=_render_data_quality(pack.data_quality),
    )


def _pct(value: Decimal) -> str:
    """A rate held as a fraction (0.12) as a percentage string (12.00%)."""
    return f"{value * Decimal('100'):.2f}%"


def _pct_points(value: Decimal) -> str:
    """A value already in percentage units, formatted with two places and a sign."""
    return f"{value:+.2f} pp"


def _inr(value: Decimal) -> str:
    """A rupee amount, grouped, two places."""
    return f"₹{value:,.2f}"


def _render_returns(section: ReturnsSection) -> str:
    return (
        f"Since the first SIP on {section.first_sip_date.isoformat()}, struck on "
        f"{section.as_of.isoformat()}. {section.cashflow_count} instalment(s), "
        f"{_inr(section.total_invested_inr)} invested, terminal value "
        f"{_inr(section.terminal_value_inr)} (a market mark).\n\n"
        f"| Return (XIRR) | Rate |\n|---|---|\n"
        f"| Portfolio | {_pct(section.portfolio_xirr)} |\n"
        f"| NIFTY-TRI benchmark | {_pct(section.benchmark_xirr)} |\n"
        f"| Theme proxy | {_pct(section.theme_xirr)} |\n"
        f"| **Excess over benchmark** | **{_pct_points(section.excess_over_benchmark)}** |\n"
        f"| **Excess over theme** | **{_pct_points(section.excess_over_theme)}** |\n"
    )


def _render_drawdown(section: DrawdownSection) -> str:
    return (
        f"Worst peak-to-trough fall (on NAV marks): **{section.max_drawdown_pct:.2f}%**, from "
        f"{_inr(section.peak_value_inr)} on {section.peak_date.isoformat()} to "
        f"{_inr(section.trough_value_inr)} on {section.trough_date.isoformat()}.\n\n"
        f"Current drawdown from the running peak: {section.current_drawdown_pct:.2f}%."
    )


def _render_rails(section: RailBreachSection) -> str:
    header = (
        f"**Rail-breach count: {section.breach_count} — verdict {section.verdict}.**\n\n"
        "Rails are unbypassable (invariant #6): the target is **zero**, and a breach is a **bug** "
        "in the rails, not a risk event that was handled. "
    )
    if section.is_clean:
        body = (
            f"No breach reached the book. For context, A8 correctly refused "
            f"{section.rail_blocks} order(s) (RAIL_BLOCK) — rails doing their job, not breaches."
        )
    else:
        rows = "\n".join(
            f"- {b['trading_date']} {b['isin']}: {b['rails']} ({b['decision']})"
            for b in section.breaches
        )
        body = (
            f"**{section.breach_count} breach(es) reached the book — investigate before "
            f"graduation.** These are defects, not risk events:\n{rows}"
        )
    return header + body


def _render_decisions(section: DecisionReviewSection) -> str:
    if not section.reviews:
        return "No T1/T2 break-condition verdicts in the window."
    rows = "\n".join(
        f"| {r.trading_date.isoformat()} | {r.actor.value} | {r.isin or '—'} | "
        f"{r.break_condition_id} | {r.verdict} | {r.subsequent_action}"
        f"{'' if r.action_date is None else ' ' + r.action_date.isoformat()} |"
        for r in section.reviews
    )
    headline = (
        f"**{section.broken_unactioned} BROKEN verdict(s) with no subsequent trade** — a thesis "
        "called broken but not acted on is the pairing to scrutinise.\n\n"
        if section.broken_unactioned
        else "Every BROKEN verdict was followed by a trade on the instrument.\n\n"
    )
    return (
        headline
        + "| Date | Tier | ISIN | Break cond. | Verdict | Outcome |\n"
        + "|---|---|---|---|---|---|\n"
        + rows
    )


def _render_turnover(section: TurnoverTaxSection) -> str:
    if section.by_sleeve:
        sleeve_rows = "\n".join(
            f"| {s.sleeve.value} | {_inr(s.buy_value_inr)} | {_inr(s.sell_value_inr)} | "
            f"{_inr(s.turnover_inr)} | {s.trade_count} |"
            for s in section.by_sleeve
        )
        turnover_table = (
            "| Sleeve | Bought | Sold | Turnover | Trades |\n|---|---|---|---|---|\n"
            + sleeve_rows
            + f"\n\n**Total turnover: {_inr(section.total_turnover_inr)}.**"
        )
    else:
        turnover_table = "No trades in the window."

    if section.tax_events:
        tax_rows = "\n".join(
            f"| {e.trading_date.isoformat()} | {e.isin} | {e.sleeve.value} | "
            f"{_inr(e.realized_gain_inr)} | {_inr(e.charges_inr)} | {e.holding_period} |"
            for e in section.tax_events
        )
        tax_table = (
            "\n\n**Tax events**\n\n"
            "| Date | ISIN | Sleeve | Realized gain | Charges | Period |\n"
            "|---|---|---|---|---|---|\n" + tax_rows
        )
    else:
        tax_table = "\n\nNo realized tax events in the window (no sells booked a gain)."

    warning = (
        ""
        if not section.untraceable_trades
        else (
            f"\n\n**⚠ {len(section.untraceable_trades)} trade(s) had no rupee value in the "
            "journal — a reconstructability defect to fix (the pack cannot be built from the "
            "journal alone while these exist).**"
        )
    )
    return turnover_table + tax_table + warning


def _render_cost(section: CostBurnSection) -> str:
    header = (
        f"Total burn: **{_inr(section.total_cost_inr)}** "
        f"({section.total_tokens_in:,} in / {section.total_tokens_out:,} out tokens)."
    )
    if not section.by_tier:
        return header + "\n\nNo model calls were billed in the window (T0 is mechanical)."
    rows = "\n".join(
        f"| {t.actor.value} | {t.call_count} | {t.tokens_in:,} | {t.tokens_out:,} | "
        f"{_inr(t.cost_inr)} |"
        for t in section.by_tier
    )
    return (
        header
        + "\n\n| Tier | Calls | Tokens in | Tokens out | Cost |\n|---|---|---|---|---|\n"
        + rows
    )


def _render_data_quality(section: DataQualitySection) -> str:
    if section.data_red_skips == 0 and section.auth_required_skips == 0:
        return "No sessions were skipped: data was green and broker auth held every day."
    dates = ", ".join(d.isoformat() for d in section.skipped_dates) or "—"
    return (
        f"**Data-red skips: {section.data_red_skips}** (no trading on those sessions, "
        f"invariant #10): {dates}.\n\n"
        f"Auth-required skips: {section.auth_required_skips} (broker session lapsed)."
    )
