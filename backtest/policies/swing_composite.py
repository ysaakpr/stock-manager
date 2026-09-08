"""A composite-signal swing policy: 7-90 day holds, with entry and exit as separate rules (§7, X2).

Where momentum v2 (:mod:`backtest.policies.momentum_v2`) rotates a book monthly on one price signal,
this policy is built for the *swing* horizon — a name is entered, held for weeks rather than a year,
and exited on a rule that is written down rather than implied by the next rebalance. Three things
differ, and each is a measured finding rather than a preference:

* **The entry score is a composite of three near-orthogonal signals**, not a single trailing return.
  Measured over the whole 2016-2026 lake (4.27M bars, ~900-name liquid cross-section, weekly
  samples), the top decile of each signal beats the equal-weight universe over 63 sessions by:
  52-week-high proximity 1.50 %, 5-day delivery share 2.59 %, 12-1 momentum 2.02 % — and the
  equal-weighted composite of the three by **3.35 % (t = 13.3)**, half again the best single part.
  The three carry different information: where the price sits in its own year, how much of the
  recent tape was *delivery* rather than intraday churn, and the classical trend.

* **Delivery share is the strongest single signal in the store and nothing else reads it.** L1 has
  carried ``deliv_pct`` since 2016; its rank-IC t-statistic (10.7 at 21 sessions, 14.8 at 90) is the
  highest of any signal tested, price signals included. It is the one genuinely India-specific
  edge available here: a rising delivery share is buying that intends to hold the stock overnight.

* **Exit is three explicit rules, not one.** A rank band (hysteresis) carries the position while it
  is merely drifting; a re-underwrite at ``max_hold`` forces a still-held name to re-earn its place;
  a *wide* trailing stop caps the tail. Tight stops were measured and rejected — see below.

**The holding period is a cost decision, and the data settles it.** The composite's excess accrues
at a near-constant ~0.2 %/week out to 125 sessions: the marginal 5-day slice earns about as much at
day 120 as at day 20, so there is no burst to capture early. Alpha is therefore linear in time held
while friction is paid per *trade* — 0.223 % statutory (``execution/costs/rates.yaml``) plus roughly
0.22 % of modelled slippage, about 0.45 % the round trip. Against a top-decile excess of 0.35 % over
5 sessions, **a 7-day rotation is underwater before it starts**; over 21 sessions the same entries
earn 1.29 % and over 63 sessions 3.35 %. Identical entries exited purely on time net ~20 %/yr at a
10-session hold against ~28 %/yr at 63. The defaults below therefore sit at the *long* end of the
7-90 day band, and ``min_hold_sessions`` exists to stop a name being churned inside a fortnight.

**What raises trade frequency without paying for it twice** is the rebalance *interval*, not the
holding period. Deciding every ``rebalance_interval_sessions`` (10 — a fortnight) instead of monthly
roughly doubles the decision points, while the sell band means most decisions change nothing: a name
is bought into the top ``top_n`` but only sold once it leaves the wider top ``sell_band``. More
opportunities to act, the same average holding period, and turnover set by the band rather than the
calendar.

**Stops, measured on 33,106 real forward paths.** Capital freed early held idle to the horizon (no
free redeployment credit), per-trade net return: no stop 6.33 %, 20 % stop 6.32 %, 12 % stop 6.03 %,
8 % stop 5.50 %, 25 % trailing 6.43 %, 10 % trailing 4.91 %, and a 15 % profit target with a 10 %
stop **2.17 %**. Two conclusions are wired in as defaults: a tight stop destroys the edge (an 8 %
stop turns a +3.4 % median into -5.5 % and a 57 % win rate into 44 %, because a momentum name's
ordinary path passes through an 8 % drawdown), and a profit target is the single worst rule tested,
since truncating the winners is exactly what removes momentum's payoff. The default is a **25 %
trailing stop** — return-neutral (6.43 % vs 6.33 %) while cutting the 5th-percentile outcome from
-25.3 % to -24.0 %, and materially more at tighter settings for those who want it. It is checked
every session against that session's close, so it is a real stop and not a month-end approximation.

**M12.1 widened the engine without moving a default.** The entry score was always a weighted sum of
rank-normalised legs; only three legs existed. Five more now do — the 5-session return, the
21-session return, delivery *acceleration* (its 5-session mean over its 63-session mean), turnover
expansion and proximity to a 50-session mean — plus volatility as a scored leg rather than only the
screen it already was. Every weight is **signed**, so a leg can be asked for its reverse: a negative
``weight_return_5`` is the short-term reversal family, which nothing in this repo had measured, and
a positive one is short-term trend. Every new weight defaults to zero, so the policy above this
paragraph is bit-for-bit the one M10.7 measured, and a leg the lake cannot compute for a name takes
its neutral value (0 for a return, 1 for a ratio) rather than dropping the name — the candidate set
must not move with the weight vector, or an arm-to-arm comparison becomes a universe comparison.

M12.1 also added the ``regime_filter`` gate momentum v2 has carried since M9.5 and this policy did
not: no new buys while the broad-market proxy sits below its own 200-session mean. It suppresses
*buys only*. Every exit is staged before the gate is consulted, because risk-off must never trap a
position the band, the re-underwrite or the stop has already decided to sell — the book runs down
through its own exits rather than being liquidated on the gate. A session with no regime reading at
all is treated as risk-**off**: an absent regime is not a licence to buy.

Point-in-time (invariant #7): every figure on a record — the 252-session high, the delivery mean,
the 12-1 return, the volatility, every M12.1 leg — is struck over sessions on or before the record's
``knowable_date``, and every read goes through ``ctx.pit.admit``, the regime reading included. The
trailing stop reads only the current session's close.

What it never does: read a wall clock (time is ``ctx.clock``), key on a symbol (ISIN only —
invariant #2), hold a cost model (the injected broker owns the one shared model — invariants #4/#5),
or reach data outside the point-in-time context. It carries per-position state (entry session and
running peak) across sessions, seeded only from what it has itself observed, so a replay from the
same start reproduces every decision exactly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from backtest.policies.momentum_v2 import RegimeReading
from backtest.replay import SessionContext, SessionDecision
from backtest.sip import simulate_sip_instalment
from dataplatform.query.pit import Dataset
from execution.broker import Exchange, Holding, OrderRequest, Side

__all__ = [
    "RegimeReading",
    "SwingCompositeData",
    "SwingCompositeParameters",
    "SwingCompositePolicy",
    "SwingRecord",
    "composite_scores",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_WEIGHT_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class SwingRecord:
    """One candidate this session: the three entry signals, a risk screen, a price, and its date.

    * ``high_proximity`` — the close divided by the highest close of the trailing 252 sessions, so
      ``1.0`` is a fresh 52-week high and ``0.7`` is 30 % below one. The strongest price signal
      measured here (rank-IC 0.101 at 63 sessions against 0.072 for the trailing return).
    * ``delivery_share`` — the mean NSE delivery percentage over the trailing 5 sessions, as a ratio
      (``0.55`` is 55 % delivered). The highest-t-statistic signal in the store.
    * ``momentum_12_1`` — the ``t-12m .. t-1m`` return, the classical momentum definition, kept
      because it carries information the other two do not.
    * ``volatility`` — trailing daily-return volatility, used only to *exclude* the most volatile
      tail (high volatility was the strongest negative signal measured; the lowest-volatility decile
      is not itself attractive, so volatility screens rather than scores).
    * ``price`` — the raw close the whole-share sizing and the trailing stop read (invariant #3:
      the adjusted series is for the signal, never for a fill).

    M12.1 added five more legs, each of which the lake already carried and no policy read. They are
    *features*, not directions: the sign lives in the weight, so ``weight_return_5`` negative is the
    short-term reversal family and positive is short-term trend, and neither is asserted here.

    * ``return_5`` — the 5-session return. The input to the one classical short-horizon effect
      nothing in this repo had measured.
    * ``momentum_1m`` — the 21-session return: the swing horizon's own trend, which the 12-1 leg
      deliberately excludes (12-1 skips the most recent month).
    * ``delivery_trend`` — the 5-session delivery mean over its 63-session mean, so ``1.2`` is a
      fifth more delivery than usual. Where ``delivery_share`` is the *level*, this is the change,
      and it is the largest name-level coefficient in X2's fitted forecast.
    * ``turnover_expansion`` — the 5-session mean traded value over its 63-session mean: whether the
      tape is getting busier in this name.
    * ``ma_proximity`` — the close over its own 50-session mean.

    Never holds a ``float``, a non-positive price, or a non-positive high proximity.
    """

    isin: str
    high_proximity: Decimal
    delivery_share: Decimal
    momentum_12_1: Decimal
    volatility: Decimal
    price: Decimal
    knowable_date: date
    # M12.1 legs. Neutral defaults, so a record built for the three original legs — or for the
    # per-session marks, which carry only a price — is unchanged and scores these at zero weight.
    return_5: Decimal = _ZERO
    momentum_1m: Decimal = _ZERO
    delivery_trend: Decimal = _ONE
    turnover_expansion: Decimal = _ONE
    ma_proximity: Decimal = _ONE

    def __post_init__(self) -> None:
        for name in (
            "high_proximity",
            "delivery_share",
            "momentum_12_1",
            "volatility",
            "price",
            "return_5",
            "momentum_1m",
            "delivery_trend",
            "turnover_expansion",
            "ma_proximity",
        ):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(
                    f"{name} must be a Decimal — money/signal is never float (CLAUDE.md)"
                )
        if self.price <= _ZERO:
            raise ValueError(f"price must be positive, got {self.price}")
        if self.high_proximity <= _ZERO:
            raise ValueError(f"high_proximity must be positive, got {self.high_proximity}")
        if self.volatility < _ZERO:
            raise ValueError(f"volatility must be non-negative, got {self.volatility}")


@dataclass(frozen=True, slots=True)
class SwingCompositeParameters:
    """The stated knobs. Every default is a measured finding or a round number, never a fit.

    Entry:

    * ``top_n`` (20) — names held, matching every other policy here so a comparison isolates the
      signal rather than the basket size.
    * ``weight_high`` / ``weight_delivery`` / ``weight_momentum`` (1 each) — equal weights on the
      three rank-normalised components. Equal because the three measured within a whisker of each
      other and any other split would be a fit.
    * ``exclude_vol_fraction`` (0.10) — drop the most volatile tenth of the candidate set before
      scoring. Volatility's negative relationship with forward return is the strongest measured
      (rank-IC -0.081 at 63 sessions), but it is not monotone at the low end, so it is a screen.

    Exit — three rules, any of which can fire:

    * ``sell_band`` (60, i.e. 3x ``top_n``) — hysteresis. A holding is sold once its composite rank
      leaves the top ``sell_band``, not merely the top ``top_n``, so a name drifting between rank 20
      and 60 is carried instead of churned. Three times the basket is the round multiple, and the
      band is the *whole* turnover control: 81 % of a top-20 composite basket is still inside the
      top 40 one fortnight later and ~95 % inside the top 100, so the band and not the calendar is
      what sets how much this policy trades. The report sweeps it.
    * ``max_hold_sessions`` (63, ~90 calendar days — the top of the band) — a re-underwrite, not a
      forced liquidation: at this age a holding must still be inside the top ``top_n`` to be
      carried, otherwise it is sold. Forcing out a name that still ranks first would pay a round
      trip to buy it straight back.
    * ``trailing_stop`` (0.25) — sell when the close falls this far below the position's highest
      close since entry. Checked every session. Wide by design: tighter settings were measured and
      reduce return (see the module docstring). ``None`` disables it.
    * ``resell_cooldown_sessions`` (21, a trading month) — how long to wait before re-staging a
      sell that did not fill. Only ever binds on a name that has stopped printing and so cannot be
      filled at all; see :class:`_Position`.
    * ``min_hold_sessions`` (5, ~7 calendar days — the bottom of the requested band) — no rule
      except the trailing stop may sell inside this age. Stops a name entered on one rebalance from
      being sold on the next before its thesis has had a fortnight to work.

    Cadence and sizing:

    * ``rebalance_interval_sessions`` (10) — decide every fortnight rather than monthly. This is the
      knob that raises trade frequency; the sell band, not the calendar, sets turnover.
    * ``buy_budget_fraction`` (0.98) — the mechanical execution margin, as elsewhere.
    * ``sleeve`` — the journal sleeve every trade is tagged with.
    """

    top_n: int = 20
    sell_band: int = 60
    rebalance_interval_sessions: int = 10
    max_hold_sessions: int = 63
    min_hold_sessions: int = 5
    trailing_stop: Decimal | None = Decimal("0.25")
    resell_cooldown_sessions: int = 21
    exclude_vol_fraction: Decimal = Decimal("0.10")
    weight_high: Decimal = _ONE
    weight_delivery: Decimal = _ONE
    weight_momentum: Decimal = _ONE
    # M12.1: signed weights on the new legs, every one zero by default so the arm above this line
    # is bit-for-bit the M10.7 policy. A negative weight ranks a leg's reverse — that is how the
    # reversal family is expressed, rather than by storing a negated feature.
    weight_return_5: Decimal = _ZERO
    weight_momentum_1m: Decimal = _ZERO
    weight_delivery_trend: Decimal = _ZERO
    weight_turnover_expansion: Decimal = _ZERO
    weight_ma_proximity: Decimal = _ZERO
    weight_volatility: Decimal = _ZERO
    regime_filter: bool = False
    buy_budget_fraction: Decimal = Decimal("0.98")
    sleeve: Sleeve = Sleeve.TACTICAL

    def __post_init__(self) -> None:
        if self.top_n <= 0:
            raise ValueError(f"top_n must be positive, got {self.top_n}")
        if self.sell_band < self.top_n:
            raise ValueError(
                f"sell_band {self.sell_band} must be at least top_n {self.top_n} — an outer band "
                "narrower than the buy set would sell a name the same session it was bought"
            )
        for name in (
            "rebalance_interval_sessions",
            "max_hold_sessions",
            "resell_cooldown_sessions",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")
        if self.min_hold_sessions < 0:
            raise ValueError(f"min_hold_sessions must be >= 0, got {self.min_hold_sessions}")
        if self.min_hold_sessions >= self.max_hold_sessions:
            raise ValueError(
                f"min_hold_sessions {self.min_hold_sessions} must be below max_hold_sessions "
                f"{self.max_hold_sessions}"
            )
        for name in (
            "buy_budget_fraction",
            "exclude_vol_fraction",
            "weight_high",
            "weight_delivery",
            "weight_momentum",
            "weight_return_5",
            "weight_momentum_1m",
            "weight_delivery_trend",
            "weight_turnover_expansion",
            "weight_ma_proximity",
            "weight_volatility",
        ):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be a Decimal")
        if not (_ZERO < self.buy_budget_fraction <= _ONE):
            raise ValueError(
                f"buy_budget_fraction must be in (0, 1], got {self.buy_budget_fraction}"
            )
        if not (_ZERO <= self.exclude_vol_fraction < _ONE):
            raise ValueError(
                f"exclude_vol_fraction must be in [0, 1), got {self.exclude_vol_fraction}"
            )
        if self.trailing_stop is not None:
            if not isinstance(self.trailing_stop, Decimal):
                raise TypeError("trailing_stop must be a Decimal or None")
            if not (_ZERO < self.trailing_stop < _ONE):
                raise ValueError(f"trailing_stop must be in (0, 1), got {self.trailing_stop}")


@runtime_checkable
class SwingCompositeData(Protocol):
    """Where the policy reads its world — the injected seam the point-in-time context wraps.

    * ``is_rebalance(session)`` — is today a decision session? The interval is the data source's to
      apply, because it owns the trading calendar; the policy only asks.
    * ``signal(as_of)`` — the candidate set as a guardable :class:`~dataplatform.query.pit.Dataset`,
      already narrowed to the investable/liquid universe.
    * ``marks(as_of)`` — this session's raw closes for held names, so the trailing stop can be
      checked on *every* session rather than only on a rebalance. Returned as a dataset for the same
      reason: it is admitted through the guard like everything else.
    """

    def is_rebalance(self, session: date) -> bool:
        """Whether ``session`` is a decision session."""

    def signal(self, as_of: date) -> Dataset[SwingRecord]:
        """The PIT candidate set as of ``as_of``."""

    def marks(self, as_of: date) -> Dataset[SwingRecord]:
        """This session's raw closes, as records, for stop checking."""

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        """The broad-market regime as of ``as_of``, as a one-element guardable dataset (M12.1).

        Read only when ``regime_filter`` is on. The reading is momentum v2's
        :class:`~backtest.policies.momentum_v2.RegimeReading` unchanged, so both policies are gated
        by the same definition of risk-off and a difference between them is never the definition.
        """


def composite_scores(
    records: Sequence[SwingRecord], params: SwingCompositeParameters
) -> dict[str, Decimal]:
    """Rank-normalise each component across ``records`` and return the weighted composite per ISIN.

    Each component is converted to its cross-sectional rank scaled onto ``[-1, +1]``
    (``2 * rank / (n + 1) - 1``) rather than a z-score, because every one of these signals has fat
    tails — a name at a 400 % trailing return would otherwise dominate a z-scored basket. Ranks are
    struck with ties broken by ISIN, so the result is deterministic and a replay reproduces it.

    Assumes ``records`` is the already-screened candidate set. Never reads a clock or a store.
    """
    n = len(records)
    if n == 0:
        return {}
    scale = Decimal(n + 1)
    scores: dict[str, Decimal] = dict.fromkeys((r.isin for r in records), _ZERO)
    for attribute, weight in (
        ("high_proximity", params.weight_high),
        ("delivery_share", params.weight_delivery),
        ("momentum_12_1", params.weight_momentum),
        ("return_5", params.weight_return_5),
        ("momentum_1m", params.weight_momentum_1m),
        ("delivery_trend", params.weight_delivery_trend),
        ("turnover_expansion", params.weight_turnover_expansion),
        ("ma_proximity", params.weight_ma_proximity),
        ("volatility", params.weight_volatility),
    ):
        if weight == _ZERO:
            continue
        order = sorted(records, key=lambda r: (getattr(r, attribute), r.isin))
        for rank, record in enumerate(order, start=1):
            scores[record.isin] += weight * (_TWO * Decimal(rank) / scale - _ONE)
    return scores


@dataclass
class _Position:
    """What the policy remembers about a holding: age, running peak, and its last sell attempt.

    ``sell_staged_ago`` counts the sessions since a sell was last staged for this holding, or
    ``None`` if none has been. It exists because an exit is not guaranteed to execute: a name that
    stops printing — suspended, delisted, moved off the segment — has no reference bar for the
    broker to fill against, so the sell is rejected and the position stays on the book. Without
    this the exit rules would re-stage that same sell on every rebalance for the rest of the run,
    which is both operationally wrong (an unfillable order is not a new decision) and would report
    a handful of stuck names as hundreds of trades.
    """

    entered_on: date
    sessions_held: int = 0
    peak: Decimal = _ZERO
    sell_staged_ago: int | None = None


class SwingCompositePolicy:
    """Composite-signal swing trading: 7-90 day holds with stated entry and exit rules.

    Construct it with a :class:`SwingCompositeData` source and :class:`SwingCompositeParameters`. It
    satisfies :class:`backtest.replay.Policy`, so the same object runs under replay and live
    (invariant #5). Every session it ages its positions and checks the trailing stop; on a decision
    session it additionally re-scores the universe and applies the band and re-underwrite rules.
    """

    __slots__ = ("_data", "_params", "_positions")

    def __init__(
        self, data: SwingCompositeData, params: SwingCompositeParameters | None = None
    ) -> None:
        self._data = data
        self._params = params if params is not None else SwingCompositeParameters()
        self._positions: dict[str, _Position] = {}

    def decide(self, ctx: SessionContext) -> SessionDecision:
        """Age the book and check stops every session; re-score and rotate on a decision session."""
        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        marks = {
            record.isin: record.price for record in ctx.pit.admit(self._data.marks(ctx.session))
        }
        self._age(ctx.session, held, marks)

        stopped = self._stop_outs(held, marks)
        self._record_sell(stopped)
        if not self._data.is_rebalance(ctx.session):
            return self._session_decision(ctx, stopped, note="stop check only; no rebalance due")
        return self._rebalance(ctx, held, marks, stopped)

    # ── position bookkeeping ─────────────────────────────────────────────────────────────────────

    def _age(
        self, session: date, held: Mapping[str, Holding], marks: Mapping[str, Decimal]
    ) -> None:
        """Advance each holding's age and running peak; forget names no longer on the book."""
        for isin in list(self._positions):
            if isin not in held:
                del self._positions[isin]
        for isin in sorted(held):
            position = self._positions.get(isin)
            if position is None:
                position = _Position(entered_on=session)
                self._positions[isin] = position
            else:
                position.sessions_held += 1
                if position.sell_staged_ago is not None:
                    position.sell_staged_ago += 1
            mark = marks.get(isin)
            if mark is not None and mark > position.peak:
                position.peak = mark

    def _may_sell(self, isin: str) -> bool:
        """Whether a sell may be staged for ``isin`` now — i.e. no recent attempt is outstanding.

        A holding still on the book ``resell_cooldown_sessions`` after its last staged sell has had
        that sell rejected (a filled one would have removed it), so the exit is retried; inside the
        cooldown it is not re-staged.
        """
        position = self._positions.get(isin)
        if position is None or position.sell_staged_ago is None:
            return True
        return position.sell_staged_ago >= self._params.resell_cooldown_sessions

    def _record_sell(self, orders: Sequence[tuple[OrderRequest, str]]) -> None:
        """Stamp each staged sell on its position, so a rejected one is not re-staged at once."""
        for order, _ in orders:
            position = self._positions.get(order.isin)
            if position is not None:
                position.sell_staged_ago = 0

    def _stop_outs(
        self, held: Mapping[str, Holding], marks: Mapping[str, Decimal]
    ) -> list[tuple[OrderRequest, str]]:
        """Full-quantity sells for holdings whose close has fallen past the trailing stop."""
        stop = self._params.trailing_stop
        if stop is None:
            return []
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            position = self._positions.get(isin)
            mark = marks.get(isin)
            if position is None or mark is None or position.peak <= _ZERO:
                continue
            if not self._may_sell(isin):
                continue
            trigger = position.peak * (_ONE - stop)
            if mark <= trigger:
                quantity = held[isin].quantity
                sells.append(
                    (
                        OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="SWING"),
                        f"trailing stop: {mark} is {stop:%} or more below the peak {position.peak} "
                        f"since entry on {position.entered_on.isoformat()}; selling {quantity}",
                    )
                )
        return sells

    # ── the decision session ─────────────────────────────────────────────────────────────────────

    def _rebalance(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        marks: Mapping[str, Decimal],
        stopped: Sequence[tuple[OrderRequest, str]],
    ) -> SessionDecision:
        """Score the universe, apply the three exit rules, then size buys toward the target set."""
        candidates = tuple(ctx.pit.admit(self._data.signal(ctx.session)))
        # Score the *whole* candidate set, so a holding is ranked among everything that is
        # scoreable. The volatility screen then narrows only what may be *bought*: screening before
        # scoring would leave a holding that turned volatile unranked, and the exit rules would read
        # that as "gone from the universe" and liquidate it on a risk screen it was never sold on.
        scores = composite_scores(candidates, self._params)
        by_isin = {record.isin: record for record in candidates}
        # Descending score, ties by ISIN — the rank a holding is judged against.
        ranked = sorted(candidates, key=lambda r: (-scores[r.isin], r.isin))
        rank_of = {record.isin: rank for rank, record in enumerate(ranked, start=1)}
        buyable = {record.isin for record in self._screen(ranked)}
        chosen = [record for record in ranked if record.isin in buyable][: self._params.top_n]

        already_selling = {order.isin for order, _ in stopped}
        rule_sells = self._rule_sells(held, rank_of, already_selling)
        self._record_sell(rule_sells)
        sells = list(stopped) + rule_sells
        exiting = {order.isin for order, _ in sells}
        target = {record.isin: record for record in chosen if record.isin not in exiting}
        # M12.1's regime gate suppresses *buys* only. Every exit above this line has already been
        # staged, which is the whole point: risk-off must never trap a position the band, the
        # re-underwrite or the stop has decided to sell. With no buys the book runs down through its
        # own exits rather than being liquidated on the gate.
        if self._params.regime_filter and not self._risk_on(ctx):
            target = {}
        buys, drift = self._buys(ctx, held, target)

        orders = tuple(order for order, _ in (*sells, *buys))
        entries = tuple(self._entry(ctx, order, note) for order, note in (*sells, *buys))
        evidence = self._evidence(ctx.session, chosen, scores, by_isin, drift, len(candidates))
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _risk_on(self, ctx: SessionContext) -> bool:
        """Whether the broad market sits at or above its own trailing mean (M12.1).

        Assumes the data source serves exactly one reading for the session, admitted through the
        same point-in-time guard as every other read. A session with no reading at all is treated
        as risk-**off**: an absent regime is not a licence to buy, and silently defaulting to
        risk-on would make the gate disappear on exactly the sessions the store is thinnest.
        """
        readings = tuple(ctx.pit.admit(self._data.regime(ctx.session)))
        if not readings:
            return False
        return readings[0].risk_on

    def _screen(self, candidates: Sequence[SwingRecord]) -> tuple[SwingRecord, ...]:
        """The names eligible to be *bought*: all but the most volatile ``exclude_vol_fraction``."""
        fraction = self._params.exclude_vol_fraction
        if fraction == _ZERO or not candidates:
            return tuple(candidates)
        keep = len(candidates) - int(len(candidates) * fraction)
        if keep <= 0:
            return ()
        # Ascending volatility, ties by ISIN: the calmest `keep` names survive, deterministically.
        by_vol = sorted(candidates, key=lambda r: (r.volatility, r.isin))
        return tuple(by_vol[:keep])

    def _rule_sells(
        self,
        held: Mapping[str, Holding],
        rank_of: Mapping[str, int],
        already_selling: set[str],
    ) -> list[tuple[OrderRequest, str]]:
        """Band and re-underwrite exits, in ISIN order. Names inside ``min_hold`` are carried."""
        params = self._params
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            if isin in already_selling:
                continue
            position = self._positions.get(isin)
            age = position.sessions_held if position is not None else 0
            if age < params.min_hold_sessions or not self._may_sell(isin):
                continue
            # A name that has dropped out of the scored universe entirely (delisted from the liquid
            # set, or no longer computable) ranks worse than any surviving name.
            rank = rank_of.get(isin)
            quantity = held[isin].quantity
            if rank is None:
                reason = f"no longer in the scored universe; selling {quantity} shares"
            elif rank > params.sell_band:
                reason = (
                    f"composite rank {rank} left the top-{params.sell_band} band "
                    f"(bought into top-{params.top_n}); selling {quantity} shares"
                )
            elif age >= params.max_hold_sessions and rank > params.top_n:
                reason = (
                    f"held {age} sessions (max {params.max_hold_sessions}) and rank {rank} no "
                    f"longer re-qualifies for the top-{params.top_n}; selling {quantity} shares"
                )
            else:
                continue
            sells.append(
                (OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="SWING"), reason)
            )
        return sells

    def _buys(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        target: Mapping[str, SwingRecord],
    ) -> tuple[list[tuple[OrderRequest, str]], Decimal]:
        """Whole-share buys toward equal weight over the target set, sized from currently-free cash.

        Budget is the cash *already* free times ``buy_budget_fraction`` — never this session's sale
        proceeds, which have not settled — so a buy is never rejected for cash it does not yet hold.
        """
        if not target:
            return [], _ZERO
        budget = ctx.broker.margins().available * self._params.buy_budget_fraction
        prices = {isin: record.price for isin, record in target.items()}
        weights = _equal_weights(sorted(target))
        existing_value = {
            isin: Decimal(held[isin].quantity) * prices[isin] for isin in target if isin in held
        }
        allocation = simulate_sip_instalment(
            instalment=budget, targets=weights, prices=prices, existing_value=existing_value
        )
        buys = [
            (
                order.to_order_request(exchange=Exchange.NSE, tag="SWING"),
                f"composite top-{self._params.top_n}: 52w-high proximity "
                f"{target[order.isin].high_proximity}, delivery "
                f"{target[order.isin].delivery_share}, 12-1 "
                f"{target[order.isin].momentum_12_1:+}; buy {order.quantity} @ {order.price}",
            )
            for order in allocation.orders
        ]
        return buys, allocation.tracking_drift

    # ── journal + evidence ───────────────────────────────────────────────────────────────────────

    def _session_decision(
        self, ctx: SessionContext, sells: Sequence[tuple[OrderRequest, str]], *, note: str
    ) -> SessionDecision:
        """A non-rebalance session: the heartbeat, plus any stop-outs it fired."""
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="held_names",
                    as_of=ctx.session,
                    value=Decimal(len(self._positions)),
                    text=f"{note}; {len(sells)} trailing-stop exit(s)",
                ),
            ),
        )
        orders = tuple(order for order, _ in sells)
        entries = tuple(self._entry(ctx, order, note) for order, note in sells)
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _entry(self, ctx: SessionContext, order: OrderRequest, rationale: str) -> JournalEntry:
        """A BUY/SELL journal entry for one order — rationale, sleeve and ISIN, per §0/§5.7."""
        decision = Decision.BUY if order.side is Side.BUY else Decision.SELL
        return JournalEntry(
            ts=ctx.clock.now(),
            trading_date=ctx.session,
            actor=Actor.T0,
            decision=decision,
            isin=order.isin,
            sleeve=self._params.sleeve,
            rationale=rationale,
        )

    def _evidence(
        self,
        session: date,
        chosen: Sequence[SwingRecord],
        scores: Mapping[str, Decimal],
        by_isin: Mapping[str, SwingRecord],
        tracking_drift: Decimal,
        universe_size: int,
    ) -> EvidenceBundle:
        """The scored table the decision was made on, as one content-addressed bundle."""
        items = [
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="L1+L2",
                label="swing_composite",
                isin=record.isin,
                as_of=session,
                value=scores[record.isin],
                detail={
                    "rank": str(rank),
                    "high_proximity": str(record.high_proximity),
                    "delivery_share": str(record.delivery_share),
                    "momentum_12_1": str(record.momentum_12_1),
                    "price": str(record.price),
                },
            )
            for rank, record in enumerate(chosen, start=1)
        ]
        items.append(
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="book",
                label="tracking_drift",
                as_of=session,
                value=tracking_drift,
                text=f"equal-weight top-{self._params.top_n} of {universe_size} scored candidates",
            )
        )
        return EvidenceBundle(trading_date=session, actor=Actor.T0, items=tuple(items))


def _equal_weights(isins: Sequence[str]) -> dict[str, Decimal]:
    """Equal weights over ``isins`` that sum to exactly 1 — the remainder lands on the last name."""
    if not isins:
        raise ValueError("cannot weight an empty basket")
    n = len(isins)
    each = (_ONE / Decimal(n)).quantize(_WEIGHT_QUANTUM)
    weights = dict.fromkeys(isins[:-1], each)
    weights[isins[-1]] = _ONE - each * (n - 1)
    return weights
