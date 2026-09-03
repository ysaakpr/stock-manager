"""M10.4 acceptance: the fundamentals backfill runner, end to end and offline.

Every acceptance criterion of the task is a test here, and every one runs offline (B8): the network
is a `RecordedTransport` scripted with the real checked-in XBRL fixtures (the M7.3 index + filing
documents), the L0 store and the `pit_fundamentals` L1 store are a temp lake, and `sync_state` is an
in-memory stand-in speaking exactly the transitions the runner drives. No socket is opened, and no
Postgres is needed.

  1. `pit_fundamentals` holds real P&L facts (revenue, PAT, EPS) for the price-window names, keyed
     by ISIN + filing_date — read back out of the store the runner wrote
     (`test_backfill_lands_real_pit_facts`).
  2. `read_pit(on_date)` returns only facts knowable then, and a restatement is a new record, never
     an overwrite (invariant #8) — Kaynes files 31-Mar-2026 twice and both versions coexist, a read
     dated before the restatement sees only the original, `read_latest` supersedes only once the
     restatement is knowable (`test_restatement_is_pit_correct_never_overwritten`).
  3. the runner is resumable (`test_resume_refetches_nothing`), reports coverage
     (`test_coverage_report_enumerates_what_ran_and_what_did_not`), and a 403 spike parks it with an
     enumerated cause, leaving later filings untouched (`test_403_spike_parks...`).

Two more guard the identity and planning invariants: an out-of-universe ISIN is skipped and surfaced
(`test_out_of_universe_isin_is_skipped_and_surfaced`), and the plan is pure/offline
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
INDEX_FIXTURE: Final = FIXTURES / "index_v1" / "corporates-financial-results_20260901.json"
FILINGS_DIR: Final = FIXTURES / "filing_v1"

WARM_URL: Final = "https://www.nseindia.com/"
NOW: Final = datetime(2026, 9, 3, 18, 30, tzinfo=IST)
CLOCK: Final = FrozenClock(NOW)

TCS: Final = "INE467B01029"
KAYNES: Final = "INE918Z01012"
Q4FY26_END: Final = date(2026, 3, 31)
KAYNES_ORIGINAL_FILED: Final = date(2026, 5, 15)
KAYNES_RESTATED_FILED: Final = date(2026, 8, 20)

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
    facts: Sequence[Any], isin: str, concept: str, period_end: date, nature: str = "Standalone"
) -> Any:
    return next(
        f.value
        for f in facts
        if f.isin == isin
        and f.concept == concept
        and f.period_end == period_end
        and f.nature.value == nature
    )


# ── acceptance 1: real P&L facts land in the store, keyed by ISIN + filing_date ─────────────────


def test_backfill_lands_real_pit_facts(tmp_path: Path) -> None:
    from decimal import Decimal

    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    sync = _FakeSync()
    report = _runner(_ok_transport(plan), settings=settings, sync=sync, universe={TCS, KAYNES}).run(
        plan
    )

    # One index chunk, four filings discovered, all four in universe and published.
    assert report.index_published == 1
    assert report.filings_discovered == 4
    assert report.filings_in_universe == 4
    assert report.filings_published == 4
    assert report.filings_failed == 0
    assert not report.parked
    assert report.covered_isins == {TCS, KAYNES}
    assert report.facts_written > 0

    # The store holds real P&L facts read back out of it, keyed by ISIN + filing_date.
    latest = read_latest(date(2026, 9, 1), data_root=settings.data_root)
    tcs_rev = _one(latest, TCS, "revenue_from_operations", date(2026, 6, 30))
    tcs_pat = _one(latest, TCS, "profit_after_tax", date(2026, 6, 30))
    tcs_eps = _one(latest, TCS, "eps_basic", date(2026, 6, 30))
    assert tcs_rev == Decimal("630000000000")
    assert tcs_pat == Decimal("122000000000")
    assert tcs_eps == Decimal("33.50")
    # Money stays Decimal across the parquet round trip (a float here would be a bug).
    for value in (tcs_rev, tcs_pat, tcs_eps):
        assert type(value) is Decimal
    # Every stored fact is tagged with a real filing_date after its period end (invariant #7).
    for fact in latest:
        assert fact.filing_date > fact.period_end


# ── acceptance 2: PIT correctness and restatement-as-new-record (invariant #8) ─────────────────


def test_restatement_is_pit_correct_never_overwritten(tmp_path: Path) -> None:
    from decimal import Decimal

    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    _runner(_ok_transport(plan), settings=settings, sync=_FakeSync(), universe={TCS, KAYNES}).run(
        plan
    )

    # Before the restatement is knowable, only the original 15-May Kaynes figure is seen.
    early = read_pit(date(2026, 6, 1), data_root=settings.data_root)
    assert _revenue_values(early, KAYNES) == {Decimal("8000000000")}

    # Once both are knowable, both versions coexist — a restatement is a new record, not an update.
    both = read_pit(date(2026, 9, 1), data_root=settings.data_root)
    assert _revenue_values(both, KAYNES) == {Decimal("8000000000"), Decimal("7800000000")}

    # read_latest supersedes to the restated value only after its filing date is knowable.
    assert _one(
        read_latest(date(2026, 6, 1), data_root=settings.data_root),
        KAYNES,
        "revenue_from_operations",
        Q4FY26_END,
    ) == Decimal("8000000000")
    assert _one(
        read_latest(date(2026, 9, 1), data_root=settings.data_root),
        KAYNES,
        "revenue_from_operations",
        Q4FY26_END,
    ) == Decimal("7800000000")

    # The original partition is physically untouched — the number the market first saw survives.
    original = read_l1(KAYNES_ORIGINAL_FILED, data_root=settings.data_root)
    assert _revenue_values(original, KAYNES) == {Decimal("8000000000")}
    restated = read_l1(KAYNES_RESTATED_FILED, data_root=settings.data_root)
    assert _revenue_values(restated, KAYNES) == {Decimal("7800000000")}


# ── acceptance 3: resumable, reports coverage, parks on a hard block ────────────────────────────


def test_resume_refetches_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    sync = _FakeSync()

    first = _runner(_ok_transport(plan), settings=settings, sync=sync, universe={TCS, KAYNES}).run(
        plan
    )
    assert first.filings_published == 4

    # A second run over the same plan and checkpoint re-fetches nothing and publishes nothing —
    # not even the index (its chunk is re-read from L0, no socket).
    transport2 = _ok_transport(plan)
    second = _runner(transport2, settings=settings, sync=sync, universe={TCS, KAYNES}).run(plan)
    assert second.filings_published == 0
    assert second.filings_skipped_published == 4
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

    report = _runner(transport, settings=settings, sync=sync, universe={TCS, KAYNES}).run(plan)

    assert report.parked
    assert report.park_reason is fb.ParkReason.FORBIDDEN_SPIKE
    assert report.park_detail is not None and "FORBIDDEN_SPIKE" in report.park_detail
    # The index chunk still landed; the filings did not.
    assert report.index_published == 1
    assert report.filings_published == 0
    # Two filings recorded FAILED before the third tripped the spike; the fourth never ran.
    assert report.filings_failed == 2
    # A parked run is never silent: the coverage report renders the enumerated cause.
    rendered = fb.render_report(from_date=FROM, to_date=TO, universe_size=2, report=report)
    assert "PARKED" in rendered and "FORBIDDEN_SPIKE" in rendered


def test_coverage_report_enumerates_what_ran_and_what_did_not(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    report = _runner(
        _ok_transport(plan), settings=settings, sync=_FakeSync(), universe={TCS, KAYNES}
    ).run(plan)

    rendered = fb.render_report(from_date=FROM, to_date=TO, universe_size=2, report=report)
    assert "Filings published: 4" in rendered
    assert "Facts written:" in rendered
    assert "ISINs covered: 2" in rendered
    assert "Window: 2026-01-01 .. 2026-12-31" in rendered


# ── identity resolution and pure planning ───────────────────────────────────────────────────────


def test_out_of_universe_isin_is_skipped_and_surfaced(tmp_path: Path) -> None:
    """A filing whose ISIN is not a resolved price-window name is skipped, not silently dropped."""
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    # Only Kaynes is in the universe; the two TCS filings must be skipped and surfaced.
    report = _runner(
        _ok_transport(plan), settings=settings, sync=_FakeSync(), universe={KAYNES}
    ).run(plan)

    assert report.filings_in_universe == 2  # the two Kaynes filings
    assert report.filings_published == 2
    assert report.skipped_out_of_universe == 2  # the two TCS filings
    assert TCS in report.unresolved_isins
    assert report.covered_isins == {KAYNES}


def test_max_filings_caps_the_ingest_but_not_the_reported_universe(tmp_path: Path) -> None:
    """A bounded sample attempts at most `max_filings`, yet still reports the true universe size."""
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    runner = fb.FundamentalsBackfillRunner(
        fetcher=_fetcher(_ok_transport(plan), settings),
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        sync=cast("Any", _FakeSync()),
        universe={TCS, KAYNES},
        commit=lambda: None,
        data_root=settings.data_root,
        max_filings=2,
    )
    report = runner.run(plan)

    assert report.filings_published == 2  # only two attempted
    assert report.filings_in_universe == 4  # but the full universe is still counted
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
