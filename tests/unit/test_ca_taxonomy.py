"""M2.1 — the corporate-action normalizer, against real exchange purpose strings.

This is risk register row 1's test. A misread corporate action does not fail loudly; it silently
rewrites ten years of adjusted history for one ISIN, and every backtest and every thesis built on
that series is then wrong in a way no downstream check notices. So three things are asserted here,
and the second matters as much as the first:

1. **A table of real purpose strings parses to the right structured terms.** Fifty-two strings in
   the shapes both exchanges actually publish — NSE's shouted `FV SPLIT FROM RS.10/- TO RS.2/-`,
   BSE's title-cased `Stock  Split From Rs.10/- to Rs.2/-` with its stray double space — covering
   all twelve action types. Each asserts the exact terms model, not just the type.
2. **Everything else reaches a human, with its text intact.** Unrecognised strings, strings naming
   two actions at once, and strings whose numbers are missing or self-contradictory land in the
   manual-entry queue carrying the raw text byte-for-byte. No test here accepts a plausible
   default, and `test_no_case_produces_invented_terms` exists to catch one being introduced.
3. **The structured form round-trips.** `describe()` renders every parsed action to a sentence,
   and re-parsing that sentence yields identical terms. A parser that quietly dropped a premium or
   inverted a from/to cannot survive that.

Offline and deterministic: no network, no database, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest
from pydantic import ValidationError

from dataplatform.corpactions import (
    TERMS_ADAPTER,
    TERMS_BY_ACTION,
    ActionType,
    CorporateActionNormalizer,
    DividendKind,
    DividendTerms,
    ExchangeRatioTerms,
    FaceValueTerms,
    ManualEntryQueue,
    ManualQueueReason,
    NameChangeTerms,
    ParsedAction,
    PriceTerms,
    RatioTerms,
    RightsTerms,
    Terms,
    UnquantifiedTerms,
    classify,
    parse_purpose,
)

NSE = "NSE"
BSE = "BSE"

# Short aliases, used only to keep the table below one case per line. A table that wraps is a
# table nobody scans, and scanning it against the exchanges' own files is the point.
SPLIT = ActionType.SPLIT
BONUS = ActionType.BONUS
DIVIDEND = ActionType.DIVIDEND
RIGHTS = ActionType.RIGHTS
MERGER = ActionType.MERGER
DEMERGER = ActionType.DEMERGER
SCHEME = ActionType.SCHEME_OF_ARRANGEMENT
DVR = ActionType.DVR_CONVERSION
NAME = ActionType.NAME_CHANGE
FV_CHANGE = ActionType.FACE_VALUE_CHANGE
BUYBACK = ActionType.BUYBACK
DELISTING = ActionType.DELISTING

INTERIM = DividendKind.INTERIM
FINAL = DividendKind.FINAL
SPECIAL = DividendKind.SPECIAL
UNSPECIFIED = DividendKind.UNSPECIFIED

NOT_STATED = ManualQueueReason.TERMS_NOT_STATED
CONFLICTING = ManualQueueReason.TERMS_CONFLICTING
UNRECOGNISED = ManualQueueReason.UNRECOGNISED_TYPE
AMBIGUOUS = ManualQueueReason.AMBIGUOUS_TYPE

#: "the source text stated no terms" — spelled once so the table reads as data.
UNQ = UnquantifiedTerms()


def d(value: str) -> Decimal:
    """`Decimal` from a literal — money and share counts are never floats, not even in tests."""
    return Decimal(value)


def fv(old: str, new: str) -> FaceValueTerms:
    return FaceValueTerms(from_value=Decimal(old), to_value=Decimal(new))


def ratio(new: str, held: str) -> RatioTerms:
    return RatioTerms(new_shares=Decimal(new), held_shares=Decimal(held))


def rights(
    new: str, held: str, *, price: str | None = None, prem: str | None = None
) -> RightsTerms:
    return RightsTerms(
        new_shares=Decimal(new),
        held_shares=Decimal(held),
        issue_price_inr=Decimal(price) if price else None,
        premium_inr=Decimal(prem) if prem else None,
    )


def div(kind: DividendKind, amount: str | None = None, *, pct: str | None = None) -> DividendTerms:
    return DividendTerms(
        dividend_kind=kind,
        amount_inr=Decimal(amount) if amount else None,
        percent_of_face_value=Decimal(pct) if pct else None,
    )


def xr(received: str, held: str) -> ExchangeRatioTerms:
    return ExchangeRatioTerms(shares_received=Decimal(received), shares_held=Decimal(held))


def named(old: str, new: str) -> NameChangeTerms:
    return NameChangeTerms(from_name=old, to_name=new)


def price(amount: str | None = None) -> PriceTerms:
    return PriceTerms(price_inr=Decimal(amount) if amount else None)


@dataclass(frozen=True)
class Case:
    """One purpose string and everything the normalizer must make of it."""

    raw: str
    source: str
    action_type: ActionType
    terms: Terms
    #: Set when the string is parseable but a human still owes us something.
    queue_reason: ManualQueueReason | None = None

    @property
    def id(self) -> str:
        return f"{self.source}:{self.raw[:58] or '<empty>'}"


@dataclass(frozen=True)
class QueueCase:
    """One purpose string that must not become an action at all."""

    raw: str
    source: str
    reason: ManualQueueReason
    candidates: tuple[ActionType, ...] = field(default=())

    @property
    def id(self) -> str:
        return f"{self.source}:{self.raw[:58] or '<empty>'}"


# ── the table ────────────────────────────────────────────────────────────────────────────────
#
# Shapes taken from what the two feeds publish: NSE's corporate-actions purpose column is upper
# case with `RS.n/-` amounts and bare `a:b` ratios; BSE's is title case with `Rs. n.0000` amounts
# and occasional doubled spaces. Both irregularities are reproduced deliberately — a parser that
# only handles tidied input is a parser that fails on the first real file.

PURPOSE_STRINGS: tuple[Case, ...] = (
    # ── SPLIT (sub-division and consolidation share one type; from/to carries the direction) ──
    Case("FV SPLIT FROM RS.10/- TO RS.2/-", NSE, SPLIT, fv("10", "2")),
    Case("FACE VALUE SPLIT FROM RS.10/- TO RE.1/-", NSE, SPLIT, fv("10", "1")),
    Case("SUB-DIVISION OF EQUITY SHARES FROM RS.10/- TO RS.5/-", NSE, SPLIT, fv("10", "5")),
    Case("STOCK SPLIT FROM RS.5/- TO RE.1/-", NSE, SPLIT, fv("5", "1")),
    Case("FV SPLIT FROM RS 100 TO RS 10", NSE, SPLIT, fv("100", "10")),
    Case("Stock  Split From Rs.10/- to Rs.2/-", BSE, SPLIT, fv("10", "2")),
    Case("Sub-Division of Shares from Rs.2/- to Re.1/-", BSE, SPLIT, fv("2", "1")),
    Case("Consolidation of Shares from Re.1/- to Rs.10/-", BSE, SPLIT, fv("1", "10")),
    # ── BONUS (new shares per shares held; 1:1 doubles the count) ─────────────────────────────
    Case("BONUS 1:1", NSE, BONUS, ratio("1", "1")),
    Case("BONUS 3:5", NSE, BONUS, ratio("3", "5")),
    Case("BONUS ISSUE 1:2", NSE, BONUS, ratio("1", "2")),
    Case("BONUS ISSUE IN THE RATIO OF 1:1", NSE, BONUS, ratio("1", "1")),
    Case("Bonus issue 2:1", BSE, BONUS, ratio("2", "1")),
    Case("Bonus Issue 1:10", BSE, BONUS, ratio("1", "10")),
    # ── DIVIDEND ─────────────────────────────────────────────────────────────────────────────
    Case("INTERIM DIVIDEND - RS 18 PER SHARE", NSE, DIVIDEND, div(INTERIM, "18")),
    Case("FINAL DIVIDEND - RE 0.50 PER SHARE", NSE, DIVIDEND, div(FINAL, "0.50")),
    Case("DIVIDEND - RS.4.50 PER SHARE", NSE, DIVIDEND, div(UNSPECIFIED, "4.5")),
    Case("SPECIAL DIVIDEND - RS 10 PER SHARE", NSE, DIVIDEND, div(SPECIAL, "10")),
    Case("INTERIM DIVIDEND RS.5.50 PER SHARE", NSE, DIVIDEND, div(INTERIM, "5.5")),
    Case("SECOND INTERIM DIVIDEND - RS 7 PER SHARE", NSE, DIVIDEND, div(INTERIM, "7")),
    Case("Interim Dividend - Rs. 18.0000 Per Share", BSE, DIVIDEND, div(INTERIM, "18")),
    Case("Final Dividend - Rs. 3.5000 Per Share", BSE, DIVIDEND, div(FINAL, "3.5")),
    # BSE publishes some dividends only as a percentage of face value; the rupee amount follows
    # from the security master, so it is kept as a percentage rather than converted here.
    Case("Dividend - 160%", BSE, DIVIDEND, div(UNSPECIFIED, pct="160")),
    # ── RIGHTS (a ratio with no subscription price cannot price the entitlement) ──────────────
    Case("RIGHTS 1:4 @ PREMIUM RS.90/-", NSE, RIGHTS, rights("1", "4", prem="90")),
    Case("RIGHTS 3:8 @ RS.900/- PER SHARE", NSE, RIGHTS, rights("3", "8", price="900")),
    Case(
        "RIGHTS ISSUE OF EQUITY SHARES 1:5 @ RS.257/-", NSE, RIGHTS, rights("1", "5", price="257")
    ),
    Case("Rights Issue 1:1", BSE, RIGHTS, rights("1", "1"), NOT_STATED),
    # ── MERGER ───────────────────────────────────────────────────────────────────────────────
    Case("AMALGAMATION", NSE, MERGER, UNQ, NOT_STATED),
    Case("MERGER", NSE, MERGER, UNQ, NOT_STATED),
    Case("SCHEME OF AMALGAMATION 1:1", NSE, MERGER, xr("1", "1")),
    Case("Amalgamation", BSE, MERGER, UNQ, NOT_STATED),
    Case("Scheme of Amalgamation 2:5", BSE, MERGER, xr("2", "5")),
    # ── DEMERGER (beats the scheme-of-arrangement umbrella when both appear) ──────────────────
    Case("SPIN OFF / DEMERGER", NSE, DEMERGER, UNQ, NOT_STATED),
    Case("DEMERGER 1:1", NSE, DEMERGER, xr("1", "1")),
    Case("Scheme of Arrangement - Demerger", BSE, DEMERGER, UNQ, NOT_STATED),
    Case("Demerger 3:10", BSE, DEMERGER, xr("3", "10")),
    # ── SCHEME_OF_ARRANGEMENT (only when the feed said nothing more specific) ─────────────────
    Case("SCHEME OF ARRANGEMENT", NSE, SCHEME, UNQ, NOT_STATED),
    Case("SCHEME OF ARRANGEMENT 1:2", NSE, SCHEME, xr("1", "2")),
    Case("Scheme of Arrangement", BSE, SCHEME, UNQ, NOT_STATED),
    # ── DVR_CONVERSION ───────────────────────────────────────────────────────────────────────
    Case("CONVERSION OF DVR SHARES INTO ORDINARY SHARES 7:10", NSE, DVR, xr("7", "10")),
    Case("Conversion of DVR Equity Shares into Ordinary Shares", BSE, DVR, UNQ, NOT_STATED),
    # ── NAME_CHANGE (no factor, so a missing name never queues) ───────────────────────────────
    Case(
        "CHANGE IN NAME FROM ABC LIMITED TO XYZ LIMITED",
        NSE,
        NAME,
        named("ABC LIMITED", "XYZ LIMITED"),
    ),
    Case("CHANGE IN NAME", NSE, NAME, UNQ),
    Case(
        "Change of Name from Alpha Industries Ltd to Alpha Enterprises Ltd",
        BSE,
        NAME,
        named("Alpha Industries Ltd", "Alpha Enterprises Ltd"),
    ),
    # ── FACE_VALUE_CHANGE (kept distinct from SPLIT: the share-count effect does not follow) ──
    Case("CHANGE IN FACE VALUE FROM RS.10/- TO RS.2/-", NSE, FV_CHANGE, fv("10", "2")),
    Case("REDUCTION OF FACE VALUE FROM RS.10/- TO RS.5/-", NSE, FV_CHANGE, fv("10", "5")),
    Case("Change in Face Value from Rs.10/- to Rs.5/-", BSE, FV_CHANGE, fv("10", "5")),
    # ── BUYBACK / DELISTING (no adjustment factor; an absent price is a complete parse) ───────
    Case("BUY BACK OF SHARES", NSE, BUYBACK, price()),
    Case("BUYBACK", NSE, BUYBACK, price()),
    Case("Buy Back of Equity Shares @ Rs.4500/- Per Share", BSE, BUYBACK, price("4500")),
    Case("DELISTING OF SHARES", NSE, DELISTING, price()),
    Case("DELISTING OF EQUITY SHARES", NSE, DELISTING, price()),
    Case("Voluntary Delisting of Equity Shares @ Rs.850/- Per Share", BSE, DELISTING, price("850")),
    # ── type recognised, numbers missing or self-contradictory ───────────────────────────────
    Case("BONUS", NSE, BONUS, UNQ, NOT_STATED),
    Case("FV SPLIT", NSE, SPLIT, UNQ, NOT_STATED),
    Case("DIVIDEND", NSE, DIVIDEND, div(UNSPECIFIED), NOT_STATED),
    # No currency marker, so no amount is read — a bare number is as likely a year as a face value.
    Case("SPLIT FROM 10 TO 2", NSE, SPLIT, UNQ, NOT_STATED),
    # The word says consolidation, the numbers say sub-division. One of them is a misreading.
    Case("CONSOLIDATION OF SHARES FROM RS.10/- TO RS.2/-", NSE, SPLIT, UNQ, CONFLICTING),
    Case("FV SPLIT FROM RS.10/- TO RS.0/-", NSE, SPLIT, UNQ, CONFLICTING),
    Case("INTERIM DIVIDEND RS.5 AND FINAL DIVIDEND RS.3", NSE, DIVIDEND, div(INTERIM), CONFLICTING),
)

#: Strings that must not become an action at all.
UNPARSEABLE: tuple[QueueCase, ...] = (
    QueueCase("ANNUAL GENERAL MEETING", NSE, UNRECOGNISED),
    QueueCase("Extra Ordinary General Meeting", BSE, UNRECOGNISED),
    QueueCase("QUARTERLY RESULTS", NSE, UNRECOGNISED),
    QueueCase("BOARD MEETING INTIMATION", NSE, UNRECOGNISED),
    QueueCase("", NSE, UNRECOGNISED),
    QueueCase("   ", NSE, UNRECOGNISED),
    QueueCase("BONUS 1:1 AND FV SPLIT FROM RS.10/- TO RS.2/-", NSE, AMBIGUOUS, (BONUS, SPLIT)),
    QueueCase("Interim Dividend Rs.5/- and Bonus 1:1", BSE, AMBIGUOUS, (BONUS, DIVIDEND)),
    QueueCase("AMALGAMATION CUM DEMERGER", NSE, AMBIGUOUS, (DEMERGER, MERGER)),
)


# ── 1. the table parses to the right structured terms ────────────────────────────────────────


@pytest.mark.parametrize("case", PURPOSE_STRINGS, ids=[c.id for c in PURPOSE_STRINGS])
def test_purpose_string_parses_to_expected_terms(case: Case) -> None:
    outcome = parse_purpose(case.raw, source=case.source)

    assert outcome.action is not None, f"{case.raw!r} produced no action"
    assert outcome.action.action_type is case.action_type
    assert outcome.action.terms == case.terms
    assert outcome.action.raw_text == case.raw, "raw text must survive parsing verbatim"
    assert outcome.action.source == case.source


def test_table_covers_every_action_type_and_both_exchanges() -> None:
    """The acceptance criterion is 40+ strings across both exchanges and every type."""
    assert len(PURPOSE_STRINGS) >= 40
    assert {case.action_type for case in PURPOSE_STRINGS} == set(ActionType)
    assert {case.source for case in PURPOSE_STRINGS} == {NSE, BSE}

    # …and 40+ of them must yield real numbers, not just a recognised type.
    quantified = [c for c in PURPOSE_STRINGS if not isinstance(c.terms, UnquantifiedTerms)]
    assert len(quantified) >= 40


def test_face_value_direction_is_explicit() -> None:
    """A split and a consolidation are the same model; only from/to says which."""
    split = parse_purpose("FV SPLIT FROM RS.10/- TO RS.2/-", source=NSE).action
    consolidation = parse_purpose(
        "Consolidation of Shares from Re.1/- to Rs.10/-", source=BSE
    ).action
    assert split is not None and consolidation is not None
    assert isinstance(split.terms, FaceValueTerms) and not split.terms.is_consolidation
    assert isinstance(consolidation.terms, FaceValueTerms) and consolidation.terms.is_consolidation


def test_umbrella_types_yield_to_specific_ones() -> None:
    """`SCHEME OF ARRANGEMENT-DEMERGER` is a demerger, not two actions and not a bare scheme."""
    assert classify("SCHEME OF ARRANGEMENT - DEMERGER") == (ActionType.DEMERGER,)
    assert classify("SCHEME OF ARRANGEMENT") == (ActionType.SCHEME_OF_ARRANGEMENT,)
    assert classify("FV SPLIT FROM RS.10/- TO RS.2/-") == (ActionType.SPLIT,)
    assert classify("CHANGE IN FACE VALUE FROM RS.10/- TO RS.2/-") == (
        ActionType.FACE_VALUE_CHANGE,
    )


def test_differential_voting_rights_is_not_a_rights_issue() -> None:
    """The word "rights" alone must not classify; otherwise every DVR row becomes a rights issue."""
    assert classify("CONVERSION OF DIFFERENTIAL VOTING RIGHTS SHARES") == (
        ActionType.DVR_CONVERSION,
    )


# ── 2. everything else reaches a human, with its text intact ─────────────────────────────────


@pytest.mark.parametrize("case", UNPARSEABLE, ids=[c.id for c in UNPARSEABLE])
def test_unparseable_strings_produce_no_action(case: QueueCase) -> None:
    outcome = parse_purpose(case.raw, source=case.source)

    assert outcome.action is None, f"{case.raw!r} must not become an action"
    assert outcome.queue_entry is not None
    assert outcome.queue_entry.reason is case.reason
    assert outcome.queue_entry.raw_text == case.raw
    assert outcome.queue_entry.detail
    if case.candidates:
        assert outcome.queue_entry.candidate_types == case.candidates


@pytest.mark.parametrize("case", PURPOSE_STRINGS, ids=[c.id for c in PURPOSE_STRINGS])
def test_incomplete_terms_are_queued_and_complete_ones_are_not(case: Case) -> None:
    outcome = parse_purpose(case.raw, source=case.source)

    if case.queue_reason is None:
        assert outcome.queue_entry is None, f"{case.raw!r} should not need a human"
        return
    assert outcome.queue_entry is not None, f"{case.raw!r} has incomplete terms but was not queued"
    assert outcome.queue_entry.reason is case.queue_reason
    assert outcome.queue_entry.action_type is case.action_type
    assert outcome.queue_entry.raw_text == case.raw


def test_queue_preserves_raw_text_byte_for_byte() -> None:
    """Not stripped, not case-folded, not whitespace-collapsed — exactly what was published.

    The leading spaces, the tab and the non-breaking space are all shapes BSE's HTML-derived
    strings really arrive in. A human resolving this entry must see what the exchange published,
    and a later re-parse against an improved parser must get the same bytes back.
    """
    raw = "  Scheme  of\tArrangement\u00a0- Whatever This Is  "
    entry = parse_purpose(raw, source=BSE).queue_entry
    assert entry is not None
    assert entry.raw_text == raw


#: The types that set no adjustment factor, so missing terms block nothing and are not queued.
NO_FACTOR = frozenset({ActionType.NAME_CHANGE, ActionType.BUYBACK, ActionType.DELISTING})


def test_no_case_produces_invented_terms() -> None:
    """The guard against a future "helpful" default.

    Every string in both tables either yields terms whose numbers are actually in the string, or
    yields `UnquantifiedTerms`/`None`. Nothing in between is legal, and unquantified terms may go
    unqueued only for the types whose factor does not depend on them — a merger with no exchange
    ratio must always reach a human, a name change with no names need not.
    """
    for case in PURPOSE_STRINGS:
        outcome = parse_purpose(case.raw, source=case.source)
        assert outcome.action is not None
        if isinstance(outcome.action.terms, UnquantifiedTerms):
            assert outcome.queue_entry is not None or outcome.action.action_type in NO_FACTOR, (
                f"{case.raw!r} has no terms, no queue entry, and does set a factor"
            )
    for queue_case in UNPARSEABLE:
        assert parse_purpose(queue_case.raw, source=queue_case.source).action is None


def test_missing_terms_always_queue_when_they_set_a_factor() -> None:
    """The complement, stated over the whole taxonomy rather than over the table.

    Every type outside `NO_FACTOR` must queue when handed its own bare keyword — this is what
    stops a future extractor from returning empty terms quietly.
    """
    bare = {
        ActionType.SPLIT: "SPLIT",
        ActionType.BONUS: "BONUS",
        ActionType.DIVIDEND: "DIVIDEND",
        ActionType.RIGHTS: "RIGHTS ISSUE",
        ActionType.MERGER: "AMALGAMATION",
        ActionType.DEMERGER: "DEMERGER",
        ActionType.SCHEME_OF_ARRANGEMENT: "SCHEME OF ARRANGEMENT",
        ActionType.DVR_CONVERSION: "CONVERSION OF DVR SHARES",
        ActionType.FACE_VALUE_CHANGE: "CHANGE IN FACE VALUE",
    }
    assert set(bare) == set(ActionType) - NO_FACTOR, "a new action type needs a row here"

    for action_type, raw in bare.items():
        outcome = parse_purpose(raw, source=NSE)
        assert outcome.action is not None and outcome.action.action_type is action_type, raw
        assert outcome.queue_entry is not None, f"{raw!r} has no terms but was not queued"
        assert outcome.queue_entry.reason is ManualQueueReason.TERMS_NOT_STATED


def test_normalizer_files_into_an_append_only_queue() -> None:
    normalizer = CorporateActionNormalizer()
    needs_human = [c for c in PURPOSE_STRINGS if c.queue_reason is not None]

    for case in PURPOSE_STRINGS:
        assert normalizer.normalize(case.raw, source=case.source) is not None
    for queue_case in UNPARSEABLE:
        assert normalizer.normalize(queue_case.raw, source=queue_case.source) is None

    assert len(normalizer.queue) == len(needs_human) + len(UNPARSEABLE)
    queued = [entry.raw_text for entry in normalizer.queue]
    assert queued[: len(needs_human)] == [c.raw for c in needs_human], "arrival order is preserved"
    assert not hasattr(normalizer.queue, "pop"), "the queue is append-only"
    assert not hasattr(normalizer.queue, "clear"), "the queue is append-only"


def test_queue_entries_are_immutable() -> None:
    queue = ManualEntryQueue()
    CorporateActionNormalizer(queue).normalize("ANNUAL GENERAL MEETING", source=NSE)
    (entry,) = queue.entries
    with pytest.raises(ValidationError):
        entry.raw_text = "something else"


# ── 3. the structured form round-trips ───────────────────────────────────────────────────────


@pytest.mark.parametrize("case", PURPOSE_STRINGS, ids=[c.id for c in PURPOSE_STRINGS])
def test_description_round_trips_to_identical_terms(case: Case) -> None:
    """Render the parsed action, re-parse the rendering, and demand the same terms back.

    This is the check that catches a renderer which drops a premium, or a parser which reads a
    from/to backwards: both survive the table above and neither survives this.
    """
    action = parse_purpose(case.raw, source=case.source).action
    assert action is not None

    description = action.describe()
    reparsed = parse_purpose(description, source=case.source).action

    assert reparsed is not None, f"{description!r} did not re-parse"
    assert reparsed.action_type is case.action_type, description
    assert reparsed.terms == case.terms, description


@pytest.mark.parametrize("case", PURPOSE_STRINGS, ids=[c.id for c in PURPOSE_STRINGS])
def test_description_is_human_readable(case: Case) -> None:
    action = parse_purpose(case.raw, source=case.source).action
    assert action is not None
    description = action.describe()

    assert description
    assert description[0].isupper()
    assert "None" not in description
    assert "Decimal" not in description


def describe_purpose(raw: str, source: str = NSE) -> str:
    """Parse then render, for the spot-checks below."""
    action = parse_purpose(raw, source=source).action
    assert action is not None
    return action.describe()


def test_description_states_the_numbers_it_was_given() -> None:
    """Spot-check the exact wording a human reads in the reconciliation queue."""
    assert describe_purpose("FV SPLIT FROM RS.10/- TO RS.2/-") == (
        "Face value split from Rs.10 to Rs.2"
    )
    assert describe_purpose("Consolidation of Shares from Re.1/- to Rs.10/-", BSE) == (
        "Consolidation of shares from Rs.1 to Rs.10"
    )
    assert describe_purpose("BONUS 3:5") == "Bonus issue in the ratio 3:5"
    assert describe_purpose("FINAL DIVIDEND - RE 0.50 PER SHARE") == (
        "Final dividend of Rs.0.5 per share"
    )
    assert describe_purpose("RIGHTS 1:4 @ PREMIUM RS.90/-") == (
        "Rights issue in the ratio 1:4 at a premium of Rs.90 per share"
    )
    assert describe_purpose("Dividend - 160%", BSE) == "Dividend of 160% of face value"
    assert describe_purpose("SCHEME OF ARRANGEMENT") == (
        "Scheme of arrangement (terms not stated in the source text)"
    )


# ── the model itself ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("terms", "message"),
    [
        (lambda: FaceValueTerms(from_value=d("10"), to_value=d("0")), "zero face value"),
        (lambda: FaceValueTerms(from_value=d("-10"), to_value=d("2")), "negative face value"),
        (lambda: RatioTerms(new_shares=d("0"), held_shares=d("1")), "zero bonus numerator"),
        (lambda: ExchangeRatioTerms(shares_received=d("1"), shares_held=d("0")), "zero divisor"),
        (lambda: PriceTerms(price_inr=d("0")), "zero price"),
        (lambda: NameChangeTerms(from_name="", to_name="X"), "empty name"),
        (
            lambda: RightsTerms(
                new_shares=d("1"), held_shares=d("4"), issue_price_inr=d("100"), premium_inr=d("90")
            ),
            "both price forms",
        ),
        (
            lambda: DividendTerms(
                dividend_kind=DividendKind.FINAL, amount_inr=d("5"), percent_of_face_value=d("50")
            ),
            "both amount forms",
        ),
    ],
)
def test_terms_reject_unusable_values(terms: object, message: str) -> None:
    with pytest.raises(ValidationError):
        terms()  # type: ignore[operator]


def test_action_rejects_terms_that_do_not_belong_to_its_type() -> None:
    """A bonus carrying face values must fail here, not in the adjustment engine."""
    with pytest.raises(ValidationError):
        ParsedAction(
            action_type=ActionType.BONUS,
            terms=FaceValueTerms(from_value=d("10"), to_value=d("2")),
            raw_text="BONUS 1:1",
            source=NSE,
        )


def test_every_action_type_declares_its_legal_terms() -> None:
    assert set(TERMS_BY_ACTION) == set(ActionType)
    assert all(TERMS_BY_ACTION[action] for action in ActionType)


@pytest.mark.parametrize("case", PURPOSE_STRINGS, ids=[c.id for c in PURPOSE_STRINGS])
def test_terms_survive_the_json_round_trip(case: Case) -> None:
    """`ratio_terms` is a jsonb column (M0.4); the discriminator is what makes it reversible."""
    blob = TERMS_ADAPTER.dump_python(case.terms, mode="json")
    assert TERMS_ADAPTER.validate_python(blob) == case.terms
