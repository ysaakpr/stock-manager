"""The daily forecast policy: what the cost bar, the turnover budget and the exit rules actually do.

The policy's whole claim is that deciding every session is affordable because *turnover* rather than
cadence is budgeted, and that a trade happens only when a projection clears the friction it costs.
Each test below pins one half of that, plus the four exit rules and the two situations the policy
must not confuse — "the model has no view on this name" and "the model has no view at all".

Offline and clockless: a fake broker supplies holdings and free cash, a stub data source supplies
the session's records, and the point-in-time context is the real one, so a leaked record trips the
real guard rather than a test's imitation of it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.policies.forecast_daily import (
    ForecastDailyParameters,
    ForecastDailyPolicy,
    ForecastRecord,
)
from backtest.replay import SessionContext
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitContext, PitError
from execution.broker import Exchange, Holding, Margins, Side

SESSION = date(2024, 3, 1)
LATER = date(2024, 4, 15)  # ~45 calendar days on, past min_hold and short of max_hold
MUCH_LATER = date(2024, 6, 10)  # ~101 days on, past max_hold_sessions=63


class _Data:
    """A stub source: fixed records and fixed marks, both knowable on the session asked for."""

    def __init__(
        self,
        records: Sequence[ForecastRecord] = (),
        marks: Mapping[str, Decimal] | None = None,
    ) -> None:
        self._records = tuple(records)
        self._marks = dict(marks or {})

    def signal(self, as_of: date) -> Dataset[ForecastRecord]:
        return Dataset.declaring(
            f"forecast@{as_of.isoformat()}",
            self._records,
            knowable_date=lambda record: record.knowable_date,
        )

    def marks(self, as_of: date) -> Mapping[str, Decimal]:
        return self._marks

    def move(
        self,
        records: Sequence[ForecastRecord] = (),
        marks: Mapping[str, Decimal] | None = None,
    ) -> None:
        """Advance the market between sessions: new records, new closes. The stub is the test's."""
        self._records = tuple(records)
        self._marks = dict(marks or {})


class _LeakingData(_Data):
    """A source whose records claim to be knowable a year after the session — a leak on purpose."""

    def signal(self, as_of: date) -> Dataset[ForecastRecord]:
        future = as_of + timedelta(days=365)
        leaked = tuple(
            ForecastRecord(
                isin=r.isin,
                expected_return=r.expected_return,
                price=r.price,
                knowable_date=future,
            )
            for r in self._records
        )
        return Dataset.declaring("leak", leaked, knowable_date=lambda record: record.knowable_date)


class _FakeBroker:
    """The `Broker` read surface the policy uses: holdings and free cash. Records nothing."""

    def __init__(self, *, cash: Decimal = Decimal("0"), holdings: tuple[Holding, ...] = ()) -> None:
        self._cash = cash
        self._holdings = holdings

    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def margins(self) -> Margins:
        return Margins(available=self._cash, utilised=Decimal("0"))


def _ctx(session: date, broker: _FakeBroker) -> SessionContext:
    return SessionContext(
        session=session,
        pit=PitContext(as_of=session),
        broker=broker,  # type: ignore[arg-type]  # the fake satisfies the read surface used
        clock=FrozenClock(session),
    )


def _record(isin: str, edge: str, price: str = "100", *, session: date = SESSION) -> ForecastRecord:
    return ForecastRecord(
        isin=isin,
        expected_return=Decimal(edge),
        price=Decimal(price),
        knowable_date=session,
    )


def _holding(isin: str, quantity: int = 100, price: str = "100") -> Holding:
    return Holding(
        isin=isin, exchange=Exchange.NSE, quantity=quantity, average_price=Decimal(price)
    )


def _isins(n: int) -> list[str]:
    """`n` distinct valid ISINs, ordered, so rank order and ISIN order are the same."""
    return [f"INE{i:03d}A01010" for i in range(1, n + 1)]


# ── the cost bar ─────────────────────────────────────────────────────────────────────────────────


def test_a_projection_under_the_bar_buys_nothing() -> None:
    """Free cash and a positive projection are not enough — it has to beat the friction.

    The default bar is twice a 0.3 % round trip, so a name projecting +0.5 % over the horizon is
    expected to make money and is still not worth buying. That is the entire reason the forecast is
    in return units instead of a rank: a rank cannot be compared with a cost.
    """
    params = ForecastDailyParameters()
    assert params.entry_bar == Decimal("0.006")

    policy = ForecastDailyPolicy(_Data([_record("INE001A01010", "0.005")]), params)
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))

    assert decision.orders == ()
    summary = decision.evidence.items[0].text
    assert summary is not None and summary.endswith("nothing cleared the bar")


def test_a_projection_over_the_bar_buys_the_best_first() -> None:
    """Over the bar, the budget is spent on the highest projections, ties by ISIN."""
    records = [
        _record("INE001A01010", "0.010"),
        _record("INE002A01010", "0.050"),
        _record("INE003A01010", "0.030"),
    ]
    policy = ForecastDailyPolicy(_Data(records), ForecastDailyParameters())

    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))

    bought = [order.isin for order in decision.orders]
    assert bought == ["INE002A01010", "INE003A01010"]  # 5 % then 3 %; the 1 % misses the budget
    assert all(order.side is Side.BUY for order in decision.orders)


# ── the turnover budget ──────────────────────────────────────────────────────────────────────────


def test_the_budget_caps_what_a_session_can_do() -> None:
    """Twenty names clearing the bar still buy only `max_trades_per_session` of them.

    This is the parameter that makes a daily cadence affordable at all: without it, a session that
    liked twenty names would pay twenty round trips, and 252 such sessions would pay ~75 % of
    capital a year in friction.
    """
    records = [_record(isin, "0.050") for isin in _isins(20)]
    policy = ForecastDailyPolicy(_Data(records), ForecastDailyParameters())

    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("10000000"))))

    assert len(decision.orders) == 2


def test_a_stop_is_exempt_from_the_budget() -> None:
    """A risk rule a turnover budget can cancel is not a risk rule.

    Two holdings are already releasing on their projections — which is the whole budget — and a
    third has breached its trailing stop. The stop fires anyway, so the session does three sells.
    """
    isins = _isins(3)
    params = ForecastDailyParameters()
    data = _Data(
        [_record(isin, "-0.010") for isin in isins],
        marks={isins[0]: Decimal("100"), isins[1]: Decimal("100"), isins[2]: Decimal("100")},
    )
    policy = ForecastDailyPolicy(data, params)
    broker = _FakeBroker(holdings=tuple(_holding(isin) for isin in isins))
    policy.decide(_ctx(SESSION, broker))  # seeds entry dates and peaks at 100

    # The third name halves, well past the 25 % stop; the others hold their price. The stub is the
    # test's own object, swapped in to move the market between sessions.
    data.move(
        [_record(isin, "-0.010", session=LATER) for isin in isins],
        marks={isins[0]: Decimal("100"), isins[1]: Decimal("100"), isins[2]: Decimal("50")},
    )
    decision = policy.decide(_ctx(LATER, broker))

    sold = [order.isin for order in decision.orders]
    assert len(sold) == 3, "the stop was budgeted away"
    assert isins[2] in sold
    stop_entries = [e for e in decision.entries if "trailing stop" in (e.rationale or "")]
    assert len(stop_entries) == 1


# ── the exit rules ───────────────────────────────────────────────────────────────────────────────


def test_the_band_carries_a_name_between_the_bars() -> None:
    """A holding projecting between the release bar and the entry bar is neither sold nor topped up.

    The gap between the two bars is the hysteresis: without it, a name oscillating around a single
    threshold would be bought and sold repeatedly, paying the round trip each time for no change of
    view.
    """
    isin = "INE001A01010"
    params = ForecastDailyParameters()
    data = _Data([_record(isin, "0.004")])
    policy = ForecastDailyPolicy(data, params)
    broker = _FakeBroker(cash=Decimal("1000000"), holdings=(_holding(isin),))
    policy.decide(_ctx(SESSION, broker))

    data.move([_record(isin, "0.004", session=LATER)])
    decision = policy.decide(_ctx(LATER, broker))

    assert params.exit_bar <= Decimal("0.004") < params.entry_bar
    assert decision.orders == ()


def test_nothing_but_the_stop_sells_inside_the_minimum_hold() -> None:
    """A name entered this session is not discarded the next, however bad its projection turns."""
    isin = "INE001A01010"
    data = _Data([_record(isin, "-0.500")])
    policy = ForecastDailyPolicy(data, ForecastDailyParameters())
    broker = _FakeBroker(holdings=(_holding(isin),))
    policy.decide(_ctx(SESSION, broker))  # stamps the entry session

    next_session = SESSION + timedelta(days=1)
    data.move([_record(isin, "-0.500", session=next_session)])
    decision = policy.decide(_ctx(next_session, broker))

    assert decision.orders == ()


def test_the_max_hold_re_underwrites_rather_than_liquidates() -> None:
    """At `max_hold` a name still clearing the entry bar is carried; one that is not is released.

    Forcing out a name the model still ranks would pay a round trip to buy it straight back, so the
    age rule is a re-underwrite and not an expiry.
    """
    isin = "INE001A01010"
    keeps_data = _Data([_record(isin, "0.050")])
    keeps = ForecastDailyPolicy(keeps_data, ForecastDailyParameters())
    broker = _FakeBroker(holdings=(_holding(isin),))
    keeps.decide(_ctx(SESSION, broker))
    keeps_data.move([_record(isin, "0.050", session=MUCH_LATER)])
    assert keeps.decide(_ctx(MUCH_LATER, broker)).orders == ()

    fades_data = _Data([_record(isin, "0.050")])
    fades = ForecastDailyPolicy(fades_data, ForecastDailyParameters())
    fades.decide(_ctx(SESSION, broker))
    fades_data.move([_record(isin, "0.004", session=MUCH_LATER)])
    released = fades.decide(_ctx(MUCH_LATER, broker))
    assert [o.side for o in released.orders] == [Side.SELL]
    assert "re-underwrite failed" in (released.entries[0].rationale or "")


def test_there_is_no_profit_target() -> None:
    """A name that has doubled is held while the model still expects a return from it.

    Truncating winners is how a momentum-shaped payoff is destroyed, so a paper profit is not a
    reason to sell. The only price-based exit is the wide trailing stop, and a name at a new high
    has not breached it.
    """
    isin = "INE001A01010"
    data = _Data([_record(isin, "0.050")], marks={isin: Decimal("100")})
    policy = ForecastDailyPolicy(data, ForecastDailyParameters())
    broker = _FakeBroker(holdings=(_holding(isin),))
    policy.decide(_ctx(SESSION, broker))

    data.move([_record(isin, "0.050", price="200", session=LATER)], marks={isin: Decimal("200")})
    decision = policy.decide(_ctx(LATER, broker))

    assert decision.orders == ()


# ── the two absences the policy must not confuse ─────────────────────────────────────────────────


def test_no_view_at_all_holds_the_book() -> None:
    """An empty cross-section is "the model has not matured", not "sell everything".

    The expanding-window fit produces nothing until enough targets have matured, so every run passes
    through this state. Liquidating a book because the model is not ready yet would turn a data
    property into a trade.
    """
    isin = "INE001A01010"
    data = _Data([_record(isin, "0.050")])
    policy = ForecastDailyPolicy(data, ForecastDailyParameters())
    broker = _FakeBroker(holdings=(_holding(isin),))
    policy.decide(_ctx(SESSION, broker))

    data.move([])  # no model this session
    decision = policy.decide(_ctx(LATER, broker))

    assert decision.orders == ()


def test_a_name_missing_from_a_real_cross_section_is_released() -> None:
    """With a view on other names but none on this one, the holding has left the universe."""
    held, other = _isins(2)
    data = _Data([_record(held, "0.050")])
    policy = ForecastDailyPolicy(data, ForecastDailyParameters())
    broker = _FakeBroker(holdings=(_holding(held),))
    policy.decide(_ctx(SESSION, broker))

    data.move([_record(other, "0.050", session=LATER)])
    decision = policy.decide(_ctx(LATER, broker))

    assert [(o.isin, o.side) for o in decision.orders] == [(held, Side.SELL)]
    assert "left the investable universe" in (decision.entries[0].rationale or "")


# ── point-in-time ────────────────────────────────────────────────────────────────────────────────


def test_a_record_knowable_after_the_session_trips_the_guard() -> None:
    """The policy reads only through `ctx.pit.admit`, so a leak raises instead of trading.

    The real risk in a *fitted* policy is upstream — coefficients told the future — and that is
    guarded where the dates are (`backtest.forecast`). This asserts the second line of defence: a
    record stamped with a knowable date after the session cannot reach a decision.
    """
    policy = ForecastDailyPolicy(
        _LeakingData([_record("INE001A01010", "0.050")]), ForecastDailyParameters()
    )

    with pytest.raises(PitError):
        policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))


# ── the parameters refuse an incoherent setting ──────────────────────────────────────────────────


def test_a_release_bar_above_the_entry_bar_is_refused() -> None:
    """An exit bar tighter than the entry bar would sell what it just bought, every session."""
    with pytest.raises(ValueError, match="exit_cost_multiple"):
        ForecastDailyParameters(entry_cost_multiple=Decimal("1"), exit_cost_multiple=Decimal("2"))


def test_a_float_bar_is_refused() -> None:
    """A bar compared against money is a Decimal, like the money it is compared with."""
    with pytest.raises(TypeError, match="never float"):
        ForecastDailyParameters(round_trip_cost=0.003)  # type: ignore[arg-type]


def test_a_new_position_is_sized_to_one_top_n_th_of_the_book() -> None:
    """Two fills a session must not build a two-name book and call it a twenty-name one.

    With a turnover budget this small, spending all free cash on the day's picks would put ~50 % of
    the portfolio into each of two names — and report a concentrated book's returns as a
    diversified strategy's. The instalment is capped at the per-name target instead, so the book
    fills toward `top_n` over the sessions the budget allows.
    """
    params = ForecastDailyParameters()
    records = [_record(isin, "0.050", price="100") for isin in _isins(5)]
    policy = ForecastDailyPolicy(_Data(records), params)

    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))

    assert len(decision.orders) == 2
    # ₹10 lakh over top_n = 20 is ₹50,000 a name, so ~500 shares at ₹100 — not ~4,900.
    for order in decision.orders:
        assert order.quantity == pytest.approx(500, abs=5)
    deployed = sum(order.quantity for order in decision.orders) * 100
    assert deployed < Decimal("120000"), "the session deployed more than two names' worth"
