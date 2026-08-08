"""A9: the §5.7 journal entry — the schema of the thing §0 calls the product.

Every agent action lands here: what was decided, by which tier, on what evidence, and why. The
schema is deliberately unforgiving, because a journal is only evidence if it cannot record a
decision nobody can reconstruct. Three families of rule enforce that:

* **A decision must be explicable.** Anything that moved money, blocked an order, escalated a
  tier or proposed a policy carries a rationale. `HOLD` and `HEARTBEAT` do not — "nothing
  changed" is its own explanation — but `HEARTBEAT` must name the evidence it considered
  (invariant #9), which is the whole difference between a heartbeat and an empty log line.
* **A decision cannot see the future.** `trading_date` may not be later than the IST date of `ts`
  (invariant #7's shape at this layer): an entry claiming to reason about tomorrow's session is a
  leak, whatever the query layer did.
* **Money is exact.** `cost_inr` is `Decimal` and a `float` is rejected on the way in rather than
  silently absorbed, and the free-form `payload` is strings only — a JSON number read back out of
  `jsonb` is a float, and a float in this system is a bug (CLAUDE.md).

The enums mirror the CHECK constraints on `decision_journal` in `0001_init.sql`; `test_journal.py`
parses that file and fails if the two ever drift, since a value this model accepts and the
database rejects would surface as a write failure in the middle of a trading decision.

Nothing here reads a clock, a database or the network. `ts` is supplied by the caller from an
injected `Clock` (B10) so a replayed decision carries the instant it originally claimed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from dataplatform.clock import IST

__all__ = [
    "EVIDENCE_REF_PATTERN",
    "ISIN_PATTERN",
    "REQUIRES_EVIDENCE",
    "REQUIRES_INSTRUMENT",
    "REQUIRES_RATIONALE",
    "Actor",
    "BreakConditionEvaluation",
    "Decision",
    "JournalEntry",
    "Money",
    "RecordedEntry",
    "Sleeve",
    "TokenSpend",
    "Verdict",
]

#: ISO 6166, shape-checked exactly as the `isin` domain in 0001_init.sql checks it. Shape only —
#: the check digit is D2's problem, and a journal entry must be writable for an instrument the
#: identity master has not ingested yet.
ISIN_PATTERN: Final = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"

#: How an evidence bundle is named on an entry. The algorithm is part of the reference so that a
#: journal read in five years says what the digits are, rather than leaving it to be inferred.
EVIDENCE_REF_PATTERN: Final = r"^sha256:[0-9a-f]{64}$"


def _reject_float(value: Any) -> Any:
    """Refuse a `float` where an exact decimal is required.

    pydantic would happily coerce `1234.55` into a `Decimal`, having already lost the last digits
    to binary floating point on the way in. Money in this system is `Decimal` from the first
    character it is read from (CLAUDE.md), so the coercion is rejected at the boundary instead of
    producing a number that is nearly right.
    """
    if isinstance(value, float):
        raise ValueError(
            f"money must be an exact decimal, got float {value!r}; "
            "pass a Decimal or a string (CLAUDE.md: a float in the cost model is a bug)"
        )
    return value


#: A rupee amount on a journal entry. Mirrors the `money_inr` domain (NUMERIC(20, 6)).
Money = Annotated[Decimal, BeforeValidator(_reject_float)]


class Actor(StrEnum):
    """Who acted. Mirrors `decision_journal.actor`'s CHECK constraint.

    The monitoring tiers of §5.4 are separate actors on purpose: the evidence pack reports T1/T2
    verdicts against subsequent outcomes (§5.7), which is unanswerable if every automated entry
    is filed under one name.
    """

    T0 = "T0"
    """Mechanical daily check — prices against rails, CA events, keyword hits. Costs ~₹0."""

    T1 = "T1"
    """Triggered LLM review of a flagged instrument, with the thesis and its break conditions."""

    T2 = "T2"
    """Scheduled deep review of a whole case, on the case's ratified cadence."""

    RAILS = "RAILS"
    """The deterministic pre-trade checks of A8. Never an LLM (invariant #6)."""

    EXEC = "EXEC"
    """The execution layer (X1) — staging, fills, reconciliation, the kill switch."""

    USER = "USER"
    """The human: a ratification, a graduation, a manual instruction."""

    SYSTEM = "SYSTEM"
    """The platform itself — the daily loop's own bookkeeping, including data-red skips."""


class Decision(StrEnum):
    """What was decided. Mirrors `decision_journal.decision`'s CHECK constraint.

    `HEARTBEAT` is a first-class member rather than the absence of a row: invariant #9 says a day
    on which nothing happened still writes what it looked at, because "no entry" and "checked,
    nothing to do" are indistinguishable after the fact and only one of them is evidence.
    """

    HOLD = "HOLD"
    """Reviewed a position and left it alone."""

    BUY = "BUY"
    """Decided to acquire. The order itself lives in `order_`, referenced by `orders_ref`."""

    SELL = "SELL"
    """Decided to reduce or exit."""

    ESCALATE = "ESCALATE"
    """A tier handed the question up — T0 → T1, or T1 → the human."""

    HEARTBEAT = "HEARTBEAT"
    """Checked, nothing happened. Carries the evidence considered (invariant #9)."""

    SKIPPED_DATA_RED = "SKIPPED_DATA_RED"
    """`/status/sync` was not green, so no decision was made and no order placed (invariant #10)."""

    RAIL_BLOCK = "RAIL_BLOCK"
    """A8 refused an order. The breached rail is named in the rationale; there is no override."""

    POLICY_PROPOSAL = "POLICY_PROPOSAL"
    """The agent proposed a policy for the human to ratify. Proposing is never ratifying."""


class Sleeve(StrEnum):
    """Which sleeve the decision concerns (§5.5). Mirrors `decision_journal.sleeve`'s CHECK."""

    CORE = "CORE"
    """Thesis-backed holdings. Membership changes only on a BROKEN verdict (decision #4)."""

    TACTICAL = "TACTICAL"
    """The rotation sleeve, where the agent has buy/sell authority inside the ratified dial."""

    CASH = "CASH"
    """Parked capital — the liquid ETF and the deployment queue (§5.6)."""


class Verdict(StrEnum):
    """A break condition's state at evaluation time (§5.4's T1 output)."""

    INTACT = "INTACT"
    """The condition has not been met; the thesis stands."""

    WEAKENED = "WEAKENED"
    """Evidence is moving against the thesis but the condition is not met. Not an exit trigger."""

    BROKEN = "BROKEN"
    """The condition is met. The only verdict that may change core membership (§5.5)."""


#: Decisions that must say why. A `BUY` with no reason is a trade nobody can review, and §0's
#: claim — "evidence seen, conditions evaluated, reason acted" — is exactly this field.
#: `HOLD` and `HEARTBEAT` are absent because they assert that nothing changed, which the evidence
#: bundle already documents.
REQUIRES_RATIONALE: Final[frozenset[Decision]] = frozenset(
    {
        Decision.BUY,
        Decision.SELL,
        Decision.ESCALATE,
        Decision.SKIPPED_DATA_RED,
        Decision.RAIL_BLOCK,
        Decision.POLICY_PROPOSAL,
    }
)

#: Decisions that are meaningless without an instrument. A `BUY` with no ISIN cannot be
#: reconciled against the order it produced, and joining it back by symbol is invariant #2's
#: forbidden shortcut.
REQUIRES_INSTRUMENT: Final[frozenset[Decision]] = frozenset({Decision.BUY, Decision.SELL})

#: Decisions that must name the evidence bundle they were made on. Invariant #9 for `HEARTBEAT`:
#: "a day with nothing to do still writes a heartbeat *with the evidence considered*" — without
#: the bundle, a heartbeat records only that the process was alive.
REQUIRES_EVIDENCE: Final[frozenset[Decision]] = frozenset({Decision.HEARTBEAT})


class BreakConditionEvaluation(BaseModel):
    """One break condition, as it was judged in this decision (§5.7's `break_conditions_evaluated`).

    Stored per entry rather than only on the thesis: the thesis records what *would* break it, the
    journal records what the agent concluded on a given day, and the evidence pack's "T1/T2
    verdicts vs subsequent outcomes" review needs the second one dated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, description="Break-condition id from the thesis, e.g. 'BC1'.")
    verdict: Verdict = Field(description="INTACT / WEAKENED / BROKEN, per §5.4's T1 contract.")
    observed: str | None = Field(
        default=None,
        description="What was actually observed, in one line. The 'because' behind the verdict.",
    )


class TokenSpend(BaseModel):
    """What one model call cost (§5.7's `tokens`), in tokens and in rupees.

    Present only when a model was involved: T0 is mechanical and spends nothing, and a zero-cost
    row for it would make the evidence pack's burn report unreadable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens_in: int = Field(ge=0, description="Prompt tokens billed.")
    tokens_out: int = Field(ge=0, description="Completion tokens billed.")
    cost_inr: Money = Field(ge=0, description="Settled rupee cost. Decimal, never float.")


class JournalEntry(BaseModel):
    """One §5.7 entry, before it has been written.

    What it does: carries everything the journal records about a single decision, and refuses to
    exist in a shape that could not be reviewed later — no unexplained trade, no heartbeat without
    evidence, no entry dated into the future.
    What it assumes: `ts` came from an injected `Clock` (B10) and `evidence_snapshot_ref` names a
    bundle that is already stored — `Journal.append` stores the bundle first for exactly that
    reason.
    What it never does: mutate. Entries are frozen here and append-only in the database
    (invariant #12); a correction is a new entry that supersedes this one, never an edit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime = Field(
        description="When the decision was made, tz-aware, from an injected Clock."
    )
    trading_date: date = Field(description="The session this decision is *about*, in IST.")
    case_id: str | None = Field(
        default=None,
        min_length=1,
        description="The case this concerns; None for platform-wide entries (a data-red skip).",
    )
    actor: Actor = Field(description="Which tier, rail, executor or human acted.")
    decision: Decision = Field(description="What was decided, including the no-ops.")
    isin: str | None = Field(
        default=None,
        pattern=ISIN_PATTERN,
        description="The instrument, when the decision is about one. ISIN only (invariant #2).",
    )
    sleeve: Sleeve | None = Field(default=None, description="CORE, TACTICAL or CASH (§5.5).")
    evidence_snapshot_ref: str | None = Field(
        default=None,
        pattern=EVIDENCE_REF_PATTERN,
        description="`sha256:<hex>` of the bundle actually shown, from the evidence store.",
    )
    break_conditions_evaluated: tuple[BreakConditionEvaluation, ...] = Field(
        default=(), description="Verdict per break condition considered in this decision."
    )
    rationale: str | None = Field(
        default=None, min_length=1, description="Why, in the agent's own words. One paragraph."
    )
    model: str | None = Field(
        default=None, min_length=1, description="Model id, when a model produced this decision."
    )
    tokens: TokenSpend | None = Field(
        default=None, description="Token and rupee cost of that call."
    )
    orders_ref: str | None = Field(
        default=None, min_length=1, description="`order_.order_uid` this decision produced, if any."
    )
    payload: Mapping[str, str] = Field(
        default_factory=dict,
        description=(
            "Free-form extras, strings only: a JSON number read back out of jsonb is a float, "
            "and a float is a bug. Render a Decimal with str() and it round-trips exactly."
        ),
    )

    @field_validator("ts")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        """A naive instant is an error, not an assumption about the host's locale (B10)."""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                f"ts must be tz-aware, got naive {value.isoformat()!r}; take it from an injected "
                "Clock, which is always aware"
            )
        return value

    @field_validator("rationale", "model", "orders_ref", "case_id")
    @classmethod
    def _must_not_be_blank(cls, value: str | None) -> str | None:
        """Whitespace is not a rationale. `min_length` alone would accept `'   '`."""
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _check_decision_shape(self) -> JournalEntry:
        """Apply the three families of rule this module exists to enforce.

        Every message names the decision and the missing field, because the caller is an agent
        module mid-daily-loop and "validation error" tells it nothing it can act on.
        """
        if self.trading_date > self.ts.astimezone(IST).date():
            raise ValueError(
                f"trading_date {self.trading_date.isoformat()} is after the IST date of ts "
                f"{self.ts.isoformat()}: a decision cannot be about a session that has not "
                "happened yet (invariant #7)"
            )
        if self.decision in REQUIRES_RATIONALE and self.rationale is None:
            raise ValueError(
                f"a {self.decision.value} entry needs a rationale — §0 requires that every action "
                "record the reason it was taken, and this one would be unreviewable without it"
            )
        if self.decision in REQUIRES_INSTRUMENT and self.isin is None:
            raise ValueError(
                f"a {self.decision.value} entry needs the isin it concerns; a trade that cannot "
                "be tied to an instrument cannot be reconciled against the order it produced"
            )
        if self.decision in REQUIRES_INSTRUMENT and self.sleeve is None:
            raise ValueError(
                f"a {self.decision.value} entry needs its sleeve (§5.5): core and tactical trades "
                "answer to different authority, and an untagged one belongs to neither"
            )
        if self.decision in REQUIRES_EVIDENCE and self.evidence_snapshot_ref is None:
            raise ValueError(
                f"a {self.decision.value} entry needs evidence_snapshot_ref — invariant #9 is "
                "'a heartbeat with the evidence considered', and without the bundle this records "
                "only that the process was alive"
            )
        if self.tokens is not None and self.model is None:
            raise ValueError(
                "tokens were recorded without a model: a cost with no attribution cannot be "
                "reported per-tier in the evidence pack (§5.7)"
            )
        return self


class RecordedEntry(JournalEntry):
    """A `JournalEntry` as the database returned it — with its identity and its landing time.

    `id` is the journal's own ordering: `bigint GENERATED ALWAYS AS IDENTITY`, so it is monotonic
    per database and is what `order_.decision_journal_id` points at. `recorded_at` is when the row
    landed, which is *not* `ts`: a replay writes entries whose `ts` is years old, and telling the
    two apart is what makes a replayed journal distinguishable from the original.
    """

    id: int = Field(gt=0, description="Primary key of the row, assigned by the database.")
    recorded_at: datetime = Field(description="When the row was written, from an injected Clock.")
