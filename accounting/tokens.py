"""X3: what every model call cost, in tokens and in rupees, attached to the decision it bought.

Decision #12 is "cost-blind for now" — quality-first model choices — and the second half of that
sentence is the part this module implements: *per-decision token accounting in the journal so cost
is measured, not unknown*. Blind is a policy; unknown is a defect. Two records come out of every
call and they are written from the same priced figure, so they can never disagree:

1. a `token_usage` row — provider, model, purpose, tokens, `cost_usd`, `cost_inr`, and the
   journal entry the call informed (§5.7's burn report reads this);
2. the `model` and `tokens` fields of that decision's journal line.

Pricing is a dated card (`model_prices.yaml`), for the same reason transaction costs are: an
introductory rate that ends on a date is a real thing, and a decision made while it was in force
must keep costing what it cost when the card is next edited. A model the card does not list raises
`UnknownModelError`. That refusal is deliberate — an unpriced call that books ₹0 does not look
like a bug in a burn report, it looks like a bargain.

Both `cost_usd` and `cost_inr` are stored. The provider bills in dollars and the book is in
rupees; keeping only one of them makes the FX rate that was applied unrecoverable, and `cost_inr`
is computed from the *rounded* `cost_usd` precisely so that dividing one by the other returns the
rate exactly.

A `StubLLM` call is priced by the same card as a real one. That is not an accident: paper and real
differ only in which implementation is injected (invariant #5), so the paper burn report is a
forecast of the live one rather than a column of zeros — and `provider` is what separates them
afterwards.

Money is `Decimal` end to end. Time is injected (B10). Nothing here commits; the caller owns the
transaction, so a decision, its order and its token row land atomically or not at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Final

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from analyst.journal import TokenSpend
from analyst.llm import (
    DEFAULT_MAX_TOKENS,
    LLM,
    LLMResponse,
    Message,
    ToolSpec,
    Usage,
)
from dataplatform.clock import Clock, SystemClock
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = [
    "PRICES_PATH",
    "AccountingError",
    "CardMeta",
    "LedgerError",
    "MeteredCompletion",
    "MeteredLLM",
    "ModelPrice",
    "NoPriceScheduleError",
    "PriceCard",
    "PriceSchedule",
    "PricedUsage",
    "Provenance",
    "TokenLedger",
    "TokenPricer",
    "UnknownModelError",
    "UnknownUsageError",
    "load_price_card",
]

_LOG = get_logger(__name__)

#: The dated price card this module is meaningless without.
PRICES_PATH: Final[Path] = Path(__file__).with_name("model_prices.yaml")

#: Card rates are quoted per million tokens, which is how the provider publishes them.
_PER_MILLION: Final[Decimal] = Decimal(1_000_000)

#: Scale of `money_inr` and of `token_usage.cost_usd` (both NUMERIC(20, 6)).
_MICRO: Final[Decimal] = Decimal("0.000001")

_ZERO: Final[Decimal] = Decimal(0)


# ── errors ───────────────────────────────────────────────────────────────────────────────────


class AccountingError(Exception):
    """Base for every refusal to account for a call. Cost fails loud; it is never assumed."""


class NoPriceScheduleError(AccountingError):
    """The call's date is before the earliest schedule on the card."""


class UnknownModelError(AccountingError):
    """The schedule in force has no price for that model — the ₹0 this module exists to prevent."""


class LedgerError(AccountingError):
    """A `token_usage` write did not do what it claimed."""


class UnknownUsageError(LedgerError):
    """No `token_usage` row matched, or it was already attached to a decision."""


# ── the price card ───────────────────────────────────────────────────────────────────────────


def _decimal_from_text(value: object) -> object:
    """Accept a quoted rate, reject a YAML float.

    `0.10` parsed by PyYAML is a binary float and is already not the rate anyone wrote down; money
    is `Decimal` from the first character it is read from (CLAUDE.md), so the config must quote it.
    """
    if isinstance(value, float):
        raise ValueError(
            "prices must be quoted strings in model_prices.yaml so they parse exactly as "
            f"Decimal, got the float {value!r}"
        )
    if isinstance(value, str):
        return Decimal(value)
    return value


#: A non-negative price, exact because it was written as text.
Rate = Annotated[Decimal, BeforeValidator(_decimal_from_text), Field(ge=0)]


class _Frozen(BaseModel):
    """Config models are frozen and reject unknown keys, so a typo in the YAML is a load error."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Provenance(StrEnum):
    """How much of a schedule was read off a primary source (see `model_prices.yaml`'s header)."""

    VERIFIED = "verified"
    RECONSTRUCTED = "reconstructed"


class CardMeta(_Frozen):
    """What this card is a card *for*. Documentation with a schema."""

    provider: str
    currency: str
    basis: str
    scope: str


class ModelPrice(_Frozen):
    """One model's four rates, US dollars per million tokens.

    The buckets are disjoint and are billed differently, which is the only reason they are
    separate fields: collapsing cache reads into ordinary input would hide a tenfold discount, and
    a burn report that cannot show whether caching is working cannot be acted on.
    """

    input: Rate
    output: Rate
    cache_write: Rate
    cache_read: Rate

    def cost_usd(self, usage: Usage) -> Decimal:
        """The exact dollar cost of one call at these rates, unrounded."""
        return (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_write_tokens * self.cache_write
            + usage.cache_read_tokens * self.cache_read
        ) / _PER_MILLION


class PriceSchedule(_Frozen):
    """One price regime, in force from `effective_from` until the next schedule starts."""

    id: str
    effective_from: date
    label: str
    provenance: Provenance
    figures_current_as_of: date
    sources: list[str] = Field(min_length=1)
    notes: str
    usd_inr: Rate = Field(gt=0)
    models: dict[str, ModelPrice] = Field(min_length=1)

    def price_for(self, model: str) -> ModelPrice:
        """This schedule's price for a model, or a refusal naming the models it does cover."""
        try:
            return self.models[model]
        except KeyError:
            raise UnknownModelError(
                f"schedule {self.id!r} has no price for model {model!r}; it covers "
                f"{sorted(self.models)}. Add the model to accounting/model_prices.yaml — an "
                f"unpriced call would be recorded at ₹0, which reads as cheap rather than "
                f"unmeasured."
            ) from None


class PriceCard(_Frozen):
    """The whole dated card: the schedules, newest last."""

    version: int
    card: CardMeta
    schedules: list[PriceSchedule] = Field(min_length=1)

    @model_validator(mode="after")
    def _schedules_are_ordered_and_unique(self) -> PriceCard:
        dates = [schedule.effective_from for schedule in self.schedules]
        if dates != sorted(dates):
            raise ValueError("schedules must be listed in ascending effective_from order")
        if len(set(dates)) != len(dates):
            raise ValueError("two schedules share an effective_from; the one in force is ambiguous")
        ids = [schedule.id for schedule in self.schedules]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate schedule id")
        return self

    def schedule_for(self, on: date) -> PriceSchedule:
        """The schedule in force on `on`.

        Raises rather than falling back to the earliest card: a call priced at rates that were not
        yet in force is a wrong number wearing the costume of a right one.
        """
        in_force = [schedule for schedule in self.schedules if schedule.effective_from <= on]
        if not in_force:
            earliest = self.schedules[0].effective_from
            raise NoPriceScheduleError(
                f"no price schedule covers {on.isoformat()}; the card starts at "
                f"{earliest.isoformat()}. Add a schedule to model_prices.yaml rather than "
                f"pricing the call with a later one."
            )
        return in_force[-1]


@lru_cache(maxsize=1)
def load_price_card(path: Path = PRICES_PATH) -> PriceCard:
    """Parse and validate the dated price card, once per process.

    Assumes the file is checked in and trusted. Raises `ValidationError` on a schema break and
    `FileNotFoundError` if it is missing — both loud, neither recoverable here.
    """
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return PriceCard.model_validate(raw)


# ── priced calls ─────────────────────────────────────────────────────────────────────────────


def _to_micro(amount: Decimal) -> Decimal:
    """Round to six decimals, half up — the scale both money columns are declared at."""
    return amount.quantize(_MICRO, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class PricedUsage:
    """One call's usage, costed. The single figure both records are written from.

    `cost_inr` is `cost_usd * usd_inr` on the *rounded* dollar figure, so an auditor reading a
    `token_usage` row years later recovers the exact rate that was applied by dividing one stored
    column by the other. Deriving it from the unrounded product would leave the rate approximate.
    """

    provider: str
    model: str
    purpose: str
    usage: Usage
    schedule_id: str
    usd_inr: Decimal
    cost_usd: Decimal
    cost_inr: Decimal

    @property
    def token_spend(self) -> TokenSpend:
        """The §5.7 journal fields for this call.

        `tokens_in` is every prompt token the provider billed — uncached, cache-written and
        cache-read alike — so that `tokens_in + tokens_out` is the true volume of the call. The
        split between those three buckets survives in `cost_inr` and in this object; the schema
        keeps only the cached portion (`token_usage.cached_tokens`).
        """
        return TokenSpend(
            tokens_in=self.usage.prompt_tokens,
            tokens_out=self.usage.output_tokens,
            cost_inr=self.cost_inr,
        )


@dataclass(frozen=True, slots=True)
class TokenPricer:
    """Turns a provider's token counts into rupees, at the rates in force on a date.

    What it does: `price` one `LLMResponse` against the dated card.
    What it assumes: the date it is given is the date the spend belongs to — the trading date of
    the decision, not the day someone happened to run a report.
    What it never does: read the clock, reach the network, or price a model the card omits.
    """

    price_card: PriceCard

    def price(self, response: LLMResponse, *, on: date, purpose: str) -> PricedUsage:
        """Cost one completed call. Raises if the date or the model is not on the card."""
        if not purpose.strip():
            raise ValueError(
                "every priced call needs a purpose (e.g. 't1_review', 'theme_mapping'): the burn "
                "report is per-purpose, and an unlabelled row cannot be attributed"
            )
        schedule = self.price_card.schedule_for(on)
        model_price = schedule.price_for(response.model)
        cost_usd = _to_micro(model_price.cost_usd(response.usage))
        return PricedUsage(
            provider=response.provider,
            model=response.model,
            purpose=purpose,
            usage=response.usage,
            schedule_id=schedule.id,
            usd_inr=schedule.usd_inr,
            cost_usd=cost_usd,
            cost_inr=_to_micro(cost_usd * schedule.usd_inr),
        )


# ── the ledger ───────────────────────────────────────────────────────────────────────────────


#: Columns written on every `token_usage` insert, in one place so the INSERT and its parameter
#: tuple cannot drift apart. `id` is assigned by the database.
_WRITE_COLUMNS: Final[tuple[str, ...]] = (
    "ts",
    "provider",
    "model",
    "purpose",
    "case_id",
    "decision_journal_id",
    "tokens_in",
    "tokens_out",
    "cached_tokens",
    "cost_inr",
    "cost_usd",
    "recorded_at",
)

_INSERT: Final[str] = (
    f"INSERT INTO token_usage ({', '.join(_WRITE_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(_WRITE_COLUMNS))}) "
    f"RETURNING id"
)

#: Attaching a decision is the one update this table takes, and it is write-once: the guard means
#: a second attempt cannot silently re-point a spend row at a different decision.
_ATTACH: Final[str] = (
    "UPDATE token_usage SET decision_journal_id = %s "
    "WHERE id = %s AND decision_journal_id IS NULL "
    "RETURNING id"
)


class TokenLedger:
    """Append access to `token_usage` — one row per model call.

    What it does: writes a priced call, and later attaches it to the journal entry the call
    informed, because the entry's id does not exist until the decision has been written.
    What it assumes: the schema is migrated and the caller owns the transaction. Nothing here
    commits, so a decision, its order and its cost land together or not at all.
    What it never does: overwrite a spend figure, or re-point a row that is already attached to a
    decision. Cost rows are evidence; a corrected one is a new row.
    """

    __slots__ = ("_clock", "_conn")

    def __init__(self, conn: Connection, *, clock: Clock | None = None) -> None:
        self._conn = conn
        self._clock = SystemClock() if clock is None else clock

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def record(
        self,
        priced: PricedUsage,
        *,
        ts: datetime | None = None,
        case_id: str | None = None,
        decision_journal_id: int | None = None,
    ) -> int:
        """Write one call's cost and return the row id.

        `ts` is when the call happened and defaults to this ledger's injected clock; `recorded_at`
        is when the row landed. A replay writes rows whose `ts` is old and whose `recorded_at` is
        now, which is what makes a replayed ledger distinguishable from the original.
        """
        moment = self._clock.now() if ts is None else ts
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise ValueError(
                f"ts must be tz-aware, got naive {moment.isoformat()!r}; take it from an injected "
                "Clock, which is always aware"
            )
        row = self._conn.execute(
            _INSERT,
            (
                moment,
                priced.provider,
                priced.model,
                priced.purpose,
                case_id,
                decision_journal_id,
                priced.usage.prompt_tokens,
                priced.usage.output_tokens,
                priced.usage.cache_read_tokens,
                priced.cost_inr,
                priced.cost_usd,
                self._clock.now(),
            ),
        ).fetchone()
        if row is None:  # pragma: no cover — RETURNING always yields a row on a successful insert
            raise LedgerError("INSERT ... RETURNING produced no row; the spend record is lost")
        usage_id = int(row[0])
        _LOG.info(
            "token_usage.record",
            usage_id=usage_id,
            provider=priced.provider,
            model=priced.model,
            purpose=priced.purpose,
            case_id=case_id,
            decision_journal_id=decision_journal_id,
            tokens_in=priced.usage.prompt_tokens,
            tokens_out=priced.usage.output_tokens,
            cost_inr=str(priced.cost_inr),
        )
        return usage_id

    def attach_decision(self, usage_id: int, decision_journal_id: int) -> None:
        """Point a recorded call at the journal entry it informed.

        The entry is written after the call that produced its rationale, so its id cannot be known
        at record time. Write-once: attaching a row that already names a decision raises, because
        one spend belongs to one decision and a silent re-point would break the burn report's
        attribution.
        """
        row = self._conn.execute(_ATTACH, (decision_journal_id, usage_id)).fetchone()
        if row is None:
            raise UnknownUsageError(
                f"no unattached token_usage row with id {usage_id}: it does not exist, or it is "
                f"already attached to a decision"
            )
        _LOG.info("token_usage.attach", usage_id=usage_id, decision_journal_id=decision_journal_id)


# ── the metered client ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MeteredCompletion:
    """A model call, its cost, and the `token_usage` row that recorded it."""

    response: LLMResponse
    priced: PricedUsage
    usage_id: int | None
    """None only when the meter was built without a ledger — a test, or a dry run."""

    @property
    def text(self) -> str:
        """What the model said."""
        return self.response.text

    def journal_fields(self) -> dict[str, Any]:
        """The §5.7 entry fields this call contributes: `model` and `tokens`.

        Spread into a `JournalEntry(...)` so the decision's line carries the cost of the call that
        produced it, taken from the same priced figure as the `token_usage` row — the two cannot
        disagree because there is only one of them.
        """
        return {"model": self.priced.model, "tokens": self.priced.token_spend}


class MeteredLLM:
    """An `LLM` wrapped so that every call it answers is priced and recorded.

    What it does: delegates to the wrapped client, prices the usage on the dated card, writes the
    `token_usage` row, and hands back everything the caller needs to journal the decision.
    What it assumes: the caller journals the decision afterwards and calls `attach_decision` with
    the entry id — the entry does not exist yet at call time.
    What it never does: satisfy `LLM` itself. That is deliberate: `complete` here returns a
    `MeteredCompletion`, not an `LLMResponse`, so a module cannot accidentally take the metered
    client and drop the cost on the floor.
    """

    __slots__ = ("_clock", "_inner", "_ledger", "_pricer")

    def __init__(
        self,
        inner: LLM,
        *,
        pricer: TokenPricer,
        ledger: TokenLedger | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._inner = inner
        self._pricer = pricer
        self._ledger = ledger
        self._clock = SystemClock() if clock is None else clock

    def __repr__(self) -> str:
        return f"{type(self).__name__}(inner={self._inner!r}, metered={self._ledger is not None})"

    @property
    def ledger(self) -> TokenLedger | None:
        """The ledger this meter writes to, for a caller that must attach a decision id."""
        return self._ledger

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        purpose: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        case_id: str | None = None,
        on: date | None = None,
    ) -> MeteredCompletion:
        """Ask the model, price the answer, and record what it cost.

        `purpose` is required and is what the burn report groups by — `t1_review`,
        `theme_mapping`, `thesis_draft`. `on` is the date whose rates apply, defaulting to the
        injected clock's today.
        """
        moment = self._clock.now()
        response = self._inner.complete(
            messages, model=model, tools=tools, system=system, max_tokens=max_tokens
        )
        priced = self._pricer.price(
            response, on=self._clock.today() if on is None else on, purpose=purpose
        )
        usage_id = (
            None
            if self._ledger is None
            else self._ledger.record(priced, ts=moment, case_id=case_id)
        )
        return MeteredCompletion(response=response, priced=priced, usage_id=usage_id)
