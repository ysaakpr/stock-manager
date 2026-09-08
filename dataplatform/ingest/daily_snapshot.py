"""The daily snapshotter — the one job in this programme with a real deadline (OPS).

Every other source in the register has a past. These do not. `sec_list.csv`, `/api/reportASM`,
`/api/reportGSM`, `/api/reportESM`, `ind_niftytotalmarket_list.csv`, `EQUITY_L.csv`,
`symbolchange.csv`, the BSE list-of-scrips and the niftyindices constituent files all serve exactly
one snapshot — the current one. No date parameter, no archive host, no historical form. Their
history is therefore being destroyed at a rate of one day per day, and no amount of future money or
effort can buy back a day nobody captured (`ops/studies/multi-fund-data-study-2026-09-07.md`, wave
W8: *"None of these has a past"*).

Three things this module is built around, each of them a lesson already paid for:

* **The lake is absolute.** Two earlier workers fetched into their own worktree's `data/L0` and the
  payloads had to be moved across afterwards. A snapshot that lands in a disposable worktree is
  worse than no snapshot, because it looks done. So `run_daily_snapshot` resolves the L0 root and
  asserts it against `Settings.snapshot_expect_lake_root` *before the first request*, and refuses
  to fetch at all if they disagree.

* **HTTP 200 is not evidence of a fresh payload.** NSE answers 200 with the previous session's rows
  (`nse_sec_bhavdata_full` does exactly that on a market holiday). The ASM, GSM and ESM payloads
  each stamp *every* row with the file's own date — one distinct value per file, measured on the
  2026-09-08 probe — so staleness is detectable from the payload alone. A payload whose own date is
  not the capture date is recorded `STALE` with a FAILED sync row and an alert, never filed as
  today's data. The two CSVs carry no date at all, so their guard is structural (exact header, a
  floor on row count) — which is what catches the soft-404 the probe found on the `niftyindices.com`
  candidate: HTTP 200, plausible size, HTML body.

* **One source must not take the others down with it.** Each source is driven independently and its
  failure is journaled to `sync_state`, alerted, and left behind. Four sources capturing history
  beats five sources debated.

Keyed by **date**, and that is load-bearing: L0 is immutable three ways (mode 0o444,
refuse-overwrite, differing-bytes-at-one-key raises), so today's snapshot must not be able to
collide with yesterday's, and re-running the job on the same day must be a no-op rather than an
error. Every filename this module writes carries the capture date, and a source already `PUBLISHED`
for the date is re-reported from the bytes already in the lake without a single request.

Offline by construction (B8): `run_daily_snapshot` is the injectable core a test drives with a
`RecordedTransport`, a fake tracker and a `tmp_path` lake; `run_daily_snapshot_job` is the thin
scheduler entry point that builds the real networked wiring. The clock is injected (B10) and joins
remain on ISIN (#2) wherever a payload carries one.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from dataplatform.alerts import Alerter, AlertOutcome, Severity, build_alerter
from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.identity.ingest import (
    NSE_EQUITY_LIST_SOURCE,
    NSE_SYMBOL_CHANGES_SOURCE,
    equity_list_filename,
    symbol_changes_filename,
)
from dataplatform.ingest.calendar import DayKind, TradingCalendar, trading_calendar
from dataplatform.ingest.constituents_ingest import (
    DEFAULT_INDEX_SET,
    CoverageReport,
    IndexSpec,
    SlugOutcome,
    run_constituents_ingest,
)
from dataplatform.ingest.fetcher import Fetcher, leased_fetcher
from dataplatform.ingest.source_register import Source, SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncRecord, SyncState, SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Ref, L0Store

if TYPE_CHECKING:  # imported lazily by the registry to avoid a scheduler→ingest import cycle
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "DEFAULT_SNAPSHOT_SET",
    "DailySnapshotError",
    "DailySnapshotReport",
    "LakeRootMismatchError",
    "PayloadFacts",
    "SnapshotContentError",
    "SnapshotOutcome",
    "SnapshotSpec",
    "SnapshotStatus",
    "SnapshotTracker",
    "run_daily_snapshot",
    "run_daily_snapshot_job",
]

_LOG = get_logger(__name__)

#: How the surveillance endpoints stamp their own date: `08-Sep-2026`, and GSM prefixes a clock
#: time to it. Parsed with an explicit format rather than a fuzzy parser — a stamp we cannot read
#: must be an error, because "unreadable" silently becomes "no staleness check" otherwise.
_NSE_STAMP_FORMAT: Final = "%d-%b-%Y"


class SnapshotStatus(StrEnum):
    """What the snapshotter did with one source on one date.

    Five outcomes and no sixth, because every one of them needs a different response from an
    operator and lumping any two together is how a real failure becomes a log line nobody reads.
    """

    CAPTURED = "CAPTURED"
    """Fetched today, structurally sound, and the payload's own date is the capture date."""

    REUSED = "REUSED"
    """L0 already held this date's payload, so no request was made. The idempotent path."""

    STALE = "STALE"
    """HTTP 200 carrying a payload whose own date is *not* the capture date.

    The failure NSE actually commits. The bytes are kept — L0 records what the source served on
    the day it served it, and that record is the evidence — but the `sync_state` row is FAILED and
    an alert goes out, so nothing downstream can read a previous session's list as today's.
    """

    MALFORMED = "MALFORMED"
    """HTTP 200 carrying something that is not the shape the register recorded.

    A soft 404 is the common case: the probe caught `niftyindices.com` answering 200 with 78 KB of
    the site's HTML shell for a CSV URL. Kept, journaled FAILED, alerted.
    """

    FAILED = "FAILED"
    """No usable response at all — a 4xx, an exhausted retry budget, a hard stop, a bad key."""

    @property
    def healthy(self) -> bool:
        """Whether this date's snapshot is one a reader may trust. Only the first two are."""
        return self in (SnapshotStatus.CAPTURED, SnapshotStatus.REUSED)


class SnapshotContentError(RuntimeError):
    """An inspector refused a payload: it is not the shape the register's `parse_check` records."""


class LakeRootMismatchError(RuntimeError):
    """The L0 root the code resolved is not the one the operator declared. Nothing was fetched.

    Raised before the first request, deliberately. This has gone wrong twice: a worker fetched into
    its own git worktree's `data/L0` and the payloads had to be transferred across afterwards. A
    snapshot in a disposable directory is worse than no snapshot because it looks done — and the
    cure is to fix the invocation, never to relax the assertion.
    """


class DailySnapshotError(RuntimeError):
    """Not one source landed a healthy snapshot for the date — a total outage.

    Raised only when *every* configured source is degraded, so the scheduler records the run FAILED
    and the next day's run is the self-heal. One source failing is not this: it is journaled,
    alerted and left behind while the rest of the sweep goes on.
    """


@dataclass(frozen=True, slots=True)
class PayloadFacts:
    """What an inspector could establish about a payload, without parsing it into rows.

    `content_date` is the payload's *own* claim about which day it describes, or `None` for a
    payload that makes no such claim. `None` is not a pass — it means the staleness check does not
    apply to this source and the structural check is the only guard there is.
    """

    rows: int
    content_date: date | None = None
    note: str = ""


#: Reads a payload and either returns what it established or raises `SnapshotContentError`.
Inspector = Callable[[bytes], PayloadFacts]


@dataclass(frozen=True, slots=True)
class SnapshotSpec:
    """One source the snapshotter captures, and how to tell a good payload from a bad one.

    What it assumes: `source_id` is a Source Register id — the URL, headers, host policy and
    spacing all come from the register, never from here.
    What it never does: name its own URL. A second place that knows an endpoint is a second
    endpoint the day one of them changes.

    `filename` takes the capture date because it must: L0 partitions by month, so an undated
    filename is one key for every date in that month (`L0Store.put`).
    """

    source_id: str
    filename: Callable[[date], str]
    inspect: Inspector
    description: str = ""


@dataclass(frozen=True, slots=True)
class SnapshotOutcome:
    """What happened to one source on one date — the row a test and an operator both read."""

    source_id: str
    status: SnapshotStatus
    ref: L0Ref | None = None
    rows: int = 0
    content_date: date | None = None
    detail: str = ""

    @property
    def healthy(self) -> bool:
        """Whether this source's snapshot for the date is trustworthy."""
        return self.status.healthy


@dataclass(frozen=True, slots=True)
class DailySnapshotReport:
    """One day's sweep: what landed, what did not, and what it cost in requests."""

    as_of: date
    day_kind: DayKind
    lake_root: Path
    outcomes: tuple[SnapshotOutcome, ...] = ()
    constituents: CoverageReport | None = None
    requests_spent: int = 0
    alerts_sent: int = 0
    #: True when the sweep was skipped because the exchange was shut. Not a failure — every
    #: source is filed `GAP` for the date, which is what makes a *missed* day distinguishable
    #: from a day nothing was owed on.
    skipped_closed: bool = False

    @property
    def captured(self) -> tuple[SnapshotOutcome, ...]:
        """Sources fetched fresh on this run."""
        return tuple(o for o in self.outcomes if o.status is SnapshotStatus.CAPTURED)

    @property
    def reused(self) -> tuple[SnapshotOutcome, ...]:
        """Sources the lake already held for the date — the zero-request path."""
        return tuple(o for o in self.outcomes if o.status is SnapshotStatus.REUSED)

    @property
    def degraded(self) -> tuple[SnapshotOutcome, ...]:
        """Sources that did not land a trustworthy snapshot: STALE, MALFORMED or FAILED."""
        return tuple(o for o in self.outcomes if not o.healthy)

    @property
    def healthy(self) -> tuple[SnapshotOutcome, ...]:
        """Sources whose snapshot for the date a reader may trust."""
        return tuple(o for o in self.outcomes if o.healthy)

    @property
    def closed(self) -> bool:
        """Whether the sweep was skipped because the exchange was shut. Not a failure."""
        return self.skipped_closed

    def summary(self) -> str:
        """One line for a log, a runbook or a commit message."""
        if self.closed:
            return (
                f"daily snapshot {self.as_of.isoformat()}: skipped, "
                f"{self.day_kind.value.lower()} — nothing was owed"
            )
        slugs = "" if self.constituents is None else f", {len(self.constituents.covered)} slugs"
        return (
            f"daily snapshot {self.as_of.isoformat()}: {len(self.captured)} captured, "
            f"{len(self.reused)} reused, {len(self.degraded)} degraded{slugs}, "
            f"{self.requests_spent} request(s)"
        )


class SnapshotTracker(Protocol):
    """The slice of the §4.4 state machine this job drives.

    A protocol rather than the concrete `SyncStateStore` so a sweep can be driven end to end with
    no Postgres anywhere near it (B8); the store satisfies it structurally. `get` is part of the
    contract because idempotence depends on it — a source already `PUBLISHED` for the date must not
    be `begin()`-ed again, since `PUBLISHED` has no outgoing edge and the call would raise.
    """

    def get(self, source: str, logical_date: date) -> SyncRecord | None: ...

    def begin(self, source: str, logical_date: date) -> SyncRecord: ...

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> SyncRecord: ...

    def mark_validated(self, source: str, logical_date: date) -> SyncRecord: ...

    def mark_normalized(self, source: str, logical_date: date) -> SyncRecord: ...

    def mark_published(self, source: str, logical_date: date) -> SyncRecord: ...

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> SyncRecord: ...

    def mark_gap(self, source: str, logical_date: date) -> SyncRecord: ...


# ── inspectors: what a good payload looks like, per source ───────────────────────────────────


def _decode(payload: bytes, *, source: str) -> str:
    """Payload as text, or a `SnapshotContentError` naming the source.

    A body that is not UTF-8 where the register says `text/csv` is not something to salvage with a
    replacement character — it is the wrong body, and guessing would hide that.
    """
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotContentError(
            f"{source}: payload is not UTF-8 ({exc}); the register records a text payload, so "
            f"this is a different body, not a decoding problem"
        ) from exc


def _csv_inspector(*, source: str, header: str, min_rows: int) -> Inspector:
    """An inspector for a headed CSV: the exact header line, and a floor on data rows.

    The header is compared verbatim because that is what the register's `parse_check` records, and
    because an HTML soft 404 fails it on the first line — which is the whole point. The row floor
    catches the other 200-shaped failure: a real CSV that has been truncated to its header.
    """

    def inspect(payload: bytes) -> PayloadFacts:
        text = _decode(payload, source=source)
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise SnapshotContentError(f"{source}: empty payload, {len(payload)} byte(s)")
        if lines[0].strip() != header:
            raise SnapshotContentError(
                f"{source}: header is {lines[0].strip()[:120]!r}, expected {header!r} — this is "
                f"not the file the register describes (a 200 carrying an error page looks exactly "
                f"like this)"
            )
        rows = len(lines) - 1
        if rows < min_rows:
            raise SnapshotContentError(
                f"{source}: {rows} data row(s) under a floor of {min_rows}; the header parsed, so "
                f"the payload is the right file truncated, not the wrong file"
            )
        return PayloadFacts(rows=rows, note=f"{rows} rows")

    return inspect


def _headerless_csv_inspector(*, source: str, columns: int, min_rows: int) -> Inspector:
    """An inspector for a CSV whose first line is already data (`symbolchange.csv`).

    There is no header to compare, so the shape check is the column count on the first line —
    which an HTML body fails, since its first line has no commas in the right number.
    """

    def inspect(payload: bytes) -> PayloadFacts:
        text = _decode(payload, source=source)
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) < min_rows:
            raise SnapshotContentError(f"{source}: {len(lines)} row(s) under a floor of {min_rows}")
        found = lines[0].count(",") + 1
        if found != columns:
            raise SnapshotContentError(
                f"{source}: first line has {found} column(s), expected {columns}: "
                f"{lines[0][:120]!r}"
            )
        return PayloadFacts(rows=len(lines), note=f"{len(lines)} rows, headerless")

    return inspect


def _load_json(payload: bytes, *, source: str) -> object:
    """Payload as JSON, or a `SnapshotContentError`. An HTML soft 404 dies here."""
    try:
        return json.loads(_decode(payload, source=source))
    except json.JSONDecodeError as exc:
        raise SnapshotContentError(
            f"{source}: payload is not JSON ({exc}); the register records a JSON payload, so a "
            f"200 here is an error page, not data"
        ) from exc


def _nse_stamp(value: object, *, source: str, key: str) -> date:
    """One NSE surveillance row's own date stamp, as a `date`.

    GSM prefixes a clock time (`08-Sep-2026 08:07:02`), the others do not, so the date part is
    taken by splitting on whitespace rather than by a second format string. An unparseable stamp
    raises: a stamp we cannot read is a staleness check we do not have, and silently returning
    `None` for it would turn the guard off exactly when the payload has changed shape.
    """
    if not isinstance(value, str) or not value.strip():
        raise SnapshotContentError(f"{source}: row has no usable {key!r} stamp, got {value!r}")
    head = value.split()[0]
    try:
        return datetime.strptime(head, _NSE_STAMP_FORMAT).date()
    except ValueError as exc:
        raise SnapshotContentError(
            f"{source}: {key}={value!r} is not {_NSE_STAMP_FORMAT!r}; the stamp is what makes a "
            f"stale 200 detectable, so an unreadable one is a break, not a missing field"
        ) from exc


def _stamped_rows(
    rows: Sequence[object], *, source: str, key: str, isin_key: str = "isin"
) -> tuple[int, date]:
    """Row count and the single date every row stamps itself with.

    The 2026-09-08 probe measured one distinct stamp per file across ASM (138 + 84), GSM (75) and
    ESM (274) — the stamp is the *file's* date, not each name's entry date, which is what makes it
    a freshness signal. More than one distinct value would mean that assumption no longer holds, so
    it raises rather than picking a winner: a wrong staleness verdict is worse than a loud one.
    """
    stamps: set[date] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise SnapshotContentError(
                f"{source}: row {index} is {type(row).__name__}, not an object"
            )
        if isin_key not in row:
            raise SnapshotContentError(
                f"{source}: row {index} has no {isin_key!r} — ISIN is the only join key (#2)"
            )
        stamps.add(_nse_stamp(row.get(key), source=source, key=key))
    if len(stamps) != 1:
        raise SnapshotContentError(
            f"{source}: {len(stamps)} distinct {key!r} values in one payload "
            f"({sorted(d.isoformat() for d in stamps)}); the register records one stamp per file, "
            f"so this endpoint has changed what {key!r} means and the staleness check must be "
            f"re-derived before it can be trusted"
        )
    return len(rows), stamps.pop()


def _asm_inspector(payload: bytes) -> PayloadFacts:
    """`/api/reportASM`: an object of `longterm` and `shortterm`, each `{"data": [...]}`."""
    source = "nse_asm_list"
    document = _load_json(payload, source=source)
    if not isinstance(document, Mapping):
        raise SnapshotContentError(
            f"{source}: payload is a {type(document).__name__}, expected an object with "
            f"'longterm' and 'shortterm'"
        )
    total = 0
    stamps: set[date] = set()
    for section in ("longterm", "shortterm"):
        block = document.get(section)
        if not isinstance(block, Mapping) or not isinstance(block.get("data"), list):
            raise SnapshotContentError(f"{source}: {section!r} is not an object carrying 'data'")
        rows = block["data"]
        if not rows:
            raise SnapshotContentError(
                f"{source}: {section!r} is empty. An empty surveillance list is not impossible, "
                f"but it is indistinguishable from a broken endpoint, so it is refused rather "
                f"than filed as 'nobody is under ASM today'"
            )
        count, stamp = _stamped_rows(rows, source=source, key="asmTime")
        total += count
        stamps.add(stamp)
    if len(stamps) != 1:
        raise SnapshotContentError(
            f"{source}: longterm and shortterm disagree on the file date "
            f"({sorted(d.isoformat() for d in stamps)})"
        )
    return PayloadFacts(rows=total, content_date=stamps.pop(), note=f"{total} rows over 2 sections")


def _flat_stamped_json_inspector(*, source: str, key: str, min_rows: int) -> Inspector:
    """An inspector for a flat JSON array of self-stamped rows (GSM, ESM)."""

    def inspect(payload: bytes) -> PayloadFacts:
        document = _load_json(payload, source=source)
        if not isinstance(document, list):
            raise SnapshotContentError(
                f"{source}: payload is a {type(document).__name__}, expected a JSON array"
            )
        if len(document) < min_rows:
            raise SnapshotContentError(
                f"{source}: {len(document)} row(s) under a floor of {min_rows}; an empty or nearly "
                f"empty surveillance list is indistinguishable from a broken endpoint"
            )
        rows, stamp = _stamped_rows(document, source=source, key=key)
        return PayloadFacts(rows=rows, content_date=stamp, note=f"{rows} rows")

    return inspect


def _bse_scrip_master_inspector(payload: bytes) -> PayloadFacts:
    """BSE list-of-scrips: a JSON array carrying `ISIN_NUMBER` and `INDUSTRY` per scrip.

    The BSE half of the classification snapshot — wider coverage than the NSE file (it includes
    every active scrip, not the broad-market 750) in BSE's own taxonomy, which is a different
    classification and so a genuine second opinion rather than a fallback.
    """
    source = "bse_scrip_master"
    document = _load_json(payload, source=source)
    if not isinstance(document, list) or not document:
        raise SnapshotContentError(
            f"{source}: expected a non-empty JSON array, got a "
            f"{type(document).__name__} — an empty scrip master is a broken response, not news"
        )
    first = document[0]
    if not isinstance(first, Mapping):
        raise SnapshotContentError(f"{source}: row 0 is a {type(first).__name__}, not an object")
    for required in ("ISIN_NUMBER", "INDUSTRY", "SCRIP_CD"):
        if required not in first:
            raise SnapshotContentError(
                f"{source}: row 0 has no {required!r}; keys are {sorted(first)[:12]}"
            )
    if len(document) < 3_000:
        raise SnapshotContentError(
            f"{source}: {len(document)} scrip(s) under a floor of 3,000 — the active equity list "
            f"has been ~4,900 since the register was swept, so this is a truncated response"
        )
    return PayloadFacts(rows=len(document), note=f"{len(document)} active scrips")


#: The archive-only half of the snapshot set: one entry per payload, each keyed by the capture
#: date. The index-constituents sweep is *not* here — it has its own runner (M10.1), writes L1 and
#: parks per slug, so it is driven as one step beside these rather than pretended to be one of them.
#:
#: `nse_equity_list` and `nse_symbol_changes` are in this daily set even though their register
#: cadence says weekly and `identity_refresh` (Saturdays) also fetches them. Deliberate: they are
#: the identity spine, a delisted company vanishes from `EQUITY_L.csv` the day it dies, and both
#: jobs now land on the same date-keyed L0 filenames — so whichever runs first does the fetch and
#: the other reuses it for nothing.
DEFAULT_SNAPSHOT_SET: Final[tuple[SnapshotSpec, ...]] = (
    SnapshotSpec(
        source_id="nse_industry_classification",
        filename=lambda on: f"ind_niftytotalmarket_list_{on:%Y%m%d}.csv",
        inspect=_csv_inspector(
            source="nse_industry_classification",
            header="Company Name,Industry,Symbol,Series,ISIN Code",
            min_rows=500,
        ),
        description="NSE Indices company→industry assignment for the broad market",
    ),
    SnapshotSpec(
        source_id="nse_price_bands",
        filename=lambda on: f"sec_list_{on:%Y%m%d}.csv",
        inspect=_csv_inspector(
            source="nse_price_bands",
            header="Symbol,Series,Security Name,Band,Remarks",
            min_rows=2_000,
        ),
        description="NSE operative per-security price band",
    ),
    SnapshotSpec(
        source_id="nse_asm_list",
        filename=lambda on: f"reportASM_{on:%Y%m%d}.json",
        inspect=_asm_inspector,
        description="NSE Additional Surveillance Measure, long- and short-term",
    ),
    SnapshotSpec(
        source_id="nse_gsm_list",
        filename=lambda on: f"reportGSM_{on:%Y%m%d}.json",
        inspect=_flat_stamped_json_inspector(source="nse_gsm_list", key="gsmTime", min_rows=10),
        description="NSE Graded Surveillance Measure",
    ),
    SnapshotSpec(
        source_id="nse_esm_list",
        filename=lambda on: f"reportESM_{on:%Y%m%d}.json",
        inspect=_flat_stamped_json_inspector(source="nse_esm_list", key="esmTime", min_rows=10),
        description="NSE Enhanced Surveillance Measure",
    ),
    SnapshotSpec(
        source_id=NSE_EQUITY_LIST_SOURCE,
        filename=equity_list_filename,
        inspect=_csv_inspector(
            source=NSE_EQUITY_LIST_SOURCE,
            header=(
                "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT,"
                " ISIN NUMBER, FACE VALUE"
            ),
            min_rows=1_500,
        ),
        description="NSE equity list — the symbol→ISIN spine",
    ),
    SnapshotSpec(
        source_id=NSE_SYMBOL_CHANGES_SOURCE,
        filename=symbol_changes_filename,
        inspect=_headerless_csv_inspector(
            source=NSE_SYMBOL_CHANGES_SOURCE, columns=4, min_rows=1_000
        ),
        description="NSE published symbol-change history",
    ),
    SnapshotSpec(
        source_id="bse_scrip_master",
        filename=lambda on: f"ListofScripData_Active_{on:%Y%m%d}.json",
        inspect=_bse_scrip_master_inspector,
        description="BSE list-of-scrips — scrip→ISIN with BSE's own INDUSTRY",
    ),
)


# ── the sweep ────────────────────────────────────────────────────────────────────────────────


def _entry(source_id: str, register: SourceRegister) -> Source:
    """The register row for a source id, or a loud failure naming it."""
    entry = next((row for row in register.sources if row.id == source_id), None)
    if entry is None:
        raise KeyError(f"source register has no row for {source_id!r}")
    return entry


def run_daily_snapshot(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SnapshotTracker,
    alerter: Alerter,
    as_of: date,
    calendar: TradingCalendar,
    commit: Callable[[], None] = lambda: None,
    specs: Sequence[SnapshotSpec] = DEFAULT_SNAPSHOT_SET,
    register: SourceRegister | None = None,
    constituents: Callable[[date], CoverageReport] | None = None,
    expect_lake_root: Path | None = None,
) -> DailySnapshotReport:
    """One day's snapshot sweep — the injectable core, offline-testable end to end.

    What it does: asserts the lake root before anything else, files a `GAP` per source and returns
    if the exchange was shut, then drives each source independently — reusing a payload the lake
    already holds for the date without a request, inspecting every payload against the shape the
    register records, comparing a self-dated payload's own date to the capture date, and journaling
    the result to `sync_state` — committing after each so a kill loses at most the source in flight.
    A degraded source is alerted once and left behind; the sweep goes on.
    What it assumes: `as_of` is the capture date, `calendar` covers it, and the caller owns the
    transaction (`commit` is injected, as everywhere else in this codebase).
    What it never does: fetch before the lake root is proven, file a payload whose own date is not
    the capture date as that date's data, abort the sweep because one source broke, or make a
    request for a date already `PUBLISHED`.

    Raises `LakeRootMismatchError` before the first request when the resolved root is not
    `expect_lake_root`, and `DailySnapshotError` when *no* source landed a healthy snapshot.
    """
    loaded = load_register() if register is None else register
    root = l0.root.resolve()
    _LOG.info(
        "snapshot.lake_root_resolved",
        as_of=as_of.isoformat(),
        lake_root=str(root),
        expected=None if expect_lake_root is None else str(expect_lake_root),
        state="RESOLVED",
    )
    if expect_lake_root is not None and root != expect_lake_root.resolve():
        raise LakeRootMismatchError(
            f"L0 resolved to {root} but the operator declared {expect_lake_root.resolve()}; "
            f"nothing was fetched. A snapshot that lands outside the authoritative lake is worse "
            f"than no snapshot, because it looks done. Fix the invocation (DATA_ROOT), not this "
            f"assertion."
        )

    day_kind = calendar.classify(as_of)
    if not day_kind.expects_data:
        for spec in specs:
            tracker.mark_gap(spec.source_id, as_of)
        commit()
        report = DailySnapshotReport(
            as_of=as_of, day_kind=day_kind, lake_root=root, skipped_closed=True
        )
        _LOG.info(
            "snapshot.skipped_closed",
            as_of=as_of.isoformat(),
            day_kind=day_kind.value,
            sources=len(specs),
            state="GAP",
        )
        return report

    outcomes: list[SnapshotOutcome] = []
    requests = 0
    for spec in specs:
        outcome, spent = _snapshot_one(
            spec, fetcher=fetcher, l0=l0, tracker=tracker, as_of=as_of, register=loaded
        )
        outcomes.append(outcome)
        requests += spent
        commit()

    coverage = None if constituents is None else constituents(as_of)
    commit()

    report = DailySnapshotReport(
        as_of=as_of,
        day_kind=day_kind,
        lake_root=root,
        outcomes=tuple(outcomes),
        constituents=coverage,
        requests_spent=requests,
    )
    alerts = _alert_degraded(report, alerter)
    commit()
    report = replace(report, alerts_sent=alerts)
    _LOG.info(
        "snapshot.sweep_done",
        as_of=as_of.isoformat(),
        captured=len(report.captured),
        reused=len(report.reused),
        degraded=len(report.degraded),
        requests=report.requests_spent,
        alerts_sent=alerts,
        state="DONE" if not report.degraded else "DEGRADED",
    )
    if not report.healthy:
        raise DailySnapshotError(
            f"daily snapshot for {as_of.isoformat()} landed no healthy source at all "
            f"({len(report.degraded)} degraded); recorded FAILED for the next run to self-heal. "
            f"See the per-source alerts and /status/sync."
        )
    return report


def _snapshot_one(
    spec: SnapshotSpec,
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SnapshotTracker,
    as_of: date,
    register: SourceRegister,
) -> tuple[SnapshotOutcome, int]:
    """Drive one source for one date. Returns its outcome and the requests it spent (0 or 1).

    Every exit path writes a `sync_state` row, because a source that broke has to reach the status
    API rather than a log line nobody reads (CLAUDE.md) — and this job runs unattended forever, so
    a silent failure is the worst outcome available to it.
    """
    source = spec.source_id
    filename = spec.filename(as_of)
    existing = tracker.get(source, as_of)

    # Already published for this date: the idempotent path. No `begin()` — PUBLISHED has no
    # outgoing edge and the call would raise — no request, and no state write. The payload is
    # re-read out of L0 (which re-verifies its checksum) purely so the report can describe it.
    if existing is not None and existing.state is SyncState.PUBLISHED:
        ref = l0.ref_for(source, as_of, filename)
        try:
            facts = spec.inspect(l0.get(ref))
        except (SnapshotContentError, OSError) as exc:
            _LOG.warning(
                "snapshot.reuse_unreadable",
                source=source,
                logical_date=as_of.isoformat(),
                l0_key=ref.key,
                error=str(exc),
                state="REUSED",
            )
            facts = PayloadFacts(rows=0, note="already published; payload not re-readable")
        _LOG.info(
            "snapshot.reused",
            source=source,
            logical_date=as_of.isoformat(),
            l0_key=ref.key,
            rows=facts.rows,
            state="REUSED",
        )
        return (
            SnapshotOutcome(
                source_id=source,
                status=SnapshotStatus.REUSED,
                ref=ref,
                rows=facts.rows,
                content_date=facts.content_date,
                detail="already in L0 for this date; no request made",
            ),
            0,
        )

    spent = 0
    tracker.begin(source, as_of)
    try:
        if l0.exists(source, as_of, filename):
            ref = l0.ref_for(source, as_of, filename)
            fresh = False
            _LOG.info(
                "snapshot.l0_hit",
                source=source,
                logical_date=as_of.isoformat(),
                l0_key=ref.key,
                state="REUSED",
            )
        else:
            url = _entry(source, register).url_template
            ref = fetcher.fetch(source, url, as_of, filename=filename)
            spent = 1
            fresh = True
        payload = l0.get(ref)
    except Exception as exc:  # containment: one source must not take the sweep down
        detail = f"{type(exc).__name__}: {exc}"
        tracker.mark_failed(source, as_of, detail, retryable=True)
        _LOG.error(
            "snapshot.failed",
            source=source,
            logical_date=as_of.isoformat(),
            filename=filename,
            error=detail,
            state="FAILED",
        )
        return SnapshotOutcome(source, SnapshotStatus.FAILED, detail=detail), spent

    tracker.mark_fetched(source, as_of, checksum=ref.sha256, l0_path=str(l0.path_of(ref)))

    try:
        facts = spec.inspect(payload)
    except SnapshotContentError as exc:
        detail = str(exc)
        tracker.mark_failed(source, as_of, detail, retryable=True)
        _LOG.error(
            "snapshot.malformed",
            source=source,
            logical_date=as_of.isoformat(),
            l0_key=ref.key,
            size_bytes=ref.size_bytes,
            error=detail,
            state="MALFORMED",
        )
        return SnapshotOutcome(source, SnapshotStatus.MALFORMED, ref=ref, detail=detail), spent

    if facts.content_date is not None and facts.content_date != as_of:
        detail = (
            f"the payload dates itself {facts.content_date.isoformat()}, not "
            f"{as_of.isoformat()}: HTTP 200 carrying another session's file. The bytes are kept in "
            f"L0 as the record of what the source served today, and this date is NOT published — "
            f"nothing downstream may read a {facts.content_date.isoformat()} list as "
            f"{as_of.isoformat()} data."
        )
        tracker.mark_failed(source, as_of, detail, retryable=True)
        _LOG.error(
            "snapshot.stale_content",
            source=source,
            logical_date=as_of.isoformat(),
            content_date=facts.content_date.isoformat(),
            l0_key=ref.key,
            rows=facts.rows,
            state="STALE",
        )
        return (
            SnapshotOutcome(
                source,
                SnapshotStatus.STALE,
                ref=ref,
                rows=facts.rows,
                content_date=facts.content_date,
                detail=detail,
            ),
            spent,
        )

    # Archive-only: there is no L1 dataset for these payloads, so VALIDATED → NORMALIZED →
    # PUBLISHED is transited in one step. PUBLISHED is the honest resting state — "readers may see
    # this date" — because for an archive-only capture the L0 payload *is* the published artefact,
    # and `quality/gaps.py` counts a PUBLISHED row with no L1 dataset as unchecked rather than
    # flagging a missing partition.
    tracker.mark_validated(source, as_of)
    tracker.mark_normalized(source, as_of)
    tracker.mark_published(source, as_of)
    _LOG.info(
        "snapshot.published",
        source=source,
        logical_date=as_of.isoformat(),
        l0_key=ref.key,
        sha256=ref.sha256,
        size_bytes=ref.size_bytes,
        rows=facts.rows,
        content_date=None if facts.content_date is None else facts.content_date.isoformat(),
        fetched=fresh,
        state="PUBLISHED",
    )
    return (
        SnapshotOutcome(
            source_id=source,
            status=SnapshotStatus.CAPTURED if fresh else SnapshotStatus.REUSED,
            ref=ref,
            rows=facts.rows,
            content_date=facts.content_date,
            detail=facts.note,
        ),
        spent,
    )


def _alert_degraded(report: DailySnapshotReport, alerter: Alerter) -> int:
    """Alert once per degraded source; return how many were sent rather than deduplicated away.

    The dedup key is `(source, date, status)`, so a source that stays broken for a week is one
    piece of news per day and not one per re-run of the same day. Every degraded source is already
    journaled with a FAILED `sync_state` row; this is the half that reaches a human.
    """
    sent = 0
    for outcome in report.degraded:
        severity = Severity.CRITICAL if outcome.status is SnapshotStatus.STALE else Severity.WARNING
        result = alerter.send(
            severity,
            f"Daily snapshot: {outcome.source_id} {outcome.status.value} for "
            f"{report.as_of.isoformat()}",
            f"The daily snapshotter could not land a trustworthy {outcome.source_id} snapshot for "
            f"{report.as_of.isoformat()}: {outcome.detail} This source is snapshot-only — there is "
            f"no archive to backfill it from, so today's list is gone unless a re-run lands it "
            f"today. The other sources were unaffected.",
            f"snapshot:{outcome.source_id}:{report.as_of.isoformat()}:{outcome.status.value}",
        )
        sent += int(result is AlertOutcome.SENT)
    return sent


# ── the scheduler entry point ────────────────────────────────────────────────────────────────


def _constituents_step(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncStateStore,
    commit: Callable[[], None],
    data_root: Path,
    register: SourceRegister,
    specs: Sequence[IndexSpec] = DEFAULT_INDEX_SET,
) -> Callable[[date], CoverageReport]:
    """M10.1's constituents sweep as one step of the daily sweep, committing per slug.

    Driven per slug rather than in one call so a kill loses at most the slug in flight, exactly as
    the weekly job (M10.2) does it. The weekly job stays registered and is not replaced: it stamps
    the ISO week's Sunday anchor, this stamps the capture date, and the two never collide because
    the daily job only runs on trading days and the weekly one runs on Saturdays.
    """

    def step(as_of: date) -> CoverageReport:
        outcomes: list[SlugOutcome] = []
        for spec in specs:
            single = run_constituents_ingest(
                fetcher=fetcher,
                l0=l0,
                tracker=tracker,
                as_of=as_of,
                specs=(spec,),
                data_root=data_root,
                register=register,
            )
            outcomes.extend(single.outcomes)
            commit()
        return CoverageReport(as_of=as_of, outcomes=tuple(outcomes))

    return step


def run_daily_snapshot_job(context: JobContext) -> None:
    """The scheduler's `daily_snapshot` job body — build real wiring and capture today.

    What it does: from the job's injected clock and settings (B10), takes the request budget for
    every host in the snapshot set, builds the networked fetcher, the L0 store, the configured
    alerter and a database connection, and runs the sweep for today — asserting the lake root
    against `Settings.snapshot_expect_lake_root` before the first request.
    What it assumes: the database is migrated and reachable and the network is up.
    What it never does: fetch into a lake the operator did not declare, run on a day the exchange
    was shut (those are filed `GAP`), or report a green run on a day nothing landed.

    Raises `DailySnapshotError` when the whole sweep landed nothing, so the run is recorded FAILED
    and tomorrow's run is the self-heal; a partial capture is a green run with per-source alerts.
    """
    settings: Settings = context.settings
    clock: Clock = context.clock
    register = load_register()
    as_of = clock.today()

    l0 = L0Store(clock=clock, data_root=settings.data_root)
    alerter = build_alerter(settings, clock=clock)
    calendar = trading_calendar()
    hosts = sorted(
        {_entry(spec.source_id, register).host for spec in DEFAULT_SNAPSHOT_SET}
        | {_entry("nifty_index_constituents", register).host}
    )

    with (
        connection(settings) as conn,
        leased_fetcher(
            hosts,
            clock=clock,
            command=f"daily snapshot {as_of.isoformat()}",
            settings=settings,
            l0=l0,
            alerter=alerter,
            register=register,
        ) as fetcher,
    ):
        sync = SyncStateStore(conn, clock=clock, calendar=calendar)
        report = run_daily_snapshot(
            fetcher=fetcher,
            l0=l0,
            tracker=sync,
            alerter=alerter,
            as_of=as_of,
            calendar=calendar,
            commit=conn.commit,
            register=register,
            constituents=_constituents_step(
                fetcher=fetcher,
                l0=l0,
                tracker=sync,
                commit=conn.commit,
                data_root=settings.data_root,
                register=register,
            ),
            expect_lake_root=settings.snapshot_expect_lake_root,
        )
    _LOG.info("snapshot.job_done", summary=report.summary(), state="DONE")
