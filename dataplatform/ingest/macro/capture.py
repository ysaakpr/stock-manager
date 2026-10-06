"""Macro capture drivers: fetch → L0 → parse → `macro_series`, one step per source.

Two scheduler jobs and one CLI sit on top of the same steps:

* **`fbil_reference_rates`** (daily) — FBIL's INR reference rates over a trailing window wide
  enough to cover the public site's few-session publication lag. Grade A: dated archive,
  administered benchmark, never revised.
* **`macro_release_capture`** (weekly) — World Bank (current vintage, dated `lastupdated`), the
  RBI's "Current Rates" panel, the OEA's WPI file, GSTN's collection workbook, and the trailing
  month of India VIX spot. The first four are Tier B forward capture: their backtestable window
  starts the day this job first ran, and the store holds no earlier partition to pretend otherwise.
* **`python -m dataplatform.ingest.macro.capture`** — the same steps by hand, plus the two dated
  backfills (`fbil`, `india-vix`).

Every step is idempotent per (source, capture date): the L0 key is checked before any request, so a
re-run the same day makes zero requests and re-derives from the stored bytes. Current-vintage
sources write only the observations that are new or whose value changed since the store last knew
them (`new_or_revised`) — a weekly capture of an unchanged table adds nothing, and a revised
provisional figure lands as a second record rather than overwriting the first.

Each step takes its host's lease on its own, so one busy or failing host never stops the others; the
job raises at the end naming every step that failed, which records the run FAILED on the status
surface instead of in a log line nobody reads.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.fetcher import Fetcher, leased_fetcher
from dataplatform.ingest.indices import tri_request_body
from dataplatform.ingest.macro.fbil import (
    FBIL_ARCHIVE_EPOCH,
    FBIL_SOURCE_ID,
    fbil_filename,
    fbil_url,
    parse_fbil_reference_rates,
)
from dataplatform.ingest.macro.gst import (
    GST_COLLECTION_URL,
    GST_SOURCE_ID,
    gst_filename,
    parse_gst_collections,
)
from dataplatform.ingest.macro.india_vix import (
    INDIA_VIX_REQUEST_NAME,
    INDIA_VIX_SOURCE_ID,
    INDIA_VIX_URL,
    india_vix_filename,
    parse_india_vix_history,
)
from dataplatform.ingest.macro.models import MacroRelease
from dataplatform.ingest.macro.rbi_rates import (
    RBI_HOME_URL,
    RBI_RATES_SOURCE_ID,
    parse_rbi_current_rates,
    rbi_home_filename,
)
from dataplatform.ingest.macro.worldbank import (
    WORLDBANK_SERIES,
    WORLDBANK_SOURCE_ID,
    parse_worldbank,
    worldbank_filename,
    worldbank_url,
)
from dataplatform.ingest.macro.wpi import (
    WPI_DOWNLOAD_PAGE_URL,
    WPI_SOURCE_ID,
    parse_wpi_download_page,
    parse_wpi_monthly_index,
    wpi_page_filename,
)
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.macro_series import read_latest, write_release

if TYPE_CHECKING:
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "FBIL_DAILY_WINDOW_DAYS",
    "STEP_HOSTS",
    "VIX_WEEKLY_WINDOW_DAYS",
    "WEEKLY_SOURCES",
    "CaptureOutcome",
    "MacroCaptureError",
    "backfill_fbil",
    "capture_fbil",
    "capture_gst",
    "capture_india_vix",
    "capture_rbi_rates",
    "capture_worldbank",
    "capture_wpi",
    "main",
    "new_or_revised",
    "run_fbil_capture",
    "run_macro_release_capture",
]

_LOG = get_logger(__name__)

#: How far back the daily FBIL job asks. The public archive showed 2026-09-29 as its newest rate
#: on 2026-10-06 (four sessions behind, across a holiday); three weeks covers that with room.
FBIL_DAILY_WINDOW_DAYS: Final = 21

#: The weekly job's India VIX window — a month, so a missed week is re-covered by the next.
VIX_WEEKLY_WINDOW_DAYS: Final = 31

#: One backfill request per this many days of FBIL archive (about 1,500 rows, ~200 kB).
FBIL_BACKFILL_CHUNK_DAYS: Final = 365

#: The register id each weekly step writes under, and the host it talks to.
STEP_HOSTS: Final[dict[str, str]] = {
    WORLDBANK_SOURCE_ID: "api.worldbank.org",
    RBI_RATES_SOURCE_ID: "www.rbi.org.in",
    WPI_SOURCE_ID: "eaindustry.nic.in",
    GST_SOURCE_ID: "tutorial.gst.gov.in",
    INDIA_VIX_SOURCE_ID: "niftyindices.com",
    FBIL_SOURCE_ID: "www.fbil.org.in",
}

#: The sources `macro_release_capture` covers, in run order.
WEEKLY_SOURCES: Final[tuple[str, ...]] = (
    WORLDBANK_SOURCE_ID,
    RBI_RATES_SOURCE_ID,
    WPI_SOURCE_ID,
    GST_SOURCE_ID,
    INDIA_VIX_SOURCE_ID,
)


class MacroCaptureError(RuntimeError):
    """One or more capture steps failed; the message names each and why."""


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    """What one step did: requests made, releases parsed, facts written."""

    source: str
    requests: int
    releases: int
    facts_written: int
    note: str = ""

    @property
    def line(self) -> str:
        """One human-readable line for the CLI and the job log."""
        extra = f" — {self.note}" if self.note else ""
        return (
            f"{self.source}: {self.requests} request(s), {self.releases} release(s), "
            f"{self.facts_written} fact(s) written{extra}"
        )


def new_or_revised(release: MacroRelease, *, data_root: Path | None) -> MacroRelease | None:
    """`release` reduced to the facts whose value the store did not already hold, or `None`.

    What it does: compare each fact with the latest value knowable on the release date for the same
    `(series_id, period_start, period_end)` and keep it only if it is new or different.
    What it assumes: `release` is a current-vintage table restated in full each capture (WPI, GST),
    dated by its capture.
    What it never does: drop a revision — a changed value is exactly what is kept — or touch a
    stored record. A re-run the same day finds everything already known and returns `None`.
    """
    wanted = {fact.series_id for fact in release.facts}
    known = {
        (fact.series_id, fact.period_start, fact.period_end): fact.value
        for fact in read_latest(release.release_date, series=wanted, data_root=data_root)
    }
    fresh = tuple(
        fact
        for fact in release.facts
        if known.get((fact.series_id, fact.period_start, fact.period_end)) != fact.value
    )
    if not fresh:
        return None
    return MacroRelease(
        release_date=release.release_date,
        source=release.source,
        facts=fresh,
        l0_key=release.l0_key,
    )


# ── steps ───────────────────────────────────────────────────────────────────────────────────


def _fetch_once(
    fetcher: Fetcher,
    l0: L0Store,
    source: str,
    url: str,
    logical_date: date,
    filename: str,
    *,
    payload: bytes | None = None,
) -> tuple[L0Ref, int]:
    """The stored ref for this key, fetching only when L0 does not already hold it."""
    if l0.exists(source, logical_date, filename):
        return l0.ref_for(source, logical_date, filename), 0
    return fetcher.fetch(source, url, logical_date, filename=filename, payload=payload), 1


def _write(releases: Sequence[MacroRelease], *, data_root: Path | None) -> int:
    written = 0
    for release in releases:
        write_release(release, data_root=data_root)
        written += len(release.facts)
    return written


def capture_worldbank(
    fetcher: Fetcher, l0: L0Store, *, on: date, data_root: Path | None
) -> CaptureOutcome:
    """Every `WORLDBANK_SERIES` indicator, one request each, dated by its envelope."""
    requests = 0
    releases: list[MacroRelease] = []
    for spec in WORLDBANK_SERIES:
        filename = worldbank_filename(spec.indicator, on)
        ref, made = _fetch_once(
            fetcher, l0, WORLDBANK_SOURCE_ID, worldbank_url(spec.indicator), on, filename
        )
        requests += made
        releases.append(parse_worldbank(l0.get(ref), spec=spec, filename=filename, l0_key=ref.key))
    vintages = sorted({release.release_date.isoformat() for release in releases})
    return CaptureOutcome(
        WORLDBANK_SOURCE_ID,
        requests,
        len(releases),
        _write(releases, data_root=data_root),
        note=f"vintage(s) {', '.join(vintages)}",
    )


def capture_rbi_rates(
    fetcher: Fetcher, l0: L0Store, *, on: date, data_root: Path | None
) -> CaptureOutcome:
    """The RBI home page's "Current Rates" panel, observed on `on`."""
    filename = rbi_home_filename(on)
    ref, made = _fetch_once(fetcher, l0, RBI_RATES_SOURCE_ID, RBI_HOME_URL, on, filename)
    release = parse_rbi_current_rates(l0.get(ref), captured=on, filename=filename, l0_key=ref.key)
    return CaptureOutcome(RBI_RATES_SOURCE_ID, made, 1, _write([release], data_root=data_root))


def capture_wpi(
    fetcher: Fetcher, l0: L0Store, *, on: date, data_root: Path | None
) -> CaptureOutcome:
    """The download page, then whichever monthly WPI file it links; new or revised months only."""
    page_name = wpi_page_filename(on)
    page, made_page = _fetch_once(fetcher, l0, WPI_SOURCE_ID, WPI_DOWNLOAD_PAGE_URL, on, page_name)
    url, month = parse_wpi_download_page(l0.get(page), filename=page_name)
    filename = f"wpi_monthly_index_{month}_{on:%Y%m%d}.xlsx"
    ref, made = _fetch_once(fetcher, l0, WPI_SOURCE_ID, url, on, filename)
    full = parse_wpi_monthly_index(l0.get(ref), captured=on, filename=filename, l0_key=ref.key)
    fresh = new_or_revised(full, data_root=data_root)
    written = _write([fresh] if fresh else [], data_root=data_root)
    return CaptureOutcome(
        WPI_SOURCE_ID, made_page + made, 1, written, note=f"file {month}, {len(full.facts)} parsed"
    )


def capture_gst(
    fetcher: Fetcher, l0: L0Store, *, on: date, data_root: Path | None
) -> CaptureOutcome:
    """GSTN's collection workbook; new or revised months only."""
    filename = gst_filename(on)
    ref, made = _fetch_once(fetcher, l0, GST_SOURCE_ID, GST_COLLECTION_URL, on, filename)
    full = parse_gst_collections(l0.get(ref), captured=on, filename=filename, l0_key=ref.key)
    fresh = new_or_revised(full, data_root=data_root)
    written = _write([fresh] if fresh else [], data_root=data_root)
    return CaptureOutcome(GST_SOURCE_ID, made, 1, written, note=f"{len(full.facts)} parsed")


def capture_india_vix(
    fetcher: Fetcher, l0: L0Store, *, start: date, end: date, data_root: Path | None
) -> CaptureOutcome:
    """India VIX spot OHLC over `[start, end]` in one POST, filed under `end`."""
    filename = india_vix_filename(start, end)
    body = tri_request_body(INDIA_VIX_REQUEST_NAME, start, end)
    ref, made = _fetch_once(
        fetcher, l0, INDIA_VIX_SOURCE_ID, INDIA_VIX_URL, end, filename, payload=body
    )
    releases = parse_india_vix_history(l0.get(ref), filename=filename, l0_key=ref.key)
    span = f"{releases[0].release_date}..{releases[-1].release_date}" if releases else "empty"
    return CaptureOutcome(
        INDIA_VIX_SOURCE_ID,
        made,
        len(releases),
        _write(releases, data_root=data_root),
        note=f"sessions {span}",
    )


def capture_fbil(
    fetcher: Fetcher, l0: L0Store, *, start: date, end: date, data_root: Path | None
) -> CaptureOutcome:
    """FBIL reference rates over `[start, end]` in one request, filed under `end`."""
    filename = fbil_filename(start, end)
    ref, made = _fetch_once(fetcher, l0, FBIL_SOURCE_ID, fbil_url(start, end), end, filename)
    releases = parse_fbil_reference_rates(l0.get(ref), filename=filename, l0_key=ref.key)
    span = f"{releases[0].release_date}..{releases[-1].release_date}" if releases else "empty"
    return CaptureOutcome(
        FBIL_SOURCE_ID,
        made,
        len(releases),
        _write(releases, data_root=data_root),
        note=f"sessions {span}",
    )


def backfill_fbil(
    fetcher: Fetcher,
    l0: L0Store,
    *,
    start: date,
    end: date,
    data_root: Path | None,
    chunk_days: int = FBIL_BACKFILL_CHUNK_DAYS,
) -> tuple[CaptureOutcome, ...]:
    """The FBIL archive from `start` to `end` in `chunk_days` windows, oldest first, resumable.

    A window already in L0 costs no request (its key is checked first), so a killed backfill
    re-run picks up where it stopped and re-derives the finished windows from the lake.
    """
    if end < start:
        raise ValueError(f"backfill ends ({end}) before it starts ({start})")
    outcomes: list[CaptureOutcome] = []
    cursor = max(start, FBIL_ARCHIVE_EPOCH)
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=chunk_days - 1))
        outcome = capture_fbil(fetcher, l0, start=cursor, end=window_end, data_root=data_root)
        _LOG.info("macro.fbil_backfill.window", line=outcome.line, state="PUBLISHED")
        outcomes.append(outcome)
        cursor = window_end + timedelta(days=1)
    return tuple(outcomes)


# ── jobs ────────────────────────────────────────────────────────────────────────────────────

StepFn = Callable[[Fetcher, L0Store, date, Path | None], CaptureOutcome]


def _weekly_steps(on: date) -> dict[str, StepFn]:
    vix_start = on - timedelta(days=VIX_WEEKLY_WINDOW_DAYS)
    return {
        WORLDBANK_SOURCE_ID: lambda f, l0, d, r: capture_worldbank(f, l0, on=d, data_root=r),
        RBI_RATES_SOURCE_ID: lambda f, l0, d, r: capture_rbi_rates(f, l0, on=d, data_root=r),
        WPI_SOURCE_ID: lambda f, l0, d, r: capture_wpi(f, l0, on=d, data_root=r),
        GST_SOURCE_ID: lambda f, l0, d, r: capture_gst(f, l0, on=d, data_root=r),
        INDIA_VIX_SOURCE_ID: lambda f, l0, d, r: capture_india_vix(
            f, l0, start=vix_start, end=d, data_root=r
        ),
    }


def _run_steps(
    steps: dict[str, StepFn], *, settings: Settings, clock: Clock, command: str
) -> tuple[CaptureOutcome, ...]:
    """Run each step under its own host lease; collect failures and raise them together."""
    on = clock.today()
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    outcomes: list[CaptureOutcome] = []
    failures: list[str] = []
    for source, step in steps.items():
        host = STEP_HOSTS[source]
        try:
            with leased_fetcher([host], clock=clock, command=command, settings=settings) as fetcher:
                outcome = step(fetcher, l0, on, settings.data_root)
        except Exception as error:  # one source failing must not stop the others; re-raised below
            _LOG.error(
                "macro.capture.failed",
                source=source,
                host=host,
                date=on.isoformat(),
                error=f"{type(error).__name__}: {error}",
                state="FAILED",
            )
            failures.append(f"{source} ({host}): {type(error).__name__}: {error}")
            continue
        _LOG.info(
            "macro.capture.done",
            source=source,
            date=on.isoformat(),
            requests=outcome.requests,
            facts=outcome.facts_written,
            state="PUBLISHED",
        )
        outcomes.append(outcome)
    if failures:
        raise MacroCaptureError(
            f"{len(failures)} of {len(steps)} macro capture step(s) failed on {on}: "
            + "; ".join(failures)
        )
    return tuple(outcomes)


def run_macro_release_capture(context: JobContext) -> None:
    """The weekly `macro_release_capture` job body (see the module note)."""
    _run_steps(
        _weekly_steps(context.clock.today()),
        settings=context.settings,
        clock=context.clock,
        command="macro_release_capture",
    )


def run_fbil_capture(context: JobContext) -> None:
    """The daily `fbil_reference_rates` job body: the trailing `FBIL_DAILY_WINDOW_DAYS`."""
    on = context.clock.today()
    start = on - timedelta(days=FBIL_DAILY_WINDOW_DAYS)
    _run_steps(
        {FBIL_SOURCE_ID: lambda f, l0, d, r: capture_fbil(f, l0, start=start, end=d, data_root=r)},
        settings=context.settings,
        clock=context.clock,
        command="fbil_reference_rates",
    )


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m dataplatform.ingest.macro.capture {weekly,fbil,india-vix}`.

    `weekly` runs the weekly job's steps now (`--only` narrows them). `fbil` and `india-vix` are the
    dated backfills; `--dry-run` prints the windows and the request count without a request.
    """
    parser = argparse.ArgumentParser(prog="macro-capture", description=main.__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    weekly = sub.add_parser("weekly", help="run the weekly macro capture steps now")
    weekly.add_argument("--only", action="append", choices=WEEKLY_SOURCES, default=None)
    for name, epoch in (("fbil", FBIL_ARCHIVE_EPOCH), ("india-vix", date(2008, 3, 1))):
        backfill = sub.add_parser(name, help=f"dated {name} backfill")
        backfill.add_argument("--start", type=date.fromisoformat, default=epoch)
        backfill.add_argument("--end", type=date.fromisoformat, default=None)
        backfill.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    clock: Clock = SystemClock()
    end = getattr(args, "end", None) or clock.today()

    try:
        if args.command == "weekly":
            steps = _weekly_steps(clock.today())
            if args.only:
                steps = {k: v for k, v in steps.items() if k in set(args.only)}
            outcomes = _run_steps(steps, settings=settings, clock=clock, command="macro-capture")
        elif args.command == "fbil":
            if args.dry_run:
                return _print_plan_fbil(args.start, end)
            outcomes = _run_steps(
                {
                    FBIL_SOURCE_ID: lambda f, l0, d, r: _fold(
                        backfill_fbil(f, l0, start=args.start, end=end, data_root=r)
                    )
                },
                settings=settings,
                clock=clock,
                command="macro-capture fbil",
            )
        else:
            if args.dry_run:
                print(f"india-vix {args.start}..{end}: 1 POST to niftyindices.com")
                return 0
            outcomes = _run_steps(
                {
                    INDIA_VIX_SOURCE_ID: lambda f, l0, d, r: capture_india_vix(
                        f, l0, start=args.start, end=end, data_root=r
                    )
                },
                settings=settings,
                clock=clock,
                command="macro-capture india-vix",
            )
    except MacroCaptureError as error:
        print(str(error), file=sys.stderr)
        return 1
    for outcome in outcomes:
        print(outcome.line)
    return 0


def _fold(outcomes: Sequence[CaptureOutcome]) -> CaptureOutcome:
    """Several windows' outcomes as one line."""
    spans = [o.note.removeprefix("sessions ") for o in outcomes if o.note != "sessions empty"]
    return CaptureOutcome(
        FBIL_SOURCE_ID,
        sum(o.requests for o in outcomes),
        sum(o.releases for o in outcomes),
        sum(o.facts_written for o in outcomes),
        note=f"{len(outcomes)} window(s): {spans[0].split('..')[0]}..{spans[-1].split('..')[-1]}"
        if spans
        else f"{len(outcomes)} window(s), all empty",
    )


def _print_plan_fbil(start: date, end: date) -> int:
    windows = 0
    cursor = max(start, FBIL_ARCHIVE_EPOCH)
    while cursor <= end:
        windows += 1
        cursor += timedelta(days=FBIL_BACKFILL_CHUNK_DAYS)
    print(f"fbil {max(start, FBIL_ARCHIVE_EPOCH)}..{end}: {windows} request(s) to www.fbil.org.in")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
