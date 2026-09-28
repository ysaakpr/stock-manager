"""X2: after-tax returns — FIFO tax lots, Indian capital-gains tax per FY, after-tax XIRR.

Every return this repo reports is pre-tax. For a resident individual the gap is large (STCG on a
monthly-rebalanced momentum book is taxed at 15-20%), so a pre-tax XIRR overstates what the
investor keeps by several points a year. This module measures that gap. It is a **post-processor**:
it reads a run's fill ledger after the fact and never touches the book or the walk that produced
it. ``PortfolioBook`` keeps one blended basis per ISIN, which is right for P&L and wrong for tax —
the Act taxes each *lot* by its own acquisition date — so the lots are reconstructed here, FIFO,
from the fills themselves.

**What is computed, per financial year (April-March):**

- realised STCG/LTCG per lot, classified by holding period on the dated threshold (held for more
  than 12 months => long-term, Sec 2(42A)), with dates as trade dates (CBDT Circular 704);
- the cost of acquisition per Sec 48 — purchase turnover plus the deductible charges, *excluding*
  STT, which Sec 48 forbids deducting — and, for lots acquired on or before 31-01-2018 and sold
  long-term under Sec 112A, the Sec 55(2)(ac) grandfathered cost;
- loss set-off within the head (Sec 70: STCL against STCG or LTCG, LTCL against LTCG only) then
  brought-forward losses (Sec 74, eight years, same restriction), oldest first;
- the Sec 112A exemption on the FY's net LTCG, then tax at the dated rate of each gain's
  transfer date, surcharge (an explicit investor parameter) and cess (dated, from the schedule);
- dividend tax for the regime in force on the credit date (DDT-exempt / Sec 115BBDA / slab).

**Taxpayer-favourable orderings (stated, not hidden).** Sec 70 and Sec 74 prescribe *which* gains
a loss may absorb but not the order among eligible gains, and Sec 112A(1) as amended in 2024 does
not say whether the FY 2024-25 exemption comes off the 10% or the 12.5% slice. Where the Act is
silent this module picks the order that minimises tax — highest-rate gains absorb losses and the
exemption first, brought-forward losses are used oldest-first so fewer expire — and the report
says so.

**Tax as a cashflow.** Tax for a financial year is modelled as an investor outflow on the date
``PaymentTiming`` names (default: 31 March, the end of the FY in which the gain arose — no later
than advance tax would have required, since Sec 234C waives interest on capital-gains shortfalls
paid in the remaining instalments). It is paid from outside the book, so the strategy's walk and
its terminal NAV are untouched and the pre-tax and after-tax XIRRs differ *only* by the tax flows.

**What it never does:** read a clock, guess a rate for a date the schedule does not cover (it
raises ``TaxScheduleCoverageError``), take a float, or default an investor assumption silently —
``InvestorProfile`` has no defaults.

**Dividends: the interface for the parallel dividend task.** Until a run credits dividends to cash
there is nothing to tax, and the report says "0 dividends credited" rather than "dividends are
untaxed". When dividends land, feed each credit in as a ``DividendCredit(isin, received,
amount)`` — the *gross* amount credited, dated the day it was received. The ledger adapter in
``backtest.tax_report`` maps a ledger row whose description starts ``DIVIDEND`` to exactly that.
"""

from __future__ import annotations

import calendar
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

import structlog
import yaml

from backtest.xirr import Cashflow, xirr
from execution.broker import Side

__all__ = [
    "SCHEDULE_PATH",
    "AfterTaxResult",
    "BonusEvent",
    "CorporateEvent",
    "DividendCredit",
    "FyTax",
    "GrandfatheringPrices",
    "InvestorProfile",
    "LotMatchError",
    "MappingGrandfatheringPrices",
    "MissingGrandfatheringPriceError",
    "OpenLot",
    "PaymentTiming",
    "Realisation",
    "ReissueEvent",
    "RunLedger",
    "SplitEvent",
    "TaxError",
    "TaxSchedule",
    "TaxScheduleCoverageError",
    "TaxScheduleError",
    "TaxTrade",
    "Term",
    "compute_after_tax",
    "financial_year",
    "fy_label",
    "is_long_term",
    "load_tax_schedule",
    "match_lots",
]

_log = structlog.get_logger(__name__)

#: The dated schedule this module reads. Checked in; every boundary carries a citation.
SCHEDULE_PATH = Path(__file__).with_name("tax_schedule.yaml")

_ZERO = Decimal("0")
_PAISA = Decimal("0.01")


# ── errors ─────────────────────────────────────────────────────────────────────────────────────


class TaxError(Exception):
    """Base for every refusal the tax layer makes. It fails loud (CLAUDE.md), never silently."""


class TaxScheduleError(TaxError):
    """The schedule file is malformed — a float, a missing field, or entries out of order."""


class TaxScheduleCoverageError(TaxError):
    """A date falls outside the schedule's stated coverage.

    Raised rather than borrowing the nearest era's rate: a 2003 transfer taxed at 2004's rate, or a
    2028 one at 2026's, is a made-up number that would read as a measured one.
    """


class LotMatchError(TaxError):
    """The fills do not form a consistent lot history — a sell of shares no lot holds."""


class MissingGrandfatheringPriceError(TaxError):
    """A lot needs its 31-01-2018 fair market value (Sec 55(2)(ac)) and none is available."""


# ── schedule ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Dated:
    """One schedule entry: the value in force from ``effective_from``, and why."""

    effective_from: date
    values: Mapping[str, str]
    citation: str
    provenance: str
    source_url: str | None


@dataclass(frozen=True, slots=True)
class TaxSchedule:
    """The dated rate schedule, validated. Lookups raise outside its stated coverage."""

    coverage_from: date
    coverage_through: date
    long_term_after_months: tuple[_Dated, ...]
    stcg_rates: tuple[_Dated, ...]
    ltcg_regimes: tuple[_Dated, ...]
    ltcg_exemption_by_fy: tuple[_Dated, ...]
    cess_by_fy: tuple[_Dated, ...]
    dividend_regimes: tuple[_Dated, ...]
    grandfather_acquired_on_or_before: date
    grandfather_fmv_date: date
    grandfather_citation: str
    loss_carry_forward_years: int
    loss_citation: str

    def _covered(self, when: date, what: str) -> None:
        if not (self.coverage_from <= when <= self.coverage_through):
            raise TaxScheduleCoverageError(
                f"{what} on {when.isoformat()} is outside the tax schedule's coverage "
                f"[{self.coverage_from.isoformat()}, {self.coverage_through.isoformat()}]"
            )

    @staticmethod
    def _in_force(entries: Sequence[_Dated], when: date, what: str) -> _Dated:
        chosen: _Dated | None = None
        for entry in entries:
            if entry.effective_from <= when:
                chosen = entry
            else:
                break
        if chosen is None:
            raise TaxScheduleCoverageError(
                f"no {what} entry in force on {when.isoformat()} (first entry "
                f"{entries[0].effective_from.isoformat()})"
            )
        return chosen

    def long_term_months(self, transfer: date) -> int:
        """Months a lot must be held *beyond* to be long-term, for a transfer on ``transfer``."""
        self._covered(transfer, "holding-period threshold")
        return int(
            self._in_force(self.long_term_after_months, transfer, "holding").values["months"]
        )

    def stcg_rate(self, transfer: date) -> Decimal:
        """Sec 111A rate for an STT-paid short-term transfer on ``transfer``."""
        self._covered(transfer, "STCG rate")
        return Decimal(self._in_force(self.stcg_rates, transfer, "STCG").values["rate"])

    def ltcg_regime(self, transfer: date) -> tuple[bool, Decimal]:
        """``(taxable, rate)`` for an STT-paid long-term transfer on ``transfer``."""
        self._covered(transfer, "LTCG regime")
        entry = self._in_force(self.ltcg_regimes, transfer, "LTCG")
        return entry.values["taxable"] == "true", Decimal(entry.values["rate"])

    def ltcg_exemption(self, fy: int) -> Decimal:
        """The Sec 112A exemption on the aggregate LTCG of financial year ``fy``."""
        start = date(fy, 4, 1)
        self._covered(max(start, self.coverage_from), "LTCG exemption")
        return Decimal(
            self._in_force(self.ltcg_exemption_by_fy, start, "exemption").values["amount"]
        )

    def cess_rate(self, fy: int) -> Decimal:
        """Cess on (tax + surcharge) for financial year ``fy``."""
        start = date(fy, 4, 1)
        self._covered(max(start, self.coverage_from), "cess")
        return Decimal(self._in_force(self.cess_by_fy, start, "cess").values["rate"])

    def dividend_regime(self, received: date) -> _Dated:
        """The dividend regime in force for a dividend received on ``received``."""
        self._covered(received, "dividend regime")
        return self._in_force(self.dividend_regimes, received, "dividend")

    def provenance_rows(self) -> tuple[tuple[str, str, str, str, str], ...]:
        """``(item, from, value, provenance, citation)`` per boundary — the report's table."""
        rows: list[tuple[str, str, str, str, str]] = []

        def add(item: str, entries: Sequence[_Dated], key: str) -> None:
            for e in entries:
                cite = e.citation + (f" <{e.source_url}>" if e.source_url else "")
                rows.append((item, e.effective_from.isoformat(), e.values[key], e.provenance, cite))

        add("Long-term after (months)", self.long_term_after_months, "months")
        add("STCG rate (Sec 111A)", self.stcg_rates, "rate")
        for e in self.ltcg_regimes:
            value = e.values["rate"] if e.values["taxable"] == "true" else "exempt"
            cite = e.citation + (f" <{e.source_url}>" if e.source_url else "")
            rows.append(
                ("LTCG (10(38)/112A)", e.effective_from.isoformat(), value, e.provenance, cite)
            )
        add("LTCG exemption per FY", self.ltcg_exemption_by_fy, "amount")
        add("Cess", self.cess_by_fy, "rate")
        for e in self.dividend_regimes:
            kind = e.values["kind"]
            if kind == "ddt_exempt_115bbda":
                kind = f"115BBDA {e.values['rate']} above {e.values['threshold']}"
            cite = e.citation + (f" <{e.source_url}>" if e.source_url else "")
            rows.append(("Dividends", e.effective_from.isoformat(), kind, e.provenance, cite))
        return tuple(rows)


def _parse_date(raw: object, where: str) -> date:
    if not isinstance(raw, str):
        raise TaxScheduleError(f"{where}: dates must be quoted ISO strings, got {raw!r}")
    return date.fromisoformat(raw)


def _parse_entries(raw: object, section: str, keys: Sequence[str]) -> tuple[_Dated, ...]:
    if not isinstance(raw, list) or not raw:
        raise TaxScheduleError(f"{section}: expected a non-empty list")
    out: list[_Dated] = []
    for i, item in enumerate(raw):
        where = f"{section}[{i}]"
        if not isinstance(item, dict):
            raise TaxScheduleError(f"{where}: expected a mapping")
        values: dict[str, str] = {}
        for key in keys:
            value = item.get(key)
            if not isinstance(value, str):
                # A bare YAML number arrives as float/int: refuse it (money is never float).
                raise TaxScheduleError(f"{where}.{key} must be a quoted string, got {value!r}")
            values[key] = value
        citation = item.get("citation")
        provenance = item.get("provenance")
        if not isinstance(citation, str) or not citation.strip():
            raise TaxScheduleError(f"{where}: every boundary needs a citation")
        if provenance not in {"primary", "secondary", "statute"}:
            raise TaxScheduleError(f"{where}: provenance must be primary/secondary/statute")
        source_url = item.get("source_url")
        out.append(
            _Dated(
                effective_from=_parse_date(item.get("effective_from"), where),
                values=values,
                citation=" ".join(citation.split()),
                provenance=provenance,
                source_url=source_url if isinstance(source_url, str) else None,
            )
        )
    for earlier, later in pairwise(out):
        if later.effective_from <= earlier.effective_from:
            raise TaxScheduleError(f"{section}: entries must be in strictly ascending date order")
    return tuple(out)


def load_tax_schedule(path: Path = SCHEDULE_PATH) -> TaxSchedule:
    """Parse and validate the dated tax schedule.

    Assumes the file is checked in and trusted. Raises ``TaxScheduleError`` on any float, missing
    citation, or out-of-order entry — a schedule that cannot say where a rate came from is not one
    this module will compute with.
    """
    with path.open(encoding="utf-8") as handle:
        raw: Any = yaml.safe_load(handle)
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise TaxScheduleError("tax schedule: expected a mapping with version: 1")
    coverage = raw["coverage"]
    gf = raw["grandfathering"]
    lcf = raw["loss_carry_forward"]
    if not isinstance(lcf.get("years"), str):
        raise TaxScheduleError("loss_carry_forward.years must be a quoted string")
    return TaxSchedule(
        coverage_from=_parse_date(coverage["from"], "coverage.from"),
        coverage_through=_parse_date(coverage["through"], "coverage.through"),
        long_term_after_months=_parse_entries(
            raw["long_term_after_months"], "long_term_after_months", ["months"]
        ),
        stcg_rates=_parse_entries(raw["stcg_rates"], "stcg_rates", ["rate"]),
        ltcg_regimes=_parse_entries(raw["ltcg_regimes"], "ltcg_regimes", ["taxable", "rate"]),
        ltcg_exemption_by_fy=_parse_entries(
            raw["ltcg_exemption_by_fy"], "ltcg_exemption_by_fy", ["amount"]
        ),
        cess_by_fy=_parse_entries(raw["cess_by_fy"], "cess_by_fy", ["rate"]),
        dividend_regimes=_parse_entries(
            raw["dividend_regimes"], "dividend_regimes", ["kind", "threshold", "rate"]
        ),
        grandfather_acquired_on_or_before=_parse_date(
            gf["acquired_on_or_before"], "grandfathering.acquired_on_or_before"
        ),
        grandfather_fmv_date=_parse_date(gf["fmv_date"], "grandfathering.fmv_date"),
        grandfather_citation=" ".join(str(gf["citation"]).split()),
        loss_carry_forward_years=int(lcf["years"]),
        loss_citation=" ".join(str(lcf["citation"]).split()),
    )


# ── calendar helpers ───────────────────────────────────────────────────────────────────────────


def financial_year(when: date) -> int:
    """The Indian financial year containing ``when``, named by its starting calendar year."""
    return when.year if when.month >= 4 else when.year - 1


def fy_label(fy: int) -> str:
    """``2019`` -> ``"FY2019-20"``."""
    return f"FY{fy}-{(fy + 1) % 100:02d}"


def _add_months(start: date, months: int) -> date:
    """``start`` plus ``months`` calendar months, the day clamped to the target month's length."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def is_long_term(acquired: date, transferred: date, schedule: TaxSchedule) -> bool:
    """True iff a lot acquired on ``acquired`` and transferred on ``transferred`` is long-term.

    Sec 2(42A): short-term means held for *not more than* the threshold, so a lot sold on the
    one-year anniversary of its purchase is still short-term and only a sale after it is long-term.
    """
    months = schedule.long_term_months(transferred)
    return transferred > _add_months(acquired, months)


# ── inputs ─────────────────────────────────────────────────────────────────────────────────────


def _require_decimal(name: str, value: object) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal — money is never float (CLAUDE.md)")


@dataclass(frozen=True, slots=True)
class TaxTrade:
    """One delivery fill, as the tax computation needs it.

    ``net_amount`` is the cash the fill moved, always positive: turnover *plus* every charge on a
    buy, turnover *minus* every charge on a sell (``CostBreakdown.net_amount``). ``stt`` is the STT
    inside that figure, which Sec 48 does not allow as a deduction; it is backed out of the cost or
    added back to the proceeds. ``stt_known`` is False when the source could not separate STT (a
    ledger row carries only the net cash) — the report then says STT was treated as deductible,
    which understates tax by the STT's share.
    """

    isin: str
    trade_date: date
    side: Side
    quantity: int
    net_amount: Decimal
    stt: Decimal
    stt_known: bool

    def __post_init__(self) -> None:
        _require_decimal("net_amount", self.net_amount)
        _require_decimal("stt", self.stt)
        if self.quantity <= 0:
            raise ValueError(f"trade quantity must be positive, got {self.quantity}")
        if self.net_amount <= _ZERO or self.stt < _ZERO:
            raise ValueError("trade net amount must be positive and STT non-negative")

    @property
    def tax_amount(self) -> Decimal:
        """Cost of acquisition (buy) or net sale consideration (sell) for Sec 48, STT excluded."""
        return self.net_amount - self.stt if self.side is Side.BUY else self.net_amount + self.stt


@dataclass(frozen=True, slots=True)
class DividendCredit:
    """A gross dividend credited to the account on ``received`` — the dividend task's output."""

    isin: str
    received: date
    amount: Decimal

    def __post_init__(self) -> None:
        _require_decimal("amount", self.amount)
        if self.amount <= _ZERO:
            raise ValueError("dividend amount must be positive")


@dataclass(frozen=True, slots=True)
class SplitEvent:
    """A split/consolidation: every open lot's count scales by ``numerator/denominator``.

    Acquisition date and cost carry over unchanged (the holding is the same asset re-denominated).

    ``resulting_quantity`` is set when the book floored a fractional entitlement and forfeited the
    fraction (``backtest.book_actions``): the holding after the split is exactly that many shares.
    Each lot is then scaled and floored on its own and the whole shares the per-lot floors leave
    over go to the oldest lot, so the lots sum to what the account holds. Which lot carries the
    odd share moves at most one share's holding period; ``None`` refuses a fractional lot instead.
    """

    isin: str
    ex_date: date
    numerator: int
    denominator: int
    resulting_quantity: int | None = None


@dataclass(frozen=True, slots=True)
class BonusEvent:
    """A bonus ``new:held``: a new lot of ``held_qty * new / held`` shares at nil cost.

    Sec 55(2)(aa)(iiia): the cost of bonus shares is nil, and the holding period runs from the
    allotment date (taken as the ex-date). The original lots are untouched. ``resulting_quantity``
    as for :class:`SplitEvent`: the whole holding after the allotment, the fraction forfeited.
    """

    isin: str
    ex_date: date
    new_shares: int
    held_shares: int
    resulting_quantity: int | None = None


@dataclass(frozen=True, slots=True)
class ReissueEvent:
    """An ISIN reissue: every lot of ``from_isin`` continues as ``isin`` from ``ex_date``, 1:1.

    A face-value split on NSE usually retires the ISIN, and the holder's shares continue under the
    successor (the carry in ``backtest.book_actions``). Not a transfer (no gain arises): each lot
    keeps its acquisition date and cost. Its ``OpenLot.isin`` — the ISIN a Sec 55(2)(ac) FMV is
    looked up on — becomes the survivor's only if the reissue is on or before the FMV date: the
    FMV is the bar of whichever ISIN actually traded on 31-01-2018.
    """

    isin: str
    ex_date: date
    from_isin: str


CorporateEvent = SplitEvent | BonusEvent | ReissueEvent


@dataclass(frozen=True, slots=True)
class RunLedger:
    """Everything the post-processor reads from a finished run.

    ``trades`` in the order the broker posted them; ``external_flows`` in XIRR sign convention
    (deposits negative, withdrawals positive); ``terminal_nav`` the run's pre-tax closing value on
    ``terminal_date``; ``terminal_prices`` the mark for every ISIN still held (used only for the
    deemed-liquidation variant). ``source`` says where the ledger came from, for the report.
    """

    source: str
    trades: tuple[TaxTrade, ...]
    external_flows: tuple[Cashflow, ...]
    terminal_date: date
    terminal_nav: Decimal
    terminal_prices: Mapping[str, Decimal]
    dividends: tuple[DividendCredit, ...] = ()
    corporate_events: tuple[CorporateEvent, ...] = ()


class PaymentTiming(StrEnum):
    """When a financial year's tax leaves the investor's pocket, as an XIRR outflow."""

    FY_END = "fy_end"
    """31 March of the FY the income arose in — the last advance-tax deadline, rounded to FY end."""

    SELF_ASSESSMENT = "self_assessment"
    """31 July after the FY — the non-audit return due date; the latest tax can be paid without
    Sec 234B/234C interest being material for capital gains. The generous bound."""

    def payment_date(self, fy: int) -> date:
        """The date the tax for financial year ``fy`` is paid under this timing."""
        if self is PaymentTiming.FY_END:
            return date(fy + 1, 3, 31)
        return date(fy + 1, 7, 31)


@dataclass(frozen=True, slots=True)
class InvestorProfile:
    """Who is paying the tax. Every field is explicit — there are no defaults to hide behind.

    ``slab_rate`` is the marginal slab rate dividends are taxed at from FY 2020-21 (before
    surcharge and cess). ``cg_surcharge_rate`` and ``dividend_surcharge_rate`` are the surcharge
    percentages that apply to the investor's total income band (for 111A/112A gains and dividends
    the statutory surcharge is capped at 15% from FY 2022-23; the caller states the rate). The
    investor is assumed to have exhausted the basic exemption limit on other income, so no part of
    a special-rate gain falls in the nil slab, and no Sec 87A rebate applies.
    ``cess_override`` replaces the schedule's dated cess when not None.
    """

    residency: str
    slab_rate: Decimal
    cg_surcharge_rate: Decimal
    dividend_surcharge_rate: Decimal
    payment_timing: PaymentTiming
    cess_override: Decimal | None = field(default=None)

    def __post_init__(self) -> None:
        if self.residency != "resident_individual":
            raise ValueError(
                f"only a resident individual is modelled, got {self.residency!r}; HUFs, "
                "non-residents and companies have different rates, thresholds and 115BBDA rules"
            )
        for name in ("slab_rate", "cg_surcharge_rate", "dividend_surcharge_rate"):
            value = getattr(self, name)
            _require_decimal(name, value)
            if not (_ZERO <= value <= Decimal("1")):
                raise ValueError(f"{name} must be a ratio in [0, 1], got {value}")
        if self.cess_override is not None:
            _require_decimal("cess_override", self.cess_override)


class GrandfatheringPrices(Protocol):
    """The Sec 55(2)(ac) fair market value per share on the grandfathering date.

    Per share *in the units the share had on that date* (a later split is applied to the lot,
    not to this price). Raises ``MissingGrandfatheringPriceError`` when it cannot answer.
    """

    def fmv_per_share(self, isin: str) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class MappingGrandfatheringPrices:
    """Grandfathering FMVs from an explicit ISIN -> price map (tests, or a pre-read table)."""

    prices: Mapping[str, Decimal]

    def fmv_per_share(self, isin: str) -> Decimal:
        price = self.prices.get(isin)
        if price is None:
            raise MissingGrandfatheringPriceError(f"no 31-01-2018 FMV for {isin}")
        _require_decimal("fmv", price)
        return price


# ── lots and realisations ──────────────────────────────────────────────────────────────────────


class Term(StrEnum):
    """Short-term or long-term, per Sec 2(42A)/2(42B)."""

    SHORT = "STCG"
    LONG = "LTCG"


@dataclass(slots=True)
class OpenLot:
    """One FIFO lot still held: when it was bought, how many shares, and its Sec 48 cost.

    ``gf_units`` is the number of shares *as of the grandfathering date* the lot represents per
    current share's worth — i.e. ``quantity / gf_units`` converts back to 31-01-2018 units — so a
    split after that date does not multiply the grandfathered FMV.
    """

    isin: str
    acquired: date
    quantity: int
    cost: Decimal
    gf_units: Decimal = Decimal("1")


@dataclass(frozen=True, slots=True)
class Realisation:
    """One lot (or slice of one) transferred, and the gain it realised."""

    isin: str
    acquired: date
    transferred: date
    quantity: int
    proceeds: Decimal
    actual_cost: Decimal
    cost: Decimal
    term: Term
    taxable: bool
    rate: Decimal
    grandfathered: bool
    deemed: bool

    @property
    def gain(self) -> Decimal:
        """Proceeds minus the (possibly grandfathered) cost; negative is a loss."""
        return self.proceeds - self.cost

    @property
    def fy(self) -> int:
        return financial_year(self.transferred)


def _realise(
    lot: OpenLot,
    quantity: int,
    proceeds: Decimal,
    transferred: date,
    schedule: TaxSchedule,
    fmv: GrandfatheringPrices,
    *,
    deemed: bool,
) -> Realisation:
    actual_cost = lot.cost * quantity / lot.quantity
    long_term = is_long_term(lot.acquired, transferred, schedule)
    if long_term:
        taxable, rate = schedule.ltcg_regime(transferred)
    else:
        taxable, rate = True, schedule.stcg_rate(transferred)
    cost = actual_cost
    grandfathered = False
    if long_term and taxable and lot.acquired <= schedule.grandfather_acquired_on_or_before:
        # Sec 55(2)(ac): higher of actual cost and lower of (FMV on 31-01-2018, consideration).
        fmv_total = fmv.fmv_per_share(lot.isin) * Decimal(quantity) / lot.gf_units
        stepped = max(actual_cost, min(fmv_total, proceeds))
        grandfathered = stepped != actual_cost
        cost = stepped
    return Realisation(
        isin=lot.isin,
        acquired=lot.acquired,
        transferred=transferred,
        quantity=quantity,
        proceeds=proceeds,
        actual_cost=actual_cost,
        cost=cost,
        term=Term.LONG if long_term else Term.SHORT,
        taxable=taxable,
        rate=rate,
        grandfathered=grandfathered,
        deemed=deemed,
    )


def _sell_fifo(
    lots: deque[OpenLot],
    isin: str,
    quantity: int,
    proceeds: Decimal,
    transferred: date,
    schedule: TaxSchedule,
    fmv: GrandfatheringPrices,
    *,
    deemed: bool,
) -> list[Realisation]:
    held = sum(lot.quantity for lot in lots)
    if held < quantity:
        raise LotMatchError(
            f"sell of {quantity} {isin} on {transferred.isoformat()} but lots hold only {held} "
            "(a missing buy, or a corporate action the ledger did not carry)"
        )
    out: list[Realisation] = []
    remaining = quantity
    allocated = _ZERO
    while remaining:
        lot = lots[0]
        take = min(lot.quantity, remaining)
        remaining -= take
        # Proceeds split pro rata by shares; the last slice takes the remainder so the slices
        # sum to the sale consideration exactly.
        slice_proceeds = proceeds - allocated if remaining == 0 else proceeds * take / quantity
        allocated += slice_proceeds
        out.append(_realise(lot, take, slice_proceeds, transferred, schedule, fmv, deemed=deemed))
        if take == lot.quantity:
            lots.popleft()
        else:
            lot.cost -= lot.cost * take / lot.quantity
            lot.quantity -= take
    return out


def _split_lots(lots: deque[OpenLot], event: SplitEvent, schedule: TaxSchedule) -> None:
    """Scale every open lot by the split ratio in place (see :class:`SplitEvent` on fractions)."""
    ratio = Decimal(event.numerator) / event.denominator
    floored = event.resulting_quantity is not None
    for lot in lots:
        scaled = Decimal(lot.quantity * event.numerator) / event.denominator
        if not floored and scaled != scaled.to_integral_value():
            raise LotMatchError(f"split on {event.isin} leaves a fractional lot ({scaled})")
        lot.quantity = int(scaled)  # int() truncates toward zero: the floor, for a positive count
        if event.ex_date > schedule.grandfather_fmv_date:
            lot.gf_units *= ratio
    if event.resulting_quantity is not None and lots:
        leftover = event.resulting_quantity - sum(lot.quantity for lot in lots)
        if not 0 <= leftover < len(lots):
            raise LotMatchError(
                f"split on {event.isin} leaves {event.resulting_quantity} shares, which the "
                f"floored lots ({sum(lot.quantity for lot in lots)}) cannot reach"
            )
        lots[0].quantity += leftover
    empty = [lot for lot in lots if lot.quantity == 0]
    for lot in empty:
        lots.remove(lot)
    if lots:
        # A lot floored to nothing keeps its cost in the holding, as the book's basis does.
        lots[0].cost += sum((lot.cost for lot in empty), _ZERO)


def match_lots(
    ledger: RunLedger, schedule: TaxSchedule, fmv: GrandfatheringPrices
) -> tuple[tuple[Realisation, ...], dict[str, deque[OpenLot]]]:
    """Reconstruct FIFO lots from the ledger's fills and realise every sell against them.

    Corporate events on a date are applied before that date's trades (a trade on the ex-date is
    already in post-event units). Returns the realisations in transfer order and the lots still
    open. Raises ``LotMatchError`` on a sell no lot can cover — never short-sells a lot.
    """
    lots: dict[str, deque[OpenLot]] = defaultdict(deque)
    timeline: list[tuple[date, int, int, CorporateEvent | TaxTrade]] = []
    for i, event in enumerate(ledger.corporate_events):
        timeline.append((event.ex_date, 0, i, event))
    for i, trade in enumerate(ledger.trades):
        timeline.append((trade.trade_date, 1, i, trade))
    timeline.sort(key=lambda item: (item[0], item[1], item[2]))

    realisations: list[Realisation] = []
    for _, _, _, item in timeline:
        if isinstance(item, ReissueEvent):
            carried = lots.pop(item.from_isin, deque())
            if item.ex_date <= schedule.grandfather_fmv_date:
                # The survivor is what traded on the FMV date, so its bar is the Sec 55(2)(ac) FMV.
                for lot in carried:
                    lot.isin = item.isin
            if carried:
                merged = sorted([*lots[item.isin], *carried], key=lambda lot: lot.acquired)
                lots[item.isin] = deque(merged)
        elif isinstance(item, SplitEvent):
            _split_lots(lots[item.isin], item, schedule)
        elif isinstance(item, BonusEvent):
            held = sum(lot.quantity for lot in lots[item.isin])
            if item.resulting_quantity is not None:
                if item.resulting_quantity < held:
                    raise LotMatchError(
                        f"bonus on {item.isin} leaves {item.resulting_quantity} shares, fewer than "
                        f"the {held} the lots hold"
                    )
                new = Decimal(item.resulting_quantity - held)
            else:
                new = Decimal(held * item.new_shares) / item.held_shares
                if new != new.to_integral_value():
                    raise LotMatchError(
                        f"bonus on {item.isin} gives a fractional allotment ({new})"
                    )
            if new:
                lots[item.isin].append(OpenLot(item.isin, item.ex_date, int(new), _ZERO))
        elif item.side is Side.BUY:
            lots[item.isin].append(
                OpenLot(item.isin, item.trade_date, item.quantity, item.tax_amount)
            )
        else:
            realisations.extend(
                _sell_fifo(
                    lots[item.isin],
                    item.isin,
                    item.quantity,
                    item.tax_amount,
                    item.trade_date,
                    schedule,
                    fmv,
                    deemed=False,
                )
            )
    return tuple(realisations), {isin: q for isin, q in lots.items() if q}


# ── per-FY tax ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FyTax:
    """One financial year's capital-gains and dividend tax, with every set-off step visible."""

    fy: int
    stcg_gross: Mapping[Decimal, Decimal]
    ltcg_gross: Mapping[Decimal, Decimal]
    st_loss: Decimal
    lt_loss: Decimal
    exempt_ltcg_net: Decimal
    brought_forward_used: Decimal
    exemption_used: Decimal
    stcg_taxable: Mapping[Decimal, Decimal]
    ltcg_taxable: Mapping[Decimal, Decimal]
    carried_forward_st: Decimal
    carried_forward_lt: Decimal
    expired: Decimal
    dividends: Decimal
    dividend_taxable: Decimal
    cg_tax: Decimal
    dividend_tax: Decimal
    surcharge: Decimal
    cess: Decimal
    payment_date: date

    @property
    def total(self) -> Decimal:
        """Everything payable for the year: tax, surcharge and cess."""
        return self.cg_tax + self.dividend_tax + self.surcharge + self.cess


@dataclass(slots=True)
class _CarriedLoss:
    origin_fy: int
    term: Term
    amount: Decimal


def _absorb(buckets: dict[Decimal, Decimal], loss: Decimal) -> Decimal:
    """Set ``loss`` off against ``buckets`` highest rate first; return the unabsorbed remainder."""
    for rate in sorted(buckets, reverse=True):
        if loss <= _ZERO:
            break
        used = min(buckets[rate], loss)
        buckets[rate] -= used
        loss -= used
    return loss


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_PAISA, rounding=ROUND_HALF_UP)


def _fy_taxes(
    realisations: Iterable[Realisation],
    dividends: Iterable[DividendCredit],
    schedule: TaxSchedule,
    profile: InvestorProfile,
) -> tuple[FyTax, ...]:
    by_fy: dict[int, list[Realisation]] = defaultdict(list)
    for r in realisations:
        by_fy[r.fy].append(r)
    div_by_fy: dict[int, list[DividendCredit]] = defaultdict(list)
    for d in dividends:
        div_by_fy[financial_year(d.received)].append(d)
    if not by_fy and not div_by_fy:
        return ()

    carried: list[_CarriedLoss] = []
    out: list[FyTax] = []
    first = min([*by_fy, *div_by_fy])
    last = max([*by_fy, *div_by_fy])
    for fy in range(first, last + 1):
        # Losses older than the carry-forward window lapse before this year's set-off (Sec 74(1)).
        expired = sum(
            (c.amount for c in carried if fy - c.origin_fy > schedule.loss_carry_forward_years),
            _ZERO,
        )
        carried = [c for c in carried if fy - c.origin_fy <= schedule.loss_carry_forward_years]

        st: dict[Decimal, Decimal] = defaultdict(lambda: _ZERO)
        lt: dict[Decimal, Decimal] = defaultdict(lambda: _ZERO)
        st_loss = lt_loss = exempt_net = _ZERO
        for r in by_fy.get(fy, []):
            gain = r.gain
            if not r.taxable:
                # Sec 10(38): the gain is exempt, and a loss from an exempt source is disregarded.
                exempt_net += gain
            elif gain >= _ZERO:
                (st if r.term is Term.SHORT else lt)[r.rate] += gain
            elif r.term is Term.SHORT:
                st_loss -= gain
            else:
                lt_loss -= gain
        stcg_gross = dict(st)
        ltcg_gross = dict(lt)

        # Sec 70(3): current LTCL only against LTCG; Sec 70(2): current STCL vs STCG, then LTCG.
        lt_left = _absorb(lt, lt_loss)
        st_left = _absorb(lt, _absorb(st, st_loss))

        # Sec 74: brought-forward losses, oldest first; STCL against either, LTCL against LTCG.
        bf_used = _ZERO
        for c in carried:
            before = c.amount
            c.amount = _absorb(lt, c.amount if c.term is Term.LONG else _absorb(st, c.amount))
            bf_used += before - c.amount
        carried = [c for c in carried if c.amount > _ZERO]
        if st_left > _ZERO:
            carried.append(_CarriedLoss(fy, Term.SHORT, st_left))
        if lt_left > _ZERO:
            carried.append(_CarriedLoss(fy, Term.LONG, lt_left))

        # Sec 112A exemption on the FY's net LTCG, off the highest-rate slice first.
        exemption_used = _ZERO
        if any(v > _ZERO for v in lt.values()):
            exemption = schedule.ltcg_exemption(fy)
            exemption_used = exemption - _absorb(lt, exemption)

        cg_tax = sum((g * rate for rate, g in st.items()), _ZERO) + sum(
            (g * rate for rate, g in lt.items()), _ZERO
        )

        div_total = _ZERO
        div_taxable = _ZERO
        div_tax = _ZERO
        regimes: dict[date, tuple[Mapping[str, str], Decimal]] = {}
        for d in div_by_fy.get(fy, []):
            regime = schedule.dividend_regime(d.received)
            values, subtotal = regimes.get(regime.effective_from, (regime.values, _ZERO))
            regimes[regime.effective_from] = (values, subtotal + d.amount)
            div_total += d.amount
        for values, subtotal in regimes.values():
            kind = values["kind"]
            if kind == "ddt_exempt":
                continue
            if kind == "ddt_exempt_115bbda":
                excess = max(_ZERO, subtotal - Decimal(values["threshold"]))
                div_taxable += excess
                div_tax += excess * Decimal(values["rate"])
            elif kind == "slab":
                div_taxable += subtotal
                div_tax += subtotal * profile.slab_rate
            else:
                raise TaxScheduleError(f"unknown dividend regime kind {kind!r}")

        surcharge = cg_tax * profile.cg_surcharge_rate + div_tax * profile.dividend_surcharge_rate
        cess_rate = (
            profile.cess_override if profile.cess_override is not None else schedule.cess_rate(fy)
        )
        cess = (cg_tax + div_tax + surcharge) * cess_rate
        out.append(
            FyTax(
                fy=fy,
                stcg_gross={k: _quantize(v) for k, v in stcg_gross.items()},
                ltcg_gross={k: _quantize(v) for k, v in ltcg_gross.items()},
                st_loss=_quantize(st_loss),
                lt_loss=_quantize(lt_loss),
                exempt_ltcg_net=_quantize(exempt_net),
                brought_forward_used=_quantize(bf_used),
                exemption_used=_quantize(exemption_used),
                stcg_taxable={k: _quantize(v) for k, v in st.items()},
                ltcg_taxable={k: _quantize(v) for k, v in lt.items()},
                carried_forward_st=_quantize(
                    sum((c.amount for c in carried if c.term is Term.SHORT), _ZERO)
                ),
                carried_forward_lt=_quantize(
                    sum((c.amount for c in carried if c.term is Term.LONG), _ZERO)
                ),
                expired=_quantize(expired),
                dividends=_quantize(div_total),
                dividend_taxable=_quantize(div_taxable),
                cg_tax=_quantize(cg_tax),
                dividend_tax=_quantize(div_tax),
                surcharge=_quantize(surcharge),
                cess=_quantize(cess),
                payment_date=profile.payment_timing.payment_date(fy),
            )
        )
    return tuple(out)


# ── the whole computation ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AfterTaxResult:
    """Pre-tax and after-tax XIRR for one run, and the per-FY tax behind the difference.

    Two after-tax figures, because they answer different questions:

    - ``after_tax_xirr_realised`` — tax on what the strategy actually sold; the holdings at the end
      are still valued pre-tax. What the run's tax bill *was*.
    - ``after_tax_xirr_liquidated`` — as if every open lot were sold at ``terminal_prices`` on the
      terminal date (no sale costs), and that tax paid too. What the investor *walks away with*.
    """

    source: str
    profile: InvestorProfile
    realisations: tuple[Realisation, ...]
    deemed_realisations: tuple[Realisation, ...]
    fy_taxes: tuple[FyTax, ...]
    fy_taxes_liquidated: tuple[FyTax, ...]
    pre_tax_xirr: Decimal
    after_tax_xirr_realised: Decimal
    #: ``None`` when the deemed sale could not be taxed; ``liquidation_error`` then says why.
    after_tax_xirr_liquidated: Decimal | None
    stt_known: bool
    dividends_credited: int
    terminal_date: date
    terminal_nav: Decimal
    #: Why the deemed-liquidation variant was withheld (a missing Sec 55(2)(ac) FMV), or None.
    liquidation_error: str | None = None

    @property
    def total_tax(self) -> Decimal:
        return sum((f.total for f in self.fy_taxes), _ZERO)

    @property
    def total_tax_liquidated(self) -> Decimal:
        return sum((f.total for f in self.fy_taxes_liquidated), _ZERO)


def _tax_flows(fy_taxes: Iterable[FyTax]) -> list[Cashflow]:
    return [Cashflow(f.payment_date, -f.total) for f in fy_taxes if f.total > _ZERO]


def compute_after_tax(
    ledger: RunLedger,
    profile: InvestorProfile,
    *,
    schedule: TaxSchedule | None = None,
    fmv: GrandfatheringPrices,
) -> AfterTaxResult:
    """Tax a finished run's ledger for ``profile`` and strike pre- and after-tax XIRR.

    What it assumes: every trade is an STT-paid delivery trade in a listed equity share on a
    recognised exchange (what ``SimBroker`` models), so Secs 111A/112A/10(38) govern; the ledger
    is complete (every buy that a sell draws on is in it). What it never does: change the ledger,
    the book, or the terminal NAV — tax is an investor-level outflow alongside the run.
    """
    schedule = schedule or load_tax_schedule()
    realisations, open_lots = match_lots(ledger, schedule, fmv)

    deemed: list[Realisation] = []
    liquidation_error: str | None = None
    try:
        for isin in sorted(open_lots):
            lots = open_lots[isin]
            price = ledger.terminal_prices.get(isin)
            if price is None:
                raise TaxError(f"no terminal price for held {isin}; cannot value the deemed sale")
            _require_decimal("terminal price", price)
            qty = sum(lot.quantity for lot in lots)
            deemed.extend(
                _sell_fifo(
                    lots, isin, qty, price * qty, ledger.terminal_date, schedule, fmv, deemed=True
                )
            )
    except MissingGrandfatheringPriceError as error:
        # Only the deemed sale needed this FMV: the realised figure does not depend on it, so it
        # stands, and the liquidated one is withheld with the reason rather than guessed.
        liquidation_error = str(error)
        deemed = []

    fy_taxes = _fy_taxes(realisations, ledger.dividends, schedule, profile)
    fy_liq = (
        ()
        if liquidation_error is not None
        else _fy_taxes([*realisations, *deemed], ledger.dividends, schedule, profile)
    )

    terminal = Cashflow(ledger.terminal_date, ledger.terminal_nav)
    base = [*ledger.external_flows, terminal]
    result = AfterTaxResult(
        source=ledger.source,
        profile=profile,
        realisations=realisations,
        deemed_realisations=tuple(deemed),
        fy_taxes=fy_taxes,
        fy_taxes_liquidated=fy_liq,
        pre_tax_xirr=xirr(base),
        after_tax_xirr_realised=xirr([*base, *_tax_flows(fy_taxes)]),
        after_tax_xirr_liquidated=(
            None if liquidation_error is not None else xirr([*base, *_tax_flows(fy_liq)])
        ),
        liquidation_error=liquidation_error,
        stt_known=all(t.stt_known for t in ledger.trades),
        dividends_credited=len(ledger.dividends),
        terminal_date=ledger.terminal_date,
        terminal_nav=ledger.terminal_nav,
    )
    _log.info(
        "tax.after_tax",
        source=ledger.source,
        realisations=len(realisations),
        pre_tax_xirr=str(result.pre_tax_xirr),
        after_tax_xirr_realised=str(result.after_tax_xirr_realised),
        after_tax_xirr_liquidated=str(result.after_tax_xirr_liquidated),
        liquidation_error=result.liquidation_error,
        total_tax=str(result.total_tax),
    )
    return result
