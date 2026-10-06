"""BSE's TDCLOINDI ex-marker scored against stored corporate actions — report only (l1-widen)."""

from __future__ import annotations

from datetime import date

from dataplatform.quality.ex_marker_witness import ActionRecord, MarkerRecord, compare

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
