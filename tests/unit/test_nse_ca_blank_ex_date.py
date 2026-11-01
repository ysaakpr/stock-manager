"""M2.3 — a CA-feed record with a blank ex-date: derived only for a T+1 face-value change.

NSE's corporate-action API is queried by ex-date, so a record with none (AvenuesAI's 2026-10-13
consolidation) has never come back from it; if one ever does, a blank `exDate` used to fail the
whole file. Now a split or consolidation that states its record date gets the T+1 ex-date
(`corpactions.ex_date_rule`: ex = record, a session on/after 2023-01-27) and carries the rule as
provenance; anything else blank still fails the file, exactly as before.

Offline: a synthetic payload in the feed's own keys, a master of one ISIN, no network.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.corpactions.taxonomy import ActionType, FaceValueTerms
from dataplatform.identity.master import (
    Exchange,
    IdentityMaster,
    ListingStatus,
    Security,
    SymbolWindow,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.corp_actions import parse

ISIN = "INE483S01038"
CLOCK = FrozenClock(datetime(2026, 10, 13, 12, 0, tzinfo=IST))


def _master() -> IdentityMaster:
    return IdentityMaster(
        [
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="CCAVENUE",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin=ISIN,
                series="EQ",
                source="test",
            )
        ],
        securities=[
            Security(
                isin=ISIN,
                name="AvenuesAI",
                primary_exchange=Exchange.NSE,
                status=ListingStatus.ACTIVE,
                first_seen_date=date(2000, 1, 1),
            )
        ],
        listings=[],
    )


def _parse(subject: str, *, rec: str) -> object:
    payload = json.dumps(
        [
            {
                "symbol": "CCAVENUE",
                "isin": ISIN,
                "exDate": "-",
                "subject": subject,
                "series": "EQ",
                "recDate": rec,
                "caBroadcastDate": "12-Oct-2026 17:32:04",
            }
        ]
    ).encode()
    return parse(payload, filename="ca.json", master=_master(), clock=CLOCK)


def test_a_t1_consolidation_with_a_blank_ex_date_gets_the_record_date_and_the_rule() -> None:
    result = _parse("Consolidation of shares from Re 1/- to Rs 10/-", rec="13-Oct-2026")
    [action] = result.actions  # type: ignore[attr-defined]
    assert action.action_type is ActionType.SPLIT
    assert action.terms == FaceValueTerms(from_value=Decimal(1), to_value=Decimal(10))
    assert action.ex_date == date(2026, 10, 13) and action.record_date == date(2026, 10, 13)
    assert action.ex_date_rule == "T1_RECORD_DATE"


def test_a_stated_ex_date_carries_no_rule() -> None:
    payload = json.dumps(
        [
            {
                "symbol": "CCAVENUE",
                "isin": ISIN,
                "exDate": "13-Oct-2026",
                "subject": "Consolidation of shares from Re 1/- to Rs 10/-",
                "series": "EQ",
                "recDate": "13-Oct-2026",
            }
        ]
    ).encode()
    [action] = parse(payload, filename="ca.json", master=_master(), clock=CLOCK).actions
    assert action.ex_date_rule is None


@pytest.mark.parametrize(
    ("subject", "rec"),
    [
        # T+2 era: never derived
        ("Consolidation of shares from Re 1/- to Rs 10/-", "14-Aug-2018"),
        # a Saturday record date is no session
        ("Consolidation of shares from Re 1/- to Rs 10/-", "17-Oct-2026"),
        # not a face-value change: the rule is for splits and consolidations only
        ("Dividend - Rs 2 Per Share", "13-Oct-2026"),
        # no record date stated
        ("Consolidation of shares from Re 1/- to Rs 10/-", "-"),
    ],
)
def test_any_other_blank_ex_date_still_fails_the_file(subject: str, rec: str) -> None:
    with pytest.raises(ParseError, match="ex-date is mandatory"):
        _parse(subject, rec=rec)
