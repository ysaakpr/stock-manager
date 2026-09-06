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

* **Redeploy proceeds next session** (``redeploy_next_session``) — a mechanical fix to a gap the
  first four leave open. A rebalance sells on session R (filling R+1) and sizes its buys from the
  cash that is *already* free on R, so the sale proceeds land on R+1 and then sit idle until the
  next monthly rebalance: with banding, two to five names a month are sold and 10-25 % of the book
  waits in cash for a month, every month. With the toggle on, the policy remembers the basket it
  chose on R and, on R+1, deploys the freed cash toward the same target weights at R+1's prices —
  one more allocator pass, no new signal, no second look at the ranking. It is an execution
  improvement, not a return knob: the basket is the one already decided.

* **Volatility target** (``vol_target_annual``) — a portfolio-level overlay on top of whichever
  basket the other toggles chose. From each name's trailing monthly-return volatility (already on
  the record) and one stated average pairwise correlation, the basket's annualised volatility is
  estimated as ``sqrt((1-rho) * sum(w_i^2 s_i^2) + rho * (sum(w_i s_i))^2)``, and the book is
  scaled to ``min(1, target / estimate)`` of its capital — the rest parked in cash. A book that is
  over its exposure sells pro-rata; one under it buys only up to the cap. It is the standard
  risk-parity-at-the-portfolio-level overlay, chosen once (15 %, rho 0.3) and never fitted.

With **all six toggles off** the policy reproduces the naive top-N decision exactly — that is the
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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Final, Protocol, runtime_checkable

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from backtest.replay import SessionContext, SessionDecision
from backtest.sip import simulate_sip_instalment
from dataplatform.query.pit import Dataset
from execution.broker import Exchange, Holding, OrderRequest, Side

__all__ = [
    "PAPER_RATIFIED_2026_09_06",
    "MomentumV2Data",
    "MomentumV2Parameters",
    "MomentumV2Policy",
    "MomentumV2Record",
    "RegimeReading",
    "basket_volatility_annual",
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
    * ``redeploy_next_session`` — on the session after a rebalance, deploy the cash the rebalance's
      sells released into the basket chosen at that rebalance (same weights, that day's prices).
    * ``vol_target_annual`` — scale the basket to this annualised volatility (``None`` = fully
      invested, the pre-overlay behaviour); ``assumed_correlation`` is the one stated average
      pairwise correlation the estimate uses (0.3, the long-run large-cap figure, chosen once).
    * ``regime_ma_days`` — the moving-average window for the regime filter (200, the standard).
    * ``buy_budget_fraction`` — the share of free cash a rebalance deploys (a mechanical execution
      margin, not a return knob — carried over from the naive policy).
    * ``sleeve`` / ``parking_sleeve`` — journal sleeves for basket trades and for regime parking.

    With ``use_12_1``, ``sell_band``, ``regime_filter``, ``vol_scaled``, ``redeploy_next_session``
    and ``vol_target_annual`` all off/``None`` the policy is the naive top-N policy exactly. Every
    field is fixed by construction and reported
    verbatim, so the "no tuning" claim stays checkable.
    """

    top_n: int = 20
    use_12_1: bool = False
    sell_band: int | None = None
    regime_filter: bool = False
    vol_scaled: bool = False
    redeploy_next_session: bool = False
    vol_target_annual: Decimal | None = None
    assumed_correlation: Decimal = Decimal("0.3")
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
        if self.vol_target_annual is not None:
            if not isinstance(self.vol_target_annual, Decimal):
                raise TypeError("vol_target_annual must be a Decimal")
            if self.vol_target_annual <= _ZERO:
                raise ValueError(
                    f"vol_target_annual must be positive, got {self.vol_target_annual}"
                )
        if not isinstance(self.assumed_correlation, Decimal):
            raise TypeError("assumed_correlation must be a Decimal")
        if not (_ZERO <= self.assumed_correlation <= _ONE):
            raise ValueError(
                f"assumed_correlation must be in [0, 1], got {self.assumed_correlation}"
            )
        if not isinstance(self.buy_budget_fraction, Decimal):
            raise TypeError("buy_budget_fraction must be a Decimal")
        if not (_ZERO < self.buy_budget_fraction <= _ONE):
            raise ValueError(
                f"buy_budget_fraction must be in (0, 1], got {self.buy_budget_fraction}"
            )


#: The momentum-sleeve configuration the owner ratified for **paper mode** on 2026-09-06 — the
#: four M9.5 toggles plus next-session redeployment of sale proceeds — on the evidence in
#: `ops/gates/M9-momentum-v2-report.md` (ten-year increment table) and
#: `ops/gates/M10-fundamentals-signal-report.md` (per-regime comparison). Recorded in
#: `HUMAN_DECISIONS.md` (D13). A fixture/paper ratification is never valid for real money (B9); the
#: volatility target stays off pending a read of its cost (BACKLOG).
PAPER_RATIFIED_2026_09_06: Final = MomentumV2Parameters(
    top_n=20,
    use_12_1=True,
    sell_band=30,
    regime_filter=True,
    vol_scaled=True,
    redeploy_next_session=True,
)


@runtime_checkable
class MomentumV2Data(Protocol):
    """Where the v2 policy reads its world — the injected seam the point-in-time context wraps.

    * ``is_rebalance(session)`` — is today a rebalance session (first trading session of the month)?
    * ``signal(as_of)`` — the candidate set as a guardable :class:`Dataset` of
      :class:`MomentumV2Record`, already narrowed to the investable PIT universe and to names with
      both momentum definitions, a price and a volatility. With ``redeploy_next_session`` on, the
      policy also reads it on the session *after* each rebalance, for that day's prices of the
      basket already chosen — a source must serve those sessions too (the L1 one does).
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

    __slots__ = ("_data", "_params", "_pending")

    def __init__(self, data: MomentumV2Data, params: MomentumV2Parameters | None = None) -> None:
        self._data = data
        self._params = params if params is not None else MomentumV2Parameters()
        #: The basket weights decided at the last rebalance, awaiting the proceeds of its sells
        #: (``redeploy_next_session``). ``None`` when nothing is pending. Deterministic state: it is
        #: a pure function of the previous session's decision, so a replay reproduces it.
        self._pending: dict[str, Decimal] | None = None

    def decide(self, ctx: SessionContext) -> SessionDecision:
        """Decide this session: a heartbeat off a rebalance, a full rebalance on one.

        With ``redeploy_next_session`` on, the session right after a rebalance is a *deployment*
        session: the cash that rebalance's sells released is put into the basket it chose.
        """
        if self._data.is_rebalance(ctx.session):
            return self._rebalance(ctx)
        if self._pending is not None:
            pending, self._pending = self._pending, None
            return self._redeploy(ctx, pending)
        return self._heartbeat(ctx)

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
        self._pending = None
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
        exposure = _ONE
        trims: list[tuple[OrderRequest, str]] = []
        if self._params.vol_target_annual is not None and target:
            exposure = self._exposure(target)
            trims = self._trims(held, target, prices, exposure, ctx, ranked)
        buys, drifts_note = self._buys(
            ctx, held, target, prices, use_12_1=use_12_1, exposure=exposure, ranked=ranked
        )
        if trims:
            buys = []  # a book being cut back to its exposure does not also add to it
        if self._params.redeploy_next_session and target and sells:
            # Only a rebalance that sold something leaves proceeds to deploy tomorrow.
            self._pending = self._weights_for(target)

        orders = tuple(order for order, _ in (*sells, *trims, *buys))
        entries = tuple(self._entry(ctx, order, note) for order, note in (*sells, *trims, *buys))
        evidence = self._evidence(
            ctx.session, chosen, drifts_note, use_12_1=use_12_1, exposure=exposure
        )
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
        exposure: Decimal = _ONE,
        ranked: Sequence[MomentumV2Record] = (),
    ) -> tuple[list[tuple[OrderRequest, str]], Decimal]:
        """Whole-share buys toward the model weights, sized from free cash; return them and drift.

        Weights are equal over the top ``top_n`` (naive) or inverse-volatility (``vol_scaled``).
        Budget is the cash *currently free* times ``buy_budget_fraction`` — never sale proceeds
        staged this session, which have not settled — and the allocation accounts for existing
        holdings in the surviving names so it tops up toward the model rather than double-buying.
        Under a volatility target (``exposure < 1``) the budget is further capped so the basket's
        value does not exceed ``exposure`` of the book's capital.
        """
        if not target:
            return [], _ZERO
        budget = ctx.broker.margins().available * self._params.buy_budget_fraction
        if exposure < _ONE:
            capital = self._capital(ctx, held, prices, ranked)
            in_basket = sum(
                (Decimal(held[isin].quantity) * prices[isin] for isin in target if isin in held),
                _ZERO,
            )
            headroom = capital * exposure - in_basket
            budget = min(budget, max(_ZERO, headroom))
            if budget <= _ZERO:
                return [], _ZERO
        weights = self._weights_for(target)
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

    def _exposure(self, target: Mapping[str, MomentumV2Record]) -> Decimal:
        """``min(1, vol_target / basket_vol)`` for the basket at its model weights."""
        assert self._params.vol_target_annual is not None
        weights = self._weights_for(target)
        estimate = basket_volatility_annual(
            {isin: target[isin].volatility for isin in target},
            weights,
            correlation=self._params.assumed_correlation,
        )
        if estimate <= _ZERO:
            return _ONE
        return min(_ONE, (self._params.vol_target_annual / estimate).quantize(_WEIGHT_QUANTUM))

    def _capital(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        prices: Mapping[str, Decimal],
        ranked: Sequence[MomentumV2Record],
    ) -> Decimal:
        """Free cash plus every holding marked at its candidate price (average cost if unpriced)."""
        price_of = {record.isin: record.price for record in ranked}
        price_of.update(prices)
        marked = sum(
            (
                Decimal(holding.quantity) * price_of.get(isin, holding.average_price)
                for isin, holding in held.items()
            ),
            _ZERO,
        )
        return ctx.broker.margins().available + marked

    def _trims(
        self,
        held: Mapping[str, Holding],
        target: Mapping[str, MomentumV2Record],
        prices: Mapping[str, Decimal],
        exposure: Decimal,
        ctx: SessionContext,
        ranked: Sequence[MomentumV2Record],
    ) -> list[tuple[OrderRequest, str]]:
        """Pro-rata partial sells that bring the held basket down to ``exposure`` of capital."""
        in_basket = {
            isin: Decimal(held[isin].quantity) * prices[isin] for isin in target if isin in held
        }
        current = sum(in_basket.values(), _ZERO)
        if current <= _ZERO:
            return []
        allowed = self._capital(ctx, held, prices, ranked) * exposure
        if current <= allowed:
            return []
        cut = (current - allowed) / current
        trims: list[tuple[OrderRequest, str]] = []
        for isin in sorted(in_basket):
            # Round the trim *up*: a risk overlay that leaves the book a share over its cap has not
            # enforced the cap. Whole shares, never more than is held.
            quantity = min(
                held[isin].quantity,
                int((Decimal(held[isin].quantity) * cut).to_integral_value(ROUND_CEILING)),
            )
            if quantity <= 0:
                continue
            trims.append(
                (
                    OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="MOMENTUM"),
                    f"vol target {self._params.vol_target_annual}: basket exposure cut to "
                    f"{exposure} of capital; trimming {quantity} shares",
                )
            )
        return trims

    def _weights_for(self, target: Mapping[str, MomentumV2Record]) -> dict[str, Decimal]:
        """The basket's model weights: inverse-vol when ``vol_scaled``, else equal."""
        if self._params.vol_scaled:
            return inverse_vol_weights({isin: target[isin].volatility for isin in target})
        return _equal_weights(sorted(target))

    # ── redeploy: the session after a rebalance ───────────────────────────────────────────────────

    def _redeploy(self, ctx: SessionContext, weights: Mapping[str, Decimal]) -> SessionDecision:
        """Put the cash yesterday's sells released into yesterday's basket at today's prices.

        Reads today's admitted candidate set only for the *prices* of the names already chosen;
        the ranking is not revisited. A basket name with no price today is left out of this pass
        (its weight is renormalised away), never guessed. If nothing is affordable the session is
        journalled as a heartbeat that says so.
        """
        records = {record.isin: record for record in ctx.pit.admit(self._data.signal(ctx.session))}
        priced = {isin: w for isin, w in weights.items() if isin in records}
        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        budget = ctx.broker.margins().available * self._params.buy_budget_fraction
        if not priced or budget <= _ZERO:
            return self._redeploy_heartbeat(ctx, budget, reason="no priced basket name or no cash")
        total = sum(priced.values(), _ZERO)
        weights_norm = {isin: (w / total).quantize(_WEIGHT_QUANTUM) for isin, w in priced.items()}
        last = sorted(weights_norm)[-1]
        weights_norm[last] = _ONE - sum(
            (w for isin, w in weights_norm.items() if isin != last), _ZERO
        )
        prices = {isin: records[isin].price for isin in priced}
        existing_value = {
            isin: Decimal(held[isin].quantity) * prices[isin] for isin in priced if isin in held
        }
        allocation = simulate_sip_instalment(
            instalment=budget, targets=weights_norm, prices=prices, existing_value=existing_value
        )
        if not allocation.orders:
            return self._redeploy_heartbeat(ctx, budget, reason="no affordable share reduces drift")
        buys = [
            (
                order.to_order_request(exchange=Exchange.NSE, tag="MOMENTUM"),
                f"redeploy: proceeds of yesterday's rebalance sells into the chosen basket; "
                f"buy {order.quantity} @ {order.price}",
            )
            for order in allocation.orders
        ]
        orders = tuple(order for order, _ in buys)
        entries = tuple(self._entry(ctx, order, note) for order, note in buys)
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="redeploy_budget",
                    as_of=ctx.session,
                    value=budget,
                    detail={
                        "names_bought": str(len(buys)),
                        "tracking_drift": str(allocation.tracking_drift),
                    },
                    text="cash freed by yesterday's rebalance sells, deployed into its basket",
                ),
            ),
        )
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _redeploy_heartbeat(
        self, ctx: SessionContext, budget: Decimal, *, reason: str
    ) -> SessionDecision:
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="redeploy_budget",
                    as_of=ctx.session,
                    value=budget,
                    text=f"redeploy session, nothing bought: {reason}",
                ),
            ),
        )
        return SessionDecision(evidence=evidence)

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
        exposure: Decimal = _ONE,
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
        if self._params.vol_target_annual is not None:
            items.append(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="vol_target_exposure",
                    as_of=session,
                    value=exposure,
                    detail={
                        "vol_target_annual": str(self._params.vol_target_annual),
                        "assumed_correlation": str(self._params.assumed_correlation),
                    },
                    text="share of capital the basket may occupy under the volatility target",
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


_MONTHS_PER_YEAR_SQRT = Decimal(12).sqrt()


def basket_volatility_annual(
    volatilities: Mapping[str, Decimal],
    weights: Mapping[str, Decimal],
    *,
    correlation: Decimal,
) -> Decimal:
    """Annualised basket volatility from monthly name volatilities and one assumed correlation.

    ``sqrt((1 - rho) * sum(w_i^2 s_i^2) + rho * (sum(w_i s_i))^2)``, then times ``sqrt(12)``. With
    ``rho = 1`` this is the weighted average volatility (names move together); with ``rho = 0`` the
    pure diversification case. Every input is a Decimal; the result is exact to Decimal precision.
    """
    if not volatilities or not weights:
        raise ValueError("cannot estimate the volatility of an empty basket")
    weighted_sq = _ZERO
    weighted = _ZERO
    for isin, weight in weights.items():
        vol = volatilities[isin]
        if not isinstance(vol, Decimal) or not isinstance(weight, Decimal):
            raise TypeError("volatilities and weights must be Decimal")
        if vol <= _ZERO:
            raise ValueError(f"volatility for {isin} must be positive, got {vol}")
        weighted_sq += weight * weight * vol * vol
        weighted += weight * vol
    variance = (_ONE - correlation) * weighted_sq + correlation * weighted * weighted
    return variance.sqrt() * _MONTHS_PER_YEAR_SQRT


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
