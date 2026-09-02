"""X2: SIP instalment mechanics — turning a fixed rupee amount into whole-share orders.

A systematic investment plan pays a fixed instalment (a ₹10,000 monthly, say) into a model
portfolio described by *target weights*. On an Indian cash exchange there are no fractional
shares (invariant echoed by ``OrderRequest``: quantities are whole ``int`` counts), so an
instalment almost never divides cleanly across the model: ₹10,000 spread over eight names is
₹1,250 apiece, and a name trading at ₹3,000 cannot be bought this month. The instalment therefore
has to *decide which holdings it actually buys* (EXECUTION_PLAN §5.6), leave what it could not
spend as **residual cash that carries forward** into the next instalment, and let the caller see
how far the resulting book drifts from the model.

**What this module does.** :func:`simulate_sip_instalment` takes the instalment, the model's
target weights, the current prices, any cash carried in from a prior instalment, and (optionally)
the value of what is already held. It returns a :class:`SipAllocation`: the whole-share buy
orders, the residual cash to carry forward, and the per-name tracking drift of the resulting
portfolio versus the model.

**How it decides — greedy drift minimisation.** Starting from what is already held, it buys
shares one at a time, each time choosing the *affordable* purchase that most reduces the
portfolio's L1 tracking drift (the summed absolute gap between actual and target weight). It stops
when no affordable purchase would reduce drift any further — exactly the moment to stop pouring
cash into names already at or above their model weight, and to carry the rest forward to a month
when the underweight (often more expensive) name is reachable. This is deterministic: the same
inputs walk the same path (ties broken by ISIN order), so a replay produces byte-identical
allocations (§8.3.3).

**What it never does.** It does not invent fractional shares, it does not read a clock (an
instalment's allocation is a pure function of its inputs; *when* it happens is the caller's
journal entry, M4.8), and it does not join on anything but the ISIN (invariant #2). Every money
figure is a ``Decimal`` and every share count is an ``int`` (CLAUDE.md).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

import structlog

from execution.broker import Exchange, OrderRequest, Side

_ZERO = Decimal("0")
_ONE = Decimal("1")

# Weights are read off a model and summed; floating rounding is not a concern (all Decimal), but a
# model hand-authored to three or four places should still be accepted as "sums to one".
_WEIGHT_SUM_TOLERANCE = Decimal("0.0001")

_ISIN_PATTERN = r"[A-Z]{2}[A-Z0-9]{9}[0-9]"

_log = structlog.get_logger(__name__)

__all__ = [
    "SipAllocation",
    "SipError",
    "SipOrder",
    "WeightDrift",
    "simulate_sip_instalment",
]


# ── errors ─────────────────────────────────────────────────────────────────────────────────────


class SipError(Exception):
    """Base for every refusal the SIP allocator makes. It fails loud (CLAUDE.md), never silently."""


# ── value objects ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SipOrder:
    """One whole-share buy the instalment decided on: the security, the count, the price it cost.

    ``price`` is the per-share price the allocation was computed against; carrying it makes the
    order self-describing (``cost`` is derived, not stored, so the two cannot disagree) and lets the
    caller build a broker :class:`OrderRequest` without a second price lookup. Keyed by ISIN.
    """

    isin: str
    quantity: int
    price: Decimal

    @property
    def cost(self) -> Decimal:
        """Turnover for this order — price times whole-share quantity, before broker charges."""
        return self.price * self.quantity

    def to_order_request(
        self, *, exchange: Exchange = Exchange.NSE, tag: str | None = None
    ) -> OrderRequest:
        """Express this SIP decision as a broker order: a market buy of the whole-share count."""
        return OrderRequest(
            isin=self.isin,
            side=Side.BUY,
            quantity=self.quantity,
            exchange=exchange,
            tag=tag,
        )


@dataclass(frozen=True, slots=True)
class WeightDrift:
    """How far one name's resulting weight sits from its model weight, after the instalment.

    ``actual_weight`` is the name's share of the *invested* value (existing holdings plus this
    instalment's buys); residual cash is deliberately not in the denominator, because the model is a
    model of the invested book, not of the cash waiting to be deployed. Keyed by ISIN.
    """

    isin: str
    target_weight: Decimal
    actual_weight: Decimal

    @property
    def drift(self) -> Decimal:
        """Signed gap: positive when the name ended up overweight, negative when underweight."""
        return self.actual_weight - self.target_weight


@dataclass(frozen=True, slots=True)
class SipAllocation:
    """The outcome of one instalment: what it bought, what it could not spend, and the drift left.

    ``orders`` holds only the names that got at least one share this instalment. ``residual_cash``
    is the amount to carry into the next instalment — the whole point of the exercise, since an
    instalment almost never divides cleanly into whole shares. ``drifts`` reports every model name
    (including those that got nothing) so the caller can journal the tracking gap (§5.6).
    """

    instalment: Decimal
    carried_in: Decimal
    orders: tuple[SipOrder, ...]
    residual_cash: Decimal
    drifts: tuple[WeightDrift, ...]

    @property
    def available(self) -> Decimal:
        """Cash the instalment had to work with: this month's payment plus what was carried in."""
        return self.instalment + self.carried_in

    @property
    def deployed(self) -> Decimal:
        """Cash actually spent on shares — turnover across every order, before broker charges."""
        return sum((order.cost for order in self.orders), _ZERO)

    @property
    def tracking_drift(self) -> Decimal:
        """L1 tracking drift: the summed absolute gap between actual and target weight over names.

        Zero would mean the invested book sits exactly on the model; the whole-share constraint
        means it rarely does, and this single number is how large that residual mismatch is.
        """
        return sum((abs(d.drift) for d in self.drifts), _ZERO)


# ── the allocator ────────────────────────────────────────────────────────────────────────────────


def simulate_sip_instalment(
    *,
    instalment: Decimal,
    targets: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    carried_in: Decimal = _ZERO,
    existing_value: Mapping[str, Decimal] | None = None,
) -> SipAllocation:
    """Allocate one SIP instalment into whole-share buys, carrying the unspendable remainder on.

    What it does: greedily buys, one share at a time, whichever affordable purchase most reduces the
    portfolio's L1 tracking drift versus ``targets``, stopping when no affordable purchase would
    reduce it further. The result is deterministic in the inputs (ties broken by ISIN order), so a
    replay reproduces it exactly.

    Assumes: ``targets`` maps ISIN → weight, weights are positive and sum to one (within a small
    tolerance), every target name has a positive price in ``prices``, and every money figure is a
    ``Decimal``. ``existing_value`` (ISIN → current mark-to-market value) lets the allocation
    account for what is already held so drift is on the whole book; omit it for a fresh SIP.

    Never: invents fractional shares, spends more than ``instalment + carried_in``, reads a clock,
    or keys on anything but the ISIN.
    """
    instalment = _require_nonneg_money("instalment", instalment)
    carried_in = _require_nonneg_money("carried_in", carried_in)
    weights = _validated_weights(targets)
    unit_prices = _validated_prices(weights, prices)
    base_value = _validated_existing(existing_value)

    isins = sorted(weights)  # deterministic evaluation and tie-break order
    bought: dict[str, int] = dict.fromkeys(isins, 0)
    remaining = instalment + carried_in

    # The capital the model is measured against is fixed for this instalment: what is already held
    # plus the cash we set out to deploy. It stays constant as buying converts cash into holdings,
    # so cash still sitting idle shows up as drift (every name underweight) — which is what makes
    # deploying it reduce drift, and stops the greedy preferring the all-cash book to any buy.
    existing_total = sum(base_value.values(), _ZERO)
    total_capital = existing_total + remaining

    # value_i = what is already held in i, plus what this instalment has bought so far.
    def value_of(isin: str) -> Decimal:
        return base_value.get(isin, _ZERO) + unit_prices[isin] * bought[isin]

    def drift_after(extra: str | None) -> Decimal:
        """L1 drift vs the model if one more share of ``extra`` were bought (``None`` = as-is)."""
        return sum(
            (
                abs(
                    (value_of(isin) + (unit_prices[isin] if isin == extra else _ZERO))
                    / total_capital
                    - weights[isin]
                )
                for isin in isins
            ),
            _ZERO,
        )

    while total_capital > _ZERO:
        current = drift_after(None)
        best_isin: str | None = None
        best_drift = current
        for isin in isins:  # sorted → first strictly-better wins, so ties break to the lowest ISIN
            if unit_prices[isin] > remaining:
                continue
            candidate = drift_after(isin)
            if candidate < best_drift:
                best_drift = candidate
                best_isin = isin
        if best_isin is None:
            break  # no affordable purchase reduces drift — carry the rest forward
        bought[best_isin] += 1
        remaining -= unit_prices[best_isin]

    orders = tuple(
        SipOrder(isin=isin, quantity=bought[isin], price=unit_prices[isin])
        for isin in isins
        if bought[isin] > 0
    )
    drifts = _final_drifts(isins, weights, base_value, unit_prices, bought)

    _log.info(
        "sip.instalment_allocated",
        instalment=str(instalment),
        carried_in=str(carried_in),
        deployed=str(instalment + carried_in - remaining),
        residual_cash=str(remaining),
        names_bought=len(orders),
        tracking_drift=str(sum((abs(d.drift) for d in drifts), _ZERO)),
    )
    return SipAllocation(
        instalment=instalment,
        carried_in=carried_in,
        orders=orders,
        residual_cash=remaining,
        drifts=drifts,
    )


def _final_drifts(
    isins: list[str],
    weights: Mapping[str, Decimal],
    base_value: Mapping[str, Decimal],
    unit_prices: Mapping[str, Decimal],
    bought: Mapping[str, int],
) -> tuple[WeightDrift, ...]:
    """Per-name actual-vs-target weights on the invested book after the buy (cash excluded)."""
    values = {
        isin: base_value.get(isin, _ZERO) + unit_prices[isin] * bought[isin] for isin in isins
    }
    total = sum(values.values(), _ZERO)
    return tuple(
        WeightDrift(
            isin=isin,
            target_weight=weights[isin],
            actual_weight=(values[isin] / total if total > _ZERO else _ZERO),
        )
        for isin in isins
    )


# ── input validation ─────────────────────────────────────────────────────────────────────────────


def _require_nonneg_money(name: str, amount: object) -> Decimal:
    if not isinstance(amount, Decimal):
        raise SipError(
            f"{name} must be a Decimal — money is never float (CLAUDE.md), got {amount!r}"
        )
    if amount < _ZERO:
        raise SipError(f"{name} must not be negative, got {amount}")
    return amount


def _validated_weights(targets: Mapping[str, Decimal]) -> dict[str, Decimal]:
    if not targets:
        raise SipError("targets is empty — an instalment needs a model to allocate against")
    weights: dict[str, Decimal] = {}
    for isin, weight in targets.items():
        if not re.fullmatch(_ISIN_PATTERN, isin):
            raise SipError(f"target key is not an ISIN: {isin!r}")
        if not isinstance(weight, Decimal):
            raise SipError(f"weight for {isin} must be a Decimal, got {weight!r}")
        if weight <= _ZERO:
            raise SipError(f"weight for {isin} must be positive, got {weight}")
        weights[isin] = weight
    total = sum(weights.values(), _ZERO)
    if abs(total - _ONE) > _WEIGHT_SUM_TOLERANCE:
        raise SipError(f"target weights must sum to 1 (±{_WEIGHT_SUM_TOLERANCE}), got {total}")
    return weights


def _validated_prices(
    weights: Mapping[str, Decimal], prices: Mapping[str, Decimal]
) -> dict[str, Decimal]:
    unit_prices: dict[str, Decimal] = {}
    for isin in weights:
        price = prices.get(isin)
        if price is None:
            raise SipError(
                f"no price for target {isin} — cannot size a whole-share order without it"
            )
        if not isinstance(price, Decimal):
            raise SipError(f"price for {isin} must be a Decimal, got {price!r}")
        if price <= _ZERO:
            raise SipError(f"price for {isin} must be positive, got {price}")
        unit_prices[isin] = price
    return unit_prices


def _validated_existing(existing_value: Mapping[str, Decimal] | None) -> dict[str, Decimal]:
    if existing_value is None:
        return {}
    validated: dict[str, Decimal] = {}
    for isin, value in existing_value.items():
        if not re.fullmatch(_ISIN_PATTERN, isin):
            raise SipError(f"existing_value key is not an ISIN: {isin!r}")
        if not isinstance(value, Decimal):
            raise SipError(f"existing_value for {isin} must be a Decimal, got {value!r}")
        if value < _ZERO:
            raise SipError(f"existing_value for {isin} must not be negative, got {value}")
        validated[isin] = value
    return validated
