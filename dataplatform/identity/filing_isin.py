"""D2: the ISIN a filing belongs to on the day it became knowable (DQ-ISIN).

NSE's corporate-filing feeds (board-meeting intimations, announcements, the shareholding master)
state a company's *original* ISIN, not the one it trades under: HDFCBANK files as `INE040A01018`,
retired at the 2011 split, while it has traded as `INE040A01034` since the 2019 one. Stored
verbatim, those rows join to nothing a reader holds, and 129 of the NIFTY 500 had no results date
and no announcement in the M17 dossiers (`ops/gates/data-lake-status-2026-11-06.md` §6.1). The
`isin_lineage` table already holds the edges; this module walks them for a filing.

**What it does.** `FilingIsinResolver.resolve(isin, knowable)` follows the lineage forward from
the filed ISIN, one edge at a time, through every edge already effective on the filing's knowable
date, and returns the ISIN the company traded under that day with the reason the walk stopped.
`rekey_rows` applies it to a dataset's rows and keeps the filed ISIN beside the resolved one.

**Point in time.** An edge is effective on its successor's first NSE session, a public fact on
that day. A filing knowable before that day stays on the predecessor: the successor did not exist
yet, and keying the filing to it would use knowledge the filing date could not have had. A filing
knowable on or after it moves to the successor, which only uses what was public by then. So the
mapping never depends on when the walk runs, only on the lineage and the filing's own date.

**What it never does.** Re-key across companies. An edge is walked only when both ends are equity
shares (`INE…01…`) of the same issuer code, the successor has exactly one predecessor, and the
edge is explained: recorded by hand, corroborated by a split or bonus, or a handover with at most
`MAX_EXPLAINED_GAP_SESSIONS` sessions between the two spans. Any other edge stops the walk, and
the status says why, so the caller counts it rather than guessing. An ISIN with no edge at all
(a reissue before the NSE ISIN era, or a filing under a debenture ISIN) is left as filed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Final

from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = [
    "MAX_EXPLAINED_GAP_SESSIONS",
    "UNMAPPED",
    "FilingEdge",
    "FilingIsin",
    "FilingIsinResolver",
    "FilingIsinStatus",
    "RekeyTally",
    "load_filing_resolver",
    "rekey_rows",
    "unmapped_counts",
]

_LOG = get_logger(__name__)

#: The longest gap, in sessions between the predecessor's last bar and the successor's first, that
#: still explains an uncorroborated edge. Measured over the 640 edges on 2026-11-06: 555 are
#: seamless (0), 6 fall at 1 to 5 (a record-date suspension: CCAVENUE's MANUAL edge is 4), and the
#: next is 7, after which the gaps run to 2,859 sessions, where an issuer-code reuse can hide
#: (`lineage.LineageEdge`). A gap past this is unexplained and the walk stops there.
MAX_EXPLAINED_GAP_SESSIONS: Final = 5

#: An equity share: `IN` + `E`, a 4-character issuer, then security type `01`.
_EQUITY_PREFIX: Final = "INE"
_EQUITY_TYPE: Final = "01"
_ISSUER_LEN: Final = 7

_MANUAL: Final = "MANUAL"
_CORROBORATED: Final = "CORROBORATED"


class FilingIsinStatus(StrEnum):
    """Why the walk stopped where it did; each is counted apart, because each is a different fix."""

    #: No edge leaves the filed ISIN: it is the ISIN the company traded under, or lineage has no
    #: evidence for its reissue. Kept as filed.
    AS_FILED = "as_filed"
    #: One or more explained edges were walked; the row is on the ISIN live on its knowable date.
    REKEYED = "rekeyed"
    #: An edge leaves the filed ISIN but takes effect after the knowable date. Kept as filed: the
    #: successor did not exist yet (module docstring).
    PRE_REISSUE = "pre_reissue"
    #: Stopped at a DERIVED, uncorroborated edge with a gap past `MAX_EXPLAINED_GAP_SESSIONS`.
    UNEXPLAINED = "unexplained"
    #: Stopped at an edge that leaves the issuer code or the equity security type.
    CROSS_ISSUER = "cross_issuer"
    #: Stopped at an edge whose successor has more than one predecessor, at a cycle, or because the
    #: re-keyed row would collide with another row's key in the same dataset.
    AMBIGUOUS = "ambiguous"


#: The statuses that leave a row short of the ISIN lineage says it became: counted as unmapped.
UNMAPPED: Final = frozenset(
    {FilingIsinStatus.UNEXPLAINED, FilingIsinStatus.CROSS_ISSUER, FilingIsinStatus.AMBIGUOUS}
)


@dataclass(frozen=True, slots=True)
class FilingEdge:
    """One `isin_lineage` row, with the evidence that decides whether a filing may follow it."""

    predecessor_isin: str
    successor_isin: str
    effective_date: date
    detected_by: str
    confidence: str
    gap_sessions: int


@dataclass(frozen=True, slots=True)
class FilingIsin:
    """Where one filed ISIN lands on one knowable date, and why it stopped there."""

    isin: str
    as_filed: str
    status: FilingIsinStatus
    hops: int


def _is_equity(isin: str) -> bool:
    return isin.startswith(_EQUITY_PREFIX) and isin[_ISSUER_LEN : _ISSUER_LEN + 2] == _EQUITY_TYPE


class FilingIsinResolver:
    """Walks `isin_lineage` forward for a filing, as of the filing's knowable date (module doc)."""

    def __init__(self, edges: Iterable[FilingEdge]) -> None:
        self._forward: dict[str, FilingEdge] = {}
        fan_in: Counter[str] = Counter()
        for edge in edges:
            if edge.predecessor_isin in self._forward:
                raise ValueError(f"two lineage edges leave {edge.predecessor_isin}")
            self._forward[edge.predecessor_isin] = edge
            fan_in[edge.successor_isin] += 1
        self._fan_in = {isin for isin, n in fan_in.items() if n > 1}

    @classmethod
    def empty(cls) -> FilingIsinResolver:
        """A resolver with no edges: every ISIN resolves to itself, `AS_FILED`."""
        return cls(())

    def __len__(self) -> int:
        return len(self._forward)

    def _refusal(self, edge: FilingEdge) -> FilingIsinStatus | None:
        """Why a filing may not follow ``edge``, or None when it may."""
        p, s = edge.predecessor_isin, edge.successor_isin
        if not (_is_equity(p) and _is_equity(s)) or p[:_ISSUER_LEN] != s[:_ISSUER_LEN]:
            return FilingIsinStatus.CROSS_ISSUER
        if s in self._fan_in:
            return FilingIsinStatus.AMBIGUOUS
        if (
            edge.detected_by == _MANUAL
            or edge.confidence == _CORROBORATED
            or edge.gap_sessions <= MAX_EXPLAINED_GAP_SESSIONS
        ):
            return None
        return FilingIsinStatus.UNEXPLAINED

    def resolve(self, isin: str, knowable: date) -> FilingIsin:
        """The ISIN ``isin`` had become by ``knowable``, through explained edges only.

        What it does: follows each edge whose effective date is on or before ``knowable``; stops at
        the first edge not yet effective, the first edge it may not follow (`_refusal`), or a
        cycle, and reports which.
        What it assumes: ``knowable`` is the filing's own knowable date (IST), never the date the
        walk happens to run.
        What it never does: follow an edge effective after ``knowable``, or one across issuers.
        """
        current, hops = isin, 0
        seen = {isin}
        stop = FilingIsinStatus.AS_FILED
        while (edge := self._forward.get(current)) is not None:
            if edge.effective_date > knowable:
                stop = FilingIsinStatus.PRE_REISSUE
                break
            if (refusal := self._refusal(edge)) is not None:
                stop = refusal
                break
            if edge.successor_isin in seen:
                stop = FilingIsinStatus.AMBIGUOUS
                break
            current = edge.successor_isin
            seen.add(current)
            hops += 1
        if stop in UNMAPPED:
            status = stop
        elif hops:
            status = FilingIsinStatus.REKEYED
        else:
            status = stop
        return FilingIsin(isin=current, as_filed=isin, status=status, hops=hops)


def load_filing_resolver(conn: Connection) -> FilingIsinResolver:
    """Every `isin_lineage` row, read as a `FilingIsinResolver`. Read-only; commits nothing."""
    rows = conn.execute(
        "SELECT predecessor_isin, successor_isin, effective_date, detected_by, confidence, "
        "gap_sessions FROM isin_lineage ORDER BY predecessor_isin"
    ).fetchall()
    resolver = FilingIsinResolver(
        FilingEdge(str(p), str(s), eff, str(by), str(conf), int(gap))
        for p, s, eff, by, conf, gap in rows
    )
    _LOG.info("filing_isin.loaded", edges=len(resolver))
    return resolver


@dataclass(slots=True)
class RekeyTally:
    """What one re-key did: rows by status, and each unmapped filed ISIN with its reason."""

    rows: int = 0
    changed: int = 0
    by_status: Counter[str] = field(default_factory=Counter)
    unmapped: dict[str, str] = field(default_factory=dict)

    def add(self, outcome: FilingIsin) -> None:
        self.rows += 1
        self.by_status[outcome.status.value] += 1
        if outcome.status in UNMAPPED:
            self.unmapped.setdefault(outcome.as_filed, outcome.status.value)

    def merge(self, other: RekeyTally) -> None:
        self.rows += other.rows
        self.changed += other.changed
        self.by_status.update(other.by_status)
        for isin, reason in other.unmapped.items():
            self.unmapped.setdefault(isin, reason)

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "changed": self.changed,
            "by_status": dict(sorted(self.by_status.items())),
            "unmapped_isins": len(self.unmapped),
        }


def rekey_rows[R](
    rows: Sequence[R],
    resolver: FilingIsinResolver,
    *,
    as_filed: Callable[[R], str],
    knowable: Callable[[R], date],
    current: Callable[[R], tuple[str, str | None]],
    rebuild: Callable[[R, FilingIsin], R],
    unique_key: Callable[[R], Hashable] | None = None,
) -> tuple[tuple[R, ...], RekeyTally]:
    """Re-key ``rows`` to the ISIN each was filed under had become on its knowable date.

    What it does: resolves each row's filed ISIN (``as_filed``, which must be the source's own
    ISIN, never an earlier re-key's result) at its ``knowable`` date and rebuilds the row with the
    resolved ISIN and the filed one beside it. With ``unique_key``, a re-keyed row whose key would
    collide with another row's is put back on its filed ISIN and counted `AMBIGUOUS`: two filings
    landing on one key is exactly the case a re-key cannot decide. ``current`` reads the row's
    present ``(isin, isin_as_filed)`` so the tally counts rows that actually change.
    What it assumes: the caller's rows all belong to one dataset.
    What it never does: change any field but the ISIN pair, or reorder ``rows``.
    """
    tally = RekeyTally()
    outcomes = [resolver.resolve(as_filed(row), knowable(row)) for row in rows]
    if unique_key is not None:
        rebuilt = [rebuild(row, o) for row, o in zip(rows, outcomes, strict=True)]
        keys = Counter(unique_key(r) for r in rebuilt)
        for i, (row, o) in enumerate(zip(rows, outcomes, strict=True)):
            if o.isin != o.as_filed and keys[unique_key(rebuilt[i])] > 1:
                outcomes[i] = FilingIsin(o.as_filed, o.as_filed, FilingIsinStatus.AMBIGUOUS, 0)
                _LOG.warning(
                    "filing_isin.key_collision",
                    as_filed=o.as_filed,
                    resolved=o.isin,
                    knowable=knowable(row).isoformat(),
                )
    out: list[R] = []
    for row, outcome in zip(rows, outcomes, strict=True):
        tally.add(outcome)
        new = rebuild(row, outcome)
        if current(new) != current(row):
            tally.changed += 1
        out.append(new)
    return tuple(out), tally


def unmapped_counts(tally: RekeyTally) -> Mapping[str, int]:
    """Unmapped filed ISINs per reason — the count the status line and the report carry."""
    return dict(sorted(Counter(tally.unmapped.values()).items()))
