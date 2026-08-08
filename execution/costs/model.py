"""X1: the one Indian transaction-cost model — every charge on a delivery-equity trade.

Invariant #4 (AGENTIC_CONTEXT §6): `SimBroker` and the backtest import *this* module. A second
implementation of Indian transaction costs is a defect by construction, because the moment paper
fills and replayed fills are priced by different code, every backtest result is a claim about a
strategy that was never run. `tests/unit/test_costs.py` greps the repo to keep it that way.

What it does: given a trade (side, quantity, price, exchange, date) it returns every line a
Zerodha contract note would show — brokerage, STT, exchange transaction charge, SEBI turnover fee,
GST, stamp duty, and the DP charge on sells — each rounded the way the contract note rounds it,
and all of it in `Decimal`.

What it assumes: the rates in force on the *trade date*, read from `rates.yaml`, which carries one
schedule per rate regime back to the GST rollout. A trade dated before the first schedule raises:
pricing 2016 with 2020's rate card would quietly falsify a backtest.

What it never does: guess. An exchange with no rate on the schedule, a pre-2020 trade with no
account state to pick a stamp-duty rate from, or a state the schedule does not list all raise. It
also never touches the wall clock — the trade date is an input, so replay is deterministic (B10).

Scope is delivery equity on NSE and BSE. Intraday, F&O, currency and commodity are a different
rate card; when they are needed they belong in `rates.yaml` and here, never in a second module.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

#: The dated rate card this module is meaningless without.
RATES_PATH: Final[Path] = Path(__file__).with_name("rates.yaml")

_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")
_PAISA: Final[Decimal] = Decimal("0.01")
_RUPEE: Final[Decimal] = Decimal("1")

#: ISIN as the identity master issues it (invariant #2): country code, 9 alphanumerics, check digit.
_ISIN_PATTERN: Final[str] = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"

__all__ = [
    "RATES_PATH",
    "CostBreakdown",
    "CostModel",
    "CostModelError",
    "DpCharge",
    "Exchange",
    "GstComponent",
    "GstRule",
    "MissingAccountStateError",
    "NoRateScheduleError",
    "Provenance",
    "RateCard",
    "Schedule",
    "Side",
    "StampDuty",
    "StampDutyRegime",
    "SttRates",
    "Trade",
    "UnknownExchangeError",
    "UnknownStampDutyStateError",
    "load_rate_card",
]


# ── errors ───────────────────────────────────────────────────────────────────────────────────


class CostModelError(Exception):
    """Base for every refusal to price a trade. Costs fail loud; they are never approximated."""


class NoRateScheduleError(CostModelError):
    """The trade date is before the earliest schedule in the rate card."""


class UnknownExchangeError(CostModelError):
    """The schedule in force has no transaction-charge rate for that exchange."""


class MissingAccountStateError(CostModelError):
    """A state-wise stamp-duty era needs the account's state and none was configured."""


class UnknownStampDutyStateError(CostModelError):
    """The schedule in force has no stamp-duty rate on file for that state."""


# ── vocabulary ───────────────────────────────────────────────────────────────────────────────


class Exchange(StrEnum):
    """The two cash exchanges. Transaction charges differ per exchange, so it is not cosmetic."""

    NSE = "NSE"
    BSE = "BSE"


class Side(StrEnum):
    """Which way the trade goes. Stamp duty and the DP charge are one-sided."""

    BUY = "BUY"
    SELL = "SELL"


class Provenance(StrEnum):
    """How much of a schedule was read off a primary source (see `rates.yaml`'s header)."""

    VERIFIED = "verified"
    RECONSTRUCTED = "reconstructed"


class StampDutyRegime(StrEnum):
    """State-wise stamp duty (pre 2020-07-01) or the uniform central rate (from 2020-07-01)."""

    STATE = "state"
    UNIFORM = "uniform"


class GstComponent(StrEnum):
    """The charges GST can be levied on. Which ones actually are is per-schedule."""

    BROKERAGE = "brokerage"
    EXCHANGE_TRANSACTION = "exchange_transaction"
    SEBI_TURNOVER = "sebi_turnover"


# ── rounding ─────────────────────────────────────────────────────────────────────────────────


def _to_paisa(amount: Decimal) -> Decimal:
    """Round to two decimals, half up — how a contract note shows a charge line."""
    return amount.quantize(_PAISA, rounding=ROUND_HALF_UP)


def _to_rupee(amount: Decimal) -> Decimal:
    """Round to whole rupees, half up.

    STT and stamp duty are levied in whole rupees: Zerodha's worked example rounds ₹52.50 of STT
    up to ₹53, which fixes the tie-breaking direction as half-up rather than banker's rounding.
    """
    return amount.quantize(_RUPEE, rounding=ROUND_HALF_UP)


# ── the rate card ────────────────────────────────────────────────────────────────────────────


def _decimal_from_text(value: object) -> object:
    """Accept a quoted rate, reject a YAML float.

    `0.0000307` parsed by PyYAML is a binary float and is already not the rate anyone wrote down;
    money and rates are `Decimal` end to end (CLAUDE.md), so the config must quote them.
    """
    if isinstance(value, float):
        raise ValueError(
            "rates must be quoted strings in rates.yaml so they parse exactly as Decimal, "
            f"got the float {value!r}"
        )
    if isinstance(value, str):
        return Decimal(value)
    return value


#: A non-negative rate or amount, exact because it was written as text.
Rate = Annotated[Decimal, BeforeValidator(_decimal_from_text), Field(ge=0)]


class _Frozen(BaseModel):
    """Config models are frozen and reject unknown keys, so a typo in the YAML is a load error."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CardMeta(_Frozen):
    """What this rate card is a card *for*. Documentation with a schema."""

    broker: str
    segment: str
    currency: str
    scope: str


class SttRates(_Frozen):
    """Securities Transaction Tax. Delivery is taxed on both legs, at rates that can differ."""

    buy_rate: Rate
    sell_rate: Rate

    def rate_for(self, side: Side) -> Decimal:
        """The STT rate for one side of a delivery trade."""
        return self.buy_rate if side is Side.BUY else self.sell_rate


class GstRule(_Frozen):
    """GST rate, and the charges it is levied on — the base changed over time."""

    rate: Rate
    applies_to: list[GstComponent] = Field(min_length=1)


class StampDuty(_Frozen):
    """Stamp duty, which changed shape (not just rate) on 2020-07-01.

    Before: each state set its own rate and duty was collected on both legs. After: one central
    rate, buy side only. Encoding the sides in the config rather than in code is what lets a 2018
    replay pay duty on its sells without a branch anywhere.
    """

    regime: StampDutyRegime
    sides: list[Side] = Field(min_length=1)
    uniform_rate: Rate | None = None
    state_rates: dict[str, Rate] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _regime_matches_rates(self) -> StampDuty:
        if self.regime is StampDutyRegime.UNIFORM:
            if self.uniform_rate is None or self.state_rates:
                raise ValueError(
                    "a uniform stamp-duty regime needs uniform_rate and no state_rates"
                )
        elif self.uniform_rate is not None or not self.state_rates:
            raise ValueError("a state stamp-duty regime needs state_rates and no uniform_rate")
        return self

    def rate_for(self, *, account_state: str | None, schedule_id: str) -> Decimal:
        """The duty rate for an account, or a loud refusal.

        A state-wise era with no configured state cannot be priced: defaulting to some state's
        rate would put a silent, plausible-looking error into every pre-2020 backtest.
        """
        if self.regime is StampDutyRegime.UNIFORM:
            # `_regime_matches_rates` makes this unreachable for a validated card; a hand-built
            # StampDuty is the only way here, and it still must not price a trade at zero.
            if self.uniform_rate is None:
                raise CostModelError(f"schedule {schedule_id!r} is uniform but has no rate")
            return self.uniform_rate
        if account_state is None:
            raise MissingAccountStateError(
                f"schedule {schedule_id!r} charges stamp duty per state; construct "
                f"CostModel(account_state=...) with one of {sorted(self.state_rates)}"
            )
        try:
            return self.state_rates[account_state]
        except KeyError:
            raise UnknownStampDutyStateError(
                f"schedule {schedule_id!r} has no stamp-duty rate for state {account_state!r}; "
                f"known states are {sorted(self.state_rates)}"
            ) from None


class DpCharge(_Frozen):
    """The depository charge, billed per scrip on a sell, flat in the quantity."""

    depository_fee: Rate
    broker_fee: Rate
    gst_applies: bool

    def amount(self, gst_rate: Decimal) -> Decimal:
        """The charge as billed — GST inclusive when it applies, because that is how it is shown."""
        gross = self.depository_fee + self.broker_fee
        if self.gst_applies:
            gross *= _ONE + gst_rate
        return _to_paisa(gross)


class Schedule(_Frozen):
    """One rate regime, in force from `effective_from` until the next schedule starts."""

    id: str
    effective_from: date
    label: str
    provenance: Provenance
    sources_read_on: date
    sources: list[str] = Field(min_length=1)
    notes: str

    brokerage_per_order: Rate
    stt: SttRates
    exchange_transaction: dict[Exchange, Rate] = Field(min_length=1)
    sebi_turnover_rate: Rate
    gst: GstRule
    stamp_duty: StampDuty
    dp_charge: DpCharge

    def transaction_rate(self, exchange: Exchange) -> Decimal:
        """The exchange's own turnover charge, or a refusal naming the exchanges it does cover."""
        try:
            return self.exchange_transaction[exchange]
        except KeyError:
            raise UnknownExchangeError(
                f"schedule {self.id!r} has no transaction-charge rate for {exchange}; "
                f"it covers {sorted(self.exchange_transaction)}"
            ) from None


class RateCard(_Frozen):
    """The whole dated card: the schedules, newest last."""

    version: int
    card: CardMeta
    schedules: list[Schedule] = Field(min_length=1)

    @model_validator(mode="after")
    def _schedules_are_ordered_and_unique(self) -> RateCard:
        dates = [schedule.effective_from for schedule in self.schedules]
        if dates != sorted(dates):
            raise ValueError("schedules must be listed in ascending effective_from order")
        if len(set(dates)) != len(dates):
            raise ValueError("two schedules share an effective_from; the one in force is ambiguous")
        ids = [schedule.id for schedule in self.schedules]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate schedule id")
        return self

    def schedule_for(self, trade_date: date) -> Schedule:
        """The schedule in force on `trade_date`.

        Raises rather than falling back to the earliest card: a trade priced with rates that were
        not yet in force is a wrong number wearing the costume of a right one.
        """
        in_force = [s for s in self.schedules if s.effective_from <= trade_date]
        if not in_force:
            earliest = self.schedules[0].effective_from
            raise NoRateScheduleError(
                f"no rate schedule covers {trade_date.isoformat()}; the card starts at "
                f"{earliest.isoformat()}. Add a schedule to rates.yaml rather than pricing the "
                f"trade with a later one."
            )
        return in_force[-1]


@lru_cache(maxsize=1)
def load_rate_card(path: Path = RATES_PATH) -> RateCard:
    """Parse and validate the dated rate card, once per process.

    Assumes the file is checked in and trusted. Raises `ValidationError` on a schema break and
    `FileNotFoundError` if it is missing — both loud, neither recoverable here.
    """
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return RateCard.model_validate(raw)


# ── trades and their charges ─────────────────────────────────────────────────────────────────


def _require_decimal(name: str, value: object) -> Decimal:
    """Reject a float where money belongs. Typed `object` so this is a real runtime check."""
    if not isinstance(value, Decimal):
        raise TypeError(
            f"{name} must be a Decimal, got {type(value).__name__} — money is never float "
            f"(CLAUDE.md)"
        )
    return value


def _require_whole_shares(value: object) -> int:
    """Reject a fractional or non-integral quantity. `True` is an int in Python; it is not a lot."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"quantity must be a whole number of shares, got {value!r}")
    if value <= 0:
        raise ValueError(f"quantity must be positive, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class Trade:
    """One executed delivery-equity trade, as much of it as pricing needs.

    Identified by ISIN, never by symbol (invariant #2): the DP charge is per scrip per day, and
    two symbols can be the same scrip across a rename.
    """

    isin: str
    trade_date: date
    side: Side
    quantity: int
    price: Decimal
    exchange: Exchange = Exchange.NSE

    def __post_init__(self) -> None:
        if not re.fullmatch(_ISIN_PATTERN, self.isin):
            raise ValueError(f"not an ISIN: {self.isin!r}")
        _require_whole_shares(self.quantity)
        if _require_decimal("price", self.price) < _ZERO:
            raise ValueError(f"price must not be negative, got {self.price}")

    @property
    def turnover(self) -> Decimal:
        """Gross value of the trade, exact — no rounding happens before the charges are computed."""
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Every charge on one trade, line by line, plus what the trade settles at.

    Each line is already rounded the way it is billed, so `total` is the sum a contract note
    shows and never a re-rounded approximation of one.
    """

    schedule_id: str
    side: Side
    turnover: Decimal
    brokerage: Decimal
    securities_transaction_tax: Decimal
    exchange_transaction_charge: Decimal
    sebi_turnover_fee: Decimal
    goods_and_services_tax: Decimal
    stamp_duty: Decimal
    depository_charge: Decimal

    @property
    def total(self) -> Decimal:
        """Everything the trade costs on top of its turnover."""
        return (
            self.brokerage
            + self.securities_transaction_tax
            + self.exchange_transaction_charge
            + self.sebi_turnover_fee
            + self.goods_and_services_tax
            + self.stamp_duty
            + self.depository_charge
        )

    @property
    def net_amount(self) -> Decimal:
        """Cash payable on a buy, cash receivable on a sell — costs always work against you."""
        return self.turnover + self.total if self.side is Side.BUY else self.turnover - self.total


@dataclass(frozen=True, slots=True)
class CostModel:
    """Prices trades against a dated rate card. The only cost model in the repo (invariant #4).

    What it does: `charge` for one trade, `charge_all` for a day's trades, which is what makes the
    DP charge come out right — it is levied once per scrip per day however many sells there were.
    What it assumes: `account_state` is where the account is registered, needed only for trades in
    the pre-2020 state-wise stamp-duty era.
    What it never does: read the clock, or price a date its rate card does not cover.
    """

    rate_card: RateCard
    account_state: str | None = None

    def charge(self, trade: Trade) -> CostBreakdown:
        """Every charge on one standalone trade, at the rates in force on its trade date."""
        return self._charge(trade, dp_charge_applies=True)

    def charge_all(self, trades: Sequence[Trade]) -> tuple[CostBreakdown, ...]:
        """Charges for a set of trades, with the DP charge levied once per scrip per sell day.

        The charge lands on the first sell of that scrip on that date in the given order; every
        later sell of the same scrip that day carries a zero DP line. Summing the results
        therefore gives what the account is actually debited, not a per-trade over-count.
        """
        charged: set[tuple[str, date]] = set()
        breakdowns: list[CostBreakdown] = []
        for trade in trades:
            scrip_day = (trade.isin, trade.trade_date)
            first_sell = trade.side is Side.SELL and scrip_day not in charged
            if first_sell:
                charged.add(scrip_day)
            breakdowns.append(self._charge(trade, dp_charge_applies=first_sell))
        return tuple(breakdowns)

    def _charge(self, trade: Trade, *, dp_charge_applies: bool) -> CostBreakdown:
        schedule = self.rate_card.schedule_for(trade.trade_date)
        turnover = trade.turnover

        brokerage = _to_paisa(schedule.brokerage_per_order)
        stt = _to_rupee(turnover * schedule.stt.rate_for(trade.side))
        transaction = _to_paisa(turnover * schedule.transaction_rate(trade.exchange))
        sebi = _to_paisa(turnover * schedule.sebi_turnover_rate)

        # GST is levied on the charge lines as billed, which is why the base uses the rounded
        # figures above rather than their exact products.
        taxable = {
            GstComponent.BROKERAGE: brokerage,
            GstComponent.EXCHANGE_TRANSACTION: transaction,
            GstComponent.SEBI_TURNOVER: sebi,
        }
        gst = _to_paisa(
            sum((taxable[component] for component in schedule.gst.applies_to), _ZERO)
            * schedule.gst.rate
        )

        stamp = _ZERO
        if trade.side in schedule.stamp_duty.sides:
            rate = schedule.stamp_duty.rate_for(
                account_state=self.account_state, schedule_id=schedule.id
            )
            stamp = _to_rupee(turnover * rate)

        # The DP charge is billed with its own GST, which is why it is not in the GST base above.
        depository = _ZERO
        if trade.side is Side.SELL and dp_charge_applies:
            depository = schedule.dp_charge.amount(schedule.gst.rate)

        return CostBreakdown(
            schedule_id=schedule.id,
            side=trade.side,
            turnover=turnover,
            brokerage=brokerage,
            securities_transaction_tax=stt,
            exchange_transaction_charge=transaction,
            sebi_turnover_fee=sebi,
            goods_and_services_tax=gst,
            stamp_duty=stamp,
            depository_charge=depository,
        )
