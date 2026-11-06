"""DQ-ISIN Phase 2: the one-shot lineage backfill selects, re-keys and fetches, offline.

The network is a `RecordedTransport` over the captured XBRL fixtures (`tests/fixtures/xbrl/`); the
captured index slice really lists HDFCBANK's Dec-2024 quarter (filed 2025-01-23) under its
original ISIN `INE040A01018`, which is the whole defect. `sync_state` is the backfill tests'
in-memory stand-in. No socket, no Postgres.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, cast

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.filing_isin import FilingEdge, FilingIsinResolver
from dataplatform.ingest import fundamentals_backfill as fb
from dataplatform.ingest import fundamentals_lineage_backfill as lb
from dataplatform.ingest.xbrl import discovery
from dataplatform.store.l0 import L0Store
from dataplatform.store.pit_fundamentals import read_l1
from tests.unit.test_fundamentals_backfill import (
    INDEX_FIXTURE,
    _FakeSync,
    _fetcher,
    _ok_transport,
    _quarterly_plan,
)

HDFC_1: Final = "INE040A01018"  # what the index states
HDFC_2: Final = "INE040A01026"  # 2011-07-15 .. 2019-09-19
HDFC_3: Final = "INE040A01034"  # live since 2019-09-20
VST_STATED: Final = "INE764D01017"
VST_NEXT: Final = "INE764D01025"  # a made-up reissue: VST is not on the list
TODAY: Final = date(2026, 11, 9)
#: The fixture's filings run to 2026; the real cut-off (2025-01-01) is tested on its own below.
BEFORE: Final = date(2027, 1, 1)


def _edge(p: str, s: str, eff: date) -> FilingEdge:
    return FilingEdge(p, s, eff, "L1_CONTIGUITY", "DERIVED", 0)


RESOLVER: Final = FilingIsinResolver(
    [
        _edge(HDFC_1, HDFC_2, date(2011, 7, 15)),
        _edge(HDFC_2, HDFC_3, date(2019, 9, 20)),
        _edge(VST_STATED, VST_NEXT, date(2020, 1, 1)),
    ]
)
NAMES: Final = {HDFC_3: "HDFCBANK"}


def _entries() -> tuple[discovery.FilingIndexEntry, ...]:
    return discovery.parse_index(INDEX_FIXTURE.read_bytes(), filename=INDEX_FIXTURE.name)


def test_the_checked_in_list_is_the_91_sized_names() -> None:
    names = lb.load_names()
    assert len(names) == 91
    assert names[HDFC_3] == "HDFCBANK"
    assert HDFC_1 not in names  # the list holds live ISINs, never a stated stale one


def test_a_stale_entry_maps_forward_to_its_filing_dates_isin_never_backward() -> None:
    """Fails if the lineage direction is inverted: the stated predecessor is re-keyed forward, and
    an entry already on the live ISIN is not this job's and is never moved back."""
    entries = _entries()
    chosen = lb.select_entries(entries, RESOLVER, NAMES, as_of=TODAY, before=BEFORE)
    assert {(e.symbol, e.isin) for e in chosen.entries} == {("HDFCBANK", HDFC_3)}
    assert chosen.names == {HDFC_3}

    on_live = [e.model_copy(update={"isin": HDFC_3}) for e in entries if e.isin == HDFC_1]
    again = lb.select_entries(on_live, RESOLVER, NAMES, as_of=TODAY, before=BEFORE)
    assert again.entries == ()
    assert again.skipped == {"current_isin": len(on_live)}


def test_a_filing_before_the_second_reissue_is_keyed_to_the_isin_live_then() -> None:
    (hdfc,) = {e.filing_id: e for e in _entries() if e.isin == HDFC_1}.values()
    in_2018 = hdfc.model_copy(update={"filing_date": date(2018, 10, 20)})
    chosen = lb.select_entries([in_2018], RESOLVER, NAMES, as_of=TODAY, before=BEFORE)
    assert [e.isin for e in chosen.entries] == [HDFC_2]


def test_a_name_outside_the_list_is_refused_and_never_fetched(tmp_path: Path) -> None:
    entries = _entries()
    chosen = lb.select_entries(entries, RESOLVER, NAMES, as_of=TODAY, before=BEFORE)
    vst = [e for e in entries if e.isin == VST_STATED]
    assert vst and chosen.refused_outside_list >= len(vst)
    assert VST_STATED in chosen.outside_isins
    assert not any(e.symbol == "VSTTILLERS" for e in chosen.entries)

    transport = _ok_transport(_quarterly_plan())
    runner = _runner(tmp_path, transport, _FakeSync(), chosen)
    report = runner.ingest(chosen.entries)
    fetched = {r.url for r in transport.requests}
    vst_docs = {e.xbrl_url for e in vst}
    assert not fetched & vst_docs
    assert report.filings_published == 1


def test_the_old_index_cut_off_leaves_forward_feed_filings_alone() -> None:
    chosen = lb.select_entries(_entries(), RESOLVER, NAMES, as_of=TODAY)
    assert chosen.entries == ()  # HDFCBANK's slice entry is filed 2025-01-23, the forward feed's
    assert chosen.skipped["after_old_index"] >= 1


def _runner(
    root: Path, transport: Any, sync: _FakeSync, chosen: lb.Selection
) -> fb.FundamentalsBackfillRunner:
    settings = Settings(data_root=root)
    clock = FrozenClock(datetime(2026, 11, 9, 11, 0, tzinfo=IST))
    return fb.FundamentalsBackfillRunner(
        fetcher=_fetcher(transport, settings),
        l0=L0Store(clock=clock, data_root=root),
        sync=cast("Any", sync),
        universe=set(chosen.names),
        commit=lambda: None,
        data_root=root,
    )


def test_a_second_run_fetches_nothing_already_published(tmp_path: Path) -> None:
    """Idempotent: the per-filing `sync_state` row, not the re-key, is what resume reads."""
    chosen = lb.select_entries(_entries(), RESOLVER, NAMES, as_of=TODAY, before=BEFORE)
    sync = _FakeSync()
    first = _runner(tmp_path, _ok_transport(_quarterly_plan()), sync, chosen).ingest(chosen.entries)
    assert (first.filings_published, first.filings_failed) == (1, 0)
    facts = read_l1(date(2025, 1, 23), data_root=tmp_path)
    assert facts and {f.isin for f in facts} == {HDFC_3}

    transport = _ok_transport(_quarterly_plan())
    second = _runner(tmp_path, transport, sync, chosen).ingest(chosen.entries)
    assert (second.filings_published, second.filings_skipped_published) == (0, 1)
    assert not [r for r in transport.requests if "/corporate/xbrl/" in r.url]


def test_the_job_does_nothing_off_its_run_days() -> None:
    holiday = FrozenClock(datetime(2026, 11, 10, 11, 0, tzinfo=IST))
    assert lb.run_fundamentals_lineage_backfill(Settings(), holiday) is None
    late = FrozenClock(datetime(2026, 11, 9, 17, 45, tzinfo=IST))
    assert lb.run_fundamentals_lineage_backfill(Settings(), late) is None
