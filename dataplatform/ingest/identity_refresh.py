"""D1: fetch the two identity files into L0, then re-derive the master from them (finding N4).

`security_master`, `symbol_history` and `exchange_listing` are the authority behind every
symbol→ISIN resolution in the platform — invariant #2 in table form. As of the 2026-09-06 audit
they had been built from:

    tests/fixtures/nse_equity_list/2026-08-08/EQUITY_L.csv
    tests/fixtures/nse_equity_list/2026-08-08/symbolchange.csv

There was no `data/L0/nse_equity_list/` tree. The register row for `nse_equity_list` said VERIFIED
and had never been fetched; `symbolchange.csv` had no register row at all. `ops/runbooks/
identity-master.md` says *"D1 fetches them into L0; this command reads files off disk"* — the first
clause had never happened, and the fixtures the runbook calls "frozen copies … to reproduce a parse
failure offline" were quietly the production input.

Three consequences, all of them holes under the same table:

* **Invariant #1 did not hold for D2.** The identity layer could not be re-derived from L0, because
  its input was not in L0. Every ISIN in `prices_raw` and every corporate-action resolution
  ultimately depended on a file outside the lake's provenance chain.
* Not covered by `L0Store` checksums, so damage to it was undetectable — and now unavoidable to
  detect, since the weekly sweep reaches it (`store/l0_verify.py`).
* Not covered by `ops/backup.sh`, which manifests L0 only, and living in a directory a test-hygiene
  cleanup is entitled to prune.

This module is the missing D1 half: two requests, both under a host lease, both landing in L0 with
their checksums, and then `identity.ingest` reading them back out. The fixtures stay fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.identity.ingest import (
    NSE_EQUITY_LIST_SOURCE,
    NSE_SYMBOL_CHANGES_SOURCE,
    IdentityIngestReport,
    identity_l0_files,
    ingest_snapshot,
    read_snapshot_from_l0,
)
from dataplatform.ingest.fetcher import Fetcher, leased_fetcher
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection, connection
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = ["IDENTITY_SOURCES", "IdentityRefreshReport", "fetch_identity_files", "refresh_identity"]

_LOG = get_logger(__name__)

#: The two register ids this refresh fetches. The L0 *filename* each lands under is a function of
#: the capture date (`identity_l0_files`), not a constant — L0 partitions by month, so an undated
#: filename would make two captures in one month collide on one key.
IDENTITY_SOURCES: Final[tuple[str, ...]] = (NSE_EQUITY_LIST_SOURCE, NSE_SYMBOL_CHANGES_SOURCE)


@dataclass(frozen=True, slots=True)
class IdentityRefreshReport:
    """What one refresh fetched and what the ingest made of it."""

    snapshot_date: date
    refs: tuple[L0Ref, ...]
    reused: tuple[str, ...]
    ingest: IdentityIngestReport

    def summary(self) -> str:
        """One line for a log or a runbook."""
        reused = f", {len(self.reused)} already in L0" if self.reused else ""
        return (
            f"identity refresh {self.snapshot_date.isoformat()}: "
            f"{len(self.refs)} file(s) in L0{reused}, "
            f"{self.ingest.counts.securities} securities changed, "
            f"{self.ingest.counts.windows_inserted} windows inserted"
        )


def fetch_identity_files(
    fetcher: Fetcher,
    snapshot_date: date,
    *,
    l0: L0Store,
    register: SourceRegister | None = None,
) -> tuple[tuple[L0Ref, ...], tuple[str, ...]]:
    """Land both identity files in L0 for `snapshot_date`. Returns the refs and what was reused.

    What it does: fetches each source's register URL through D1's one door, so both files arrive
    checksummed, immutable and inside the provenance chain every other ingested byte lives in.
    What it assumes: the caller holds this host's request budget. Both files come from
    `nsearchives.nseindia.com`, the host every campaign uses.
    What it never does: re-fetch a payload L0 already holds for the date. L0 is immutable, so a
    second fetch of the same key either wastes a request or raises `L0ImmutabilityError` when the
    source's bytes have drifted — and a weekly refresh re-run on the same day is a no-op, not a
    failure.
    """
    loaded = load_register() if register is None else register
    by_id = {entry.id: entry for entry in loaded.sources}
    refs: list[L0Ref] = []
    reused: list[str] = []
    for source, filename in identity_l0_files(snapshot_date):
        entry = by_id.get(source)
        if entry is None:
            raise KeyError(f"source register has no row for {source!r}")
        if l0.exists(source, snapshot_date, filename):
            refs.append(l0.ref_for(source, snapshot_date, filename))
            reused.append(source)
            _LOG.info(
                "identity.refresh_reused_l0",
                source=source,
                snapshot_date=snapshot_date.isoformat(),
                filename=filename,
            )
            continue
        refs.append(fetcher.fetch(source, entry.url_template, snapshot_date, filename=filename))
    return tuple(refs), tuple(reused)


def refresh_identity(
    *,
    conn: Connection,
    clock: Clock | None = None,
    settings: Settings | None = None,
    snapshot_date: date | None = None,
    fetcher: Fetcher | None = None,
    l0: L0Store | None = None,
    dry_run: bool = False,
) -> IdentityRefreshReport:
    """Fetch both identity files into L0 and re-derive the master from them. Does not commit.

    What it does: takes the host lease, fetches what L0 does not already hold, then reads both
    payloads back out (re-checksummed) and drives `ingest_snapshot`. The read-back is the point —
    the master is derived from the lake, not from whatever the fetch happened to hold in memory,
    so the whole path is the one a rebuild-from-L0 would take.
    What it assumes: the caller owns the transaction, as everywhere else in this codebase.
    What it never does: fetch without a lease when it builds its own fetcher. An injected `fetcher`
    is the caller's responsibility — that is how the scheduler job and a test both work.
    """
    resolved_clock = SystemClock() if clock is None else clock
    resolved_settings = get_settings() if settings is None else settings
    on_date = resolved_clock.today() if snapshot_date is None else snapshot_date
    store = (
        L0Store(clock=resolved_clock, data_root=resolved_settings.data_root) if l0 is None else l0
    )

    if fetcher is None:
        with leased_fetcher(
            [_host_of(source) for source in IDENTITY_SOURCES],
            clock=resolved_clock,
            command="identity refresh",
            settings=resolved_settings,
            l0=store,
        ) as built:
            refs, reused = fetch_identity_files(built, on_date, l0=store)
    else:
        refs, reused = fetch_identity_files(fetcher, on_date, l0=store)

    equity_list, changes = read_snapshot_from_l0(on_date, store=store)
    report = ingest_snapshot(
        conn, equity_list=equity_list, symbol_changes=changes, snapshot_date=on_date
    )
    if dry_run:
        conn.rollback()
    result = IdentityRefreshReport(snapshot_date=on_date, refs=refs, reused=reused, ingest=report)
    _LOG.info(
        "identity.refresh_done",
        snapshot_date=on_date.isoformat(),
        fetched=len(refs) - len(reused),
        reused=len(reused),
        clean=report.is_clean,
        dry_run=dry_run,
    )
    return result


def _host_of(source: str, register: SourceRegister | None = None) -> str:
    """The host a source is fetched from, read from the register rather than repeated here."""
    loaded = load_register() if register is None else register
    entry = next((row for row in loaded.sources if row.id == source), None)
    if entry is None:
        raise KeyError(f"source register has no row for {source!r}")
    return entry.host


def main() -> int:
    """Fetch and re-derive. Exit 1 when the ingest was not clean, so a bad refresh is visible."""
    from dataplatform.logging import configure_logging

    configure_logging()
    with connection() as conn:
        report = refresh_identity(conn=conn)
        conn.commit()
    print(report.summary())
    return 0 if report.ingest.is_clean else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    import sys

    sys.exit(main())
