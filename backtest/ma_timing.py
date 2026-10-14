"""Research: index timing on a simple moving average — hold the TRI above its N-SMA, else cash.

The owner's question (2026-10-14): does a 30-day moving-average rule beat buy-and-hold here, as an
article claimed it did in the US? This module is the small, honest backtester that answers it on
the lake's published NSE total-return indices. It reuses the repo's readers and models rather than
re-implementing any of them:

* **The index** is the exchange's published TRI out of ``L1/benchmark_tri``
  (:func:`dataplatform.ingest.indices.read_tri_series`, ``method="published"``) — the same series
  the D13 regime gate reads. :func:`load_index` refuses a series whose ``index_name`` disagrees
  other than by letter case (the lake carries ``'NIFTY 50'`` on two sessions and ``'Nifty 50'`` on
  every other one; that is a label, one point per session, and is normalised here).
* **Costs** go through the one shared cost model (:mod:`execution.costs`, invariant #4). Each switch
  is a whole-book trade of a liquid index ETF at the next session's close, plus a stated slippage.
* **Idle cash** earns RBI repo less 50 bp (:mod:`backtest.cash_interest`, the repo's convention).
* **Tax** is the repo's FIFO lot model (:func:`backtest.tax.compute_after_tax`).

**The rule (the reading implemented, stated because the article's exact rule is unknown):** on each
*check* session ``t`` (every session, the last session of each week, or of each month), compare the
TRI's close with its ``N``-session simple moving average over the last ``N`` closes knowable by
``t``'s close (the close itself included). Invested when ``close >= SMA x (1 + band)``, out when
``close < SMA x (1 - band)``, unchanged in between (with ``band = 0`` that is simply above/below).
The position decided at ``t``'s close is traded at the close of the **next** session ``t+1`` —
never the same close — and earns from there.

What this module never does: read a wall clock, take a float for money, write anywhere, fetch, or
let a point with ``knowable_date`` after a session reach that session's signal (invariant #7).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Final

from backtest.cash_interest import RepoRateSchedule
from backtest.sharpe import sharpe_stats
from backtest.tax import (
    AfterTaxResult,
    InterestIncome,
    InvestorProfile,
    MappingGrandfatheringPrices,
    PaymentTiming,
    RunLedger,
    TaxSchedule,
    TaxTrade,
    compute_after_tax,
)
from backtest.xirr import Cashflow, xirr
from dataplatform.ingest.indices import TRI_METHOD_PUBLISHED, read_tri_series
from execution.costs import CostModel, Side, Trade

__all__ = [
    "COST_CARD_FROM",
    "ETF_ISIN",
    "ETF_UNIT_DIVISOR",
    "OPENING_CASH",
    "SLIPPAGE",
    "TAX_PROFILE",
    "Cadence",
    "CostMode",
    "IndexPoint",
    "IndexSeries",
    "MaTimingError",
    "Metrics",
    "SimResult",
    "TimingRule",
    "TradeRecord",
    "after_tax",
    "load_index",
    "metrics",
    "simulate",
    "states",
]

#: The book every run opens with — the repo's backtest convention (₹10 L).
OPENING_CASH: Final = Decimal("1000000")
#: Slippage per side on an index-ETF switch, as a fraction of the close: half a typical NIFTYBEES
#: bid/ask plus a little iNAV premium/discount. Stated once, never tuned.
SLIPPAGE: Final = Decimal("0.0010")
#: The instrument a switch trades, for the cost model's per-scrip DP charge only: Nippon India ETF
#: Nifty 50 BeES (NIFTYBEES). Every listed index ETF is on the same delivery rate card, so the
#: same key prices a NIFTY 500 / midcap / smallcap ETF switch identically.
ETF_ISIN: Final = "INF204KB14I2"
#: An ETF unit is modelled as the TRI level ÷ 100 (NIFTYBEES trades near the price index ÷ 100).
#: Only the whole-unit rounding of a switch depends on it.
ETF_UNIT_DIVISOR: Final = Decimal("100")
#: The shared cost model's rate card starts here; an earlier switch is priced on this date's card
#: (a stated proxy — the model itself refuses a pre-card date, rightly).
COST_CARD_FROM: Final = date(2006, 1, 1)
#: The investor the after-tax figures are for: the fold campaign's (30% slab, no surcharge, tax
#: paid at FY end).
TAX_PROFILE: Final = InvestorProfile(
    residency="resident_individual",
    slab_rate=Decimal("0.30"),
    cg_surcharge_rate=Decimal("0"),
    dividend_surcharge_rate=Decimal("0"),
    payment_timing=PaymentTiming.FY_END,
)

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_PAISA: Final = Decimal("0.01")
_DAYS_IN_YEAR: Final = Decimal("365")
_YEAR: Final = Decimal("365.25")
_SESSIONS_PER_YEAR: Final = 252
#: Sizing margin on a buy: units are first sized so price x (1 + this) fits the cash, then trimmed
#: until turnover plus the model's charges fit exactly.
_BUY_MARGIN: Final = Decimal("0.003")
_GRANDFATHER_DATE: Final = date(2018, 1, 31)


class MaTimingError(Exception):
    """The series or the run cannot be built as stated. Fails loud (CLAUDE.md)."""


# ── the index ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IndexPoint:
    """One published close: its session, when it became knowable, and the TRI level."""

    session: date
    knowable: date
    level: Decimal


@dataclass(frozen=True, slots=True)
class IndexSeries:
    """A published TRI series, label-normalised, with what the normalisation found."""

    slug: str
    name: str
    points: tuple[IndexPoint, ...]
    #: Each raw ``index_name`` spelling and the sessions that carried it (the case-duplicate check).
    spellings: Mapping[str, tuple[date, ...]]

    @property
    def sessions(self) -> tuple[date, ...]:
        return tuple(p.session for p in self.points)


def load_index(slug: str, *, through: date, data_root: Path | None) -> IndexSeries:
    """The published TRI for ``slug`` through ``through``, read-only, or raise.

    Assumes nothing about the label: spellings that differ only in case are one index (recorded in
    ``spellings`` for the report); spellings that differ otherwise raise, as do a session carried
    twice, a ``knowable_date`` before its session, or knowable dates that go backwards.
    """
    series = read_tri_series(slug, through, method=TRI_METHOD_PUBLISHED, data_root=data_root)
    if series is None:
        raise MaTimingError(f"no published {slug!r} TRI in L1/benchmark_tri through {through}")
    spellings: dict[str, list[date]] = {}
    for p in series.points:
        spellings.setdefault(p.index_name, []).append(p.as_of)
    normalised = {name.casefold() for name in spellings}
    if len(normalised) != 1:
        raise MaTimingError(f"{slug}: index_name disagrees beyond case: {sorted(spellings)}")
    points: list[IndexPoint] = []
    for p in series.points:
        level = Decimal(p.tri_value)
        if level <= _ZERO:
            raise MaTimingError(f"{slug}: non-positive TRI {level} on {p.as_of}")
        if p.knowable_date < p.as_of:
            raise MaTimingError(f"{slug}: {p.as_of} knowable before its own close")
        points.append(IndexPoint(p.as_of, p.knowable_date, level))
    for a, b in pairwise(points):
        if b.session <= a.session:
            raise MaTimingError(f"{slug}: session {b.session} repeats or goes backwards")
        if b.knowable < a.knowable:
            raise MaTimingError(f"{slug}: knowable dates go backwards at {b.session}")
    # The most common spelling is the name; the rest are recorded, not hidden.
    name = max(spellings, key=lambda n: len(spellings[n]))
    return IndexSeries(
        slug=slug,
        name=name,
        points=tuple(points),
        spellings={n: tuple(d) for n, d in spellings.items()},
    )


# ── the rule ──────────────────────────────────────────────────────────────────────────────────────


class CostMode(StrEnum):
    """How a switch is charged. Every mode trades the same whole units at the same closes."""

    #: No charges, no slippage — the "before costs" reading.
    FRICTIONLESS = "frictionless"
    #: The shared cost model's delivery-equity card plus :data:`SLIPPAGE` — the headline.
    CARD = "card"
    #: As ``CARD`` with the STT line left out. The card charges delivery-equity STT (0.1% a leg
    #: since 2012) because it has no ETF instrument type; an exchange-traded equity ETF pays far
    #: less, so this is the lower bound on cost the headline is bracketed by. Nothing is computed
    #: outside the model: its own STT line is simply not charged.
    CARD_EX_STT = "card_ex_stt"


class Cadence(StrEnum):
    """How often the signal is read. Between check sessions the last decision stands."""

    DAILY = "daily"
    #: The last session of each ISO week (the Friday, or the session before a Friday holiday).
    WEEKLY = "weekly"
    #: The last session of each calendar month.
    MONTHLY = "monthly"


@dataclass(frozen=True, slots=True)
class TimingRule:
    """``N``-session SMA timing with an optional symmetric hysteresis band and a check cadence."""

    ma_days: int
    band: Decimal = _ZERO
    cadence: Cadence = Cadence.DAILY

    def __post_init__(self) -> None:
        if self.ma_days <= 0:
            raise ValueError(f"ma_days must be positive, got {self.ma_days}")
        if not isinstance(self.band, Decimal) or not (_ZERO <= self.band < _ONE):
            raise ValueError(f"band must be a Decimal in [0, 1), got {self.band!r}")

    @property
    def label(self) -> str:
        parts = [f"N={self.ma_days}"]
        if self.band:
            parts.append(f"±{(self.band * 100).normalize()}% band")
        if self.cadence is not Cadence.DAILY:
            parts.append(self.cadence.value)
        return ", ".join(parts)


def _check_sessions(sessions: Sequence[date], cadence: Cadence) -> set[date]:
    """The sessions on which ``cadence`` reads the signal (the exchange calendar is published in
    advance, so knowing a session is a week's or month's last is not look-ahead)."""
    if cadence is Cadence.DAILY:
        return set(sessions)
    key = (
        (lambda d: d.isocalendar()[:2])
        if cadence is Cadence.WEEKLY
        else (lambda d: (d.year, d.month))
    )
    last: dict[object, date] = {}
    for s in sessions:
        last[key(s)] = s
    return set(last.values())


def states(
    points: Sequence[IndexPoint], rule: TimingRule
) -> dict[date, tuple[bool | None, Decimal | None]]:
    """The invested/out decision struck at each session's close, with the SMA it used.

    The decision at session ``t`` uses only points whose ``knowable_date`` is on or before ``t``
    (invariant #7) — the last ``N`` of them, ``t``'s own close included when it is knowable at
    ``t``. ``None`` until ``N`` points are knowable. Hysteresis state carries across sessions and
    between check sessions; it is a function of the past alone.
    """
    sessions = [p.session for p in points]
    checks = _check_sessions(sessions, rule.cadence)
    upper = _ONE + rule.band
    lower = _ONE - rule.band
    window: deque[Decimal] = deque()
    running = _ZERO
    admitted = 0
    state: bool | None = None
    out: dict[date, tuple[bool | None, Decimal | None]] = {}
    for session in sessions:
        while admitted < len(points) and points[admitted].knowable <= session:
            level = points[admitted].level
            window.append(level)
            running += level
            if len(window) > rule.ma_days:
                running -= window.popleft()
            admitted += 1
        if len(window) < rule.ma_days:
            out[session] = (None, None)
            continue
        sma = running / Decimal(rule.ma_days)
        close = window[-1]
        if session in checks:
            if close >= sma * upper:
                state = True
            elif close < sma * lower:
                state = False
            elif state is None:
                # Inside the band with no prior decision: the unbanded reading starts the state.
                state = close >= sma
        out[session] = (state, sma)
    return out


# ── the walk ──────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One switch as the account saw it: the fill, its charges and the cash it moved."""

    session: date
    side: Side
    units: int
    price: Decimal
    charges: Decimal
    stt: Decimal
    net_amount: Decimal


@dataclass(frozen=True, slots=True)
class SimResult:
    """One run over one window: daily NAV, what was held, every switch and the cash interest."""

    label: str
    start: date
    end: date
    opening: Decimal
    nav: tuple[tuple[date, Decimal], ...]
    #: Whether the book held the index over the session ending on each date (from the prior close).
    invested: tuple[bool, ...]
    trades: tuple[TradeRecord, ...]
    interest: tuple[InterestIncome, ...]
    #: Per-session cash earning rate (annual), to strike excess returns for the Sharpe ratio.
    cash_rate: tuple[Decimal, ...]
    terminal_units: int
    terminal_unit_price: Decimal
    gf_unit_price: Decimal | None
    costs: CostMode
    #: Sessions before the cost card or the repo schedule that were priced on a stated proxy.
    proxied_cost_trades: int = 0
    proxied_cash_days: int = 0

    @property
    def switches(self) -> int:
        """Changes of position after the opening entry (each buy or sell after the first trade)."""
        return max(len(self.trades) - 1, 0) if self.trades else 0

    @property
    def charges(self) -> Decimal:
        return sum((t.charges for t in self.trades), _ZERO)

    @property
    def stt(self) -> Decimal:
        return sum((t.stt for t in self.trades), _ZERO)

    @property
    def slippage_cost(self) -> Decimal:
        """Rupees lost to the stated slippage across every fill (vs the close)."""
        total = _ZERO
        for t in self.trades:
            mid = t.price / ((_ONE + SLIPPAGE) if t.side is Side.BUY else (_ONE - SLIPPAGE))
            total += abs(t.price - mid) * t.units
        return total.quantize(_PAISA) if self.costs is not CostMode.FRICTIONLESS else _ZERO


@dataclass(slots=True)
class _Accrual:
    month: tuple[int, int] | None = None
    amount: Decimal = _ZERO
    credits: list[InterestIncome] = field(default_factory=list)


def _earning_rate(schedule: RepoRateSchedule, day: date) -> tuple[Decimal, bool]:
    """Repo less 50 bp on ``day``; before the schedule opens, its first day's rate (a proxy)."""
    if day < schedule.coverage_from:
        return schedule.earning_rate(schedule.coverage_from), True
    return schedule.earning_rate(day), False


def simulate(
    series: IndexSeries,
    *,
    start: date,
    end: date,
    rule: TimingRule | None,
    cost_model: CostModel,
    repo: RepoRateSchedule,
    costs: CostMode = CostMode.CARD,
    lag: int = 1,
    decided: Mapping[date, tuple[bool | None, Decimal | None]] | None = None,
) -> SimResult:
    """Walk ``[start, end]``: buy-and-hold when ``rule`` is ``None``, else the timing rule.

    Each session: first the book earns the move from the prior close (units x level change, cash
    interest over the calendar days elapsed, credited monthly as in :mod:`backtest.cash_interest`);
    then, at this close, it trades to the decision struck ``lag`` sessions earlier. ``lag`` is 1 in
    every reported figure — the signal at ``t``'s close trades at ``t+1``'s close; 0 exists only so
    a test can prove the lag is real. A session whose decision is still ``None`` (too few points)
    stays in cash. Buy-and-hold buys at the first session's close.

    ``costs`` picks the charging (:class:`CostMode`); every mode trades the same whole units.
    """
    if lag < 0:
        raise ValueError("lag must be >= 0")
    all_sessions = series.sessions
    index = {s: i for i, s in enumerate(all_sessions)}
    window = [s for s in all_sessions if start <= s <= end]
    if len(window) < 2:
        raise MaTimingError(f"{series.slug}: fewer than two sessions in {start}..{end}")
    levels = {p.session: p.level for p in series.points}
    if rule is not None and decided is None:
        decided = states(series.points, rule)

    cash = OPENING_CASH
    units = 0
    accrual = _Accrual()
    nav: list[tuple[date, Decimal]] = []
    invested: list[bool] = []
    rates: list[Decimal] = []
    trades: list[TradeRecord] = []
    proxied_cost = proxied_cash = 0
    previous: date | None = None

    def fill(session: date, side: Side, qty: int, mid: Decimal) -> TradeRecord:
        nonlocal proxied_cost
        if costs is CostMode.FRICTIONLESS:
            turnover = (mid * qty).quantize(_PAISA)
            return TradeRecord(session, side, qty, mid, _ZERO, _ZERO, turnover)
        price = (mid * ((_ONE + SLIPPAGE) if side is Side.BUY else (_ONE - SLIPPAGE))).quantize(
            _PAISA
        )
        priced_on = session
        if session < COST_CARD_FROM:
            priced_on = COST_CARD_FROM
            proxied_cost += 1
        cost = cost_model.charge(Trade(ETF_ISIN, priced_on, side, qty, price))
        stt = cost.securities_transaction_tax
        if costs is CostMode.CARD_EX_STT:
            net = cost.net_amount - stt if side is Side.BUY else cost.net_amount + stt
            return TradeRecord(session, side, qty, price, cost.total - stt, _ZERO, net)
        return TradeRecord(session, side, qty, price, cost.total, stt, cost.net_amount)

    for session in window:
        level = levels[session]
        mid = (level / ETF_UNIT_DIVISOR).quantize(_PAISA)
        rates.append(_earning_rate(repo, session)[0])
        # 1. Carry the book from the previous close: interest on cash for each calendar day.
        if previous is not None:
            invested.append(units > 0)
            day = previous
            while day < session:
                day_rate, day_proxy = _earning_rate(repo, day)
                if day_proxy and cash > _ZERO:
                    proxied_cash += 1
                month = (day.year, day.month)
                if accrual.month != month:
                    if accrual.amount > _ZERO and accrual.month is not None:
                        # The prior month's accrual lands on this session (first visit in a later
                        # month), spendable from here — the repo convention.
                        credit = accrual.amount.quantize(_PAISA)
                        if credit > _ZERO:
                            accrual.credits.append(InterestIncome(session, credit))
                            cash += credit
                    accrual.month, accrual.amount = month, _ZERO
                accrual.amount += cash * day_rate / _DAYS_IN_YEAR
                day = date.fromordinal(day.toordinal() + 1)
        else:
            accrual.month = (session.year, session.month)
        # 2. Trade at this close to the decision struck `lag` sessions earlier.
        if rule is None:
            want = True
        else:
            assert decided is not None
            i = index[session] - lag
            want = bool(decided[all_sessions[i]][0]) if i >= 0 else False
        if want and units == 0:
            qty = int((cash / (mid * (_ONE + _BUY_MARGIN))).to_integral_value(ROUND_DOWN))
            while qty > 0:
                record = fill(session, Side.BUY, qty, mid)
                if record.net_amount <= cash:
                    break
                qty -= 1
            if qty > 0:
                trades.append(record)
                cash -= record.net_amount
                units = qty
        elif not want and units > 0:
            record = fill(session, Side.SELL, units, mid)
            trades.append(record)
            cash += record.net_amount
            units = 0
        nav.append((session, (cash + units * mid).quantize(_PAISA)))
        previous = session

    last = window[-1]
    gf = levels.get(_GRANDFATHER_DATE)
    return SimResult(
        label="buy-and-hold" if rule is None else rule.label,
        start=window[0],
        end=last,
        opening=OPENING_CASH,
        nav=tuple(nav),
        invested=tuple(invested),
        trades=tuple(trades),
        interest=tuple(accrual.credits),
        cash_rate=tuple(rates),
        terminal_units=units,
        terminal_unit_price=(levels[last] / ETF_UNIT_DIVISOR).quantize(_PAISA),
        gf_unit_price=None if gf is None else (gf / ETF_UNIT_DIVISOR).quantize(_PAISA),
        costs=costs,
        proxied_cost_trades=proxied_cost,
        proxied_cash_days=proxied_cash,
    )


# ── the numbers ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Metrics:
    """The mandate's figures for one run, all ``Decimal`` (ratios, not percentages)."""

    cagr: Decimal
    max_drawdown: Decimal
    volatility: Decimal
    sharpe: Decimal
    invested_share: Decimal
    switches_per_year: Decimal
    switches: int
    years: Decimal

    @property
    def ratio(self) -> Decimal:
        """CAGR ÷ max drawdown — the mandate's ranking figure."""
        return self.cagr / self.max_drawdown if self.max_drawdown > _ZERO else _ZERO


def _max_drawdown(values: Iterable[Decimal]) -> Decimal:
    peak = _ZERO
    worst = _ZERO
    for v in values:
        peak = max(peak, v)
        if peak > _ZERO:
            worst = max(worst, _ONE - v / peak)
    return worst


def metrics(run: SimResult) -> Metrics:
    """CAGR (the XIRR of a single deposit), max drawdown, volatility, Sharpe vs the cash rate,
    time invested and switching rate over the run."""
    first, last = run.nav[0], run.nav[-1]
    years = Decimal((last[0] - first[0]).days) / _YEAR
    cagr = xirr([Cashflow(first[0], -run.opening), Cashflow(last[0], last[1])])
    values = [v for _, v in run.nav]
    returns: list[float] = []
    excess: list[float] = []
    for i in range(1, len(run.nav)):
        r = values[i] / values[i - 1] - _ONE
        days = Decimal((run.nav[i][0] - run.nav[i - 1][0]).days)
        returns.append(float(r))
        excess.append(float(r - run.cash_rate[i - 1] * days / _DAYS_IN_YEAR))
    vol = Decimal(repr(sharpe_stats(returns).stdev)) * Decimal(_SESSIONS_PER_YEAR).sqrt()
    sharpe = Decimal(repr(sharpe_stats(excess).sharpe)) * Decimal(_SESSIONS_PER_YEAR).sqrt()
    held = sum(1 for x in run.invested if x)
    share = Decimal(held) / Decimal(len(run.invested)) if run.invested else _ZERO
    return Metrics(
        cagr=cagr,
        max_drawdown=_max_drawdown(values),
        volatility=vol,
        sharpe=sharpe,
        invested_share=share,
        switches_per_year=Decimal(run.switches) / years if years > _ZERO else _ZERO,
        switches=run.switches,
        years=years,
    )


def after_tax(run: SimResult, *, schedule: TaxSchedule | None = None) -> AfterTaxResult:
    """The run taxed for :data:`TAX_PROFILE` — FIFO lots, STCG/LTCG, slab tax on cash interest.

    The comparable figure is the *liquidated* after-tax XIRR: what is held at the end is deemed
    sold at the close (no sale cost), so buy-and-hold's deferred LTCG is charged too. The ETF's
    Sec 55(2)(ac) FMV is the TRI unit price on 31-01-2018.
    """
    trades = tuple(
        TaxTrade(
            isin=ETF_ISIN,
            trade_date=t.session,
            side=t.side,
            quantity=t.units,
            net_amount=t.net_amount,
            stt=t.stt,
            stt_known=True,
        )
        for t in run.trades
    )
    ledger = RunLedger(
        source=f"ma_timing {run.label} {run.start}..{run.end}",
        trades=trades,
        external_flows=(Cashflow(run.start, -run.opening),),
        terminal_date=run.end,
        terminal_nav=run.nav[-1][1],
        terminal_prices={ETF_ISIN: run.terminal_unit_price},
        interest=run.interest,
    )
    fmv = MappingGrandfatheringPrices(
        {ETF_ISIN: run.gf_unit_price} if run.gf_unit_price is not None else {}
    )
    return compute_after_tax(ledger, TAX_PROFILE, schedule=schedule, fmv=fmv)
