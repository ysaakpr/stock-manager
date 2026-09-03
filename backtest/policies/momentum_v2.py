"""M9.5 — momentum policy v2: 12-1, turnover banding, regime filter, vol-scaling (§7, X2).

The naive policy (:mod:`backtest.policies.naive_momentum`) existed to validate the *engine*, on
inputs that were still being made trustworthy. With M9.2 (adjusted prices), M9.3 (an investable,
liquid universe) and M9.4 (a real total-return benchmark) in place, this evolves the strategy into
a documented *v2* — four independent, a-priori improvements, **each behind its own toggle** so the
"a-priori vs tuned" line stays checkable and each change can be measured in isolation:

* **12-1 momentum** (``use_12_1``) — rank on the ``t-12m .. t-1m`` return, *skipping the most
  recent month*, instead of the raw ``0..12m`` return. The last month is the one that mean-reverts
  (the short-term reversal effect); dropping it is the standard academic momentum definition
  (Jegadeesh-Titman) and is chosen once, not fitted.
* **Turnover banding / hysteresis** (``sell_band``) — buy into the top ``top_n`` but only *sell* a
  holding once it has fallen out of a wider outer band (the top ``sell_band``, e.g. top-30). A name
  drifting between rank 20 and rank 30 is held rather than churned, which is what cuts the
  ~2079-sell turnover the naive ten-year run showed.
* **Regime filter** (``regime_filter``) — hold the momentum basket only while the index is above its
  own 200-day moving average; when it is below, sell the basket and *park in the liquid sleeve*
  (hold cash), re-entering when the regime turns back up. The classic trend-following overlay that
  sidesteps the worst of a bear leg.
* **Volatility-scaled weights** (``vol_scaled``) — size each name at ``~ 1 / vol`` (risk parity)
  rather than pure equal weight, so a jumpy name does not dominate the book's risk. Weights are
  computed over the same top ``top_n`` and normalised to sum to one.

With **all four toggles off** the policy reproduces the naive top-N decision exactly — that is the
"naive" baseline the increment report is struck against, and a unit test pins the parity. Every knob
is a stated parameter, echoed verbatim into the report, so nothing here is silently tuned.

Point-in-time (invariant #7): the 12-1 and 0-12 returns, the volatility estimate and the regime
reading are all carried on records tagged with the date they became knowable, and every one is read
through ``ctx.pit.admit`` — a datum not yet knowable on the session date trips the guard rather than
leaking. The volatility estimate and the moving average read only closes on or before the session.

What it never does: read a wall clock (time is ``ctx.clock`` — B10), key on a symbol (ISIN only —
invariant #2), hold a cost model (the injected broker owns the one shared model — invariant #4/#5),
or reach data outside the point-in-time context. Given the same inputs it returns the same decision.
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
    "MomentumV2Data",
    "MomentumV2Parameters",
    "MomentumV2Policy",
    "MomentumV2Record",
    "RegimeReading",
    "inverse_vol_weights",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_WEIGHT_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class MomentumV2Record:
    """One candidate this session: two momentum signals, its price, its vol, and when it was known.

    Carries *both* momentum definitions so the ``use_12_1`` toggle selects between them without a
    second data read:

    * ``momentum_0_12`` — the trailing ``0..12m`` total return (the naive signal, a plain ratio:
      ``0.25`` is +25 %);
    * ``momentum_12_1`` — the ``t-12m .. t-1m`` total return, skipping the most recent month.

    ``price`` is the current raw close the whole-share allocation and valuation use; ``volatility``
    is the trailing return volatility used only when ``vol_scaled`` is on (a positive dispersion, in
    the same ratio units); ``knowable_date`` is the date all four became knowable — the session's
    own close for an EOD close. The point-in-time guard checks ``knowable_date`` against the
    session's
    ``as_of`` (invariant #7).

    Never holds a ``float`` (all figures are ``Decimal``), a non-positive price (unpriceable names
    cannot be sized into whole shares), or a non-positive volatility (it would divide by zero under
    inverse-vol weighting).
    """

    isin: str
    momentum_0_12: Decimal
    momentum_12_1: Decimal
    price: Decimal
    volatility: Decimal
    knowable_date: date

    def __post_init__(self) -> None:
        for name in ("momentum_0_12", "momentum_12_1", "price", "volatility"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(
                    f"{name} must be a Decimal — money/signal is never float (CLAUDE.md)"
                )
        if self.price <= _ZERO:
            raise ValueError(f"price must be positive, got {self.price}")
        if self.volatility <= _ZERO:
            raise ValueError(f"volatility must be positive, got {self.volatility}")

    def momentum(self, *, use_12_1: bool) -> Decimal:
        """The ranking signal under the chosen definition: 12-1 if ``use_12_1`` else 0-12."""
        return self.momentum_12_1 if use_12_1 else self.momentum_0_12


@dataclass(frozen=True, slots=True)
class RegimeReading:
    """The market regime as of a session: the index level and its own moving average.

    ``index_level`` is the benchmark index's close on the session; ``moving_average`` is its
    trailing ``N``-day simple moving average (N stated in the parameters). ``risk_on`` is the trend
    filter: hold the basket only while the level sits at or above its average. ``knowable_date`` is
    the session close both were computed on — the guard refuses a reading dated past the session.

    The moving average is computed over closes on or before the session, so the reading is
    point-in-time by construction (invariant #7): no future close enters it.
    """

    index_level: Decimal
    moving_average: Decimal
    knowable_date: date

    def __post_init__(self) -> None:
        for name in ("index_level", "moving_average"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be a Decimal — never float (CLAUDE.md)")
            if getattr(self, name) <= _ZERO:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")

    @property
    def risk_on(self) -> bool:
        """Whether the index is at or above its moving average — the basket is held only if so."""
        return self.index_level >= self.moving_average


@dataclass(frozen=True, slots=True)
class MomentumV2Parameters:
    """The v2 knobs — the naive top-N plus four independently-toggleable, a-priori improvements.

    * ``top_n`` — names held (and the *buy* band), equal- or vol-weighted. 20, the naive default.
    * ``use_12_1`` — rank on the 12-1 return (skip the most recent month) instead of 0-12.
    * ``sell_band`` — the outer hysteresis band: a holding is sold only once it leaves the top
      ``sell_band``. Must be ``>= top_n``; ``None`` disables banding (sell the moment a name leaves
      the top ``top_n`` — the naive rule). E.g. ``top_n=20, sell_band=30`` buys the top-20 but holds
      anything still inside the top-30.
    * ``regime_filter`` — hold the basket only while the index is above its ``regime_ma_days``-day
      moving average; below it, sell the basket and park in the liquid (CASH) sleeve.
    * ``vol_scaled`` — size names at ``~ 1/vol`` instead of equal weight.
    * ``regime_ma_days`` — the moving-average window for the regime filter (200, the standard).
    * ``buy_budget_fraction`` — the share of free cash a rebalance deploys (a mechanical execution
      margin, not a return knob — carried over from the naive policy).
    * ``sleeve`` / ``parking_sleeve`` — journal sleeves for basket trades and for regime parking.

    With ``use_12_1``, ``sell_band``, ``regime_filter`` and ``vol_scaled`` all off/``None`` the
    policy is the naive top-N policy exactly. Every field is fixed by construction and reported
    verbatim, so the "no tuning" claim stays checkable.
    """

    top_n: int = 20
    use_12_1: bool = False
    sell_band: int | None = None
    regime_filter: bool = False
    vol_scaled: bool = False
    regime_ma_days: int = 200
    buy_budget_fraction: Decimal = Decimal("0.98")
    sleeve: Sleeve = Sleeve.TACTICAL
    parking_sleeve: Sleeve = Sleeve.CASH

    def __post_init__(self) -> None:
        if self.top_n <= 0:
            raise ValueError(f"top_n must be positive, got {self.top_n}")
        if self.sell_band is not None and self.sell_band < self.top_n:
            raise ValueError(
                f"sell_band must be >= top_n (the outer band cannot be tighter than the buy band): "
                f"got sell_band={self.sell_band}, top_n={self.top_n}"
            )
        if self.regime_ma_days <= 0:
            raise ValueError(f"regime_ma_days must be positive, got {self.regime_ma_days}")
        if not isinstance(self.buy_budget_fraction, Decimal):
            raise TypeError("buy_budget_fraction must be a Decimal")
        if not (_ZERO < self.buy_budget_fraction <= _ONE):
            raise ValueError(
                f"buy_budget_fraction must be in (0, 1], got {self.buy_budget_fraction}"
            )


@runtime_checkable
class MomentumV2Data(Protocol):
    """Where the v2 policy reads its world — the injected seam the point-in-time context wraps.

    * ``is_rebalance(session)`` — is today a rebalance session (first trading session of the month)?
    * ``signal(as_of)`` — the candidate set as a guardable :class:`Dataset` of
      :class:`MomentumV2Record`, already narrowed to the investable PIT universe and to names with
      both momentum definitions, a price and a volatility.
    * ``regime(as_of)`` — the :class:`RegimeReading` for the session, as a one-element guardable
      dataset. Only read when ``regime_filter`` is on; a source that does not model regime need not
      implement anything more than a stub when the filter is off.

    A test supplies an in-memory implementation; the ten-year run supplies one backed by L1 through
    the query layer. The policy never reaches past this surface.
    """

    def is_rebalance(self, session: date) -> bool:
        """Whether ``session`` is a rebalance session."""

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        """The PIT candidate set as of ``as_of``, as a guardable dataset."""

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        """The regime reading as of ``as_of``, as a one-element guardable dataset."""


class MomentumV2Policy:
    """Top-N momentum v2 — 12-1 ranking, turnover banding, regime filter and vol-scaled weights.

    Construct it with a :class:`MomentumV2Data` source and :class:`MomentumV2Parameters`. It
    satisfies :class:`backtest.replay.Policy`, so the replay engine drives it session by session; it
    reads data only through ``ctx.pit`` (invariant #7), account state only through ``ctx.broker``
    (invariant #5) and time only through ``ctx.clock`` (B10). It holds no cost model and touches no
    store.

    Determinism: every branch is a pure function of the session's admitted candidates, the admitted
    regime reading and the broker's reported book, with ties broken by ISIN, so a replay reproduces
    the decision exactly.
    """

    __slots__ = ("_data", "_params")

    def __init__(self, data: MomentumV2Data, params: MomentumV2Parameters | None = None) -> None:
        self._data = data
        self._params = params if params is not None else MomentumV2Parameters()

    def decide(self, ctx: SessionContext) -> SessionDecision:
        """Decide this session: a heartbeat off a rebalance, a full rebalance on one."""
        if not self._data.is_rebalance(ctx.session):
            return self._heartbeat(ctx)
        return self._rebalance(ctx)

    # ── branches ──────────────────────────────────────────────────────────────────────────────────

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
        """Rank the admitted candidates, apply the toggled overlays, and stage the orders."""
        # Regime filter: read the regime through the PIT guard *first*. When it is risk-off, the
        # basket is not held at all — sell everything and park in the liquid sleeve (invariant #7:
        # the reading is admitted, so a future-dated regime trips the guard rather than leaking).
        if self._params.regime_filter:
            (reading,) = ctx.pit.admit(self._data.regime(ctx.session))
            if not reading.risk_on:
                return self._park(ctx, reading)

        candidates = ctx.pit.admit(self._data.signal(ctx.session))
        use_12_1 = self._params.use_12_1
        ranked = sorted(
            candidates, key=lambda record: (-record.momentum(use_12_1=use_12_1), record.isin)
        )
        chosen = ranked[: self._params.top_n]
        # The outer band that governs sells: the top ``sell_band`` names (banding), or just the
        # top ``top_n`` (no banding — the naive rule). A held name inside this band is retained.
        band = self._params.sell_band if self._params.sell_band is not None else self._params.top_n
        keep = {record.isin for record in ranked[:band]}
        target = {record.isin: record for record in chosen}
        prices = {record.isin: record.price for record in chosen}

        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        sells = self._sells(held, keep)
        buys, drifts_note = self._buys(ctx, held, target, prices, use_12_1=use_12_1)

        orders = tuple(order for order, _ in (*sells, *buys))
        entries = tuple(self._entry(ctx, order, note) for order, note in (*sells, *buys))
        evidence = self._evidence(ctx.session, chosen, drifts_note, use_12_1=use_12_1)
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _park(self, ctx: SessionContext, reading: RegimeReading) -> SessionDecision:
        """Regime risk-off: sell the whole basket and hold cash (the liquid sleeve)."""
        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            quantity = held[isin].quantity
            sells.append(
                (
                    OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="MOMENTUM"),
                    f"regime risk-off (index {reading.index_level} < {self._params.regime_ma_days}"
                    f"d MA {reading.moving_average}); parking {quantity} shares to liquid sleeve",
                )
            )
        orders = tuple(order for order, _ in sells)
        entries = tuple(self._park_entry(ctx, order, note) for order, note in sells)
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.PRICE,
                    source="benchmark",
                    label="regime_index_vs_ma",
                    as_of=ctx.session,
                    value=reading.index_level,
                    detail={
                        "moving_average": str(reading.moving_average),
                        "ma_days": str(self._params.regime_ma_days),
                        "risk_on": "false",
                    },
                    text="index below its moving average — basket parked to the liquid sleeve",
                ),
            ),
        )
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    # ── sells ─────────────────────────────────────────────────────────────────────────────────────

    def _sells(self, held: Mapping[str, Holding], keep: set[str]) -> list[tuple[OrderRequest, str]]:
        """One full-quantity SELL for each settled holding outside the keep band, in ISIN order."""
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            if isin in keep:
                continue
            quantity = held[isin].quantity
            band = (
                self._params.sell_band if self._params.sell_band is not None else self._params.top_n
            )
            sells.append(
                (
                    OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="MOMENTUM"),
                    f"left the top-{band} momentum band; liquidating {quantity} shares",
                )
            )
        return sells

    # ── buys ──────────────────────────────────────────────────────────────────────────────────────

    def _buys(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        target: Mapping[str, MomentumV2Record],
        prices: Mapping[str, Decimal],
        *,
        use_12_1: bool,
    ) -> tuple[list[tuple[OrderRequest, str]], Decimal]:
        """Whole-share buys toward the model weights, sized from free cash; return them and drift.

        Weights are equal over the top ``top_n`` (naive) or inverse-volatility (``vol_scaled``).
        Budget is the cash *currently free* times ``buy_budget_fraction`` — never sale proceeds
        staged this session, which have not settled — and the allocation accounts for existing
        holdings in the surviving names so it tops up toward the model rather than double-buying.
        """
        if not target:
            return [], _ZERO
        budget = ctx.broker.margins().available * self._params.buy_budget_fraction
        if self._params.vol_scaled:
            weights = inverse_vol_weights({isin: target[isin].volatility for isin in target})
        else:
            weights = _equal_weights(sorted(target))
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
                f"top-{self._params.top_n} momentum "
                f"{target[order.isin].momentum(use_12_1=use_12_1):+} "
                f"({'12-1' if use_12_1 else '0-12'}); buy {order.quantity} @ {order.price}",
            )
            for order in allocation.orders
        ]
        return buys, allocation.tracking_drift

    # ── journal + evidence ──────────────────────────────────────────────────────────────────────

    def _entry(self, ctx: SessionContext, order: OrderRequest, rationale: str) -> JournalEntry:
        """A BUY/SELL journal entry for one basket order — rationale, sleeve and ISIN (§0/§5.7)."""
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

    def _park_entry(self, ctx: SessionContext, order: OrderRequest, rationale: str) -> JournalEntry:
        """A SELL entry for a regime-parking exit — tagged with the parking (CASH) sleeve."""
        return JournalEntry(
            ts=ctx.clock.now(),
            trading_date=ctx.session,
            actor=Actor.T0,
            decision=Decision.SELL,
            isin=order.isin,
            sleeve=self._params.parking_sleeve,
            rationale=rationale,
        )

    def _evidence(
        self,
        session: date,
        chosen: list[MomentumV2Record],
        tracking_drift: Decimal,
        *,
        use_12_1: bool,
    ) -> EvidenceBundle:
        """The ranked momentum table the decision was made on, as one content-addressed bundle."""
        label = "momentum_12_1" if use_12_1 else "momentum_0_12"
        items = [
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="L2",
                label=label,
                isin=record.isin,
                as_of=session,
                value=record.momentum(use_12_1=use_12_1),
                detail={
                    "price": str(record.price),
                    "volatility": str(record.volatility),
                    "rank": str(rank),
                },
            )
            for rank, record in enumerate(chosen, start=1)
        ]
        weighting = "inverse-vol" if self._params.vol_scaled else "equal-weight"
        items.append(
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="book",
                label="tracking_drift",
                as_of=session,
                value=tracking_drift,
                text=f"{weighting} top-{self._params.top_n} rebalance",
            )
        )
        return EvidenceBundle(trading_date=session, actor=Actor.T0, items=tuple(items))


# ── weighting ───────────────────────────────────────────────────────────────────────────────────


def _equal_weights(isins: list[str]) -> dict[str, Decimal]:
    """Equal weights over ``isins`` that sum to exactly 1 — the remainder lands on the last name.

    ``Decimal(1)/Decimal(n)`` does not generally have an exact ``n``-fold sum, so the last ISIN
    absorbs the rounding residue; the result sums to exactly 1 and every weight is positive, which
    the allocator's weight check (M4.7) requires. Deterministic in the (sorted) ISIN order.
    """
    if not isins:
        raise ValueError("cannot weight an empty basket")
    n = len(isins)
    each = (_ONE / Decimal(n)).quantize(_WEIGHT_QUANTUM)
    weights = dict.fromkeys(isins[:-1], each)
    weights[isins[-1]] = _ONE - each * (n - 1)
    return weights


def inverse_vol_weights(volatilities: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Weights proportional to ``1 / vol``, summing to exactly 1 — the risk-parity sizing (M9.5).

    A less volatile name earns a larger weight, so no single jumpy name dominates the book's risk.
    The raw ``1/vol`` figures are normalised by their sum; the last ISIN (in sorted order) absorbs
    the quantisation residue so the result sums to exactly 1 and every weight stays positive, which
    the allocator's weight check requires. Deterministic in the sorted ISIN order.

    Assumes every volatility is a positive ``Decimal``; a non-positive vol is a bug (it would give a
    zero or negative weight, or divide by zero), and is refused loudly rather than silently dropped.
    """
    if not volatilities:
        raise ValueError("cannot weight an empty basket")
    isins = sorted(volatilities)
    inverses: dict[str, Decimal] = {}
    for isin in isins:
        vol = volatilities[isin]
        if not isinstance(vol, Decimal):
            raise TypeError(f"volatility for {isin} must be a Decimal, got {vol!r}")
        if vol <= _ZERO:
            raise ValueError(f"volatility for {isin} must be positive, got {vol}")
        inverses[isin] = _ONE / vol
    total = sum(inverses.values(), _ZERO)
    weights: dict[str, Decimal] = {}
    running = _ZERO
    for isin in isins[:-1]:
        weight = (inverses[isin] / total).quantize(_WEIGHT_QUANTUM)
        weights[isin] = weight
        running += weight
    # The last name absorbs the residue so the weights sum to exactly 1.
    weights[isins[-1]] = _ONE - running
    return weights
