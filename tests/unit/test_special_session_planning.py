"""Every fetch planner asks for the declared weekend sessions, and none for an ordinary weekend.

The 2026-10-05 data-quality audit found six weekend sessions since 2016 missing from every price
source, and the cause was one line deep: each planner iterates `expected_data_dates`, and the
calendar called those dates WEEKEND. The calendar now declares them (`DayKind.SPECIAL`); this file
pins that the declaration reaches each planner that decides what to request — the M1.9 backfill
for each of its source sets, the W1 legacy and W2 PR-bundle sweeps — and the offline price rebuild,
so a later planner that switches to `expected_sessions` fails here rather than in an audit.

Offline: plans are pure date arithmetic over the checked-in calendar and register (B8).
"""

from __future__ import annotations

from datetime import date

import pytest

from dataplatform.ingest import backfill, legacy_backfill, pr_bundle_campaign, price_rebuild
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import NotAGapError, expected_gap_kind

#: (special session, the ordinary weekend day beside it). Legacy-era and UDiFF-era both.
LEGACY_ERA = (date(2020, 2, 1), date(2020, 2, 2))
DR_DRILL = (date(2024, 5, 18), date(2024, 5, 19))
UDIFF_ERA = (date(2025, 2, 1), date(2025, 2, 2))
SUNDAY_BUDGET = (date(2026, 2, 1), date(2026, 1, 31))


def _dates(plan: list[backfill.FetchRequest]) -> set[date]:
    return {request.trade_date for request in plan}


@pytest.mark.parametrize(
    ("source", "pair"),
    [
        (backfill.NSE_BHAVCOPY, LEGACY_ERA),
        (backfill.NSE_BHAVCOPY, DR_DRILL),
        (backfill.NSE_BHAVCOPY, UDIFF_ERA),
        (backfill.NSE_BHAVCOPY, SUNDAY_BUDGET),
        (backfill.NSE_DELIVERY, LEGACY_ERA),
        (backfill.NSE_DELIVERY, UDIFF_ERA),
        (backfill.BSE_BHAVCOPY_LEGACY, LEGACY_ERA),
        (backfill.BSE_BHAVCOPY_LEGACY, DR_DRILL),
        (backfill.BSE_BHAVCOPY, UDIFF_ERA),
        (backfill.BSE_BHAVCOPY, SUNDAY_BUDGET),
    ],
    ids=str,
)
def test_the_backfill_requests_the_special_session(source: str, pair: tuple[date, date]) -> None:
    special, ordinary = pair
    plan = backfill.build_plan(
        backfill.SOURCE_SETS[source],
        min(pair),
        max(pair),
        calendar=trading_calendar(),
        register=load_register(),
    )
    assert _dates(plan) == {special}
    assert ordinary not in _dates(plan)


def test_the_backfill_names_the_special_sessions_file() -> None:
    """The request is for that day's own file, not the neighbouring session's."""
    plan = backfill.build_plan(
        backfill.SOURCE_SETS[backfill.NSE_BHAVCOPY],
        date(2020, 2, 1),
        date(2020, 2, 1),
        calendar=trading_calendar(),
        register=load_register(),
    )
    assert [request.filename for request in plan] == ["cm01FEB2020bhav.csv.zip"]


@pytest.mark.parametrize("pair", [LEGACY_ERA, DR_DRILL, UDIFF_ERA, SUNDAY_BUDGET], ids=str)
def test_the_pr_bundle_sweep_requests_the_special_session(pair: tuple[date, date]) -> None:
    special, ordinary = pair
    plan = pr_bundle_campaign.plan_sessions(min(pair), max(pair), calendar=trading_calendar())
    assert plan.basis == "calendar"
    assert special in plan.dates
    assert ordinary not in plan.dates


def test_the_pr_bundle_sweep_requests_the_pre_2016_saturdays() -> None:
    """Eight of the ten pre-2016 weekend sessions had no bundle in L0: the plan never named them."""
    plan = pr_bundle_campaign.plan_sessions(
        date(2010, 1, 4), date(2015, 12, 31), calendar=trading_calendar()
    )
    for day in (date(2010, 2, 6), date(2012, 1, 7), date(2014, 3, 22), date(2015, 2, 28)):
        assert day in plan.dates, day
    assert date(2015, 3, 1) not in plan.dates


def test_the_legacy_sweep_requests_the_special_session() -> None:
    plan = legacy_backfill.plan_sessions(
        date(2006, 6, 23), date(2006, 6, 26), calendar=trading_calendar()
    )
    assert plan.basis == "calendar"
    assert list(plan.dates) == [date(2006, 6, 23), date(2006, 6, 25), date(2006, 6, 26)]


def test_the_price_rebuild_covers_the_special_session() -> None:
    """The offline rebuild works from the dates the ingest fetched, weekend sessions included."""
    plan = price_rebuild.plan_sessions(date(2024, 5, 17), date(2024, 5, 20))
    assert date(2024, 5, 18) in plan
    assert date(2024, 5, 19) not in plan


def test_a_missing_special_session_cannot_be_filed_as_a_gap() -> None:
    """`sync_state` refuses GAP for it: an absent file on a day the exchange traded is FAILED."""
    with pytest.raises(NotAGapError, match="SPECIAL"):
        expected_gap_kind(date(2024, 3, 2), calendar=trading_calendar())
    assert expected_gap_kind(date(2024, 3, 3), calendar=trading_calendar()).value == "WEEKEND"
