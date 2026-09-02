"""M4.10 — a naive top-N momentum policy, for engine validation (EXECUTION_PLAN §7, X2).

A deliberately simple cross-sectional momentum strategy: on the first trading session of each
month, rank the point-in-time universe by trailing 12-month total return, hold the top ``N`` in
equal weight, and let everything else go. It exists to *validate the engine*, not to make money —
the point of M4.10 is to prove that the PIT universe (D4), the one shared cost model (invariant #4),
whole-share SIP allocation (M4.7), portfolio accounting (M4.6) and journaling (invariant #9) all
survive a full ten-year replay. **No parameter here is tuned.** They are the round, obvious choices
a person would reach for first (N = 20, monthly, equal weight, 12-month look-back), chosen once and
stated, because a tuned toy strategy is a lie the engine will tell you later.

How it behaves each session:

* **Not a rebalance session** — it returns a bare evidence bundle and no orders, so the engine
  writes the one heartbeat per session invariant #9 requires. "Checked, nothing due today" is a
  journalled decision, not a missing row.
* **A rebalance session** — it reads its candidates *only* through the injected point-in-time
  context (``ctx.pit.admit``), so a datum not yet knowable on the session date trips the guard
  rather than leaking (invariant #7). It ranks them by trailing return, takes the top ``N``, then:
  sells — in full — every settled holding that has dropped out of the target set, and allocates the
  cash currently free (less a small execution buffer) across the target names as whole shares,
  through the same M4.7 allocator the SIP tests cover. Sells free their cash for the next rebalance;
  the strategy never spends cash it does not yet hold, so it never depends on same-session sale
  proceeds.

What it never does: read a wall clock (timestamps come from ``ctx.clock``), key on a symbol
(everything is ISIN — invariant #2), hold a second cost model (the broker it hands orders to owns
the one shared model), or reach data outside the point-in-time context. Given the same inputs it
returns the same decision — the determinism the replay harness (§8.3.3) is built on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from backtest.replay import SessionContext, SessionDecision
from backtest.sip import simulate_sip_instalment
from dataplatform.query.pit import Dataset
from execution.broker import Exchange, Holding, OrderRequest, Side

__all__ = [
    "MomentumData",
    "MomentumParameters",
    "MomentumRecord",
    "NaiveMomentumPolicy",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class MomentumRecord:
    """One candidate the policy may hold this session: its ranking signal and its current price.

    What it carries: the ``isin`` (the only identity — invariant #2), its ``momentum`` (the trailing
    total return over the look-back window, as a plain ratio — ``0.25`` is +25 %), the current
    adjusted ``price`` the allocation and valuation use, and ``knowable_date`` — the date on which
    both figures became knowable (the session's own close, for an EOD close). The point-in-time
    guard checks ``knowable_date`` against the session's ``as_of`` (invariant #7), so a record dated
    into the future is refused rather than silently used.

    What it never does: hold a ``float`` (``momentum`` and ``price`` are ``Decimal``), or a
    non-positive price (an unpriceable name cannot be sized into whole shares).
    """

    isin: str
    momentum: Decimal
    price: Decimal
    knowable_date: date

    def __post_init__(self) -> None:
        for name in ("momentum", "price"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(
                    f"{name} must be a Decimal — money/signal is never float (CLAUDE.md)"
                )
        if self.price <= _ZERO:
            raise ValueError(f"price must be positive, got {self.price}")


@dataclass(frozen=True, slots=True)
class MomentumParameters:
    """The strategy's a-priori knobs — chosen once, stated, never tuned (M4.10 acceptance #3).

    * ``top_n`` — how many names to hold, equal-weighted. 20 is the round default.
    * ``buy_budget_fraction`` — the share of currently-free cash a rebalance deploys. Held a little
      under 1 so the next session's open plus slippage and charges still fits the cash the buy was
      sized against; it is a mechanical execution margin, **not** a return-tuning parameter.
    * ``sleeve`` — the journal sleeve every trade is tagged with (§5.5). A backtest momentum book is
      the rotation/tactical sleeve.

    All fixed by construction; the run reports them verbatim so the "no tuning" claim is checkable.
    """

    top_n: int = 20
    buy_budget_fraction: Decimal = Decimal("0.98")
    sleeve: Sleeve = Sleeve.TACTICAL

    def __post_init__(self) -> None:
        if self.top_n <= 0:
            raise ValueError(f"top_n must be positive, got {self.top_n}")
        if not isinstance(self.buy_budget_fraction, Decimal):
            raise TypeError("buy_budget_fraction must be a Decimal")
        if not (_ZERO < self.buy_budget_fraction <= _ONE):
            raise ValueError(
                f"buy_budget_fraction must be in (0, 1], got {self.buy_budget_fraction}"
            )


@runtime_checkable
class MomentumData(Protocol):
    """Where the policy reads its world — the injected seam the point-in-time context wraps.

    Two questions per session, both answered as of the session date and nothing later:

    * ``is_rebalance(session)`` — is today a rebalance session (the first trading session of its
      month)? The calendar rule lives with the data source, which knows the trading calendar; the
      policy only asks.
    * ``signal(as_of)`` — the candidate set as a :class:`~dataplatform.query.pit.Dataset` of
      :class:`MomentumRecord`, already narrowed to the PIT universe and to names with a computable
      look-back return and a current price. It is returned as a ``Dataset`` (not a bare tuple) so
      the policy admits it through ``ctx.pit`` and the guard proves there is no leak.

    A test supplies an in-memory implementation; the ten-year run supplies one backed by L1 through
    the query layer. Either way the policy never reaches past this surface.
    """

    def is_rebalance(self, session: date) -> bool:
        """Whether ``session`` is a rebalance session."""

    def signal(self, as_of: date) -> Dataset[MomentumRecord]:
        """The PIT candidate set as of ``as_of``, as a guardable dataset."""


class NaiveMomentumPolicy:
    """Top-N trailing-return momentum, rebalanced monthly — the M4.10 engine-validation strategy.

    Construct it with a :class:`MomentumData` source and :class:`MomentumParameters`. It satisfies
    :class:`backtest.replay.Policy`, so the replay engine drives it session by session; it reads
    data only through the injected ``ctx.pit`` (invariant #7), account state only through the
    injected ``ctx.broker`` (the ``Broker`` protocol — invariant #5), and time only through
    ``ctx.clock`` (B10). It holds no cost model and touches no store.

    Determinism: every branch is a pure function of the session's admitted candidates and the
    broker's reported book, with ties broken by ISIN, so a replay reproduces the decision exactly.
    """

    __slots__ = ("_data", "_params")

    def __init__(self, data: MomentumData, params: MomentumParameters | None = None) -> None:
        self._data = data
        self._params = params if params is not None else MomentumParameters()

    def decide(self, ctx: SessionContext) -> SessionDecision:
        """Decide this session: a heartbeat off a rebalance, a full rebalance on one."""
        if not self._data.is_rebalance(ctx.session):
            return self._heartbeat(ctx)
        return self._rebalance(ctx)

    # ── the two branches ─────────────────────────────────────────────────────────────────────────

    def _heartbeat(self, ctx: SessionContext) -> SessionDecision:
        """A no-order session: return the heartbeat evidence the engine stamps (inv. #9)."""
        holdings = ctx.broker.holdings()
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="held_names",
                    value=Decimal(len(holdings)),
                    text="no rebalance due this session",
                ),
            ),
        )
        return SessionDecision(evidence=evidence)

    def _rebalance(self, ctx: SessionContext) -> SessionDecision:
        """Rank the admitted candidates, sell the drop-outs, buy toward the equal-weight top-N."""
        # The one sanctioned read: admit the candidate dataset through the session's PIT scope. A
        # record not yet knowable on ctx.session raises here (invariant #7); the policy need not
        # filter for look-ahead itself.
        candidates = ctx.pit.admit(self._data.signal(ctx.session))
        ranked = sorted(candidates, key=lambda record: (-record.momentum, record.isin))
        chosen = ranked[: self._params.top_n]
        target = {record.isin: record for record in chosen}
        prices = {record.isin: record.price for record in chosen}

        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        sells = self._sells(held, target)
        buys, drifts_note = self._buys(ctx, held, target, prices)

        orders = tuple(order for order, _ in (*sells, *buys))
        entries = tuple(self._entry(ctx, order, note) for order, note in (*sells, *buys))
        evidence = self._evidence(ctx.session, chosen, drifts_note)
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    # ── sells: liquidate settled holdings that fell out of the target set ─────────────────────────

    def _sells(
        self,
        held: Mapping[str, Holding],
        target: Mapping[str, MomentumRecord],
    ) -> list[tuple[OrderRequest, str]]:
        """One full-quantity SELL for each settled holding no longer in the top-N, in ISIN order."""
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            if isin in target:
                continue
            quantity = held[isin].quantity
            sells.append(
                (
                    OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="MOMENTUM"),
                    f"exited top-{self._params.top_n} momentum set; liquidating {quantity} shares",
                )
            )
        return sells

    # ── buys: deploy currently-free cash across the equal-weight target basket ────────────────────

    def _buys(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        target: Mapping[str, MomentumRecord],
        prices: Mapping[str, Decimal],
    ) -> tuple[list[tuple[OrderRequest, str]], Decimal]:
        """Whole-share buys toward equal weight, sized from free cash; return them and total drift.

        Budget is the cash *currently free* (``margins().available``) times ``buy_budget_fraction``
        — never sale proceeds staged this session, which have not settled — so a buy is never
        rejected for cash it does not yet have. The allocation is the shared M4.7 greedy allocator,
        accounting for what is already held in the surviving names so it tops up toward equal weight
        rather than double-buying.
        """
        if not target:
            return [], _ZERO
        budget = ctx.broker.margins().available * self._params.buy_budget_fraction
        weights = _equal_weights(sorted(target))
        # What is already held in names that survive into the target, marked at the current price —
        # so drift is measured on the whole book, not just this instalment (M4.7).
        existing_value = {
            isin: Decimal(held[isin].quantity) * prices[isin] for isin in target if isin in held
        }
        allocation = simulate_sip_instalment(
            instalment=budget,
            targets=weights,
            prices=prices,
            existing_value=existing_value,
        )
        buys = [
            (
                order.to_order_request(exchange=Exchange.NSE, tag="MOMENTUM"),
                f"top-{self._params.top_n} momentum {target[order.isin].momentum:+} 12m; "
                f"buy {order.quantity} @ {order.price}",
            )
            for order in allocation.orders
        ]
        return buys, allocation.tracking_drift

    # ── journal + evidence ───────────────────────────────────────────────────────────────────────

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
        self, session: date, chosen: list[MomentumRecord], tracking_drift: Decimal
    ) -> EvidenceBundle:
        """The ranked momentum table the decision was made on, as one content-addressed bundle."""
        items = [
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="L2",
                label="momentum_12m",
                isin=record.isin,
                as_of=session,
                value=record.momentum,
                detail={"price": str(record.price), "rank": str(rank)},
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
                text=f"equal-weight top-{self._params.top_n} rebalance",
            )
        )
        return EvidenceBundle(trading_date=session, actor=Actor.T0, items=tuple(items))


def _equal_weights(isins: list[str]) -> dict[str, Decimal]:
    """Equal weights over ``isins`` that sum to exactly 1 — the remainder lands on the last name.

    ``Decimal(1)/Decimal(n)`` does not generally have an exact ``n``-fold sum, so the last ISIN
    absorbs the rounding residue; the result sums to exactly 1 and every weight is positive, which
    is what the allocator's weight check (M4.7) requires. Deterministic in the (sorted) ISIN order.
    """
    if not isins:
        raise ValueError("cannot weight an empty basket")
    n = len(isins)
    each = (_ONE / Decimal(n)).quantize(Decimal("0.00000001"))
    weights = dict.fromkeys(isins[:-1], each)
    weights[isins[-1]] = _ONE - each * (n - 1)
    return weights
