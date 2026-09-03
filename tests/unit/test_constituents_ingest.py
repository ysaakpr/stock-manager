"""M10.1 — index-constituents ingest runner (the sector/industry classification map).

The file is laid out as the three acceptance criteria, each written so a plausible wrong
implementation fails it:

1. **Every liquid name resolves to an Industry as-of a date via `membership_asof` over ingested
   data.** After a sweep, every constituent of the broad list read back through `membership_asof`
   carries a non-empty industry, and a name is looked up by its *sector* list too — the map is
   queryable, not merely written.
2. **The broad list plus at least 8 sectoral lists are ingested and queryable.** The sweep's
   `CoverageReport` reports one broad and ≥8 sectoral/thematic lists covered, and each is readable
   back out of L1 by `membership_asof`.
3. **The runner is resumable and reports coverage; a gated/failed slug parks with an enumerated
   cause.** A second sweep over already-ingested slugs re-fetches nothing (the transport is never
   touched) and reports them skipped; a gated slug (HTML soft-404, or a 403) and a plain fetch
   failure (404) each park with the right `ParkCause` and do not abort the rest of the sweep.

The suite is offline (B8): every response is a checked-in fixture or a scripted status, and a
socket is a test bug.
"""

from __future__ import annotations

import socket
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.constituents_ingest import (
    DEFAULT_INDEX_SET,
    CoverageReport,
    IndexCategory,
    IndexSpec,
    ParkCause,
    SlugStatus,
    render_coverage_markdown,
    run_constituents_ingest,
)
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
    ScriptedOutcome,
)
from dataplatform.ingest.indices import (
    constituents_state_source,
    constituents_url,
    membership_asof,
)
from dataplatform.status.sync_state import SyncRecord, SyncState, SyncStateStore
from dataplatform.store.l0 import L0Store
from tests.conftest import SettingsLoader

FIXTURES: Final = Path("tests/fixtures/nifty_indices/constituents")
AS_OF: Final = date(2026, 9, 1)
NOW: Final = datetime(2026, 9, 3, 18, 30, tzinfo=IST)

#: The slugs we have fixtures for at AS_OF — one broad list plus nine sectoral/thematic lists.
BROAD: Final = IndexSpec("nifty500", "NIFTY 500", IndexCategory.BROAD)
SECTORALS: Final[tuple[IndexSpec, ...]] = (
    IndexSpec("niftybank", "NIFTY BANK", IndexCategory.SECTORAL),
    IndexSpec("niftyit", "NIFTY IT", IndexCategory.SECTORAL),
    IndexSpec("niftyauto", "NIFTY AUTO", IndexCategory.SECTORAL),
    IndexSpec("niftypharma", "NIFTY PHARMA", IndexCategory.SECTORAL),
    IndexSpec("niftyfmcg", "NIFTY FMCG", IndexCategory.SECTORAL),
    IndexSpec("niftymetal", "NIFTY METAL", IndexCategory.SECTORAL),
    IndexSpec("niftyrealty", "NIFTY REALTY", IndexCategory.SECTORAL),
    IndexSpec("niftymedia", "NIFTY MEDIA", IndexCategory.SECTORAL),
    IndexSpec("niftyenergy", "NIFTY ENERGY", IndexCategory.THEMATIC),
)
SPECS: Final[tuple[IndexSpec, ...]] = (BROAD, *SECTORALS)


def _fixture_name(slug: str) -> Path:
    return FIXTURES / f"ind_{slug}list_{AS_OF:%Y%m%d}.csv"


# ── a §4.4 test double, so a whole sweep runs without Postgres (B8) ─────────────────────────────


class RecordingTracker:
    """An in-memory §4.4 state machine delegating every rule to `SyncRecord.transition`."""

    def __init__(self, clock: FrozenClock) -> None:
        self._clock = clock
        self.rows: dict[tuple[str, date], SyncRecord] = {}
        self.history: list[SyncState] = []

    def begin(self, source: str, logical_date: date) -> SyncRecord:
        existing = self.rows.get((source, logical_date))
        now = self._clock.now()
        record = (
            SyncRecord(
                source=source,
                logical_date=logical_date,
                state=SyncState.PENDING,
                updated_at=now,
                attempts=1,
                first_attempt_at=now,
            )
            if existing is None
            else existing.transition(SyncState.PENDING, at=now)
        )
        return self._store(record)

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> SyncRecord:
        return self._advance(
            source, logical_date, SyncState.FETCHED, checksum=checksum, l0_path=l0_path
        )

    def mark_validated(self, source: str, logical_date: date) -> SyncRecord:
        return self._advance(source, logical_date, SyncState.VALIDATED)

    def mark_normalized(self, source: str, logical_date: date) -> SyncRecord:
        return self._advance(source, logical_date, SyncState.NORMALIZED)

    def mark_published(self, source: str, logical_date: date) -> SyncRecord:
        return self._advance(source, logical_date, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> SyncRecord:
        return self._advance(
            source, logical_date, SyncState.FAILED, error=error, retryable=retryable
        )

    def _advance(
        self, source: str, logical_date: date, to_state: SyncState, **kwargs: Any
    ) -> SyncRecord:
        current = self.rows[(source, logical_date)]
        return self._store(current.transition(to_state, at=self._clock.now(), **kwargs))

    def _store(self, record: SyncRecord) -> SyncRecord:
        self.rows[record.key] = record
        self.history.append(record.state)
        return record


def _tracker_protocol_is_satisfied_by_the_real_store(store: SyncStateStore) -> Any:
    """`mypy --strict` fails here if M1.3's store ever stops fitting the runner's protocol."""
    from dataplatform.ingest.indices import SyncTracker

    tracker: SyncTracker = store
    return tracker


class SpyAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str, str]] = []

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        self.sent.append((severity, title, body, dedup_key))
        return AlertOutcome.SENT


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in this module is a bug: every response is scripted or a checked-in file (B8)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; constituents tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def settings(load_settings: SettingsLoader) -> Settings:
    return load_settings(None)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def tracker(clock: FrozenClock) -> RecordingTracker:
    return RecordingTracker(clock)


@pytest.fixture
def build(clock: FrozenClock, settings: Settings, tmp_path: Path) -> Any:
    """Build a real `Fetcher` over a recorded transport and a real L0 store under `tmp_path`.

    Returns the fetcher, the L0 store, and the transport itself, so a test can assert exactly
    which URLs were (and were not) fetched — the evidence for the resume path.
    """
    from dataplatform.ingest.source_register import load as load_register

    register = load_register()

    def _build(
        script: dict[str, ScriptedOutcome | list[ScriptedOutcome]],
    ) -> tuple[Fetcher, L0Store, RecordedTransport]:
        transport = RecordedTransport(script)
        l0 = L0Store(clock=clock, data_root=tmp_path)
        fetcher = Fetcher(
            transport=transport,
            l0=l0,
            alerter=SpyAlerter(),
            clock=clock,
            register=register,
            settings=settings,
            sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
        )
        return fetcher, l0, transport

    return _build


def _ok_csv(slug: str, repo_root: Path) -> RecordedResponse:
    return RecordedResponse(
        body=(repo_root / _fixture_name(slug)).read_bytes(),
        headers={"content-type": "application/octet-stream"},
    )


def _all_ok_script(repo_root: Path) -> dict[str, ScriptedOutcome | list[ScriptedOutcome]]:
    return {constituents_url(spec.slug): _ok_csv(spec.slug, repo_root) for spec in SPECS}


# ── acceptance 2: the broad list plus ≥8 sectoral lists are ingested and queryable ─────────────


def test_the_sweep_ingests_broad_and_at_least_eight_sectoral_lists(
    build: Any, tracker: RecordingTracker, repo_root: Path, tmp_path: Path
) -> None:
    fetcher, l0, _ = build(_all_ok_script(repo_root))
    report = run_constituents_ingest(
        fetcher=fetcher,
        l0=l0,
        tracker=tracker,
        as_of=AS_OF,
        specs=SPECS,
        data_root=tmp_path,
    )
    assert report.broad_covered >= 1
    assert report.sectoral_covered >= 8
    assert report.is_covered
    assert not report.parked
    # Every configured slug is now queryable back out of L1.
    for spec in SPECS:
        view = membership_asof(spec.slug, AS_OF, data_root=tmp_path)
        assert view is not None and view.rows, f"{spec.slug} not queryable after the sweep"


# ── acceptance 1: every liquid name resolves to an Industry as-of a date ────────────────────────


def test_every_liquid_name_resolves_to_an_industry_asof_a_date(
    build: Any, tracker: RecordingTracker, repo_root: Path, tmp_path: Path
) -> None:
    fetcher, l0, _ = build(_all_ok_script(repo_root))
    run_constituents_ingest(
        fetcher=fetcher, l0=l0, tracker=tracker, as_of=AS_OF, specs=SPECS, data_root=tmp_path
    )
    # A date after the snapshot sees the snapshot's membership, and every name carries an industry.
    broad = membership_asof(BROAD.slug, date(2026, 9, 15), data_root=tmp_path)
    assert broad is not None
    for row in broad.rows:
        assert row.industry.strip(), f"{row.isin} has no industry"
    # A specific liquid name resolves to its published industry.
    reliance = next(r for r in broad.rows if r.symbol == "RELIANCE")
    assert reliance.industry == "Oil Gas & Consumable Fuels"
    # And it is findable through its sector list too — the classification map is by ISIN (#2).
    energy = membership_asof("niftyenergy", date(2026, 9, 15), data_root=tmp_path)
    assert energy is not None
    assert reliance.isin in energy.members


def test_a_name_before_the_first_snapshot_resolves_to_nothing_not_todays_map(
    build: Any, tracker: RecordingTracker, repo_root: Path, tmp_path: Path
) -> None:
    """The survivorship guard the runner must not defeat: no snapshot before AS_OF → no members."""
    fetcher, l0, _ = build(_all_ok_script(repo_root))
    run_constituents_ingest(
        fetcher=fetcher, l0=l0, tracker=tracker, as_of=AS_OF, specs=SPECS, data_root=tmp_path
    )
    assert membership_asof(BROAD.slug, date(2026, 8, 31), data_root=tmp_path) is None


# ── acceptance 3: resumable, reports coverage, and parks a gated/failed slug with a cause ───────


def test_the_sweep_is_resumable_and_refetches_nothing_already_present(
    build: Any, tracker: RecordingTracker, repo_root: Path, tmp_path: Path
) -> None:
    fetcher, l0, _ = build(_all_ok_script(repo_root))
    first = run_constituents_ingest(
        fetcher=fetcher, l0=l0, tracker=tracker, as_of=AS_OF, specs=SPECS, data_root=tmp_path
    )
    assert len(first.published) == len(SPECS)

    # Second sweep with an *empty* script: any re-fetch would raise UnrecordedRequestError. It must
    # not — everything is already in L1, so every slug is skipped and the transport is untouched.
    fetcher2, l0_2, transport2 = build({})
    second = run_constituents_ingest(
        fetcher=fetcher2, l0=l0_2, tracker=tracker, as_of=AS_OF, specs=SPECS, data_root=tmp_path
    )
    assert len(second.skipped) == len(SPECS)
    assert not second.published and not second.parked
    assert transport2.requests == []  # the resume path opened no request at all
    # Coverage is still reported on a resume — skipped slugs count as covered.
    assert second.is_covered


def test_a_gated_slug_parks_and_does_not_abort_the_sweep(
    build: Any, tracker: RecordingTracker, repo_root: Path, tmp_path: Path
) -> None:
    """A soft-404 HTML shell (200 + markup) and a 403 both park as GATED; a 404 parks FETCH_FAILED.

    The other slugs still publish — one gate does not cost the whole map (acceptance 3).
    """
    script = _all_ok_script(repo_root)
    # niftymedia: the Angular shell answering a bad path with HTML and a 200 — a session gate.
    script[constituents_url("niftymedia")] = RecordedResponse(
        status_code=200,
        body=b"<!DOCTYPE html><html><body>app</body></html>",
        headers={"content-type": "text/html"},
    )
    # niftyrealty: a hard 403 refusal — also a gate.
    script[constituents_url("niftyrealty")] = RecordedResponse(status_code=403, body=b"denied")
    # niftymetal: a plain 404 — a fetch failure, not a gate.
    script[constituents_url("niftymetal")] = RecordedResponse(status_code=404, body=b"missing")

    fetcher, l0, _ = build(script)
    report = run_constituents_ingest(
        fetcher=fetcher, l0=l0, tracker=tracker, as_of=AS_OF, specs=SPECS, data_root=tmp_path
    )

    parked = {o.spec.slug: o for o in report.parked}
    assert parked["niftymedia"].cause is ParkCause.GATED
    assert parked["niftyrealty"].cause is ParkCause.GATED
    assert parked["niftymetal"].cause is ParkCause.FETCH_FAILED
    # Every parked outcome carries an enumerated cause and a human detail.
    for outcome in report.parked:
        assert outcome.cause is not None and outcome.detail
    # The good slugs still landed — the sweep did not abort on the first gate.
    good = {o.spec.slug for o in report.published}
    assert "nifty500" in good and "niftyit" in good and "niftyenergy" in good
    assert membership_asof("niftyit", AS_OF, data_root=tmp_path) is not None
    # A parked slug is not queryable.
    assert membership_asof("niftymedia", AS_OF, data_root=tmp_path) is None


def test_a_parked_slug_is_journaled_as_failed_on_the_sync_row(
    build: Any, tracker: RecordingTracker, repo_root: Path, tmp_path: Path
) -> None:
    """A gate does not vanish — the shared sync row reaches FAILED so /status/sync can see it."""
    script = _all_ok_script(repo_root)
    script[constituents_url("niftymetal")] = RecordedResponse(status_code=404, body=b"missing")
    fetcher, l0, _ = build(script)
    run_constituents_ingest(
        fetcher=fetcher, l0=l0, tracker=tracker, as_of=AS_OF, specs=SPECS, data_root=tmp_path
    )
    assert SyncState.FAILED in tracker.history
    # Each slug files its own sync row, so the failing slug is journaled on its own key.
    assert (constituents_state_source("niftymetal"), AS_OF) in tracker.rows
    assert tracker.rows[(constituents_state_source("niftymetal"), AS_OF)].state is SyncState.FAILED
    # A slug that succeeded reached PUBLISHED on its own row — no collision.
    assert tracker.rows[(constituents_state_source("nifty500"), AS_OF)].state is SyncState.PUBLISHED


def test_coverage_below_bar_when_too_few_sectorals_land(
    build: Any, tracker: RecordingTracker, repo_root: Path, tmp_path: Path
) -> None:
    """`is_covered` is a real gate: the broad list alone, or too few sectorals, is below the bar."""
    only_broad_and_two = (BROAD, SECTORALS[0], SECTORALS[1])
    script = {spec.slug: _ok_csv(spec.slug, repo_root) for spec in only_broad_and_two}
    fetcher, l0, _ = build({constituents_url(s): v for s, v in script.items()})
    report = run_constituents_ingest(
        fetcher=fetcher,
        l0=l0,
        tracker=tracker,
        as_of=AS_OF,
        specs=only_broad_and_two,
        data_root=tmp_path,
    )
    assert report.broad_covered == 1
    assert report.sectoral_covered == 2
    assert not report.is_covered


# ── the report renders, and the default set is sane ────────────────────────────────────────────


def test_the_coverage_report_renders_markdown_with_the_verdict(
    build: Any, tracker: RecordingTracker, repo_root: Path, tmp_path: Path
) -> None:
    fetcher, l0, _ = build(_all_ok_script(repo_root))
    report = run_constituents_ingest(
        fetcher=fetcher, l0=l0, tracker=tracker, as_of=AS_OF, specs=SPECS, data_root=tmp_path
    )
    md = render_coverage_markdown(report)
    assert "M10.1" in md
    assert "PASS" in md
    assert "nifty500" in md and "niftyit" in md
    assert AS_OF.isoformat() in md


def test_the_default_index_set_meets_the_coverage_bar_shape() -> None:
    """The shipped default set is a broad list plus enough sectorals to clear the bar."""
    broad = [s for s in DEFAULT_INDEX_SET if s.category.is_broad]
    sectoral = [s for s in DEFAULT_INDEX_SET if not s.category.is_broad]
    assert broad, "the default set names at least one broad list"
    assert len(sectoral) >= 8, "the default set names at least 8 sectoral/thematic lists"
    # Slugs are unique — one name per index end to end.
    slugs = [s.slug for s in DEFAULT_INDEX_SET]
    assert len(slugs) == len(set(slugs))


def test_coverage_report_is_a_frozen_value() -> None:
    """The report is an immutable value object — an outcome cannot be edited after the sweep."""
    report = CoverageReport(as_of=AS_OF, outcomes=())
    with pytest.raises(FrozenInstanceError):
        report.outcomes = ()  # type: ignore[misc]
    assert report.as_of == AS_OF
    assert SlugStatus.PUBLISHED  # the status enum is importable and used
