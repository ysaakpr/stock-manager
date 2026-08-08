"""D3: the normalized corporate-action taxonomy and its structured terms.

A corporate action reaches this platform as a line of free text. NSE writes
`FV SPLIT FROM RS.10/- TO RS.2/-`; BSE writes `Stock  Split From Rs.10/- to Rs.2/-`; both mean the
same 5-for-1 sub-division, and getting that wrong silently rewrites ten years of adjusted history
for that ISIN (risk register row 1, EXECUTION_PLAN §4.3). This module is the vocabulary that free
text is normalized *into*: twelve action types, and a small closed set of terms models that say
exactly what an action's numbers are — with explicit `from`/`to` values rather than a ratio whose
direction a later reader has to infer.

Three properties shape the design.

*Terms are typed per action, not a bag.* A dividend and a demerger have nothing in common but an
ex-date, so one `ratio_terms` dict with optional everything would push every "is this field
populated for this type?" question into the adjustment engine. Instead each action type declares
which terms models are legal for it (`TERMS_BY_ACTION`), and `ParsedAction` refuses the
combinations that are not — a bonus carrying face values fails at construction, not in M2.4.

*Absent terms are a value, not a zero.* `UnquantifiedTerms` means "the source text did not state
the terms". It is what an `AMALGAMATION` with no exchange ratio normalizes to, and it is
deliberately distinguishable from a parsed 1:1, because the adjustment engine must refuse to
compute a factor from the first and may compute one from the second. Nothing in this module ever
substitutes a plausible default for a missing number.

*Every action can be rendered back to a sentence.* `describe()` is not decoration: it is what the
reconciliation queue (M2.3) and the decision journal (A9) show a human, and its output is written
so that `parse_purpose` reads it back to identical terms. That round trip is the cheap proof that
the structured form did not quietly lose or invent anything.

Money is `Decimal` throughout. Nothing here reads a clock, a database or the network.

Note for M2.2: `corporate_actions.action_type` in `0001_init.sql` predates this taxonomy and
enumerates a different set (it has `CONSOLIDATION` and `OTHER`, and lacks
`SCHEME_OF_ARRANGEMENT`, `DVR_CONVERSION`, `NAME_CHANGE`, `BUYBACK`, `DELISTING`). The task that
first writes these rows owns the migration that widens that CHECK; see `ops/BACKLOG.md`.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

__all__ = [
    "TERMS_ADAPTER",
    "TERMS_BY_ACTION",
    "ActionType",
    "DividendKind",
    "DividendTerms",
    "ExchangeRatioTerms",
    "FaceValueTerms",
    "NameChangeTerms",
    "ParsedAction",
    "PriceTerms",
    "RatioTerms",
    "RightsTerms",
    "Terms",
    "UnquantifiedTerms",
    "describe",
    "tidy",
]


class ActionType(StrEnum):
    """The normalized action types. Every purpose string becomes one of these, or nothing.

    "Or nothing" is the point: there is no `OTHER` bucket. A string this taxonomy does not
    recognise goes to the manual-entry queue with its text intact rather than being filed under a
    catch-all that the adjustment engine would then have to guess about.
    """

    SPLIT = "SPLIT"
    """Sub-division *or* consolidation of face value — `FaceValueTerms` carries the direction.

    One type, not two, because the arithmetic is identical and the direction is already explicit
    in from/to: a 10→2 split multiplies share count by 5, a 1→10 consolidation multiplies it by
    0.1. A separate CONSOLIDATION type would be a second name for the same computation and a
    second place to get its sign wrong.
    """

    BONUS = "BONUS"
    """Free shares issued to holders. `RatioTerms` is new-per-held: `BONUS 1:1` doubles the
    share count."""

    DIVIDEND = "DIVIDEND"
    """Cash distribution per share, or a percentage of face value where that is all the feed
    states."""

    RIGHTS = "RIGHTS"
    """Entitlement to subscribe new shares at a stated price. Ratio and price together set the
    factor, so a ratio alone is incomplete terms."""

    MERGER = "MERGER"
    """Amalgamation into another entity. A structural break, never a return (§4.3 rule 3)."""

    DEMERGER = "DEMERGER"
    """Spin-off of a business into a new listed entity. Also a structural break."""

    SCHEME_OF_ARRANGEMENT = "SCHEME_OF_ARRANGEMENT"
    """A court/NCLT scheme the feed did not further classify. Umbrella: if the same string also
    names a merger, demerger or DVR conversion, the specific type wins."""

    DVR_CONVERSION = "DVR_CONVERSION"
    """Differential-voting-rights shares converted into ordinary shares at a stated ratio."""

    NAME_CHANGE = "NAME_CHANGE"
    """Company renamed. No price or quantity effect; it matters to identity (D2), not to factors."""

    FACE_VALUE_CHANGE = "FACE_VALUE_CHANGE"
    """Face value changed *without* the text saying it was a sub-division — a capital reduction, a
    re-denomination. Kept distinct from SPLIT because the share-count effect does not follow from
    the face values alone, and assuming it does is exactly the guess this taxonomy forbids."""

    BUYBACK = "BUYBACK"
    """Company repurchases shares, usually by tender. No adjustment factor."""

    DELISTING = "DELISTING"
    """Shares cease to trade, voluntarily or by exchange action."""


class DividendKind(StrEnum):
    """Which dividend it is. `UNSPECIFIED` when the feed said only "dividend"."""

    INTERIM = "INTERIM"
    FINAL = "FINAL"
    SPECIAL = "SPECIAL"
    UNSPECIFIED = "UNSPECIFIED"


class _TermsBase(BaseModel):
    """Shared configuration: frozen, closed, and comparable by value.

    Frozen because terms are evidence about a past event — mutating one after the factor chain has
    been computed from it is how history silently changes. Closed (`extra="forbid"`) because a
    typo'd field in a stored `ratio_terms` blob must fail on load, not read back as absent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class UnquantifiedTerms(_TermsBase):
    """The action type is known; the source text did not state the terms.

    Never a stand-in for "terms we could not be bothered to parse" — the parser emits this only
    alongside a manual-queue entry, so a human is always asked for the numbers.
    """

    kind: Literal["unquantified"] = "unquantified"


class FaceValueTerms(_TermsBase):
    """Face value before and after, in rupees. Used by SPLIT and FACE_VALUE_CHANGE.

    Explicit from/to rather than a multiple: `FV SPLIT FROM RS.10 TO RS.2` and a hypothetical
    "5:1 split" are the same event, but only the first form survives a reader who does not know
    which side of the ratio is which.
    """

    kind: Literal["face_value"] = "face_value"
    from_value: Decimal = Field(gt=0, description="Face value per share before the action, INR.")
    to_value: Decimal = Field(gt=0, description="Face value per share after the action, INR.")

    @property
    def is_consolidation(self) -> bool:
        """True when face value rose — shares were consolidated, not sub-divided."""
        return self.to_value > self.from_value


class RatioTerms(_TermsBase):
    """New shares received per shares already held. Used by BONUS.

    Additive, not a replacement: `BONUS 1:1` leaves a holder of 1 share with 2, so the quantity
    multiple is `(new + held) / held`. `ExchangeRatioTerms` is the replacement-shaped cousin, kept
    separate precisely so the adjustment engine cannot confuse the two arithmetics.
    """

    kind: Literal["ratio"] = "ratio"
    new_shares: Decimal = Field(gt=0, description="Shares issued for every `held_shares` held.")
    held_shares: Decimal = Field(gt=0, description="Existing shares the entitlement is per.")


class RightsTerms(_TermsBase):
    """A rights entitlement: ratio plus the money a holder must pay to take it up.

    The subscription price is what makes a rights issue adjustable at all — without it the
    theoretical ex-rights price is undefined — so a ratio with neither price nor premium is
    incomplete terms, and the parser queues it.

    Exactly one of `issue_price_inr` and `premium_inr` may be set: feeds state one or the other
    ("@ RS.900/- PER SHARE" vs "@ PREMIUM RS.90/-"), and holding both would let a rendered
    description silently drop one of them.
    """

    kind: Literal["rights"] = "rights"
    new_shares: Decimal = Field(gt=0, description="Shares offered for every `held_shares` held.")
    held_shares: Decimal = Field(gt=0, description="Existing shares the entitlement is per.")
    issue_price_inr: Decimal | None = Field(
        default=None, gt=0, description="Full subscription price per new share, INR."
    )
    premium_inr: Decimal | None = Field(
        default=None, gt=0, description="Premium over face value per new share, INR."
    )

    @model_validator(mode="after")
    def _at_most_one_price_form(self) -> RightsTerms:
        if self.issue_price_inr is not None and self.premium_inr is not None:
            raise ValueError("rights terms carry either an issue price or a premium, never both")
        return self


class DividendTerms(_TermsBase):
    """A cash distribution, as rupees per share or as a percentage of face value.

    BSE sometimes publishes only "DIVIDEND-160%". That is a complete statement of the terms — the
    rupee amount follows from the security's face value, which the identity master knows — so it
    is kept as a percentage rather than converted here with a face value this module has no
    business fetching. Exactly one of the two may be set.
    """

    kind: Literal["dividend"] = "dividend"
    dividend_kind: DividendKind
    amount_inr: Decimal | None = Field(default=None, gt=0, description="Rupees per share.")
    percent_of_face_value: Decimal | None = Field(
        default=None, gt=0, description="Percent of face value, as published (160 means 160%)."
    )

    @model_validator(mode="after")
    def _at_most_one_amount_form(self) -> DividendTerms:
        if self.amount_inr is not None and self.percent_of_face_value is not None:
            raise ValueError("dividend terms carry either a rupee amount or a percentage")
        return self


class ExchangeRatioTerms(_TermsBase):
    """Shares received in the resulting entity per shares held in this one.

    Replacement-shaped: in a 1:1 amalgamation a holder of 1 share ends up with 1 share of the
    acquirer, not 2. Used by MERGER, DEMERGER, SCHEME_OF_ARRANGEMENT and DVR_CONVERSION.

    The resulting entity is deliberately *not* a field. Extracting a company name from free text
    and treating it as an identity is precisely the guess this module refuses to make; the
    counterparty is established by reconciliation against the other exchange's row and the
    identity master (M2.3), on ISIN. The raw text is preserved on `ParsedAction` either way.
    """

    kind: Literal["exchange_ratio"] = "exchange_ratio"
    shares_received: Decimal = Field(gt=0, description="Shares received per `shares_held` held.")
    shares_held: Decimal = Field(gt=0, description="Shares held that the entitlement is per.")


class NameChangeTerms(_TermsBase):
    """Old and new company name, verbatim from the source text (case preserved)."""

    kind: Literal["name_change"] = "name_change"
    from_name: str = Field(min_length=1)
    to_name: str = Field(min_length=1)


class PriceTerms(_TermsBase):
    """A per-share price, where the action has one. Used by BUYBACK and DELISTING.

    `None` is a complete parse, not a gap: exchange purpose strings routinely say only "BUY BACK
    OF SHARES", and neither action produces an adjustment factor, so a missing price blocks
    nothing downstream and is not queued for a human.
    """

    kind: Literal["price"] = "price"
    price_inr: Decimal | None = Field(default=None, gt=0)


#: The tagged union stored in `corporate_actions.ratio_terms`. The `kind` discriminator is what
#: makes a jsonb blob round-trip back into the right model instead of the first one that fits.
Terms = Annotated[
    UnquantifiedTerms
    | FaceValueTerms
    | RatioTerms
    | RightsTerms
    | DividendTerms
    | ExchangeRatioTerms
    | NameChangeTerms
    | PriceTerms,
    Field(discriminator="kind"),
]

#: Serializer/deserializer for `Terms`. M2.2 writes `TERMS_ADAPTER.dump_python(terms, mode="json")`
#: into the `ratio_terms` jsonb column and reads it back with `validate_python`.
TERMS_ADAPTER: Final[TypeAdapter[Terms]] = TypeAdapter(Terms)

#: Which terms models each action type may legally carry. The engine that computes factors reads
#: this instead of re-deriving "can a dividend have a ratio?" from first principles every time.
TERMS_BY_ACTION: Final[dict[ActionType, tuple[type[BaseModel], ...]]] = {
    ActionType.SPLIT: (FaceValueTerms, UnquantifiedTerms),
    ActionType.BONUS: (RatioTerms, UnquantifiedTerms),
    ActionType.DIVIDEND: (DividendTerms,),
    ActionType.RIGHTS: (RightsTerms, UnquantifiedTerms),
    ActionType.MERGER: (ExchangeRatioTerms, UnquantifiedTerms),
    ActionType.DEMERGER: (ExchangeRatioTerms, UnquantifiedTerms),
    ActionType.SCHEME_OF_ARRANGEMENT: (ExchangeRatioTerms, UnquantifiedTerms),
    ActionType.DVR_CONVERSION: (ExchangeRatioTerms, UnquantifiedTerms),
    ActionType.NAME_CHANGE: (NameChangeTerms, UnquantifiedTerms),
    ActionType.FACE_VALUE_CHANGE: (FaceValueTerms, UnquantifiedTerms),
    ActionType.BUYBACK: (PriceTerms,),
    ActionType.DELISTING: (PriceTerms,),
}


class ParsedAction(BaseModel):
    """One purpose string, normalized: what kind of action it is and what its numbers are.

    What it holds: the action type, its terms, the source id the string came from, and the raw
    text **verbatim** — never a cleaned-up copy, because reconciliation (M2.3) and any later
    re-parse must be able to see exactly what the exchange published.

    What it never holds: ISIN, ex-date, record date. Those come from the feed row around the
    purpose string and are M2.2's job; this is the normalizer's output, not the database row.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_type: ActionType
    terms: Terms
    raw_text: str = Field(description="The exchange's purpose string, exactly as published.")
    source: str = Field(min_length=1, description="Source register id, e.g. 'NSE' or 'BSE'.")

    @model_validator(mode="after")
    def _terms_are_legal_for_type(self) -> ParsedAction:
        allowed = TERMS_BY_ACTION[self.action_type]
        if not isinstance(self.terms, allowed):
            names = ", ".join(model.__name__ for model in allowed)
            raise ValueError(
                f"{self.action_type} takes {names}, not {type(self.terms).__name__}",
            )
        return self

    def describe(self) -> str:
        """This action as one human-readable sentence. See `describe()`."""
        return describe(self.action_type, self.terms)


# ── rendering ────────────────────────────────────────────────────────────────────────────────
#
# The wording below is not free: `parse_purpose` must read every one of these strings back to the
# terms that produced it, and `test_ca_taxonomy.py` asserts that for every case in its table. So
# each phrase keeps the keyword that classifies its type ("split", "amalgamation", "buy back")
# and states its numbers in a form the parser's regexes accept. Change the prose and the
# round-trip test tells you immediately.

#: What an action of each type reads as when the source text stated no terms.
_UNQUANTIFIED: Final[dict[ActionType, str]] = {
    ActionType.SPLIT: "Face value split",
    ActionType.BONUS: "Bonus issue",
    ActionType.DIVIDEND: "Dividend",
    ActionType.RIGHTS: "Rights issue",
    ActionType.MERGER: "Amalgamation",
    ActionType.DEMERGER: "Demerger",
    ActionType.SCHEME_OF_ARRANGEMENT: "Scheme of arrangement",
    ActionType.DVR_CONVERSION: "Conversion of DVR shares into ordinary shares",
    ActionType.NAME_CHANGE: "Change in name",
    ActionType.FACE_VALUE_CHANGE: "Change in face value",
    ActionType.BUYBACK: "Buyback of shares",
    ActionType.DELISTING: "Delisting of equity shares",
}

#: How each exchange-ratio type names its ratio. A merger exchanges shares; a demerger entitles
#: you to shares of the new entity; the words differ because the events do.
_EXCHANGE_RATIO_PHRASE: Final[dict[ActionType, str]] = {
    ActionType.MERGER: "Amalgamation in the exchange ratio",
    ActionType.DEMERGER: "Demerger in the entitlement ratio",
    ActionType.SCHEME_OF_ARRANGEMENT: "Scheme of arrangement in the ratio",
    ActionType.DVR_CONVERSION: "Conversion of DVR shares into ordinary shares in the ratio",
}

_DIVIDEND_PREFIX: Final[dict[DividendKind, str]] = {
    DividendKind.INTERIM: "Interim dividend",
    DividendKind.FINAL: "Final dividend",
    DividendKind.SPECIAL: "Special dividend",
    DividendKind.UNSPECIFIED: "Dividend",
}

#: Appended when the type is known but the numbers are not — the rendered form of the same fact
#: that put the action in the manual-entry queue.
_NOT_STATED: Final[str] = "(terms not stated in the source text)"


def tidy(value: Decimal) -> Decimal:
    """`Decimal` without trailing-zero noise, so 18.0000 and 18 render and store identically.

    Assumes the value is a quantity or a rupee amount, not a factor: it never rounds, it only
    drops zeros that carry no information. `normalize()` alone would turn 10 into `1E+1`, which
    is why integral values take the quantize path.
    """
    if value == value.to_integral_value():
        return value.quantize(Decimal(1))
    return value.normalize()


def _num(value: Decimal) -> str:
    return f"{tidy(value):f}"


def _money(value: Decimal) -> str:
    return f"Rs.{_num(value)}"


def describe(action_type: ActionType, terms: Terms) -> str:
    """Render an action as one human-readable sentence.

    What it does: produces the line shown in the reconciliation queue, the decision journal and
    any operator-facing report, in wording `parse_purpose` reads back to identical terms.
    What it assumes: `terms` is legal for `action_type` (`ParsedAction` guarantees this).
    What it never does: consult a database, a face value, or the raw text — the structured terms
    are the whole input, which is what makes the round trip a real check on them.
    """
    match terms:
        case UnquantifiedTerms():
            return f"{_UNQUANTIFIED[action_type]} {_NOT_STATED}"
        case FaceValueTerms():
            lead = (
                "Consolidation of shares from"
                if action_type is ActionType.SPLIT and terms.is_consolidation
                else "Face value split from"
                if action_type is ActionType.SPLIT
                else "Change in face value from"
            )
            return f"{lead} {_money(terms.from_value)} to {_money(terms.to_value)}"
        case RatioTerms():
            return f"Bonus issue in the ratio {_num(terms.new_shares)}:{_num(terms.held_shares)}"
        case RightsTerms():
            base = f"Rights issue in the ratio {_num(terms.new_shares)}:{_num(terms.held_shares)}"
            if terms.premium_inr is not None:
                return f"{base} at a premium of {_money(terms.premium_inr)} per share"
            if terms.issue_price_inr is not None:
                return f"{base} at {_money(terms.issue_price_inr)} per share"
            return base
        case DividendTerms():
            prefix = _DIVIDEND_PREFIX[terms.dividend_kind]
            if terms.amount_inr is not None:
                return f"{prefix} of {_money(terms.amount_inr)} per share"
            if terms.percent_of_face_value is not None:
                return f"{prefix} of {_num(terms.percent_of_face_value)}% of face value"
            return f"{prefix} (amount not stated in the source text)"
        case ExchangeRatioTerms():
            ratio = f"{_num(terms.shares_received)}:{_num(terms.shares_held)}"
            return f"{_EXCHANGE_RATIO_PHRASE[action_type]} {ratio}"
        case NameChangeTerms():
            return f"Change in name from {terms.from_name} to {terms.to_name}"
        case PriceTerms():
            base = _UNQUANTIFIED[action_type]
            if terms.price_inr is None:
                return base
            return f"{base} at {_money(terms.price_inr)} per share"
        case _:  # pragma: no cover - exhaustiveness is checked by the type checker
            assert_never(terms)
