"""D27 and D28 as registry entries: each accepts its own known set and nothing next to it.

D27 freezes 31 `ca_reconciliation` ERROR flags by fingerprint: a flag in the set is ACCEPTED on
its own, a new disagreement never is, and the check (and its alert) turns amber only once every
open ERROR flag is in the set. D28 accepts two sources' 404/empty-body failures only while the row
is retryable and less than 7 days past its first attempt.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Final, cast

import pytest

from dataplatform.clock import IST
from dataplatform.status.console import data_render
from dataplatform.status.deviations import (
    DeviationJudge,
    DeviationStatus,
    GapItem,
    OpenFlag,
    QualityEntry,
    RetryWindow,
    load_deviations,
)
from dataplatform.status.models import CheckCountOut, QualityFlagOut, QualityOut, QualitySeverity
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection

REGISTRY: Final = load_deviations()
D27: Final = REGISTRY.get("d27-ca-reconciliation-backlog")
TODAY: Final = date(2026, 10, 24)
KNOWN: Final = "fa424e5f2c8c4c801aa4d309c8137b12"  # RATIO_MISMATCH INE109C01017 2016-09-12
RAISED: Final = datetime(2026, 9, 7, 14, 24, tzinfo=IST)


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _Flags:
    """`quality_flag`'s open ERROR rows of one check, as the judge reads them."""

    def __init__(self, *fingerprints: str | None) -> None:
        self.rows = [
            (i, fp, "INE109C01017", date(2016, 9, 12), RAISED) for i, fp in enumerate(fingerprints)
        ]
        self.queries = 0

    def execute(self, sql: str, params: object = ()) -> _Rows:
        self.queries += 1
        return _Rows(self.rows if "FROM quality_flag" in sql else [])


def _judge(conn: object | None = None, today: date = TODAY) -> DeviationJudge:
    return DeviationJudge(
        REGISTRY,
        cast(Connection, conn or _Flags()),
        cast(SyncStateStore, object()),
        today,
    )


def _flag(fingerprint: str | None) -> OpenFlag:
    return OpenFlag(
        id=1, fingerprint=fingerprint, isin="INE109C01017", logical_date=date(2016, 9, 12),
        raised_at=RAISED,
    )  # fmt: skip


# ── D27 ────────────────────────────────────────────────────────────────────────────────────


def test_d27_is_a_frozen_fingerprint_set_and_nothing_else() -> None:
    assert isinstance(D27, QualityEntry)
    match = D27.match
    assert match.fingerprints is not None
    assert len(match.fingerprints) == 31 and KNOWN in match.fingerprints
    assert match.ids is None and match.isins is None
    assert match.raised_before is None and match.dated_before is None
    assert D27.review_by == date(2027, 1, 31)


def test_a_flag_in_the_set_is_accepted_and_a_new_one_is_not() -> None:
    judge = _judge()
    accepted = judge.quality_flag("ca_reconciliation", _flag(KNOWN))
    assert accepted is not None and accepted.status is DeviationStatus.ACCEPTED
    assert "M2.3" in accepted.remark
    assert judge.quality_flag("ca_reconciliation", _flag("0" * 32)) is None
    assert judge.quality_flag("ca_reconciliation", _flag(None)) is None
    assert judge.quality_flag("l2_continuity", _flag(KNOWN)) is None


def test_the_check_is_amber_only_when_every_open_flag_is_in_the_set() -> None:
    mixed = _Flags(KNOWN, "0" * 32)
    judge = _judge(mixed)
    assert judge.quality("ca_reconciliation") is None
    assert judge.quality_accepted("ca_reconciliation") == 1
    assert judge.alert("quality:ca_reconciliation:red") is None  # the alert stays critical
    only_known = _judge(_Flags(KNOWN))
    assert only_known.quality("ca_reconciliation") is not None
    assert only_known.alert("quality:ca_reconciliation:red") is not None


def test_after_its_review_date_d27_accepts_nothing() -> None:
    verdict = _judge(today=date(2027, 2, 1)).quality_flag("ca_reconciliation", _flag(KNOWN))
    assert verdict is not None and verdict.status is DeviationStatus.EXPIRED
    on_the_day = _judge(today=date(2027, 1, 31)).quality_flag("ca_reconciliation", _flag(KNOWN))
    assert on_the_day is not None and on_the_day.accepted


def test_a_check_no_entry_targets_reads_nothing() -> None:
    conn = _Flags(KNOWN)
    assert _judge(conn).quality_accepted("l2_continuity") == 0 and conn.queries == 0


def test_the_quality_panel_counts_and_marks_accepted_flags_apart() -> None:
    verdict = _judge().quality_flag("ca_reconciliation", _flag(KNOWN))
    check = CheckCountOut(
        check_name="ca_reconciliation", severity=QualitySeverity.ERROR, count=194,
        first_date=date(2001, 7, 23), last_date=date(2026, 10, 1), raised_today=0,
        raised_last_7_days=2, accepted=31,
    )  # fmt: skip

    def flag(fid: int, deviation: Any) -> QualityFlagOut:
        return QualityFlagOut(
            id=fid, date=date(2016, 9, 12), check_name="ca_reconciliation",
            severity=QualitySeverity.ERROR, isin="INE109C01017", raised_at=RAISED,
            deviation=deviation,
        )  # fmt: skip

    html = data_render.render_quality(
        QualityOut(
            as_of=RAISED,
            open_total=194,
            counts=[],
            by_check=[check],
            flags=[flag(1, verdict), flag(2, None)],
            limit=50,
        )
    )
    assert '194 <span class="muted">· 31 accepted</span>' in html
    assert '<tr data-flag="1" data-severity="ERROR" data-deviation="ACCEPTED"' in html
    assert '<tr data-flag="2" data-severity="ERROR">' in html


# ── D28 ────────────────────────────────────────────────────────────────────────────────────

XBRL_404: Final = (
    "FetchHTTPError: nse_xbrl_filing: https://nsearchives.nseindia.com/corporate/xbrl/"
    "INTEGRATED_FILING_INDAS_1567157_22102026010412_WEB.xml returned 404, which no retry would "
    "change"
)
XBRL_EMPTY: Final = (
    "parse failed: INTEGRATED_FILING_INDAS_1566848_20102026_WEB.xml: empty response body"
)
GDELT_404: Final = (
    "FetchHTTPError: gdelt_v2_event_files: http://data.gdeltproject.org/gdeltv2/"
    "20261020064500.export.CSV.zip returned 404, which no retry would change"
)


def _gap(source: str, error: str, *, first: datetime, retryable: bool = True) -> GapItem:
    return GapItem(
        source=source, logical_date=first.date(), reason="FAILED", last_error=error,
        retryable=retryable, first_attempt_at=first,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("nse_xbrl_filing", XBRL_404),
        ("nse_xbrl_filing", XBRL_EMPTY),
        ("gdelt_v2_event_files", GDELT_404),
    ],
)
def test_a_retrying_failure_is_amber_for_seven_days_then_red(source: str, error: str) -> None:
    first = datetime(2026, 10, 20, 12, 15, tzinfo=IST)
    judge = _judge()
    for days, accepted in ((0, True), (6, True), (7, False), (30, False)):
        today = first.date() + timedelta(days=days)
        verdict = _judge(today=today).gap(_gap(source, error, first=first))
        assert (verdict is not None and verdict.accepted) is accepted, (source, days)
    assert judge.gap(_gap(source, error, first=first, retryable=False)) is None


def test_only_the_two_sources_and_their_error_shapes() -> None:
    first = datetime(2026, 10, 22, 2, 1, tzinfo=IST)
    judge = _judge()
    parse = "parse failed: INTEGRATED_FILING_INDAS_1_2_WEB.xml: no results column contexts"
    assert judge.gap(_gap("nse_xbrl_filing", parse, first=first)) is None
    assert judge.gap(_gap("nse_announcements", XBRL_404, first=first)) is None
    assert judge.gap(_gap("gdelt_v2_event_files", XBRL_404, first=first)) is None
    assert judge.gap(_gap("nse_xbrl_filing", GDELT_404, first=first)) is None


def test_a_row_with_no_first_attempt_is_in_no_window() -> None:
    assert not RetryWindow(days=7).holds(True, None, TODAY)
    assert not RetryWindow(days=7).holds(None, datetime(2026, 10, 23, tzinfo=IST), TODAY)
