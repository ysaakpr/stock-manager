"""M5.2 / EXECUTION_PLAN 8.3.4: no generated order stream can breach a cap.

The unit tests show A8 blocks the specific orders they hand it. That is not enough for a rail: a
rail's claim is universal — *no* order it lets through leaves the book over a cap, whatever the
sequence — and a rail with a bug that only bites on the third buy of one sector passes every
hand-written case and fails in production. So this file generates the sequences.

The method is the one §8.3.4 names. A universe of instruments with fixed prices, a ratified set of
rails, and a `hypothesis`-generated stream of buy/sell intents. Each intent is cleared through
`check_order`; the allowed ones are applied with `apply_order`; and after every step the resulting
book is asserted to satisfy every cap. Because prices are fixed, case value is invariant under
trades (a buy moves cash into a lot at the same price it is marked at), so the percentages the
rails bound move only when the name they concern trades — which is exactly when the engine
re-checks them. If the engine's arithmetic and the property's disagree, this file finds the seed.

Two invariants are asserted after every allowed step:

* **Position and sector caps hold for every lot and every sector**, not only the traded one — an
  off-by-one that checked the wrong lot would pass the traded-lot case and fail here.
* **The minimum-holdings floor, once reached, is never breached by a sell.** A book below the floor
  is still being built and the rail does not bite; but the step it crosses the floor upward, it may
  never cross back down through a sell.

`apply_order` and `check_order` are pure, so nothing here touches a database, a clock or the
network (CLAUDE.md): the property is over the logic, and the journalling half is the unit file's.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from analyst.cases import RiskRails
from analyst.rails import Portfolio, ProposedOrder, apply_order, assess_drawdown, check_order
from analyst.rails.policies import drawdown_of
from execution.broker import Exchange, OrderRequest, Side

# A fixed universe: eight names across three sectors, each at a whole-rupee price. Fixed so that
# case value is invariant under trades and the caps move only when their name trades — the property
# the whole test rests on. ISINs are shape-valid (D2's check digit is not this test's concern).
_UNIVERSE: tuple[tuple[str, str, Decimal], ...] = (
    ("INE001A01001", "IT", Decimal("100")),
    ("INE002A01009", "IT", Decimal("250")),
    ("INE003A01007", "IT", Decimal("500")),
    ("INE004A01005", "PHARMA", Decimal("120")),
    ("INE005A01003", "PHARMA", Decimal("300")),
    ("INE006A01001", "PHARMA", Decimal("75")),
    ("INE007A01009", "ENERGY", Decimal("200")),
    ("INE008A01007", "ENERGY", Decimal("90")),
)

#: Starting cash. Large relative to the caps so the book can be driven hard into a single name and
#: the rails, not affordability, are what stops it.
_INITIAL_CASH = Decimal("10000000")


@dataclass(frozen=True, slots=True)
class _Intent:
    """A generated trade intent, before it is clamped to what the book can execute."""

    index: int
    buy: bool
    quantity: int


_intents = st.lists(
    st.builds(
        _Intent,
        index=st.integers(min_value=0, max_value=len(_UNIVERSE) - 1),
        buy=st.booleans(),
        quantity=st.integers(min_value=1, max_value=20000),
    ),
    max_size=60,
)


# Rails drawn inside the model's own constraints: max_position_pct x min_holdings must reach 100%
# (else no fully-invested book satisfies both and every order blocks), and max_sector_pct must be
# at least max_position_pct. min_holdings is kept small enough that the eight-name universe can
# actually reach the floor, so the min-holdings rail is exercised rather than vacuously true.
@st.composite
def _rails(draw: st.DrawFn) -> RiskRails:
    min_holdings = draw(st.integers(min_value=2, max_value=5))
    # Smallest position cap that admits the floor, plus some headroom, capped at 100.
    floor_cap = (100 + min_holdings - 1) // min_holdings
    max_position = draw(st.integers(min_value=floor_cap, max_value=100))
    max_sector = draw(st.integers(min_value=max_position, max_value=100))
    return RiskRails(
        max_position_pct=Decimal(max_position),
        max_sector_pct=Decimal(max_sector),
        min_holdings=min_holdings,
        drawdown_review_pct=Decimal("25"),
        # Per-order sanity caps set generous, so the concentration caps are what bind and get
        # exercised; the per-order caps have their own targeted unit tests.
        max_order_value_inr=Decimal("100000000"),
        max_order_pct_of_case=Decimal("100"),
    )


def _position_pct(value: Decimal, total: Decimal) -> Decimal:
    """The same percentage the engine computes — identical rounding, so the two never disagree."""
    if total <= 0:
        return Decimal(0)
    return value * Decimal(100) / total


@given(intents=_intents, rails=_rails())
@settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
def test_no_allowed_order_ever_breaches_a_cap(intents: list[_Intent], rails: RiskRails) -> None:
    """Clear a generated stream; assert the book never sits over a cap after an allowed order."""
    book = Portfolio(case_id="PROP", lots=(), cash=_INITIAL_CASH)
    reached_floor = book.holding_count >= rails.min_holdings

    for intent in intents:
        isin, sector, price = _UNIVERSE[intent.index]
        if intent.buy:
            affordable = int(book.cash // price)
            quantity = min(intent.quantity, affordable)
            if quantity <= 0:
                continue
            side = Side.BUY
        else:
            held = book.lot(isin)
            if held is None:
                continue
            quantity = min(intent.quantity, held.quantity)
            if quantity <= 0:  # pragma: no cover - a held lot always has a positive quantity
                continue
            side = Side.SELL

        order = ProposedOrder(
            request=OrderRequest(isin=isin, side=side, quantity=quantity, exchange=Exchange.NSE),
            price=price,
            sector=sector,
        )
        assessment = check_order(order, book, rails)
        if assessment.allowed:
            book = apply_order(book, order)

        # ── invariants, after every step ──────────────────────────────────────────────────────
        total = book.total_value
        for lot in book.lots:
            assert _position_pct(lot.value, total) <= rails.max_position_pct, (
                f"{lot.isin} at {_position_pct(lot.value, total)}% over {rails.max_position_pct}%"
            )
        for held_sector in {lot.sector for lot in book.lots}:
            sector_pct = _position_pct(book.sector_value(held_sector), total)
            assert sector_pct <= rails.max_sector_pct, (
                f"sector {held_sector} at {sector_pct}% over {rails.max_sector_pct}%"
            )

        if book.holding_count >= rails.min_holdings:
            reached_floor = True
        if reached_floor:
            assert book.holding_count >= rails.min_holdings, (
                f"holdings fell to {book.holding_count}, below the floor of {rails.min_holdings}"
            )


@given(
    values=st.lists(
        st.integers(min_value=1, max_value=10_000_000).map(Decimal),
        min_size=1,
        max_size=40,
    )
)
@settings(max_examples=200)
def test_drawdown_matches_the_worst_peak_to_trough(values: list[Decimal]) -> None:
    """`assess_drawdown` reports exactly the deepest peak-to-trough fall in the series.

    Cross-checked against a brute-force scan of every (peak, later value) pair, so an off-by-one in
    the running-peak walk (missing the last point, or resetting the peak on a dip) is caught.
    """
    rails = RiskRails(
        max_position_pct=Decimal("15"),
        max_sector_pct=Decimal("35"),
        min_holdings=8,
        drawdown_review_pct=Decimal("25"),
        max_order_value_inr=Decimal("1000000"),
        max_order_pct_of_case=Decimal("20"),
    )
    status = assess_drawdown(values, rails)

    worst = Decimal(0)
    for i, peak in enumerate(values):
        for trough in values[i:]:
            fall = drawdown_of(peak, trough)
            worst = max(worst, fall)

    assert status.drawdown_pct == worst
    assert status.review_forced == (worst >= rails.drawdown_review_pct)


@given(
    peak=st.integers(min_value=1, max_value=10_000_000).map(Decimal),
    fraction=st.integers(min_value=0, max_value=100),
)
@settings(max_examples=200)
def test_a_25pct_fall_always_forces_review(peak: Decimal, fraction: int) -> None:
    """Any peak-to-trough fall of 25% or more trips the forced review; anything less does not.

    Acceptance criterion 2, as a property rather than one example: a trough set to a chosen
    fraction of the peak lands on both sides of the -25% line, and the verdict follows the number.
    """
    rails = RiskRails(
        max_position_pct=Decimal("15"),
        max_sector_pct=Decimal("35"),
        min_holdings=8,
        drawdown_review_pct=Decimal("25"),
        max_order_value_inr=Decimal("1000000"),
        max_order_pct_of_case=Decimal("20"),
    )
    trough = peak * Decimal(fraction) / Decimal(100)
    status = assess_drawdown([peak, trough], rails)
    fall = drawdown_of(peak, trough)
    assert status.drawdown_pct == fall
    assert status.review_forced == (fall >= Decimal("25"))
