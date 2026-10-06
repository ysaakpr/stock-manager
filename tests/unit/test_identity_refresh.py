"""The identity master's inputs come from L0, not from `tests/fixtures/` (finding N4).

`security_master` is the authority behind every symbol→ISIN resolution — invariant #2 in table
form — and until the 2026-09-06 audit it was built from two files that lived only under
`tests/fixtures/nse_equity_list/`. There was no `data/L0/nse_equity_list/` tree at all. So the one
table every ISIN join depends on was outside L0's provenance chain, outside its checksums, outside
the backup, and inside a directory a test-hygiene cleanup is entitled to prune. Invariant #1 —
"every L1 value is re-derivable from L0" — did not hold for D2.

These tests pin the closure, and each one fails a plausible wrong implementation:

* a refresh that reads the fixture path instead of L0 (the hole, restated as a fallback);
* a refresh that re-fetches a payload L0 already holds, which on an immutable lake is either a
  wasted request or an `L0ImmutabilityError` on a weekly job's second run;
* a read-back that trusts the bytes the fetch returned rather than re-reading them, which would
  make the master derived from memory rather than from the lake;
* an ingest that keeps going when L0 has nothing, silently leaving yesterday's master in place.

Offline by construction (B8): a `RecordedTransport` scripted with the real frozen fixtures, a
`tmp_path` lake, a `FrozenClock`. No socket is opened.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.ingest import (
    EQUITY_LIST_FILENAME,
    NSE_EQUITY_LIST_SOURCE,
    NSE_SYMBOL_CHANGES_SOURCE,
    SYMBOL_CHANGES_FILENAME,
    L0PayloadMissingError,
    equity_list_filename,
    read_snapshot_from_l0,
    symbol_changes_filename,
)
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
)
from dataplatform.ingest.identity_refresh import IDENTITY_SOURCES, fetch_identity_files
from dataplatform.ingest.source_register import load as load_register
from dataplatform.store.l0 import L0Store

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "nse_equity_list" / "2026-08-08"
SNAPSHOT: Final = date(2026, 8, 8)
NOW: Final = datetime(2026, 8, 8, 7, 0, tzinfo=IST)

EQUITY_URL: Final = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
CHANGES_URL: Final = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"


class _SilentAlerter:
    """An `Alerter` that swallows. Nothing here exercises the 403 path, which has its own suite."""

    def send(self, severity: Any, title: str, body: str, dedup_key: str) -> Any:
        from dataplatform.alerts import AlertOutcome

        return AlertOutcome.SENT


@pytest.fixture
def lake(tmp_path: Path) -> L0Store:
    return L0Store(clock=FrozenClock(NOW), data_root=tmp_path)


def _fetcher(lake: L0Store, **script: Any) -> tuple[Fetcher, RecordedTransport]:
    """A real `Fetcher` over a scripted transport serving the two frozen fixtures."""
    transport = RecordedTransport(
        script
        or {
            EQUITY_URL: RecordedResponse(body=(FIXTURES / EQUITY_LIST_FILENAME).read_bytes()),
            CHANGES_URL: RecordedResponse(body=(FIXTURES / SYMBOL_CHANGES_FILENAME).read_bytes()),
        }
    )
    return (
        Fetcher(
            transport=transport,
            l0=lake,
            alerter=_SilentAlerter(),
            clock=FrozenClock(NOW),
            settings=Settings(data_root=lake.data_root),
            sleep=lambda _seconds: None,
        ),
        transport,
    )


# ── the register knows both files ────────────────────────────────────────────────────────────


def test_both_identity_files_have_a_source_register_row() -> None:
    """`symbolchange.csv` had none, so nothing could fetch it and D7 could not miss it.

    Half of the master's input was invisible to the register, the gap report and the crawl policy
    at once (`ops/BACKLOG.md`, M1.7).
    """
    ids = {entry.id for entry in load_register().sources}

    assert NSE_EQUITY_LIST_SOURCE in ids
    assert NSE_SYMBOL_CHANGES_SOURCE in ids


def test_both_rows_name_the_same_host_so_one_lease_covers_them() -> None:
    by_id = {entry.id: entry for entry in load_register().sources}

    hosts = {by_id[source].host for source in IDENTITY_SOURCES}

    assert hosts == {"nsearchives.nseindia.com"}


# ── fetching into L0, and reading back out of it ─────────────────────────────────────────────


def test_a_refresh_lands_both_files_in_l0(lake: L0Store) -> None:
    fetcher, transport = _fetcher(lake)

    refs, reused = fetch_identity_files(fetcher, SNAPSHOT, l0=lake)

    assert reused == ()
    # Dated L0 keys, not the source's own undated filenames: L0 partitions by month, so
    # `EQUITY_L.csv` twice in one September would be one key and the second put would raise.
    assert [ref.filename for ref in refs] == [
        equity_list_filename(SNAPSHOT),
        symbol_changes_filename(SNAPSHOT),
    ]
    assert lake.exists(NSE_EQUITY_LIST_SOURCE, SNAPSHOT, equity_list_filename(SNAPSHOT))
    assert lake.exists(NSE_SYMBOL_CHANGES_SOURCE, SNAPSHOT, symbol_changes_filename(SNAPSHOT))
    assert len(transport.requests) == 2


def test_a_second_refresh_on_the_same_date_fetches_nothing(lake: L0Store) -> None:
    """A weekly job re-run must be a no-op, not an `L0ImmutabilityError` and not a wasted request.

    L0 is immutable, so re-fetching a key it already holds is at best pointless and at worst a
    failure when the source's bytes have drifted since.
    """
    fetcher, transport = _fetcher(lake)
    fetch_identity_files(fetcher, SNAPSHOT, l0=lake)

    refs, reused = fetch_identity_files(fetcher, SNAPSHOT, l0=lake)

    assert len(refs) == 2
    assert set(reused) == {NSE_EQUITY_LIST_SOURCE, NSE_SYMBOL_CHANGES_SOURCE}
    assert len(transport.requests) == 2, "the second pass must open nothing"


def test_the_master_is_derived_from_the_lake_not_from_the_fetch(lake: L0Store) -> None:
    """`read_snapshot_from_l0` re-reads through `L0Store.get`, which re-hashes on the way out.

    Deriving from whatever the fetch held in memory would make the master the one thing in the
    platform a rebuild-from-L0 could not reproduce — which is exactly the state finding N4 found.
    """
    fetcher, _ = _fetcher(lake)
    fetch_identity_files(fetcher, SNAPSHOT, l0=lake)

    equity_list, changes = read_snapshot_from_l0(SNAPSHOT, store=lake)

    assert equity_list == (FIXTURES / EQUITY_LIST_FILENAME).read_text(encoding="utf-8")
    assert changes == (FIXTURES / SYMBOL_CHANGES_FILENAME).read_text(encoding="utf-8")


def test_reading_from_an_empty_lake_refuses_and_says_how_to_fill_it(lake: L0Store) -> None:
    """No silent fallback to a path on disk. A quiet fallback is how the hole stayed open."""
    with pytest.raises(L0PayloadMissingError) as caught:
        read_snapshot_from_l0(SNAPSHOT, store=lake)

    message = str(caught.value)
    assert NSE_EQUITY_LIST_SOURCE in message
    assert "identity_refresh" in message


def test_a_missing_rename_file_is_refused_even_when_the_equity_list_is_there(
    lake: L0Store,
) -> None:
    """Half an input is not an input. Ingesting with no renames would silently drop the history.

    `symbolchange.csv` is what turns a snapshot of today's listings into a symbol *history*; going
    ahead without it would rebuild the master with every rename chain missing and report success.
    """
    lake.put(
        NSE_EQUITY_LIST_SOURCE,
        SNAPSHOT,
        equity_list_filename(SNAPSHOT),
        (FIXTURES / EQUITY_LIST_FILENAME).read_bytes(),
    )

    with pytest.raises(L0PayloadMissingError, match=NSE_SYMBOL_CHANGES_SOURCE):
        read_snapshot_from_l0(SNAPSHOT, store=lake)


def test_two_captures_in_one_month_are_two_keys_not_one(lake: L0Store) -> None:
    """The bug the dated filenames fix: L0 partitions by month, so an undated name is one key.

    `EQUITY_LIST_FILENAME` was the L0 filename until this test existed, and this job had never
    run — so nobody had discovered that the *second* capture inside any one month would raise
    `L0ImmutabilityError` against the first (different bytes, same
    `L0/nse_equity_list/2026/09/EQUITY_L.csv` key). That is the exact opposite of the register
    row's `pit_notes`: *"keep every weekly snapshot forever and never overwrite; symbol changes
    and delistings are only reconstructible from the accumulated series."* A delisted company
    vanishes from this file the day it dies, so the accumulated series is the only record it was
    ever listed at all.
    """
    first, second = date(2026, 9, 8), date(2026, 9, 15)
    equity = (FIXTURES / EQUITY_LIST_FILENAME).read_bytes()

    lake.put(NSE_EQUITY_LIST_SOURCE, first, equity_list_filename(first), equity)
    lake.put(NSE_EQUITY_LIST_SOURCE, second, equity_list_filename(second), equity + b"\nDRIFTED,,")

    assert equity_list_filename(first) != equity_list_filename(second)
    assert lake.exists(NSE_EQUITY_LIST_SOURCE, first, equity_list_filename(first))
    assert lake.exists(NSE_EQUITY_LIST_SOURCE, second, equity_list_filename(second))


def test_the_dated_filename_keeps_the_sources_own_extension_and_stem() -> None:
    """L0 is what the source served; the date is the only thing added to its name."""
    assert equity_list_filename(date(2026, 9, 8)) == "EQUITY_L_20260908.csv"
    assert symbol_changes_filename(date(2026, 9, 8)) == "symbolchange_20260908.csv"
