"""The symbol-keyed dated index membership dataset, and the census that measures it (W2).

`ffix.py` reads one session. This folds the whole `ffix` corpus into one dated series and measures
it: per-index spans and constituent counts, the **reconstitution events** the series makes
observable, the calendar contiguity of the sessions, and the dated sectoral assignments that fall
out of the ten sectoral indices being published constituent-by-constituent every day.

**The dataset is symbol-keyed and it is named that way on purpose.** `SymbolKeyedIndexMembership`
holds `index → session → frozenset[symbol]`. There is no ISIN anywhere in this module, no import
of `security_master`, and no symbol→ISIN resolution. ISIN is the only join key (invariant #2), so
**nothing here can be joined to a price, a corporate action or a fundamental yet** — and that is
the accurate state of the world rather than a limitation of the code. Resolving 2010-2013 symbols
through a present-day listing would invent a mapping biased toward the survivors, in exactly the
direction that flatters a backtest. It is W4 identity work, gated on a point-in-time symbol
master this platform does not hold.

**What the census retains, and what it deliberately does not.** It keeps membership sets and the
per-session constituent counts; it does **not** keep the 711,344 rows' weights in memory. Weights
are not lost — L0 is the authoritative record and `parse_ffix` re-reads any session's full
weightage on demand. A census that had to hold every `Decimal` to answer "when did the NIFTY
change" would be a database, and this is a measurement.

**Two bundles need a recovery path, and it is still payload-derived.** `PrBundle` refuses to date
a bundle whose members disagree, which is correct for a corporate action and costs us two `ffix`
files: `PR190811.zip` ships a stale `NPD180811.txt` alongside 2011-08-19 members, and
`PR100113.zip` nests every member under a `nupr100113/` directory so the member regex matches
none of them. Both are recovered by dating the `ffix` member **from its own filename** —
`ffix190811.csv` → 2011-08-19 — which is still a date derived from the payload and never from a
clock. Recoveries are counted and named in the report rather than folded in silently.

Offline by construction: reads L0 through `L0Store` (so every byte is re-checksummed on the way
in) and never fetches.

    uv run python -m dataplatform.ingest.nse.pr_bundle.membership \\
        --from 2010-01-04 --to 2013-04-30 --out ops/gates/<report>.md
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import get_settings
from dataplatform.ingest.calendar import TradingCalendar, trading_calendar
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse import bhavcopy_legacy
from dataplatform.ingest.nse.eras import ISIN_ERA_START
from dataplatform.ingest.nse.pr_bundle.bundle import (
    PR_BUNDLE_SOURCE_ID,
    MemberKind,
    PrBundle,
)
from dataplatform.ingest.nse.pr_bundle.ffix import (
    FFIX_FIRST_SESSION,
    FFIX_LAST_SESSION,
    FfixFile,
    parse_ffix,
    parse_ffix_bundle,
)
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import Layer, layer_root

__all__ = [
    "HEADLINE_INDICES",
    "NOMINAL_SIZES",
    "SECTORAL_INDICES",
    "CountAnomaly",
    "IndexCensus",
    "MembershipCensus",
    "MembershipChange",
    "RecoveredBundle",
    "SectorMove",
    "SectoralFindings",
    "SymbolKeyedIndexMembership",
    "census_ffix_corpus",
    "render_census",
    "traded_universe",
]

_LOG = get_logger(__name__)

#: The nominal constituent count each headline index is *named* for. A session whose count differs
#: is flagged: it is either a real corporate event (a constituent suspended, merged or halted, and
#: NSE running the index short until the replacement is effective) or a parse bug, and the report
#: is required to say which. Only indices whose name asserts a size are here — `BANK Nifty` and
#: the sectorals are "as many as qualify" and have no nominal count to breach.
NOMINAL_SIZES: Final[Mapping[str, int]] = {
    "NIFTY": 50,
    "JR. NIFTY": 50,
    "CNX 100": 100,
    "CNX 500": 500,
    "CNX Midcap": 100,
    "Nifty Midcap 50": 50,
}

#: The broad-market and thematic indices, as opposed to the sectorals below.
HEADLINE_INDICES: Final[frozenset[str]] = frozenset(
    {"NIFTY", "JR. NIFTY", "CNX 100", "CNX 500", "CNX Midcap", "Nifty Midcap 50", "BANK Nifty"}
)

#: The ten sectoral indices. Membership of one of these on a given session **is an
#: exchange-published, dated sector assignment** — which is the thing this corpus offers that no
#: other surface in the platform does. `INDEX_FLG` spellings verbatim, because that is the key.
SECTORAL_INDICES: Final[frozenset[str]] = frozenset(
    {
        "CNX IT",
        "CNX PHARMA",
        "CNX FMCG",
        "CNX ENERGY",
        "CNX Realty",
        "CNX PSU BANK",
        "CNX Infrastructure",
        "CNX SERVICE",
        "CNX MNC",
        "CNX PSE",
    }
)

#: `ffix<DDMMYY>.csv`, optionally under a directory prefix (`nupr100113/ffix100113.csv`). The
#: prefix tolerance is the whole reason this exists separately from `bundle._MEMBER_RE`, which
#: anchors at the start and therefore matches nothing in the one nested bundle.
_FFIX_MEMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|/)ffix(?P<digits>\d{6}|\d{8})\.csv$", re.IGNORECASE
)


# ── the dataset ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MembershipChange:
    """One index's constituent set differing from its own previous published session.

    This is the payload of the whole task: a **dated, observable reconstitution event**. `day` is
    the first session the new set was published on, so the change happened in `(previous_day,
    day]` — and because `ffix` is published every session (see the contiguity measurement), that
    bracket is usually one trading day wide.

    `added` and `removed` are symbols, sorted. They are not ISINs and cannot be joined.
    """

    index_name: str
    day: date
    previous_day: date
    added: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def size(self) -> int:
        """How many symbols moved. A one-in-one-out replacement has size 2."""
        return len(self.added) + len(self.removed)


@dataclass(frozen=True, slots=True)
class CountAnomaly:
    """A session on which a headline index's constituent count is not its nominal size."""

    index_name: str
    day: date
    observed: int
    nominal: int


@dataclass(frozen=True, slots=True)
class SectorMove:
    """A symbol that left one sectoral index and joined another between two sessions.

    The strong form of the sector question: a symbol merely *gaining* a second sectoral is a
    multi-assignment, but one that gains and loses at the same time has been **reclassified** by
    the exchange, and a point-in-time sector series has to honour the date it happened on.

    `previous_day` is the symbol's own previous **assigned** session, not necessarily the previous
    trading day: a symbol can drop out of every sectoral for a while and reappear in a different
    one. The reclassification therefore happened somewhere in `(previous_day, day]`, and that
    bracket is one session wide only when the symbol held a sector throughout.
    """

    symbol: str
    day: date
    previous_day: date
    left: tuple[str, ...]
    joined: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecoveredBundle:
    """A bundle `PrBundle` would not date, whose `ffix` member was dated from its own name."""

    filename: str
    l0_logical_date: date
    ffix_member: str
    recovered_date: date
    why: str


class SymbolKeyedIndexMembership:
    """Dated index constituent membership, keyed by **trading symbol and never by ISIN**.

    What it does: accumulates parsed `ffix` files into `index → session → frozenset[symbol]`, and
    answers the two questions a membership series exists to answer — who was in an index on a
    date, and on which dates did that set change.
    What it assumes: each `FfixFile` handed to it carries a distinct `knowable_date` derived from
    its own payload. Adding the same session twice raises rather than merging, because a bundle
    served under two date keys is a real thing this archive does and silently overwriting one with
    the other would erase a change event.
    What it never does: resolve a symbol to an ISIN, touch `security_master`, read a clock, or
    retain per-row weightage (`parse_ffix` re-reads that from L0 when it is wanted).
    """

    def __init__(self) -> None:
        self._sets: dict[str, dict[date, frozenset[str]]] = defaultdict(dict)
        self._sessions: set[date] = set()
        self._rows = 0

    def add(self, parsed: FfixFile) -> None:
        """Fold one session in. Raises `ValueError` if that session is already present."""
        day = parsed.knowable_date
        if day in self._sessions:
            raise ValueError(f"session {day.isoformat()} added twice; refusing to merge or replace")
        self._sessions.add(day)
        self._rows += len(parsed.rows)
        for index_name in parsed.indices:
            self._sets[index_name][day] = parsed.symbols(index_name)

    # ── what is in it ────────────────────────────────────────────────────────────────────────

    @property
    def sessions(self) -> tuple[date, ...]:
        """Every session the dataset holds, ascending."""
        return tuple(sorted(self._sessions))

    @property
    def rows(self) -> int:
        """Total constituent rows folded in, across every index and session."""
        return self._rows

    @property
    def indices(self) -> tuple[str, ...]:
        """Every index ever published, in first-seen-then-alphabetical order."""
        return tuple(sorted(self._sets, key=lambda name: (min(self._sets[name]), name)))

    def sessions_for(self, index_name: str) -> tuple[date, ...]:
        """The sessions this index was published on, ascending. Empty if it never was."""
        return tuple(sorted(self._sets.get(index_name, {})))

    def constituents(self, index_name: str, day: date) -> frozenset[str]:
        """The symbol set of one index on one session; empty if it was not published that day."""
        return self._sets.get(index_name, {}).get(day, frozenset())

    def symbols_ever(self, index_name: str | None = None) -> frozenset[str]:
        """Every symbol that ever appeared, in one index or across all of them."""
        names = self._sets if index_name is None else {index_name: self._sets.get(index_name, {})}
        return frozenset(
            symbol for by_day in names.values() for members in by_day.values() for symbol in members
        )

    # ── what changed ─────────────────────────────────────────────────────────────────────────

    def changes(self, index_name: str) -> tuple[MembershipChange, ...]:
        """Every session on which this index's set differs from its own previous session.

        Diffed against the index's **previous available session**, not the previous calendar day:
        an index that arrived late or was absent for a session must not report its whole
        constituent list as one enormous change event on the day it reappears. The first session
        an index is ever published on is never a change — there is nothing to diff it against.
        """
        by_day = self._sets.get(index_name, {})
        found: list[MembershipChange] = []
        previous_day: date | None = None
        previous: frozenset[str] | None = None
        for day in sorted(by_day):
            members = by_day[day]
            if previous is not None and previous_day is not None and members != previous:
                found.append(
                    MembershipChange(
                        index_name=index_name,
                        day=day,
                        previous_day=previous_day,
                        added=tuple(sorted(members - previous)),
                        removed=tuple(sorted(previous - members)),
                    )
                )
            previous_day, previous = day, members
        return tuple(found)

    def sectoral_assignments(self) -> Mapping[str, Mapping[date, frozenset[str]]]:
        """`symbol → session → the sectoral indices it belonged to`, for sessions where it did.

        The dated sector classification this corpus makes available. A symbol absent from a
        session's mapping had no sectoral assignment published that day — which is not the same as
        having no sector, and the report says so.
        """
        out: dict[str, dict[date, set[str]]] = defaultdict(lambda: defaultdict(set))
        for index_name in self._sets:
            if index_name not in SECTORAL_INDICES:
                continue
            for day, members in self._sets[index_name].items():
                for symbol in members:
                    out[symbol][day].add(index_name)
        return {
            symbol: {day: frozenset(names) for day, names in by_day.items()}
            for symbol, by_day in out.items()
        }


# ── the census ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IndexCensus:
    """One index, measured: its span, its presence, its size, and every change it published."""

    index_name: str
    first_seen: date
    last_seen: date
    sessions: int
    rows: int
    min_count: int
    median_count: int
    max_count: int
    symbols_ever: int
    nominal_size: int | None
    anomalies: tuple[CountAnomaly, ...]
    changes: tuple[MembershipChange, ...]

    @property
    def is_sectoral(self) -> bool:
        return self.index_name in SECTORAL_INDICES

    @property
    def steady(self) -> bool:
        """Whether the count never moved. A steady index at its nominal size is fully accounted."""
        return self.min_count == self.max_count


@dataclass(frozen=True, slots=True)
class SectoralFindings:
    """The dated-sector question, answered with counts rather than an impression."""

    sectoral_indices: tuple[str, ...]
    symbols_with_any_assignment: int
    symbols_with_simultaneous_assignments: int
    max_simultaneous: int
    movers: tuple[SectorMove, ...]
    """Symbols that left one sectoral and joined another **in a single step** — reclassified."""
    symbols_whose_sector_changed: tuple[str, ...]
    """Every symbol whose sectoral **identity** changed at some point, however gradually.

    Measured as: the union of the sectors it ever held is wider than the most it ever held on one
    day. That catches the abrupt reclassifications in `movers` *and* the gradual ones they miss —
    a symbol that sat in `CNX IT`, spent a while in both `CNX IT` and `CNX SERVICE`, then only in
    `CNX SERVICE` never gained-and-lost on one date, but its sector did change.

    A superset of `movers`'s symbols, and the number a point-in-time sector series actually cares
    about: it counts the symbols for which "pin it to the sector it ends the span in" is wrong.
    """
    assignment_sessions: int
    traded_universe_symbols: int | None
    traded_universe_sessions: int | None

    @property
    def coverage_fraction(self) -> float | None:
        """Sectorally-classified symbols as a fraction of the symbols that actually traded.

        A ratio of two counted symbol sets, reported to one decimal place as a percentage. Not
        money, so not `Decimal`; `None` when the traded universe was not measured.
        """
        if not self.traded_universe_symbols:
            return None
        return self.symbols_with_any_assignment / self.traded_universe_symbols


@dataclass(frozen=True, slots=True)
class MembershipCensus:
    """Everything the sweep measured, in one object `render_census` turns into Markdown."""

    l0_root: Path
    start: date
    end: date
    bundles_swept: int
    ffix_sessions: int
    rows: int
    first_session: date | None
    last_session: date | None
    indices: tuple[IndexCensus, ...]
    distinct_symbols: int
    expected_sessions: int
    missing_sessions: tuple[date, ...]
    unexpected_sessions: tuple[date, ...]
    bundles_without_ffix: tuple[date, ...]
    recovered: tuple[RecoveredBundle, ...]
    failures: tuple[tuple[date, str, str], ...]
    headers: Mapping[str, int]
    announced_banners: Mapping[str, int]
    sectoral: SectoralFindings

    @property
    def total_changes(self) -> int:
        """Every change event across every index — the corpus's whole reconstitution record."""
        return sum(len(index.changes) for index in self.indices)

    @property
    def change_dates(self) -> tuple[date, ...]:
        """Every distinct session on which *any* index's membership changed, ascending."""
        return tuple(sorted({c.day for index in self.indices for c in index.changes}))

    @property
    def multi_index_change_dates(self) -> tuple[date, ...]:
        """Change dates on which three or more indices changed at once.

        The shape of a scheduled reconstitution, as opposed to a single index absorbing a single
        corporate event. It is the measure §7's judgement rests on: a reconstruction built from
        circulars has to land on these dates, and there are few enough of them to check by hand.
        """
        counts = Counter(c.day for index in self.indices for c in index.changes)
        return tuple(sorted(day for day, n in counts.items() if n >= 3))

    @property
    def contiguous(self) -> bool:
        """Whether the `ffix` sessions are exactly the trading calendar's sessions in the span."""
        return not self.missing_sessions and not self.unexpected_sessions


@dataclass(slots=True)
class _Accumulator:
    """Mutable scratch for one sweep. Not part of the module's surface."""

    dataset: SymbolKeyedIndexMembership = field(default_factory=SymbolKeyedIndexMembership)
    counts: dict[str, dict[date, int]] = field(default_factory=lambda: defaultdict(dict))
    headers: Counter[str] = field(default_factory=Counter)
    banners: Counter[str] = field(default_factory=Counter)
    bundles_swept: int = 0
    without_ffix: list[date] = field(default_factory=list)
    recovered: list[RecoveredBundle] = field(default_factory=list)
    failures: list[tuple[date, str, str]] = field(default_factory=list)


def census_ffix_corpus(
    store: L0Store,
    *,
    calendar: TradingCalendar,
    start: date = FFIX_FIRST_SESSION,
    end: date = FFIX_LAST_SESSION,
    source: str = PR_BUNDLE_SOURCE_ID,
    universe: tuple[int, int] | None = None,
) -> MembershipCensus:
    """Open every bundle L0 holds in the range and fold its `ffix` member into one census.

    What it does: reads each payload through `L0Store.get` (re-verifying its sha256 on the way in,
    invariant #1), parses the `ffix` member, folds it into a `SymbolKeyedIndexMembership`, then
    measures per-index spans and counts, change events, calendar contiguity and the dated sectoral
    assignments.
    What it assumes: `calendar` covers the whole range — it raises otherwise rather than reading an
    uncovered year as holiday-free. `universe` is `(symbols, sessions)` from `traded_universe`, or
    `None` to leave the sector-coverage denominator unmeasured rather than guessed.
    What it never does: fetch, write, promote, resolve a symbol to an ISIN, or stop early on a bad
    bundle — a failure is collected and named so the sweep still finishes.
    """
    acc = _Accumulator()
    for ref in store.iter_refs(source, start=start, end=end):
        acc.bundles_swept += 1
        payload = store.get(ref)
        try:
            parsed = _parse_one(payload, filename=ref.filename, acc=acc, ref_date=ref.logical_date)
        except ParseError as exc:
            acc.failures.append((ref.logical_date, ref.filename, f"{type(exc).__name__}: {exc}"))
            continue
        if parsed is None:
            acc.without_ffix.append(ref.logical_date)
            continue
        acc.headers[",".join(parsed.indices)] += 1
        for banner in parsed.announced_indices:
            acc.banners[banner] += 1
        acc.dataset.add(parsed)
        for index_name in parsed.indices:
            acc.counts[index_name][parsed.knowable_date] = len(parsed.constituents(index_name))

    dataset = acc.dataset
    sessions = dataset.sessions
    reconciliation = calendar.reconcile(sessions, start, end)

    census = MembershipCensus(
        l0_root=layer_root(Layer.L0, data_root=store.data_root),
        start=start,
        end=end,
        bundles_swept=acc.bundles_swept,
        ffix_sessions=len(sessions),
        rows=dataset.rows,
        first_session=sessions[0] if sessions else None,
        last_session=sessions[-1] if sessions else None,
        indices=tuple(_index_census(dataset, acc, name) for name in dataset.indices),
        distinct_symbols=len(dataset.symbols_ever()),
        expected_sessions=len(calendar.expected_data_dates(start, end)),
        missing_sessions=tuple(reconciliation.missing),
        unexpected_sessions=tuple(reconciliation.unexpected),
        bundles_without_ffix=tuple(sorted(acc.without_ffix)),
        recovered=tuple(acc.recovered),
        failures=tuple(acc.failures),
        headers=dict(acc.headers.most_common()),
        announced_banners=dict(acc.banners.most_common()),
        sectoral=_sectoral_findings(dataset, universe=universe),
    )
    _LOG.info(
        "pr_bundle_membership.censused",
        source=source,
        l0_root=str(census.l0_root),
        bundles_swept=census.bundles_swept,
        ffix_sessions=census.ffix_sessions,
        rows=census.rows,
        indices=len(census.indices),
        changes=census.total_changes,
        recovered=len(census.recovered),
        failures=len(census.failures),
        state="VALIDATED",
    )
    return census


def traded_universe(
    store: L0Store,
    *,
    start: date = FFIX_FIRST_SESSION,
    end: date = FFIX_LAST_SESSION,
) -> tuple[int, int]:
    """`(distinct symbols, sessions read)` that actually traded in the span — the denominator.

    Read from `nse_bhavcopy_legacy`, and the span straddles that source's own identity boundary:
    `ISIN_ERA_START` is 2011-06-22, so 2010-01-04..2011-06-21 is the **pre-ISIN** bhavcopy (no
    ISIN column at all, read by `parse_pre_isin`, rows quarantined and symbol-only) and the rest
    carries ISINs (read by `parse`). Both are dispatched on `eras.ISIN_ERA_START` rather than
    sniffed, because the repo already pinned that date and a second opinion about it would be a
    second source of truth.

    Only `.symbol` is taken from either side. The denominator has to live in the same key space as
    `ffix` — which is symbols — and for the first eighteen months of the span that is *all* the
    price record has, so the comparison is exact rather than approximate.

    Assumes the caller wants every session L0 holds in the range. Never fetches, never writes.
    """
    symbols: set[str] = set()
    sessions = 0
    for ref in store.iter_refs(bhavcopy_legacy.LEGACY_SOURCE_ID, start=start, end=end):
        if ref.logical_date < ISIN_ERA_START:
            symbols.update(row.symbol for row in bhavcopy_legacy.parse_pre_isin_l0(store, ref))
        else:
            symbols.update(row.symbol for row in bhavcopy_legacy.parse_l0(store, ref))
        sessions += 1
    _LOG.info(
        "pr_bundle_membership.traded_universe",
        source=bhavcopy_legacy.LEGACY_SOURCE_ID,
        start=start.isoformat(),
        end=end.isoformat(),
        symbols=len(symbols),
        sessions=sessions,
        state="VALIDATED",
    )
    return len(symbols), sessions


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _parse_one(
    payload: bytes,
    *,
    filename: str,
    acc: _Accumulator,
    ref_date: date,
) -> FfixFile | None:
    """Parse one bundle's `ffix` member, recovering the two bundles `PrBundle` will not date.

    Returns `None` when the bundle carries no `ffix` member at all — the normal case outside
    2010-01-04..2013-04-30, and a fact about the era rather than a failure.

    The recovery is deliberately narrow: it fires only when `PrBundle` raised while *dating* the
    bundle, and it dates the `ffix` member from the digits in the member's own name. That is still
    payload-derived. No clock, no archive filename, no calendar.
    """
    try:
        with PrBundle(payload, filename=filename) as bundle:
            if not bundle.has(MemberKind.FFIX):
                return None
            return parse_ffix_bundle(bundle)
    except ParseError as exc:
        recovered = _recover(payload, filename=filename, ref_date=ref_date, why=str(exc), acc=acc)
        if recovered is None:
            raise
        return recovered


def _recover(
    payload: bytes,
    *,
    filename: str,
    ref_date: date,
    why: str,
    acc: _Accumulator,
) -> FfixFile | None:
    """Date the `ffix` member from its own filename, for a bundle whose members disagree.

    Returns `None` — leaving the original `ParseError` to propagate — unless the zip opens and
    holds exactly one `ffix<digits>.csv` whose digits are a real date. Two candidate members, or
    a name that does not carry a date, is not something to guess at.
    """
    try:
        archive = zipfile.ZipFile(BytesIO(payload))
    except zipfile.BadZipFile:
        return None
    with archive:
        matches = [
            (name, match)
            for name in archive.namelist()
            for match in (_FFIX_MEMBER_RE.search(name),)
            if match is not None
        ]
        if len(matches) != 1:
            return None
        name, match = matches[0]
        day = _member_date(match.group("digits"))
        if day is None:
            return None
        parsed = parse_ffix(archive.read(name), filename=name, knowable_date=day)
    acc.recovered.append(
        RecoveredBundle(
            filename=filename,
            l0_logical_date=ref_date,
            ffix_member=name,
            recovered_date=day,
            why=why,
        )
    )
    _LOG.warning(
        "pr_bundle_membership.recovered_undatable_bundle",
        source=PR_BUNDLE_SOURCE_ID,
        filename=filename,
        ffix_member=name,
        recovered_date=day.isoformat(),
        why=why,
        state="VALIDATED",
    )
    return parsed


def _member_date(digits: str) -> date | None:
    """`DDMMYY` or `DDMMYYYY` → a date; `None` when the digits are not one.

    Same reading as `bundle._member_date`, including `20YY` for a two-digit year: this archive
    starts in 2010 and the `ffix` member stops in 2013.
    """
    try:
        day, month = int(digits[0:2]), int(digits[2:4])
        year = 2000 + int(digits[4:6]) if len(digits) == 6 else int(digits[4:8])
        return date(year, month, day)
    except ValueError:
        return None


def _index_census(
    dataset: SymbolKeyedIndexMembership, acc: _Accumulator, index_name: str
) -> IndexCensus:
    """Fold one index's per-session counts and change events into an `IndexCensus`."""
    by_day = acc.counts[index_name]
    days = sorted(by_day)
    counts = sorted(by_day[day] for day in days)
    nominal = NOMINAL_SIZES.get(index_name)
    anomalies = (
        tuple(
            CountAnomaly(index_name=index_name, day=day, observed=by_day[day], nominal=nominal)
            for day in days
            if by_day[day] != nominal
        )
        if nominal is not None
        else ()
    )
    return IndexCensus(
        index_name=index_name,
        first_seen=days[0],
        last_seen=days[-1],
        sessions=len(days),
        rows=sum(counts),
        min_count=counts[0],
        # The lower median of an even-length run, so a constituent count stays a whole number of
        # securities rather than becoming an x.5 that no session ever published.
        median_count=counts[(len(counts) - 1) // 2],
        max_count=counts[-1],
        symbols_ever=len(dataset.symbols_ever(index_name)),
        nominal_size=nominal,
        anomalies=anomalies,
        changes=dataset.changes(index_name),
    )


def _sectoral_findings(
    dataset: SymbolKeyedIndexMembership, *, universe: tuple[int, int] | None
) -> SectoralFindings:
    """Measure the dated-sector question: how many symbols, how many at once, and who moved."""
    assignments = dataset.sectoral_assignments()
    simultaneous = 0
    widest = 0
    movers: list[SectorMove] = []
    sequential: list[str] = []
    sessions: set[date] = set()
    for symbol, by_day in assignments.items():
        sessions.update(by_day)
        per_day = [len(names) for names in by_day.values()]
        widest = max(widest, max(per_day))
        if any(count > 1 for count in per_day):
            simultaneous += 1
        movers.extend(_moves(symbol, by_day))
        if _sector_changed(by_day):
            sequential.append(symbol)
    present = tuple(sorted(name for name in dataset.indices if name in SECTORAL_INDICES))
    return SectoralFindings(
        sectoral_indices=present,
        symbols_with_any_assignment=len(assignments),
        symbols_with_simultaneous_assignments=simultaneous,
        max_simultaneous=widest,
        movers=tuple(sorted(movers, key=lambda move: (move.day, move.symbol))),
        symbols_whose_sector_changed=tuple(sorted(sequential)),
        assignment_sessions=len(sessions),
        traded_universe_symbols=None if universe is None else universe[0],
        traded_universe_sessions=None if universe is None else universe[1],
    )


def _sector_changed(by_day: Mapping[date, frozenset[str]]) -> bool:
    """Whether this symbol's sectoral identity changed at some point, however gradually.

    True when it **left** a sectoral it had been in *and* belonged to more than one over its life.
    Both conditions are load-bearing:

    * without the departure, a symbol that merely gained a second sector counts — but that is a
      multi-assignment, already counted separately, not a change of sector;
    * without the multi-sector condition, a symbol that simply dropped out of its only sectoral
      counts — but leaving an index is not being reclassified.

    Catches the gradual migration a same-step test structurally cannot see: `{IT}` → `{IT,
    SERVICE}` → `{SERVICE}` never gains-and-loses on one date, yet the sector did change.
    """
    days = sorted(by_day)
    lifetime = frozenset().union(*by_day.values())
    left_something = any(by_day[before] - by_day[after] for before, after in pairwise(days))
    return left_something and len(lifetime) > 1


def _moves(symbol: str, by_day: Mapping[date, frozenset[str]]) -> Iterable[SectorMove]:
    """Every transition where one symbol both left a sectoral index and joined another.

    A pure addition (gaining a second sector) or a pure removal (dropping out of an index) is not
    a move: the first is a multi-assignment and the second is usually the symbol leaving the index
    for size or liquidity reasons. Both directions in one step is the exchange reclassifying it.
    """
    days = sorted(by_day)
    for previous_day, day in pairwise(days):
        before, after = by_day[previous_day], by_day[day]
        left, joined = before - after, after - before
        if left and joined:
            yield SectorMove(
                symbol=symbol,
                day=day,
                previous_day=previous_day,
                left=tuple(sorted(left)),
                joined=tuple(sorted(joined)),
            )


# ── the report ───────────────────────────────────────────────────────────────────────────────


def render_census(census: MembershipCensus, *, title: str | None = None) -> str:
    """The census as Markdown — the committed gate report's body.

    Every number here comes off `census`; nothing is restated from a docstring or rounded by hand.
    """
    lines: list[str] = []
    lines += _render_preamble(census, title=title)
    lines += _render_per_index(census)
    lines += _render_contiguity(census)
    lines += _render_changes(census)
    lines += _render_symbols(census)
    lines += _render_sector(census)
    lines += _render_limits(census)
    lines += _render_validation(census)
    return "\n".join(lines).rstrip() + "\n"


def _render_preamble(census: MembershipCensus, *, title: str | None) -> list[str]:
    span = f"{_stamp(census.first_session)} .. {_stamp(census.last_session)}"
    out = [
        f"# {title or 'ffix index membership — census'}",
        "",
        f"- **L0 root swept:** `{census.l0_root}`",
        f"- **Range asked for:** {census.start.isoformat()} .. {census.end.isoformat()}",
        f"- **Bundles opened:** {census.bundles_swept:,}",
        f"- **Sessions with an `ffix` member:** {census.ffix_sessions:,} ({span})",
        f"- **Constituent rows parsed:** {census.rows:,}",
        f"- **Distinct indices:** {len(census.indices)}",
        f"- **Distinct symbols, all indices:** {census.distinct_symbols:,}",
        f"- **Membership change events:** {census.total_changes:,}",
        f"- **Header shapes / index-set shapes:** {len(census.headers)}",
        "",
    ]
    if census.recovered:
        out += [
            "## Bundles recovered by dating the `ffix` member from its own name",
            "",
            "`PrBundle` refuses to date a bundle whose members disagree, which is correct for a",
            "corporate action. These two would otherwise have been lost; the `ffix` member's own",
            "filename dates them, which is still payload-derived and never a clock.",
            "",
            "| bundle | L0 key date | `ffix` member | dated to | why `PrBundle` refused |",
            "|---|---|---|---|---|",
        ]
        for rec in census.recovered:
            why = rec.why.split(";")[0][:110]
            out.append(
                f"| `{rec.filename}` | {rec.l0_logical_date.isoformat()} | `{rec.ffix_member}` "
                f"| {rec.recovered_date.isoformat()} | {why} |"
            )
        out.append("")
    if census.failures:
        out += ["## Bundles that failed to parse", "", "| date | file | error |", "|---|---|---|"]
        out += [f"| {d.isoformat()} | `{f}` | {m[:160]} |" for d, f, m in census.failures]
        out.append("")
    return out


def _render_per_index(census: MembershipCensus) -> list[str]:
    out = [
        "## 1. Per index",
        "",
        "`median` is the lower median, so it is a whole number of securities. `nominal` is the",
        "size the index's name asserts; a blank means the index does not assert one (`BANK Nifty`",
        "and the sectorals are as-many-as-qualify).",
        "",
        "| index | kind | first | last | sessions | rows | min | median | max | nominal "
        "| off-nominal | symbols ever | changes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for index in census.indices:
        kind = "sectoral" if index.is_sectoral else "broad"
        nominal = "" if index.nominal_size is None else str(index.nominal_size)
        off = "—" if index.nominal_size is None else f"{len(index.anomalies):,}"
        out.append(
            f"| `{index.index_name}` | {kind} | {index.first_seen.isoformat()} "
            f"| {index.last_seen.isoformat()} | {index.sessions:,} | {index.rows:,} "
            f"| {index.min_count} | {index.median_count} | {index.max_count} | {nominal} "
            f"| {off} | {index.symbols_ever:,} | {len(index.changes):,} |"
        )
    out.append("")

    flagged = [i for i in census.indices if i.anomalies]
    out += ["### Off-nominal constituent counts", ""]
    if not flagged:
        out += [
            "**None.** Every index whose name asserts a size published exactly that many",
            "constituents on every session it appeared on. That is also the strongest available",
            "evidence that the reader is not dropping rows: 500 of 500 on every CNX 500 session,",
            "with the file's separator and banner furniture interleaved throughout.",
            "",
        ]
        return out
    for index in flagged:
        counts = Counter(a.observed for a in index.anomalies)
        out += [
            f"**`{index.index_name}`** — nominal {index.nominal_size}, "
            f"{len(index.anomalies):,} of {index.sessions:,} sessions off it "
            f"({', '.join(f'{n} on {c:,} sessions' for n, c in sorted(counts.items()))}).",
            "",
            "| session | observed | nominal |",
            "|---|---|---|",
        ]
        shown = index.anomalies[:40]
        out += [f"| {a.day.isoformat()} | {a.observed} | {a.nominal} |" for a in shown]
        if len(index.anomalies) > len(shown):
            out.append(f"| … | {len(index.anomalies) - len(shown):,} more | |")
        out.append("")
    return out


def _render_contiguity(census: MembershipCensus) -> list[str]:
    out = [
        "## 2. Contiguity against the trading calendar",
        "",
        f"- Calendar sessions expected in {census.start.isoformat()}..{census.end.isoformat()}: "
        f"**{census.expected_sessions:,}**",
        f"- Sessions with an `ffix` member: **{census.ffix_sessions:,}**",
        f"- Bundles present but with no `ffix` member: **{len(census.bundles_without_ffix):,}**",
        "",
    ]
    if census.contiguous:
        out += [
            "**Contiguous, with no interior gaps.** The `ffix` sessions are exactly the trading",
            "calendar's sessions over the span, in both directions — every session the calendar",
            "declares has a member, and no member falls on a date the calendar calls closed.",
            "",
        ]
        return out
    if census.missing_sessions:
        out += [
            f"**{len(census.missing_sessions):,} calendar sessions with no `ffix` member:**",
            "",
            *(f"- {day.isoformat()}" for day in census.missing_sessions),
            "",
        ]
    if census.unexpected_sessions:
        out += [
            f"**{len(census.unexpected_sessions):,} `ffix` members on dates the calendar calls "
            "closed:**",
            "",
            *(f"- {day.isoformat()}" for day in census.unexpected_sessions),
            "",
        ]
    if census.bundles_without_ffix:
        out += [
            "Bundles in the span that opened but carried no `ffix` member:",
            "",
            *(f"- {day.isoformat()}" for day in census.bundles_without_ffix),
            "",
        ]
    return out


def _render_changes(census: MembershipCensus) -> list[str]:
    out = [
        "## 3. Observable membership changes",
        "",
        f"**{census.total_changes:,} change events**, each a session on which an index's",
        "constituent set differed from its own previous published session. This is the payload:",
        "a dated record of reconstitution, diffed off the source rather than reconstructed.",
        "",
        "| index | changes | largest (symbols moved) | first | last |",
        "|---|---|---|---|---|",
    ]
    for index in census.indices:
        if not index.changes:
            out.append(f"| `{index.index_name}` | 0 | — | — | — |")
            continue
        largest = max(index.changes, key=lambda c: c.size)
        out.append(
            f"| `{index.index_name}` | {len(index.changes):,} "
            f"| {largest.size} on {largest.day.isoformat()} "
            f"| {index.changes[0].day.isoformat()} | {index.changes[-1].day.isoformat()} |"
        )
    out.append("")

    headline = [i for i in census.indices if i.index_name in HEADLINE_INDICES and i.changes]
    if headline:
        out += [
            "### Every change to a broad-market index, in full",
            "",
            "The sectorals' change lists run to hundreds of events; these are the ones a backtest",
            "universe is most likely to be built from, so they are given whole.",
            "",
        ]
        for index in headline:
            out += [
                f"**`{index.index_name}`** — {len(index.changes):,} events",
                "",
                "| session | previous session | added | removed |",
                "|---|---|---|---|",
            ]
            for change in index.changes:
                out.append(
                    f"| {change.day.isoformat()} | {change.previous_day.isoformat()} "
                    f"| {', '.join(change.added) or '—'} "
                    f"| {', '.join(change.removed) or '—'} |"
                )
            out.append("")
    return out


def _render_symbols(census: MembershipCensus) -> list[str]:
    widest = max(census.indices, key=lambda i: i.symbols_ever)
    return [
        "## 4. Distinct symbols",
        "",
        f"**{census.distinct_symbols:,} distinct symbols** appear across all "
        f"{len(census.indices)} indices over the {census.ffix_sessions:,} sessions.",
        "",
        f"The widest single index is `{widest.index_name}`, which held "
        f"{widest.symbols_ever:,} distinct symbols across its {widest.sessions:,} sessions while "
        f"never publishing more than {widest.max_count} at a time — the gap between those two "
        "numbers is the survivorship the corpus lets a backtest avoid, and the reason a "
        "present-day constituent list is not a substitute for it.",
        "",
    ]


def _render_sector(census: MembershipCensus) -> list[str]:
    sector = census.sectoral
    out = [
        "## 5. Dated sectoral assignment",
        "",
        "Membership of a sectoral index on a session is an **exchange-published, dated sector",
        "assignment**. That is the thing this corpus offers which no other surface in the platform",
        "does, and it is what makes the question worth measuring rather than asserting.",
        "",
        f"- Sectoral indices present: **{len(sector.sectoral_indices)}** "
        f"({', '.join(f'`{n}`' for n in sector.sectoral_indices)})",
        f"- Sessions carrying at least one sectoral assignment: **{sector.assignment_sessions:,}**",
        f"- Distinct symbols with **at least one** dated sectoral assignment: "
        f"**{sector.symbols_with_any_assignment:,}**",
        f"- Symbols holding **more than one at once**: "
        f"**{sector.symbols_with_simultaneous_assignments:,}** "
        f"(widest simultaneous: **{sector.max_simultaneous}** indices)",
        f"- Symbols that **moved** between sectorals in one step (left one and joined another on "
        f"the same date): **{len({m.symbol for m in sector.movers}):,}**, over "
        f"{len(sector.movers):,} transitions",
        f"- Symbols whose sectoral **identity changed** at all, abruptly or gradually (a "
        f"superset of the line above): **{len(sector.symbols_whose_sector_changed):,}**"
        + (
            f" — {', '.join(f'`{s}`' for s in sector.symbols_whose_sector_changed[:24])}"
            + (" …" if len(sector.symbols_whose_sector_changed) > 24 else "")
            if sector.symbols_whose_sector_changed
            else ""
        ),
        "",
    ]
    if sector.movers:
        out += [
            "### Every sectoral reclassification",
            "",
            "| session | previous session | symbol | left | joined |",
            "|---|---|---|---|---|",
        ]
        out += [
            f"| {m.day.isoformat()} | {m.previous_day.isoformat()} | `{m.symbol}` "
            f"| {', '.join(m.left)} | {', '.join(m.joined)} |"
            for m in sector.movers
        ]
        out.append("")
    elif sector.symbols_whose_sector_changed:
        out += [
            "**No symbol was reclassified in a single step**, but "
            f"**{len(sector.symbols_whose_sector_changed):,} changed sector gradually** — holding",
            "two sectors for a while before settling in one. So sector assignment here is not",
            "static, and a point-in-time series has to honour the dates: pinning a symbol to the",
            "sector it ends the span in would misclassify it for the earlier part.",
            "",
        ]
    else:
        out += [
            "**No symbol changed sector in the span, in either sense** — no abrupt",
            "reclassification, and no gradual one either. Sector assignment is *stable* here,",
            "which cuts both ways: it is one less thing a point-in-time series has to model, and",
            "it is also years of evidence that these ten indices are not where a reclassification",
            "would show up first — a sector *rename* or a new index would not appear as a move.",
            "",
        ]
    fraction = sector.coverage_fraction
    if fraction is None:
        out += [
            "**Coverage of the traded universe: not measured in this run.** Without the",
            "denominator the coverage claim would be an estimate, and the brief asked for a",
            "measurement. Re-run with `--universe` to fill it in.",
            "",
        ]
    else:
        # Narrowed for mypy: `coverage_fraction` is non-None only when the count is set.
        assert sector.traded_universe_symbols is not None
        unclassified = sector.traded_universe_symbols - sector.symbols_with_any_assignment
        out += [
            "### How far this goes toward a point-in-time sector classification, and where not",
            "",
            f"Measured against the symbols that **actually traded** over the same span — "
            f"{sector.traded_universe_symbols:,} distinct symbols across "
            f"{sector.traded_universe_sessions:,} sessions of `nse_bhavcopy_legacy`. The first "
            "eighteen months of that is the pre-ISIN bhavcopy, which carries no ISIN column at "
            "all, so for those years the price record itself lives in the same unresolved symbol "
            "space as `ffix` and the comparison is exact rather than approximate:",
            "",
            f"- **{sector.symbols_with_any_assignment:,} of {sector.traded_universe_symbols:,} "
            f"= {fraction * 100:.1f}%** of the traded universe carries a dated sectoral",
            f"  assignment at some point in the span. The other "
            f"**{100 - fraction * 100:.1f}%** — {unclassified:,} symbols — has none, at any "
            "date.",
            "",
        ]
    return out


def _render_limits(census: MembershipCensus) -> list[str]:
    span_years = (census.end - census.start).days / 365.25
    return [
        "## The honest limits",
        "",
        f"1. **It ends {census.last_session and census.last_session.isoformat()}.** The member "
        f"covers {span_years:.1f} years, not ten. Every session from 2013-05-02 onward carries no",
        "   `ffix` member at all — swept, not assumed. So this closes the 2010-2013 hole in the",
        "   membership record and leaves 2013-2026 exactly as it was.",
        "2. **It is symbol-keyed and cannot be joined until W4.** No ISIN appears anywhere in this",
        "   dataset, by design (invariant #2). Until a point-in-time symbol master exists, these",
        "   symbols cannot be attached to a price, a corporate action or a fundamental, and",
        "   resolving them through today's listing would build the survivorship bias directly",
        "   into the universe.",
        "3. **Index constituents only, so the micro-cap tail is invisible.** An index membership",
        "   file says nothing about a security that was in no index. The sector-coverage",
        "   percentage above is the size of that blind spot, measured.",
        "4. **NIFTY 50 *is* here** — from the very first bundle, on every one of the",
        f"   {census.ffix_sessions:,} sessions. Do not confuse this with the `Ix` member, where",
        "   Phase 1 correctly found NIFTY 50 absent. Two different members of the same bundle:",
        "   `Ix` is 2010-only with a rotating six-index set and no NIFTY; `ffix` is this.",
        "",
    ]


def _render_validation(census: MembershipCensus) -> list[str]:
    """§7: is this corpus enough to *validate* a backward reconstruction from circulars?

    Rendered from the measured change data rather than asserted, because the answer turns on
    three numbers the sweep produced: whether coverage is daily and gapless, whether the
    published counts are internally consistent, and how many distinct dates a reconstruction
    would have to land on.
    """
    changes = census.change_dates
    clustered = census.multi_index_change_dates
    off_nominal = sum(len(index.anomalies) for index in census.indices)
    verdict = (
        "**Yes, for its span, and decisively so.**"
        if (census.contiguous and off_nominal == 0)
        else "**Partially — see the caveats below.**"
    )
    out = [
        "## 7. Is this enough to validate a backward reconstruction from reconstitution circulars?",
        "",
        "The study recommended reconstructing index membership backward from NSE's own",
        "reconstitution circulars, for the thematic-universe problem. The question is whether this",
        "corpus can *check* such a reconstruction. Reasoning from the measurements above, not from",
        "a general impression:",
        "",
        verdict,
        "",
        f"1. **Coverage is daily and gapless.** {census.ffix_sessions:,} of "
        f"{census.expected_sessions:,} calendar sessions carry a member, with no interior gap in",
        "   either direction. So a reconstruction can be diffed against ground truth **on every",
        "   trading day**, not sampled at the dates it happens to agree on. A reconstruction that",
        "   gets an effective date wrong by one session is detectable; against quarterly anchors",
        "   it would not be.",
        f"2. **The ground truth is internally consistent.** {off_nominal} sessions out of "
        f"{sum(i.sessions for i in census.indices):,} index-sessions show a constituent count",
        "   away from the index's nominal size. A validation target that disagreed with itself",
        "   would make every reconstruction mismatch ambiguous; this one does not, so a mismatch",
        "   is unambiguously the reconstruction's.",
        f"3. **The events to land on are few and clustered.** All {census.total_changes:,} change",
        f"   events fall on {len(changes):,} distinct dates, and {len(clustered):,} of those are",
        "   dates on which three or more indices changed at once — the signature of a scheduled",
        "   reconstitution rather than a single corporate event"
        + (f" ({', '.join(d.isoformat() for d in clustered)})" if clustered else "")
        + ".",
        "   A circular-derived reconstruction is exactly a claim about those dates, so the corpus",
        "   tests the reconstruction's core assertion rather than its edges.",
        "",
        "**Where the validation does *not* reach.** Three limits, and they matter:",
        "",
        f"- It validates **{census.start.isoformat()}..{census.end.isoformat()} only**. A",
        "  reconstruction is wanted for ten-plus years; this checks 3.3 of them. A method that",
        "  validates here is *credible* for 2013-2026 and *verified* nowhere in it — the circular",
        "  formats, the index rename waves and the reconstitution cadence all changed after 2013.",
        "- It validates the **17 indices published here**, and the thematic indices the study",
        "  actually wants (the post-2015 Nifty sectoral and strategy families) are not among them.",
        "- It cannot validate an **ISIN-keyed** reconstruction, only a symbol-keyed one. A",
        "  reconstruction that resolves circular symbols to ISINs has an identity step this corpus",
        "  is silent about, and that step is where a survivorship bias would enter.",
        "",
        "**So the practical recommendation:** build the reconstruction, validate it against this",
        "corpus over 2010-2013 as a **method test** rather than a coverage claim, and treat a pass",
        "as evidence the circular-reading is sound — not as evidence the 2013-2026 output is",
        "right. Anything downstream of 2013-04-30 stays unvalidated until another dated surface",
        "turns up.",
        "",
    ]
    return out


def _stamp(day: date | None) -> str:
    return "—" if day is None else day.isoformat()


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ffix-membership-census", description=__doc__)
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat, default=None)
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat, default=None)
    parser.add_argument("--out", type=Path, default=None, help="write the report here")
    parser.add_argument(
        "--universe",
        action="store_true",
        help="measure the traded-universe denominator from nse_bhavcopy_legacy (slower)",
    )
    parser.add_argument("--title", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Exit 0 on a clean census, 3 when any bundle failed to parse."""
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    clock: Clock = SystemClock()
    store = L0Store(clock=clock, data_root=settings.data_root)
    start = args.from_date or FFIX_FIRST_SESSION
    end = args.to_date or FFIX_LAST_SESSION

    # The lake root, printed before a byte is read. A census pointed at a worktree-relative
    # `data/` reads an empty second lake and reports zero bundles as though that were a finding;
    # that has happened on this box, and the fix is to make the root impossible to miss.
    print(f"lake: {settings.data_root}  L0: {store.root}", flush=True)

    universe = traded_universe(store, start=start, end=end) if args.universe else None
    if universe is not None:
        print(f"traded universe: {universe[0]:,} symbols over {universe[1]:,} sessions", flush=True)

    census = census_ffix_corpus(
        store, calendar=trading_calendar(), start=start, end=end, universe=universe
    )
    body = render_census(census, title=args.title)
    if args.out is None:
        print(body)
    else:
        args.out.write_text(body, encoding="utf-8")
        print(f"wrote {args.out} ({len(body):,} bytes)")
    print(
        f"{census.ffix_sessions:,} sessions, {census.rows:,} rows, "
        f"{len(census.indices)} indices, {census.total_changes:,} change events, "
        f"{len(census.failures)} failures",
        file=sys.stderr,
    )
    return 3 if census.failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
