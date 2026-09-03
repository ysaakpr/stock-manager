"""Index-constituents ingest runner — the sector/industry classification map (M10.1).

M3.9 built the parser, the immutable writer and `membership_asof`, and a *single-index* driver
(`ingest_constituents`): fetch → L0 → parse → L1 → sync, for one slug. What it never had was the
thing that fills the map — a runner that sweeps the whole set of index lists the analyst reasons
about (the broad NIFTY 500 / NIFTY 50, and the sectoral/thematic lists: Bank, IT, Auto, Pharma,
FMCG, Metal, Energy, Realty, …). This module is that runner.

**What it does.** `run_constituents_ingest` drives every configured `IndexSpec` through M3.9's
single-index driver, once, for one `as_of` snapshot date, and returns a `CoverageReport` that
partitions the outcome per slug into *published*, *skipped* (already ingested — the resume path)
and *parked* (a failure, tagged with an enumerated `ParkCause`). One slug's failure never aborts
the others: a gated sectoral list parks and the sweep moves on, so a partial map is still a map.

**Resumability.** The constituents source id is *shared* by every index (they are the same
endpoint with a different slug), so a shared sync row cannot tell one slug's progress from
another's. Resume is therefore keyed on the L1 artefact itself: a slug whose `(slug, as_of)`
snapshot already exists in L1 is skipped, not re-fetched. Combined with M3.9's idempotent writer
(a re-derivation of unchanged membership is a no-op), a re-run after a crash costs at most the
slugs that had not yet landed.

**The hard limit, restated for the caller (§4.1, register `pit_notes`).** Each CSV is *always*
"as of today" — niftyindices publishes no historical constituents download. This runner ingests
the *current* snapshot only; accumulating dated snapshots into real point-in-time sector history
is M10.2's job. A static "today" map applied to a past date is survivorship-biased, which is
exactly why `membership_asof` returns *nothing* before the first snapshot rather than today's list.

Conventions inherited from D1: the module is offline by construction (it takes an already-wired
`Fetcher`, whose transport is the only socket), the clock is injected (B10), and joins are on ISIN
— the constituents file carries it natively, so nothing here touches a symbol as a key (#2).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.fetcher import (
    Fetcher,
    FetchError,
    FetchHTTPError,
    ForbiddenError,
    ForbiddenSpikeError,
    build_fetcher,
)
from dataplatform.ingest.indices import (
    CONSTITUENTS_SOURCE_ID,
    ConstituentSnapshot,
    ImmutableSnapshotError,
    SyncTracker,
    ingest_constituents,
    read_constituents_l1,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store

__all__ = [
    "DEFAULT_INDEX_SET",
    "CoverageReport",
    "IndexCategory",
    "IndexSpec",
    "ParkCause",
    "SlugOutcome",
    "SlugStatus",
    "render_coverage_markdown",
    "run_constituents_ingest",
]

_LOG = get_logger(__name__)

#: How many sectoral/thematic lists must land for the map to be "covered" (acceptance 2: "the
#: broad list plus at least 8 sectoral lists"). A threshold, not the whole set — the default set
#: is larger so a couple of gated slugs still clear the bar.
SECTORAL_COVERAGE_MIN: Final = 8


class IndexCategory(StrEnum):
    """What kind of list a slug names — the axis acceptance 2 counts along.

    `BROAD` is a market-wide list (NIFTY 500, NIFTY 50); `SECTORAL` is a single-sector list
    (Bank, IT, Pharma, …); `THEMATIC` is a cross-sector theme (Energy, Infrastructure, …).
    Sectoral and thematic both count toward the sectoral-coverage bar — they are the non-broad
    lists that carry a usable industry cut — while broad is counted on its own.
    """

    BROAD = "broad"
    SECTORAL = "sectoral"
    THEMATIC = "thematic"

    @property
    def is_broad(self) -> bool:
        return self is IndexCategory.BROAD


class ParkCause(StrEnum):
    """Why one slug could not be ingested — the enumerated cause acceptance 3 requires.

    Every parked slug carries exactly one of these, so an operator reading the coverage report
    knows whether to wait (a transient fetch failure), escalate (a session gate that needs a real
    browser handshake), or fix a bug (a parse failure on a format that changed):

    * ``GATED`` — the endpoint refused or answered with its Angular shell instead of the CSV: a
      403/403-spike, or the HTML soft-404 that wears a 200. This is the "session-gated" outcome
      §4.1 warns about; a retry with the same client will not change it.
    * ``FETCH_FAILED`` — an HTTP or transport error that is not a refusal (404, 5xx, timeout).
    * ``PARSE_FAILED`` — the bytes arrived but were not the five-column list (wrong header, bad
      ISIN, a company listed twice) — a real defect in the file or the parser, not a gate.
    * ``IMMUTABLE_CONFLICT`` — a stored month's membership would have been overwritten with
      different members (§4.1). The stored snapshot is kept; this one is refused.
    * ``UNKNOWN`` — anything the classifier did not recognise, surfaced rather than hidden.
    """

    GATED = "gated"
    FETCH_FAILED = "fetch_failed"
    PARSE_FAILED = "parse_failed"
    IMMUTABLE_CONFLICT = "immutable_conflict"
    UNKNOWN = "unknown"


class SlugStatus(StrEnum):
    """The outcome of one slug in one sweep."""

    PUBLISHED = "published"  # fetched and written to L1 this run
    SKIPPED = "skipped"  # already in L1 for this as_of — the resume path
    PARKED = "parked"  # could not be ingested; see the ParkCause


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """One index list to ingest — its lake slug, its human name, and what kind of list it is.

    What it assumes: `slug` is the niftyindices file slug (`ind_<slug>list.csv`, C.1) *and* the
    lake identifier `membership_asof(slug, …)` reads back — they are deliberately the same string,
    so there is one name for one index end to end.
    """

    slug: str
    name: str
    category: IndexCategory


@dataclass(frozen=True, slots=True)
class SlugOutcome:
    """What the sweep did with one slug — the row a test and an operator both assert against."""

    spec: IndexSpec
    status: SlugStatus
    rows: int = 0
    cause: ParkCause | None = None
    detail: str | None = None

    @property
    def covered(self) -> bool:
        """Is this slug's membership now queryable? — published this run or already in L1."""
        return self.status in (SlugStatus.PUBLISHED, SlugStatus.SKIPPED)


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """What one sweep produced — the per-slug outcomes and the coverage they add up to.

    `is_covered` is acceptance 2 as a property: a broad list *plus* at least
    `SECTORAL_COVERAGE_MIN` sectoral/thematic lists are queryable. The parked list is acceptance 3:
    every failure present with its enumerated cause, never a silent absence.
    """

    as_of: date
    outcomes: tuple[SlugOutcome, ...]

    @property
    def published(self) -> tuple[SlugOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status is SlugStatus.PUBLISHED)

    @property
    def skipped(self) -> tuple[SlugOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status is SlugStatus.SKIPPED)

    @property
    def parked(self) -> tuple[SlugOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status is SlugStatus.PARKED)

    @property
    def covered(self) -> tuple[SlugOutcome, ...]:
        """Every slug now queryable in L1 — published this run or skipped as already present."""
        return tuple(o for o in self.outcomes if o.covered)

    @property
    def broad_covered(self) -> int:
        return sum(1 for o in self.covered if o.spec.category.is_broad)

    @property
    def sectoral_covered(self) -> int:
        return sum(1 for o in self.covered if not o.spec.category.is_broad)

    @property
    def is_covered(self) -> bool:
        """Acceptance 2: at least one broad list and `SECTORAL_COVERAGE_MIN` sectoral/thematic."""
        return self.broad_covered >= 1 and self.sectoral_covered >= SECTORAL_COVERAGE_MIN


#: The default set of index lists the runner sweeps (C.1 `ind_<slug>list.csv`). Two broad lists and
#: a wide sectoral/thematic set, so the ~8-list coverage bar clears even if a couple of slugs are
#: gated on a given day. Slugs are the niftyindices file slugs, which are also the lake identifiers.
DEFAULT_INDEX_SET: Final[tuple[IndexSpec, ...]] = (
    IndexSpec("nifty500", "NIFTY 500", IndexCategory.BROAD),
    IndexSpec("nifty50", "NIFTY 50", IndexCategory.BROAD),
    IndexSpec("niftybank", "NIFTY BANK", IndexCategory.SECTORAL),
    IndexSpec("niftyit", "NIFTY IT", IndexCategory.SECTORAL),
    IndexSpec("niftyauto", "NIFTY AUTO", IndexCategory.SECTORAL),
    IndexSpec("niftypharma", "NIFTY PHARMA", IndexCategory.SECTORAL),
    IndexSpec("niftyfmcg", "NIFTY FMCG", IndexCategory.SECTORAL),
    IndexSpec("niftymetal", "NIFTY METAL", IndexCategory.SECTORAL),
    IndexSpec("niftyrealty", "NIFTY REALTY", IndexCategory.SECTORAL),
    IndexSpec("niftymedia", "NIFTY MEDIA", IndexCategory.SECTORAL),
    IndexSpec("niftypsubank", "NIFTY PSU BANK", IndexCategory.SECTORAL),
    IndexSpec("niftyprivatebank", "NIFTY PRIVATE BANK", IndexCategory.SECTORAL),
    IndexSpec("niftyfinance", "NIFTY FINANCIAL SERVICES", IndexCategory.SECTORAL),
    IndexSpec("niftyhealthcare", "NIFTY HEALTHCARE INDEX", IndexCategory.SECTORAL),
    IndexSpec("niftyconsumerdurables", "NIFTY CONSUMER DURABLES", IndexCategory.SECTORAL),
    IndexSpec("niftyenergy", "NIFTY ENERGY", IndexCategory.THEMATIC),
    IndexSpec("niftyinfra", "NIFTY INFRASTRUCTURE", IndexCategory.THEMATIC),
)


def run_constituents_ingest(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    as_of: date,
    specs: tuple[IndexSpec, ...] = DEFAULT_INDEX_SET,
    data_root: Path | None = None,
    register: SourceRegister | None = None,
) -> CoverageReport:
    """Sweep every configured index list into L1 for one `as_of`, and report the coverage.

    For each spec: if a snapshot already exists in L1 for `(slug, as_of)`, skip it (the resume
    path — no re-fetch); otherwise drive M3.9's single-index `ingest_constituents`
    (fetch → L0 → parse → L1 → sync). A slug that raises is *parked* with an enumerated
    `ParkCause` and the sweep continues — one gated list must not cost the rest of the map.

    Returns a `CoverageReport` however the sweep ends. Raises nothing for a per-slug failure; the
    only things that propagate are programmer errors (a bad `data_root`, a missing register entry),
    which are not a slug's fault.
    """
    outcomes: list[SlugOutcome] = []
    for spec in specs:
        existing = _existing_snapshot(spec.slug, as_of, data_root=data_root)
        if existing is not None:
            outcomes.append(
                SlugOutcome(spec=spec, status=SlugStatus.SKIPPED, rows=len(existing.rows))
            )
            _LOG.info(
                "constituents.skip_present",
                source=CONSTITUENTS_SOURCE_ID,
                index=spec.slug,
                as_of=as_of.isoformat(),
                rows=len(existing.rows),
                state="PUBLISHED",
            )
            continue
        try:
            snapshot = ingest_constituents(
                fetcher=fetcher,
                l0=l0,
                tracker=tracker,
                index_slug=spec.slug,
                index_name=spec.name,
                as_of=as_of,
                data_root=data_root,
                register=register,
            )
        except Exception as exc:  # one slug's failure parks it; the sweep goes on
            cause = _classify(exc)
            detail = f"{type(exc).__name__}: {exc}"
            outcomes.append(
                SlugOutcome(spec=spec, status=SlugStatus.PARKED, cause=cause, detail=detail)
            )
            _LOG.warning(
                "constituents.parked",
                source=CONSTITUENTS_SOURCE_ID,
                index=spec.slug,
                as_of=as_of.isoformat(),
                cause=str(cause),
                error=detail,
                state="PARKED",
            )
            continue
        outcomes.append(
            SlugOutcome(spec=spec, status=SlugStatus.PUBLISHED, rows=len(snapshot.rows))
        )

    report = CoverageReport(as_of=as_of, outcomes=tuple(outcomes))
    _LOG.info(
        "constituents.sweep_done",
        source=CONSTITUENTS_SOURCE_ID,
        as_of=as_of.isoformat(),
        published=len(report.published),
        skipped=len(report.skipped),
        parked=len(report.parked),
        broad_covered=report.broad_covered,
        sectoral_covered=report.sectoral_covered,
        is_covered=report.is_covered,
    )
    return report


def _existing_snapshot(
    slug: str, as_of: date, *, data_root: Path | None
) -> ConstituentSnapshot | None:
    """The stored `(slug, as_of)` snapshot, or `None` — the resume check keyed on the L1 artefact.

    Returns the snapshot object (whose `.rows` the caller counts) so a skip still reports coverage.
    Keyed on the artefact rather than the sync row because the constituents source id is shared by
    every slug, so a shared sync row cannot tell one slug's progress from another's.
    """
    try:
        return read_constituents_l1(slug, as_of, data_root=data_root)
    except FileNotFoundError:
        return None


def _classify(exc: BaseException) -> ParkCause:
    """Map a raised exception to the enumerated park cause an operator acts on.

    Order matters: `ForbiddenError` is a `FetchHTTPError`, and an HTML soft-404 is a `ParseError`
    whose message says so — both are *gates*, not ordinary fetch/parse failures, so they are
    matched first.
    """
    if isinstance(exc, ForbiddenSpikeError | ForbiddenError):
        return ParkCause.GATED
    if isinstance(exc, ImmutableSnapshotError):
        return ParkCause.IMMUTABLE_CONFLICT
    if isinstance(exc, ParseError):
        # The Angular shell answering a bad path with markup and a 200 is a session gate, not a
        # format defect — the parser flags it with this exact phrase (`_decode_csv`).
        if "markup, not CSV" in str(exc):
            return ParkCause.GATED
        return ParkCause.PARSE_FAILED
    if isinstance(exc, FetchHTTPError | FetchError):
        return ParkCause.FETCH_FAILED
    return ParkCause.UNKNOWN


def render_coverage_markdown(report: CoverageReport) -> str:
    """Render a `CoverageReport` as the operator-facing gate report (deliverable, §5 DoD).

    A table per outcome plus the coverage verdict, so the gate artefact is generated from the same
    object the runner returns rather than hand-kept in sync with it.
    """
    lines: list[str] = []
    lines.append("# M10.1 — index-constituents ingest coverage")
    lines.append("")
    lines.append(f"**As-of snapshot:** {report.as_of.isoformat()}")
    lines.append(
        f"**Coverage:** {report.broad_covered} broad + {report.sectoral_covered} "
        f"sectoral/thematic queryable "
        f"(bar: 1 broad + {SECTORAL_COVERAGE_MIN} sectoral) — "
        f"**{'PASS' if report.is_covered else 'BELOW BAR'}**"
    )
    lines.append(
        f"**Sweep:** {len(report.published)} published, {len(report.skipped)} skipped "
        f"(already in L1), {len(report.parked)} parked"
    )
    lines.append("")
    lines.append("| Slug | Index | Category | Status | Rows | Park cause |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for outcome in report.outcomes:
        cause = "" if outcome.cause is None else str(outcome.cause)
        rows = "" if outcome.rows == 0 and outcome.status is SlugStatus.PARKED else outcome.rows
        lines.append(
            f"| {outcome.spec.slug} | {outcome.spec.name} | {outcome.spec.category} "
            f"| {outcome.status} | {rows} | {cause} |"
        )
    if report.parked:
        lines.append("")
        lines.append("## Parked slugs")
        for outcome in report.parked:
            lines.append(f"- **{outcome.spec.slug}** ({outcome.cause}): {outcome.detail}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: sweep the configured index set for today's snapshot and print coverage.

    Wires the real networked fetcher, L0 store and Postgres sync state (mirroring `backfill.main`),
    commits after each slug so a kill loses at most the slug in flight, and writes the coverage
    report to `--report` when asked. Exit code is 0 when the coverage bar is met, 1 when it is not,
    so an orchestrator can gate on it.
    """
    parser = argparse.ArgumentParser(prog="constituents-ingest", description=__doc__)
    parser.add_argument(
        "--as-of",
        dest="as_of",
        type=date.fromisoformat,
        default=None,
        help="snapshot date (default: today, from the system clock)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="write the coverage report markdown to this path",
    )
    args = parser.parse_args(argv)

    settings: Settings = get_settings()
    clock: Clock = SystemClock()
    register = load_register()
    as_of = args.as_of if args.as_of is not None else clock.now().date()

    fetcher = build_fetcher(clock=clock, settings=settings, register=register)
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    calendar = trading_calendar()
    with connection(settings) as conn:
        sync = SyncStateStore(conn, clock=clock, calendar=calendar)
        report = _run_committing(
            fetcher=fetcher,
            l0=l0,
            sync=sync,
            commit=conn.commit,
            as_of=as_of,
            data_root=settings.data_root,
            register=register,
        )

    markdown = render_coverage_markdown(report)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(markdown, encoding="utf-8")
    print(
        f"constituents {as_of.isoformat()}: {len(report.published)} published, "
        f"{len(report.skipped)} skipped, {len(report.parked)} parked — "
        f"{report.broad_covered} broad + {report.sectoral_covered} sectoral "
        f"({'covered' if report.is_covered else 'below bar'})"
    )
    return 0 if report.is_covered else 1


def _run_committing(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    sync: SyncStateStore,
    commit: Callable[[], None],
    as_of: date,
    data_root: Path | None,
    register: SourceRegister | None,
) -> CoverageReport:
    """Run one slug at a time, committing the sync transaction after each so it is a checkpoint.

    `run_constituents_ingest` sweeps the whole set in one call; here each slug is its own call so
    the commit lands between slugs — the checkpoint the next run resumes from. `commit` is the
    connection's `commit` callable.
    """
    outcomes: list[SlugOutcome] = []
    for spec in DEFAULT_INDEX_SET:
        single = run_constituents_ingest(
            fetcher=fetcher,
            l0=l0,
            tracker=sync,
            as_of=as_of,
            specs=(spec,),
            data_root=data_root,
            register=register,
        )
        outcomes.extend(single.outcomes)
        commit()
    return CoverageReport(as_of=as_of, outcomes=tuple(outcomes))


if __name__ == "__main__":
    sys.exit(main())
