"""Fetch the BSE scrip master into L0 and merge it into the D2 identity master.

The BSE half of "Symbol / ISIN master". BSE keys every row it publishes on `SC_CODE`, never on
ISIN, so until this has run there is no `scrip_code → ISIN` map and **no BSE row can be joined at
all** (invariant #2). That is why `exchange_listing` holds NSE rows only, why the pre-2024 BSE
bhavcopy era — which has no ISIN column, only `SC_CODE` — cannot be backfilled, and why every
`ca_reconciliation` flag reads `SINGLE_SOURCE`: there is no BSE action to compare an NSE one to.

**Three statuses, not one.** The register's `pit_notes` are categorical: *"status=Active returns
live scrips only. Delisted and suspended scrips must be pulled with the other status values or the
backfill silently acquires survivorship bias."* A master built from Active alone cannot resolve the
scrip code of a company that has since delisted, so every one of its rows is quarantined and ten
years of BSE history quietly becomes ten years of survivors. This driver fetches all three and
refuses to merge a partial set — the failure mode it exists to prevent is invisible in the output,
so it has to be impossible rather than merely discouraged.

**Cost.** Three requests to `api.bseindia.com`, ~1.7 MB each. Not a bulk campaign, and a different
host from the NSE archives, so it neither needs the 10-year-backfill go nor competes with a running
NSE campaign for its budget.

    uv run python -m dataplatform.ingest.bse_scrip_refresh              # fetch + merge
    uv run python -m dataplatform.ingest.bse_scrip_refresh --dry-run    # fetch + parse, no write
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.bse.scrip_master import (
    BseScripIngestReport,
    ingest_scrip_master,
    parse_scrip_master,
)
from dataplatform.ingest.fetcher import Fetcher, leased_fetcher
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = ["SCRIP_STATUSES", "BseScripRefreshReport", "refresh_bse_scrip_master"]

_LOG = get_logger(__name__)

SOURCE_ID: Final = "bse_scrip_master"
_HOST: Final = "api.bseindia.com"

#: Every status the endpoint serves. All three, always — see the module docstring.
SCRIP_STATUSES: Final = ("Active", "Suspended", "Delisted")

#: The register's URL carries `status={Active|Suspended|Delisted}` as documentation of the choice,
#: not as a substitution token. This is the token actually replaced.
_STATUS_TOKEN: Final = "{Active|Suspended|Delisted}"


@dataclass(frozen=True, slots=True)
class BseScripRefreshReport:
    """What one refresh fetched and merged."""

    snapshot_date: date
    scrips_by_status: dict[str, int]
    reused_from_l0: tuple[str, ...]
    ingest: BseScripIngestReport | None

    @property
    def total_scrips(self) -> int:
        return sum(self.scrips_by_status.values())

    def summary(self) -> str:
        per_status = ", ".join(f"{s}={n}" for s, n in sorted(self.scrips_by_status.items()))
        merged = "dry-run, not merged" if self.ingest is None else _merged_summary(self.ingest)
        return f"{self.total_scrips} scrips ({per_status}); {merged}"


def _merged_summary(report: BseScripIngestReport) -> str:
    """One line naming what the merge changed and whether the identity held up."""
    return (
        f"{report.scrips_seen} scrips seen, {report.skipped_no_isin} without an ISIN, "
        f"{report.counts.total} rows written, "
        f"{len(report.conflicts)} conflicts, {len(report.refusals)} refusals"
    )


def _url_for(register: SourceRegister, status: str) -> str:
    entry = next((e for e in register.sources if e.id == SOURCE_ID), None)
    if entry is None:
        raise KeyError(f"source register has no row for {SOURCE_ID!r}")
    if _STATUS_TOKEN not in entry.url_template:
        raise ValueError(
            f"{SOURCE_ID} url_template no longer carries {_STATUS_TOKEN!r}; the register changed "
            f"shape and this driver would silently fetch one status three times"
        )
    return entry.url_template.replace(_STATUS_TOKEN, status)


def _land_in_l0(
    fetcher: Fetcher, *, l0: L0Store, register: SourceRegister, snapshot_date: date
) -> tuple[dict[str, L0Ref], tuple[str, ...]]:
    """Fetch each status into L0 for `snapshot_date`; reuse a payload L0 already holds."""
    refs: dict[str, L0Ref] = {}
    reused: list[str] = []
    for status in SCRIP_STATUSES:
        filename = f"ListofScripData_{status}.json"
        if l0.exists(SOURCE_ID, snapshot_date, filename):
            refs[status] = l0.ref_for(SOURCE_ID, snapshot_date, filename)
            reused.append(status)
            _LOG.info("bse_scrip.reused_l0", status=status, filename=filename)
            continue
        refs[status] = fetcher.fetch(
            SOURCE_ID, _url_for(register, status), snapshot_date, filename=filename
        )
        _LOG.info("bse_scrip.fetched", status=status, filename=filename)
    return refs, tuple(reused)


def refresh_bse_scrip_master(
    *,
    clock: Clock | None = None,
    settings: Settings | None = None,
    snapshot_date: date | None = None,
    dry_run: bool = False,
) -> BseScripRefreshReport:
    """Fetch all three scrip-master statuses into L0 and merge them into D2. Commits on success.

    What it assumes: nothing else holds `api.bseindia.com`'s request budget — the lease enforces it.
    What it never does: merge a partial snapshot. A status that fails to fetch raises before any
    write, because a master missing its delisted scrips is worse than no master: it resolves just
    enough rows to look correct.
    """
    clock = SystemClock() if clock is None else clock
    settings = get_settings() if settings is None else settings
    snapshot_date = clock.today() if snapshot_date is None else snapshot_date
    register = load_register()
    l0 = L0Store(clock=clock, data_root=settings.data_root)

    with leased_fetcher(
        [_HOST], clock=clock, command="bse_scrip_refresh", settings=settings
    ) as fetcher:
        refs, reused = _land_in_l0(fetcher, l0=l0, register=register, snapshot_date=snapshot_date)

    # Parsed outside the lease: the requests are done, and a parse failure must not hold a budget.
    payloads = {status: l0.get(ref).decode("utf-8") for status, ref in refs.items()}
    counts = {status: len(parse_scrip_master(text)[0]) for status, text in payloads.items()}
    _LOG.info("bse_scrip.parsed", **counts)

    if dry_run:
        return BseScripRefreshReport(snapshot_date, counts, reused, None)

    # One merge over the concatenated snapshot: `derive_master` detects ambiguity *within* the
    # snapshot as well as against the store, and three separate merges would hide a scrip code
    # that appears under two statuses from that check.
    combined = _concatenate(payloads)
    with connection(settings) as conn:
        report = ingest_scrip_master(
            conn, scrip_master_json=combined, snapshot_date=snapshot_date, clock=clock
        )
        conn.commit()
    _LOG.info("bse_scrip.merged", summary=_merged_summary(report), clean=report.is_clean)
    return BseScripRefreshReport(snapshot_date, counts, reused, report)


def _concatenate(payloads: dict[str, str]) -> str:
    """One JSON array holding every status's records, in a stable status order."""
    import json

    records: list[object] = []
    for status in SCRIP_STATUSES:
        records.extend(json.loads(payloads[status]))
    return json.dumps(records)


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Fetch and merge the BSE scrip master.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch into L0 and parse, but do not merge into the identity master",
    )
    parser.add_argument(
        "--snapshot-date",
        type=date.fromisoformat,
        default=None,
        help="the L0 logical date to land under (default: today)",
    )
    args = parser.parse_args()
    report = refresh_bse_scrip_master(snapshot_date=args.snapshot_date, dry_run=args.dry_run)
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
