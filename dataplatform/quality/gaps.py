"""D7: the gap report — every date the platform owed data for, and why it does not have it.

The M1 gate says the gap report must explain **100% of missing days**. That is a claim about
absences, and absences are the one thing a database cannot show you: a date nobody ever fetched
has no `sync_state` row, so a query over the table alone can never see it. This module is the
join that makes the claim checkable — the C.2 trading calendar says which dates owed a file, the
D1 source register says which sources owed it and over what era, `sync_state` (D5) says what
happened to each attempt, and the L1 lake says whether the bytes survived.

Every `(source, date)` pair in scope comes out of `classify_pair` with exactly one `GapReason`,
and every reason is either **explained** (the exchange was shut, or that source's URL pattern did
not exist yet) or **UNEXPLAINED** — a day someone owes an answer for. `GapReport.fully_explained`
is the gate criterion, one boolean, and it is false whenever a single pair is unexplained.

Five ways a pair goes unexplained, and they are separate reasons because they need separate
fixes: no row at all (`NEVER_ATTEMPTED` — the runner never reached this date), a recorded failure
(`FAILED`), a row stuck mid-pipeline (`IN_PROGRESS`), a `GAP` on a date the calendar calls a
trading day (`GAP_ON_A_TRADING_DAY` — a real miss filed as a holiday, which is exactly the lie
the M1 gate exists to catch), and `PUBLISHED` with the L1 partition gone
(`L1_PARTITION_MISSING` — the state machine remembers a success whose data no longer exists).

The report **enumerates**; it does not summarise. A count of unexplained days tells an operator
nothing about which ones, so every entry carries the pair's full `sync_state` history — state,
attempts, retryable, last error, first attempt and last update — and the partition path when the
lake was the thing that came up short.

Two halves, as elsewhere in this codebase. `classify_pair` and `build_report` hold every rule and
take their facts as arguments — no database and no clock, so the whole classification is testable
offline. `GapScanner` gathers those facts from a live connection and the lake.
`/status/gaps?from=&to=` serves its answer.

**What it never does:** report an empty unexplained set as proof of completeness without saying
what it looked at. `GapReport` carries the sources examined, the pairs examined, and how many
`PUBLISHED` pairs it could *not* check against L1 — because "we found nothing wrong" and "we
checked nothing" must never render identically.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Final, Protocol

from dataplatform.ingest import source_register
from dataplatform.ingest.calendar import (
    CalendarCoverageError,
    DayKind,
    TradingCalendar,
    trading_calendar,
)
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncKey, SyncRecord, SyncState, SyncStateStore
from dataplatform.store.db import Connection
from dataplatform.store.paths import l0_dir, l1_partition_dir
from dataplatform.store.schemas import PRICES_RAW_DATASET

__all__ = [
    "PER_SESSION_CADENCES",
    "GapEntry",
    "GapReason",
    "GapReport",
    "GapReportError",
    "GapScanner",
    "L0Presence",
    "L1Check",
    "L1Presence",
    "L1Result",
    "LakeL0Presence",
    "LakeL1Presence",
    "SourceExpectation",
    "build_report",
    "classify_pair",
    "expectations_from_register",
]

_log = get_logger(__name__)


class GapReportError(Exception):
    """The report was asked for something it cannot answer — an inverted range, say."""


# ── the classification ───────────────────────────────────────────────────────────────────────


class GapReason(StrEnum):
    """Why one `(source, date)` pair has no usable data. Exactly one applies.

    The split that matters is `explained`: the first three are days nobody owed us anything for,
    and the rest are days somebody does. "100% of missing days explained" means every entry in a
    report falls in the first group.
    """

    WEEKEND = "WEEKEND"
    """Saturday or Sunday with no special session. The exchange was shut."""

    HOLIDAY = "HOLIDAY"
    """A weekday the exchange declared closed (C.2's holiday file)."""

    OUTSIDE_SOURCE_ERA = "OUTSIDE_SOURCE_ERA"
    """A trading day, but this source's URL pattern did not cover it.

    The legacy and UDiFF bhavcopies split on 2024-07-08; without this reason every session on the
    far side of a cutover would read as a missing day for the source that never served it.
    """

    NEVER_ATTEMPTED = "NEVER_ATTEMPTED"
    """Data was owed and there is no `sync_state` row at all — the runner never reached the date.

    The reason this module exists. A pure `sync_state` query cannot produce this entry, because
    the evidence for it is an absent row.
    """

    FAILED = "FAILED"
    """The attempt is recorded as FAILED. The entry carries attempts, retryable and last_error."""

    IN_PROGRESS = "IN_PROGRESS"
    """Stuck at PENDING/FETCHED/VALIDATED/NORMALIZED. Started, never finished, not retried."""

    GAP_ON_A_TRADING_DAY = "GAP_ON_A_TRADING_DAY"
    """Filed as an expected GAP on a date the calendar says the exchange published.

    `SyncStateStore.mark_gap` refuses to create this, so seeing it means either the holiday file
    was corrected after the row was written, or something wrote the row without going through the
    state machine. Either way a real miss is currently disguised as a holiday.
    """

    L1_PARTITION_MISSING = "L1_PARTITION_MISSING"
    """PUBLISHED, but the L1 partition holds no data. The record outlived the rows."""

    L0_PRESENT_L1_ABSENT = "L0_PRESENT_L1_ABSENT"
    """The attempt failed, but the payload it failed on is in L0 — a parse problem, not a fetch one.

    A narrower `FAILED`, and the distinction is the whole fix: bytes on disk mean the answer is a
    parser change plus a re-derive, at zero request cost, while no bytes mean a re-fetch behind
    whatever autonomy limit applies. 2020-07-13 and 2021-02-16 sat in the first class for months
    and nothing said which class they were in (audit finding N2).
    """

    OUTSIDE_CALENDAR = "OUTSIDE_CALENDAR"
    """A row exists for a date the C.2 holiday file makes no claim about.

    Only reachable for a unit row: the per-session walk is driven *by* the calendar and cannot
    leave it, but a filing carries whatever date its index entry stated. Unexplained on purpose —
    the honest answer is to extend the holiday file, not to guess whether the exchange was open.
    """

    @property
    def explained(self) -> bool:
        """Whether this reason means nothing was owed. False for every real miss."""
        return self in _EXPLAINED


#: The reasons that close a missing day honestly: the exchange was shut, or this source did not
#: serve that era. Everything else in `GapReason` is a day someone owes an answer for.
_EXPLAINED: Final[frozenset[GapReason]] = frozenset(
    {GapReason.WEEKEND, GapReason.HOLIDAY, GapReason.OUTSIDE_SOURCE_ERA}
)

#: `DayKind` → the reason a closed day is explained by. Sessions never appear here.
_CLOSED_DAY_REASON: Final[Mapping[DayKind, GapReason]] = {
    DayKind.WEEKEND: GapReason.WEEKEND,
    DayKind.HOLIDAY: GapReason.HOLIDAY,
}


# ── the L1 half ──────────────────────────────────────────────────────────────────────────────


class L1Check(StrEnum):
    """What looking for one L1 partition on disk established."""

    PRESENT = "PRESENT"
    """The partition directory exists and holds at least one non-empty file."""

    ABSENT = "ABSENT"
    """The dataset has partitions, but not this date's — a deletion or a normalisation that lied."""

    NO_DATASET = "NO_DATASET"
    """The dataset directory does not exist, so nothing has ever been normalised for this source.

    Reported rather than treated as ABSENT: before M1.8 runs there is no L1 at all, and turning
    that into one unexplained entry per published date would bury the real misses in noise. It is
    counted as `GapReport.l1_unchecked` so an empty report never reads as "L1 was verified".
    """


@dataclass(frozen=True, slots=True)
class L1Result:
    """The outcome of one partition lookup, with the path that was looked at.

    The path travels with the answer so an operator reading `L1_PARTITION_MISSING` is told which
    directory to go and look in, rather than having to reconstruct the layout by hand.
    """

    check: L1Check
    partition: Path


class L0Presence(Protocol):
    """Whether the payload an attempt failed on is still on disk. Injected, like `L1Presence`."""

    def holds(
        self,
        sources: Sequence[str],
        logical_date: date,
        l0_path: str | None,
        *,
        per_session: bool,
    ) -> bool:
        """Whether any payload for this pair is in L0. Never raises for an absent path."""
        ...


@dataclass(frozen=True, slots=True)
class LakeL0Presence:
    """`L0Presence` against the real lake (§4.2 layout, via `dataplatform.store.paths`).

    Two questions, in order. The `sync_state` row records the exact `l0_path` the fetch produced
    whenever the attempt got as far as FETCHED, and that is the authoritative answer. Only 1 of
    2,397 failed rows in this lake carries one — a parse that fails inside the same step never
    reaches `mark_fetched` — so there is a fallback, and it is deliberately narrow.

    **The fallback is for per-session sources only.** There, the date identifies the payload: one
    bhavcopy, one delivery file, and the source's month directory holding data for that date means
    this pair's bytes are there. For a unit-keyed source it means nothing — `nse_xbrl_filing`'s
    September-2019 directory holds thousands of documents, and "some document for that month is on
    disk" is not evidence about the one filing that failed. Answering yes there would tell an
    operator not to re-fetch the 774 filings whose documents were never fetched at all, which is
    the opposite of what they need to do.
    """

    data_root: Path | None = None

    def holds(
        self,
        sources: Sequence[str],
        logical_date: date,
        l0_path: str | None,
        *,
        per_session: bool,
    ) -> bool:
        """Whether a payload for this pair is in L0."""
        if l0_path:
            # `L0Ref.key` is the logical key `<source>/<iso-date>/<filename>`, not a disk path —
            # the lake shards by year and month. Rebuild the real path with the same function the
            # store writes through rather than reimplementing the layout here.
            filename = l0_path.rsplit("/", 1)[-1]
            if any(
                _is_file_with_data(
                    l0_dir(source, logical_date, data_root=self.data_root) / filename
                )
                for source in sources
            ):
                return True
        if not per_session:
            return False
        return any(
            _holds_data(l0_dir(source, logical_date, data_root=self.data_root))
            for source in sources
        )


class L1Presence(Protocol):
    """Whether a dataset/date partition holds data. Injected so the rules stay testable."""

    def check(self, dataset: str, logical_date: date) -> L1Result:
        """Look up one partition. Never raises for an absent path — absence is the answer."""
        ...


@dataclass(frozen=True, slots=True)
class LakeL1Presence:
    """`L1Presence` against the real lake (§4.2 layout, via `dataplatform.store.paths`).

    What it does: resolves `L1/<dataset>/date=<yyyy-mm-dd>/` and reports whether it holds bytes.
    What it assumes: a partition is one or more non-empty files. A zero-byte `part.parquet` is a
    failed write, not a partition, and is reported ABSENT — the M1 gate is about data existing,
    not about a file name existing.
    What it never does: read the parquet. Row-level validation is the sentinel's job, not the gap
    report's.
    """

    data_root: Path | None = None

    def check(self, dataset: str, logical_date: date) -> L1Result:
        """Whether `dataset`'s partition for `logical_date` holds data."""
        partition = l1_partition_dir(dataset, logical_date, data_root=self.data_root)
        if not partition.parent.is_dir():
            return L1Result(L1Check.NO_DATASET, partition)
        return L1Result(
            L1Check.PRESENT if _holds_data(partition) else L1Check.ABSENT,
            partition,
        )


def _is_file_with_data(path: Path) -> bool:
    """Whether `path` is a file with bytes in it. An empty file is a failed write, not a payload."""
    return path.is_file() and path.stat().st_size > 0


def _holds_data(partition: Path) -> bool:
    """True when the partition directory exists and contains at least one non-empty file."""
    if not partition.is_dir():
        return False
    return any(entry.is_file() and entry.stat().st_size > 0 for entry in partition.iterdir())


# ── what each source owes ────────────────────────────────────────────────────────────────────

#: Register cadences that owe a file for every date the exchange published on. Anything else
#: (`weekly`, `monthly`, `quarterly`, `per_filing`, ...) keeps its own schedule, and measuring it
#: against the trading calendar would manufacture a missing day for every session it never owed.
PER_SESSION_CADENCES: Final[frozenset[str]] = frozenset(
    {"daily", "backfill_only", "intraday_poll_at_eod"}
)


@dataclass(frozen=True, slots=True)
class SourceExpectation:
    """Which dates one source owes a file for, and where its rows land in L1.

    What it assumes: the register's `era` bounds are inclusive on both ends — the legacy and
    UDiFF bhavcopy entries both name 2024-07-08, the cutover session both patterns served.
    What it never does: shrink the expectation to what was fetched. This says what was *owed*, so
    that a source nobody ever ran still produces a report full of missing days.
    """

    source: str
    per_session: bool = True
    era_start: date | None = None
    era_end: date | None = None
    l1_dataset: str | None = None
    #: The register ids whose L0 trees hold this source's payloads, when they are not its own id.
    l0_sources: tuple[str, ...] = ()

    def l0_trees(self) -> tuple[str, ...]:
        """The L0 source trees a payload for this source could be in.

        A state-source name is not always an L0 tree name: the price core is checkpointed under
        `nse_bhavcopy` and lands in `nse_bhavcopy_legacy`/`nse_bhavcopy_udiff`, the same split that
        made `l1_dataset` necessary. Defaults to the source's own id, which is right for every
        source whose register id *is* its state name.
        """
        return self.l0_sources or (self.source,)

    def in_era(self, logical_date: date) -> bool:
        """Whether this source's URL pattern covered `logical_date`."""
        if self.era_start is not None and logical_date < self.era_start:
            return False
        return not (self.era_end is not None and logical_date > self.era_end)

    def era_text(self) -> str:
        """The era as one readable span, for the entry's `detail` line."""
        start = "the beginning" if self.era_start is None else self.era_start.isoformat()
        end = "now" if self.era_end is None else self.era_end.isoformat()
        return f"{start}..{end}"


#: The §4.1 rows whose parsed sessions all land in the single canonical L1 `prices_raw` dataset —
#: both exchanges and both eras, tagged by `exchange` (see `ingest.backfill.write_prices_raw`). The
#: L1 writer does *not* shard price data per source id, so a price source's presence must be probed
#: under `prices_raw`, not under its own id. Every other per-session source keeps its id as its
#: dataset name (the `/status/sync?dataset=` convention); an unknown directory reports `NO_DATASET`.
_PRICES_RAW_ROWS: Final[frozenset[str]] = frozenset(
    {
        "NSE equity OHLCV (≤ ~08-Jul-2024)",
        "NSE equity OHLCV (UDiFF, ≥ Jul-2024)",
        "BSE equity OHLCV",
    }
)

#: The cash price core is *ingested* under an era-independent source-set name, not under the
#: register's per-era ids: `sync_state` rows and the trading interlock are keyed on
#: `backfill.NSE_BHAVCOPY` / `BSE_BHAVCOPY` (see `backfill.FetchRequest.state_source`), and every
#: session — both eras, both exchanges — lands in the single `prices_raw` L1 dataset. The register,
#: meanwhile, splits each exchange into per-era entries. So the source the gap report actually scans
#: (`GapScanner` defaults to the sources `sync_state` holds rows for) is a name the register does
#: not contain, and without this it falls through to the loud default and probes
#: `data/L1/nse_bhavcopy/`, which never exists — the M1.14 defect. This maps each state-source name
#: to the register ids it aggregates; the union of their eras is the state-source's era, so a
#: pre-cutover BSE session (legacy era not yet wired) is not reported as owed. `test_gaps` pins
#: these names and constituents to `backfill.SOURCE_SETS` so they cannot drift.
#:
#: `nse_delivery` is here for the same reason and with the same resolution: M1.6 ingests the two
#: delivery eras (MTO before 2019-09-30, `sec_bhavdata_full` after) under one set name, and the
#: figures land as `deliv_qty`/`deliv_pct` columns *on* `prices_raw` rather than in a dataset of
#: their own. Without this row it fell through to the loud default and probed
#: `data/L1/nse_delivery/` — harmless while nothing scanned it, and a `NO_DATASET` on every one
#: of 2,471 published sessions the moment migration 0008 let the gap report see the source.
_PRICE_STATE_SOURCES: Final[dict[str, tuple[str, ...]]] = {
    "nse_bhavcopy": ("nse_bhavcopy_legacy", "nse_bhavcopy_udiff"),
    "bse_bhavcopy": ("bse_bhavcopy_udiff",),
    "nse_delivery": ("nse_mto", "nse_sec_bhavdata_full"),
}


#: Sources the register calls `daily` — truthfully, the endpoint answers for any date — but which
#: ingest fetches as dated *ranges*, checkpointing one `sync_state` row per range rather than per
#: session. `cadence` describes how often the source publishes; `per_session` asks whether ingest
#: owes a row every trading day, and for these two the answers differ.
#:
#: Without this, `nse_corp_actions` (11 rows spanning 2015..2026, one per yearly chunk) reports
#: NEVER_ATTEMPTED on all 2,461 sessions in the lake's range — 2,461 entries that are not misses,
#: burying the handful that are. Changing the register's `cadence` instead would be the wrong fix:
#: it would make the register lie about the feed to satisfy one of its readers.
_RANGE_FETCHED_SOURCES: Final[frozenset[str]] = frozenset({"nse_corp_actions", "bse_corp_actions"})


def expectations_from_register(
    register: source_register.SourceRegister | None = None,
) -> dict[str, SourceExpectation]:
    """Build the gap report's expectations from the D1 Source Register (C.1).

    What it does: reads each entry's `cadence` and `era` — the register is already the single
    place those facts live — and *additionally* emits an expectation under each era-independent
    price source-set name (`_PRICE_STATE_SOURCES`), because that is the name ingest tracks the
    price core under in `sync_state` and the name `GapScanner` therefore scans.
    What it assumes: a price source's rows land in the canonical `prices_raw` L1 dataset — both the
    per-era register ids (`_PRICES_RAW_ROWS`) and the aggregate state-source names resolve there.
    Every other per-session source's L1 dataset carries the source's own id, the convention
    `/status/sync?dataset=` uses. Probing the wrong directory reports `NO_DATASET` and is *counted
    as unchecked* — it never flags a missing partition — so a price source pointed at its own id
    (the pre-M1.14 bug) silently left every published day unverified.
    """
    loaded = source_register.load() if register is None else register
    expectations = {
        entry.id: SourceExpectation(
            source=entry.id,
            per_session=(
                entry.cadence in PER_SESSION_CADENCES and entry.id not in _RANGE_FETCHED_SOURCES
            ),
            era_start=entry.era.start,
            era_end=entry.era.end,
            l1_dataset=_l1_dataset_for(entry.plan_row, entry.id, entry.cadence),
        )
        for entry in loaded.sources
    }
    expectations.update(_price_state_expectations(loaded))
    return expectations


def _l1_dataset_for(plan_row: str, source_id: str, cadence: str) -> str | None:
    """The L1 dataset a source's partitions actually land in, or None if it owes no per-session L1.

    Price-OHLCV rows resolve to `prices_raw` (the writer's one dataset for both exchanges); other
    per-session sources keep their own id; non-per-session sources have no per-day L1 partition.
    """
    if cadence not in PER_SESSION_CADENCES:
        return None
    if plan_row in _PRICES_RAW_ROWS:
        return PRICES_RAW_DATASET
    return source_id


def _price_state_expectations(
    register: source_register.SourceRegister,
) -> dict[str, SourceExpectation]:
    """One expectation per era-independent price source-set name, resolving to `prices_raw`.

    The era is the union of the state-source's constituent register eras — contiguous by
    construction (legacy meets UDiFF at the cutover) — so the aggregate owes a file exactly on the
    sessions its wired eras covered and no earlier. A constituent the register does not carry is
    skipped, so a not-yet-wired era (BSE legacy) neither errors here nor manufactures owed days.
    """
    by_id = {entry.id: entry for entry in register.sources}
    out: dict[str, SourceExpectation] = {}
    for state_source, constituent_ids in _PRICE_STATE_SOURCES.items():
        eras = [by_id[cid].era for cid in constituent_ids if cid in by_id]
        if not eras:
            continue
        starts = [era.start for era in eras]
        ends = [era.end for era in eras]
        # A None bound on either side means "unbounded" and swallows the others: a source with an
        # open-ended era owes files to the range's edge. Otherwise the union is the widest span.
        era_start = None if any(s is None for s in starts) else min(s for s in starts if s)
        era_end = None if any(e is None for e in ends) else max(e for e in ends if e)
        out[state_source] = SourceExpectation(
            source=state_source,
            per_session=True,
            era_start=era_start,
            era_end=era_end,
            l1_dataset=PRICES_RAW_DATASET,
            l0_sources=tuple(cid for cid in constituent_ids if cid in by_id),
        )
    return out


@lru_cache(maxsize=1)
def _registered() -> tuple[SourceExpectation, ...]:
    """The register's expectations, parsed once per process."""
    return tuple(expectations_from_register().values())


def _expectation_for(
    source: str, expectations: Mapping[str, SourceExpectation]
) -> SourceExpectation:
    """The expectation for `source`, or the loud default for one the register does not know.

    The default treats an unknown source as owing a file every session, which is the answer that
    fails noisily. Assuming the opposite would let a source drop out of the register and take its
    missing days with it.
    """
    return expectations.get(source, SourceExpectation(source=source, l1_dataset=source))


# ── one pair's verdict ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GapEntry:
    """One `(source, date)` pair with no usable data, and the evidence for its reason.

    Carries the pair's whole `sync_state` history rather than a state name, because "M1.11
    enumerates every unexplained pair with its sync_state history" is the difference between a
    report an operator can act on and a number they have to go and re-derive.
    """

    source: str
    logical_date: date
    reason: GapReason
    detail: str
    day_kind: DayKind
    unit: str = ""
    state: SyncState | None = None
    attempts: int = 0
    retryable: bool | None = None
    last_error: str | None = None
    first_attempt_at: datetime | None = None
    updated_at: datetime | None = None
    l1_partition: str | None = None

    @property
    def explained(self) -> bool:
        """Whether this absence is accounted for. False means somebody owes an answer."""
        return self.reason.explained

    @property
    def qualified_source(self) -> str:
        """`source`, or `source/unit` for a row keyed on a filing, slug, chunk or scrip.

        What an operator has to type to find the row again, so it is what the report renders.
        """
        return self.source if not self.unit else f"{self.source}/{self.unit}"

    @property
    def key(self) -> tuple[date, str]:
        """Sort key: date first, so a report reads as a timeline."""
        return (self.logical_date, self.qualified_source)


def _entry(
    expectation: SourceExpectation,
    logical_date: date,
    day_kind: DayKind,
    reason: GapReason,
    detail: str,
    record: SyncRecord | None = None,
    l1: L1Result | None = None,
    unit: str = "",
) -> GapEntry:
    """Assemble an entry, folding in whatever history the pair has."""
    return GapEntry(
        source=expectation.source,
        unit=unit,
        logical_date=logical_date,
        reason=reason,
        detail=detail,
        day_kind=day_kind,
        state=None if record is None else record.state,
        attempts=0 if record is None else record.attempts,
        retryable=None if record is None else record.retryable,
        last_error=None if record is None else record.last_error,
        first_attempt_at=None if record is None else record.first_attempt_at,
        updated_at=None if record is None else record.updated_at,
        l1_partition=None if l1 is None else str(l1.partition),
    )


def classify_pair(
    expectation: SourceExpectation,
    logical_date: date,
    day_kind: DayKind,
    record: SyncRecord | None,
    *,
    l1: L1Result | None = None,
    l0_present: bool = False,
    unit: str = "",
) -> GapEntry | None:
    """Classify one `(source, date)` pair, or return None when there is nothing to explain.

    What it does: applies, in order, the four questions an operator would ask — is this pair in
    scope at all, is there a row, does the row say the data is there, and is the data actually
    there. The first one that produces an absence wins, and the reason names it.
    What it assumes: `day_kind` came from the C.2 calendar and `l1` from an `L1Presence` lookup
    for exactly this pair. Nothing here touches a disk, a database or a clock.
    What it never does: return None for a pair that owed data and does not have it. None means
    complete or out of scope, and out of scope is only ever reached with no row to contradict it.

    Returns None in exactly two situations: the pair is complete (PUBLISHED with its L1 partition
    present, or with no L1 dataset to check), or the source never owed anything for this date and
    nothing was recorded against it.

    `unit` names the sub-key when the row is one of many under its source — a filing, an index
    slug, a chunk, a scrip. It changes no rule here: a unit row is classified exactly as the
    source's own row would be, and the unit only travels onto the entry so the report can name
    the thing an operator has to go and look at.
    """
    verdict = _classify(expectation, logical_date, day_kind, record, l1=l1, l0_present=l0_present)
    if verdict is None or not unit:
        return verdict
    return replace(verdict, unit=unit)


def _classify(
    expectation: SourceExpectation,
    logical_date: date,
    day_kind: DayKind,
    record: SyncRecord | None,
    *,
    l1: L1Result | None = None,
    l0_present: bool = False,
) -> GapEntry | None:
    """`classify_pair` without the unit tagging — the rules themselves."""
    if record is None:
        return _absent(expectation, logical_date, day_kind)

    if record.state is SyncState.PUBLISHED:
        if l1 is not None and l1.check is L1Check.ABSENT:
            return _entry(
                expectation,
                logical_date,
                day_kind,
                GapReason.L1_PARTITION_MISSING,
                f"{expectation.source} is PUBLISHED for {logical_date.isoformat()} but the L1 "
                f"partition {l1.partition} holds no data — the sync_state row outlived its rows",
                record,
                l1,
            )
        return None

    if record.state is SyncState.GAP:
        if day_kind.expects_data:
            return _entry(
                expectation,
                logical_date,
                day_kind,
                GapReason.GAP_ON_A_TRADING_DAY,
                f"filed as an expected GAP, but the calendar calls {logical_date.isoformat()} a "
                f"{day_kind.value} — the exchange published, so this is a real miss in disguise",
                record,
            )
        return _entry(
            expectation,
            logical_date,
            day_kind,
            _CLOSED_DAY_REASON[day_kind],
            _closed_detail(logical_date, day_kind),
            record,
        )

    if record.state is SyncState.FAILED:
        return _entry(
            expectation,
            logical_date,
            day_kind,
            GapReason.L0_PRESENT_L1_ABSENT if l0_present else GapReason.FAILED,
            f"{record.attempts} attempt(s), "
            f"{'retryable' if record.retryable else 'not retryable'}: "
            f"{record.last_error or 'no error recorded'}"
            + (
                " — the payload is in L0, so this is a parser fix and a re-derive, not a re-fetch"
                if l0_present
                else ""
            ),
            record,
        )

    return _entry(
        expectation,
        logical_date,
        day_kind,
        GapReason.IN_PROGRESS,
        f"stuck at {record.state.value} since {record.updated_at.isoformat()} after "
        f"{record.attempts} attempt(s); the pipeline never reached PUBLISHED",
        record,
    )


def _absent(
    expectation: SourceExpectation, logical_date: date, day_kind: DayKind
) -> GapEntry | None:
    """Classify a pair with no `sync_state` row — the case only the calendar can explain."""
    if not day_kind.expects_data:
        if not expectation.per_session:
            return None
        return _entry(
            expectation,
            logical_date,
            day_kind,
            _CLOSED_DAY_REASON[day_kind],
            _closed_detail(logical_date, day_kind),
        )

    if not expectation.per_session:
        return None
    if not expectation.in_era(logical_date):
        return _entry(
            expectation,
            logical_date,
            day_kind,
            GapReason.OUTSIDE_SOURCE_ERA,
            f"{expectation.source} served {expectation.era_text()}; "
            f"{logical_date.isoformat()} lies outside it, so no file was ever owed",
        )
    return _entry(
        expectation,
        logical_date,
        day_kind,
        GapReason.NEVER_ATTEMPTED,
        f"{logical_date.isoformat()} is a {day_kind.value} and {expectation.source} has no "
        f"sync_state row for it — the date was never attempted",
    )


def _closed_detail(logical_date: date, day_kind: DayKind) -> str:
    """The one-line explanation for a day the exchange was shut."""
    shut = "a weekend" if day_kind is DayKind.WEEKEND else "a declared NSE holiday"
    return f"{logical_date.isoformat()} is {shut}; the exchange published nothing and none was owed"


# ── the report ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GapReport:
    """Every absence over a `(sources, date range)`, each with its reason.

    `entries` is the enumeration the M1 gate asks for: one per pair with no usable data, explained
    or not. Complete pairs are counted, not listed — a report that repeated 7,000 healthy days
    would bury the eleven that matter.
    """

    from_date: date
    to_date: date
    sources: tuple[str, ...]
    entries: tuple[GapEntry, ...]
    pairs_examined: int
    complete: int
    l1_unchecked: int = 0
    _by_reason: dict[GapReason, int] = field(default_factory=dict, compare=False, repr=False)

    @property
    def unexplained(self) -> tuple[GapEntry, ...]:
        """The pairs somebody owes an answer for. Empty is the M1 gate's pass condition."""
        return tuple(entry for entry in self.entries if not entry.explained)

    @property
    def explained(self) -> tuple[GapEntry, ...]:
        """Absences the calendar or the source register accounts for."""
        return tuple(entry for entry in self.entries if entry.explained)

    @property
    def fully_explained(self) -> bool:
        """True when every missing day in the range has an explained reason (the M1 criterion)."""
        return not self.unexplained

    def counts_by_reason(self) -> dict[GapReason, int]:
        """How many entries carry each reason, in `GapReason` declaration order."""
        return {
            reason: self._by_reason[reason] for reason in GapReason if reason in self._by_reason
        }

    def for_source(self, source: str) -> tuple[GapEntry, ...]:
        """Every entry for one source, in date order."""
        return tuple(entry for entry in self.entries if entry.source == source)

    def summary(self) -> str:
        """One line for a log, a runbook or the M1 backfill report."""
        detail = ", ".join(
            f"{reason.value}={count}" for reason, count in self.counts_by_reason().items()
        )
        unverified = (
            f"; L1 unverified for {self.l1_unchecked} published pair(s)"
            if self.l1_unchecked
            else ""
        )
        return (
            f"{self.from_date}..{self.to_date}: {len(self.sources)} source(s), "
            f"{self.pairs_examined} pair(s) — {self.complete} complete, "
            f"{len(self.explained)} explained, {len(self.unexplained)} UNEXPLAINED"
            f"{f' [{detail}]' if detail else ''}{unverified}"
        )


def build_report(
    from_date: date,
    to_date: date,
    *,
    sources: Sequence[str],
    records: Mapping[tuple[str, date], SyncRecord],
    unit_records: Sequence[SyncRecord] = (),
    calendar: TradingCalendar | None = None,
    expectations: Mapping[str, SourceExpectation] | None = None,
    l1_presence: L1Presence | None = None,
    l0_presence: L0Presence | None = None,
) -> GapReport:
    """Classify every `(source, date)` pair in an inclusive range.

    What it does: walks the calendar day by day, asks `classify_pair` for each source, and
    assembles the enumeration. The L1 lookup is the one thing done here rather than inside the
    classifier, so the classifier stays a pure function of already-gathered facts and this
    function is the only place that touches the injected `L1Presence`. Given the records, the
    calendar and the expectations, the result is a function of its arguments — no database and no
    clock — which is what lets the whole classification be tested offline.
    What it assumes: `records` holds every `sync_state` row for the range, keyed
    `(source, logical_date)`; a key that is absent means the pair genuinely has no row, which is
    what becomes `NEVER_ATTEMPTED`. `unit_records` holds the rows that carry a sub-key — one
    filing, index slug, chunk or scrip — and those are walked separately: a unit exists only
    because something discovered it, so its absence is not a question the calendar can ask and
    only its *state* can be wrong. Their sources still need to be in `sources` to be examined.
    What it never does: guess outside the calendar. A range that leaves C.2's coverage raises
    `CalendarCoverageError` from the calendar itself rather than resolving to "no holidays".

    Raises `GapReportError` on an inverted range.
    """
    if from_date > to_date:
        raise GapReportError(
            f"from={from_date.isoformat()} is after to={to_date.isoformat()}; "
            f"a gap report needs a range that runs forwards"
        )

    resolved_calendar = trading_calendar() if calendar is None else calendar
    resolved_expectations = (
        {entry.source: entry for entry in _registered()} if expectations is None else expectations
    )
    wanted = tuple(dict.fromkeys(sources))
    per_source = {source: _expectation_for(source, resolved_expectations) for source in wanted}

    entries: list[GapEntry] = []
    by_reason: dict[GapReason, int] = {}
    pairs = 0
    complete = 0
    l1_unchecked = 0

    for day, kind in resolved_calendar.days(from_date, to_date):
        for source in wanted:
            expectation = per_source[source]
            pairs += 1
            record = records.get((source, day))
            l1 = _l1_lookup(expectation, day, record, l1_presence)
            l0_present = _l0_lookup(expectation, day, record, l0_presence)
            claims_data = record is not None and record.state is SyncState.PUBLISHED
            if claims_data and (l1 is None or l1.check is L1Check.NO_DATASET):
                l1_unchecked += 1
            entry = classify_pair(expectation, day, kind, record, l1=l1, l0_present=l0_present)
            if entry is None:
                complete += int(claims_data)
                continue
            entries.append(entry)
            by_reason[entry.reason] = by_reason.get(entry.reason, 0) + 1

    wanted_set = set(wanted)
    for record in unit_records:
        key = SyncKey.parse(record.source)
        if key.base not in wanted_set:
            continue
        try:
            kind = resolved_calendar.classify(record.logical_date)
        except CalendarCoverageError:
            # A filing dated outside the holiday file's coverage is a real row we cannot classify
            # a day for. Counting it as examined and saying nothing would be the silent drop this
            # module exists to prevent, so it is reported with the reason that names the cause.
            pairs += 1
            entries.append(
                _entry(
                    per_source[key.base],
                    record.logical_date,
                    DayKind.SESSION,
                    GapReason.OUTSIDE_CALENDAR,
                    f"{record.logical_date.isoformat()} lies outside the C.2 calendar's coverage, "
                    f"so this row's day cannot be classified",
                    record,
                    unit=key.unit,
                )
            )
            by_reason[GapReason.OUTSIDE_CALENDAR] = by_reason.get(GapReason.OUTSIDE_CALENDAR, 0) + 1
            continue
        pairs += 1
        entry = classify_pair(
            per_source[key.base],
            record.logical_date,
            kind,
            record,
            l0_present=_l0_lookup(per_source[key.base], record.logical_date, record, l0_presence),
            unit=key.unit,
        )
        if entry is None:
            complete += int(record.state is SyncState.PUBLISHED)
            continue
        entries.append(entry)
        by_reason[entry.reason] = by_reason.get(entry.reason, 0) + 1

    entries.sort(key=lambda entry: entry.key)
    return GapReport(
        from_date=from_date,
        to_date=to_date,
        sources=wanted,
        entries=tuple(entries),
        pairs_examined=pairs,
        complete=complete,
        l1_unchecked=l1_unchecked,
        _by_reason=by_reason,
    )


def _l0_lookup(
    expectation: SourceExpectation,
    logical_date: date,
    record: SyncRecord | None,
    l0_presence: L0Presence | None,
) -> bool:
    """Whether this pair's payload is on disk, asked only when the answer changes the verdict.

    Only a FAILED pair's reason depends on it, so the probe is skipped for every other state —
    otherwise a decade of weekends would cost ten thousand pointless `stat` calls for an answer
    nothing reads.
    """
    if l0_presence is None or record is None or record.state is not SyncState.FAILED:
        return False
    return l0_presence.holds(
        expectation.l0_trees(),
        logical_date,
        record.l0_path,
        per_session=expectation.per_session,
    )


def _l1_lookup(
    expectation: SourceExpectation,
    logical_date: date,
    record: SyncRecord | None,
    l1_presence: L1Presence | None,
) -> L1Result | None:
    """Look the L1 partition up, but only for a pair whose row claims the data is there.

    A pair that is not PUBLISHED is already unexplained for a better reason, and stat-ing a
    partition for every weekend of a decade would be ten thousand pointless syscalls.
    """
    if l1_presence is None or expectation.l1_dataset is None:
        return None
    if record is None or record.state is not SyncState.PUBLISHED:
        return None
    return l1_presence.check(expectation.l1_dataset, logical_date)


# ── the live scanner ─────────────────────────────────────────────────────────────────────────


class GapScanner:
    """`build_report` against a live `sync_state` table and the real lake.

    What it does: reads every row in the range in one query, asks the pure rules, and returns the
    report. Backs `/status/gaps?from=&to=`.
    What it assumes: the caller owns the transaction and the schema is migrated. Nothing here
    writes or commits — the status surface is read-only by construction.
    What it never does: invent the source list. Asked for no sources it reports the ones
    `sync_state` has ever held a row for, and says so in `GapReport.sources`, because an empty
    report over an empty source list means "we track nothing", not "nothing is missing".
    """

    def __init__(
        self,
        conn: Connection,
        *,
        calendar: TradingCalendar | None = None,
        expectations: Mapping[str, SourceExpectation] | None = None,
        l1_presence: L1Presence | None = None,
        l0_presence: L0Presence | None = None,
    ) -> None:
        self._store = SyncStateStore(conn, calendar=calendar)
        self._calendar = self._store.calendar
        self._expectations = expectations
        self._l1_presence = LakeL1Presence() if l1_presence is None else l1_presence
        self._l0_presence: L0Presence = LakeL0Presence() if l0_presence is None else l0_presence

    def report(
        self, from_date: date, to_date: date, *, sources: Iterable[str] | None = None
    ) -> GapReport:
        """The gap report for an inclusive date range.

        `sources` defaults to every source `sync_state` has a row for anywhere in its history —
        the platform's own claim about what it ingests. Naming them explicitly is what the M1
        backfill report does, so a source that has never run once still gets its missing days
        enumerated instead of silently dropping out of the denominator.
        """
        wanted = tuple(sources) if sources is not None else self._store.tracked_sources()
        rows = self._store.rows_in_range(from_date, to_date, sources=wanted)
        # One query, split here rather than in SQL: a row that carries a sub-key is not a
        # (source, date) pair and must not land in the mapping, where it would either collide
        # with the source's own row or — since its key is the qualified name — be silently
        # unreachable. Migration 0008 is what makes the two distinguishable at all.
        keyed: dict[tuple[str, date], SyncRecord] = {}
        units: list[SyncRecord] = []
        for row in rows:
            key = SyncKey.parse(row.source)
            if key.unit:
                units.append(row)
            else:
                keyed[(key.base, row.logical_date)] = row
        report = build_report(
            from_date,
            to_date,
            sources=wanted,
            records=keyed,
            unit_records=units,
            calendar=self._calendar,
            expectations=self._expectations,
            l1_presence=self._l1_presence,
            l0_presence=self._l0_presence,
        )
        _log.info(
            "gaps.report",
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            sources=list(report.sources),
            pairs=report.pairs_examined,
            units=len(units),
            unexplained=len(report.unexplained),
            fully_explained=report.fully_explained,
        )
        return report
