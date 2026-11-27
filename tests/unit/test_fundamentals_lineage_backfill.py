"""DQ-ISIN Phase 2: the one-shot lineage backfill selects, re-keys and fetches, offline.

The network is a `RecordedTransport` over the captured XBRL fixtures (`tests/fixtures/xbrl/`); the
captured index slice really lists HDFCBANK's Dec-2024 quarter (filed 2025-01-23) under its
original ISIN `INE040A01018`, which is the whole defect. `sync_state` is the backfill tests'
in-memory stand-in. No socket, no Postgres.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final, cast

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.filing_isin import (
    FilingEdge,
    FilingIsinResolver,
    load_filing_aliases,
)
from dataplatform.identity.link_filing_history import load_pre_era_links
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


# --- Second batch (M18.1 follow-up, owner go 2026-11-27, D46): the 26 names linked on 2026-11-25.

SUN_OLD: Final = "INE044A01028"  # SUNPHARMA as filed; MANUAL edge, pre_era_lineage.yaml
SUN_LIVE: Final = "INE044A01036"
HUDCO_BOND: Final = "INE031A07873"  # HUDCO files under this debenture (filing_aliases.yaml)
HUDCO_EQUITY: Final = "INE031A01017"
HUDCO_FIRST_SESSION: Final = date(2017, 5, 19)
SECOND_DAY: Final = date(2026, 11, 30)

#: The 26 live ISINs, pinned: 23 MANUAL-edge names and 3 bond-alias names. TATACAP is absent (the
#: old index names nothing for it: it listed 2025-10-13).
SECOND_BATCH: Final = frozenset(
    {
        "INE006I01046", "INE031A01017", "INE044A01036", "INE053F01010", "INE115A01026",
        "INE123F01029", "INE126A01031", "INE139A01034", "INE169A01031", "INE176B01034",
        "INE195A01028", "INE202E01016", "INE213A01029", "INE216A01030", "INE237A01036",
        "INE267A01025", "INE326A01037", "INE367G01038", "INE389H01022", "INE406A01037",
        "INE437A01024", "INE494B01023", "INE510A01028", "INE548C01032", "INE787D01026",
        "INE891D01026",
    }
)  # fmt: skip


def _second_resolver() -> FilingIsinResolver:
    edges = [
        _edge(HDFC_1, HDFC_2, date(2011, 7, 15)),
        _edge(HDFC_2, HDFC_3, date(2019, 9, 20)),
        FilingEdge(SUN_OLD, SUN_LIVE, date(2010, 12, 2), "MANUAL", "DERIVED", 0),
    ]
    hudco = next(a for a in load_filing_aliases() if a.symbol == "HUDCO")
    assert (hudco.filed_isin, hudco.isin, hudco.effective_date) == (
        HUDCO_BOND,
        HUDCO_EQUITY,
        HUDCO_FIRST_SESSION,
    )
    return FilingIsinResolver(edges, [hudco])


def _hdfc_entry() -> discovery.FilingIndexEntry:
    (hdfc,) = {e.filing_id: e for e in _entries() if e.isin == HDFC_1}.values()
    return hdfc


def _as(isin: str, symbol: str, filed: date, seq: str) -> discovery.FilingIndexEntry:
    return _hdfc_entry().model_copy(
        update={"isin": isin, "symbol": symbol, "filing_date": filed, "seq_number": seq}
    )


def test_the_second_batch_is_the_26_linked_names_and_no_other() -> None:
    second = lb.load_names(batch="second_batch")
    assert frozenset(second) == SECOND_BATCH
    assert "INE976I01016" not in second  # TATACAP
    assert not frozenset(second) & frozenset(lb.load_names())
    # Every M18.1 link and bond alias names one of these (or TATACAP), never another name.
    symbols = set(second.values())
    linked = {link.symbol for link in load_pre_era_links()} | {
        a.symbol for a in load_filing_aliases()
    }
    assert linked - symbols == {"TATACAP"}
    assert symbols <= linked
    for alias in load_filing_aliases():
        if alias.symbol != "TATACAP":
            assert second[alias.isin] == alias.symbol


def test_a_name_listed_in_both_batches_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "names.yaml"
    path.write_text(
        "version: 1\n"
        f"names:\n  - {{isin: {HDFC_3}, symbol: HDFCBANK}}\n"
        f"second_batch:\n  - {{isin: {HDFC_3}, symbol: HDFCBANK}}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="listed twice"):
        lb.load_names(path, batch="second_batch")


def test_each_run_day_drives_its_own_batch() -> None:
    assert {
        date(2026, 11, 9),
        date(2026, 11, 11),
        date(2026, 11, 30),
        date(2026, 12, 1),
    } == lb.RUN_DAYS
    assert {d: lb.BATCHES[d] for d in sorted(lb.RUN_DAYS)} == {
        date(2026, 11, 9): "names",
        date(2026, 11, 11): "names",
        date(2026, 11, 30): "second_batch",
        date(2026, 12, 1): "second_batch",
    }


def test_the_second_batch_takes_its_names_and_nothing_else() -> None:
    """A MANUAL-edge name and a bond-alias name are in scope on 30 Nov; a first-batch one is not."""
    entries = [
        _as(SUN_OLD, "SUNPHARMA", date(2019, 5, 28), "S1"),
        _as(HUDCO_BOND, "HUDCO", date(2019, 8, 13), "H1"),
        _as(HDFC_1, "HDFCBANK", date(2018, 10, 20), "D1"),
    ]
    names = lb.load_names(batch="second_batch")
    chosen = lb.select_entries(entries, _second_resolver(), names, as_of=SECOND_DAY)
    assert {(e.symbol, e.isin) for e in chosen.entries} == {
        ("SUNPHARMA", SUN_LIVE),
        ("HUDCO", HUDCO_EQUITY),
    }
    assert chosen.refused_outside_list == 1
    assert chosen.outside_isins == {HDFC_1}


def test_a_bond_id_entry_keys_to_the_equity_only_from_its_first_session() -> None:
    """Fails if the alias is walked before the equity existed, or if a pre-equity bond entry is
    fetched under the bond ISIN: it is skipped and counted `before_equity`."""
    names = lb.load_names(batch="second_batch")
    on_day = _as(HUDCO_BOND, "HUDCO", HUDCO_FIRST_SESSION, "H0")
    before = _as(HUDCO_BOND, "HUDCO", date(2017, 5, 18), "HB")
    chosen = lb.select_entries([on_day, before], _second_resolver(), names, as_of=SECOND_DAY)
    assert [(e.seq_number, e.isin) for e in chosen.entries] == [("H0", HUDCO_EQUITY)]
    assert chosen.skipped == {"before_equity": 1}
    assert not any(e.isin == HUDCO_BOND for e in chosen.entries)


def test_a_pre_reissue_equity_filing_still_keys_to_the_equity_live_then() -> None:
    """The `before_equity` skip is about debentures only: a filing before a reissue stays on the
    predecessor equity ISIN, which is the PIT key, and is driven."""
    names = lb.load_names(batch="second_batch")
    early = _as(SUN_OLD, "SUNPHARMA", date(2010, 11, 1), "S0")
    chosen = lb.select_entries([early], _second_resolver(), names, as_of=SECOND_DAY)
    assert [e.isin for e in chosen.entries] == [SUN_OLD]


@pytest.mark.parametrize(
    "day",
    [date(2026, 11, 28), date(2026, 11, 29), date(2026, 12, 2), date(2027, 11, 30)],
)
def test_the_job_refuses_a_day_outside_its_run_days(
    day: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if any of these days were allowed. The first step past the day check is a tripwire, so
    an allowed day fails here before it can reach L0, a lease or the store."""
    _tripwire(monkeypatch)
    clock = FrozenClock(datetime.combine(day, time(10, 0), IST))
    assert lb.run_fundamentals_lineage_backfill(Settings(), clock) is None


def test_every_other_day_of_the_two_years_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    _tripwire(monkeypatch)
    day = date(2026, 1, 1)
    while day < date(2028, 1, 1):
        if day not in lb.RUN_DAYS:
            clock = FrozenClock(datetime.combine(day, time(10, 0), IST))
            assert lb.run_fundamentals_lineage_backfill(Settings(), clock) is None, day
        day += timedelta(days=1)


def _tripwire(monkeypatch: pytest.MonkeyPatch) -> None:
    def tripped(*_: object, **__: object) -> None:
        raise AssertionError("the run went past its day check")

    monkeypatch.setattr(lb, "load_names", tripped)
