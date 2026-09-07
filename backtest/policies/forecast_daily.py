"""A daily-decision policy driven by a fitted forward-return forecast (EXECUTION_PLAN §7, X2).

Every other policy here decides on a calendar: the first session of a month, or every tenth session.
Nothing in the engine requires that — `ReplayEngine` calls `decide` on *every* session and the
policy decides what a decision is — so the monthly cadence was always a stated choice rather than a
constraint. This policy decides every session. What stops that being ruinous is not the cadence but
the *turnover*: a round trip costs about 0.29 % of position value (0.2542 % statutory off
`execution/costs/rates.yaml`, plus roughly 2 bps of slippage each way), so replacing a twenty-name
book daily would pay about 75 % of capital a year in friction against a monthly book's ~3.5 %.

The design follows from that arithmetic, and it is the whole idea here:

* **Daily evaluation, monthly turnover.** ``max_trades_per_session`` (2) is set so the annual fill
  count lands near what monthly rebalancing already spends — the measured decade runs fired ~293
  fills a year — so this arm differs from a monthly one in *when* it acts, not in how much it
  trades. Anything the budget cannot afford this session is simply not done; there is no queue and
  no catch-up, because a trade worth doing tomorrow is worth re-deriving tomorrow.
* **A trade must clear the friction it costs.** The forecast is an expected return over
  ``horizon`` sessions in return units, so it can be compared against a number. A name is bought
  only when its projection exceeds ``entry_cost_multiple`` times ``round_trip_cost``, and released
  when it falls below ``exit_cost_multiple`` times it — a band, so a name hovering at the bar is
  carried rather than churned. ``round_trip_cost`` is *not* a cost model: it is the stated bar a
  projection must clear, and no fill is priced by it (the broker owns the one shared model —
  invariants #4/#5).
* **Exit on the projection, not on a profit.** A holding is sold when the model stops expecting a
  return from it, when it reaches ``max_hold_sessions``, or when the trailing stop fires. There is
  deliberately **no profit target**: truncating winners is how a momentum-shaped payoff is
  destroyed, and taking a profit is not a reason to sell a name the model still ranks. That is the
  one instinct behind "rotate into near-term profits" that the shape of the payoff rejects.
* **Stops are exempt from the budget.** A risk rule a turnover budget can cancel is not a risk
  rule. Everything else is budgeted, sells before buys — reducing a position the model has turned
  against comes before opening a new one.

Cash discipline is the same as elsewhere: buys are sized from *currently free* cash, never from
proceeds of sells staged this session, which fill T+1 and have not settled. So the book fills over
the first sessions and thereafter buys are naturally rare, which is a feature — the budget is spent
where the model has changed its mind, not on rebuilding a basket that is already right.

Point-in-time (invariant #7): the policy reads candidates only through ``ctx.pit.admit``, so a
record whose forecast was not knowable on the session trips the guard rather than leaking. The
forecast itself is fitted only on pairs whose target window closed on or before the session
(:mod:`backtest.forecast`), which is where the real look-ahead risk in a fitted model lives. The
trailing stop reads only the current session's close.

What it never does: read a wall clock (``ctx.clock``), key on a symbol (ISIN only — invariant #2),
hold a cost model, or reach data outside the point-in-time context. Given the same inputs it makes
the same decision, which is what the replay determinism requirement rests on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from backtest.forecast import HORIZON_3M
from backtest.replay import SessionContext, SessionDecision
from backtest.sip import simulate_sip_instalment
from dataplatform.query.pit import Dataset
from execution.broker import Exchange, Holding, OrderRequest, Side

__all__ = [
    "ForecastDailyParameters",
    "ForecastDailyPolicy",
    "ForecastRecord",
    "ForecastSignalData",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_TAG = "FORECAST"


@dataclass(frozen=True, slots=True)
class ForecastRecord:
    """One candidate this session: what the model expects of it, and what it costs to buy.

    ``expected_return`` is the fitted projection over the model's horizon as a plain ratio —
    ``Decimal("0.031")`` is +3.1 % expected over the next ``horizon`` sessions. It arrives as a
    ``Decimal`` because from here on it is compared against money: a cost bar in the same units, and
    a price that sizes whole shares. The fit itself is float linear algebra
    (:mod:`backtest.forecast`); this field is the seam where the statistic becomes a decision input.

    ``price`` is the current *raw* close — the price that actually traded, which is what sizes an
    order (invariant #3: an adjusted series is for analysis, never execution). ``knowable_date`` is
    the date on which everything on this record became knowable, and the PIT guard checks it against
    the session.
    """

    isin: str
    expected_return: Decimal
    price: Decimal
    knowable_date: date

    def __post_init__(self) -> None:
        for name in ("expected_return", "price"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be a Decimal — money and its bars are never float")
        if self.price <= _ZERO:
            raise ValueError(f"price must be positive, got {self.price}")


@runtime_checkable
class ForecastSignalData(Protocol):
    """What the policy needs of its data source: this session's candidates and their marks.

    ``signal`` returns the admitted-through-PIT candidate set for a session — the names the model
    has a view on. An empty dataset is a real answer and the policy treats it as one: early in a
    window the expanding fit has too few matured pairs to produce a model at all, and a policy with
    no view holds what it has rather than acting.

    ``marks`` returns the current close for every name that printed, used to age the trailing stop
    against a real price rather than the last price the policy happened to see.
    """

    def signal(self, as_of: date) -> Dataset[ForecastRecord]:
        """The candidate set knowable on ``as_of``."""

    def marks(self, as_of: date) -> Mapping[str, Decimal]:
        """Raw closes on ``as_of`` for marking and stop checks."""


@dataclass(frozen=True, slots=True)
class ForecastDailyParameters:
    """The knobs — every one a stated a-priori choice, none fitted to a result.

    * ``top_n`` (20) — names held. The same basket size as every other policy here, so a
      comparison against them isolates the cadence and the signal rather than the concentration.
    * ``horizon`` (63 sessions, ~3 months) — the forecast horizon the projection is over, and
      therefore the horizon the entry bar is expressed in. The upper end of "one to three months".
    * ``round_trip_cost`` (0.003) — the friction a trade must earn back, as a fraction of position
      value: 0.2542 % statutory on a ₹50,000 leg pair under the current rate card plus about 4 bps
      of modelled slippage, rounded up to a round 30 bps. A bar, not a cost model.
    * ``entry_cost_multiple`` (2) — a new position must project at least twice its round trip.
      Two, because one would mean opening a position expected to break even after friction.
    * ``exit_cost_multiple`` (0) — a holding is released once its projection falls below zero
      expected return. Lower than the entry bar on purpose: the gap between the two is the
      hysteresis band that stops a name at the bar being bought and sold repeatedly.
    * ``max_trades_per_session`` (2) — the turnover budget, and the parameter that makes daily
      decisions affordable. Two fills a session is ~500 a year, the neighbourhood monthly
      rebalancing already spends, so cadence is varied while turnover is held roughly fixed.
    * ``min_hold_sessions`` (5) — nothing but the stop may sell inside a week, so a name entered
      on one session cannot be discarded on the next before its thesis has had any time.
    * ``max_hold_sessions`` (63) — at this age a holding must still clear the *entry* bar to be
      carried; otherwise it is released. A re-underwrite, not a forced liquidation, so a name the
      model still likes is not sold to be bought back.
    * ``trailing_stop`` (0.25) — sell when the close falls 25 % below the position's highest close
      since entry, checked every session. Wide by design: a momentum-shaped name's ordinary path
      passes through a large drawdown, and a tight stop turns that path into a realised loss.
      ``None`` disables it.
    * ``buy_budget_fraction`` (0.98) — the mechanical execution margin, as in every other policy.
    * ``sleeve`` — the journal sleeve every trade is tagged with.
    """

    top_n: int = 20
    horizon: int = HORIZON_3M
    round_trip_cost: Decimal = Decimal("0.003")
    entry_cost_multiple: Decimal = Decimal("2")
    exit_cost_multiple: Decimal = Decimal("0")
    max_trades_per_session: int = 2
    min_hold_sessions: int = 5
    max_hold_sessions: int = 63
    trailing_stop: Decimal | None = Decimal("0.25")
    buy_budget_fraction: Decimal = Decimal("0.98")
    sleeve: Sleeve = Sleeve.TACTICAL

    def __post_init__(self) -> None:
        if self.top_n <= 0:
            raise ValueError(f"top_n must be positive, got {self.top_n}")
        if self.horizon <= 0:
            raise ValueError(f"horizon must be positive, got {self.horizon}")
        if self.max_trades_per_session <= 0:
            raise ValueError(
                f"max_trades_per_session must be positive, got {self.max_trades_per_session}"
            )
        if self.min_hold_sessions < 0:
            raise ValueError(f"min_hold_sessions must be >= 0, got {self.min_hold_sessions}")
        if self.max_hold_sessions < self.min_hold_sessions:
            raise ValueError(
                f"max_hold_sessions must be >= min_hold_sessions: got "
                f"{self.max_hold_sessions} < {self.min_hold_sessions}"
            )
        for name in ("round_trip_cost", "entry_cost_multiple", "exit_cost_multiple"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be a Decimal — a bar on money is never float")
        if self.round_trip_cost < _ZERO:
            raise ValueError(f"round_trip_cost must be >= 0, got {self.round_trip_cost}")
        if self.exit_cost_multiple > self.entry_cost_multiple:
            raise ValueError(
                f"exit_cost_multiple must be <= entry_cost_multiple (the release bar cannot be "
                f"tighter than the entry bar): got {self.exit_cost_multiple} > "
                f"{self.entry_cost_multiple}"
            )
        if self.trailing_stop is not None:
            if not isinstance(self.trailing_stop, Decimal):
                raise TypeError("trailing_stop must be a Decimal")
            if not (_ZERO < self.trailing_stop < _ONE):
                raise ValueError(f"trailing_stop must be in (0, 1), got {self.trailing_stop}")
        if not isinstance(self.buy_budget_fraction, Decimal):
            raise TypeError("buy_budget_fraction must be a Decimal")
        if not (_ZERO < self.buy_budget_fraction <= _ONE):
            raise ValueError(
                f"buy_budget_fraction must be in (0, 1], got {self.buy_budget_fraction}"
            )

    @property
    def entry_bar(self) -> Decimal:
        """The expected return a new position must project over the horizon."""
        return self.round_trip_cost * self.entry_cost_multiple

    @property
    def exit_bar(self) -> Decimal:
        """The expected return below which a holding is released."""
        return self.round_trip_cost * self.exit_cost_multiple


@dataclass(frozen=True, slots=True)
class _Action:
    """One candidate action this session, with the reason that will be journalled."""

    isin: str
    side: Side
    quantity: int
    edge: Decimal
    reason: str
    exempt: bool = False


class ForecastDailyPolicy:
    """Decide every session; trade only what the projection justifies and the budget allows.

    Carries per-position state — the session a holding was entered and the highest close seen since
    — because the age rules and the trailing stop need it and the book itself does not record it.
    The state is keyed by ISIN, seeded the first session a holding is seen, and dropped when the
    holding goes, so a replay from a fixed start reproduces it exactly.
    """

    def __init__(self, data: ForecastSignalData, parameters: ForecastDailyParameters) -> None:
        self._data = data
        self._params = parameters
        self._entered: dict[str, date] = {}
        self._peak: dict[str, Decimal] = {}

    def decide(self, ctx: SessionContext) -> SessionDecision:
        """Age the book, check stops, re-score, and spend the session's turnover budget."""
        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        marks = self._data.marks(ctx.session)
        self._age(ctx.session, held, marks)

        candidates = tuple(ctx.pit.admit(self._data.signal(ctx.session)))
        by_isin = {record.isin: record for record in candidates}

        actions = self._sell_actions(ctx.session, held, by_isin, marks, has_view=bool(candidates))
        budget = self._params.max_trades_per_session - sum(1 for a in actions if not a.exempt)
        selling = {action.isin for action in actions}
        buys = self._buy_actions(ctx, held, by_isin, selling, budget, self._held_value(held, marks))

        orders = tuple(
            OrderRequest(
                isin=a.isin,
                side=a.side,
                quantity=a.quantity,
                exchange=Exchange.NSE,
                tag=_TAG,
            )
            for a in (*actions, *buys)
        )
        entries = tuple(self._entry(ctx, a) for a in (*actions, *buys))
        evidence = self._evidence(ctx.session, candidates, held, actions, buys)
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    @staticmethod
    def _held_value(held: Mapping[str, Holding], marks: Mapping[str, Decimal]) -> Decimal:
        """The book's marked value, from the marks available this session.

        A holding with no mark today is carried at its average price rather than dropped: leaving it
        out would understate the book and so overstate the target weight of the next buy, which is
        the direction that concentrates. Names that did not print are rare and small either way.
        """
        total = _ZERO
        for isin, holding in held.items():
            price = marks.get(isin, holding.average_price)
            total += Decimal(holding.quantity) * price
        return total

    # ── per-position state ───────────────────────────────────────────────────────────────────────

    def _age(
        self, session: date, held: Mapping[str, Holding], marks: Mapping[str, Decimal]
    ) -> None:
        """Seed state for a new holding, carry the peak forward, drop what is no longer held.

        A holding first seen this session is stamped with this session as its entry, which is the
        conservative reading: it makes the name as young as possible, so the ``min_hold`` rule can
        only ever protect it for longer than the truth, never sell it earlier.
        """
        for isin in sorted(held):
            if isin not in self._entered:
                self._entered[isin] = session
            mark = marks.get(isin)
            if mark is not None:
                previous = self._peak.get(isin)
                if previous is None or mark > previous:
                    self._peak[isin] = mark
        for isin in [isin for isin in self._entered if isin not in held]:
            del self._entered[isin]
            self._peak.pop(isin, None)

    def _age_sessions(self, session: date, isin: str) -> int:
        """Calendar days held, as a stand-in for sessions — monotone and enough for the age rules.

        The policy sees only the sessions it is driven with, so counting them would need a calendar
        it does not hold. Calendar days are monotone in sessions, and both age rules are round
        a-priori numbers rather than precise counts, so the substitution is stated rather than
        hidden: ``min_hold_sessions=5`` behaves as "about a week".
        """
        entered = self._entered.get(isin)
        return 0 if entered is None else (session - entered).days

    # ── sells ────────────────────────────────────────────────────────────────────────────────────

    def _sell_actions(
        self,
        session: date,
        held: Mapping[str, Holding],
        by_isin: Mapping[str, ForecastRecord],
        marks: Mapping[str, Decimal],
        *,
        has_view: bool,
    ) -> tuple[_Action, ...]:
        """Stops (budget-exempt), then age and projection releases, worst projection first.

        ``has_view`` separates the two very different reasons a holding might have no record this
        session. If the model produced nothing at all — the expanding window has not matured, so
        *every* name is absent — the book is held as it stands, because "no opinion yet" is not a
        reason to liquidate. If the model produced a cross-section that this name is simply not in,
        the name has left the investable universe and is released. Only the trailing stop acts in
        either case.
        """
        stops: list[_Action] = []
        releases: list[_Action] = []
        for isin in sorted(held):
            quantity = held[isin].quantity
            if quantity <= 0:
                continue
            stop_reason = self._stop_reason(isin, marks)
            if stop_reason is not None:
                stops.append(
                    _Action(
                        isin=isin,
                        side=Side.SELL,
                        quantity=quantity,
                        edge=_ZERO,
                        reason=stop_reason,
                        exempt=True,
                    )
                )
                continue
            age = self._age_sessions(session, isin)
            if age < self._params.min_hold_sessions:
                continue  # nothing but the stop sells inside the minimum hold
            record = by_isin.get(isin)
            if record is None:
                if not has_view:
                    continue  # the model has no view at all yet; hold, do not liquidate
                releases.append(
                    _Action(
                        isin=isin,
                        side=Side.SELL,
                        quantity=quantity,
                        edge=_ZERO,
                        reason=(
                            "the name is not in this session's forecast cross-section (it left the "
                            "investable universe); releasing the position"
                        ),
                    )
                )
                continue
            edge = record.expected_return
            if age >= self._params.max_hold_sessions:
                if edge < self._params.entry_bar:
                    releases.append(
                        _Action(
                            isin=isin,
                            side=Side.SELL,
                            quantity=quantity,
                            edge=edge,
                            reason=(
                                f"held {age}d, re-underwrite failed: projected {edge:+} over "
                                f"{self._params.horizon} sessions is under the entry bar "
                                f"{self._params.entry_bar:+}"
                            ),
                        )
                    )
                continue
            if edge < self._params.exit_bar:
                releases.append(
                    _Action(
                        isin=isin,
                        side=Side.SELL,
                        quantity=quantity,
                        edge=edge,
                        reason=(
                            f"projected {edge:+} over {self._params.horizon} sessions is under the "
                            f"release bar {self._params.exit_bar:+}"
                        ),
                    )
                )
        releases.sort(key=lambda a: (a.edge, a.isin))
        budgeted = releases[: max(0, self._params.max_trades_per_session)]
        return (*stops, *budgeted)

    def _stop_reason(self, isin: str, marks: Mapping[str, Decimal]) -> str | None:
        """Why the trailing stop fired for `isin` this session, or ``None`` if it did not."""
        stop = self._params.trailing_stop
        if stop is None:
            return None
        peak = self._peak.get(isin)
        mark = marks.get(isin)
        if peak is None or mark is None or peak <= _ZERO:
            return None
        drawdown = _ONE - (mark / peak)
        if drawdown < stop:
            return None
        return (
            f"trailing stop: close {mark} is {drawdown:.1%} below the peak {peak} since entry "
            f"(stop {stop:.0%})"
        )

    # ── buys ─────────────────────────────────────────────────────────────────────────────────────

    def _buy_actions(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        by_isin: Mapping[str, ForecastRecord],
        selling: frozenset[str] | set[str],
        budget: int,
        held_value: Decimal,
    ) -> tuple[_Action, ...]:
        """Whole-share buys into the best-projected names the budget and free cash allow.

        **Each new name is sized to one ``top_n``-th of the whole book**, not to a share of the cash
        that happens to be free today. With a turnover budget of two fills a session, spending all
        free cash on the day's two picks would build a two-name portfolio at ~50 % each and call it
        a twenty-name one — the returns of a concentrated book reported as a diversified strategy's.
        So the instalment is ``min(free cash x margin, per-name target x names chosen)`` where the
        target is ``(free cash + marked holdings) / top_n``, and the book fills toward ``top_n``
        over the sessions the budget allows rather than in one session.

        Sized from currently *free* cash — sells staged this session fill T+1 and their proceeds
        have not settled, so a buy never depends on them. Room is what the book has left toward
        ``top_n`` after this session's releases.
        """
        if budget <= 0:
            return ()
        room = self._params.top_n - (len(held) - len(selling))
        if room <= 0:
            return ()
        wanted = [
            record
            for record in by_isin.values()
            if record.isin not in held
            and record.isin not in selling
            and record.expected_return >= self._params.entry_bar
        ]
        wanted.sort(key=lambda r: (-r.expected_return, r.isin))
        chosen = wanted[: min(room, budget)]
        if not chosen:
            return ()
        free = ctx.broker.margins().available
        deployable = free * self._params.buy_budget_fraction
        if deployable <= _ZERO:
            return ()
        per_name = (free + held_value) / Decimal(self._params.top_n)
        budget_cash = min(deployable, per_name * Decimal(len(chosen)))
        if budget_cash <= _ZERO:
            return ()
        weights = _equal_weights([record.isin for record in chosen])
        prices = {record.isin: record.price for record in chosen}
        allocation = simulate_sip_instalment(
            instalment=budget_cash, targets=weights, prices=prices, existing_value={}
        )
        return tuple(
            _Action(
                isin=order.isin,
                side=Side.BUY,
                quantity=order.quantity,
                edge=by_isin[order.isin].expected_return,
                reason=(
                    f"projected {by_isin[order.isin].expected_return:+} over "
                    f"{self._params.horizon} sessions clears the entry bar "
                    f"{self._params.entry_bar:+}; buy {order.quantity} @ {order.price}"
                ),
            )
            for order in allocation.orders
        )

    # ── journal + evidence ───────────────────────────────────────────────────────────────────────

    def _entry(self, ctx: SessionContext, action: _Action) -> JournalEntry:
        """A BUY/SELL journal entry for one action — its rationale, sleeve and ISIN."""
        decision = Decision.BUY if action.side is Side.BUY else Decision.SELL
        return JournalEntry(
            ts=ctx.clock.now(),
            trading_date=ctx.session,
            actor=Actor.T0,
            decision=decision,
            isin=action.isin,
            sleeve=self._params.sleeve,
            rationale=action.reason,
        )

    def _evidence(
        self,
        session: date,
        candidates: Sequence[ForecastRecord],
        held: Mapping[str, Holding],
        sells: Sequence[_Action],
        buys: Sequence[_Action],
    ) -> EvidenceBundle:
        """This session's evidence — including the sessions on which nothing was done.

        A no-order session is a journalled decision and not a missing row (invariant #9), and on a
        daily policy most sessions are exactly that: the model was consulted, nothing cleared its
        bar or the budget was better spent later.
        """
        best = max((record.expected_return for record in candidates), default=_ZERO)
        items = [
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="book",
                label="held_names",
                value=Decimal(len(held)),
                text=(
                    f"{len(candidates)} forecast candidates; {len(sells)} released, "
                    f"{len(buys)} opened"
                    if (sells or buys)
                    else f"{len(candidates)} forecast candidates; nothing cleared the bar"
                ),
            ),
            EvidenceItem(
                # PRICE, not a kind of its own: the projection's provenance *is* the platform's own
                # L1/L2 — trailing returns, realised volatility, turnover and delivery share, with
                # one point-in-time fundamental — so the evidence pack's "what was in front of the
                # agent" slicing stays honest. The text says it is a fitted number, not a print.
                kind=EvidenceKind.PRICE,
                source="forecast",
                label="best_projected_return",
                value=best,
                text=(
                    f"best fitted projection over {self._params.horizon} sessions from L1/L2 "
                    f"features, against an entry bar of {self._params.entry_bar:+}"
                ),
            ),
        ]
        return EvidenceBundle(trading_date=session, actor=Actor.T0, items=tuple(items))


def _equal_weights(isins: Sequence[str]) -> dict[str, Decimal]:
    """Equal target weights over `isins`, summing to exactly one with the remainder on the last."""
    count = len(isins)
    if count == 0:
        return {}
    share = (_ONE / Decimal(count)).quantize(Decimal("0.00000001"))
    weights = dict.fromkeys(isins, share)
    weights[isins[-1]] = _ONE - share * Decimal(count - 1)
    return weights
