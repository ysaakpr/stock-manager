"""D29: the api.bseindia.com sources are paused — what that changes, and the gates it never touches.

The register holds the pause (`paused`: reason, since, HUMAN_DECISIONS D29) on exactly the three
api.bseindia.com sources; `bse_bhavcopy_udiff` (www.bseindia.com) keeps running. Each job leg's own
skip is tested in its module (`test_ca_refresh`, `test_daily_capture`, `test_daily_snapshot`); this
module holds what crosses them:

* the status surface shows PAUSED, grey, with the reason — and `healthy` keeps its strict value;
* the gap report explains a paused source's days from `since` as PAUSED, like the era rule, and
  leaves every earlier day (and every row) as it was;
* identity_refresh holds a conflict on the paused exchange instead of failing on it;
* **the gate boundary**: no gate counts a paused source as fresh, published or set aside;
* D26's entries still cover the FAILED rows from before the pause.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, cast

from dataplatform.clock import IST
from dataplatform.identity.ingest import IdentityIngestReport
from dataplatform.identity.master import (
    ConflictKind,
    DetectedBy,
    Exchange,
    IdentityConflict,
    WriteCounts,
)
from dataplatform.ingest.calendar import DayKind, trading_calendar
from dataplatform.ingest.identity_refresh import IdentityRefreshReport, refresh_failure
from dataplatform.ingest.source_register import load, paused_sources
from dataplatform.quality.gaps import GapReason, build_report, expectations_from_register
from dataplatform.status import sync_state
from dataplatform.status.console import data_render
from dataplatform.status.deviations import DeviationJudge, GapItem, default_deviations
from dataplatform.status.models import SourcesOut, SourceStatusOut
from dataplatform.status.sync_state import (
    SourceStatus,
    SyncRecord,
    SyncState,
    SyncStateStore,
    evaluate_green,
)
from dataplatform.store.db import Connection

ROOT: Final = Path(__file__).resolve().parents[2]
PAUSED: Final = ("bse_announcements", "bse_corp_actions", "bse_scrip_master")
SINCE: Final = date(2026, 10, 24)
MONDAY: Final = date(2026, 10, 26)  # a session after the pause
FRIDAY: Final = date(2026, 10, 23)  # a session before it
AT: Final = datetime(2026, 10, 23, 0, 31, tzinfo=IST)
SPIKE: Final = (
    "ForbiddenSpikeError: ingestion from api.bseindia.com is hard-stopped after a 403 spike; this "
    "process will not request it again."
)


# ── the register ───────────────────────────────────────────────────────────────────────────


def test_exactly_the_api_bseindia_sources_are_paused_under_d29() -> None:
    register = load()
    paused = paused_sources()
    assert sorted(paused) == list(PAUSED)
    for source_id, record in paused.items():
        assert record.since == SINCE and record.decision.startswith("HUMAN_DECISIONS D29")
        row = next(s for s in register.sources if s.id == source_id)
        assert row.host == "api.bseindia.com" and row.status.value == "VERIFIED"
    by_id = {s.id: s for s in register.sources}
    assert not by_id["bse_bhavcopy_udiff"].is_paused  # www.bseindia.com still works
    assert all(not s.is_paused for s in register.sources if s.host != "api.bseindia.com"), (
        "only the blocked host's sources are paused"
    )


def test_the_cited_decision_exists() -> None:
    text = (ROOT / "HUMAN_DECISIONS.md").read_text(encoding="utf-8")
    assert "### D29 — " in text


# ── the gate boundary ──────────────────────────────────────────────────────────────────────


def test_a_paused_source_is_never_green_published_or_set_aside() -> None:
    """Invariant #10: PAUSED is not data. The interlock is red for a paused dataset on a session,
    and a pause is not a decline — `is_green` sets aside only DECLINED sources."""
    store = SyncStateStore(cast(Connection, object()), calendar=trading_calendar())
    assert not set(store.declined) & set(PAUSED)
    for source in PAUSED:
        green = evaluate_green(
            MONDAY,
            (source,),
            {},
            day_kind=DayKind.SESSION,
            open_error_flags=0,
            declined=store.declined.keys(),
        )
        assert not green.green and source in green.missing
        published_before_pause = SyncRecord(
            source=source, logical_date=MONDAY, state=SyncState.FAILED, attempts=1,
            retryable=True, last_error=SPIKE, updated_at=AT,
        )  # fmt: skip
        assert not evaluate_green(
            MONDAY, (source,), {source: published_before_pause}, day_kind=DayKind.SESSION,
            open_error_flags=0, declined=store.declined.keys(),
        ).green  # fmt: skip


def test_the_interlock_code_never_reads_a_pause() -> None:
    """Pinned statically: `is_green`, `evaluate_green` and the store's `is_green` name no pause."""
    for function in (sync_state.is_green, sync_state.evaluate_green, SyncStateStore.is_green):
        tree = ast.parse(inspect.getsource(function).lstrip())
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert not {n for n in names if "paus" in n.lower()}, function.__name__


def test_paused_is_display_only_on_the_source_line() -> None:
    """`healthy` keeps its strict value; only the word and colour change; counted apart."""
    status = SourceStatus(
        source="bse_announcements", last_success_date=None, last_success_at=None,
        latest_date=FRIDAY, lag_days=None, lag_sessions=None, failure_streak=24,
        last_failure_date=FRIDAY, last_error=SPIKE, last_failure_retryable=True,
        counts={SyncState.FAILED: 24}, paused=paused_sources()["bse_announcements"],
    )  # fmt: skip
    assert not status.healthy
    line = SourceStatusOut.of(status)
    assert line.paused is not None and line.paused.decision.startswith("HUMAN_DECISIONS D29")
    assert data_render.source_health(line) == ("PAUSED", data_render.Rag.GREY)
    other = SourceStatusOut.of(
        SourceStatus(
            source="nse_xbrl_filing",
            last_success_date=FRIDAY,
            last_success_at=AT,
            latest_date=FRIDAY,
            lag_days=0,
            lag_sessions=0,
            failure_streak=1,
            last_failure_date=FRIDAY,
            last_error="KeyError: 'x'",
            last_failure_retryable=True,
            counts={SyncState.FAILED: 1, SyncState.PUBLISHED: 3},
        )
    )
    html = data_render.render_sources(
        SourcesOut(as_of=MONDAY, sources=[line, other]), [other, line]
    )
    assert 'data-health="PAUSED"' in html and "rag-grey" in html and "D29" in html
    assert "2 not healthy: 1 error, 0 accepted, 1 paused" in html


# ── the gap report ─────────────────────────────────────────────────────────────────────────


def _gaps(day: date, *records: SyncRecord) -> list[tuple[str, GapReason]]:
    report = build_report(
        day,
        day,
        sources=["bse_announcements", "nse_announcements"],
        records={(r.source, r.logical_date): r for r in records},
        calendar=trading_calendar(),
        expectations=expectations_from_register(),
    )
    return [(entry.source, entry.reason) for entry in report.entries]


def test_a_paused_sources_days_from_since_are_explained_as_paused() -> None:
    assert ("bse_announcements", GapReason.PAUSED) in _gaps(MONDAY)
    assert GapReason.PAUSED.explained
    # The same day is still owed by the source that is not paused.
    assert ("nse_announcements", GapReason.NEVER_ATTEMPTED) in _gaps(MONDAY)


def test_days_before_the_pause_and_rows_after_it_keep_their_own_verdicts() -> None:
    assert ("bse_announcements", GapReason.NEVER_ATTEMPTED) in _gaps(FRIDAY)
    failed = SyncRecord(
        source="bse_announcements", logical_date=MONDAY, state=SyncState.FAILED, attempts=1,
        retryable=True, last_error=SPIKE, updated_at=AT,
    )  # fmt: skip
    assert ("bse_announcements", GapReason.FAILED) in _gaps(MONDAY, failed)


# ── identity_refresh: conflicts on the paused exchange are held, not failed on ────────────────


def _identity(*conflicts: IdentityConflict, refusals: bool = False) -> IdentityRefreshReport:
    ingest = IdentityIngestReport(
        snapshot_date=SINCE, exchange=Exchange.NSE, securities_seen=2605, renames_applied=0,
        unlinked_changes=0, counts=WriteCounts(), conflicts=conflicts,
        refusals=cast(Any, ("refused",)) if refusals else (),
    )  # fmt: skip
    return IdentityRefreshReport(snapshot_date=SINCE, refs=(), reused=(), ingest=ingest)


def _conflict(exchange: Exchange, symbol: str) -> IdentityConflict:
    return IdentityConflict(
        kind=ConflictKind.SYMBOL_TO_ISIN, exchange=exchange, on_date=date(2026, 9, 6),
        symbols=(symbol,), isins=("INE0000A0001", "INE0000B0001"), detected_by=DetectedBy.INGEST,
        source="bse_scrip_master",
    )  # fmt: skip


def test_bse_conflicts_are_held_while_the_scrip_master_is_paused() -> None:
    paused = paused_sources()
    assert refresh_failure(_identity(_conflict(Exchange.BSE, "ALPHA")), paused) is None
    # Inverted: the same conflict with the scrip master live fails the run, as it always did.
    assert refresh_failure(_identity(_conflict(Exchange.BSE, "ALPHA")), {}) is not None


def test_an_nse_conflict_or_a_refusal_still_fails_the_refresh() -> None:
    paused = paused_sources()
    nse = _identity(_conflict(Exchange.BSE, "ALPHA"), _conflict(Exchange.NSE, "BLSE"))
    assert refresh_failure(nse, paused) is not None
    assert refresh_failure(_identity(refusals=True), paused) is not None
    assert refresh_failure(_identity(), paused) is None


# ── D26 still covers the failures from before the pause ───────────────────────────────────


def test_d26_still_accepts_the_pre_pause_403_rows() -> None:
    judge = DeviationJudge(
        default_deviations(),
        cast(Connection, object()),
        cast(SyncStateStore, object()),
        MONDAY,
    )
    for source in PAUSED:
        verdict = judge.gap(GapItem(source, FRIDAY, "FAILED", SPIKE))
        assert verdict is not None and verdict.accepted, source
