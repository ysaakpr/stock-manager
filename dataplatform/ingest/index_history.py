"""Point-in-time index membership history, reconstructed backward from today's list (DQ-5).

The constituents snapshots (`indices.membership_asof`) only start on 2026-09-08 — after the last L1
price date — so before this module an index-scoped backtest saw an empty universe or today's list.
Here the history is *derived*, from two primary inputs and nothing else:

* an **anchor** — the exchange's constituents CSV for each tracked index on the build date
  (`nifty_index_constituents`, placeholders rejected at parse); and
* every **change announcement** since (`index_changes`, `nifty_index_press_releases`): which
  company entered or left which index, effective when, announced when.

**The walk.** Start from the anchor set on the anchor date and step back through the change dates.
Crossing an inclusion effective on `E` backward removes the company (it was not a member before
`E`); crossing an exclusion adds it back. Crossing an ISIN reissue (a split that retired the ISIN,
`identity.lineage`) renames the member to its predecessor, so every interval carries the ISIN the
exchange printed on those sessions. Changes announced but effective after the anchor are applied
forward. Each member's stay becomes one interval row:

    (index_slug, isin, effective_from, effective_to, knowable_from, exit_knowable)

**The PIT rule** (§8.3.6, invariant #7): a decision on `D` sees an ISIN as a member iff
`effective_from <= D < effective_to` (open-ended when `effective_to` is null) **and**
`knowable_from <= D`. `knowable_from` is the announcement date of the inclusion — the day the
market could know — and `exit_knowable` the announcement of the exclusion, kept for audit.
`members_asof` applies the rule; nothing returns an interval to a caller that could forget it.

**Depth is earned, not assumed.** The history is valid only from `coverage_start`, the latest of:
the effective date of any candidate release not in L0 (budget or failure), of any tracked section a
release could not be read for, and of any event whose symbol did not resolve to an ISIN at its date.
Before `coverage_start` the history answers `None` — a gap, never an extrapolation of the oldest
reconstructed set. Every step that could not be reconciled is a `Residual` on the report: a count
that is not the index's fixed size, an inclusion of a company the later set does not hold, an
exclusion of one it still does, a member whose first NSE session falls inside its interval.

**Identity** (invariant #2). The releases print a symbol and a name, never an ISIN. A symbol is
resolved at the event's effective date against symbol windows built from the exchange's own
session files — the NSE bhavcopy in L1 prints `(symbol, ISIN)` for every session — and, for a
company that listed after the last L1 session, against the constituents snapshots and anchors
(the exchange's list, which also prints both). That is symbol→ISIN *identity* evidence at a date,
the same thing `IdentityMaster` resolves; no price is read through a symbol. A symbol that does not
resolve, or resolves to two ISINs, is quarantined with the release that named it, and it cuts that
index's `coverage_start` — the walk never crosses an event it could not place.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from dataplatform.identity.lineage import derive_edges, read_equity_spans
from dataplatform.ingest.index_changes import (
    PRESS_RELEASE_SOURCE_ID,
    TRACKED_INDICES,
    ChangeAction,
    IndexChangeEvent,
    PressRelease,
    PressReleaseParse,
    candidate_releases,
    parse_press_release_l0,
    parse_press_release_listing,
)
from dataplatform.ingest.index_transcription import (
    ReleaseTranscription,
    load_release_transcriptions,
    transcription_parse,
)
from dataplatform.ingest.indices import (
    CONSTITUENTS_DATASET,
    CONSTITUENTS_SOURCE_ID,
    ConstituentSnapshot,
    l0_constituents_filename,
    parse_constituents_l0,
)
from dataplatform.ingest.models import IngestError, ParseError
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Store
from dataplatform.store.l2 import open_connection, register_raw_view
from dataplatform.store.paths import Layer, l1_partition_path, layer_root, partition_date_of

__all__ = [
    "EVENTS_DATASET",
    "HISTORY_DATASET",
    "UNDATED_RELEASE_HORIZON",
    "HistoryBuild",
    "ImmutableHistoryError",
    "IndexHistory",
    "MembershipInterval",
    "Residual",
    "ResidualKind",
    "ResolvedEvent",
    "SymbolEvidence",
    "build_membership_history",
    "load_symbol_evidence",
    "members_asof",
    "read_membership_history",
    "reconstruct_index",
    "render_history_report",
    "write_membership_history",
]

_LOG = get_logger(__name__)

#: L1 datasets this module owns. Both are new with DQ-5; nothing else writes them.
HISTORY_DATASET: Final = "index_membership_history"
EVENTS_DATASET: Final = "index_change_events"

#: How far past its announcement a release with no readable effective date is assumed to reach.
#: Index changes take effect within weeks of announcement (semi-annual reviews: ~5-6 weeks); a
#: release we could not read is assumed to have changed anything up to 60 days after it, so the
#: coverage start is pushed past it rather than reconstructed across it.
UNDATED_RELEASE_HORIZON: Final = timedelta(days=60)

#: Two sessions either side of an effective date a symbol window may miss by: an excluded company is
#: often suspended for a few sessions before its exclusion takes effect, an included one may list
#: on the effective date itself.
_RESOLVE_SLACK: Final = timedelta(days=15)

#: A reissue edge is trusted for renaming only when the handover is (near-)seamless; a long gap is
#: the signal of an issuer-code reuse, not a split (`identity.lineage.LineageEdge`).
_MAX_REISSUE_GAP_SESSIONS: Final = 5


class ImmutableHistoryError(IngestError):
    """A stored build's history would be overwritten with different intervals. Refused."""


class ResidualKind(StrEnum):
    """What did not reconcile, at one date, in one index — each one a fact for the report."""

    COUNT = "count"  # reconstructed members on a date differ from the index's fixed size
    INCLUDE_NOT_HELD_LATER = "include_not_held_later"  # included, yet absent from the later set
    EXCLUDE_STILL_HELD_LATER = "exclude_still_held_later"  # excluded, yet present in the later set
    ENTRY_BEFORE_FIRST_SESSION = "entry_before_first_session"  # member before it ever traded
    UNRESOLVED_SYMBOL = "unresolved_symbol"  # an event whose symbol named no ISIN at its date
    UNREADABLE_SECTION = "unreadable_section"  # a tracked section a release could not be read for
    DERIVED_EVENTS = "derived_events"  # events a transcription derived rather than read (flagged)
    MISSING_RELEASE = "missing_release"  # a candidate release that is not in L0
    SNAPSHOT_MISMATCH = "snapshot_mismatch"  # reconstruction ≠ a stored daily snapshot


@dataclass(frozen=True, slots=True)
class Residual:
    """One reconciliation failure, with enough to find it again."""

    index_slug: str
    kind: ResidualKind
    on_date: date
    detail: str
    isin: str | None = None
    release: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedEvent:
    """An announcement event with the ISIN its symbol resolved to at the effective date."""

    event: IndexChangeEvent
    isin: str | None
    reason: str | None = None  # why it did not resolve, when `isin` is None


@dataclass(frozen=True, slots=True)
class MembershipInterval:
    """One ISIN's continuous stay in one index — the L1 row, and the PIT atom.

    `effective_to` is exclusive and `None` while the stay is open. `knowable_from` is the date the
    inclusion was announced (or `coverage_start` for a member already in the index there, or the
    reissue date for a renamed ISIN); `exit_knowable` the announcement of the exclusion.
    """

    index_slug: str
    isin: str
    effective_from: date
    effective_to: date | None
    knowable_from: date
    exit_knowable: date | None
    entry_basis: str
    exit_basis: str | None

    def member_on(self, on_date: date) -> bool:
        """The PIT rule: effective on `on_date` and knowable by it."""
        if on_date < self.effective_from or self.knowable_from > on_date:
            return False
        return self.effective_to is None or on_date < self.effective_to


@dataclass(frozen=True, slots=True)
class IndexHistory:
    """One index's reconstructed history and the evidence of how well it reconciles."""

    index_slug: str
    anchor_date: date
    anchor_l0_key: str
    coverage_start: date
    expected_size: int
    intervals: tuple[MembershipInterval, ...]
    residuals: tuple[Residual, ...]
    segments: tuple[tuple[date, int], ...]  # (segment start, reconstructed member count)
    events_applied: int

    def members_on(self, on_date: date) -> frozenset[str] | None:
        """The PIT membership on `on_date`, or `None` before `coverage_start`."""
        if on_date < self.coverage_start:
            return None
        return frozenset(i.isin for i in self.intervals if i.member_on(on_date))


@dataclass(frozen=True, slots=True)
class HistoryBuild:
    """Everything one build produced — per-index histories, the resolved events, the inputs."""

    as_of: date
    listing_l0_key: str
    releases_considered: int
    releases_in_l0: int
    releases_missing: tuple[str, ...]
    histories: Mapping[str, IndexHistory]
    events: tuple[ResolvedEvent, ...]
    unparsed: tuple[tuple[str, str], ...] = field(default=())
    transcribed: tuple[str, ...] = field(default=())  # releases read from a curated transcription


# ── identity evidence: symbol windows from the exchange's own files ────────────────────────────


@dataclass(frozen=True, slots=True)
class _Window:
    isin: str
    first: date
    last: date


class SymbolEvidence:
    """Symbol→ISIN windows at a date, from the NSE bhavcopy in L1 and the constituents lists.

    What it does: answers "which ISIN traded as this symbol around this date" from the exchange's
    own session files, which print both on every row — the identity fact, at the date it held.
    What it assumes: the windows it was handed span the dates it is asked about; a symbol outside
    them is `None`, never the nearest guess beyond `_RESOLVE_SLACK`.
    What it never does: return a price, or pick between two ISINs that both held the symbol on the
    date — that is an ambiguity, reported as such.
    """

    def __init__(
        self,
        bhavcopy: Iterable[tuple[str, str, date, date]],
        lists: Iterable[tuple[str, str, date]] = (),
        first_sessions: Mapping[str, date] | None = None,
    ) -> None:
        by_symbol: dict[str, list[_Window]] = defaultdict(list)
        for symbol, isin, first, last in bhavcopy:
            by_symbol[_norm(symbol)].append(_Window(isin, first, last))
        listed: dict[str, list[_Window]] = defaultdict(list)
        for symbol, isin, on_date in lists:
            listed[_norm(symbol)].append(_Window(isin, on_date, on_date))
        self._bhav = dict(by_symbol)
        self._lists = dict(listed)
        self._first = dict(first_sessions or {})
        for windows in listed.values():
            for w in windows:
                if w.isin not in self._first or w.first < self._first[w.isin]:
                    self._first[w.isin] = w.first

    def first_seen(self, isin: str) -> date | None:
        """The first date the exchange's files show the ISIN — its first NSE session in L1, or the
        first constituents list naming it — or `None` if no file ever has."""
        return self._first.get(isin)

    def resolve(self, symbol: str, on_date: date) -> tuple[str | None, str | None]:
        """`(isin, None)` when one ISIN held `symbol` around `on_date`, else `(None, why)`."""
        key = _norm(symbol)
        for source, windows in (
            ("bhavcopy", self._bhav.get(key, [])),
            ("lists", self._lists.get(key, [])),
        ):
            covering = {w.isin for w in windows if w.first <= on_date <= w.last}
            if not covering:
                near = [
                    (min(abs((w.first - on_date).days), abs((w.last - on_date).days)), w.isin)
                    for w in windows
                    if w.first - _RESOLVE_SLACK <= on_date <= w.last + _RESOLVE_SLACK
                ]
                if near:
                    best = min(d for d, _ in near)
                    covering = {isin for d, isin in near if d == best}
            if len(covering) == 1:
                return next(iter(covering)), None
            if len(covering) > 1:
                equity = {isin for isin in covering if isin.startswith("INE") and isin[7:9] == "01"}
                if len(equity) == 1:
                    return next(iter(equity)), None
                return None, f"{symbol} named {sorted(covering)} on {on_date} in the {source}"
        return None, f"no ISIN traded as {symbol} within {_RESOLVE_SLACK.days} days of {on_date}"


def _norm(symbol: str) -> str:
    return symbol.strip().upper()


def load_symbol_evidence(
    *, data_root: Path | None = None, anchors: Iterable[ConstituentSnapshot] = ()
) -> SymbolEvidence:
    """Build `SymbolEvidence` from L1 `prices_raw` (NSE rows) and every stored constituents list."""
    con = open_connection()
    try:
        register_raw_view(con, view="prices_raw", data_root=data_root)
        bhav = con.execute(
            "SELECT symbol, isin, min(trade_date), max(trade_date) FROM prices_raw "
            "WHERE exchange = 'NSE' AND isin IS NOT NULL AND symbol IS NOT NULL "
            "GROUP BY symbol, isin"
        ).fetchall()
        firsts = con.execute(
            "SELECT isin, min(trade_date) FROM prices_raw WHERE exchange = 'NSE' GROUP BY isin"
        ).fetchall()
    finally:
        con.close()
    lists: list[tuple[str, str, date]] = []
    for snapshot in (*_stored_snapshots(data_root), *anchors):
        lists.extend((row.symbol, row.isin, snapshot.as_of) for row in snapshot.rows)
    _LOG.info(
        "index_history.symbol_evidence_loaded",
        bhavcopy_windows=len(bhav),
        list_rows=len(lists),
        isins_with_sessions=len(firsts),
    )
    return SymbolEvidence(
        ((str(s), str(i), f, last) for s, i, f, last in bhav),
        lists,
        {str(i): f for i, f in firsts},
    )


def _stored_snapshots(data_root: Path | None) -> Iterable[ConstituentSnapshot]:
    from dataplatform.ingest.indices import read_constituents_l1

    root = layer_root(Layer.L1, data_root=data_root) / CONSTITUENTS_DATASET
    if not root.is_dir():
        return
    for partition in sorted(root.iterdir()):
        try:
            as_of = partition_date_of(partition)
        except ValueError:
            continue
        for path in sorted(partition.glob("*.parquet")):
            yield read_constituents_l1(path.stem, as_of, data_root=data_root)


def _stored_snapshot_sets(slug: str, data_root: Path | None) -> dict[date, frozenset[str]]:
    return {s.as_of: s.members for s in _stored_snapshots(data_root) if s.index_slug == slug}


# ── the walk ───────────────────────────────────────────────────────────────────────────────────


def reconstruct_index(
    slug: str,
    *,
    anchor: ConstituentSnapshot,
    anchor_l0_key: str,
    events: Sequence[ResolvedEvent],
    coverage_start: date,
    reissues: Sequence[tuple[str, str, date]] = (),
    evidence: SymbolEvidence | None = None,
    snapshots: Mapping[date, frozenset[str]] | None = None,
    expected_size: int | None = None,
) -> IndexHistory:
    """Walk one index backward from its anchor to `coverage_start` — a pure function, no I/O.

    `events` must already be resolved and limited to this index and to effective dates on or after
    `coverage_start`; an unresolved event in that range is the caller's bug (it should have cut the
    coverage). `reissues` are `(predecessor, successor, successor's first session)` edges.
    Returns the intervals and every residual found; never raises for a reconciliation failure.
    """
    expected = TRACKED_INDICES.get(slug) if expected_size is None else expected_size
    if expected is None:
        raise IngestError(f"{slug} is not a tracked index; no expected size to reconcile against")
    anchor_date = anchor.as_of
    residuals: list[Residual] = []
    intervals: list[MembershipInterval] = []

    usable = [r for r in events if r.isin is not None and r.event.effective >= coverage_start]
    past = sorted((r for r in usable if r.event.effective <= anchor_date), key=_event_order)
    future = sorted((r for r in usable if r.event.effective > anchor_date), key=_event_order)

    # open[isin] = (effective_to, exit_knowable, exit_basis) of the stay that contains the cursor
    open_: dict[str, tuple[date | None, date | None, str | None]] = dict.fromkeys(
        anchor.members, (None, None, None)
    )

    # Forward: changes announced by the anchor but effective after it.
    for resolved in future:
        isin, ev = resolved.isin, resolved.event
        assert isin is not None
        if ev.action is ChangeAction.EXCLUDE:
            if isin in open_ and open_[isin][0] is None:
                open_[isin] = (ev.effective, ev.announced, ev.release)
            else:
                residuals.append(
                    Residual(
                        slug,
                        ResidualKind.EXCLUDE_STILL_HELD_LATER,
                        ev.effective,
                        f"future exclusion of {ev.symbol} not in the anchor",
                        isin,
                        ev.release,
                    )
                )
        else:
            intervals.append(
                MembershipInterval(
                    slug, isin, ev.effective, None, ev.announced, None, ev.release, None
                )
            )

    by_date: dict[date, list[ResolvedEvent]] = defaultdict(list)
    for resolved in past:
        by_date[resolved.event.effective].append(resolved)
    renames: dict[date, list[tuple[str, str]]] = defaultdict(list)
    for predecessor, successor, effective in reissues:
        if coverage_start < effective <= anchor_date:
            renames[effective].append((predecessor, successor))

    applied_renames: set[date] = set()
    for change_date in sorted(set(by_date) | set(renames), reverse=True):
        for resolved in by_date.get(change_date, []):
            isin, ev = resolved.isin, resolved.event
            assert isin is not None
            if ev.action is ChangeAction.INCLUDE:
                if isin in open_:
                    to, exit_known, exit_basis = open_.pop(isin)
                    intervals.append(
                        MembershipInterval(
                            slug,
                            isin,
                            change_date,
                            to,
                            ev.announced,
                            exit_known,
                            ev.release,
                            exit_basis,
                        )
                    )
                else:
                    residuals.append(
                        Residual(
                            slug,
                            ResidualKind.INCLUDE_NOT_HELD_LATER,
                            change_date,
                            f"{ev.symbol} included but not in the later set",
                            isin,
                            ev.release,
                        )
                    )
            else:
                if isin in open_:
                    residuals.append(
                        Residual(
                            slug,
                            ResidualKind.EXCLUDE_STILL_HELD_LATER,
                            change_date,
                            f"{ev.symbol} excluded but still in the later set",
                            isin,
                            ev.release,
                        )
                    )
                else:
                    open_[isin] = (change_date, ev.announced, ev.release)
        for predecessor, successor in renames.get(change_date, []):
            if successor in open_ and predecessor not in open_:
                to, exit_known, exit_basis = open_.pop(successor)
                intervals.append(
                    MembershipInterval(
                        slug,
                        successor,
                        change_date,
                        to,
                        change_date,
                        exit_known,
                        "reissue",
                        exit_basis,
                    )
                )
                open_[predecessor] = (change_date, change_date, "reissue")
                applied_renames.add(change_date)

    for isin, (to, exit_known, exit_basis) in open_.items():
        intervals.append(
            MembershipInterval(
                slug,
                isin,
                coverage_start,
                to,
                coverage_start,
                exit_known,
                "coverage_start",
                exit_basis,
            )
        )

    if evidence is not None:
        # A company cannot have been a member before the exchange's files first show it. This is
        # how a demerger spin-off is placed: it enters its parent's indices on the ex-date by a
        # "corporate adjustment" release that names the indices, not the company's symbol, so the
        # walk carries it back past its own existence. It is clipped to its first appearance, and
        # the clip is reported — the ex-date-to-listing stand-in window is not reconstructed.
        clipped: list[MembershipInterval] = []
        for interval in intervals:
            first = evidence.first_seen(interval.isin) or anchor_date
            if interval.effective_from >= first:
                clipped.append(interval)
                continue
            residuals.append(
                Residual(
                    slug,
                    ResidualKind.ENTRY_BEFORE_FIRST_SESSION,
                    interval.effective_from,
                    f"walked back to {interval.effective_from} but first seen {first}; "
                    "clipped to its first appearance",
                    interval.isin,
                    interval.entry_basis,
                )
            )
            if interval.effective_to is not None and interval.effective_to <= first:
                continue  # wholly before it existed
            clipped.append(
                MembershipInterval(
                    slug,
                    interval.isin,
                    first,
                    interval.effective_to,
                    max(interval.knowable_from, first),
                    interval.exit_knowable,
                    f"first_seen({interval.entry_basis})",
                    interval.exit_basis,
                )
            )
        intervals = clipped

    ordered = tuple(sorted(intervals, key=lambda i: (i.effective_from, i.isin)))
    history = IndexHistory(
        index_slug=slug,
        anchor_date=anchor_date,
        anchor_l0_key=anchor_l0_key,
        coverage_start=coverage_start,
        expected_size=expected,
        intervals=ordered,
        residuals=(),
        segments=(),
        events_applied=len(past) + len(future),
    )
    segment_starts = sorted(
        {
            coverage_start,
            *(i.effective_from for i in intervals if i.entry_basis.startswith("first_seen")),
            *by_date,
            *applied_renames,
            *(r.event.effective for r in future),
            anchor_date,
        }
    )
    segments: list[tuple[date, int]] = []
    for start in segment_starts:
        # Count on effective membership alone: the size check is about who *was* in the index.
        count = sum(
            1
            for i in ordered
            if i.effective_from <= start and (i.effective_to is None or start < i.effective_to)
        )
        segments.append((start, count))
        if count != expected:
            residuals.append(
                Residual(slug, ResidualKind.COUNT, start, f"{count} members, expected {expected}")
            )
    for snap_date, members in sorted((snapshots or {}).items()):
        if snap_date < coverage_start:
            continue
        rebuilt = history.members_on(snap_date) or frozenset()
        if rebuilt != members:
            extra, missing = sorted(rebuilt - members), sorted(members - rebuilt)
            residuals.append(
                Residual(
                    slug,
                    ResidualKind.SNAPSHOT_MISMATCH,
                    snap_date,
                    f"reconstruction has {len(extra)} not in the snapshot {extra[:5]}, "
                    f"lacks {len(missing)} it holds {missing[:5]}",
                )
            )
    return IndexHistory(
        index_slug=slug,
        anchor_date=anchor_date,
        anchor_l0_key=anchor_l0_key,
        coverage_start=coverage_start,
        expected_size=expected,
        intervals=ordered,
        residuals=tuple(residuals),
        segments=tuple(segments),
        events_applied=history.events_applied,
    )


def _event_order(resolved: ResolvedEvent) -> tuple[date, str, str]:
    return (resolved.event.effective, resolved.event.action.value, resolved.isin or "")


@dataclass(frozen=True, slots=True)
class _Voiding:
    """A release that declared earlier announcements void in prose no table parser can read."""

    voided_releases: tuple[str, ...]
    effective: date
    except_indices: tuple[str, ...]
    quote: str


#: Errata read from the releases themselves, keyed by the release that states them. Each one is a
#: sentence of the exchange's, quoted, applied only when that release is in L0. Kept as data
#: because it is one sentence in thirteen hundred releases; a second would earn a parser.
_VOIDINGS: Final[Mapping[str, _Voiding]] = {
    "ind_prs13052020.pdf": _Voiding(
        voided_releases=("ind_prs18022020.pdf", "ind_prs12032020.pdf", "ind_prs19032020.pdf"),
        effective=date(2020, 3, 27),
        except_indices=("nifty50",),
        quote=(
            "Replacements in various indices effective March 27, 2020 announced vide press release "
            "dated February 18, March 12 and March 19, 2020 (except replacements in NIFTY 50 and "
            "NIFTY Bank index as they had been rebalanced effective March 19, 2020) shall stand "
            "null and void"
        ),
    ),
}


def _apply_voidings(
    events: Sequence[IndexChangeEvent], present: Iterable[str]
) -> list[IndexChangeEvent]:
    """Drop every event of a voided release, whatever date the parser read for it.

    The voiding names whole releases ("replacements … announced vide press release dated February
    18, March 12 and March 19, 2020 … shall stand null and void"), each of which was entirely the
    March 27, 2020 rebalancing — so it is matched by release, not by (release, effective). Matching
    on the date let ind_prs19032020's Midcap 150 rows through: its prose recounts a March 19 change
    before its tables, and the parser dated the next section by it, which put Yes Bank into
    NIFTY Midcap 150 for 2017-2020 alongside its NIFTY 50 seat.
    """
    held = set(present)
    excepted: dict[str, tuple[str, ...]] = {}
    for release, voiding in _VOIDINGS.items():
        if release not in held:
            continue
        for target in voiding.voided_releases:
            excepted[target] = voiding.except_indices
    kept: list[IndexChangeEvent] = []
    for ev in events:
        if ev.release in excepted and ev.index_slug not in excepted[ev.release]:
            _LOG.info(
                "index_history.event_voided",
                source=PRESS_RELEASE_SOURCE_ID,
                index=ev.index_slug,
                symbol=ev.symbol,
                release=ev.release,
                effective=ev.effective.isoformat(),
            )
            continue
        kept.append(ev)
    return kept


def _apply_revocations(events: Sequence[IndexChangeEvent]) -> list[IndexChangeEvent]:
    """Withdraw each change a later release revoked before it took effect.

    A revocation cancels the most recently announced matching change (same index, symbol and the
    action it names) that was announced before it and had not yet taken effect when it was. One
    with nothing to cancel is logged and dropped — it cannot add membership on its own.
    """
    revocations = [ev for ev in events if ev.action.is_revocation]
    live = [ev for ev in events if not ev.action.is_revocation]
    for rev in revocations:
        targets = [
            ev
            for ev in live
            if ev.index_slug == rev.index_slug
            and ev.action is rev.action.revoked
            and _norm(ev.symbol or "") == _norm(rev.symbol or "")
            and ev.announced < rev.announced <= ev.effective
        ]
        if not targets:
            _LOG.warning(
                "index_history.revocation_without_target",
                source=PRESS_RELEASE_SOURCE_ID,
                index=rev.index_slug,
                symbol=rev.symbol,
                release=rev.release,
            )
            continue
        live.remove(max(targets, key=lambda ev: (ev.announced, ev.release)))
    return live


def _supersede(events: Sequence[IndexChangeEvent]) -> list[IndexChangeEvent]:
    """Drop announcements a later release revised before they took effect.

    A postponed change (Jio Financial's exclusion, announced for one date and re-announced for a
    later one) appears twice for the same (index, company, action). The earlier one is superseded
    when the later release was announced on or before the earlier effective date — it changed a
    plan that had not yet happened. Two genuine changes years apart are both kept.
    """
    keep: list[IndexChangeEvent] = []
    groups: dict[tuple[str, str, str], list[IndexChangeEvent]] = defaultdict(list)
    for ev in events:
        groups[(ev.index_slug, ev.action.value, _norm(ev.symbol or ev.company_name))].append(ev)
    for group in groups.values():
        ordered = sorted(group, key=lambda e: (e.announced, e.release))
        for i, ev in enumerate(ordered):
            if any(
                later.announced <= ev.effective and later.release != ev.release
                for later in ordered[i + 1 :]
            ):
                continue
            if any(
                k.release == ev.release and k.effective == ev.effective
                for k in keep
                if (k.index_slug, k.action, k.symbol) == (ev.index_slug, ev.action, ev.symbol)
            ):
                continue
            keep.append(ev)
    return keep


# ── the build ──────────────────────────────────────────────────────────────────────────────────


def build_membership_history(
    *,
    l0: L0Store,
    as_of: date,
    data_root: Path | None = None,
    evidence: SymbolEvidence | None = None,
    reissues: Sequence[tuple[str, str, date]] | None = None,
    transcriptions: Mapping[str, ReleaseTranscription] | None = None,
) -> HistoryBuild:
    """Rebuild every tracked index's history from L0 (listing, anchors, releases) and L1 identity.

    Offline by construction: it reads the listing captured for `as_of`, the anchor CSVs captured
    for `as_of`, and whichever candidate releases are in L0. It writes nothing — pass the result to
    `write_membership_history`. `evidence` and `reissues` default to the lake's (L1 `prices_raw`
    and the lineage derived from it); a test injects them.

    A release with no text layer is read from its curated transcription
    (`index_transcription`, default: the reviewed file) when one pins that exact L0 object; its
    derived (not read) rows are reported per index as `DERIVED_EVENTS`.
    """
    if transcriptions is None:
        transcriptions = load_release_transcriptions()
    listing_ref = l0.ref_for(
        PRESS_RELEASE_SOURCE_ID, as_of, f"press_release_listing_{as_of:%Y%m%d}.html"
    )
    releases = parse_press_release_listing(l0.get(listing_ref), filename=listing_ref.filename)
    candidates = candidate_releases(r for r in releases if r.announced <= as_of)

    anchors: dict[str, tuple[ConstituentSnapshot, str]] = {}
    for slug in TRACKED_INDICES:
        ref = l0.ref_for(CONSTITUENTS_SOURCE_ID, as_of, l0_constituents_filename(slug, as_of))
        snapshot = parse_constituents_l0(l0, ref, index_slug=slug, index_name=slug, as_of=as_of)
        anchors[slug] = (snapshot, ref.key)

    parses: list[PressReleaseParse] = []
    missing: list[PressRelease] = []
    unreadable: list[tuple[PressRelease, str]] = []
    transcribed: list[ReleaseTranscription] = []
    for release in candidates:
        if not l0.exists(PRESS_RELEASE_SOURCE_ID, release.announced, release.filename):
            missing.append(release)
            continue
        ref = l0.ref_for(PRESS_RELEASE_SOURCE_ID, release.announced, release.filename)
        try:
            parses.append(parse_press_release_l0(l0, ref, release))
        except ParseError as exc:
            transcription = transcriptions.get(release.filename)
            if transcription is None:
                unreadable.append((release, str(exc)))
                continue
            try:
                parses.append(transcription_parse(transcription, ref))
            except ParseError as mismatch:
                unreadable.append((release, f"{exc}; {mismatch}"))
                continue
            transcribed.append(transcription)
            _LOG.info(
                "index_history.release_transcribed",
                source=PRESS_RELEASE_SOURCE_ID,
                filename=release.filename,
                announced=release.announced.isoformat(),
                transcribed_by=transcription.transcribed_by,
                transcribed_on=transcription.transcribed_on.isoformat(),
                state="VALIDATED",
            )

    # The global horizon: no reconstruction across a release we do not hold or cannot read at all.
    global_horizon = min(r.announced for r in candidates) if candidates else as_of
    residual_seed: list[Residual] = []
    for release in missing:
        reach = release.title_effective or release.announced + UNDATED_RELEASE_HORIZON
        global_horizon = max(global_horizon, reach)
    for release, why in unreadable:
        reach = release.title_effective or release.announced + UNDATED_RELEASE_HORIZON
        global_horizon = max(global_horizon, reach)
        for slug in TRACKED_INDICES:
            residual_seed.append(
                Residual(
                    slug, ResidualKind.UNREADABLE_SECTION, reach, why, release=release.filename
                )
            )

    horizon: dict[str, date] = dict.fromkeys(TRACKED_INDICES, global_horizon)
    for transcription in transcribed:
        for section in transcription.sections:
            derived = [r for r in (*section.exclude, *section.include) if r.derived]
            if derived:
                residual_seed.append(
                    Residual(
                        section.index_slug,
                        ResidualKind.DERIVED_EVENTS,
                        transcription.effective,
                        f"{len(derived)} of {len(section.exclude) + len(section.include)} events "
                        f"derived, not read ({section.derivation})",
                        release=transcription.release,
                    )
                )
    release_by_name = {r.filename: r for r in candidates}
    unparsed_rows: list[tuple[str, str]] = []
    for parse in parses:
        for problem in parse.unparsed:
            unparsed_rows.append((parse.release, problem))
            release = release_by_name[parse.release]
            reach = release.title_effective or release.announced + UNDATED_RELEASE_HORIZON
            for slug in _slugs_named(problem):
                horizon[slug] = max(horizon[slug], reach)
                residual_seed.append(
                    Residual(
                        slug, ResidualKind.UNREADABLE_SECTION, reach, problem, release=parse.release
                    )
                )

    if evidence is None:
        evidence = load_symbol_evidence(
            data_root=data_root, anchors=[a for a, _ in anchors.values()]
        )
    if reissues is None:
        spans, sessions = read_equity_spans(data_root=data_root)
        reissues = tuple(
            (e.predecessor_isin, e.successor_isin, e.effective_date)
            for e in derive_edges(spans, sessions, {})
            if e.gap_sessions <= _MAX_REISSUE_GAP_SESSIONS
        )

    raw_events = [
        ev for p in parses for ev in p.events if ev.effective <= as_of + timedelta(days=120)
    ]
    all_events = _supersede(
        _apply_revocations(_apply_voidings(raw_events, (p.release for p in parses)))
    )
    resolved: list[ResolvedEvent] = []
    for ev in all_events:
        if ev.symbol is None:
            resolved.append(ResolvedEvent(ev, None, "the release prints no symbol"))
            continue
        found, reason = evidence.resolve(ev.symbol, ev.effective)
        resolved.append(ResolvedEvent(ev, found, reason))

    for r in resolved:
        if r.isin is None and r.event.effective >= horizon[r.event.index_slug]:
            # An event we cannot place: the walk may not cross it, so the depth stops at it.
            residual_seed.append(
                Residual(
                    r.event.index_slug,
                    ResidualKind.UNRESOLVED_SYMBOL,
                    r.event.effective,
                    f"{r.event.company_name} ({r.event.symbol}): {r.reason}",
                    release=r.event.release,
                )
            )
            _LOG.error(
                "index_history.event_quarantined",
                source=PRESS_RELEASE_SOURCE_ID,
                index=r.event.index_slug,
                symbol=r.event.symbol,
                company=r.event.company_name,
                effective=r.event.effective.isoformat(),
                release=r.event.release,
                reason=r.reason,
                state="QUARANTINED",
            )
            horizon[r.event.index_slug] = max(
                horizon[r.event.index_slug], r.event.effective + timedelta(days=1)
            )

    histories: dict[str, IndexHistory] = {}
    for slug, (anchor, anchor_key) in anchors.items():
        mine = [r for r in resolved if r.event.index_slug == slug]
        history = reconstruct_index(
            slug,
            anchor=anchor,
            anchor_l0_key=anchor_key,
            events=mine,
            coverage_start=min(horizon[slug], as_of),
            reissues=reissues,
            evidence=evidence,
            snapshots=_stored_snapshot_sets(slug, data_root),
        )
        # Only what bounds or falls inside the coverage: an older unreadable section is beyond it.
        seeded = tuple(
            r
            for r in residual_seed
            if r.index_slug == slug and r.on_date >= history.coverage_start - timedelta(days=1)
        )
        histories[slug] = IndexHistory(
            index_slug=history.index_slug,
            anchor_date=history.anchor_date,
            anchor_l0_key=history.anchor_l0_key,
            coverage_start=history.coverage_start,
            expected_size=history.expected_size,
            intervals=history.intervals,
            residuals=seeded + history.residuals,
            segments=history.segments,
            events_applied=history.events_applied,
        )
        _LOG.info(
            "index_history.index_built",
            source=PRESS_RELEASE_SOURCE_ID,
            index=slug,
            as_of=as_of.isoformat(),
            coverage_start=history.coverage_start.isoformat(),
            intervals=len(history.intervals),
            events=history.events_applied,
            residuals=len(histories[slug].residuals),
            state="NORMALIZED",
        )

    return HistoryBuild(
        as_of=as_of,
        listing_l0_key=listing_ref.key,
        releases_considered=len(candidates),
        releases_in_l0=len(parses) + len(unreadable),
        releases_missing=tuple(r.filename for r in missing),
        histories=histories,
        events=tuple(resolved),
        unparsed=tuple(unparsed_rows) + tuple((r.filename, why) for r, why in unreadable),
        transcribed=tuple(t.release for t in transcribed),
    )


def _slugs_named(problem: str) -> list[str]:
    """Which tracked index an unparsed-section message is about (its label precedes the colon)."""
    from dataplatform.ingest.index_changes import canonical_index_slug

    slug = canonical_index_slug(problem.split(":", 1)[0])
    return [slug] if slug is not None else []


# ── L1: write once, read the latest build ──────────────────────────────────────────────────────

_HISTORY_SCHEMA: Final = pa.schema(
    [
        pa.field("index_slug", pa.string(), nullable=False),
        pa.field("isin", pa.string(), nullable=False),
        pa.field("effective_from", pa.date32(), nullable=False),
        pa.field("effective_to", pa.date32(), nullable=True),
        pa.field("knowable_from", pa.date32(), nullable=False),
        pa.field("exit_knowable", pa.date32(), nullable=True),
        pa.field("entry_basis", pa.string(), nullable=False),
        pa.field("exit_basis", pa.string(), nullable=True),
        pa.field("coverage_start", pa.date32(), nullable=False),
        pa.field("anchor_date", pa.date32(), nullable=False),
        pa.field("anchor_l0_key", pa.string(), nullable=False),
    ]
)

_EVENTS_SCHEMA: Final = pa.schema(
    [
        pa.field("index_slug", pa.string(), nullable=False),
        pa.field("action", pa.string(), nullable=False),
        pa.field("company_name", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=True),
        pa.field("isin", pa.string(), nullable=True),
        pa.field("effective", pa.date32(), nullable=False),
        pa.field("announced", pa.date32(), nullable=False),
        pa.field("release", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=True),
        pa.field("quarantine_reason", pa.string(), nullable=True),
    ]
)


def write_membership_history(
    build: HistoryBuild, *, data_root: Path | None = None
) -> tuple[Path, ...]:
    """Write a build to L1: one `<slug>.parquet` per index plus the resolved events, write-once.

    Layout: `L1/index_membership_history/date=<as_of>/<slug>.parquet` and
    `L1/index_change_events/date=<as_of>/events.parquet`. A rebuild that produces byte-identical
    rows is a no-op; one that would change a stored build raises `ImmutableHistoryError` — a newer
    reconstruction is a newer `as_of`, never an edit of an old one.
    """
    written: list[Path] = []
    for slug, history in sorted(build.histories.items()):
        rows = [_interval_record(history, i) for i in history.intervals]
        path = l1_partition_path(
            HISTORY_DATASET, build.as_of, filename=f"{slug}.parquet", data_root=data_root
        )
        written.append(_write_once(path, pa.Table.from_pylist(rows, schema=_HISTORY_SCHEMA)))
    event_rows = [
        {
            "index_slug": r.event.index_slug,
            "action": r.event.action.value,
            "company_name": r.event.company_name,
            "symbol": r.event.symbol,
            "isin": r.isin,
            "effective": r.event.effective,
            "announced": r.event.announced,
            "release": r.event.release,
            "l0_key": r.event.l0_key,
            "quarantine_reason": r.reason,
        }
        for r in sorted(
            build.events,
            key=lambda r: (
                r.event.effective,
                r.event.index_slug,
                r.event.action.value,
                r.event.company_name,
            ),
        )
    ]
    path = l1_partition_path(
        EVENTS_DATASET, build.as_of, filename="events.parquet", data_root=data_root
    )
    written.append(_write_once(path, pa.Table.from_pylist(event_rows, schema=_EVENTS_SCHEMA)))
    _LOG.info(
        "index_history.l1_written",
        source=PRESS_RELEASE_SOURCE_ID,
        as_of=build.as_of.isoformat(),
        dataset=HISTORY_DATASET,
        files=len(written),
        state="PUBLISHED",
    )
    return tuple(written)


def _write_once(path: Path, table: pa.Table) -> Path:
    if path.exists():
        if pq.read_table(path, schema=table.schema).equals(table):
            return path
        raise ImmutableHistoryError(
            f"{path} already holds a different build; a newer reconstruction takes a newer as_of"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)
    return path


def _interval_record(history: IndexHistory, interval: MembershipInterval) -> dict[str, Any]:
    return {
        "index_slug": interval.index_slug,
        "isin": interval.isin,
        "effective_from": interval.effective_from,
        "effective_to": interval.effective_to,
        "knowable_from": interval.knowable_from,
        "exit_knowable": interval.exit_knowable,
        "entry_basis": interval.entry_basis,
        "exit_basis": interval.exit_basis,
        "coverage_start": history.coverage_start,
        "anchor_date": history.anchor_date,
        "anchor_l0_key": history.anchor_l0_key,
    }


def read_membership_history(
    index_slug: str, *, data_root: Path | None = None
) -> IndexHistory | None:
    """The newest stored build of one index's history, or `None` when none was ever written."""
    root = layer_root(Layer.L1, data_root=data_root) / HISTORY_DATASET
    if not root.is_dir():
        return None
    builds: list[date] = []
    for partition in root.iterdir():
        try:
            build_date = partition_date_of(partition)
        except ValueError:
            continue
        if (partition / f"{index_slug}.parquet").exists():
            builds.append(build_date)
    if not builds:
        return None
    path = l1_partition_path(
        HISTORY_DATASET, max(builds), filename=f"{index_slug}.parquet", data_root=data_root
    )
    records = pq.read_table(path, schema=_HISTORY_SCHEMA).to_pylist()
    if not records:
        raise ParseError("history file has no rows", filename=str(path))
    head = records[0]
    intervals = tuple(
        MembershipInterval(
            index_slug=str(r["index_slug"]),
            isin=str(r["isin"]),
            effective_from=r["effective_from"],
            effective_to=r["effective_to"],
            knowable_from=r["knowable_from"],
            exit_knowable=r["exit_knowable"],
            entry_basis=str(r["entry_basis"]),
            exit_basis=None if r["exit_basis"] is None else str(r["exit_basis"]),
        )
        for r in records
    )
    return IndexHistory(
        index_slug=index_slug,
        anchor_date=head["anchor_date"],
        anchor_l0_key=str(head["anchor_l0_key"]),
        coverage_start=head["coverage_start"],
        expected_size=TRACKED_INDICES.get(index_slug, 0),
        intervals=intervals,
        residuals=(),
        segments=(),
        events_applied=0,
    )


def members_asof(
    index_slug: str, on_date: date, *, data_root: Path | None = None
) -> tuple[frozenset[str], IndexHistory] | None:
    """The PIT membership of `index_slug` on `on_date` from the stored history, with its build.

    What it does: applies the PIT rule (effective on `on_date` and knowable by it) to the newest
    stored build.
    What it never does: answer for a date before the build's `coverage_start` — that is `None`,
    the same "gap, not today's list" answer `indices.membership_asof` gives before its first
    snapshot.
    """
    history = read_membership_history(index_slug, data_root=data_root)
    if history is None:
        return None
    members = history.members_on(on_date)
    if members is None:
        return None
    return members, history


# ── the report ─────────────────────────────────────────────────────────────────────────────────


def _clipped_on(history: IndexHistory, on_date: date) -> int:
    """Members effective on `on_date` whose stay was clipped to their first appearance."""
    return sum(
        1
        for i in history.intervals
        if i.entry_basis.startswith("first_seen")
        and i.effective_from <= on_date
        and (i.effective_to is None or on_date < i.effective_to)
    )


def _second_lines_on(history: IndexHistory, on_date: date) -> int:
    """Members effective on `on_date` that are a second share class of an issuer also in the index.

    Tata Motors' 'A' Ordinary (DVR) shares (IN9155A01020) sat in NIFTY 100 and NIFTY 200 beside
    the ordinary shares (INE155A01022) until 2020-06-26: the index carried 101/201 securities of
    100/200 companies. An `IN9` ISIN (a DVR or partly-paid line) whose issuer code (ISIN characters
    3-7) matches another member's is that case, and its excess is real, not a reconstruction error.
    """
    members = [
        i.isin
        for i in history.intervals
        if i.effective_from <= on_date and (i.effective_to is None or on_date < i.effective_to)
    ]
    issuers = {isin[3:7] for isin in members if not isin.startswith("IN9")}
    return sum(1 for isin in members if isin.startswith("IN9") and isin[3:7] in issuers)


def render_history_report(build: HistoryBuild) -> str:
    """The depth and reconciliation report, generated from the build itself."""
    lines = [
        f"# Index membership history — build {build.as_of.isoformat()}",
        "",
        f"Listing: `{build.listing_l0_key}`. Candidate releases: {build.releases_considered}; "
        f"in L0: {build.releases_in_l0}; not fetched: {len(build.releases_missing)}.",
        "",
        "A segment is the span between two consecutive change dates; its count is the members "
        "effective on its first day. *Explained* off-size segments are those where the excess is "
        "exactly the members clipped to their first appearance (a demerger spin-off's stand-in "
        "window, which the index really carried) plus any second share class of an issuer already "
        "in the index (Tata Motors DVR beside the ordinary shares, to 2020-06-26).",
        "",
        "| Index | Expected | Coverage start | Anchor | Events applied | Segments | "
        "Off-size (unexplained) | Max abs unexplained residual | Other residuals |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for slug, h in build.histories.items():
        off = [(d, c) for d, c in h.segments if c != h.expected_size]
        unexplained = [
            (d, c)
            for d, c in off
            if c - _clipped_on(h, d) - _second_lines_on(h, d) != h.expected_size
        ]
        worst = max((abs(c - h.expected_size) for _, c in unexplained), default=0)
        other: defaultdict[str, int] = defaultdict(int)
        for r in h.residuals:
            if r.kind is not ResidualKind.COUNT:
                other[r.kind.value] += 1
        lines.append(
            f"| {slug} | {h.expected_size} | {h.coverage_start} | {h.anchor_date} | "
            f"{h.events_applied} | {len(h.segments)} | {len(off)} ({len(unexplained)}) | {worst} | "
            f"{', '.join(f'{k}={v}' for k, v in sorted(other.items())) or '—'} |"
        )
    for slug, h in build.histories.items():
        lines += [
            "",
            f"## {slug}",
            "",
            "| Segment start | Reconstructed | Residual | Clipped stand-ins | Second share class |",
            "| --- | --- | --- | --- | --- |",
        ]
        for d, c in h.segments:
            lines.append(
                f"| {d} | {c} | {c - h.expected_size:+d} | {_clipped_on(h, d) or ''} | "
                f"{_second_lines_on(h, d) or ''} |"
            )
        detail = [r for r in h.residuals if r.kind is not ResidualKind.COUNT]
        if detail:
            lines += ["", "Residuals:", ""]
            for r in detail:
                lines.append(
                    f"- {r.on_date} `{r.kind.value}` {r.isin or ''} {r.release or ''} — {r.detail}"
                )
    if build.transcribed:
        lines += ["", "## Releases read from a curated transcription", ""]
        lines += [
            f"- {name}: no text layer; read from `index_release_transcriptions.yaml` "
            "(sha256-pinned to the L0 object)"
            for name in build.transcribed
        ]
    quarantined = [r for r in build.events if r.isin is None]
    if quarantined:
        lines += ["", "## Quarantined events (unresolved symbol)", ""]
        for q in quarantined:
            lines.append(
                f"- {q.event.effective} {q.event.index_slug} {q.event.action.value} "
                f"{q.event.company_name} ({q.event.symbol}) [{q.event.release}] — {q.reason}"
            )
    if build.unparsed:
        lines += ["", "## Unreadable releases / sections", ""]
        lines += [f"- {name}: {why}" for name, why in build.unparsed]
    lines.append("")
    return "\n".join(lines)
