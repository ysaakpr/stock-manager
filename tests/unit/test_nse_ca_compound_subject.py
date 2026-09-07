"""M2.2 — NSE joins several events on one line, and the parser used to refuse the whole line.

`Bonus 2:1/Dividend- Rs 1.60 Per Share` is how NSE's corporate-action feed publishes a bonus and a
dividend with one ex-date. `parse_purpose` reads it as one string naming two types and queues it as
AMBIGUOUS — correct for a string that *is* one event described two ways, wrong for a list. The cost
was measured on 2026-09-07: UNOMINDA's 2:1 bonus (2018) and HINDPETRO's 1:2 bonus (2017) never
became NSE actions, their BSE twins were left single-source, and L2 carried both unadjusted.

The NSE parser now splits a subject on the feed's separator — a slash followed by a letter, so the
rupee idiom `Rs 10/-` and a date never split — and parses each event on its own. Every action and
queue entry that results still carries the whole published line, so provenance is unchanged.

Offline: a synthetic payload, a master of one ISIN, no network.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from dataplatform.clock import IST, FrozenClock
from dataplatform.corpactions.parse_terms import ManualQueueReason
from dataplatform.corpactions.taxonomy import (
    ActionType,
    DividendTerms,
    FaceValueTerms,
    RatioTerms,
    UnquantifiedTerms,
)
from dataplatform.identity.master import (
    Exchange,
    IdentityMaster,
    ListingStatus,
    Security,
    SymbolWindow,
)
from dataplatform.ingest.nse.corp_actions import parse

ISIN = "INE405E01023"  # UNOMINDA
CLOCK = FrozenClock(datetime(2026, 9, 7, 12, 0, tzinfo=IST))


def _master(*isins: str) -> IdentityMaster:
    return IdentityMaster(
        [
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="UNOMINDA",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin=isin,
                series="EQ",
                source="test",
            )
            for isin in isins
        ],
        securities=[
            Security(
                isin=isin,
                name="UNO Minda",
                primary_exchange=Exchange.NSE,
                status=ListingStatus.ACTIVE,
                first_seen_date=date(2000, 1, 1),
            )
            for isin in isins
        ],
        listings=[],
    )


def _payload(subject: str, *, isin: str = ISIN) -> bytes:
    return json.dumps(
        [
            {
                "symbol": "UNOMINDA",
                "isin": isin,
                "exDate": "11-Jul-2018",
                "subject": subject,
                "series": "EQ",
                "faceVal": 2,
                "recDate": "12-Jul-2018",
                "caBroadcastDate": "25-Jun-2018 17:32:04",
            }
        ]
    ).encode()


def _parse(subject: str, *, isin: str = ISIN, known: tuple[str, ...] = (ISIN,)) -> object:
    return parse(
        _payload(subject, isin=isin), filename="ca.json", master=_master(*known), clock=CLOCK
    )


def test_a_bonus_and_a_dividend_on_one_line_are_two_actions() -> None:
    subject = "Bonus 2:1/Dividend- Rs 1.60 Per Share"
    result = _parse(subject)

    assert result.queued == () and result.unresolved == ()  # type: ignore[attr-defined]
    by_type = {a.action_type: a for a in result.actions}  # type: ignore[attr-defined]
    assert set(by_type) == {ActionType.BONUS, ActionType.DIVIDEND}
    assert by_type[ActionType.BONUS].terms == RatioTerms(
        new_shares=Decimal(2), held_shares=Decimal(1)
    )
    dividend = by_type[ActionType.DIVIDEND].terms
    assert isinstance(dividend, DividendTerms) and dividend.amount_inr == Decimal("1.60")
    # Both carry the line exactly as published and the same identity and dates.
    assert {a.raw_text for a in result.actions} == {subject}  # type: ignore[attr-defined]
    assert {a.isin for a in result.actions} == {ISIN}  # type: ignore[attr-defined]
    assert {a.ex_date for a in result.actions} == {date(2018, 7, 11)}  # type: ignore[attr-defined]


def test_the_rupee_idiom_is_not_a_separator() -> None:
    """`Rs 10/-` closes an amount; only a slash followed by a letter separates events."""
    subject = (
        "Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"
    )
    result = _parse(subject)

    by_type = {a.action_type: a for a in result.actions}  # type: ignore[attr-defined]
    assert set(by_type) == {ActionType.BONUS, ActionType.SPLIT}
    assert by_type[ActionType.SPLIT].terms == FaceValueTerms(
        from_value=Decimal(10), to_value=Decimal(2)
    )
    assert result.queued == ()  # type: ignore[attr-defined]


def test_a_meeting_beside_a_dividend_is_not_something_a_human_must_resolve() -> None:
    result = _parse("Annual General Meeting/Dividend - Rs 10 Per Share")

    (action,) = result.actions  # type: ignore[attr-defined]
    assert action.action_type is ActionType.DIVIDEND
    assert result.queued == (), "a segment with no action keyword is an event, not a defect"  # type: ignore[attr-defined]


def test_a_line_naming_no_action_at_all_is_queued_once_as_before() -> None:
    result = _parse("Annual General Meeting/Book Closure")

    assert result.actions == ()  # type: ignore[attr-defined]
    (entry,) = result.queued  # type: ignore[attr-defined]
    assert entry.reason is ManualQueueReason.UNRECOGNISED_TYPE
    assert entry.raw_text == "Annual General Meeting/Book Closure"


def test_a_segment_missing_its_terms_queues_the_whole_line() -> None:
    subject = "Capital Reduction Rs 10 To Rs 3.30 / Consolidation Rs 3.30 To Rs.10"
    result = _parse(subject)

    (action,) = result.actions  # type: ignore[attr-defined]
    assert action.action_type is ActionType.SPLIT
    assert isinstance(action.terms, UnquantifiedTerms)
    assert action.raw_text == subject
    (entry,) = result.queued  # type: ignore[attr-defined]
    assert entry.reason is ManualQueueReason.TERMS_NOT_STATED
    assert entry.raw_text == subject, "the queue carries the published line, not the fragment"
    assert "Consolidation" in entry.detail


def test_a_plain_subject_is_parsed_exactly_as_before() -> None:
    result = _parse("Bonus 1:4")

    (action,) = result.actions  # type: ignore[attr-defined]
    assert action.action_type is ActionType.BONUS
    assert action.terms == RatioTerms(new_shares=Decimal(1), held_shares=Decimal(4))
    assert action.raw_text == "Bonus 1:4"
    assert result.queued == ()  # type: ignore[attr-defined]


def test_an_unresolvable_compound_line_is_one_unresolved_note() -> None:
    result = _parse("Bonus 2:1/Dividend- Rs 1.60 Per Share", isin="INE999Z01019", known=(ISIN,))

    assert result.actions == ()  # type: ignore[attr-defined]
    (note,) = result.unresolved  # type: ignore[attr-defined]
    assert note.raw_text == "Bonus 2:1/Dividend- Rs 1.60 Per Share"
