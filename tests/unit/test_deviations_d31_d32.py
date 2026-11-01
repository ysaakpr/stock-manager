"""D31, D32 and D28's truncated-body shape: each accepts its own evidence and nothing next to it.

D31 accepts an `nse_xbrl_filing` 404 that carries the index's zero-byte statement, on any date and
permanently; a plain 404, or one whose index states another size, is not it. D32 accepts a plain
404 for a filing dated 2025-07-03 or 2025-07-04 only. D28 now also covers a body cut off before
its document closes — while it is retried, never a body malformed some other way. The messages are
the stored texts the runner writes (`fundamentals_backfill`), several verbatim from the live rows.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Final, cast

import pytest

from dataplatform.clock import IST
from dataplatform.ingest.fundamentals_backfill import zero_byte_evidence
from dataplatform.ingest.xbrl.discovery import FilingIndexEntry
from dataplatform.ingest.xbrl.models import Nature
from dataplatform.status.deviations import DeviationJudge, GapItem, load_deviations
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection

REGISTRY: Final = load_deviations()
TODAY: Final = date(2026, 11, 1)
URL: Final = (
    "https://nsearchives.nseindia.com/corporate/xbrl/"
    "INTEGRATED_FILING_INDAS_1658433_24042026042507_WEB.xml"
)
PLAIN_404: Final = (
    f"FetchHTTPError: nse_xbrl_filing: {URL} returned 404, which no retry would change"
)
LONG_AGO: Final = datetime(2026, 9, 6, 22, 23, tzinfo=IST)


def _entry(size: str | None) -> FilingIndexEntry:
    return FilingIndexEntry(
        isin="INE364U01010", symbol="ADANIGREEN", name="Adani Green Energy",
        period_end=date(2026, 3, 31), filing_date=date(2026, 4, 24), nature=Nature.STANDALONE,
        xbrl_url=URL, seq_number="IF152668", xbrl_file_size=size,
    )  # fmt: skip


#: Exactly what the runner records for ADANIGREEN IF152668 now.
ZERO_404: Final = PLAIN_404 + zero_byte_evidence(_entry("0 Bytes"))


def _judge(today: date = TODAY) -> DeviationJudge:
    return DeviationJudge(
        REGISTRY, cast(Connection, object()), cast(SyncStateStore, object()), today
    )


def _gap(
    error: str,
    *,
    on: date = date(2026, 4, 24),
    first: datetime = LONG_AGO,
    retryable: bool = True,
    reason: str = "FAILED",
) -> GapItem:
    return GapItem(
        source="nse_xbrl_filing", logical_date=on, reason=reason, last_error=error,
        retryable=retryable, first_attempt_at=first,
    )  # fmt: skip


def _id(item: GapItem, today: date = TODAY) -> str | None:
    verdict = _judge(today).gap(item)
    return verdict.id if verdict is not None and verdict.accepted else None


# ── D31 ────────────────────────────────────────────────────────────────────────────────────


def test_the_runner_writes_the_statement_d31_matches() -> None:
    assert ZERO_404.endswith("; NSE's index lists this XBRL file as '0 Bytes'")
    assert _entry("0 Bytes").xbrl_stated_empty
    assert not _entry("16.93 KB").xbrl_stated_empty and not _entry(None).xbrl_stated_empty


def test_a_404_carrying_the_zero_byte_statement_is_accepted_permanently() -> None:
    assert _id(_gap(ZERO_404)) == "d31-xbrl-zero-byte-index"
    years_later = date(2030, 1, 1)
    assert _id(_gap(ZERO_404), years_later) == "d31-xbrl-zero-byte-index"
    assert _id(_gap(ZERO_404, retryable=False)) == "d31-xbrl-zero-byte-index"
    verdict = _judge().gap(_gap(ZERO_404))
    assert verdict is not None and "0 Bytes" in verdict.remark and "permanently" in verdict.remark


def test_a_zero_byte_filing_inside_its_retry_window_shows_d31_not_d28() -> None:
    fresh = datetime(2026, 10, 29, 2, 2, tzinfo=IST)
    assert _id(_gap(ZERO_404, on=date(2026, 10, 28), first=fresh)) == "d31-xbrl-zero-byte-index"


@pytest.mark.parametrize(
    "error",
    [
        PLAIN_404,  # no recorded evidence: an ordinary missing document stays red
        PLAIN_404 + zero_byte_evidence(_entry("16.93 KB")),
        PLAIN_404 + "; NSE's index lists this XBRL file as '0 Bytes' and more",
        ZERO_404.replace("returned 404", "returned 410"),
        "parse failed: X.xml: empty response body; NSE's index lists this XBRL file as '0 Bytes'",
    ],
)
def test_anything_short_of_the_evidence_is_not_d31(error: str) -> None:
    assert _id(_gap(error)) is None


def test_d31_covers_the_failed_reason_only() -> None:
    assert _id(_gap(ZERO_404, reason="L0_PRESENT_L1_ABSENT")) is None


# ── D32 ────────────────────────────────────────────────────────────────────────────────────

JULY_404: Final = (
    "FetchHTTPError: nse_xbrl_filing: https://nsearchives.nseindia.com/corporate/xbrl/"
    "INTEGRATED_FILING_INDAS_1477033_03072025073621_WEB.xml returned 404, which no retry would "
    "change"
)


@pytest.mark.parametrize(
    ("on", "accepted"),
    [
        (date(2025, 7, 2), False),
        (date(2025, 7, 3), True),
        (date(2025, 7, 4), True),
        (date(2025, 7, 5), False),
        (date(2026, 7, 3), False),
    ],
)
def test_d32_is_the_two_outage_days_only(on: date, accepted: bool) -> None:
    assert (_id(_gap(JULY_404, on=on)) == "d32-xbrl-july-2025-outage") is accepted
    if not accepted:
        assert _id(_gap(JULY_404, on=on)) is None


def test_another_failure_on_the_outage_days_is_not_d32() -> None:
    parse = "parse failed: X.xml: no results column covers 2025-04-01→2025-06-30"
    assert _id(_gap(parse, on=date(2025, 7, 3))) is None
    assert _id(_gap(JULY_404.replace("404", "500"), on=date(2025, 7, 4))) is None


# ── D28's truncated-body shape ─────────────────────────────────────────────────────────────

#: Verbatim from the live rows (SUDARCOLOR IF205837, PARADEEP IF206642, LAURUSLABS IF121347).
TRUNCATED: Final = (
    "parse failed: INTEGRATED_FILING_INDAS_1748197_28102026075000_WEB.xml: body is not "
    "well-formed XML: unclosed token: line 1, column 131038",
    "parse failed: INTEGRATED_FILING_INDAS_1749228_29102026065735_WEB.xml: body is not "
    "well-formed XML: no element found: line 1, column 32768",
    "parse failed: INTEGRATED_FILING_INDAS_1555947_23102025113639_WEB.xml: body is not "
    "well-formed XML: unclosed token: line 1, column 65227",
)


@pytest.mark.parametrize("error", TRUNCATED)
def test_a_truncated_body_is_amber_while_retried_then_red(error: str) -> None:
    first = datetime(2026, 10, 29, 2, 6, tzinfo=IST)
    for days, accepted in ((0, True), (6, True), (7, False), (30, False)):
        today = first.date() + timedelta(days=days)
        found = _id(_gap(error, on=date(2026, 10, 28), first=first), today)
        assert (found == "d28-xbrl-filing-retrying") is accepted, days
    assert _id(_gap(error, on=date(2026, 10, 28), first=first, retryable=False)) is None


@pytest.mark.parametrize(
    "error",
    [
        "parse failed: X.xml: body is not well-formed XML: mismatched tag: line 4, column 2",
        "parse failed: X.xml: body is not well-formed XML: not well-formed (invalid token): "
        "line 1, column 0",
        "parse failed: X.xml: body is not well-formed XML: unclosed token: line 1, column 9; more",
        "parse failed: X.xml: root element is 'html', not an XBRL <xbrl> instance",
    ],
)
def test_d28_does_not_widen_to_other_malformed_documents(error: str) -> None:
    first = datetime(2026, 10, 30, 2, 1, tzinfo=IST)
    assert _id(_gap(error, on=date(2026, 10, 29), first=first)) is None
