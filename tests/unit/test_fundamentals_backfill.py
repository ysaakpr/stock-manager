"""M10.4 acceptance: the fundamentals backfill runner, end to end and offline.

Every acceptance criterion of the task is a test here, and every one runs offline (B8): the network
is a `RecordedTransport` scripted with the **captured** NSE fixtures (`tests/fixtures/xbrl/`) — a
real index response and the real XBRL documents it links — the L0 store and the `pit_fundamentals`
L1 store are a temp lake, and `sync_state` is an in-memory stand-in speaking exactly the transitions
the runner drives. No socket is opened, and no Postgres is needed.

  1. `pit_fundamentals` holds real P&L facts (revenue, PAT, EPS) for the price-window names, keyed
     by ISIN + filing_date — read back out of the store the runner wrote
     (`test_backfill_lands_real_pit_facts`).
  2. `read_pit(on_date)` returns only facts knowable then, and a restatement is a new record, never
     an overwrite (invariant #8) — V.S.T Tillers really filed its 31-Dec-2024 quarter twice, first
     overstating revenue by 10x and correcting it seventeen months later, so both versions coexist,
     a read dated before the correction sees only the wrong original figure, and `read_latest`
     supersedes only once the correction is knowable
     (`test_restatement_is_pit_correct_never_overwritten`).
  3. the runner is resumable (`test_resume_refetches_nothing`), reports coverage
     (`test_coverage_report_enumerates_what_ran_and_what_did_not`), and a 403 spike parks it with an
     enumerated cause, leaving later filings untouched (`test_403_spike_parks...`).

Three more guard the identity and planning invariants: an out-of-universe ISIN is skipped and
surfaced (`test_out_of_universe_isin_is_skipped_and_surfaced`), an announcement carrying no XBRL
document is skipped and counted rather than fetched
(`test_an_announcement_with_no_document_is_skipped_and_counted`), and the plan is pure/offline
(`test_index_plan_is_pure_and_offline`).
"""

from __future__ import annotations

import socket
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, cast

import pytest

from dataplatform.alerts import build_alerter
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest import fundamentals_backfill as fb
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
)
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncState
from dataplatform.store.l0 import L0Store
from dataplatform.store.pit_fundamentals import read_l1, read_latest, read_pit

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "xbrl"
INDEX_FIXTURE: Final = FIXTURES / "index" / "corporates-financial-results_slice.json"
FILINGS_DIR: Final = FIXTURES / "filings"

WARM_URL: Final = "https://www.nseindia.com/"
NOW: Final = datetime(2026, 9, 3, 18, 30, tzinfo=IST)
CLOCK: Final = FrozenClock(NOW)

#: The captured index slice holds 18 real announcements — 17 with an XBRL document and one with
#: none. `VSTTILLERS` accounts for four (two natures x an original filing and its correction) and
#: `SCHAEFFLER` for four (two natures x a quarterly and an annual entry over the same documents).
VSTTILLERS: Final = "INE764D01017"
SCHAEFFLER: Final = "INE513A01014"
VIDEOIND: Final = "INE703A01011"  # the announcement with no XBRL document
ENTRIES_IN_SLICE: Final = 18
Q3FY25_END: Final = date(2024, 12, 31)
VST_ORIGINAL_FILED: Final = date(2025, 2, 11)
VST_RESTATED_FILED: Final = date(2026, 7, 30)
#: What the original filing said, and what the correction seventeen months later said.
VST_ORIGINAL_REVENUE: Final = "21910000000.00"
VST_CORRECTED_REVENUE: Final = "2191000000.00"

FROM: Final = date(2026, 1, 1)
TO: Final = date(2026, 12, 31)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in this module is a bug: every fetch is a `RecordedTransport` (B8)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; fundamentals-backfill tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


# ── wiring (offline) ─────────────────────────────────────────────────────────────────────────


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_root=tmp_path)


def _fetcher(transport: RecordedTransport, settings: Settings) -> Fetcher:
    return Fetcher(
        transport=transport,
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        alerter=build_alerter(settings, clock=CLOCK),
        clock=CLOCK,
        register=load_register(),
        settings=settings,
        sleep=lambda _seconds: None,
    )


def _quarterly_plan() -> list[fb.IndexUnit]:
    """One quarterly index chunk over the window — the whole fixture arrives in a single fetch."""
    return fb.build_index_units(
        FROM,
        TO,
        register=load_register(),
        chunk_months=12,
        periods=(fb.Period.QUARTERLY,),
    )


def _ok_transport(plan: Sequence[fb.IndexUnit]) -> RecordedTransport:
    """Serve the warm-up, each index chunk's fixture, and each filing's XBRL fixture, all 200."""
    script: dict[str, RecordedResponse | list[RecordedResponse]] = {
        WARM_URL: RecordedResponse(status_code=200, body=b"", headers={"content-type": "text/html"})
    }
    for unit in plan:
        script[unit.url] = RecordedResponse(
            status_code=200,
            body=INDEX_FIXTURE.read_bytes(),
            headers={"content-type": "application/json"},
        )
    for xml in FILINGS_DIR.glob("*.xml"):
        url = f"https://nsearchives.nseindia.com/corporate/xbrl/{xml.name}"
        script[url] = RecordedResponse(
            status_code=200, body=xml.read_bytes(), headers={"content-type": "application/xml"}
        )
    return RecordedTransport(cast("Any", script))


def _runner(
    transport: RecordedTransport,
    *,
    settings: Settings,
    sync: _FakeSync,
    universe: set[str],
) -> fb.FundamentalsBackfillRunner:
    return fb.FundamentalsBackfillRunner(
        fetcher=_fetcher(transport, settings),
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        sync=cast("Any", sync),
        universe=universe,
        commit=lambda: None,
        data_root=settings.data_root,
    )


def _revenue_values(facts: Sequence[Any], isin: str) -> set[Any]:
    return {f.value for f in facts if f.isin == isin and f.concept == "revenue_from_operations"}


def _one(
    facts: Sequence[Any],
    isin: str,
    concept: str,
    period_end: date,
    nature: str = "Standalone",
    period_start: date | None = None,
) -> Any:
    """The single value matching these coordinates; a non-unique match is a test bug, not a pick.

    `period_start` is part of the key because one captured document is linked by both a quarterly
    and an annual entry, so `(isin, concept, period_end, nature)` alone can name two real facts.
    """
    found = [
        f.value
        for f in facts
        if f.isin == isin
        and f.concept == concept
        and f.period_end == period_end
        and f.nature.value == nature
        and (period_start is None or f.period_start == period_start)
    ]
    assert len(found) == 1, f"expected 1 fact for {isin}/{concept}/{period_end}, got {found}"
    return found[0]


# ── acceptance 1: real P&L facts land in the store, keyed by ISIN + filing_date ─────────────────


def test_backfill_lands_real_pit_facts(tmp_path: Path) -> None:
    from decimal import Decimal

    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    sync = _FakeSync()
    report = _runner(
        _ok_transport(plan), settings=settings, sync=sync, universe={VSTTILLERS, SCHAEFFLER}
    ).run(plan)

    # One index chunk; of the 18 announcements, the 8 belonging to the two universe names publish.
    assert report.index_published == 1
    assert report.filings_discovered == ENTRIES_IN_SLICE
    assert report.filings_in_universe == 8
    assert report.filings_published == 8
    assert report.filings_failed == 0
    assert not report.parked
    assert report.covered_isins == {VSTTILLERS, SCHAEFFLER}
    assert report.facts_written > 0

    # The store holds real P&L facts read back out of it, keyed by ISIN + filing_date.
    latest = read_latest(date(2026, 9, 1), data_root=settings.data_root)
    rev = _one(latest, VSTTILLERS, "revenue_from_operations", Q3FY25_END)
    pat = _one(latest, VSTTILLERS, "profit_after_tax", Q3FY25_END)
    eps = _one(latest, VSTTILLERS, "eps_basic", Q3FY25_END)
    assert rev == Decimal(VST_CORRECTED_REVENUE)
    assert pat == Decimal("17000000.00")
    assert eps == Decimal("1.97")
    # Money stays Decimal across the parquet round trip (a float here would be a bug).
    for value in (rev, pat, eps):
        assert type(value) is Decimal
    # Every stored fact is tagged with a real filing_date after its period end (invariant #7).
    for fact in latest:
        assert fact.filing_date > fact.period_end


def test_one_document_lands_its_quarter_and_its_year_separately(tmp_path: Path) -> None:
    """Schaeffler's document is linked by a quarterly and an annual entry; both must land.

    The runner ingests per *index entry*, not per document, which is what lets one set of bytes
    yield two records — and their `period_start` is the only thing that tells them apart, since
    they share `period_end`, `nature` and `filing_date`.
    """
    from decimal import Decimal

    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    _runner(_ok_transport(plan), settings=settings, sync=_FakeSync(), universe={SCHAEFFLER}).run(
        plan
    )

    stored = read_latest(date(2026, 9, 1), data_root=settings.data_root)
    quarter = _one(
        stored,
        SCHAEFFLER,
        "revenue_from_operations",
        Q3FY25_END,
        nature="Consolidated",
        period_start=date(2024, 10, 1),
    )
    year = _one(
        stored,
        SCHAEFFLER,
        "revenue_from_operations",
        Q3FY25_END,
        nature="Consolidated",
        period_start=date(2024, 1, 1),
    )
    assert quarter == Decimal("21360600000.00")
    assert year == Decimal("82323800000.00")


# ── acceptance 2: PIT correctness and restatement-as-new-record (invariant #8) ─────────────────


def test_restatement_is_pit_correct_never_overwritten(tmp_path: Path) -> None:
    from decimal import Decimal

    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    _runner(_ok_transport(plan), settings=settings, sync=_FakeSync(), universe={VSTTILLERS}).run(
        plan
    )

    # Before the correction is knowable, only the original (10x overstated) figure is seen. This is
    # the point of the PIT store: a 2025 backtest must read the wrong number the market had.
    early = read_pit(date(2025, 6, 1), data_root=settings.data_root)
    assert _revenue_values(early, VSTTILLERS) == {Decimal(VST_ORIGINAL_REVENUE)}

    # Once both are knowable, both versions coexist — a restatement is a new record, not an update.
    both = read_pit(date(2026, 9, 1), data_root=settings.data_root)
    assert _revenue_values(both, VSTTILLERS) == {
        Decimal(VST_ORIGINAL_REVENUE),
        Decimal(VST_CORRECTED_REVENUE),
    }

    # read_latest supersedes to the corrected value only after its filing date is knowable.
    assert _one(
        read_latest(date(2025, 6, 1), data_root=settings.data_root),
        VSTTILLERS,
        "revenue_from_operations",
        Q3FY25_END,
    ) == Decimal(VST_ORIGINAL_REVENUE)
    assert _one(
        read_latest(date(2026, 9, 1), data_root=settings.data_root),
        VSTTILLERS,
        "revenue_from_operations",
        Q3FY25_END,
    ) == Decimal(VST_CORRECTED_REVENUE)

    # The original partition is physically untouched — the number the market first saw survives.
    original = read_l1(VST_ORIGINAL_FILED, data_root=settings.data_root)
    assert _revenue_values(original, VSTTILLERS) == {Decimal(VST_ORIGINAL_REVENUE)}
    corrected = read_l1(VST_RESTATED_FILED, data_root=settings.data_root)
    assert _revenue_values(corrected, VSTTILLERS) == {Decimal(VST_CORRECTED_REVENUE)}


# ── acceptance 3: resumable, reports coverage, parks on a hard block ────────────────────────────


def test_resume_refetches_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    sync = _FakeSync()

    first = _runner(
        _ok_transport(plan), settings=settings, sync=sync, universe={VSTTILLERS, SCHAEFFLER}
    ).run(plan)
    assert first.filings_published == 8

    # A second run over the same plan and checkpoint re-fetches nothing and publishes nothing —
    # not even the index (its chunk is re-read from L0, no socket).
    transport2 = _ok_transport(plan)
    second = _runner(
        transport2, settings=settings, sync=sync, universe={VSTTILLERS, SCHAEFFLER}
    ).run(plan)
    assert second.filings_published == 0
    assert second.filings_skipped_published == 8
    assert second.index_published == 0
    assert second.index_skipped_published == 1
    assert transport2.requests == []  # not a single socket call, warm-up included


def test_403_spike_parks_with_enumerated_cause(tmp_path: Path) -> None:
    """The index succeeds, then every XBRL fetch 403s; the third trips the spike and parks."""
    settings = _settings(tmp_path)
    assert settings.http_forbidden_streak_limit == 3
    plan = _quarterly_plan()

    script: dict[str, RecordedResponse | list[RecordedResponse]] = {
        WARM_URL: RecordedResponse(status_code=200, body=b"", headers={"content-type": "text/html"})
    }
    for unit in plan:
        script[unit.url] = RecordedResponse(
            status_code=200,
            body=INDEX_FIXTURE.read_bytes(),
            headers={"content-type": "application/json"},
        )
    for xml in FILINGS_DIR.glob("*.xml"):
        url = f"https://nsearchives.nseindia.com/corporate/xbrl/{xml.name}"
        script[url] = RecordedResponse(status_code=403, body=b"forbidden")
    transport = RecordedTransport(cast("Any", script))
    sync = _FakeSync()

    report = _runner(
        transport, settings=settings, sync=sync, universe={VSTTILLERS, SCHAEFFLER}
    ).run(plan)

    assert report.parked
    assert report.park_reason is fb.ParkReason.FORBIDDEN_SPIKE
    assert report.park_detail is not None and "FORBIDDEN_SPIKE" in report.park_detail
    # The index chunk still landed; the filings did not.
    assert report.index_published == 1
    assert report.filings_published == 0
    # Two filings recorded FAILED before the third tripped the spike; the rest never ran.
    assert report.filings_failed == 2
    # A parked run is never silent: the coverage report renders the enumerated cause.
    rendered = fb.render_report(from_date=FROM, to_date=TO, universe_size=2, report=report)
    assert "PARKED" in rendered and "FORBIDDEN_SPIKE" in rendered


def test_coverage_report_enumerates_what_ran_and_what_did_not(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    report = _runner(
        _ok_transport(plan), settings=settings, sync=_FakeSync(), universe={VSTTILLERS, SCHAEFFLER}
    ).run(plan)

    rendered = fb.render_report(from_date=FROM, to_date=TO, universe_size=2, report=report)
    assert "Filings published: 8" in rendered
    assert "Facts written:" in rendered
    assert "ISINs covered: 2" in rendered
    assert "Window: 2026-01-01 .. 2026-12-31" in rendered
    # Both non-ingest outcomes are named, so a reader is never left guessing at the difference
    # between the discovered count and the published one.
    assert "Entries skipped (ISIN not in universe): 10" in rendered
    assert "Entries skipped (no XBRL document in the feed): 0" in rendered


# ── identity resolution and pure planning ───────────────────────────────────────────────────────


def test_out_of_universe_isin_is_skipped_and_surfaced(tmp_path: Path) -> None:
    """A filing whose ISIN is not a resolved price-window name is skipped, not silently dropped."""
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    # Only V.S.T Tillers is in the universe; the other 14 announcements must be skipped and named.
    report = _runner(
        _ok_transport(plan), settings=settings, sync=_FakeSync(), universe={VSTTILLERS}
    ).run(plan)

    assert report.filings_in_universe == 4  # the four V.S.T Tillers entries
    assert report.filings_published == 4
    assert report.skipped_out_of_universe == ENTRIES_IN_SLICE - 4
    assert SCHAEFFLER in report.unresolved_isins
    assert report.covered_isins == {VSTTILLERS}


def test_an_announcement_with_no_document_is_skipped_and_counted(tmp_path: Path) -> None:
    """A real announcement with no XBRL attachment is counted, never fetched, never dropped.

    The feed states such a record's `xbrl` as the archive path ending in a bare `-`. The register
    forbids constructing the URL, so there is nothing to fetch — but a coverage report that simply
    lost the row would be the silent drop CLAUDE.md rules out.
    """
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    transport = _ok_transport(plan)
    report = _runner(transport, settings=settings, sync=_FakeSync(), universe={VIDEOIND}).run(plan)

    assert report.skipped_no_document == 1
    assert report.filings_in_universe == 0
    assert report.filings_published == 0
    assert report.filings_failed == 0
    assert not report.parked
    # Nothing was fetched from the archive host for it: only the index chunk and the warm-up ran.
    assert not any("nsearchives" in request.url for request in transport.requests)
    rendered = fb.render_report(from_date=FROM, to_date=TO, universe_size=1, report=report)
    assert "Entries skipped (no XBRL document in the feed): 1" in rendered


def test_max_filings_caps_the_ingest_but_not_the_reported_universe(tmp_path: Path) -> None:
    """A bounded sample attempts at most `max_filings`, yet still reports the true universe size."""
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    runner = fb.FundamentalsBackfillRunner(
        fetcher=_fetcher(_ok_transport(plan), settings),
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        sync=cast("Any", _FakeSync()),
        universe={VSTTILLERS, SCHAEFFLER},
        commit=lambda: None,
        data_root=settings.data_root,
        max_filings=2,
    )
    report = runner.run(plan)

    assert report.filings_published == 2  # only two attempted
    assert report.filings_in_universe == 8  # but the full universe is still counted
    assert len(report.covered_isins) <= 2


def test_index_plan_is_pure_and_offline() -> None:
    register = load_register()
    # Both periods across 3-month chunks over a full year: 4 quarters x 2 periods = 8 chunks.
    plan = fb.build_index_units(FROM, TO, register=register, chunk_months=3)
    assert len(plan) == 8
    quarterly = [u for u in plan if u.period is fb.Period.QUARTERLY]
    annual = [u for u in plan if u.period is fb.Period.ANNUAL]
    assert len(quarterly) == len(annual) == 4
    # The URL carries the filled period and date range, with no leftover placeholder.
    first = quarterly[0]
    assert "period=Quarterly" in first.url
    assert "from_date=01-01-2026" in first.url
    assert "{" not in first.url
    # A bounded sample truncates the plan (B1 verify-then-go).
    assert len(fb.build_index_units(FROM, TO, register=register, chunk_months=3, limit=3)) == 3


# ── in-memory sync_state stand-in ──────────────────────────────────────────────────────────────


class _SyncRow:
    """The slice of a `SyncRecord` the runner reads: its `state` and `retryable` flag."""

    def __init__(self, state: SyncState, *, retryable: bool = True) -> None:
        self.state = state
        self.retryable = retryable


class _FakeSync:
    """In-memory `SyncStateStore` stand-in keyed by `(source, logical_date)`, as the real store is.

    Models only what the runner calls; an unknown call would be an `AttributeError`, the loud
    failure we want (B8). The runner's resume check — "is this unit's row PUBLISHED?" — is a real
    round trip against it.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, date], _SyncRow] = {}

    def get(self, source: str, logical_date: date) -> _SyncRow | None:
        return self._rows.get((source, logical_date))

    def begin(self, source: str, logical_date: date) -> _SyncRow:
        row = _SyncRow(SyncState.PENDING)
        self._rows[(source, logical_date)] = row
        return row

    def _advance(self, source: str, logical_date: date, state: SyncState) -> _SyncRow:
        row = self._rows[(source, logical_date)]
        row.state = state
        return row

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> _SyncRow:
        return self._advance(source, logical_date, SyncState.FETCHED)

    def mark_validated(self, source: str, logical_date: date) -> _SyncRow:
        return self._advance(source, logical_date, SyncState.VALIDATED)

    def mark_normalized(self, source: str, logical_date: date) -> _SyncRow:
        return self._advance(source, logical_date, SyncState.NORMALIZED)

    def mark_published(self, source: str, logical_date: date) -> _SyncRow:
        return self._advance(source, logical_date, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> _SyncRow:
        row = self._rows.setdefault((source, logical_date), _SyncRow(SyncState.PENDING))
        row.state = SyncState.FAILED
        row.retryable = retryable
        return row
