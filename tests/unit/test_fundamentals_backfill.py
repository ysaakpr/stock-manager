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

import shutil
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
from dataplatform.ingest.xbrl import FundamentalFact
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

#: The captured index slice holds 28 real announcements — 27 with an XBRL document and one with
#: none. `VSTTILLERS` accounts for four (two natures x an original filing and its correction) and
#: `SCHAEFFLER` for four (two natures x a quarterly and an annual entry over the same documents);
#: the rest span the older format eras (`tests/fixtures/xbrl/README.md`).
VSTTILLERS: Final = "INE764D01017"
SCHAEFFLER: Final = "INE513A01014"
VIDEOIND: Final = "INE703A01011"  # the announcement with no XBRL document
#: The older format eras (`tests/fixtures/xbrl/README.md`).
ALBK: Final = "INE428A01015"
MCL: Final = "INE813V01014"
TARACHAND: Final = "INE555Z01012"
JKBANK: Final = "INE168A01017"
HEALTHX: Final = "INE019J01013"
ENTRIES_IN_SLICE: Final = 28
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
    assert "Entries skipped (ISIN not in universe): 20" in rendered
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


def test_the_older_format_eras_ingest_end_to_end(tmp_path: Path) -> None:
    """The runner drives the pre-2023 documents through fetch -> L0 -> parse -> write_pit too.

    The unit tests above all use filings broadcast in 2025-2026. These are the generations before
    them — undeclared column contexts, no contexts at all, the Non-Ind-AS vocabulary, a BSE scrip
    code for an identifier — and they are the bulk of a ten-year backfill, so the runner must be
    exercised on them and not only the parser.
    """
    from decimal import Decimal

    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    era = {ALBK, MCL, TARACHAND, JKBANK}
    report = _runner(_ok_transport(plan), settings=settings, sync=_FakeSync(), universe=era).run(
        plan
    )

    assert report.filings_published == len(era) + 1  # J&K Bank is named by two entries
    assert report.filings_failed == 0
    assert not report.parked
    assert report.covered_isins == era

    stored = read_latest(date(2026, 9, 1), data_root=settings.data_root)
    # An old banking `_WEB` filing whose column contexts are never declared, and whose header block
    # sits on the zero-filled `OneD`: the real loss must land, not a zero.
    assert _one(stored, ALBK, "profit_after_tax", date(2019, 3, 31), nature="Consolidated") == (
        Decimal("-84573800000.00")
    )
    # A document with no `<context>` whatever, identified only by its `Symbol` fact.
    assert _one(stored, MCL, "revenue_from_operations", date(2020, 3, 31)) == (
        Decimal("2022783000.00")
    )
    # The Non-Ind-AS vocabulary, where total income is `Revenue`.
    assert _one(stored, TARACHAND, "total_income", date(2023, 3, 31)) == Decimal("1445608000.00")
    # A BSE scrip code for an identifier, and one document answering both its entries.
    jk_periods = {
        (f.period_start, f.period_end)
        for f in stored
        if f.isin == JKBANK and f.concept == "eps_basic"
    }
    assert jk_periods == {
        (date(2021, 4, 1), date(2022, 3, 31)),
        (date(2022, 1, 1), date(2022, 3, 31)),
    }


def test_a_renamed_company_ingests_via_the_injected_symbol_history(tmp_path: Path) -> None:
    """The runner passes D2's symbol history, which is what lets a renamed company's filing land.

    Sastasundar Ventures files as `SASTASUNDR` under an index entry that now says `HEALTHX`.
    Without the history the parser cannot tell that from a document about the wrong company, so it
    refuses — correctly. With it, the filing lands under the index's ISIN, which the rename never
    touched.
    """
    from decimal import Decimal

    settings = _settings(tmp_path)
    plan = _quarterly_plan()

    without = _runner(
        _ok_transport(plan), settings=settings, sync=_FakeSync(), universe={HEALTHX}
    ).run(plan)
    assert without.filings_published == 0
    assert without.filings_failed == 2  # both of its entries, refused on identity
    assert any("name the same company" in message for _label, message in without.failures)

    with_history = fb.FundamentalsBackfillRunner(
        fetcher=_fetcher(_ok_transport(plan), settings),
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        sync=cast("Any", _FakeSync()),
        universe={HEALTHX},
        commit=lambda: None,
        data_root=settings.data_root,
        symbol_history=lambda isin: frozenset({"SASTASUNDR"}) if isin == HEALTHX else frozenset(),
    ).run(plan)

    assert with_history.filings_published == 2
    assert with_history.filings_failed == 0
    stored = read_latest(date(2026, 9, 1), data_root=settings.data_root)
    # Its annual and its fourth-quarter entries share an end date and a nature, so both records
    # survive `read_latest` only because `period_start` is part of its collapse key.
    assert _one(
        stored,
        HEALTHX,
        "profit_after_tax",
        date(2023, 3, 31),
        nature="Consolidated",
        period_start=date(2022, 4, 1),
    ) == Decimal("-994692000.00")
    assert _one(
        stored,
        HEALTHX,
        "profit_after_tax",
        date(2023, 3, 31),
        nature="Consolidated",
        period_start=date(2023, 1, 1),
    ) == Decimal("-480010000.00")
    # The join key is the index's ISIN throughout; the document's own symbol is only a cross-check.
    assert all(f.isin == HEALTHX for f in stored)


def test_the_two_periods_of_one_chunk_do_not_share_a_checkpoint() -> None:
    """The Quarterly and Annual chunks of one date range are two units, keyed apart.

    They share a start date (the `logical_date`) and differ only in period, so a checkpoint keyed
    on the register id plus the start date collapses them into one row. The consequence is not a
    lost checkpoint but lost *data*: publishing the Quarterly chunk makes the Annual chunk
    resume-skip, and the resume path then reads back the Quarterly filename's payload or none at
    all — every Annual filing of the backfill silently gone.
    """
    plan = fb.build_index_units(FROM, TO, register=load_register(), chunk_months=12)
    quarterly = next(u for u in plan if u.period is fb.Period.QUARTERLY)
    annual = next(u for u in plan if u.period is fb.Period.ANNUAL)

    assert (quarterly.logical_date, quarterly.from_date, quarterly.to_date) == (
        annual.logical_date,
        annual.from_date,
        annual.to_date,
    )
    assert quarterly.state_source != annual.state_source
    assert (quarterly.state_source, quarterly.logical_date) != (
        annual.state_source,
        annual.logical_date,
    )
    # Every unit in a full plan is distinctly keyed, whatever the chunking.
    for months in (1, 3, 12):
        units = fb.build_index_units(FROM, TO, register=load_register(), chunk_months=months)
        keys = [(u.state_source, u.logical_date) for u in units]
        assert len(set(keys)) == len(units), months


def test_a_chunk_whose_range_changed_is_not_considered_fetched() -> None:
    """Re-chunking changes which payload a unit needs, so its checkpoint must not carry over.

    Two runs can produce a chunk with the same period and start date but a different end — a
    different `--to`, or a different `--chunk-months`. The second covers filings the first never
    fetched, so treating it as published would skip a range and then look for a payload under a
    filename nothing ever wrote.
    """
    register = load_register()
    short = fb.build_index_units(FROM, date(2026, 6, 30), register=register, chunk_months=12)
    long = fb.build_index_units(FROM, TO, register=register, chunk_months=12)
    first_short = next(u for u in short if u.period is fb.Period.QUARTERLY)
    first_long = next(u for u in long if u.period is fb.Period.QUARTERLY)

    assert first_short.logical_date == first_long.logical_date
    assert first_short.filename != first_long.filename
    assert first_short.state_source != first_long.state_source


def test_the_checkpoint_key_tracks_the_payload_filename() -> None:
    """One state key ↔ one L0 filename. The resume path reads the payload by filename, so any
    two units that differ in filename must differ in key, or a resume reads the wrong bytes."""
    units = fb.build_index_units(FROM, TO, register=load_register(), chunk_months=3)
    by_key: dict[tuple[str, date], str] = {}
    for unit in units:
        key = (unit.state_source, unit.logical_date)
        assert by_key.setdefault(key, unit.filename) == unit.filename, key


def test_a_published_chunk_whose_l0_payload_is_gone_is_reported_not_a_crash(
    tmp_path: Path,
) -> None:
    """A resume whose payload vanished must surface the chunk and continue.

    `L0Store.ref_for` raises `L0NotFoundError`, which is **not** a `FileNotFoundError` — catching
    only the builtin let a decade-long resume die on the single case this handler exists for. The
    chunk is counted as failed and named in the report, never swallowed.
    """
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    sync = _FakeSync()
    # Mark every chunk published without ever writing its L0 payload.
    for unit in plan:
        sync.begin(unit.state_source, unit.logical_date)
        sync.mark_published(unit.state_source, unit.logical_date)

    transport = _ok_transport(plan)
    report = _runner(transport, settings=settings, sync=sync, universe={VSTTILLERS}).run(plan)

    assert not report.parked
    assert report.index_skipped_published == len(plan)
    assert report.index_failed == len(plan)
    assert report.filings_discovered == 0
    assert transport.requests == []  # a resume opens no socket, even a broken one
    rendered = fb.render_report(from_date=FROM, to_date=TO, universe_size=1, report=report)
    assert "index reparse failed" in rendered


def test_a_bounded_re_run_spends_its_cap_on_work_not_on_resume_skips(tmp_path: Path) -> None:
    """`--max-filings` counts attempts, so a bounded resume makes progress instead of marking time.

    Charging a resume-skip to the cap is the difference between a bounded re-run doing something
    and doing nothing: after a first pass published four filings, a second `--max-filings 4` run
    would spend its whole budget re-confirming those four and never reach the rest — which is
    exactly the case an operator hits when restarting a large run.
    """
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    sync = _FakeSync()
    universe = {VSTTILLERS, SCHAEFFLER}  # eight entries between them

    first = _runner(_ok_transport(plan), settings=settings, sync=sync, universe=universe).run(plan)
    assert first.filings_published == 8

    # Everything is published; a capped re-run attempts nothing and reports every unit resumed.
    second = fb.FundamentalsBackfillRunner(
        fetcher=_fetcher(_ok_transport(plan), settings),
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        sync=cast("Any", sync),
        universe=universe,
        commit=lambda: None,
        data_root=settings.data_root,
        max_filings=4,
    ).run(plan)
    assert second.filings_published == 0
    assert second.filings_skipped_published == 8  # all eight, not the first four


def test_a_bounded_run_resumes_where_it_left_off(tmp_path: Path) -> None:
    """Two capped runs cover what one uncapped run would, rather than redoing the first batch."""
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    sync = _FakeSync()
    universe = {VSTTILLERS, SCHAEFFLER}

    first = fb.FundamentalsBackfillRunner(
        fetcher=_fetcher(_ok_transport(plan), settings),
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        sync=cast("Any", sync),
        universe=universe,
        commit=lambda: None,
        data_root=settings.data_root,
        max_filings=3,
    ).run(plan)
    assert first.filings_published == 3

    second = fb.FundamentalsBackfillRunner(
        fetcher=_fetcher(_ok_transport(plan), settings),
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        sync=cast("Any", sync),
        universe=universe,
        commit=lambda: None,
        data_root=settings.data_root,
        max_filings=3,
    ).run(plan)
    assert second.filings_skipped_published == 3  # the first batch, confirmed and not re-fetched
    assert second.filings_published == 3  # and three *new* ones, not a repeat of the first three


def test_a_failed_filing_is_retried_by_the_next_run(tmp_path: Path) -> None:
    """Only `PUBLISHED` is skipped, so a failure heals on the next run without editing state.

    That is the point of recording `retryable` as a report label rather than a resume gate (the
    price and corporate-action backfills do the same): a parser that has since been fixed, or a
    host that has stopped refusing, should be picked up by simply running the command again.
    """
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    sync = _FakeSync()

    # First pass: every XBRL fetch 404s, so every filing lands FAILED (and no 403 spike trips).
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
        script[f"https://nsearchives.nseindia.com/corporate/xbrl/{xml.name}"] = RecordedResponse(
            status_code=404, body=b"not found"
        )
    broken = _runner(
        RecordedTransport(cast("Any", script)), settings=settings, sync=sync, universe={VSTTILLERS}
    ).run(plan)
    assert broken.filings_published == 0
    assert broken.filings_failed == 4
    assert not broken.parked

    # Second pass over the same checkpoint, with the host healthy: the failures are re-attempted.
    healed = _runner(_ok_transport(plan), settings=settings, sync=sync, universe={VSTTILLERS}).run(
        plan
    )
    assert healed.filings_published == 4
    assert healed.filings_failed == 0
    assert healed.filings_skipped_published == 0  # nothing had been published to skip


def test_a_document_named_by_two_entries_is_fetched_once(tmp_path: Path) -> None:
    """Two entries over one document cost one download and still produce two filings.

    Schaeffler's document is named by both a Quarterly and an Annual entry on the same broadcast
    date, so the second entry's payload is already in L0. Across the ten-year index this is a fifth
    of the campaign's requests. The *parse* still runs per entry — the entry selects the results
    column, so the two yield different periods — which is why this skips the download and not the
    work.
    """
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    transport = _ok_transport(plan)
    report = _runner(transport, settings=settings, sync=_FakeSync(), universe={SCHAEFFLER}).run(
        plan
    )

    assert report.filings_published == 4  # two natures x (quarterly, annual)
    assert report.filings_l0_reused == 2  # one per nature: the second entry over each document
    archive = [r.url for r in transport.requests if "nsearchives" in r.url]
    assert len(archive) == 2  # two documents, two downloads — not four
    assert len(set(archive)) == 2

    rendered = fb.render_report(from_date=FROM, to_date=TO, universe_size=1, report=report)
    assert "Filings whose L0 payload was reused (no re-fetch): 2" in rendered


def test_a_reused_payload_yields_the_same_facts_as_a_fetch(tmp_path: Path) -> None:
    """Reuse must be indistinguishable from a fetch, or it is a correctness hole not a saving."""
    from decimal import Decimal

    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    _runner(_ok_transport(plan), settings=settings, sync=_FakeSync(), universe={SCHAEFFLER}).run(
        plan
    )

    stored = read_latest(date(2026, 9, 1), data_root=settings.data_root)
    # The annual entry's filing is the one whose payload was reused (it sorts after the quarterly).
    assert _one(
        stored,
        SCHAEFFLER,
        "revenue_from_operations",
        date(2024, 12, 31),
        nature="Consolidated",
        period_start=date(2024, 1, 1),
    ) == Decimal("82323800000.00")
    assert _one(
        stored,
        SCHAEFFLER,
        "revenue_from_operations",
        date(2024, 12, 31),
        nature="Consolidated",
        period_start=date(2024, 10, 1),
    ) == Decimal("21360600000.00")


class _RefusingTransport:
    """A transport that turns any request into a failure, so "offline" is enforced not assumed."""

    def __init__(self, on_request: object) -> None:
        self._on_request = on_request

    def request(self, *args: object, **kwargs: object) -> object:
        return self._on_request(*args, **kwargs)  # type: ignore[operator]


def test_a_rebuild_from_l0_opens_no_socket_and_reproduces_the_store(tmp_path: Path) -> None:
    """The whole point of a portable campaign: fetch anywhere, rebuild at home with no network.

    A first run populates L0 and L1. A second run over a *fresh* lake root — sharing only the L0
    tree — must reproduce exactly the same facts while making zero requests, index chunks included.
    That is what lets the fetching happen on one machine and the store be rebuilt on another from
    nothing but the archive.
    """
    settings = _settings(tmp_path)
    plan = _quarterly_plan()
    universe = {VSTTILLERS, SCHAEFFLER}

    online = _runner(
        _ok_transport(plan), settings=settings, sync=_FakeSync(), universe=universe
    ).run(plan)
    assert online.filings_published == 8
    before = read_latest(date(2026, 9, 1), data_root=settings.data_root)
    assert before

    # A second lake that shares L0 and has no L1 and no checkpoints — a transferred archive.
    fresh = tmp_path / "rebuilt"
    (fresh / "L0").parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(settings.data_root / "L0", fresh / "L0")
    rebuilt_settings = settings.model_copy(update={"data_root": fresh})

    # A transport that fails on any request at all, so "no socket" is enforced, not assumed.
    def forbidden(*_a: object, **_k: object) -> object:
        raise AssertionError("a rebuild-from-l0 run attempted a network request")

    offline = fb.FundamentalsBackfillRunner(
        fetcher=_fetcher(cast("Any", _RefusingTransport(forbidden)), rebuilt_settings),
        l0=L0Store(clock=CLOCK, data_root=fresh),
        sync=cast("Any", _FakeSync()),
        universe=universe,
        commit=lambda: None,
        data_root=fresh,
        rebuild_from_l0=True,
    ).run(plan)

    assert offline.filings_published == 8
    assert offline.filings_failed == 0
    assert not offline.parked
    after = read_latest(date(2026, 9, 1), data_root=fresh)

    def key(f: FundamentalFact) -> tuple[object, ...]:
        return (f.isin, f.period_start, f.period_end, f.nature, f.concept, f.segment)

    assert {key(f): f.value for f in after} == {key(f): f.value for f in before}


def test_a_rebuild_fails_loudly_on_an_incomplete_lake(tmp_path: Path) -> None:
    """A rebuild must never quietly reach for the network to paper over a missing payload.

    Otherwise a store rebuilt from a partial archive looks identical to one rebuilt from a whole
    archive, and nobody can reproduce it from what they were handed.
    """
    settings = _settings(tmp_path)
    plan = _quarterly_plan()

    def forbidden(*_a: object, **_k: object) -> object:
        raise AssertionError("a rebuild-from-l0 run attempted a network request")

    report = fb.FundamentalsBackfillRunner(
        fetcher=_fetcher(cast("Any", _RefusingTransport(forbidden)), settings),
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        sync=cast("Any", _FakeSync()),
        universe={VSTTILLERS},
        commit=lambda: None,
        data_root=settings.data_root,
        rebuild_from_l0=True,
    ).run(plan)

    # Nothing in L0 at all, so every index chunk is unrebuildable — recorded, not fetched.
    assert report.index_published == 0
    assert report.index_failed == len(plan)
    assert report.filings_published == 0
    assert any("rebuild-from-l0 forbids fetching" in message for _l, message in report.failures)


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
