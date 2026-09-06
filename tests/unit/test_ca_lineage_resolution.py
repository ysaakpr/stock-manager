"""An NSE action filed against a retired ISIN reaches the surviving security, or is held back.

This is the join between D2's lineage (0009) and D3's corporate actions (0010). Before it, a
face-value split was refused at ingest because it names the ISIN it retires: 362 SPLITs in the
L0 payloads, 17 in the store. Each test here is one way the fallback could be too permissive —
resolving without a lineage, onto an unknown survivor, or over the top of a live ISIN.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dataplatform.clock import FrozenClock
from dataplatform.identity import (
    Exchange,
    IdentityMaster,
    LineageResolver,
    ListingStatus,
    Security,
    SymbolWindow,
)
from dataplatform.ingest.nse.corp_actions import parse

_IST = ZoneInfo("Asia/Kolkata")
_CLOCK = FrozenClock(datetime(2026, 9, 6, 9, 0, tzinfo=_IST))

_RETIRED = "INE335Y01012"  # IRCTC before the 2021 face-value split
_SURVIVOR = "INE335Y01020"  # …and after it
_EX = date(2021, 10, 28)  # NSE dates the split to the predecessor's last session
_EFFECTIVE = date(2021, 10, 29)  # the survivor's first session


def _master(*isins: str) -> IdentityMaster:
    """A master that knows exactly `isins`, all trading as IRCTC on NSE."""
    return IdentityMaster(
        [
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="IRCTC",
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
                name="IRCTC",
                primary_exchange=Exchange.NSE,
                status=ListingStatus.ACTIVE,
                first_seen_date=date(2000, 1, 1),
            )
            for isin in isins
        ],
        listings=[],
    )


def _payload(isin: str) -> bytes:
    return json.dumps(
        [
            {
                "symbol": "IRCTC",
                "isin": isin,
                "exDate": "28-Oct-2021",
                "subject": "Face Value Split (Sub-Division) - From Rs 10/- To Rs 2/-",
                "comp": "Indian Railway Catering And Tourism Corporation Limited",
                "series": "EQ",
                "faceVal": 2,
                "recDate": "-",
                "bcStartDate": "-",
                "bcEndDate": "-",
                "ndStartDate": "-",
                "ndEndDate": "-",
                "caBroadcastDate": None,
                "ind": "-",
            }
        ]
    ).encode()


def _lineage() -> LineageResolver:
    return LineageResolver({_RETIRED: (_SURVIVOR, _EFFECTIVE)})


def test_without_a_lineage_the_split_is_still_held_back() -> None:
    """The old behaviour, kept honest: an unknown ISIN is unresolved, never guessed at."""
    result = parse(_payload(_RETIRED), filename="ca.json", master=_master(_SURVIVOR), clock=_CLOCK)
    assert result.actions == ()
    assert len(result.unresolved) == 1


def test_a_split_filed_against_the_retired_isin_lands_on_the_survivor() -> None:
    result = parse(
        _payload(_RETIRED),
        filename="ca.json",
        master=_master(_SURVIVOR),
        clock=_CLOCK,
        lineage=_lineage(),
    )
    assert len(result.actions) == 1
    action = result.actions[0]
    assert action.isin == _SURVIVOR
    assert action.filed_against_isin == _RETIRED
    assert action.ex_date == _EX


def test_a_live_isin_is_never_rewritten_through_the_lineage() -> None:
    """The fallback is only ever reached by an ISIN the master does not know."""
    result = parse(
        _payload(_SURVIVOR),
        filename="ca.json",
        master=_master(_SURVIVOR),
        clock=_CLOCK,
        lineage=_lineage(),
    )
    assert result.actions[0].isin == _SURVIVOR
    assert result.actions[0].filed_against_isin is None


def test_a_survivor_the_master_does_not_know_is_still_held_back() -> None:
    """A lineage edge is not licence to invent a security."""
    result = parse(
        _payload(_RETIRED),
        filename="ca.json",
        master=_master("INE999Z01011"),
        clock=_CLOCK,
        lineage=_lineage(),
    )
    assert result.actions == ()
    assert len(result.unresolved) == 1


def test_an_isin_with_no_lineage_edge_is_held_back() -> None:
    result = parse(
        _payload("INE000X01011"),
        filename="ca.json",
        master=_master(_SURVIVOR),
        clock=_CLOCK,
        lineage=_lineage(),
    )
    assert result.actions == ()
    assert len(result.unresolved) == 1
