"""Acquire dated payloads into L0 and stop there — no parse, no L1, no `sync_state`.

``uv run python -m dataplatform.ingest.l0_acquire --source nse_bhavcopy --from 2026-09-02 \
--to 2026-10-01``
``uv run python -m dataplatform.ingest.l0_acquire --tri nifty50 --tri niftyit --end 2026-10-05``

The gap this closes: every price driver in this package fetches *and* derives. `backfill` drives a
session `fetch → L0 → parse → L1 → sync_state` in one step, and `tri_backfill` writes the series to
L1 the moment it lands. That is right for the daily job and wrong for a catch-up whose L1 must not
move yet — a 2026-10 audit found the bhavcopy family three weeks behind while four other fixes were
changing how L1 and L2 are derived, so the missed sessions had to be *acquired* now and *derived*
once, in the single sequenced rebuild that follows those fixes. L0 is the record and L1 a
derivation of it (invariant #1); acquiring is the half that cannot wait, because the archive is the
only copy of the bytes and every one of these sources is a third party's to withdraw.

What it reuses, so an acquired payload is indistinguishable from one the backfill would have
fetched: the request is built by the *same* `SourceSet.build_request` the backfill and the daily
pipeline use (era, URL template and L0 filename included), and the TRI request by the same
`indices` helpers `ingest_tri` uses. A later `backfill` run over the same range therefore finds the
payload under the key it would have written and derives L1 from it without a request
(`BackfillRunner` reuses a stored payload).

What it never does: overwrite (`L0Store.put` cannot), fetch a key L0 already holds (that is a
request spent on bytes we have), write anything outside L0, or route around the 403 hard stop. It
takes the host lease for every host it will touch, so it cannot run beside another driver against
the same request budget. A payload the archive does not serve is named with its HTTP status in the
report and the exit code is non-zero — an absence is evidence, never a silent skip.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.backfill import (
    BSE_BHAVCOPY,
    NSE_BHAVCOPY,
    NSE_DELIVERY,
    SOURCE_SETS,
    FetchRequest,
)
from dataplatform.ingest.calendar import CalendarCoverageError, trading_calendar
from dataplatform.ingest.fetcher import (
    Fetcher,
    FetchError,
    FetchHTTPError,
    ForbiddenSpikeError,
    leased_fetcher,
)
from dataplatform.ingest.indices import (
    TRI_SOURCE_ID,
    l0_tri_filename,
    tri_request_body,
    tri_url,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.ingest.tri_backfill import DEFAULT_INDEX_SET, EARLIEST_REQUESTED, IndexSpec
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Error, L0Store

__all__ = [
    "ACQUIRABLE_SOURCE_SETS",
    "AcquireOutcome",
    "AcquireReport",
    "AcquireStatus",
    "AcquireUnit",
    "acquire",
    "main",
    "price_units",
    "tri_units",
]

_LOG = get_logger(__name__)

#: The dated price source sets this driver may acquire. Each one's `build_request` names its own
#: era's register id, so the cutover dates are the backfill's, never re-spelled here.
ACQUIRABLE_SOURCE_SETS: Final[tuple[str, ...]] = (NSE_BHAVCOPY, NSE_DELIVERY, BSE_BHAVCOPY)


class AcquireStatus(StrEnum):
    """What happened to one unit. Only `FETCHED` spent a request that landed bytes."""

    FETCHED = "FETCHED"
    PRESENT = "PRESENT"  # L0 already held this key; no request was made
    MISSING = "MISSING"  # the archive answered non-2xx — the status code is the evidence
    FAILED = "FAILED"  # transport/5xx exhausted, or L0 refused the bytes


@dataclass(frozen=True, slots=True)
class AcquireUnit:
    """One payload to land in L0: which register source, which date, from where, under what name.

    `body` is the POST body for endpoints that take one (TRI); `None` for a plain GET.
    """

    label: str
    source_id: str
    logical_date: date
    url: str
    filename: str
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class AcquireOutcome:
    unit: AcquireUnit
    status: AcquireStatus
    detail: str = ""
    http_status: int | None = None
    l0_key: str | None = None


@dataclass(slots=True)
class AcquireReport:
    """Every unit's outcome, in plan order, and whether the run ended on a hard stop."""

    outcomes: list[AcquireOutcome] = field(default_factory=list)
    hard_stopped: bool = False

    def count(self, status: AcquireStatus) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status is status)

    @property
    def clean(self) -> bool:
        """True when every unit is in L0 now — fetched this run or already there."""
        return not self.hard_stopped and all(
            o.status in (AcquireStatus.FETCHED, AcquireStatus.PRESENT) for o in self.outcomes
        )


def _unit_of(request: FetchRequest) -> AcquireUnit:
    return AcquireUnit(
        label=request.state_source,
        source_id=request.fetch_source,
        logical_date=request.trade_date,
        url=request.url,
        filename=request.filename,
    )


def price_units(
    source_set: str, from_date: date, to_date: date, *, register: SourceRegister
) -> list[AcquireUnit]:
    """One unit per session the C.2 calendar expects data for in `[from_date, to_date]`.

    Raises `CalendarCoverageError` for a range the calendar cannot vouch for — planning fetches for
    dates nobody can say traded is how phantom sessions are made — and `ValueError` for a source
    set this driver does not acquire.
    """
    if source_set not in ACQUIRABLE_SOURCE_SETS:
        raise ValueError(f"{source_set!r} is not acquirable; choose from {ACQUIRABLE_SOURCE_SETS}")
    sessions = trading_calendar().expected_data_dates(from_date, to_date)
    build = SOURCE_SETS[source_set].build_request
    return [_unit_of(build(day, register)) for day in sessions]


def tri_units(
    indices: Sequence[IndexSpec],
    *,
    end: date,
    start: date = EARLIEST_REQUESTED,
    register: SourceRegister,
) -> list[AcquireUnit]:
    """One whole-history TRI request per index, named and bodied exactly as `ingest_tri` would."""
    url = tri_url(register)
    return [
        AcquireUnit(
            label=f"tri/{spec.slug}",
            source_id=TRI_SOURCE_ID,
            logical_date=end,
            url=url,
            filename=l0_tri_filename(spec.slug, start, end),
            body=tri_request_body(spec.name, start, end),
        )
        for spec in indices
    ]


def acquire(
    units: Sequence[AcquireUnit],
    *,
    fetcher: Fetcher,
    l0: L0Store,
    should_stop: Callable[[], bool] = lambda: False,
) -> AcquireReport:
    """Land every unit in L0 that is not already there, and say what happened to each.

    What it does: skips a unit whose key L0 already holds (no request), otherwise fetches it under
    its register policy. A non-2xx answer is recorded `MISSING` with its status code and the run
    goes on; a 403 spike ends the run (`hard_stopped`) with every later unit unrecorded.
    What it assumes: `fetcher` and `l0` share one lake root, and the caller holds the host lease.
    What it never does: parse, write outside L0, or retry what the fetcher already gave up on.
    """
    report = AcquireReport()
    for unit in units:
        if should_stop():
            break
        context = {
            "source": unit.source_id,
            "date": unit.logical_date.isoformat(),
            "unit": unit.label,
            "filename": unit.filename,
        }
        if l0.exists(unit.source_id, unit.logical_date, unit.filename):
            key = l0.ref_for(unit.source_id, unit.logical_date, unit.filename).key
            report.outcomes.append(AcquireOutcome(unit, AcquireStatus.PRESENT, l0_key=key))
            _LOG.info("l0_acquire.present", **context, l0_key=key, state="PRESENT")
            continue
        try:
            ref = fetcher.fetch(
                unit.source_id,
                unit.url,
                unit.logical_date,
                filename=unit.filename,
                payload=unit.body,
            )
        except ForbiddenSpikeError as spike:
            report.outcomes.append(AcquireOutcome(unit, AcquireStatus.FAILED, detail=str(spike)))
            report.hard_stopped = True
            _LOG.critical("l0_acquire.hard_stop", **context, error=str(spike), state="HARD_STOPPED")
            break
        except FetchHTTPError as error:
            report.outcomes.append(
                AcquireOutcome(
                    unit, AcquireStatus.MISSING, detail=str(error), http_status=error.status_code
                )
            )
            _LOG.warning(
                "l0_acquire.missing",
                **context,
                http_status=error.status_code,
                url=error.url,
                state="MISSING",
            )
            continue
        except (FetchError, L0Error, OSError) as error:
            # L0Error covers the store's own refusals (immutability, checksum) — a stored key that
            # disagrees with the archive is a human's call, never a reason to overwrite.
            detail = f"{type(error).__name__}: {error}"
            report.outcomes.append(AcquireOutcome(unit, AcquireStatus.FAILED, detail=detail))
            _LOG.error("l0_acquire.failed", **context, error=detail, state="FAILED")
            continue
        report.outcomes.append(AcquireOutcome(unit, AcquireStatus.FETCHED, l0_key=ref.key))
        _LOG.info(
            "l0_acquire.fetched",
            **context,
            l0_key=ref.key,
            size_bytes=ref.size_bytes,
            sha256=ref.sha256,
            state="FETCHED",
        )
    return report


def _hosts(units: Sequence[AcquireUnit], register: SourceRegister) -> list[str]:
    by_id = {source.id: source.host for source in register.sources}
    return sorted({by_id[unit.source_id] for unit in units})


def _print_report(report: AcquireReport) -> None:
    for outcome in report.outcomes:
        unit = outcome.unit
        extra = f" http={outcome.http_status}" if outcome.http_status is not None else ""
        tail = outcome.l0_key or outcome.detail
        print(
            f"{unit.logical_date.isoformat()}\t{unit.label}\t{outcome.status.value}{extra}\t{tail}"
        )
    print(
        f"\n{len(report.outcomes)} unit(s): "
        + ", ".join(f"{status.value}={report.count(status)}" for status in AcquireStatus)
        + (" — HARD STOPPED on a 403 spike" if report.hard_stopped else "")
    )


def main(argv: Sequence[str] | None = None, *, clock: Clock | None = None) -> int:
    """CLI entry point. Exit 0 when every planned unit is in L0, 1 when any is not, 2 on a bad
    invocation, 3 on a 403 hard stop. `--dry-run` prints the plan and opens nothing."""
    parser = argparse.ArgumentParser(prog="l0-acquire", description=__doc__)
    parser.add_argument("--source", action="append", default=[], choices=ACQUIRABLE_SOURCE_SETS)
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat)
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat)
    known = {spec.slug: spec for spec in DEFAULT_INDEX_SET}
    parser.add_argument("--tri", action="append", default=[], choices=sorted(known))
    parser.add_argument("--end", type=date.fromisoformat, help="TRI window end (default: today)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.source and not args.tri:
        parser.error("name at least one --source or --tri")
    if args.source and (args.from_date is None or args.to_date is None):
        parser.error("--source needs --from and --to")

    clock = SystemClock() if clock is None else clock
    settings: Settings = get_settings()
    register = load_register()
    units: list[AcquireUnit] = []
    try:
        for name in args.source:
            units.extend(price_units(name, args.from_date, args.to_date, register=register))
    except (CalendarCoverageError, ValueError) as error:
        print(f"cannot plan: {error}", file=sys.stderr)
        return 2
    if args.tri:
        end = clock.today() if args.end is None else args.end
        units.extend(tri_units([known[slug] for slug in args.tri], end=end, register=register))

    if args.dry_run:
        for unit in units:
            print(f"{unit.logical_date.isoformat()}\t{unit.source_id}\t{unit.filename}\t{unit.url}")
        print(f"\n{len(units)} unit(s) planned (no fetch performed)")
        return 0

    l0 = L0Store(clock=clock, data_root=settings.data_root)
    print(f"resolved L0 root: {settings.data_root / 'L0'}")
    with leased_fetcher(
        _hosts(units, register),
        clock=clock,
        command="l0-acquire",
        settings=settings,
        register=register,
        l0=l0,
    ) as fetcher:
        report = acquire(units, fetcher=fetcher, l0=l0)
    _print_report(report)
    if report.hard_stopped:
        return 3
    return 0 if report.clean else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
