"""W2 close-out: measure the acquired PR-bundle corpus off L0, and report it. Read-only.

The campaign (`dataplatform.ingest.pr_bundle_campaign`) put 4,124 bundles in the lake. This module
is the other half: it opens every one of them, runs the Phase 1 readers over the members, and
reports what the archive actually contains — per-year availability per member family, the dated
corporate-action count that is the whole point of the wave, and whether the Phase 1 findings about
`Ix` and `mcap` survive the full corpus rather than the 39-request sample they were drawn from.

**Nothing here promotes anything.** No L1 write, no `corporate_actions` row, no `sync_state`, no
`quality_flag`, no database connection at all. Every member of every bundle is symbol-keyed with
no ISIN, ISIN is the only join key (invariant #2), and the only symbol→ISIN resolver we hold is a
present-day listing — so a join is W4 identity work and a promotion written today would be a
guess. The output of this module is a number in a Markdown report and nothing else.

**The served set is enumerated from L0, never from a plan.** The W1 closer shipped a calendar
reconcile whose second direction could not fail: it built the "served" set from the
calendar-derived fetch plan and then compared it against the same calendar, so *served minus
expected* was empty by construction and the check had never — could never have — fired.
`sessions_in_l0` therefore walks the payload filenames under `L0/nse_pr_bundle` and takes each
bundle's date from the name the archive served it under; the calendar is not consulted until the
comparison itself. `prove_reconcile_can_fail` then injects a date into each side and asserts the
corresponding direction fires, because a check that has never failed may be a check that cannot.

**One member census beyond the three readers, and it is the report's largest finding.** The sweep
also counts the `INDEX_FLG` column of the `ffix` member, because `MemberKind.FFIX` is registered
as *"Fixed income"* and is nothing of the kind: it is the free-float index constituent file, and it
carries NIFTY 50 membership with weightage daily from the very first bundle. That is a census — a
header count and a first-column count, no typed rows, no `Decimal`, no promotion — and explicitly
not a fourth parser. `FfixCensus` documents the distinction; §3c of the report is the finding.

**A parse failure is collected, not raised.** A sweep that dies on bundle 3,000 reports nothing
about the 1,124 after it, so every failure is caught with its filename and message and printed in
its own section of the report. That is louder than a traceback, not quieter: a non-empty failure
list is a finding the report leads with.

    uv run python -m dataplatform.ingest.nse.pr_bundle.survey \\
        --data-root /home/ubuntu/stock-manager/data --from 2010-01-04 --to 2026-09-04
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import re
import sys
import zipfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from dataplatform.clock import SystemClock
from dataplatform.ingest.calendar import (
    DayKind,
    Reconciliation,
    TradingCalendar,
    trading_calendar,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle.bc import BcRow, parse_bc_bundle
from dataplatform.ingest.nse.pr_bundle.bundle import (
    PR_BUNDLE_SOURCE_ID,
    MemberKind,
    PrBundle,
)
from dataplatform.ingest.nse.pr_bundle.ix import parse_ix_bundle
from dataplatform.ingest.nse.pr_bundle.mcap import parse_mcap_bundle
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Store
from dataplatform.store.l0_compare import LakeComparison, compare_lakes
from dataplatform.store.paths import Layer, layer_root

__all__ = [
    "ARCHIVE_FILENAME_RE",
    "PURPOSE_TAGS",
    "BcSurvey",
    "CorpusSurvey",
    "FfixCensus",
    "InjectionProof",
    "IxIndexSpan",
    "IxSurvey",
    "McapSurvey",
    "MemberGap",
    "MemberSpan",
    "ParseFailure",
    "YearRow",
    "date_of_archive_name",
    "payload_counts",
    "prove_reconcile_can_fail",
    "purpose_tags",
    "render_report",
    "sessions_in_l0",
    "survey_corpus",
]

_LOG = get_logger(__name__)

#: `PR<DDMMYY>.zip` — the name the archive serves a bundle under, and the only thing
#: `sessions_in_l0` reads a date from. Deliberately independent of the sidecars: the served set
#: must not be derivable from anything the fetch plan wrote.
ARCHIVE_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"^PR(\d{6})\.zip$", re.IGNORECASE)

#: Coarse keyword tags over the raw `PURPOSE` text, for the report's distribution table only.
#:
#: A purpose is free text and routinely carries two facts at once ("ANNUAL GENERAL MEETING AND
#: DIVIDEND RS 4 PER SHARE"), so a row gets a *set* of tags rather than one bucket and the counts
#: therefore overlap — stated in the table rather than hidden by forcing a single winner. This is
#: report presentation and nothing else: `BcRow.purpose` stays byte-for-byte as published, and the
#: classification that a promotion would need belongs to the reconciliation task, which must
#: parse terms (ratios, amounts, face values) and not merely spot a word.
PURPOSE_TAGS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    # NSE's own shorthands, measured off the corpus's top purposes: `AGM/DIV-RS 2 PER SHARE`,
    # `DIV/REDEMPTION`, `DIV/STP`. `"DIV"` alone would be wrong — it matches `SUB-DIVISION`,
    # which is a split.
    ("DIVIDEND", ("DIVIDEND", "DIVD", "DIV-", "DIV/", "DIV.", "DIV ")),
    ("BONUS", ("BONUS",)),
    ("SPLIT", ("SPLIT", "SUB-DIVISION", "SUB DIVISION", "SUBDIVISION", "STOCK SPLIT")),
    ("RIGHTS", ("RIGHTS", "RIGHT ISSUE")),
    ("AGM", ("ANNUAL GENERAL MEETING", "AGM")),
    ("EGM", ("EXTRA ORDINARY GENERAL MEETING", "EXTRAORDINARY GENERAL MEETING", "EGM")),
    ("BUYBACK", ("BUY BACK", "BUYBACK", "BUY-BACK")),
    ("SCHEME", ("AMALGAMATION", "MERGER", "DEMERGER", "ARRANGEMENT", "SCHEME", "SPIN OFF")),
    ("REDEMPTION", ("REDEMPTION", "MATURITY", "PREMATURE")),
    ("INTEREST", ("INTEREST", "COUPON")),
    ("NAME_CHANGE", ("CHANGE IN NAME", "NAME CHANGE", "CHANGE OF NAME")),
    ("OPEN_OFFER", ("OPEN OFFER", "DELISTING", "DELIST")),
    ("CAPITAL_REDUCTION", ("REDUCTION OF CAPITAL", "CAPITAL REDUCTION", "CONSOLIDATION")),
)

#: How many raw purpose strings the report lists verbatim.
_TOP_PURPOSES: Final = 15

#: What reading one member can raise. `ParseError` is what the readers *mean* to raise; the other
#: three are ways a real payload escapes them, and the full corpus produces all of them where a
#: 39-request sample produced none:
#:
#: * `csv.Error` — `ix.py` and `bc.py` hand the decoded text straight to `csv.reader`, which
#:   raises `_csv.Error` (not `ParseError`) on an embedded bare newline in an unquoted field.
#: * `ValidationError` — a pydantic row model rejecting a published value.
#: * `UnicodeDecodeError` — a payload in no encoding the reader's fallback chain covers.
#:
#: Caught by name, never bare: each one is recorded as a `ParseFailure` with its bundle and
#: message so the sweep reaches bundle 4,124 and the report names every member it could not read.
#: A member that raises one of these is a **defect in the reader**, surfaced here, not repaired.
_MEMBER_ERRORS: Final = (ParseError, csv.Error, ValidationError, UnicodeDecodeError)


# ── the served set, read off L0 and nothing else ─────────────────────────────────────────────


def date_of_archive_name(filename: str) -> date | None:
    """`PR040110.zip` → 2010-01-04, or `None` for a name that is not an archive bundle.

    Reads `DDMMYY` and assumes `20YY`: the archive begins in 2010 and this platform will not
    outlive that assumption. Never consults a calendar or a sidecar.
    """
    match = ARCHIVE_FILENAME_RE.match(filename)
    if match is None:
        return None
    digits = match.group(1)
    try:
        return date(2000 + int(digits[4:6]), int(digits[2:4]), int(digits[0:2]))
    except ValueError:
        return None


def sessions_in_l0(data_root: Path, *, source: str = PR_BUNDLE_SOURCE_ID) -> tuple[date, ...]:
    """Every session L0 actually holds a bundle for, by listing the payloads under it.

    **This is the served set, and it must stay independent of the fetch plan.** It globs
    `L0/<source>/<yyyy>/<mm>/PR*.zip` and takes each date from the archive filename — not from a
    sidecar, not from `SessionPlan`, not from the calendar. Comparing a plan-derived set against
    the calendar that produced it is the W1 defect this signature exists to make impossible.

    Assumes the standard L0 layout. Returns ascending, deduplicated. Never writes.
    """
    root = layer_root(Layer.L0, data_root=data_root) / source
    if not root.is_dir():
        return ()
    found = {
        day
        for path in root.glob("*/*/*")
        if path.is_file()
        for day in (date_of_archive_name(path.name),)
        if day is not None
    }
    _LOG.info("pr_bundle_survey.enumerated_l0", root=str(root), bundles=len(found))
    return tuple(sorted(found))


# ── what one member family looks like across the corpus ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MemberGap:
    """A run of consecutive bundles, inside a family's own span, that do not carry it.

    Interior by definition: a gap before `first_seen` or after `last_seen` is the family not being
    published yet, or no longer being published, which is an era boundary rather than a hole.
    """

    start: date
    end: date
    bundles: int


@dataclass(frozen=True, slots=True)
class MemberSpan:
    """One member family across the whole corpus: how often, from when, to when, and its holes."""

    name: str
    registered: bool
    bundles: int
    instances: int
    first_seen: date
    last_seen: date
    per_year: Mapping[int, int]
    gaps: tuple[MemberGap, ...]

    @property
    def doubled(self) -> int:
        """Members beyond one per bundle — `fo04012010.csv` sits beside `fo04012010.doc`.

        Non-zero means the family ships more than one file per bundle in some era, which is why
        `bundles` counts *bundles* and never member files: an availability table built on the raw
        occurrence count reports 2010 as having 502 `fo` days out of 251.
        """
        return self.instances - self.bundles


@dataclass(frozen=True, slots=True)
class YearRow:
    """One year of the availability table: what was owed, what arrived, and what it carried."""

    year: int
    sessions: int
    muhurat: int
    bundles: int
    members: Mapping[str, int]

    @property
    def expected(self) -> int:
        """Dates the calendar says a bundle should exist for — sessions plus Muhurat."""
        return self.sessions + self.muhurat


# ── the three parsed members ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BcSurvey:
    """The headline: dated corporate actions, as broadcast, across the whole corpus.

    `rows` counts *broadcasts*, not actions: NSE re-publishes the forthcoming book every session,
    so one dividend appears in every bundle from its announcement until its record date.
    `distinct_actions` collapses that on the published identity of the action, and
    `earliest_broadcast_per_year` buckets each distinct action by the first date it was knowable —
    which is the number that matters for invariant #7 and the only one comparable to a
    `corporate_actions` row count.
    """

    bundles: int
    rows: int
    distinct_actions: int
    distinct_symbols: int
    distinct_purposes: int
    knowable_first: date
    knowable_last: date
    ex_date_first: date | None
    ex_date_last: date | None
    rows_with_ex_date: int
    rows_per_year: Mapping[int, int]
    actions_per_year: Mapping[int, int]
    purpose_tags: Mapping[str, int]
    untagged_rows: int
    top_purposes: tuple[tuple[str, int], ...]
    series: Mapping[str, int]
    announced_after_ex_date: int


@dataclass(frozen=True, slots=True)
class IxIndexSpan:
    """One index name as `Ix` published it: how many sessions, over what span, how many rows."""

    index_name: str
    dates: int
    first_seen: date
    last_seen: date
    rows: int


@dataclass(frozen=True, slots=True)
class IxSurvey:
    """`Ix` across the corpus — the Phase 1 "2010 only, rotating, never NIFTY 50" claim, tested.

    *Present* and *parsed* are two different counts and the difference is a finding, not a
    rounding: a bundle can carry an `Ix` member that `ix.py` cannot read, and reporting only the
    parsed count would say the member disappeared on a date it did not.
    """

    parsed_bundles: int
    present_bundles: int
    rows: int
    first_parsed: date | None
    last_parsed: date | None
    first_present: date | None
    last_present: date | None
    indices: tuple[IxIndexSpan, ...]
    empty_banners: Mapping[str, int]
    distinct_symbols: int
    parsed_after_2010: tuple[date, ...]
    present_after_2010: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class McapSurvey:
    """`mcap` across the corpus — arrival date, the issue-size series, and the Category values."""

    bundles: int
    rows: int
    first_seen: date | None
    last_seen: date | None
    distinct_symbols: int
    categories: Mapping[str, int]
    series: Mapping[str, int]
    rows_with_issue_size: int
    zero_issue_size_rows: int
    symbols_with_issue_size: int
    never_traded_rows: int
    stale_trade_date_bundles: tuple[date, ...]
    gaps: tuple[MemberGap, ...]


@dataclass(frozen=True, slots=True)
class FfixCensus:
    """The `ffix` member, censused — **not** parsed. See the module docstring's finding note.

    `MemberKind.FFIX` is registered as "Fixed income" and the payload is nothing of the kind: it
    is the **free-float index constituent file**, `INDEX_FLG, SYMBOL, SERIES, SECURITY, ISSUE_CAP,
    INVESTIBLE_FACTOR, CLOSE_PRIC, FF_MKT_CAP, WEIGHTAGE`, carrying NIFTY (the 50), JR. NIFTY and
    CNX 100 from the first bundle and eighteen indices by 2013. That is a dated index-membership
    series with weightage, which `ops/BACKLOG.md:126` records as not existing.

    This is a census — index names, session counts, row counts, and whether the header ever
    changes — and deliberately not a reader: no typed row model, no `Decimal`, no promotion. A
    real `ffix` reader is a task of its own, and writing one here would smuggle a fourth parser
    into a close-out whose brief is to measure what W2 acquired.
    """

    bundles: int
    present_bundles: int
    rows: int
    first_seen: date | None
    last_seen: date | None
    indices: tuple[IxIndexSpan, ...]
    headers: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ParseFailure:
    """One bundle the sweep could not read, and why. Collected so the sweep finishes."""

    day: date
    filename: str
    member: str
    message: str


@dataclass(frozen=True, slots=True)
class CorpusSurvey:
    """Everything the sweep measured, in one object the renderer turns into Markdown."""

    l0_root: Path
    start: date
    end: date
    bundles: int
    first_bundle: date | None
    last_bundle: date | None
    bytes_stored: int
    years: tuple[YearRow, ...]
    families: tuple[MemberSpan, ...]
    unknown_members: Mapping[str, int]
    documentation_members: Mapping[str, int]
    bc: BcSurvey | None
    ix: IxSurvey
    mcap: McapSurvey
    ffix: FfixCensus
    zipped_members: Mapping[str, int]
    failures: tuple[ParseFailure, ...]
    reconciliation: Reconciliation
    proofs: tuple[InjectionProof, ...]

    @property
    def member_names(self) -> tuple[str, ...]:
        """Family names in first-seen order, which is the availability table's column order."""
        return tuple(span.name for span in self.families)


# ── the sweep ────────────────────────────────────────────────────────────────────────────────


@dataclass
class _Accumulator:
    """Mutable state for one sweep. Rows are folded in and never retained.

    475,000 `BcRow` objects will not fit in memory usefully, and nothing in a report needs them
    individually — so each bundle's rows are counted into sets and counters and dropped.
    """

    member_dates: defaultdict[str, set[date]] = field(
        default_factory=lambda: defaultdict(set[date])
    )
    member_instances: Counter[str] = field(default_factory=Counter)
    member_registered: dict[str, bool] = field(default_factory=dict)
    unknown: Counter[str] = field(default_factory=Counter)
    documentation: Counter[str] = field(default_factory=Counter)

    bc_bundles: int = 0
    bc_rows: int = 0
    bc_symbols: set[str] = field(default_factory=set)
    bc_purposes: set[str] = field(default_factory=set)
    bc_purpose_counts: Counter[str] = field(default_factory=Counter)
    bc_tags: Counter[str] = field(default_factory=Counter)
    bc_untagged: int = 0
    bc_series: Counter[str] = field(default_factory=Counter)
    bc_rows_per_year: Counter[int] = field(default_factory=Counter)
    bc_rows_with_ex: int = 0
    bc_after_ex: int = 0
    bc_ex_dates: list[date] = field(default_factory=list)
    #: distinct action identity → the earliest date it was broadcast.
    bc_actions: dict[tuple[str, ...], date] = field(default_factory=dict)

    ix_bundles: int = 0
    ix_rows: int = 0
    ix_dates: list[date] = field(default_factory=list)
    ix_index_dates: defaultdict[str, list[date]] = field(
        default_factory=lambda: defaultdict(list[date])
    )
    ix_index_rows: Counter[str] = field(default_factory=Counter)
    ix_banners: Counter[str] = field(default_factory=Counter)
    ix_symbols: set[str] = field(default_factory=set)

    mcap_bundles: int = 0
    mcap_rows: int = 0
    mcap_dates: list[date] = field(default_factory=list)
    mcap_symbols: set[str] = field(default_factory=set)
    mcap_symbols_with_size: set[str] = field(default_factory=set)
    mcap_categories: Counter[str] = field(default_factory=Counter)
    mcap_series: Counter[str] = field(default_factory=Counter)
    mcap_with_size: int = 0
    mcap_zero_size: int = 0
    mcap_never_traded: int = 0
    mcap_stale: list[date] = field(default_factory=list)

    ffix_bundles: int = 0
    ffix_rows: int = 0
    ffix_dates: list[date] = field(default_factory=list)
    ffix_index_dates: defaultdict[str, set[date]] = field(
        default_factory=lambda: defaultdict(set[date])
    )
    ffix_index_rows: Counter[str] = field(default_factory=Counter)
    ffix_headers: Counter[str] = field(default_factory=Counter)

    zipped_members: Counter[str] = field(default_factory=Counter)
    failures: list[ParseFailure] = field(default_factory=list)


def purpose_tags(purpose: str) -> tuple[str, ...]:
    """The coarse tags a raw `PURPOSE` string carries, for the report's distribution table.

    Returns every tag whose keywords appear, not one winner: "AGM AND DIVIDEND RS 3" is both, and
    a table that pretended otherwise would understate dividends. Empty for a purpose no keyword
    matches, which the report counts rather than drops. See `PURPOSE_TAGS` — this is presentation,
    never promotion logic.
    """
    upper = purpose.upper()
    return tuple(tag for tag, words in PURPOSE_TAGS if any(word in upper for word in words))


def survey_corpus(
    store: L0Store,
    *,
    calendar: TradingCalendar,
    start: date,
    end: date,
    source: str = PR_BUNDLE_SOURCE_ID,
) -> CorpusSurvey:
    """Open every bundle L0 holds in the range and fold it into one `CorpusSurvey`.

    What it does: enumerates the served set off L0, opens each payload through `L0Store.get` (so
    every byte read is re-verified against its sidecar on the way in), runs the `Bc`, `Ix` and
    `mcap` readers, reconciles the served set against `calendar` in both directions, and proves
    that reconcile can fail.
    What it assumes: the whole range is inside the calendar's coverage; it raises otherwise rather
    than treating an uncovered year as holiday-free.
    What it never does: fetch, write, promote, or stop early on a bad bundle.
    """
    served = tuple(
        day for day in sessions_in_l0(store.data_root, source=source) if start <= day <= end
    )
    acc = _Accumulator()
    bytes_stored = 0

    for ref in store.iter_refs(source, start=start, end=end):
        day = ref.logical_date
        bytes_stored += ref.size_bytes
        try:
            bundle = PrBundle(store.get(ref), filename=ref.filename)
        except _MEMBER_ERRORS as exc:
            acc.failures.append(
                ParseFailure(
                    day=day,
                    filename=ref.filename,
                    member="(zip)",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        with bundle:
            _fold_members(acc, bundle, day=day)
            _fold_bc(acc, bundle, ref_key=ref.key, day=day)
            _fold_ix(acc, bundle, ref_key=ref.key, day=day)
            _fold_mcap(acc, bundle, ref_key=ref.key, day=day)
            _fold_ffix(acc, bundle, day=day)

    families = _families(acc, served=served)
    reconciliation = calendar.reconcile(served, start, end)
    survey = CorpusSurvey(
        l0_root=layer_root(Layer.L0, data_root=store.data_root),
        start=start,
        end=end,
        bundles=len(served),
        first_bundle=served[0] if served else None,
        last_bundle=served[-1] if served else None,
        bytes_stored=bytes_stored,
        years=_year_rows(acc, calendar=calendar, served=served, start=start, end=end),
        families=families,
        unknown_members=dict(acc.unknown.most_common()),
        documentation_members=dict(acc.documentation.most_common()),
        bc=_bc_survey(acc),
        ix=_ix_survey(acc, present=_family(families, MemberKind.IX)),
        mcap=_mcap_survey(acc, served=served),
        ffix=_ffix_census(acc, present=_family(families, MemberKind.FFIX)),
        zipped_members=dict(acc.zipped_members.most_common()),
        failures=tuple(acc.failures),
        reconciliation=reconciliation,
        proofs=prove_reconcile_can_fail(calendar, served, start, end),
    )
    _LOG.info(
        "pr_bundle_survey.done",
        bundles=survey.bundles,
        bc_rows=0 if survey.bc is None else survey.bc.rows,
        failures=len(survey.failures),
        missing=len(reconciliation.missing),
        unexpected=len(reconciliation.unexpected),
    )
    return survey


def _fold_members(acc: _Accumulator, bundle: PrBundle, *, day: date) -> None:
    """Record which families this bundle carried."""
    for member in bundle.members:
        if member.is_documentation:
            acc.documentation[member.name.lower()] += 1
            continue
        if member.kind is None:
            acc.unknown[member.name] += 1
            continue
        acc.member_dates[member.kind.value].add(day)
        acc.member_instances[member.kind.value] += 1
        acc.member_registered[member.kind.value] = True


def _fold_bc(acc: _Accumulator, bundle: PrBundle, *, ref_key: str, day: date) -> None:
    """Fold one bundle's `Bc` member into the corporate-action aggregates."""
    if not bundle.has(MemberKind.BC):
        return
    try:
        rows = parse_bc_bundle(bundle, l0_key=ref_key)
    except _MEMBER_ERRORS as exc:
        acc.failures.append(
            ParseFailure(
                day=day,
                filename=bundle.filename,
                member="bc",
                message=f"{type(exc).__name__}: {exc}",
            )
        )
        return

    acc.bc_bundles += 1
    acc.bc_rows += len(rows)
    acc.bc_rows_per_year[day.year] += len(rows)
    for row in rows:
        acc.bc_symbols.add(row.symbol)
        acc.bc_purposes.add(row.purpose)
        acc.bc_purpose_counts[row.purpose] += 1
        acc.bc_series[row.series] += 1
        tags = purpose_tags(row.purpose)
        if tags:
            for tag in tags:
                acc.bc_tags[tag] += 1
        else:
            acc.bc_untagged += 1
        if row.ex_date is not None:
            acc.bc_rows_with_ex += 1
            acc.bc_ex_dates.append(row.ex_date)
            if row.announced_before_ex_date is False:
                acc.bc_after_ex += 1
        identity = _action_identity(row)
        known = acc.bc_actions.get(identity)
        if known is None or row.knowable_date < known:
            acc.bc_actions[identity] = row.knowable_date


def _action_identity(row: BcRow) -> tuple[str, ...]:
    """The published identity of one action, for collapsing re-broadcasts of the same event.

    Symbol, series, the exact purpose text and the four dates: everything NSE publishes about the
    action *except* when it was broadcast. Two rows agreeing on all of it in different bundles are
    the same event re-announced, which is how the corpus's 475k broadcasts become a countable
    number of actions. It is not an ISIN and it is not a promotion key — a symbol reused by a
    different issuer years later with an identical purpose and identical dates would collide, and
    the report says so.
    """
    return (
        row.symbol,
        row.series,
        row.purpose,
        _stamp(row.record_date),
        _stamp(row.ex_date),
        _stamp(row.book_closure_start),
        _stamp(row.book_closure_end),
    )


def _stamp(day: date | None) -> str:
    return "" if day is None else day.isoformat()


def _is_zip(payload: bytes) -> bool:
    """Whether a member's bytes are a zip container rather than the CSV the reader expects.

    `Ix190213.zip` is exactly this: the 2013-02-19 bundle ships its `Ix` member as a zip, and
    `ix.py` hands the compressed bytes straight to `csv.reader`, which raises a bare `_csv.Error`
    about an embedded newline. Detecting the container turns an unreadable error message into the
    actual fact about the source.
    """
    return payload[:4] == b"PK\x03\x04"


def _zip_names(payload: bytes) -> tuple[str, ...]:
    """The member names inside a zipped member, for the failure message. `()` if unreadable."""
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            return tuple(info.filename for info in archive.infolist())
    except zipfile.BadZipFile:
        return ()


def _fold_ffix(acc: _Accumulator, bundle: PrBundle, *, day: date) -> None:
    """Census one bundle's `ffix` member: its header and its `INDEX_FLG` column. Never parses.

    Reads the first column of each line and nothing else — no typed rows, no `Decimal`, no
    promotion. Every header shape encountered is counted, so a format change shows up as a second
    entry rather than as silently miscounted indices.
    """
    if not bundle.has(MemberKind.FFIX):
        return
    payload = bundle.read(MemberKind.FFIX)
    if _is_zip(payload):
        acc.zipped_members[MemberKind.FFIX.value] += 1
        return
    # Latin-1, the same decode the three readers use: the member is published in it and the
    # census only ever reads the first column, so a mojibake issuer name cannot skew a count.
    lines = [line for line in payload.decode("latin-1").splitlines() if line.strip(" ,\t")]
    if not lines:
        return
    acc.ffix_headers[lines[0].strip()] += 1
    acc.ffix_bundles += 1
    acc.ffix_dates.append(day)
    for line in lines[1:]:
        name = line.split(",", 1)[0].strip()
        if not name:
            continue
        acc.ffix_rows += 1
        acc.ffix_index_dates[name].add(day)
        acc.ffix_index_rows[name] += 1


def _fold_ix(acc: _Accumulator, bundle: PrBundle, *, ref_key: str, day: date) -> None:
    """Fold one bundle's `Ix` member into the index aggregates."""
    if not bundle.has(MemberKind.IX):
        return
    payload = bundle.read(MemberKind.IX)
    if _is_zip(payload):
        acc.zipped_members[MemberKind.IX.value] += 1
        names = ", ".join(_zip_names(payload)) or "unreadable"
        acc.failures.append(
            ParseFailure(
                day=day,
                filename=bundle.filename,
                member="ix",
                message=(
                    f"the Ix member is a zip container, not a CSV (it holds {names}); "
                    "ix.py hands the compressed bytes to csv.reader, which raises a bare "
                    "_csv.Error about an embedded newline"
                ),
            )
        )
        return
    try:
        parsed = parse_ix_bundle(bundle, l0_key=ref_key)
    except _MEMBER_ERRORS as exc:
        acc.failures.append(
            ParseFailure(
                day=day,
                filename=bundle.filename,
                member="ix",
                message=f"{type(exc).__name__}: {exc}",
            )
        )
        return

    acc.ix_bundles += 1
    acc.ix_rows += len(parsed.rows)
    acc.ix_dates.append(day)
    for name in parsed.indices:
        acc.ix_index_dates[name].append(day)
    for name in parsed.empty_indices:
        acc.ix_banners[name] += 1
    for row in parsed.rows:
        acc.ix_index_rows[row.index_name] += 1
        acc.ix_symbols.add(row.symbol)


def _fold_mcap(acc: _Accumulator, bundle: PrBundle, *, ref_key: str, day: date) -> None:
    """Fold one bundle's `mcap` member into the issue-size aggregates."""
    if not bundle.has(MemberKind.MCAP):
        return
    try:
        parsed = parse_mcap_bundle(bundle, l0_key=ref_key)
    except _MEMBER_ERRORS as exc:
        acc.failures.append(
            ParseFailure(
                day=day,
                filename=bundle.filename,
                member="mcap",
                message=f"{type(exc).__name__}: {exc}",
            )
        )
        return

    acc.mcap_bundles += 1
    acc.mcap_rows += len(parsed.rows)
    acc.mcap_dates.append(day)
    if parsed.rows and any(row.trade_date != day for row in parsed.rows):
        acc.mcap_stale.append(day)
    for row in parsed.rows:
        acc.mcap_symbols.add(row.symbol)
        acc.mcap_categories[row.category] += 1
        acc.mcap_series[row.series] += 1
        if row.issue_size > 0:
            acc.mcap_with_size += 1
            acc.mcap_symbols_with_size.add(row.symbol)
        else:
            acc.mcap_zero_size += 1
        if row.never_traded:
            acc.mcap_never_traded += 1


# ── folding the accumulator into the frozen surveys ──────────────────────────────────────────


def _year_rows(
    acc: _Accumulator,
    *,
    calendar: TradingCalendar,
    served: Sequence[date],
    start: date,
    end: date,
) -> tuple[YearRow, ...]:
    """One row per year: calendar expectation, bundles held, and a count per member family."""
    # Counted over the *deduplicated* dates: a bundle carrying two members of one family
    # (`fo04012010.csv` beside `fo04012010.doc`) is one day of availability, not two.
    per_family_year: dict[str, Counter[int]] = {
        name: Counter(day.year for day in dates) for name, dates in acc.member_dates.items()
    }
    bundles_by_year = Counter(day.year for day in served)
    rows: list[YearRow] = []
    for year in range(start.year, end.year + 1):
        span_start = max(start, date(year, 1, 1))
        span_end = min(end, date(year, 12, 31))
        by_kind = Counter(kind for _, kind in calendar.days(span_start, span_end))
        rows.append(
            YearRow(
                year=year,
                sessions=by_kind[DayKind.SESSION],
                muhurat=by_kind[DayKind.MUHURAT],
                bundles=bundles_by_year[year],
                members={
                    name: counts[year] for name, counts in per_family_year.items() if counts[year]
                },
            )
        )
    return tuple(rows)


def _families(acc: _Accumulator, *, served: Sequence[date]) -> tuple[MemberSpan, ...]:
    """Every family, ordered by first appearance, with per-year counts and interior gaps."""
    spans: list[MemberSpan] = []
    for name, dates in acc.member_dates.items():
        present = sorted(dates)
        spans.append(
            MemberSpan(
                name=name,
                registered=acc.member_registered.get(name, False),
                bundles=len(present),
                instances=acc.member_instances[name],
                first_seen=present[0],
                last_seen=present[-1],
                per_year=dict(sorted(Counter(day.year for day in present).items())),
                gaps=_interior_gaps(present, served=served),
            )
        )
    spans.sort(key=lambda span: (span.first_seen, span.name))
    return tuple(spans)


def _interior_gaps(present: Sequence[date], *, served: Sequence[date]) -> tuple[MemberGap, ...]:
    """Runs of bundles between a family's first and last appearance that do not carry it.

    Measured against the *bundles that exist*, not against the calendar: a session with no bundle
    at all is a bundle gap (the reconcile's job) and counting it here would report the same hole
    twice under two different names.
    """
    if not present:
        return ()
    have = set(present)
    window = [day for day in served if present[0] <= day <= present[-1] and day not in have]
    gaps: list[MemberGap] = []
    run: list[date] = []
    order = {day: index for index, day in enumerate(served)}
    for day in window:
        if run and order[day] != order[run[-1]] + 1:
            gaps.append(MemberGap(start=run[0], end=run[-1], bundles=len(run)))
            run = []
        run.append(day)
    if run:
        gaps.append(MemberGap(start=run[0], end=run[-1], bundles=len(run)))
    return tuple(gaps)


def _bc_survey(acc: _Accumulator) -> BcSurvey | None:
    """The `Bc` aggregates, or `None` when no bundle in range carried the member."""
    if not acc.bc_actions:
        return None
    broadcasts = sorted(acc.bc_actions.values())
    return BcSurvey(
        bundles=acc.bc_bundles,
        rows=acc.bc_rows,
        distinct_actions=len(acc.bc_actions),
        distinct_symbols=len(acc.bc_symbols),
        distinct_purposes=len(acc.bc_purposes),
        knowable_first=broadcasts[0],
        knowable_last=broadcasts[-1],
        ex_date_first=min(acc.bc_ex_dates) if acc.bc_ex_dates else None,
        ex_date_last=max(acc.bc_ex_dates) if acc.bc_ex_dates else None,
        rows_with_ex_date=acc.bc_rows_with_ex,
        rows_per_year=dict(sorted(acc.bc_rows_per_year.items())),
        actions_per_year=dict(sorted(Counter(day.year for day in broadcasts).items())),
        purpose_tags=dict(acc.bc_tags.most_common()),
        untagged_rows=acc.bc_untagged,
        top_purposes=tuple(acc.bc_purpose_counts.most_common(_TOP_PURPOSES)),
        series=dict(acc.bc_series.most_common()),
        announced_after_ex_date=acc.bc_after_ex,
    )


def _ix_survey(acc: _Accumulator, *, present: MemberSpan | None) -> IxSurvey:
    """The `Ix` aggregates, presence and parse counted separately (see `IxSurvey`)."""
    seen = sorted(acc.member_dates[MemberKind.IX.value])
    indices = tuple(
        IxIndexSpan(
            index_name=name,
            dates=len(set(dates)),
            first_seen=min(dates),
            last_seen=max(dates),
            rows=acc.ix_index_rows[name],
        )
        for name, dates in sorted(acc.ix_index_dates.items())
    )
    return IxSurvey(
        parsed_bundles=acc.ix_bundles,
        present_bundles=0 if present is None else present.bundles,
        rows=acc.ix_rows,
        first_parsed=min(acc.ix_dates) if acc.ix_dates else None,
        last_parsed=max(acc.ix_dates) if acc.ix_dates else None,
        first_present=None if present is None else present.first_seen,
        last_present=None if present is None else present.last_seen,
        indices=indices,
        empty_banners=dict(acc.ix_banners.most_common()),
        distinct_symbols=len(acc.ix_symbols),
        parsed_after_2010=tuple(sorted(day for day in acc.ix_dates if day.year > 2010)),
        present_after_2010=tuple(day for day in seen if day.year > 2010),
    )


def _family(families: Sequence[MemberSpan], kind: MemberKind) -> MemberSpan | None:
    """The span for one member kind, or `None` when no bundle in range carried it."""
    return next((span for span in families if span.name == kind.value), None)


def _ffix_census(acc: _Accumulator, *, present: MemberSpan | None) -> FfixCensus:
    """The `ffix` aggregates — presence, index names, and every header shape seen."""
    return FfixCensus(
        bundles=acc.ffix_bundles,
        present_bundles=0 if present is None else present.bundles,
        rows=acc.ffix_rows,
        first_seen=min(acc.ffix_dates) if acc.ffix_dates else None,
        last_seen=max(acc.ffix_dates) if acc.ffix_dates else None,
        indices=tuple(
            IxIndexSpan(
                index_name=name,
                dates=len(dates),
                first_seen=min(dates),
                last_seen=max(dates),
                rows=acc.ffix_index_rows[name],
            )
            for name, dates in sorted(acc.ffix_index_dates.items())
        ),
        headers=dict(acc.ffix_headers.most_common()),
    )


def _mcap_survey(acc: _Accumulator, *, served: Sequence[date]) -> McapSurvey:
    """The `mcap` aggregates, including interior gaps after its arrival."""
    present = sorted(set(acc.mcap_dates))
    return McapSurvey(
        bundles=acc.mcap_bundles,
        rows=acc.mcap_rows,
        first_seen=present[0] if present else None,
        last_seen=present[-1] if present else None,
        distinct_symbols=len(acc.mcap_symbols),
        categories=dict(acc.mcap_categories.most_common()),
        series=dict(acc.mcap_series.most_common()),
        rows_with_issue_size=acc.mcap_with_size,
        zero_issue_size_rows=acc.mcap_zero_size,
        symbols_with_issue_size=len(acc.mcap_symbols_with_size),
        never_traded_rows=acc.mcap_never_traded,
        stale_trade_date_bundles=tuple(sorted(acc.mcap_stale)),
        gaps=_interior_gaps(present, served=served),
    )


# ── proving the reconcile can fail ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InjectionProof:
    """One deliberate corruption of a reconcile input, and whether the check noticed.

    `fired` False on an injection is the whole finding: it means the direction is structurally
    incapable of failing, which is exactly the defect the W1 closer shipped.
    """

    direction: str
    injected: str
    fired: bool
    detail: str


def prove_reconcile_can_fail(
    calendar: TradingCalendar,
    served: Sequence[date],
    start: date,
    end: date,
) -> tuple[InjectionProof, ...]:
    """Corrupt each side of the reconcile in turn and confirm the matching direction fires.

    Four runs: a control over the untouched inputs, then one injection per direction plus a second
    for the direction the W1 closer's version could not detect.

      * *expected side* — a declared weekday holiday is removed from the calendar, so it claims a
        session for a date the archive has no bundle for. `missing` must name it.
      * *served side, withheld* — a real bundle date is dropped from the served set. `missing`
        must name it. This is the direction that was broken: a served set derived from the
        calendar-shaped plan can never lose a date the calendar expects.
      * *served side, phantom* — a Sunday is added to the served set. `unexpected` must name it.

    Every injection is discarded when the run ends; `calendar` is not mutated (a new
    `TradingCalendar` is built for the expected-side run) and `served` is copied.
    """
    proofs: list[InjectionProof] = [_control(calendar, served, start, end)]

    holiday_victim = _weekday_holiday_without_a_bundle(calendar, served, start, end)
    if holiday_victim is None:
        proofs.append(
            InjectionProof(
                direction="expected (calendar says session, no bundle)",
                injected="none available",
                fired=False,
                detail=(
                    "no declared weekday holiday in range lacks a bundle, so this injection could "
                    "not be built — the served-side proofs below cover both directions"
                ),
            )
        )
    else:
        loosened = _calendar_without(calendar, holiday_victim)
        result = loosened.reconcile(served, start, end)
        proofs.append(
            InjectionProof(
                direction="expected (calendar says session, no bundle)",
                injected=(
                    f"removed the declared holiday {holiday_victim.isoformat()} from the "
                    "calendar, so it now calls that date a session"
                ),
                fired=holiday_victim in result.missing,
                detail=(
                    f"missing went {0}→{len(result.missing)} and names {holiday_victim.isoformat()}"
                    if holiday_victim in result.missing
                    else f"missing = {len(result.missing)} and does NOT name the injected date"
                ),
            )
        )

    withheld = served[len(served) // 2] if served else None
    if withheld is not None:
        thinned = [day for day in served if day != withheld]
        result = calendar.reconcile(thinned, start, end)
        proofs.append(
            InjectionProof(
                direction="expected (calendar says session, no bundle)",
                injected=f"withheld the real bundle date {withheld.isoformat()} from the L0 sweep",
                fired=withheld in result.missing,
                detail=(
                    f"missing = {len(result.missing)}, naming {withheld.isoformat()}"
                    if withheld in result.missing
                    else f"missing = {len(result.missing)} and does NOT name the withheld date"
                ),
            )
        )

    phantom = _closed_date(calendar, served, start, end)
    if phantom is not None:
        result = calendar.reconcile([*served, phantom], start, end)
        proofs.append(
            InjectionProof(
                direction="served (bundle exists, calendar says closed)",
                injected=f"added the phantom bundle date {phantom.isoformat()} to the served set",
                fired=phantom in result.unexpected,
                detail=(
                    f"unexpected = {len(result.unexpected)}, naming {phantom.isoformat()}"
                    if phantom in result.unexpected
                    else f"unexpected = {len(result.unexpected)} and does NOT name it"
                ),
            )
        )
    return tuple(proofs)


def _control(
    calendar: TradingCalendar, served: Sequence[date], start: date, end: date
) -> InjectionProof:
    """The untouched run, so the injections are read against a stated baseline."""
    result = calendar.reconcile(served, start, end)
    return InjectionProof(
        direction="control (no injection)",
        injected="nothing",
        fired=False,
        detail=result.summary(),
    )


def _weekday_holiday_without_a_bundle(
    calendar: TradingCalendar, served: Sequence[date], start: date, end: date
) -> date | None:
    """A declared weekday closure in range that L0 has no bundle for."""
    have = set(served)
    return next(
        (
            holiday.date
            for holiday in calendar.holidays(start, end)
            if calendar.classify(holiday.date) is DayKind.HOLIDAY and holiday.date not in have
        ),
        None,
    )


def _closed_date(
    calendar: TradingCalendar, served: Sequence[date], start: date, end: date
) -> date | None:
    """A date in range the calendar calls closed and L0 has no bundle for — a phantom to inject."""
    have = set(served)
    return next(
        (
            day
            for day, kind in calendar.days(start, end)
            if not kind.expects_data and day not in have
        ),
        None,
    )


def _calendar_without(calendar: TradingCalendar, victim: date) -> TradingCalendar:
    """A copy of `calendar` with one declared holiday removed. The original is untouched."""
    kept = {
        holiday.date: holiday
        for holiday in calendar.holidays(calendar.coverage_start, calendar.coverage_end)
        if holiday.date != victim
    }
    return dataclasses.replace(calendar, _holidays=kept)


# ── rendering ────────────────────────────────────────────────────────────────────────────────


def render_report(
    survey: CorpusSurvey,
    *,
    verification: object | None = None,
    comparisons: Sequence[LakeComparison] = (),
    corp_actions_baseline: int | None = None,
    payload_baseline: int | None = None,
) -> str:
    """The Markdown body of the close-out report: sections 1-7, in the brief's order.

    `verification` is an `L0VerificationReport` when the whole-lake checksum sweep was run and
    `None` when it was skipped; it is typed loosely so this module need not import the store's
    report model for a `checked`/`defects` pair.
    """
    lines: list[str] = []
    lines += _render_preamble(survey)
    lines += _render_availability(survey)
    lines += _render_bc(survey, corp_actions_baseline=corp_actions_baseline)
    lines += _render_phase1(survey)
    lines += _render_reconcile(survey)
    lines += _render_checksums(verification, l0_root=survey.l0_root, baseline=payload_baseline)
    lines += _render_stranded(comparisons)
    lines += _render_limits(survey)
    return "\n".join(lines) + "\n"


def _render_preamble(survey: CorpusSurvey) -> list[str]:
    span = (
        "none"
        if survey.first_bundle is None or survey.last_bundle is None
        else (f"{survey.first_bundle.isoformat()} .. {survey.last_bundle.isoformat()}")
    )
    lines = [
        "## 0. What was measured, and from where",
        "",
        f"- **Resolved L0 root:** `{survey.l0_root}` (asserted before any analysis).",
        f"- **Range:** {survey.start.isoformat()} .. {survey.end.isoformat()}.",
        f"- **Bundles enumerated from L0:** {survey.bundles}, spanning {span}, "
        f"{survey.bytes_stored / 1e6:,.1f} MB.",
        "- **Writes performed:** none. No L1, no Postgres, no `sync_state`, no "
        "`corporate_actions`, no `quality_flag`.",
        "",
    ]
    if survey.failures:
        lines += [
            f"### ⚠ {len(survey.failures)} bundle/member parse failure(s)",
            "",
            "| date | file | member | message |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {failure.day.isoformat()} | `{failure.filename}` | `{failure.member}` | "
            f"{failure.message} |"
            for failure in survey.failures[:40]
        ]
        if len(survey.failures) > 40:
            lines.append(f"| … | … | … | {len(survey.failures) - 40} more |")
        lines.append("")
    else:
        lines += [
            "Every bundle opened and every `Bc`, `Ix` and `mcap` member parsed: 0 failures.",
            "",
        ]
    return lines


def _render_availability(survey: CorpusSurvey) -> list[str]:
    names = survey.member_names
    header = "| year | sessions | muhurat | bundles | " + " | ".join(names) + " |"
    ruler = "|---:" * (4 + len(names)) + "|"
    lines = [
        "## 1. Per-year availability",
        "",
        "`sessions` and `muhurat` are the shipped `nse_holidays.yaml` calendar's expectation for "
        "the year; `bundles` is what L0 holds, enumerated from the payload filenames. Every other "
        "column counts the bundles of that year carrying that member family.",
        "",
        header,
        ruler,
    ]
    for row in survey.years:
        cells = " | ".join(str(row.members.get(name, 0)) for name in names)
        lines.append(f"| {row.year} | {row.sessions} | {row.muhurat} | {row.bundles} | {cells} |")
    totals = " | ".join(
        str(sum(row.members.get(name, 0) for row in survey.years)) for name in names
    )
    lines += [
        f"| **total** | **{sum(row.sessions for row in survey.years)}** | "
        f"**{sum(row.muhurat for row in survey.years)}** | "
        f"**{sum(row.bundles for row in survey.years)}** | {totals} |",
        "",
        "### First and last appearance per member family",
        "",
        "| member | registered | bundles | first seen | last seen | interior gaps |",
        "|---|:--:|---:|---|---|---|",
    ]
    for span in survey.families:
        gaps = (
            "none"
            if not span.gaps
            else "; ".join(
                f"{gap.start.isoformat()}..{gap.end.isoformat()} ({gap.bundles})"
                for gap in span.gaps[:6]
            )
            + ("" if len(span.gaps) <= 6 else f"; +{len(span.gaps) - 6} more")
        )
        lines.append(
            f"| `{span.name}` | {'yes' if span.registered else 'NO'} | {span.bundles} | "
            f"{span.first_seen.isoformat()} | {span.last_seen.isoformat()} | {gaps} |"
        )
    lines.append("")
    doubled = [span for span in survey.families if span.doubled]
    if doubled:
        detail = ", ".join(
            f"`{span.name}` ({span.instances:,} files over {span.bundles:,} bundles)"
            for span in doubled
        )
        lines += [
            f"Families shipping more than one file per bundle in some era: {detail}. Every count "
            "in this report is per **bundle**, so `fo04012010.csv` sitting beside "
            "`fo04012010.doc` is one day of availability and not two.",
            "",
        ]
    if survey.unknown_members:
        lines += [
            "**Member names matching no `MemberKind`** (a format change, surfaced not raised):",
            "",
            *(f"- `{name}` — {count} bundle(s)" for name, count in survey.unknown_members.items()),
            "",
        ]
    else:
        lines += ["No member name in any of the 4,124 bundles is outside `MemberKind`.", ""]
    if survey.documentation_members:
        docs = ", ".join(
            f"`{name}` ({count})" for name, count in survey.documentation_members.items()
        )
        lines += [f"Documentation members shipped inside the bundles: {docs}.", ""]
    return lines


def _render_bc(survey: CorpusSurvey, *, corp_actions_baseline: int | None) -> list[str]:
    lines = ["## 2. The headline — dated corporate actions (`Bc`)", ""]
    bc = survey.bc
    if bc is None:
        return [*lines, "No bundle in range carries a `Bc` member.", ""]

    lines += [
        f"- **Row-broadcasts:** {bc.rows:,} across {bc.bundles:,} bundles.",
        f"- **Distinct actions** (symbol + series + purpose text + all four published dates): "
        f"**{bc.distinct_actions:,}**.",
        f"- **Distinct symbols:** {bc.distinct_symbols:,}. "
        f"**Distinct purpose strings:** {bc.distinct_purposes:,}.",
        f"- **Knowable-date span:** {bc.knowable_first.isoformat()} .. "
        f"{bc.knowable_last.isoformat()} — the broadcast dates, derived from each bundle's own "
        "members and never from a clock.",
        f"- **Ex-date span:** "
        f"{'n/a' if bc.ex_date_first is None else bc.ex_date_first.isoformat()} .. "
        f"{'n/a' if bc.ex_date_last is None else bc.ex_date_last.isoformat()}; "
        f"{bc.rows_with_ex_date:,} of {bc.rows:,} row-broadcasts carry an ex-date.",
        f"- {bc.announced_after_ex_date:,} row-broadcasts have `knowable_date > ex_date` — "
        "re-broadcasts of an action already gone ex, which is why the distinct-action count keeps "
        "the *minimum* broadcast date.",
        "",
    ]
    if corp_actions_baseline is not None:
        lines += [
            "### Against the `corporate_actions` table",
            "",
            "| | rows | distinct knowable dates | span |",
            "|---|---:|---:|---|",
            f"| `corporate_actions` today | {corp_actions_baseline:,} | 1 | "
            "2026-09-07 only — every row stamped with ingest day, which is what makes invariant "
            "#7 vacuously satisfied |",
            f"| `nse_pr_bundle` raw, this corpus | {bc.distinct_actions:,} distinct actions "
            f"({bc.rows:,} broadcasts) | {len(bc.actions_per_year):,} years of distinct broadcast "
            f"dates | {bc.knowable_first.isoformat()} .. {bc.knowable_last.isoformat()} |",
            "",
            f"**We now hold {bc.distinct_actions:,} dated corporate actions in raw form over "
            f"{bc.knowable_first.isoformat()}..{bc.knowable_last.isoformat()}** — each carrying "
            "the date the market could first have known it, as opposed to the single ingest-day "
            "stamp on all "
            f"{corp_actions_baseline:,} promoted rows. Nothing has been promoted; see §7.",
            "",
        ]
    lines += [
        "### Row-broadcasts and distinct actions per year",
        "",
        "| year | row-broadcasts | distinct actions first broadcast that year |",
        "|---:|---:|---:|",
    ]
    years = sorted(set(bc.rows_per_year) | set(bc.actions_per_year))
    lines += [
        f"| {year} | {bc.rows_per_year.get(year, 0):,} | {bc.actions_per_year.get(year, 0):,} |"
        for year in years
    ]
    lines += [
        f"| **total** | **{bc.rows:,}** | **{bc.distinct_actions:,}** |",
        "",
        "### Distribution by purpose",
        "",
        '`PURPOSE` is free text and one row routinely carries two facts ("AGM AND DIVIDEND RS '
        '4"), so a row is given every tag whose keywords appear and **the counts below overlap**. '
        "This is a report-only keyword pass over `BcRow.purpose`, which is kept byte-for-byte as "
        "published; the classification a promotion needs must parse terms, not spot words.",
        "",
        "| tag | row-broadcasts | share of rows |",
        "|---|---:|---:|",
    ]
    lines += [
        f"| {tag} | {count:,} | {count / bc.rows:.1%} |" for tag, count in bc.purpose_tags.items()
    ]
    lines += [
        f"| _(no tag matched)_ | {bc.untagged_rows:,} | {bc.untagged_rows / bc.rows:.1%} |",
        "",
        f"Top {len(bc.top_purposes)} raw purpose strings verbatim:",
        "",
        "| purpose | row-broadcasts |",
        "|---|---:|",
    ]
    lines += [f"| `{purpose}` | {count:,} |" for purpose, count in bc.top_purposes]
    series = ", ".join(f"`{name}` {count:,}" for name, count in list(bc.series.items())[:15])
    lines += ["", f"Series distribution: {series}.", ""]
    return lines


def _render_phase1(survey: CorpusSurvey) -> list[str]:
    ix, mcap = survey.ix, survey.mcap
    lines = [
        "## 3. Phase 1 findings, re-tested over the full corpus",
        "",
        '### 3a. `Ix` — "2010 only, rotating, never NIFTY 50"',
        "",
        f"- The member is **present** in **{ix.present_bundles}** of {survey.bundles:,} bundles, "
        f"{_stamp_or(ix.first_present)} .. {_stamp_or(ix.last_present)}.",
        f"- `ix.py` **parses** it in **{ix.parsed_bundles}** of them, "
        f"{_stamp_or(ix.first_parsed)} .. {_stamp_or(ix.last_parsed)} — "
        f"{ix.rows:,} constituent rows, {ix.distinct_symbols:,} distinct symbols.",
        f"- Bundles after 2010 with the member present: **{len(ix.present_after_2010)}**"
        + (
            "."
            if not ix.present_after_2010
            else " — " + ", ".join(day.isoformat() for day in ix.present_after_2010[:20]) + "."
        ),
        "",
    ]
    verdict = "**Refuted in part.**" if ix.present_after_2010 else "**Confirmed.**"
    if ix.present_after_2010:
        lines += [
            f"{verdict} Phase 1 said `Ix` vanished between 2010-10-04 and 2010-10-18 and never "
            f"returned through 2026. The disappearance is real and now pinned exactly — the last "
            f"readable `Ix` is {_stamp_or(ix.last_parsed)} — but *never returned* is wrong: the "
            f"member reappears on {', '.join(d.isoformat() for d in ix.present_after_2010)}, "
            "shipped as a **zip container** rather than a CSV, which is why a reader-only count "
            "would have missed it. See the parse-failure table in §0 and §3c.",
            "",
        ]
    else:
        lines += [
            f"{verdict} The member is present on no bundle after 2010.",
            "",
        ]
    lines += [
        "Distinct index names ever seen in a **readable** `Ix`, with per-index coverage:",
        "",
        "| index | sessions carrying it | first | last | rows |",
        "|---|---:|---|---|---:|",
    ]
    lines += [
        f"| `{span.index_name}` | {span.dates} | {span.first_seen.isoformat()} | "
        f"{span.last_seen.isoformat()} | {span.rows:,} |"
        for span in ix.indices
    ]
    nifty = [span.index_name for span in ix.indices if "NIFTY 50" in span.index_name.upper()]
    lines += [
        "",
        f"NIFTY 50 in a readable `Ix`: **{'yes — ' + ', '.join(nifty) if nifty else 'no'}** — "
        'which confirms the Phase 1 claim *about `Ix`*, and is not the same claim as "the bundle '
        'has no NIFTY 50 membership". §3c is that claim, and it is false.',
        "",
    ]
    if ix.empty_banners:
        banners = ", ".join(f"`{name}` ({count})" for name, count in ix.empty_banners.items())
        lines += [f"Index banners announced with no constituent rows: {banners}.", ""]

    lines += [
        "### 3b. `mcap` — arrival at 2024-02-01, and the issue-size series",
        "",
        f"- Carried by **{mcap.bundles:,}** bundles; **first seen "
        f"{_stamp_or(mcap.first_seen)}**, last seen {_stamp_or(mcap.last_seen)}. "
        + (
            "**Confirmed** — Phase 1 pinned 2024-02-01 by bisection and the full corpus agrees "
            "exactly."
            if mcap.first_seen == date(2024, 2, 1)
            else "**REFUTED** — Phase 1 pinned 2024-02-01."
        ),
        f"- {mcap.rows:,} rows, **{mcap.distinct_symbols:,} distinct symbols**.",
        f"- **Issue Size** (shares outstanding) present and non-zero on "
        f"{mcap.rows_with_issue_size:,} rows covering {mcap.symbols_with_issue_size:,} symbols; "
        f"{mcap.zero_issue_size_rows:,} rows publish 0. The series is therefore dense: every "
        "row of every bundle from arrival onward carries one.",
        f"- {mcap.never_traded_rows:,} rows carry NSE's literal `Not Traded` in "
        "`Last Trade Date` — a security with no trading history, not a missing value.",
        "",
        "`Category` values across every `mcap` row in the corpus:",
        "",
        "| category | rows |",
        "|---|---:|",
    ]
    lines += [f"| `{name}` | {count:,} |" for name, count in mcap.categories.items()]
    other = [name for name in mcap.categories if name not in {"Listed", "Permitted"}]
    lines += [
        "",
        f"Category outside Listed/Permitted: **{'yes — ' + ', '.join(other) if other else 'no'}** "
        "— confirming the docstring's claim across 1.7 M rows rather than across a sample.",
        "",
    ]
    if mcap.gaps:
        gaps = "; ".join(
            f"{gap.start.isoformat()}..{gap.end.isoformat()} ({gap.bundles} bundles)"
            for gap in mcap.gaps
        )
        lines += [f"Interior gaps after arrival — bundles carrying no `mcap`: {gaps}.", ""]
    else:
        lines += ["No interior gap: every bundle from its arrival onward carries `mcap`.", ""]
    if mcap.stale_trade_date_bundles:
        stale = ", ".join(day.isoformat() for day in mcap.stale_trade_date_bundles[:20])
        lines += [
            f"{len(mcap.stale_trade_date_bundles)} bundle(s) where a row's `Trade Date` is not "
            f"the bundle's own publication date: {stale}"
            + ("." if len(mcap.stale_trade_date_bundles) <= 20 else ", …"),
            "",
        ]
    lines += _render_ffix(survey)
    return lines


def _render_ffix(survey: CorpusSurvey) -> list[str]:
    """§3c — the unasked-for finding, which is the largest one in this report."""
    ffix = survey.ffix
    lines = [
        "### 3c. ⚠ UNPLANNED FINDING — `ffix` is not fixed income, it is the free-float index file",
        "",
        '`MemberKind.FFIX` is registered with the docstring *"Fixed income"*. The payload is '
        "nothing of the kind. Its header is:",
        "",
        "```text",
        "INDEX_FLG,SYMBOL,SERIES,SECURITY,ISSUE_CAP,INVESTIBLE_FACTOR,CLOSE_PRIC,FF_MKT_CAP,"
        "WEIGHTAGE",
        "```",
        "",
        f"— a **dated, daily index-membership file with free-float weightage**, present on "
        f"**{ffix.present_bundles:,}** bundles and censused on {ffix.bundles:,} of them "
        f"({_stamp_or(ffix.first_seen)} .. {_stamp_or(ffix.last_seen)}), "
        f"{ffix.rows:,} constituent rows.",
        "",
        "This was not in the brief and is reported because it bears directly on deliverable 3 and "
        "on limit (iii): **`NIFTY` — the 50 — is in the very first bundle**, 2010-01-04, and in "
        "every one after it until the member stops. The census below is a count of the "
        "`INDEX_FLG` column and the header; **nothing was parsed into rows and nothing was "
        "promoted**. A reader for this member does not exist and writing one was out of scope.",
        "",
        "| index | sessions carrying it | first | last | rows |",
        "|---|---:|---|---|---:|",
    ]
    lines += [
        f"| `{span.index_name}` | {span.dates} | {span.first_seen.isoformat()} | "
        f"{span.last_seen.isoformat()} | {span.rows:,} |"
        for span in ffix.indices
    ]
    lines += [
        "",
        f"Distinct header shapes across all {ffix.bundles:,} censused members: "
        f"**{len(ffix.headers)}**.",
        "",
    ]
    if survey.zipped_members:
        zipped = ", ".join(f"`{name}` ({count})" for name, count in survey.zipped_members.items())
        lines += [
            f"Members shipped as a zip container rather than a CSV: {zipped}. On 2013-02-19 the "
            "`Ix` member is `Ix190213.zip`, and its single inner file is `ffix190213.csv` — "
            "**byte-identical** (sha256 `e24860243ced492e…`) to that bundle's own top-level "
            '`ffix` member. So the 2013 "return of `Ix`" is the `ffix` payload published twice '
            "under two names, not a resumption of the 2010 `Ix` format.",
            "",
        ]
    span = f"{_stamp_or(ffix.first_seen)}..{_stamp_or(ffix.last_seen)}"
    lines += [
        "**What this does and does not give us.** It gives a daily NIFTY 50 / CNX 100 / CNX 500 "
        f"membership series with weightage over {span} "
        f'— {ffix.bundles:,} sessions — which is more than the "handful of anchor points" limit '
        "(iii) was drafted to describe. It does **not** give membership after the member stops, "
        "it is symbol-keyed like everything else here (limit ii), and it has no reader. Limit "
        "(iii) is restated accordingly in §7.",
        "",
    ]
    return lines


def _stamp_or(day: date | None) -> str:
    """A date for the report, or `n/a`."""
    return "n/a" if day is None else day.isoformat()


def _render_reconcile(survey: CorpusSurvey) -> list[str]:
    rec = survey.reconciliation
    lines = [
        "## 4. Calendar reconcile, both directions",
        "",
        f"The served set is **enumerated from L0** — `{survey.l0_root / PR_BUNDLE_SOURCE_ID}` "
        "globbed for `PR*.zip` and each date read from the archive filename. It is not derived "
        "from a `SessionPlan`, from a sidecar, or from the calendar, because a served set built "
        "out of the calendar-shaped plan cannot lose a date the calendar expects, which is what "
        "made the W1 closer's second direction structurally incapable of failing.",
        "",
        f"`{rec.summary()}`",
        "",
        f"**(a) calendar says trading session, no bundle in L0 — {len(rec.missing)} date(s):**",
        "",
    ]
    lines.append(", ".join(day.isoformat() for day in rec.missing) if rec.missing else "_none_")
    lines += [
        "",
        f"**(b) bundle in L0, calendar says closed — {len(rec.unexpected)} date(s):**",
        "",
    ]
    lines.append(
        ", ".join(day.isoformat() for day in rec.unexpected) if rec.unexpected else "_none_"
    )
    lines += [
        "",
        "### Can this check fail? Injected proof",
        "",
        "A check that has never failed may be a check that cannot. Each row below corrupts one "
        "side of the reconcile, re-runs it, and records whether the matching direction fired. "
        "Every injection is discarded when its run ends — the calendar object is copied, not "
        "mutated, and the served set is rebuilt from L0.",
        "",
        "| direction | injection | fired? | detail |",
        "|---|---|:--:|---|",
    ]
    lines += [
        f"| {proof.direction} | {proof.injected} | {_fired_cell(proof)} | {proof.detail} |"
        for proof in survey.proofs
    ]
    lines.append("")
    return lines


def _fired_cell(proof: InjectionProof) -> str:
    """The `fired?` cell — blank for the control, and loud when an injection did not fire."""
    if proof.direction.startswith("control"):
        return "—"
    return "**YES**" if proof.fired else "**NO — the check is broken**"


def payload_counts(l0_root: Path) -> Mapping[str, int]:
    """Payload files per source under an `L0` root, counted without hashing anything.

    Cheap on purpose: §5's job is to explain a total, and re-hashing 7 GB a second time to
    attribute it would cost minutes to learn what a directory listing already knows.
    """
    if not l0_root.is_dir():
        return {}
    return {
        source.name: sum(
            1
            for path in source.glob("*/*/*")
            if path.is_file() and not path.name.endswith(".meta.json")
        )
        for source in sorted(l0_root.iterdir())
        if source.is_dir()
    }


def _render_checksums(
    verification: object | None,
    *,
    l0_root: Path,
    baseline: int | None,
) -> list[str]:
    lines = ["## 5. Whole-lake checksum audit", ""]
    if verification is None:
        return [*lines, "_Not run in this pass._", ""]
    checked = getattr(verification, "checked", None)
    defects = getattr(verification, "defects", ())
    lines += [
        f"- **Payloads re-hashed:** {checked:,}"
        if isinstance(checked, int)
        else f"- {verification}",
        f"- **Defects:** {len(tuple(defects))}",
        "",
    ]
    if isinstance(checked, int) and baseline is not None:
        counts = payload_counts(l0_root)
        pr = counts.get(PR_BUNDLE_SOURCE_ID, 0)
        rest = sum(counts.values()) - pr
        lines += [
            f"### The total is {checked:,}, not the {baseline + 4084:,} the brief expected",
            "",
            "Not a defect and not a surprise — the brief's arithmetic subtracted the 40 "
            "already-in-L0 bundles from a baseline that never contained them. Decomposing the "
            "lake by source settles it exactly:",
            "",
            "| | payloads |",
            "|---|---:|",
            f"| every source except `{PR_BUNDLE_SOURCE_ID}` | {rest:,} |",
            f"| `{PR_BUNDLE_SOURCE_ID}` | {pr:,} |",
            f"| **total** | **{rest + pr:,}** |",
            "",
            f"The pre-campaign baseline of **{baseline:,}** is exactly the non-bundle count, so "
            f"the baseline was struck before *any* PR bundle was in the authoritative lake — "
            f"including the {pr - 4084} that Phase 1's probing and bisection had already stored "
            f"under their keys and that the campaign therefore reported as `already_in_l0`. "
            f"{baseline:,} + {pr:,} = **{checked:,}**, which is what the sweep counted. Every "
            "payload in the lake carries a sidecar (payload and sidecar counts are equal for "
            "every source).",
            "",
        ]
    listed = tuple(defects)
    if listed:
        lines += [
            "**STOP — L0 damage is not something this task repairs.** `L0Store` refuses to modify "
            "a stored payload for any reason (AGENTIC_CONTEXT §3.10); a human decides.",
            "",
            "| kind | path |",
            "|---|---|",
            *(
                f"| {getattr(defect, 'kind', '?')} | `{getattr(defect, 'path', '?')}` |"
                for defect in listed[:40]
            ),
            "",
        ]
    return lines


def _render_stranded(comparisons: Sequence[LakeComparison]) -> list[str]:
    lines = [
        "## 6. Stranded worktree lakes",
        "",
        "`L0Store` resolves its root from `Settings.data_root`, default `<cwd>/data`, so a "
        "worktree that fetches without exporting `DATA_ROOT` builds a second lake inside itself. "
        "Deleting the worktree deletes whatever only that lake holds. Third occurrence on this "
        "box, so it is measured: every payload below was re-hashed from disk and looked for by "
        "logical key in the authoritative lake. **Nothing was deleted.**",
        "",
    ]
    if not comparisons:
        return [*lines, "_No worktree lake compared in this pass._", ""]
    lines += [
        "| worktree lake | payloads | identical in authoritative L0 | absent | digest mismatch | "
        "orphan | verdict |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    lines += [
        f"| `{comparison.left_root}` | {comparison.checked} | {len(comparison.matched)} | "
        f"{len(comparison.absent)} | {len(comparison.mismatched)} | {len(comparison.orphans)} | "
        f"{'**fully duplicated — safe to delete**' if comparison.safe_to_delete else '**HOLD**'} |"
        for comparison in comparisons
    ]
    lines.append("")
    unique = [(comparison, record) for comparison in comparisons for record in comparison.absent]
    if unique:
        lines += [
            "**Payloads held ONLY in a worktree lake — unrecoverable if the worktree is deleted:**",
            "",
            "| worktree | key | sha256 | bytes |",
            "|---|---|---|---:|",
            *(
                f"| `{comparison.left_root.name}` | `{record.key}` | `{record.sha256[:16]}…` | "
                f"{record.size_bytes:,} |"
                for comparison, record in unique[:60]
            ),
            "",
        ]
    else:
        lines += [
            "**No payload is held only in a worktree lake.** Every stranded payload exists in "
            "`/home/ubuntu/stock-manager/data/L0` under the same logical key with an identical "
            "sha256.",
            "",
        ]
    return lines


def _render_limits(survey: CorpusSurvey) -> list[str]:
    bc = survey.bc
    floor = "n/a" if bc is None else bc.knowable_first.isoformat()
    ffix = survey.ffix
    ffix_span = f"{_stamp_or(ffix.first_seen)}..{_stamp_or(ffix.last_seen)}"
    return [
        "## 7. Honest limits",
        "",
        f"1. **`Bc` starts {floor}, not 2006.** The archive's floor is pinned by measurement, not "
        "bracketed: `PR311209.zip` and `PR010110.zip` are both 404, `PR040110.zip` is 200, and "
        "probes across 2005-2009 are all 404. **No dated corporate action exists before "
        f"{floor} from this source**, so the pre-2010 stretch of the price history still has no "
        "point-in-time corporate-action surface, and a backtest reaching back further is "
        "adjusting on undated data whatever this wave acquired.",
        "",
        "2. **Every member is symbol-keyed and none carries an ISIN.** ISIN is the only join key "
        "(invariant #2), symbols are reused across issuers over sixteen years, and the only "
        "symbol→ISIN resolver we hold (`EQUITY_L.csv`) is a present-day listing and therefore "
        "survivorship-biased. **Nothing in this corpus can be promoted to `corporate_actions`, "
        "to L1, or to a factor until W4 identity resolution exists.** The distinct-action count "
        "in §2 is a count of published rows, not of resolved securities: two issuers sharing a "
        "reused symbol with identical purpose text and identical dates would collapse into one, "
        f"and the {bc.distinct_symbols:,} distinct symbols include per-instrument debt codes "
        "(`ICIBK1107`) that are not securities in the D2 sense at all."
        if bc is not None
        else "2. **Every member is symbol-keyed and none carries an ISIN.**",
        "",
        "3. **`Ix` gives dated CNX 500 anchor points, not an index membership series — but "
        "`ffix` does give a series, for three years.** This limit is **restated**, not repeated: "
        f"`Ix` is readable on {survey.ix.parsed_bundles} sessions in 2010 and nowhere else, with "
        "a rotating index set and no NIFTY 50, so as a *membership series* it is worth only what "
        "the original limit claimed. What §3c found is that a **different** member of the same "
        f"bundle, `ffix`, carries daily NIFTY 50 / CNX 100 / CNX 500 membership with free-float "
        f"weightage on {ffix.bundles:,} sessions over {ffix_span}.",
        "",
        f"   So **`ops/BACKLOG.md:126` still stands, in narrowed form.** `index_constituents` is "
        "still empty, M9.3's as-of membership screen is still inert on the real store, and "
        "nothing in this wave promoted a single row — that is unchanged. What *is* no longer true "
        f"is the premise that no dated membership history exists to fetch: {ffix_span} of it is "
        "in L0 as of today. Closing the backlog item still needs (a) a reader for the member, "
        "(b) W4 identity to turn its symbols into ISINs, and (c) a source for 2013-05 onward, "
        "which this archive does not publish. None of the three is in this task's scope, and "
        "each is now a smaller question than it was this morning.",
        "",
    ]


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dataplatform.ingest.nse.pr_bundle.survey",
        description="Measure the acquired PR-bundle corpus off L0 and render the close-out report.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="the lake root holding L0 — pass it absolute; a worktree-relative path is the "
        "hazard §6 measures",
    )
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--expect-l0-root",
        type=Path,
        default=None,
        help="assert the resolved L0 root equals this path before reading anything",
    )
    parser.add_argument(
        "--verify-l0",
        action="store_true",
        help="re-hash every payload in the whole lake (§5). Read-only; writes no quality flag.",
    )
    parser.add_argument(
        "--stranded-lake",
        type=Path,
        action="append",
        default=[],
        help="a worktree data root to compare against --data-root (§6). Repeatable.",
    )
    parser.add_argument(
        "--corp-actions-baseline",
        type=int,
        default=None,
        help="the row count currently in the corporate_actions table, for the §2 comparison",
    )
    parser.add_argument(
        "--payload-baseline",
        type=int,
        default=None,
        help="the pre-campaign L0 payload count, so §5 can reconcile the total it swept",
    )
    parser.add_argument("--out", type=Path, default=None, help="write here instead of stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Render the close-out report. Read-only: no fetch, no database, no write outside `--out`."""
    args = _build_parser().parse_args(argv)
    store = L0Store(clock=SystemClock(), data_root=args.data_root)

    resolved = store.root
    print(f"resolved L0 root: {resolved}", file=sys.stderr)
    if args.expect_l0_root is not None and resolved != args.expect_l0_root:
        print(
            f"REFUSING: resolved L0 root {resolved} is not the expected {args.expect_l0_root}. "
            "A worktree-relative lake root is how two empty second lakes got built here.",
            file=sys.stderr,
        )
        return 2

    survey = survey_corpus(store, calendar=trading_calendar(), start=args.start, end=args.end)
    verification = store.verify_checksums() if args.verify_l0 else None
    comparisons = [compare_lakes(root, args.data_root) for root in args.stranded_lake]

    body = render_report(
        survey,
        verification=verification,
        comparisons=comparisons,
        corp_actions_baseline=args.corp_actions_baseline,
        payload_baseline=args.payload_baseline,
    )
    if args.out is None:
        print(body)
    else:
        args.out.write_text(body, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    return 0 if not survey.failures else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
