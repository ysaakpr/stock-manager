"""BSE's TDCLOINDI ex-marker scored against stored corporate actions — report only (l1-widen)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from dataplatform.clock import FrozenClock
from dataplatform.ingest.bse import corp_actions as bse_ca
from dataplatform.ingest.bse.bhavcopy import BseLegacyQuote, parse_legacy
from dataplatform.quality.ex_marker_witness import (
    MAX_SUSPENSION_DAYS,
    ActionRecord,
    MarkerRecord,
    compare,
)

D = date(2020, 3, 10)


def test_each_marker_outcome_is_classified() -> None:
    markers = [
        MarkerRecord("INE000A01011", D, "XD"),  # same-day dividend → exact
        MarkerRecord("INE000B01011", D, "XB"),  # bonus two days later → near
        MarkerRecord("INE000C01011", D, "SS"),  # only a dividend that day → type_mismatch
        MarkerRecord("INE000D01011", D, "XR"),  # nothing → unmatched
        MarkerRecord("INE000E01011", D, "CS"),  # consolidation filed as SPLIT → exact
    ]
    actions = [
        ActionRecord("INE000A01011", D, "DIVIDEND"),
        ActionRecord("INE000B01011", date(2020, 3, 12), "BONUS"),
        ActionRecord("INE000C01011", D, "DIVIDEND"),
        ActionRecord("INE000E01011", D, "SPLIT"),
    ]
    report = compare(markers, actions)
    assert report.marker_to_action["XD"]["exact"] == 1
    assert report.marker_to_action["XB"]["near"] == 1
    assert report.marker_to_action["SS"]["type_mismatch"] == 1
    assert report.marker_to_action["XR"]["unmatched"] == 1
    assert report.marker_to_action["CS"]["exact"] == 1
    assert report.agreement() == 2 / 5


def test_an_action_filed_against_a_predecessor_isin_still_matches() -> None:
    markers = [MarkerRecord("INE111A01022", D, "SS")]
    actions = [ActionRecord("INE111A01014", D, "SPLIT", filed_against_isin="INE111A01022")]
    assert compare(markers, actions).marker_to_action["SS"]["exact"] == 1


def test_the_reverse_direction_counts_only_actions_with_a_bse_row_that_day() -> None:
    markers = [MarkerRecord("INE000A01011", D, "XD")]
    actions = [
        ActionRecord("INE000A01011", D, "DIVIDEND"),  # traded, marked → witnessed
        ActionRecord("INE000F01011", D, "DIVIDEND"),  # traded, unmarked → no_marker
        ActionRecord("INE000G01011", D, "DIVIDEND"),  # not traded on BSE → excluded
        ActionRecord("INE000F01011", D, "BUYBACK"),  # no marker kind for it → ignored
    ]
    traded = {("INE000A01011", D), ("INE000F01011", D)}
    reverse = compare(markers, actions, traded=traded).action_to_marker["DIVIDEND"]
    assert reverse["witnessed"] == 1
    assert reverse["no_marker"] == 1
    assert reverse["no_bse_row_that_day"] == 1


def test_the_witness_never_changes_the_actions_it_is_given() -> None:
    actions = [ActionRecord("INE000A01011", D, "DIVIDEND")]
    before = list(actions)
    compare([MarkerRecord("INE000A01011", D, "SS")], actions)
    assert actions == before


# ── a consolidation is marked where the new basis first trades, not on the stored ex-date ─────

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ONTIC, ONTIC_SCRIP = "INE989S01042", "540386"
#: Real L0 bytes (provenance in each fixture directory's notes): BSE's per-scrip corporate-action
#: payload for ONTIC, and the legacy bhavcopy of its last old-basis session (2017-04-11) and of
#: the session its consolidated shares first traded (2017-05-25, the `CS` row).
_ONTIC_CA = FIXTURES / "corp_actions" / "bse" / "2026-09-07" / "defaultdata_540386.json"
_LAST_OLD = FIXTURES / "bse_bhavcopy" / "legacy" / "EQ110417_CSV.ZIP"
_FIRST_NEW = FIXTURES / "bse_bhavcopy" / "legacy" / "EQ250517_CSV.ZIP"


def _ontic_rows() -> list[BseLegacyQuote]:
    rows: list[BseLegacyQuote] = []
    for path, day in ((_LAST_OLD, date(2017, 4, 11)), (_FIRST_NEW, date(2017, 5, 25))):
        quotes = parse_legacy(path.read_bytes(), filename=path.name, trade_date=day)
        rows.extend(q for q in quotes if q.scrip_code == ONTIC_SCRIP)
    return rows


def _ontic_consolidation() -> list[ActionRecord]:
    parsed = bse_ca.parse(
        _ONTIC_CA.read_bytes(),
        filename=_ONTIC_CA.name,
        scrip_index={ONTIC_SCRIP: ONTIC},
        clock=FrozenClock(date(2026, 9, 7)),
    )
    return [
        ActionRecord(a.isin, a.ex_date, a.action_type.value, a.filed_against_isin)
        for a in parsed.actions
    ]


def test_the_frozen_sources_show_the_two_dates_of_one_consolidation() -> None:
    """The feed dates it 2017-04-13; the scrip is suspended; the CS row is 2017-05-25."""
    [action] = [a for a in _ontic_consolidation() if a.ex_date.year == 2017]
    assert (action.ex_date, action.action_type) == (date(2017, 4, 13), "SPLIT")
    rows = {r.trade_date: r for r in _ontic_rows()}
    assert sorted(rows) == [date(2017, 4, 11), date(2017, 5, 25)]
    assert rows[date(2017, 4, 11)].close_indicator == ""
    first_new = rows[date(2017, 5, 25)]
    assert first_new.close_indicator == "CS"
    # BSE's own prev-close is the old basis' 1.79: no session printed between the two
    assert first_new.prev_close == rows[date(2017, 4, 11)].close == Decimal("1.79")


def test_a_cs_marker_matches_the_consolidation_on_the_scrips_first_session() -> None:
    rows = _ontic_rows()
    markers = [
        MarkerRecord(ONTIC, r.trade_date, r.close_indicator) for r in rows if r.close_indicator
    ]
    actions = _ontic_consolidation()
    # Same-day comparison — what scored every CS marker of 2016-09..2024-07 unmatched.
    assert compare(markers, actions).marker_to_action["CS"]["unmatched"] == 1
    report = compare(markers, actions, sessions={ONTIC: [r.trade_date for r in rows]})
    assert report.marker_to_action["CS"] == {"first_session": 1}
    assert report.matched("CS") == 1.0 and report.agreement("CS") == 0.0
    # and the reverse direction finds the marker on that first session
    assert report.action_to_marker["SPLIT"]["witnessed"] == 1


def test_a_marker_after_a_session_the_scrip_did_trade_is_not_the_ex_dates_own() -> None:
    """Had the scrip traded on or after the ex-date, a later marker belongs to something else."""
    ex = date(2020, 3, 2)
    marker = [MarkerRecord("INE000A01011", date(2020, 3, 9), "CS")]
    actions = [ActionRecord("INE000A01011", ex, "SPLIT")]
    traded_after_ex = {"INE000A01011": [date(2020, 2, 28), date(2020, 3, 3), date(2020, 3, 9)]}
    suspended = {"INE000A01011": [date(2020, 2, 28), date(2020, 3, 9)]}
    assert compare(marker, actions, sessions=traded_after_ex).marker_to_action["CS"] == {
        "unmatched": 1
    }
    assert compare(marker, actions, sessions=suspended).marker_to_action["CS"] == {
        "first_session": 1
    }


def test_a_first_session_further_than_the_suspension_cap_does_not_match() -> None:
    ex = date(2020, 1, 1)
    late = ex + timedelta(days=MAX_SUSPENSION_DAYS + 1)
    marker = [MarkerRecord("INE000A01011", late, "CS")]
    actions = [ActionRecord("INE000A01011", ex, "SPLIT")]
    sessions = {"INE000A01011": [date(2019, 12, 31), late]}
    assert compare(marker, actions, sessions=sessions).marker_to_action["CS"] == {"unmatched": 1}


def test_the_first_session_never_matches_backwards() -> None:
    """A marker before the stored ex-date is never that action's session — the gap runs forward."""
    marker = [MarkerRecord("INE000A01011", date(2020, 3, 2), "CS")]
    actions = [ActionRecord("INE000A01011", date(2020, 3, 9), "SPLIT")]
    sessions = {"INE000A01011": [date(2020, 3, 2), date(2020, 3, 10)]}
    assert compare(marker, actions, sessions=sessions).marker_to_action["CS"] == {"unmatched": 1}
