"""D2: primary-exchange selection and the per-ISIN canonical daily series (M3.2).

An ISIN commonly trades on both NSE and BSE, and the two sessions disagree — different closes,
very different volumes, and on a thin day one exchange may not print at all. A decision, a chart
or a backtest wants *one* price per ISIN per day, so something has to pick which exchange speaks
for the security. That pick is what this module makes.

Two rules, kept apart on purpose:

* **Primary selection** (`select_primary`) chooses the primary exchange for an ISIN from a window
  of liquidity. Liquidity, not the last close, because the exchange where a security actually
  trades is the one whose price is real — a stale BSE print on ten shares is not a second opinion
  on NIFTY-heavyweight liquidity. The measure defaults to turnover (rupee value traded), the
  standard listing-liquidity yardstick, taken as the *median* over a rolling window so a single
  block-deal spike cannot crown an exchange for a month.

* **Stability** is the reason selection takes an `incumbent`. Two near-tied exchanges whose
  medians cross by a rupee day to day would hand the canonical series a new primary every morning,
  and a series that switches exchange on noise is worse than one slightly stale. So the incumbent
  is retained unless a challenger beats it by a *margin* (`hysteresis`) — a documented dead-band,
  not a coin toss. The first selection, with no incumbent, is decided purely on liquidity with a
  deterministic tie-break.

* **Dedup** (`canonical_daily`) is a pure read-layer projection: given both exchanges' raw rows
  and the primary map, it yields one row per `(isin, trade_date)` — the primary's row, or the
  other exchange's with `fell_back=True` when the primary did not trade that day. It never mutates
  or drops a raw row. Keeping both exchanges' rows in L1 and deduping on read is invariant-shaped:
  the raw truth stays queryable (M3.3's cross-exchange check reads *both*), and the canonical view
  is always recomputable from it.

Offline and clockless by construction. The decision date is passed in as `as_of` (never read from
a wall clock — B10), the liquidity window is defined over *observed* trading dates rather than a
calendar, so no market calendar is needed, and every output is sorted so two runs over the same
inputs produce identical results.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from dataplatform.identity.master import Exchange, IdentityError
from dataplatform.logging import get_logger

__all__ = [
    "Canonical",
    "DailyLiquidity",
    "ExchangeLiquidity",
    "LiquidityMetric",
    "ListingKeyed",
    "NoLiquidityError",
    "PrimaryDecision",
    "PrimaryRule",
    "canonical_daily",
    "select_primary",
    "select_primary_map",
]

_LOG = get_logger(__name__)

#: The default liquidity window and dead-band. 21 observed trading dates is roughly one month of
#: sessions — long enough that a single day cannot decide, short enough to follow a genuine shift
#: in where a security trades. 20% is the margin a challenger must clear to unseat the incumbent:
#: below it the two exchanges are "near-tied" and the primary is held to stop churn (acceptance 2).
_DEFAULT_LOOKBACK: int = 21
_DEFAULT_HYSTERESIS: Decimal = Decimal("0.20")


class NoLiquidityError(IdentityError):
    """No exchange had enough liquidity in the window to name a primary for this ISIN.

    Raised rather than guessed: an ISIN with no observed turnover in the lookback (newly listed,
    long suspended, or a gap in the lake) has no basis for a primary, and picking one anyway would
    be a fact the caller could not tell from a real one. The caller decides whether that is a data
    gap to fill or a security to skip.
    """


class LiquidityMetric(StrEnum):
    """Which traded quantity stands in for "how much this security trades on this exchange".

    Turnover (rupee value) is the default and the right one for a listing-liquidity comparison: it
    is comparable across the two exchanges for the same ISIN and is what an execution desk means by
    liquidity. Volume and trade count are offered for diagnostics and for the rare security whose
    turnover the source did not publish, never as the standing rule without a reason recorded.
    """

    TURNOVER = "TURNOVER"
    """Rupee value traded in the session (`total_traded_value`). The default."""

    VOLUME = "VOLUME"
    """Shares traded. Comparable across exchanges, but blind to price — a penny stock's volume
    dwarfs a heavyweight's turnover."""

    TRADES = "TRADES"
    """Number of trades executed. A crude depth proxy when value and volume are both suspect."""


@dataclass(frozen=True, slots=True)
class DailyLiquidity:
    """One exchange's liquidity for one ISIN on one session — the input to selection.

    Turnover is `Decimal` (money, never float — a float here is a bug). Volume and trades are whole
    counts. A caller builds these from `prices_raw` rows: `total_traded_value`, `total_traded_qty`
    and `total_trades` for the ISIN's row on each exchange.
    """

    isin: str
    exchange: Exchange
    trade_date: date
    turnover: Decimal
    volume: int = 0
    trades: int = 0

    def score(self, metric: LiquidityMetric) -> Decimal:
        """This session's value under `metric`, always as a `Decimal` for one comparison type."""
        if metric is LiquidityMetric.TURNOVER:
            return self.turnover
        if metric is LiquidityMetric.VOLUME:
            return Decimal(self.volume)
        return Decimal(self.trades)


@dataclass(frozen=True, slots=True)
class PrimaryRule:
    """The parameters of primary selection, recorded on every decision so the rule is auditable.

    What it fixes: the liquidity `metric`, the `lookback` (number of most-recent observed trading
    dates the window spans), the `min_observations` an exchange needs in that window to be a
    candidate at all, the `hysteresis` dead-band a challenger must clear to unseat the incumbent,
    and the `tie_break` order that breaks a genuine liquidity tie deterministically.
    What it never does: carry state. It is a pure configuration value; the same rule decides every
    ISIN, and a decision quotes it back via `describe()`.
    """

    metric: LiquidityMetric = LiquidityMetric.TURNOVER
    lookback: int = _DEFAULT_LOOKBACK
    min_observations: int = 1
    hysteresis: Decimal = _DEFAULT_HYSTERESIS
    tie_break: tuple[Exchange, ...] = (Exchange.NSE, Exchange.BSE)

    def __post_init__(self) -> None:
        if self.lookback < 1:
            raise ValueError(f"lookback must be >= 1 observed session, got {self.lookback}")
        if self.min_observations < 1:
            raise ValueError(
                f"min_observations must be >= 1, got {self.min_observations}: an exchange with no "
                "session in the window is not a candidate"
            )
        if self.hysteresis < 0:
            raise ValueError(
                f"hysteresis must be >= 0, got {self.hysteresis}: a negative dead-band would flip "
                "the primary toward the *weaker* exchange"
            )
        if not self.tie_break:
            raise ValueError("tie_break must name at least one exchange to break a liquidity tie")

    def describe(self) -> str:
        """The rule as one line, stored on a decision so an audit reads the policy, not the code."""
        order = "/".join(exchange.value for exchange in self.tie_break)
        return (
            f"primary=median({self.metric.value}) over last {self.lookback} sessions, "
            f"min_obs={self.min_observations}, hysteresis={self.hysteresis}, tie_break={order}"
        )

    def tie_break_index(self, exchange: Exchange) -> int:
        """Position of `exchange` in the tie-break order; unlisted exchanges sort last."""
        try:
            return self.tie_break.index(exchange)
        except ValueError:
            return len(self.tie_break)


@dataclass(frozen=True, slots=True)
class ExchangeLiquidity:
    """One exchange's computed liquidity score over the window — an input a decision records.

    `score` is the median of the exchange's per-session `metric` values within the window;
    `observations` is how many sessions that median was taken over, so a one-print BSE "median" is
    never mistaken for a month of depth.
    """

    exchange: Exchange
    score: Decimal
    observations: int
    window_start: date
    window_end: date


@dataclass(frozen=True, slots=True)
class PrimaryDecision:
    """The chosen primary for one ISIN, with the rule and the inputs that produced it.

    Everything an audit needs to see *why* this exchange won: the `scores` each exchange posted,
    the `incumbent` carried in (if any), whether the pick `changed`, the `rule` in force and a
    human `reason`. `scores` is sorted by exchange so the record is stable across runs.
    """

    isin: str
    primary: Exchange
    as_of: date
    metric: LiquidityMetric
    scores: tuple[ExchangeLiquidity, ...]
    incumbent: Exchange | None
    changed: bool
    rule: str
    reason: str

    def score_for(self, exchange: Exchange) -> ExchangeLiquidity | None:
        """The recorded liquidity for one exchange, or `None` if it had no window observations."""
        for entry in self.scores:
            if entry.exchange is exchange:
                return entry
        return None


def select_primary(
    isin: str,
    observations: Iterable[DailyLiquidity],
    *,
    as_of: date,
    rule: PrimaryRule | None = None,
    incumbent: Exchange | None = None,
) -> PrimaryDecision:
    """Choose the primary exchange for one ISIN as of `as_of`, from a window of liquidity.

    What it does: takes the most-recent `rule.lookback` observed trading dates on or before
    `as_of`, scores each exchange by the median of its `metric` over that window, and picks the
    exchange with the highest score. When an `incumbent` is given and still trades in the window it
    is retained unless a challenger's score exceeds the incumbent's by more than `rule.hysteresis` —
    the dead-band that stops the canonical series flip-flopping on near-ties (acceptance 2).
    What it assumes: `observations` are all for this `isin`; a foreign ISIN is a programming error
    and raises. Only sessions with `trade_date <= as_of` are considered — no future data reaches
    the decision (invariant #7).
    What it never does: invent a primary. If no exchange clears `min_observations` in the window it
    raises `NoLiquidityError`, and a first selection with no incumbent breaks a tie only by the
    documented `tie_break` order, never by chance.
    """
    rule = rule or PrimaryRule()
    scores = _window_scores(isin, observations, as_of=as_of, rule=rule)
    candidates = tuple(
        entry for entry in scores.values() if entry.observations >= rule.min_observations
    )
    if not candidates:
        raise NoLiquidityError(
            f"ISIN {isin!r} has no exchange with >= {rule.min_observations} session(s) in the "
            f"{rule.lookback}-session window ending {as_of.isoformat()}; no basis for a primary"
        )

    ranked = sorted(
        candidates,
        key=lambda e: (-e.score, rule.tie_break_index(e.exchange), e.exchange.value),
    )
    best = ranked[0]
    ordered_scores = tuple(sorted(scores.values(), key=lambda entry: entry.exchange.value))

    incumbent_entry = scores.get(incumbent) if incumbent is not None else None
    incumbent_is_candidate = (
        incumbent_entry is not None and incumbent_entry.observations >= rule.min_observations
    )

    if incumbent is None:
        primary, changed, reason = best.exchange, False, _first_reason(best, ranked, rule)
    elif not incumbent_is_candidate:
        primary, changed = best.exchange, True
        reason = (
            f"incumbent {incumbent.value} had no qualifying session in the window; "
            f"switched to {best.exchange.value}"
        )
    else:
        assert incumbent_entry is not None  # narrowed by incumbent_is_candidate
        primary, changed, reason = _apply_hysteresis(best, incumbent_entry, rule)

    decision = PrimaryDecision(
        isin=isin,
        primary=primary,
        as_of=as_of,
        metric=rule.metric,
        scores=ordered_scores,
        incumbent=incumbent,
        changed=changed,
        rule=rule.describe(),
        reason=reason,
    )
    if changed:
        _LOG.info(
            "primary.changed",
            isin=isin,
            as_of=as_of.isoformat(),
            **{"from": None if incumbent is None else incumbent.value},
            to=primary.value,
            reason=reason,
        )
    return decision


def select_primary_map(
    observations: Iterable[DailyLiquidity],
    *,
    as_of: date,
    rule: PrimaryRule | None = None,
    incumbents: Mapping[str, Exchange] | None = None,
) -> dict[str, PrimaryDecision]:
    """Run `select_primary` for every ISIN present, carrying each one's prior primary forward.

    The bulk entry point the read layer calls once per session: group the day's liquidity by ISIN,
    decide each against yesterday's `incumbents` map, and return a decision per ISIN. An ISIN with
    no qualifying liquidity is skipped with a logged warning rather than failing the whole batch — a
    single thin security must not sink the day's canonicalisation.
    """
    rule = rule or PrimaryRule()
    incumbents = incumbents or {}
    by_isin: dict[str, list[DailyLiquidity]] = {}
    for obs in observations:
        by_isin.setdefault(obs.isin, []).append(obs)

    decisions: dict[str, PrimaryDecision] = {}
    for isin in sorted(by_isin):
        try:
            decisions[isin] = select_primary(
                isin,
                by_isin[isin],
                as_of=as_of,
                rule=rule,
                incumbent=incumbents.get(isin),
            )
        except NoLiquidityError as exc:
            _LOG.warning(
                "primary.no_liquidity", isin=isin, as_of=as_of.isoformat(), detail=str(exc)
            )
    return decisions


@runtime_checkable
class ListingKeyed(Protocol):
    """The three fields `canonical_daily` needs off any row to dedup it — nothing about prices.

    A `prices_raw` row satisfies this once its `exchange` string is mapped to `Exchange`; so does a
    test dataclass. The dedup carries the row through untyped-by-price on purpose: it decides
    *which* exchange speaks for a date, and leaves the OHLCV to the caller.
    """

    @property
    def isin(self) -> str: ...

    @property
    def exchange(self) -> Exchange: ...

    @property
    def trade_date(self) -> date: ...


@dataclass(frozen=True, slots=True)
class Canonical[T: ListingKeyed]:
    """One deduped `(isin, trade_date)` — which exchange's raw row speaks, and whether it fell back.

    `row` is the untouched raw row that was chosen; `exchange` is where it traded; `primary` is the
    ISIN's primary for the day. `fell_back` is True when the primary did not print that session and
    the other exchange's row was used instead — a visible, queryable fact, not a silent swap.
    """

    isin: str
    trade_date: date
    exchange: Exchange
    primary: Exchange
    fell_back: bool
    row: T = field(compare=False)


def canonical_daily[T: ListingKeyed](
    rows: Iterable[T],
    primary_by_isin: Mapping[str, Exchange],
) -> tuple[Canonical[T], ...]:
    """Project both exchanges' raw rows down to one canonical row per `(isin, trade_date)`.

    What it does: for each `(isin, trade_date)` keeps the primary exchange's row; when the primary
    did not trade that day it keeps the other exchange's row and marks `fell_back=True`. The result
    is sorted by `(isin, trade_date)` so it is byte-stable across runs.
    What it assumes: `primary_by_isin` names a primary for every ISIN in `rows` (build it from
    `select_primary_map`); an ISIN with no entry raises rather than guessing, because a canonical
    series that quietly picked an exchange would be undetectably wrong downstream.
    What it never does: mutate, drop or deduplicate the *raw* rows. `rows` still holds both
    exchanges after this returns — dedup is a read-time view, and L1 keeps every raw print
    (acceptance 3). Two rows for the same `(isin, exchange, trade_date)` are a raw contradiction
    (one session is one row) and raise.
    """
    grouped: dict[tuple[str, date], dict[Exchange, T]] = {}
    for row in rows:
        key = (row.isin, row.trade_date)
        by_exchange = grouped.setdefault(key, {})
        if row.exchange in by_exchange:
            raise ValueError(
                f"two raw rows for ISIN {row.isin!r} on {row.exchange.value} "
                f"{row.trade_date.isoformat()}; one session is one row and dedup cannot arbitrate"
            )
        by_exchange[row.exchange] = row

    canon: list[Canonical[T]] = []
    for (isin, trade_date), by_exchange in grouped.items():
        try:
            primary = primary_by_isin[isin]
        except KeyError:
            raise ValueError(
                f"no primary exchange for ISIN {isin!r} on {trade_date.isoformat()}; "
                "canonicalisation needs a primary for every ISIN (see select_primary_map)"
            ) from None
        chosen_exchange, chosen_row, fell_back = _choose_exchange(by_exchange, primary)
        canon.append(
            Canonical(
                isin=isin,
                trade_date=trade_date,
                exchange=chosen_exchange,
                primary=primary,
                fell_back=fell_back,
                row=chosen_row,
            )
        )

    return tuple(sorted(canon, key=lambda c: (c.isin, c.trade_date)))


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _window_scores(
    isin: str,
    observations: Iterable[DailyLiquidity],
    *,
    as_of: date,
    rule: PrimaryRule,
) -> dict[Exchange, ExchangeLiquidity]:
    """Score each exchange by its median metric over the last `rule.lookback` observed sessions.

    The window is defined over *observed* trading dates on or before `as_of` — the union of dates
    any exchange printed — rather than a calendar, so no market-calendar dependency and no future
    date (invariant #7). Each exchange's score is the median of its own sessions inside that date
    set; an exchange that printed on fewer of those dates carries a lower `observations` count.
    """
    in_scope: list[DailyLiquidity] = []
    for obs in observations:
        if obs.isin != isin:
            raise ValueError(
                f"observation for ISIN {obs.isin!r} passed to select_primary({isin!r}); "
                "selection is per ISIN and does not mix securities"
            )
        if obs.trade_date <= as_of:
            in_scope.append(obs)

    window_dates = sorted({obs.trade_date for obs in in_scope}, reverse=True)[: rule.lookback]
    if not window_dates:
        return {}
    window = set(window_dates)
    window_start, window_end = min(window_dates), max(window_dates)

    by_exchange: dict[Exchange, list[Decimal]] = {}
    for obs in in_scope:
        if obs.trade_date in window:
            by_exchange.setdefault(obs.exchange, []).append(obs.score(rule.metric))

    return {
        exchange: ExchangeLiquidity(
            exchange=exchange,
            score=_median(values),
            observations=len(values),
            window_start=window_start,
            window_end=window_end,
        )
        for exchange, values in by_exchange.items()
    }


def _apply_hysteresis(
    best: ExchangeLiquidity,
    incumbent: ExchangeLiquidity,
    rule: PrimaryRule,
) -> tuple[Exchange, bool, str]:
    """Decide whether the challenger unseats the incumbent, given the dead-band.

    The incumbent is retained unless the best challenger's score exceeds it by strictly more than
    `hysteresis` (as a fraction of the incumbent's score). Equality and near-ties inside the band
    keep the incumbent — that retention *is* the stability property.
    """
    if best.exchange is incumbent.exchange:
        return (
            incumbent.exchange,
            False,
            f"{incumbent.exchange.value} retained: still the most liquid (score {incumbent.score})",
        )
    threshold = incumbent.score * (Decimal(1) + rule.hysteresis)
    if best.score > threshold:
        return (
            best.exchange,
            True,
            f"{best.exchange.value} score {best.score} exceeds {incumbent.exchange.value} "
            f"{incumbent.score} by more than the {rule.hysteresis} dead-band; switched",
        )
    return (
        incumbent.exchange,
        False,
        f"{incumbent.exchange.value} retained: challenger {best.exchange.value} score "
        f"{best.score} within the {rule.hysteresis} dead-band of {incumbent.score} (no churn)",
    )


def _first_reason(
    best: ExchangeLiquidity, ranked: Sequence[ExchangeLiquidity], rule: PrimaryRule
) -> str:
    """The reason for a first selection (no incumbent) — names a tie-break if one was used."""
    if len(ranked) > 1 and ranked[1].score == best.score:
        order = "/".join(e.value for e in rule.tie_break)
        return (
            f"{best.exchange.value} chosen on tie-break ({order}): tied with "
            f"{ranked[1].exchange.value} at score {best.score}"
        )
    return f"{best.exchange.value} chosen: highest median {rule.metric.value} (score {best.score})"


def _choose_exchange[T: ListingKeyed](
    by_exchange: Mapping[Exchange, T], primary: Exchange
) -> tuple[Exchange, T, bool]:
    """Pick the primary's row for a date, or fall back to another exchange's when it did not print.

    A deterministic fallback: when the primary is absent, the remaining exchanges are taken in name
    order so the canonical series never depends on dict insertion order.
    """
    primary_row = by_exchange.get(primary)
    if primary_row is not None:
        return primary, primary_row, False
    fallback = min(by_exchange, key=lambda exchange: exchange.value)
    return fallback, by_exchange[fallback], True


def _median(values: Sequence[Decimal]) -> Decimal:
    """Median of a non-empty sequence of `Decimal`s, robust to a single liquidity spike.

    Median rather than mean on purpose: one block-deal session can multiply an exchange's turnover
    for a day, and a mean would let that day crown the exchange for the whole window. The even-count
    case averages the two central values; the divide-by-two is exact for money.
    """
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)
