"""M4.4 — the shared Indian transaction cost model, and invariant #4's guard.

Three things are under test here:

1. **The arithmetic**, against Zerodha's own published figures. The worked example is the one in
   their STT support article — buy 500 shares at ₹100 and sell 500 at ₹105, NSE delivery — whose
   STT lines Zerodha states as ₹50 and ₹53. Every other line is computed by hand below from the
   rate card published at zerodha.com/charges and read on 2026-08-08, and asserted to the paisa.
2. **Date effectiveness.** The same trade in 2018 pays 2018's rates: a different exchange charge,
   a different SEBI fee, a GST base that excluded the SEBI fee, stamp duty on *both* legs at the
   account's state rate, and the DP charge of the period. A cost model that quietly prices 2018
   with today's card would make every backtest a claim about a market that did not exist.
3. **Invariant #4** — one cost model. The guard at the bottom greps the repo: no second module may
   define a cost rate or compute a charge, and no other YAML may hold a rate table.
4. **The pre-GST era** (W0-7). The card used to start at 2017-07-01, so a full-span backtest over
   the existing lake raised on its first fill. Ten schedules now reach back to 2006-01-01, and the
   section near the bottom asserts both the arithmetic of two representative eras and that each
   boundary is on its statutory day — the test that fails if an era is off by one, or if a rate
   silently reverts to the modern one.

Nothing here touches the network or the clock; the trade date is an input.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
import yaml
from pydantic import ValidationError

from execution.costs import (
    CostBreakdown,
    CostModel,
    Exchange,
    MissingAccountStateError,
    NoRateScheduleError,
    RateCard,
    Side,
    Trade,
    UnknownStampDutyStateError,
    load_rate_card,
)
from execution.costs.model import RATES_PATH, UnknownExchangeError

# The tokenizer that blanks comments and string literals, so prose about a charge is never read as
# an implementation of one. Shared with the clock guard rather than copied, because the two guards
# must agree on what counts as "code".
from tests.unit.test_clock_guard import SKIPPED_DIRS, code_lines

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Infosys. Costs never join on a symbol (invariant #2), and the DP charge is per scrip per day.
INFY: Final[str] = "INE009A01021"
TCS: Final[str] = "INE467B01029"


@pytest.fixture(scope="module")
def card() -> RateCard:
    return load_rate_card()


@pytest.fixture(scope="module")
def model(card: RateCard) -> CostModel:
    """Today's model. No account state: the uniform stamp-duty regime does not need one."""
    return CostModel(rate_card=card)


@pytest.fixture(scope="module")
def model_2018(card: RateCard) -> CostModel:
    """A Karnataka account, which is what the pre-2020 stamp-duty era needs to price a trade."""
    return CostModel(rate_card=card, account_state="KA")


def buy(on: date, *, price: str = "100", quantity: int = 500, isin: str = INFY) -> Trade:
    return Trade(isin=isin, trade_date=on, side=Side.BUY, quantity=quantity, price=Decimal(price))


def sell(on: date, *, price: str = "105", quantity: int = 500, isin: str = INFY) -> Trade:
    return Trade(isin=isin, trade_date=on, side=Side.SELL, quantity=quantity, price=Decimal(price))


# ── 1. the contract note, to the paisa ───────────────────────────────────────────────────────
#
# Rate card in force (zerodha.com/charges, read 2026-08-08): brokerage ₹0, STT 0.1% both sides
# rounded to the rupee, NSE transaction charge 0.00307%, SEBI ₹10/crore, GST 18% on
# (brokerage + transaction + SEBI), stamp duty 0.015% buy side rounded to the rupee, DP ₹15.34 per
# scrip on sell (₹3.50 CDSL + ₹9.50 Zerodha + ₹2.34 GST).

TODAY: Final[date] = date(2026, 8, 7)

# Buy 500 @ ₹100 = ₹50,000 turnover.
#   STT      0.1%      x 50,000 = 50.00      → ₹50   (Zerodha's published figure)
#   NSE txn  0.00307%  x 50,000 =  1.535     → ₹1.54
#   SEBI     ₹10/crore x 50,000 =  0.05      → ₹0.05
#   GST      18%       x (0 + 1.54 + 0.05) = 0.2862 → ₹0.29
#   Stamp    0.015%    x 50,000 =  7.50      → ₹8    (half up)
#   Total ₹59.88, payable ₹50,059.88
EXPECTED_BUY: Final[dict[str, Decimal]] = {
    "brokerage": Decimal("0.00"),
    "securities_transaction_tax": Decimal("50"),
    "exchange_transaction_charge": Decimal("1.54"),
    "sebi_turnover_fee": Decimal("0.05"),
    "goods_and_services_tax": Decimal("0.29"),
    "stamp_duty": Decimal("8"),
    "depository_charge": Decimal("0"),
}

# Sell 500 @ ₹105 = ₹52,500 turnover.
#   STT      0.1%      x 52,500 = 52.50      → ₹53   (Zerodha's published figure: 52.50 rounds up)
#   NSE txn  0.00307%  x 52,500 =  1.61175   → ₹1.61
#   SEBI     ₹10/crore x 52,500 =  0.0525    → ₹0.05
#   GST      18%       x (0 + 1.61 + 0.05) = 0.2988 → ₹0.30
#   Stamp    buy side only                   → ₹0
#   DP       (3.50 + 9.50) x 1.18 = 15.34    → ₹15.34
#   Total ₹70.30, receivable ₹52,429.70
EXPECTED_SELL: Final[dict[str, Decimal]] = {
    "brokerage": Decimal("0.00"),
    "securities_transaction_tax": Decimal("53"),
    "exchange_transaction_charge": Decimal("1.61"),
    "sebi_turnover_fee": Decimal("0.05"),
    "goods_and_services_tax": Decimal("0.30"),
    "stamp_duty": Decimal("0"),
    "depository_charge": Decimal("15.34"),
}


def lines(breakdown: CostBreakdown) -> dict[str, Decimal]:
    """The charge lines of a breakdown, keyed the way the expected tables above are."""
    return {
        "brokerage": breakdown.brokerage,
        "securities_transaction_tax": breakdown.securities_transaction_tax,
        "exchange_transaction_charge": breakdown.exchange_transaction_charge,
        "sebi_turnover_fee": breakdown.sebi_turnover_fee,
        "goods_and_services_tax": breakdown.goods_and_services_tax,
        "stamp_duty": breakdown.stamp_duty,
        "depository_charge": breakdown.depository_charge,
    }


def test_buy_reconciles_line_by_line(model: CostModel) -> None:
    charged = model.charge(buy(TODAY))
    assert lines(charged) == EXPECTED_BUY
    assert charged.turnover == Decimal("50000")
    assert charged.total == Decimal("59.88")
    assert charged.net_amount == Decimal("50059.88")


def test_sell_reconciles_line_by_line(model: CostModel) -> None:
    charged = model.charge(sell(TODAY))
    assert lines(charged) == EXPECTED_SELL
    assert charged.turnover == Decimal("52500")
    assert charged.total == Decimal("70.30")
    assert charged.net_amount == Decimal("52429.70")


def test_round_trip_cost_is_the_sum_of_both_notes(model: CostModel) -> None:
    """₹5,000 gross profit on the example trade costs ₹130.18 to realise."""
    bought = model.charge(buy(TODAY))
    sold = model.charge(sell(TODAY))
    assert bought.total + sold.total == Decimal("130.18")
    assert sold.net_amount - bought.net_amount == Decimal("2369.82")


def test_stt_rounds_half_up_to_the_whole_rupee(model: CostModel) -> None:
    """Zerodha's worked example: ₹50.00 stays ₹50 and ₹52.50 becomes ₹53, not ₹52."""
    assert model.charge(buy(TODAY)).securities_transaction_tax == Decimal("50")
    assert model.charge(sell(TODAY)).securities_transaction_tax == Decimal("53")


def test_every_charge_is_a_decimal(model: CostModel) -> None:
    """A float anywhere in the cost model is a bug (CLAUDE.md), including in its output."""
    for name, amount in lines(model.charge(sell(TODAY))).items():
        assert isinstance(amount, Decimal), name


# ── inversions the arithmetic must not survive ───────────────────────────────────────────────


def test_stamp_duty_is_charged_on_the_buy_leg_only(model: CostModel) -> None:
    assert model.charge(buy(TODAY)).stamp_duty == Decimal("8")
    assert model.charge(sell(TODAY)).stamp_duty == Decimal("0")


def test_the_dp_charge_is_levied_on_the_sell_leg_only(model: CostModel) -> None:
    assert model.charge(buy(TODAY)).depository_charge == Decimal("0")
    assert model.charge(sell(TODAY)).depository_charge == Decimal("15.34")


def test_gst_is_levied_on_the_fee_lines_and_not_on_the_taxes(model: CostModel) -> None:
    """GST applies to brokerage, transaction and SEBI charges — never to STT or stamp duty.

    Taxing the whole bill instead would turn ₹0.29 into ₹10.37 on this trade, so this assertion
    fails loudly if the base is ever widened by accident.
    """
    charged = model.charge(buy(TODAY))
    fees = charged.brokerage + charged.exchange_transaction_charge + charged.sebi_turnover_fee
    assert charged.goods_and_services_tax == (fees * Decimal("0.18")).quantize(Decimal("0.01"))
    assert charged.goods_and_services_tax < charged.securities_transaction_tax


def test_the_dp_charge_is_flat_in_quantity_but_the_rest_scales(model: CostModel) -> None:
    one_lot = model.charge(sell(TODAY, quantity=500))
    ten_lots = model.charge(sell(TODAY, quantity=5000))  # ₹525,000 turnover
    assert ten_lots.depository_charge == one_lot.depository_charge == Decimal("15.34")
    # Every other line scales with turnover, up to its own rounding: STT 525.00 → ₹525 (where one
    # lot's 52.50 rounded up to 53), transaction charge 16.1175 → ₹16.12, SEBI 0.525 → ₹0.53.
    assert ten_lots.securities_transaction_tax == Decimal("525")
    assert ten_lots.exchange_transaction_charge == Decimal("16.12")
    assert ten_lots.sebi_turnover_fee == Decimal("0.53")
    assert ten_lots.goods_and_services_tax == Decimal("3.00")  # 18% x (16.12 + 0.53)


def test_bse_costs_more_per_trade_than_nse_today(model: CostModel) -> None:
    """The exchange is a real input: BSE's 0.00375% is above NSE's 0.00307%."""
    on_bse = Trade(
        isin=INFY,
        trade_date=TODAY,
        side=Side.BUY,
        quantity=500,
        price=Decimal("100"),
        exchange=Exchange.BSE,
    )
    assert model.charge(on_bse).exchange_transaction_charge == Decimal("1.88")  # 0.00375% x 50,000


# ── 2. the rates are dated ───────────────────────────────────────────────────────────────────
#
# 2018 rate card: NSE 0.00325%, SEBI ₹15/crore, GST 18% on (brokerage + transaction) only, stamp
# duty state-wise (Karnataka 0.01%) on BOTH legs, DP ₹13.50 + 18% GST = ₹15.93.

IN_2018: Final[date] = date(2018, 4, 2)

# Buy 500 @ ₹100 = ₹50,000.  STT 50 · txn 1.625→1.63 · SEBI 0.075→0.08 · GST 18%x1.63=0.2934→0.29
#   · stamp 0.01% x 50,000 = 5.00 → ₹5.  Total ₹57.00
EXPECTED_BUY_2018: Final[dict[str, Decimal]] = {
    "brokerage": Decimal("0.00"),
    "securities_transaction_tax": Decimal("50"),
    "exchange_transaction_charge": Decimal("1.63"),
    "sebi_turnover_fee": Decimal("0.08"),
    "goods_and_services_tax": Decimal("0.29"),
    "stamp_duty": Decimal("5"),
    "depository_charge": Decimal("0"),
}

# Sell 500 @ ₹105 = ₹52,500. STT 53 · txn 1.70625→1.71 · SEBI 0.07875→0.08 · GST 18%x1.71→0.31
#   · stamp 0.01% x 52,500 = 5.25 → ₹5 · DP 13.50 x 1.18 = 15.93.  Total ₹76.03
EXPECTED_SELL_2018: Final[dict[str, Decimal]] = {
    "brokerage": Decimal("0.00"),
    "securities_transaction_tax": Decimal("53"),
    "exchange_transaction_charge": Decimal("1.71"),
    "sebi_turnover_fee": Decimal("0.08"),
    "goods_and_services_tax": Decimal("0.31"),
    "stamp_duty": Decimal("5"),
    "depository_charge": Decimal("15.93"),
}


def test_a_2018_buy_uses_2018_rates(model_2018: CostModel) -> None:
    charged = model_2018.charge(buy(IN_2018))
    assert charged.schedule_id == "gst-era-state-stamp"
    assert lines(charged) == EXPECTED_BUY_2018
    assert charged.total == Decimal("57.00")


def test_a_2018_sell_uses_2018_rates(model_2018: CostModel) -> None:
    charged = model_2018.charge(sell(IN_2018))
    assert charged.schedule_id == "gst-era-state-stamp"
    assert lines(charged) == EXPECTED_SELL_2018
    assert charged.total == Decimal("76.03")


def test_2018_and_today_disagree_on_every_dated_line(model_2018: CostModel) -> None:
    """Only STT is unchanged since 2018; every other line moved, and the model must show it."""
    then = lines(model_2018.charge(sell(IN_2018)))
    now = lines(model_2018.charge(sell(TODAY)))
    assert then["securities_transaction_tax"] == now["securities_transaction_tax"]
    for line in ("exchange_transaction_charge", "sebi_turnover_fee", "depository_charge"):
        assert then[line] != now[line], line
    assert then["stamp_duty"] > now["stamp_duty"] == Decimal("0")


def test_stamp_duty_was_charged_on_sells_before_the_uniform_regime(model_2018: CostModel) -> None:
    """The 2020-07-01 change was structural, not a rate tweak: sells stopped paying duty."""
    assert model_2018.charge(sell(date(2020, 6, 30))).stamp_duty == Decimal("5")
    assert model_2018.charge(sell(date(2020, 7, 1))).stamp_duty == Decimal("0")


@pytest.mark.parametrize(
    ("trade_date", "schedule_id"),
    [
        (date(2017, 7, 1), "gst-era-state-stamp"),
        (date(2020, 6, 30), "gst-era-state-stamp"),
        (date(2020, 7, 1), "uniform-stamp-duty"),
        (date(2024, 9, 30), "uniform-stamp-duty"),
        (date(2024, 10, 1), "true-to-label"),
        (date(2025, 3, 31), "true-to-label"),
        (date(2025, 4, 1), "current"),
        (TODAY, "current"),
    ],
)
def test_a_schedule_starts_on_its_effective_date(
    model_2018: CostModel, trade_date: date, schedule_id: str
) -> None:
    assert model_2018.charge(buy(trade_date)).schedule_id == schedule_id


def test_a_trade_before_the_card_starts_raises_rather_than_guessing(model: CostModel) -> None:
    """Still a refusal, just a decade earlier: W0-7 moved the start to 2006-01-01."""
    with pytest.raises(NoRateScheduleError, match="2005-12-31"):
        model.charge(buy(date(2005, 12, 31)))


def test_the_state_era_refuses_to_price_without_an_account_state(model: CostModel) -> None:
    """Defaulting to some state's rate would put a plausible wrong number in every 2018 backtest."""
    with pytest.raises(MissingAccountStateError, match="per state"):
        model.charge(buy(IN_2018))


def test_an_unlisted_state_raises(card: RateCard) -> None:
    with pytest.raises(UnknownStampDutyStateError, match="TN"):
        CostModel(rate_card=card, account_state="TN").charge(buy(IN_2018))


def test_the_account_state_is_irrelevant_once_stamp_duty_is_uniform(card: RateCard) -> None:
    karnataka = CostModel(rate_card=card, account_state="KA").charge(buy(TODAY))
    stateless = CostModel(rate_card=card).charge(buy(TODAY))
    assert karnataka.stamp_duty == stateless.stamp_duty == Decimal("8")


def test_an_exchange_with_no_rate_on_the_schedule_raises(card: RateCard) -> None:
    schedule = card.schedule_for(TODAY)
    stripped = schedule.model_copy(
        update={"exchange_transaction": {Exchange.NSE: Decimal("0.0000307")}}
    )
    with pytest.raises(UnknownExchangeError, match="BSE"):
        stripped.transaction_rate(Exchange.BSE)


# ── the DP charge is per scrip per day, not per trade ────────────────────────────────────────


def test_the_dp_charge_is_levied_once_per_scrip_per_day(model: CostModel) -> None:
    trades = [
        sell(TODAY, quantity=100),
        sell(TODAY, quantity=200),
        sell(TODAY, isin=TCS),
        sell(date(2026, 8, 6), quantity=100),
        buy(TODAY),
    ]
    charged = model.charge_all(trades)
    dp = [breakdown.depository_charge for breakdown in charged]
    assert dp == [
        Decimal("15.34"),  # first Infosys sell of 2026-08-07 carries it
        Decimal("0"),  # second Infosys sell the same day does not
        Decimal("15.34"),  # a different scrip is a different charge
        Decimal("15.34"),  # the same scrip on another day is another charge
        Decimal("0"),  # buys never pay it
    ]


def test_charging_trades_one_by_one_would_over_count_the_dp_charge(model: CostModel) -> None:
    """`charge_all` exists precisely because `charge` prices a trade as if it stood alone."""
    two_sells = [sell(TODAY, quantity=100), sell(TODAY, quantity=200)]
    per_trade = sum(model.charge(t).depository_charge for t in two_sells)
    per_day = sum(b.depository_charge for b in model.charge_all(two_sells))
    assert per_trade == Decimal("30.68")
    assert per_day == Decimal("15.34")


# ── the trade itself is validated ────────────────────────────────────────────────────────────


def test_a_float_price_is_rejected() -> None:
    with pytest.raises(TypeError, match="never float"):
        Trade(isin=INFY, trade_date=TODAY, side=Side.BUY, quantity=1, price=100.5)  # type: ignore[arg-type]


def test_a_fractional_quantity_is_rejected() -> None:
    with pytest.raises(TypeError, match="whole number of shares"):
        Trade(isin=INFY, trade_date=TODAY, side=Side.BUY, quantity=1.5, price=Decimal("100"))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["INFY", "", "INE009A0102", "ine009a01021"])
def test_a_symbol_is_not_an_isin(bad: str) -> None:
    with pytest.raises(ValueError, match="not an ISIN"):
        Trade(isin=bad, trade_date=TODAY, side=Side.BUY, quantity=1, price=Decimal("100"))


# ── the rate card file itself ────────────────────────────────────────────────────────────────


def _floats(node: object, path: str = "") -> Iterator[str]:
    """Every path in a loaded YAML tree whose value came back as a float."""
    if isinstance(node, float):
        yield f"{path} = {node!r}"
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _floats(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _floats(value, f"{path}[{index}]")


def test_no_rate_in_the_card_is_a_float() -> None:
    """An unquoted 0.0000307 in YAML is already not the rate anyone wrote down."""
    raw = yaml.safe_load(RATES_PATH.read_text(encoding="utf-8"))
    assert list(_floats(raw, "rates.yaml")) == []


def test_a_float_rate_is_rejected_at_load() -> None:
    raw = yaml.safe_load(RATES_PATH.read_text(encoding="utf-8"))
    raw["schedules"][0]["sebi_turnover_rate"] = 0.0000015
    with pytest.raises(ValidationError, match="quoted strings"):
        RateCard.model_validate(raw)


def test_schedules_out_of_order_are_rejected_at_load() -> None:
    raw = yaml.safe_load(RATES_PATH.read_text(encoding="utf-8"))
    raw["schedules"].reverse()
    with pytest.raises(ValidationError, match="ascending effective_from"):
        RateCard.model_validate(raw)


def test_every_schedule_records_where_its_rates_came_from(card: RateCard) -> None:
    """A rate with no provenance is a number someone remembered."""
    for schedule in card.schedules:
        assert schedule.sources, schedule.id
        assert schedule.notes.strip(), schedule.id


# ── 2b. W0-7: the pre-GST era, back to 2006 ──────────────────────────────────────────────────
#
# Two representative dates, one per regime shape:
#
#   2016-09-01 — `krishi-kalyan-cess`. The date the existing L0 lake starts, and the reason this
#     block exists: before it, the first fill of a full-span backtest raised. STT 0.1% both legs,
#     service tax 15%, NSE 0.00325%, SEBI ₹20/crore, KA stamp 0.01% on both legs, DP ₹13.50.
#   2008-06-01 — `service-tax-12-36`. The other STT level (0.125%, both legs) and the longest
#     pre-GST service-tax era (12.36%), with SEBI still at ₹10/crore.
#
# `gst` in the pre-2017 schedules holds the era's effective *service tax* rate; the breakdown field
# is still called `goods_and_services_tax` because the model does not branch on the name.

IN_2016: Final[date] = date(2016, 9, 1)
IN_2008: Final[date] = date(2008, 6, 1)

# Buy 500 @ ₹100 = ₹50,000.
#   STT     0.1%       x 50,000 = 50.00              → ₹50
#   NSE txn 0.00325%   x 50,000 =  1.625             → ₹1.63
#   SEBI    ₹20/crore  x 50,000 =  0.10              → ₹0.10
#   ST      15%        x (0 + 1.63) = 0.2445         → ₹0.24
#   Stamp   0.01% (KA) x 50,000 =  5.00              → ₹5
#   Total ₹56.97
EXPECTED_BUY_2016: Final[dict[str, Decimal]] = {
    "brokerage": Decimal("0.00"),
    "securities_transaction_tax": Decimal("50"),
    "exchange_transaction_charge": Decimal("1.63"),
    "sebi_turnover_fee": Decimal("0.10"),
    "goods_and_services_tax": Decimal("0.24"),
    "stamp_duty": Decimal("5"),
    "depository_charge": Decimal("0"),
}

# Sell 500 @ ₹105 = ₹52,500.
#   STT     0.1%       x 52,500 = 52.50              → ₹53
#   NSE txn 0.00325%   x 52,500 =  1.70625           → ₹1.71
#   SEBI    ₹20/crore  x 52,500 =  0.105             → ₹0.11
#   ST      15%        x (0 + 1.71) = 0.2565         → ₹0.26
#   Stamp   0.01% (KA) x 52,500 =  5.25              → ₹5   (both legs, pre-2020)
#   DP      13.50 x 1.15 = 15.525                    → ₹15.53
#   Total ₹75.61
EXPECTED_SELL_2016: Final[dict[str, Decimal]] = {
    "brokerage": Decimal("0.00"),
    "securities_transaction_tax": Decimal("53"),
    "exchange_transaction_charge": Decimal("1.71"),
    "sebi_turnover_fee": Decimal("0.11"),
    "goods_and_services_tax": Decimal("0.26"),
    "stamp_duty": Decimal("5"),
    "depository_charge": Decimal("15.53"),
}

# Buy 500 @ ₹100 = ₹50,000.
#   STT     0.125%     x 50,000 = 62.50              → ₹63  (half up)
#   NSE txn 0.00325%   x 50,000 =  1.625             → ₹1.63
#   SEBI    ₹10/crore  x 50,000 =  0.05              → ₹0.05
#   ST      12.36%     x (0 + 1.63) = 0.201468       → ₹0.20
#   Stamp   0.01% (KA) x 50,000 =  5.00              → ₹5
#   Total ₹69.88
EXPECTED_BUY_2008: Final[dict[str, Decimal]] = {
    "brokerage": Decimal("0.00"),
    "securities_transaction_tax": Decimal("63"),
    "exchange_transaction_charge": Decimal("1.63"),
    "sebi_turnover_fee": Decimal("0.05"),
    "goods_and_services_tax": Decimal("0.20"),
    "stamp_duty": Decimal("5"),
    "depository_charge": Decimal("0"),
}

# Sell 500 @ ₹105 = ₹52,500.
#   STT     0.125%     x 52,500 = 65.625             → ₹66
#   NSE txn 0.00325%   x 52,500 =  1.70625           → ₹1.71
#   SEBI    ₹10/crore  x 52,500 =  0.0525            → ₹0.05
#   ST      12.36%     x (0 + 1.71) = 0.211356       → ₹0.21
#   Stamp   0.01% (KA) x 52,500 =  5.25              → ₹5
#   DP      13.50 x 1.1236 = 15.1686                 → ₹15.17
#   Total ₹88.14
EXPECTED_SELL_2008: Final[dict[str, Decimal]] = {
    "brokerage": Decimal("0.00"),
    "securities_transaction_tax": Decimal("66"),
    "exchange_transaction_charge": Decimal("1.71"),
    "sebi_turnover_fee": Decimal("0.05"),
    "goods_and_services_tax": Decimal("0.21"),
    "stamp_duty": Decimal("5"),
    "depository_charge": Decimal("15.17"),
}


def test_the_first_session_of_the_lake_is_priceable(model_2018: CostModel) -> None:
    """The whole point of W0-7: 2016-09-01 used to raise, and the lake starts there."""
    charged = model_2018.charge(buy(IN_2016))
    assert charged.schedule_id == "krishi-kalyan-cess"
    assert lines(charged) == EXPECTED_BUY_2016
    assert charged.total == Decimal("56.97")


def test_a_2016_sell_prices_at_the_last_pre_gst_rates(model_2018: CostModel) -> None:
    charged = model_2018.charge(sell(IN_2016))
    assert charged.schedule_id == "krishi-kalyan-cess"
    assert lines(charged) == EXPECTED_SELL_2016
    assert charged.total == Decimal("75.61")


def test_a_2008_buy_pays_the_higher_stt_and_the_service_tax_of_the_day(
    model_2018: CostModel,
) -> None:
    charged = model_2018.charge(buy(IN_2008))
    assert charged.schedule_id == "service-tax-12-36"
    assert lines(charged) == EXPECTED_BUY_2008
    assert charged.total == Decimal("69.88")


def test_a_2008_sell_pays_the_higher_stt_and_the_service_tax_of_the_day(
    model_2018: CostModel,
) -> None:
    charged = model_2018.charge(sell(IN_2008))
    assert charged.schedule_id == "service-tax-12-36"
    assert lines(charged) == EXPECTED_SELL_2008
    assert charged.total == Decimal("88.14")


@pytest.mark.parametrize("on", [IN_2008, IN_2016])
def test_a_same_day_round_trip_prices_in_the_new_eras(model_2018: CostModel, on: date) -> None:
    """The nearest thing to an "intraday fill" this card can express, and it must not raise.

    `card.scope` is delivery equity only — intraday has its own STT rate and its own transaction
    charges, and both are deliberately absent, so there is no product dimension on `Trade` to set.
    A buy and a sell of the same scrip on the same date is therefore what an intraday fill looks
    like here, and `charge_all` is the path that gets the DP charge right: once per scrip per day,
    on the sell.
    """
    legs = model_2018.charge_all([buy(on), sell(on)])
    assert [leg.schedule_id for leg in legs] == [model_2018.rate_card.schedule_for(on).id] * 2
    assert legs[0].depository_charge == Decimal("0")
    assert legs[1].depository_charge > Decimal("0")
    assert sum(leg.total for leg in legs) > Decimal("0")


#: Every boundary in the pre-GST block, with the day before it. Each pair is a statutory date and
#: the last day of the schedule it ended, so an era shifted by one day fails here.
ERA_BOUNDARIES: Final[tuple[tuple[date, str], ...]] = (
    (date(2006, 1, 1), "service-tax-10-2"),
    (date(2006, 4, 17), "service-tax-10-2"),
    (date(2006, 4, 18), "service-tax-12-24"),
    (date(2006, 5, 31), "service-tax-12-24"),
    (date(2006, 6, 1), "stt-0-125-both-legs"),
    (date(2007, 5, 10), "stt-0-125-both-legs"),
    (date(2007, 5, 11), "service-tax-12-36"),
    (date(2009, 2, 23), "service-tax-12-36"),
    (date(2009, 2, 24), "service-tax-10-3"),
    (date(2012, 3, 31), "service-tax-10-3"),
    (date(2012, 4, 1), "service-tax-12-36-restored"),
    (date(2012, 6, 30), "service-tax-12-36-restored"),
    (date(2012, 7, 1), "stt-delivery-cut-to-0-1"),
    (date(2015, 5, 31), "stt-delivery-cut-to-0-1"),
    (date(2015, 6, 1), "service-tax-14"),
    (date(2015, 11, 14), "service-tax-14"),
    (date(2015, 11, 15), "swachh-bharat-cess"),
    (date(2016, 5, 31), "swachh-bharat-cess"),
    (date(2016, 6, 1), "krishi-kalyan-cess"),
    (date(2017, 6, 30), "krishi-kalyan-cess"),
    (date(2017, 7, 1), "gst-era-state-stamp"),
)


@pytest.mark.parametrize(("trade_date", "schedule_id"), ERA_BOUNDARIES)
def test_each_pre_gst_era_starts_on_its_statutory_day(
    model_2018: CostModel, trade_date: date, schedule_id: str
) -> None:
    assert model_2018.charge(buy(trade_date)).schedule_id == schedule_id


@pytest.mark.parametrize(
    ("last_day", "first_day", "before", "after"),
    [
        # Finance Act 2006: 0.1% -> 0.125%, both legs, from 2006-06-01.
        (date(2006, 5, 31), date(2006, 6, 1), Decimal("0.001"), Decimal("0.00125")),
        # Finance Act 2012: 0.125% -> 0.1%, both legs, from 2012-07-01.
        (date(2012, 6, 30), date(2012, 7, 1), Decimal("0.00125"), Decimal("0.001")),
    ],
)
def test_the_stt_rate_moves_on_the_exact_finance_act_date_and_on_both_legs(
    card: RateCard, last_day: date, first_day: date, before: Decimal, after: Decimal
) -> None:
    """Off by a day here and a whole trading day is priced at the wrong STT — ₹12.50 a leg.

    Both legs are asserted on purpose. Delivery STT has always been symmetric; a card that put a
    delivery change on the sell side alone would still pass a sell-only test.
    """
    for on, want in ((last_day, before), (first_day, after)):
        schedule = card.schedule_for(on)
        assert schedule.stt.buy_rate == want, (on, "buy")
        assert schedule.stt.sell_rate == want, (on, "sell")


#: The effective service-tax rate of each pre-GST schedule, from the cited notification history.
#: None of these may be 18% — that is the GST rate, and it does not exist before 2017-07-01.
PRE_GST_TAX_RATES: Final[dict[str, Decimal]] = {
    "service-tax-10-2": Decimal("0.102"),
    "service-tax-12-24": Decimal("0.1224"),
    "stt-0-125-both-legs": Decimal("0.1224"),
    "service-tax-12-36": Decimal("0.1236"),
    "service-tax-10-3": Decimal("0.103"),
    "service-tax-12-36-restored": Decimal("0.1236"),
    "stt-delivery-cut-to-0-1": Decimal("0.1236"),
    "service-tax-14": Decimal("0.14"),
    "swachh-bharat-cess": Decimal("0.145"),
    "krishi-kalyan-cess": Decimal("0.15"),
}

GST_ARRIVES: Final[date] = date(2017, 7, 1)


def test_no_pre_gst_schedule_is_taxed_at_the_gst_rate(card: RateCard) -> None:
    """The failure this guards: a new era pasted in and its tax rate left at the modern 18%."""
    pre_gst = [s for s in card.schedules if s.effective_from < GST_ARRIVES]
    assert {s.id for s in pre_gst} == set(PRE_GST_TAX_RATES)
    for schedule in pre_gst:
        assert schedule.gst.rate == PRE_GST_TAX_RATES[schedule.id], schedule.id
        assert schedule.gst.rate != Decimal("0.18"), schedule.id


def test_the_service_tax_base_never_includes_the_sebi_fee(card: RateCard) -> None:
    """Every dated broker card of the era says "on (brokerage + transaction charges)"."""
    for schedule in card.schedules:
        if schedule.effective_from < GST_ARRIVES:
            assert [c.value for c in schedule.gst.applies_to] == [
                "brokerage",
                "exchange_transaction",
            ], schedule.id


def test_every_pre_gst_schedule_charges_state_stamp_duty_on_both_legs(card: RateCard) -> None:
    """The regime is data, not a branch: the state era ran unbroken from 2006 to 2020-07-01."""
    for schedule in card.schedules:
        if schedule.effective_from < GST_ARRIVES:
            assert schedule.stamp_duty.regime.value == "state", schedule.id
            assert [s.value for s in schedule.stamp_duty.sides] == ["BUY", "SELL"], schedule.id
            assert schedule.stamp_duty.uniform_rate is None, schedule.id
            assert set(schedule.stamp_duty.state_rates) == {"KA", "MH"}, schedule.id


def test_only_karnataka_and_maharashtra_are_encoded_and_anything_else_refuses(
    card: RateCard,
) -> None:
    """A known limit, asserted so it stays loud: an unlisted state raises, never defaults.

    The card encodes KA and MH only. Capped states (Telangana, Haryana) and every other state are
    absent, and `backtest/run.py` hard-codes `account_state='MH'`, so a long-horizon backtest is
    implicitly a Maharashtra account. Both are reported limits, not silent ones.
    """
    for on in (IN_2008, IN_2016, date(2018, 4, 2)):
        with pytest.raises(UnknownStampDutyStateError, match="TN"):
            CostModel(rate_card=card, account_state="TN").charge(buy(on))


def test_the_card_now_covers_the_whole_lake_and_a_pre_2016_backfill(model_2018: CostModel) -> None:
    """Spot dates across every added era, plus the lake's first and last sessions."""
    for on in (
        date(2006, 1, 2),
        date(2008, 6, 2),
        date(2011, 6, 22),  # the first NSE bhavcopy carrying an ISIN
        date(2013, 1, 2),
        date(2016, 9, 1),  # L1 prices_raw starts here
        date(2026, 9, 4),  # and ends here
    ):
        charged = model_2018.charge(buy(on))
        assert charged.total > Decimal("0"), on


def test_every_new_schedule_says_it_is_reconstructed_and_cites_its_sources(card: RateCard) -> None:
    """A long-horizon result has to carry the accuracy caveat with it."""
    for schedule in card.schedules:
        if schedule.effective_from < GST_ARRIVES:
            assert schedule.provenance.value == "reconstructed", schedule.id
            assert schedule.sources, schedule.id
            assert schedule.notes.strip(), schedule.id


# ── 3. invariant #4: exactly one cost implementation ─────────────────────────────────────────

#: The only files allowed to compute Indian trading charges, relative to the repo root.
COST_MODULE: Final[frozenset[str]] = frozenset(
    {"execution/costs/model.py", "execution/costs/__init__.py", "tests/unit/test_costs.py"}
)

#: Charge components. A module that talks about two or more of them is doing cost arithmetic.
COMPONENTS: Final[dict[str, re.Pattern[str]]] = {
    "stt": re.compile(r"\b(stt|securities_transaction_tax)\w*", re.IGNORECASE),
    "stamp duty": re.compile(r"\bstamp_?duty\w*", re.IGNORECASE),
    "sebi fee": re.compile(r"\bsebi_\w+", re.IGNORECASE),
    "transaction charge": re.compile(r"\b\w*transaction_charge\w*", re.IGNORECASE),
    "gst": re.compile(r"\b(gst|goods_and_services_tax)\w*", re.IGNORECASE),
    "dp charge": re.compile(r"\b(dp_charge\w*|depository_(charge|fee)\w*)", re.IGNORECASE),
    "brokerage": re.compile(r"\bbrokerage\w*", re.IGNORECASE),
}

#: A name that looks like a cost quantity being *defined* as a number — a rate table in disguise.
RATE_DEFINITION: Final[re.Pattern[str]] = re.compile(
    r"\b\w*(stt|gst|stamp|sebi|brokerage|dp_charge|transaction_charge|turnover_fee)\w*"
    r"\s*(?::[^=\n]+)?=\s*(?:Decimal\s*\(|\d)",
    re.IGNORECASE,
)

IMPORTS_THE_COST_MODEL: Final[re.Pattern[str]] = re.compile(
    r"^\s*(from\s+execution(\.costs)?[\w.]*\s+import|import\s+execution\.costs)", re.MULTILINE
)


def python_files(root: Path) -> Iterator[Path]:
    """Every product, test and ops `.py` file under `root`."""
    for parent, dirnames, filenames in root.walk():
        dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS and not d.startswith(".")]
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield parent / filename


def yaml_files(root: Path) -> Iterator[Path]:
    """Every checked-in YAML file under `root`."""
    for parent, dirnames, filenames in root.walk():
        dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS and not d.startswith(".")]
        for filename in sorted(filenames):
            if filename.endswith((".yaml", ".yml")):
                yield parent / filename


def scan_source(source: str, *, name: str) -> list[str]:
    """Reasons `source` looks like a second cost implementation. Empty means it is not one."""
    code = "\n".join(code_lines(source))
    found = sorted(label for label, pattern in COMPONENTS.items() if pattern.search(code))
    problems = []
    if len(found) >= 2 and not IMPORTS_THE_COST_MODEL.search(code):
        problems.append(
            f"{name}: computes {', '.join(found)} without importing execution.costs — "
            f"charges come from the shared model or they diverge from it (invariant #4)"
        )
    for line_number, line in enumerate(code.splitlines(), start=1):
        if RATE_DEFINITION.search(line):
            problems.append(f"{name}:{line_number}: defines a cost rate: {line.strip()}")
    return problems


def test_no_second_cost_implementation_exists_in_the_repo() -> None:
    """Invariant #4, mechanically: `SimBroker` and the backtest import this module or nothing."""
    scanned = [
        path
        for path in python_files(REPO_ROOT)
        if path.relative_to(REPO_ROOT).as_posix() not in COST_MODULE
    ]
    assert len(scanned) > 10, (
        f"the walk found only {len(scanned)} files — this would pass vacuously"
    )

    problems = [
        problem
        for path in scanned
        for problem in scan_source(
            path.read_text(encoding="utf-8"), name=path.relative_to(REPO_ROOT).as_posix()
        )
    ]
    assert not problems, "import execution.costs instead of recomputing charges:\n" + "\n".join(
        problems
    )


def test_no_second_rate_table_exists_in_the_repo() -> None:
    """A rate table in another YAML is a second cost model that has not grown its code yet."""
    offenders = []
    scanned = [path for path in yaml_files(REPO_ROOT) if path != RATES_PATH]
    assert len(scanned) > 2, f"the walk found only {len(scanned)} YAML files — vacuous"
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        keys = {key for line in text.splitlines() for key in _yaml_keys(line)}
        matched = sorted(
            label for label, pattern in COMPONENTS.items() if any(pattern.match(k) for k in keys)
        )
        if len(matched) >= 2:
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {', '.join(matched)}")
    assert not offenders, "rates live in execution/costs/rates.yaml only:\n" + "\n".join(offenders)


def _yaml_keys(line: str) -> Iterator[str]:
    """The mapping key on a YAML line, if the line is a mapping entry."""
    match = re.match(r"^\s*(?:-\s*)?([A-Za-z_][\w-]*)\s*:", line)
    if match:
        yield match.group(1)


def test_the_guard_catches_a_second_implementation() -> None:
    """A plausible SimBroker that prices its own fills must not pass."""
    offender = (
        "from decimal import Decimal\n"
        "STT_RATE = Decimal('0.001')\n"
        "def charges(turnover):\n"
        "    stt = turnover * STT_RATE\n"
        "    stamp_duty = turnover * Decimal('0.00015')\n"
        "    return stt + stamp_duty\n"
    )
    problems = scan_source(offender, name="execution/sim_broker.py")
    assert problems
    assert any("invariant #4" in problem for problem in problems)
    assert any("defines a cost rate" in problem for problem in problems)


def test_the_guard_would_flag_the_cost_model_itself() -> None:
    """A vacuous scanner would let a second implementation through unnoticed."""
    source = (REPO_ROOT / "execution" / "costs" / "model.py").read_text(encoding="utf-8")
    assert scan_source(source, name="execution/costs/model.py")


def test_a_consumer_that_imports_the_shared_model_is_not_flagged() -> None:
    """Reading charge lines off a `CostBreakdown` is the intended use, not a second model."""
    consumer = (
        "from execution.costs import CostModel, Trade\n"
        "def fill(model: CostModel, trade: Trade) -> None:\n"
        "    charged = model.charge(trade)\n"
        "    print(charged.stamp_duty, charged.brokerage, charged.securities_transaction_tax)\n"
    )
    assert scan_source(consumer, name="execution/sim_broker.py") == []
