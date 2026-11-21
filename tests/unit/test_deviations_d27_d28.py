"""D27 retired, D28 as registry entries: each accepts its own known set and nothing next to it.

D27 froze `ca_reconciliation` ERROR flags by fingerprint (31 from 2026-10-24; code fixes, the
owner-approved consistent-dividend rules and the BEML ruling resolved all but one by 2026-11-01).
The last, CORALFINAC's, was ruled on 2026-11-16 (D42) and its flag closed at the ca_refresh of
2026-11-21, so the entry was deleted: no fingerprint is accepted any more, and a regression that
raised any of them again is red, its check and alert with it. D28 accepts two sources' 404/
empty-body failures only while the row is retryable and less than 7 days past its first attempt.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Final, cast

import pytest

from dataplatform.clock import IST
from dataplatform.status.console import data_render
from dataplatform.status.deviations import (
    DeviationJudge,
    DeviationKind,
    DeviationOut,
    DeviationStatus,
    GapItem,
    OpenFlag,
    RetryWindow,
    load_deviations,
)
from dataplatform.status.models import CheckCountOut, QualityFlagOut, QualityOut, QualitySeverity
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection

REGISTRY: Final = load_deviations()
#: What D27 targeted: no entry may target either any more.
CA_TARGETS: Final = ("ca_reconciliation", "quality:ca_reconciliation:red")
TODAY: Final = date(2026, 10, 24)
#: CORALFINAC's, D27's last; its flag closed at the 2026-11-21 ca_refresh (D42).
KNOWN: Final = "69102f228404b6f9cbe237d4bf3d38b1"  # RATIO_MISMATCH INE558D01021 2017-09-06
#: Resolved by the "<n> Per Share" parser fix (2026-11-01) and so no longer accepted: a regression
#: that raised it again must be red, not silently waved through.
RESOLVED: Final = "fa424e5f2c8c4c801aa4d309c8137b12"  # RATIO_MISMATCH INE109C01017 2016-09-12
#: Resolved by rule A, silence yields (2026-11-01): likewise no longer accepted.
RESOLVED_BY_RULE: Final = "4061d5bb710b19c7188af37ad28fdcfb"  # RATIO_MISMATCH INE121E01018
#: Resolved by the owner's BEML ruling (`superseded_dividends`, 2026-11-01): no longer accepted.
RESOLVED_BY_RULING: Final = "8c8fac0ad26f33b777fc94552e9cabb0"  # RATIO_MISMATCH INE258A01024
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
            (i, fp, "INE558D01021", date(2017, 9, 6), RAISED, "RATIO_MISMATCH")
            for i, fp in enumerate(fingerprints)
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
        id=1, fingerprint=fingerprint, isin="INE558D01021", logical_date=date(2017, 9, 6),
        raised_at=RAISED,
    )  # fmt: skip


# ── D27 (retired 2026-11-21) ──────────────────────────────────────────────────────────────


def test_d27_is_gone_from_the_registry() -> None:
    ids = {entry.id for entry in REGISTRY.deviations}
    assert not {i for i in ids if i.startswith("d27-")}
    assert not [e for e in REGISTRY.deviations if e.target in CA_TARGETS], "nothing replaced it"


@pytest.mark.parametrize("fingerprint", [KNOWN, RESOLVED, RESOLVED_BY_RULE, RESOLVED_BY_RULING])
@pytest.mark.parametrize("today", [TODAY, date(2026, 11, 21), date(2026, 11, 22)])
def test_no_former_d27_fingerprint_is_accepted(fingerprint: str, today: date) -> None:
    judge = _judge(today=today)
    assert judge.quality_flag("ca_reconciliation", _flag(fingerprint)) is None


def test_a_reraised_coralfinac_flag_keeps_the_check_and_its_alert_red() -> None:
    conn = _Flags(KNOWN)
    judge = _judge(conn)
    assert judge.quality("ca_reconciliation") is None
    assert judge.quality_accepted("ca_reconciliation") == 0
    assert judge.alert("quality:ca_reconciliation:red") is None  # the alert stays critical
    assert conn.queries == 0, "no entry targets the check, so the judge reads nothing"


def test_a_check_no_entry_targets_reads_nothing() -> None:
    conn = _Flags(KNOWN)
    # Not l2_continuity: since D33 an entry targets it.
    assert _judge(conn).quality_accepted("shareholding_no_isin") == 0 and conn.queries == 0


def test_the_quality_panel_counts_and_marks_accepted_flags_apart() -> None:
    verdict = DeviationOut(
        id="test-quality-entry", kind=DeviationKind.QUALITY, target="ca_reconciliation",
        status=DeviationStatus.ACCEPTED, remark="a known flag", basis="test",
        since=date(2026, 9, 7), review_by=None,
    )  # fmt: skip
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
