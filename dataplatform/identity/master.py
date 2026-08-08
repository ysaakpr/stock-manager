"""D2: the identity master — the one place a symbol may become an ISIN.

Invariant #2 says nothing in this system joins on a raw symbol. That is not a style rule: NSE
symbols are recycled and renamed (`CADILAHC` became `ZYDUSLIFE` in March 2022; `LTI` became
`LTIM` that December), so a table keyed on a symbol silently stitches two companies' histories
together, and the join that did it leaves no trace. Every other module therefore holds an
`IdentityMaster` and calls `resolve(symbol, on_date)`, which is the only legitimate symbol→ISIN
path in the codebase.

The module is in three layers.

**Model** — `SymbolWindow`, `Security`, `Listing`. A window is `(isin, exchange, symbol)` valid
over `[valid_from, valid_to]`, `valid_to = None` meaning current. Renames *append* a window and
close the previous one; a symbol is never edited in place, because yesterday's bhavcopy still
says the old name and must still resolve (§4.1: "keep full change history, never overwrite").

**Resolution** — `IdentityMaster`, a pure in-memory index over windows. Two directions
(`resolve` and `symbol_as_of`), each with a `try_` variant returning `None` for "never heard of
it", because a bulk writer must be able to quarantine and count unresolvable rows rather than
die on the first one (M1.8). Neither variant is lenient about *ambiguity*: a symbol naming two
ISINs on one date raises `AmbiguousSymbolError` and lands in the reconciliation queue, because
picking one is an error nothing downstream could ever detect.

**Persistence** — `IdentityStore` over `security_master`, `symbol_history`, `exchange_listing`
and `identity_reconciliation`. It never commits; the caller owns the transaction, exactly as
`dataplatform.store.db.connection` intends.

Deriving windows from NSE's published files is `dataplatform.identity.ingest`'s job, not this
module's. Nothing here reads a file, opens a socket or knows what NSE's CSVs look like.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from dataplatform.clock import Clock, SystemClock
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = [
    "AmbiguousSymbolError",
    "ConflictKind",
    "DetectedBy",
    "Exchange",
    "HistoryPlan",
    "HistoryRefusal",
    "IdentityConflict",
    "IdentityError",
    "IdentityMaster",
    "IdentityStore",
    "InMemoryReconciliationQueue",
    "Listing",
    "ListingStatus",
    "ReconciliationQueue",
    "Security",
    "SymbolWindow",
    "UnknownIsinError",
    "UnknownSymbolError",
    "WriteCounts",
    "detect_conflicts",
    "plan_history",
]

_log = get_logger(__name__)

#: The date used as "the end of time" when comparing an open window (`valid_to is None`) against
#: a closed one. Only ever an intermediate value inside an overlap test; never stored.
_OPEN_END = date.max


class Exchange(StrEnum):
    """The exchanges this system holds identities for. Mirrors the CHECK in 0001_init.sql."""

    NSE = "NSE"
    BSE = "BSE"


class ListingStatus(StrEnum):
    """Whether a security still trades. Mirrors the CHECK in 0001_init.sql."""

    ACTIVE = "ACTIVE"
    """Listed and trading as of the most recent snapshot that mentioned it."""

    SUSPENDED = "SUSPENDED"
    """Listed but not trading. Comes back; the ISIN keeps its history either way."""

    DELISTED = "DELISTED"
    """Gone. The row is kept forever — a universe that can lose a dead security is
    survivorship-biased by construction (§4.5)."""


class ConflictKind(StrEnum):
    """Which direction of the mapping broke."""

    SYMBOL_TO_ISIN = "SYMBOL_TO_ISIN"
    """One symbol, two ISINs, one date. The dangerous one: it silently merges two companies."""

    ISIN_TO_SYMBOL = "ISIN_TO_SYMBOL"
    """One ISIN, two symbols, one date. Usually a rename recorded with the wrong effective
    date, and it makes the as-of symbol lookup a coin toss."""


class DetectedBy(StrEnum):
    """Where the ambiguity was noticed. The same defect can be found by both."""

    INGEST = "INGEST"
    """Two source rows disagreed while the master was being built."""

    RESOLVE = "RESOLVE"
    """A caller asked the stored master a question it could not answer with one row."""


class IdentityError(Exception):
    """Base for every identity failure, so a caller can catch the module without the world."""


class UnknownSymbolError(IdentityError):
    """No security has ever traded under this (exchange, symbol) on this date."""


class UnknownIsinError(IdentityError):
    """This ISIN has no symbol window covering this date on this exchange."""


class AmbiguousSymbolError(IdentityError):
    """The mapping is not a function here. Carries the conflict that was queued for a human.

    Raised rather than resolved by a heuristic on purpose: every tie-break available (prefer the
    open window, prefer the larger security, prefer the newer row) is right often enough to hide
    the times it is wrong, and a wrong ISIN is undetectable downstream.
    """

    def __init__(self, conflict: IdentityConflict) -> None:
        super().__init__(str(conflict))
        self.conflict = conflict


@dataclass(frozen=True, slots=True)
class SymbolWindow:
    """One `(exchange, symbol)` an ISIN traded under, and the dates it was valid for.

    `valid_to is None` means current — the window is open and today's file still uses this
    symbol. Both bounds are inclusive trading dates in Asia/Kolkata.

    Sorted through `sort_key` rather than by declaration order (`order=True`), which would
    compare an open window's `None` against a closed one's date and raise. Every list this
    module produces is sorted, because a plan and a conflict list that reorder between runs are
    not reproducible.
    """

    exchange: Exchange
    symbol: str
    valid_from: date
    valid_to: date | None
    isin: str
    series: str | None = None
    source: str = "unknown"

    def covers(self, on_date: date) -> bool:
        """Whether this window was in force on `on_date`, both bounds inclusive."""
        return self.valid_from <= on_date and (self.valid_to is None or on_date <= self.valid_to)

    def overlaps(self, other: SymbolWindow) -> bool:
        """Whether the two windows share at least one date. Exchange is not considered."""
        return self.valid_from <= (other.valid_to or _OPEN_END) and other.valid_from <= (
            self.valid_to or _OPEN_END
        )

    @property
    def key(self) -> tuple[str, Exchange, str, date]:
        """The `symbol_history` unique key: one row per (isin, exchange, symbol, valid_from)."""
        return (self.isin, self.exchange, self.symbol, self.valid_from)

    @property
    def sort_key(self) -> tuple[str, str, date, date, str]:
        """A total order over windows, defined even when one of them is open.

        `date.max` stands in for "no end yet", so sorting a mix of open and closed windows
        never compares a date with `None`.
        """
        return (
            self.exchange.value,
            self.symbol,
            self.valid_from,
            self.valid_to or _OPEN_END,
            self.isin,
        )


@dataclass(frozen=True, slots=True)
class Security:
    """One instrument, keyed by ISIN — the spine every other instrument-scoped table points at.

    `first_seen_date`/`last_seen_date` are about *our* snapshots, not the exchange's records:
    they widen as more snapshots are ingested and are the only evidence this file series gives
    about when a security existed.
    """

    isin: str
    name: str
    primary_exchange: Exchange
    status: ListingStatus
    first_seen_date: date
    last_seen_date: date | None = None
    face_value_inr: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Listing:
    """Per-exchange listing facts for an ISIN. One ISIN is commonly listed on both exchanges."""

    isin: str
    exchange: Exchange
    status: ListingStatus
    security_code: str | None = None
    series: str | None = None
    lot_size: int | None = None
    face_value_inr: Decimal | None = None
    listing_date: date | None = None
    delisting_date: date | None = None


@dataclass(frozen=True, slots=True)
class IdentityConflict:
    """An identity that resolves to more than one answer — the thing a human has to fix.

    `symbols` and `isins` are sorted, so the same underlying defect produces the same row
    whichever order the candidates were discovered in and the queue's UNIQUE constraint
    deduplicates it.
    """

    kind: ConflictKind
    exchange: Exchange
    on_date: date
    symbols: tuple[str, ...]
    isins: tuple[str, ...]
    detected_by: DetectedBy
    source: str
    note: str = ""

    def __str__(self) -> str:
        if self.kind is ConflictKind.SYMBOL_TO_ISIN:
            subject = f"symbol {self.symbols[0]!r} -> {list(self.isins)}"
        else:
            subject = f"ISIN {self.isins[0]!r} -> {list(self.symbols)}"
        note = f" ({self.note})" if self.note else ""
        return (
            f"{self.exchange.value} {subject} on {self.on_date.isoformat()} is ambiguous; "
            f"queued for reconciliation{note}"
        )


@dataclass(frozen=True, slots=True)
class HistoryRefusal:
    """A derived window this run declined to write because it would have rewritten history.

    Not an error the caller can fix in code: the stored row is the one the platform has been
    resolving against, and silently moving it would change what a past date means. Surfaced so
    the operator sees that the source and the store now disagree.
    """

    stored: SymbolWindow
    derived: SymbolWindow
    reason: str


@dataclass(frozen=True, slots=True)
class HistoryPlan:
    """What a re-ingest would change in `symbol_history`, decided before any SQL runs."""

    inserts: tuple[SymbolWindow, ...] = ()
    closes: tuple[SymbolWindow, ...] = ()
    unchanged: int = 0
    refusals: tuple[HistoryRefusal, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when the store already says exactly what this run derived — the idempotent case."""
        return not self.inserts and not self.closes

    def applied_to(self, stored: Iterable[SymbolWindow]) -> tuple[SymbolWindow, ...]:
        """The window set this plan would leave behind, without touching the database.

        What `detect_conflicts` has to judge: ambiguity is a property of the *result*, never of
        the union of before and after. Mid-rename that union holds one ISIN's old window still
        open and its new one already open — which reads as ambiguous and is in fact precisely the
        state this plan exists to remove.
        """
        merged = {window.key: window for window in stored}
        for window in (*self.closes, *self.inserts):
            merged[window.key] = window
        return tuple(sorted(merged.values(), key=_sort_key))


@dataclass(frozen=True, slots=True)
class WriteCounts:
    """Rows the store actually changed. Every field is zero on an idempotent re-ingest."""

    securities: int = 0
    listings: int = 0
    windows_inserted: int = 0
    windows_closed: int = 0
    conflicts_queued: int = 0

    @property
    def total(self) -> int:
        return (
            self.securities
            + self.listings
            + self.windows_inserted
            + self.windows_closed
            + self.conflicts_queued
        )


class ReconciliationQueue(Protocol):
    """Where an ambiguous identity goes so a human can fix the data behind it.

    A protocol rather than a class because the resolve path runs in three places with three
    different notions of "record it": a live pipeline writes Postgres, a backtest replay must
    not write anything at all, and a unit test wants a list it can assert on.
    """

    def record(self, conflict: IdentityConflict) -> bool:
        """Queue one conflict. Returns whether it was new; recording twice is not an error."""


class InMemoryReconciliationQueue:
    """A queue that keeps conflicts in a list. The default when no store is wired.

    What it does: deduplicates on the conflict itself and preserves discovery order.
    What it assumes: the process that raised the conflict is the one that will read it — a
    caller that needs the conflict to outlive the process wires `IdentityStore` instead.
    What it never does: swallow the conflict. `IdentityMaster` raises whether or not the queue
    accepted the row; the queue is the audit trail, not the error channel.
    """

    def __init__(self) -> None:
        self._items: list[IdentityConflict] = []
        self._seen: set[IdentityConflict] = set()

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[IdentityConflict]:
        return iter(self._items)

    @property
    def items(self) -> tuple[IdentityConflict, ...]:
        """Every conflict recorded, in discovery order."""
        return tuple(self._items)

    def record(self, conflict: IdentityConflict) -> bool:
        if conflict in self._seen:
            return False
        self._seen.add(conflict)
        self._items.append(conflict)
        return True


class IdentityMaster:
    """The resolver: `(exchange, symbol, date) → ISIN`, and the same journey back.

    What it does: indexes symbol windows by symbol and by ISIN and answers as-of questions
    against them in constant time per lookup, so a bulk L1 write can resolve a few hundred
    thousand rows without touching Postgres.
    What it assumes: the windows it was handed are the whole truth for the exchanges it will be
    asked about. It is a snapshot — an ingest that ran after it was built is invisible to it.
    What it never does: guess. An unknown identity is `None` or `UnknownSymbolError`, and an
    ambiguous one is always the exception, never a pick.
    """

    def __init__(
        self,
        windows: Iterable[SymbolWindow],
        *,
        securities: Iterable[Security] = (),
        listings: Iterable[Listing] = (),
        queue: ReconciliationQueue | None = None,
        source: str = "identity_master",
    ) -> None:
        self._queue: ReconciliationQueue = InMemoryReconciliationQueue() if queue is None else queue
        self._source = source
        by_symbol: dict[tuple[Exchange, str], list[SymbolWindow]] = {}
        by_isin: dict[str, list[SymbolWindow]] = {}
        for window in sorted(windows, key=_sort_key):
            by_symbol.setdefault((window.exchange, window.symbol), []).append(window)
            by_isin.setdefault(window.isin, []).append(window)
        self._by_symbol = {key: tuple(value) for key, value in by_symbol.items()}
        self._by_isin = {key: tuple(value) for key, value in by_isin.items()}
        self._securities = {security.isin: security for security in securities}
        self._listings = {(listing.isin, listing.exchange): listing for listing in listings}

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(securities={len(self._securities)}, "
            f"windows={sum(len(w) for w in self._by_isin.values())})"
        )

    @property
    def queue(self) -> ReconciliationQueue:
        """The reconciliation queue every ambiguity found here is written to."""
        return self._queue

    @property
    def securities(self) -> Mapping[str, Security]:
        """Every security this master knows, by ISIN."""
        return self._securities

    def security(self, isin: str) -> Security:
        """The security for an ISIN. Raises `UnknownIsinError` rather than returning `None`."""
        try:
            return self._securities[isin]
        except KeyError:
            raise UnknownIsinError(f"no security_master row for ISIN {isin!r}") from None

    def listing(self, isin: str, exchange: Exchange = Exchange.NSE) -> Listing | None:
        """The per-exchange listing facts for an ISIN, or `None` if it is not listed there."""
        return self._listings.get((isin, exchange))

    def windows_for(self, isin: str) -> tuple[SymbolWindow, ...]:
        """Every symbol this ISIN has ever traded under, oldest first. Empty if unknown."""
        return self._by_isin.get(isin, ())

    def resolve(self, symbol: str, on_date: date, *, exchange: Exchange = Exchange.NSE) -> str:
        """The ISIN that traded as `symbol` on `exchange` on `on_date`.

        What it does: finds the one symbol window covering that date. This is the only
        legitimate symbol→ISIN path in the codebase (invariant #2).
        What it assumes: `on_date` is a trading date in Asia/Kolkata, and the symbol is the raw
        one off the exchange file — case and surrounding whitespace are normalized here.
        What it never does: pick between two candidates. Two ISINs on one date raise
        `AmbiguousSymbolError` and land in the reconciliation queue.
        """
        resolved = self.try_resolve(symbol, on_date, exchange=exchange)
        if resolved is None:
            raise UnknownSymbolError(
                f"no {exchange.value} security traded as {_normalize(symbol)!r} on "
                f"{on_date.isoformat()}"
            )
        return resolved

    def try_resolve(
        self, symbol: str, on_date: date, *, exchange: Exchange = Exchange.NSE
    ) -> str | None:
        """`resolve`, but `None` instead of `UnknownSymbolError` for an identity we never saw.

        For the bulk writers (M1.8), whose contract is that an unresolvable row is quarantined
        and counted rather than dropped or fatal. Ambiguity still raises: "we do not know" and
        "we know two contradictory things" are different facts and only the first is routine.
        """
        clean = _normalize(symbol)
        matches = [w for w in self._by_symbol.get((exchange, clean), ()) if w.covers(on_date)]
        if not matches:
            return None
        isins = tuple(sorted({window.isin for window in matches}))
        if len(isins) > 1:
            raise AmbiguousSymbolError(
                self._queue_conflict(
                    ConflictKind.SYMBOL_TO_ISIN,
                    exchange=exchange,
                    on_date=on_date,
                    symbols=(clean,),
                    isins=isins,
                    note=f"{len(matches)} overlapping symbol_history windows",
                )
            )
        return isins[0]

    def symbol_as_of(self, isin: str, on_date: date, *, exchange: Exchange = Exchange.NSE) -> str:
        """The symbol this ISIN traded under on `on_date` — the reverse direction.

        What it does: the as-of lookup a report needs to print a name a human recognizes for a
        date in the past, without letting that name back into any join.
        What it assumes: the same window set `resolve` uses, so the two directions can never
        disagree about a date.
        What it never does: fall back to the current symbol. A 2021 report that prints today's
        name for a security that was renamed since is quietly wrong about what was bought.
        """
        resolved = self.try_symbol_as_of(isin, on_date, exchange=exchange)
        if resolved is None:
            raise UnknownIsinError(
                f"ISIN {isin!r} has no {exchange.value} symbol on {on_date.isoformat()}"
            )
        return resolved

    def try_symbol_as_of(
        self, isin: str, on_date: date, *, exchange: Exchange = Exchange.NSE
    ) -> str | None:
        """`symbol_as_of`, returning `None` for an ISIN with no window covering that date."""
        matches = [
            w for w in self._by_isin.get(isin, ()) if w.exchange is exchange and w.covers(on_date)
        ]
        if not matches:
            return None
        symbols = tuple(sorted({window.symbol for window in matches}))
        if len(symbols) > 1:
            raise AmbiguousSymbolError(
                self._queue_conflict(
                    ConflictKind.ISIN_TO_SYMBOL,
                    exchange=exchange,
                    on_date=on_date,
                    symbols=symbols,
                    isins=(isin,),
                    note=f"{len(matches)} overlapping symbol_history windows",
                )
            )
        return symbols[0]

    def _queue_conflict(
        self,
        kind: ConflictKind,
        *,
        exchange: Exchange,
        on_date: date,
        symbols: tuple[str, ...],
        isins: tuple[str, ...],
        note: str,
    ) -> IdentityConflict:
        """Build the conflict, queue it, log it once, and hand it to the exception."""
        conflict = IdentityConflict(
            kind=kind,
            exchange=exchange,
            on_date=on_date,
            symbols=symbols,
            isins=isins,
            detected_by=DetectedBy.RESOLVE,
            source=self._source,
            note=note,
        )
        if self._queue.record(conflict):
            _log.error(
                "identity.ambiguous",
                kind=kind.value,
                exchange=exchange.value,
                on_date=on_date.isoformat(),
                symbols=list(symbols),
                isins=list(isins),
                detected_by=DetectedBy.RESOLVE.value,
            )
        return conflict


def detect_conflicts(
    windows: Iterable[SymbolWindow],
    *,
    source: str,
    detected_by: DetectedBy = DetectedBy.INGEST,
) -> tuple[IdentityConflict, ...]:
    """Every ambiguity latent in a set of windows, found before anything asks a question.

    What it does: compares windows pairwise within each `(exchange, symbol)` group and each
    `(exchange, isin)` group and reports any overlapping pair that disagrees. The conflict's
    `on_date` is the first date both claims are valid — the earliest date a resolve would break.
    What it assumes: nothing about ordering; the result is sorted, so two runs over the same
    data produce the same list.
    What it never does: modify or drop a window. Deciding which claim is wrong is the human's
    job, and the source files are both still evidence until then.
    """
    materialized = sorted(windows, key=_sort_key)
    conflicts: set[IdentityConflict] = set()

    by_symbol: dict[tuple[Exchange, str], list[SymbolWindow]] = {}
    by_isin: dict[tuple[Exchange, str], list[SymbolWindow]] = {}
    for window in materialized:
        by_symbol.setdefault((window.exchange, window.symbol), []).append(window)
        by_isin.setdefault((window.exchange, window.isin), []).append(window)

    for (exchange, symbol), group in by_symbol.items():
        for left, right in _overlapping_pairs(group, lambda w: w.isin):
            conflicts.add(
                IdentityConflict(
                    kind=ConflictKind.SYMBOL_TO_ISIN,
                    exchange=exchange,
                    on_date=max(left.valid_from, right.valid_from),
                    symbols=(symbol,),
                    isins=tuple(sorted({left.isin, right.isin})),
                    detected_by=detected_by,
                    source=source,
                    note=f"{_describe(left)} and {_describe(right)} overlap",
                )
            )

    for (exchange, isin), group in by_isin.items():
        for left, right in _overlapping_pairs(group, lambda w: w.symbol):
            conflicts.add(
                IdentityConflict(
                    kind=ConflictKind.ISIN_TO_SYMBOL,
                    exchange=exchange,
                    on_date=max(left.valid_from, right.valid_from),
                    symbols=tuple(sorted({left.symbol, right.symbol})),
                    isins=(isin,),
                    detected_by=detected_by,
                    source=source,
                    note=f"{_describe(left)} and {_describe(right)} overlap",
                )
            )

    return tuple(sorted(conflicts, key=lambda c: (c.kind, c.exchange, c.on_date, c.symbols)))


def plan_history(stored: Iterable[SymbolWindow], derived: Iterable[SymbolWindow]) -> HistoryPlan:
    """Decide what a re-ingest may do to `symbol_history` — insert, close, or refuse.

    What it does: diffs the derived windows against what is already stored on the
    `(isin, exchange, symbol, valid_from)` key. A key that is not stored is an insert. A stored
    key whose `valid_to` is open and whose derived `valid_to` is a date is a *close* — the one
    mutation history permits, and the mutation a rename is made of.
    What it assumes: both sides describe the same exchanges; windows for an exchange this run
    did not look at simply do not appear on the derived side and are left alone.
    What it never does: move a bound that is already closed, reopen a closed window, or change
    a window's ISIN. Those become `refusals`, so "never overwrite history" (§4.1) is enforced
    here — where it can be unit-tested — rather than trusted to an `ON CONFLICT` clause.
    """
    by_key = {window.key: window for window in stored}
    inserts: list[SymbolWindow] = []
    closes: list[SymbolWindow] = []
    refusals: list[HistoryRefusal] = []
    unchanged = 0

    for window in sorted(derived, key=_sort_key):
        existing = by_key.get(window.key)
        if existing is None:
            inserts.append(window)
        elif existing.valid_to == window.valid_to:
            unchanged += 1
        elif existing.valid_to is None:
            closes.append(window)
        elif window.valid_to is None:
            refusals.append(
                HistoryRefusal(
                    existing,
                    window,
                    f"stored window closed on {existing.valid_to.isoformat()}; the source now "
                    "calls it current. A closed window is never reopened.",
                )
            )
            unchanged += 1
        else:
            refusals.append(
                HistoryRefusal(
                    existing,
                    window,
                    f"stored window ends {existing.valid_to.isoformat()}, the source now says "
                    f"{window.valid_to.isoformat()}. A closed window is never moved.",
                )
            )
            unchanged += 1

    return HistoryPlan(
        inserts=tuple(inserts),
        closes=tuple(closes),
        unchanged=unchanged,
        refusals=tuple(refusals),
    )


class IdentityStore:
    """The master's four tables, and the only code that writes them.

    What it does: reads `security_master` / `symbol_history` / `exchange_listing` into an
    `IdentityMaster`, applies an ingest's rows back, and queues conflicts into
    `identity_reconciliation`.
    What it assumes: the schema is migrated and one writer is active at a time — a weekly master
    refresh, not a concurrent pipeline.
    What it never does: commit, roll back, or close the connection. The caller owns the
    transaction, so an ingest and the L1 write that depends on it can share one.
    """

    def __init__(self, conn: Connection, *, clock: Clock | None = None) -> None:
        self._conn = conn
        self._clock: Clock = SystemClock() if clock is None else clock

    # ── reads ───────────────────────────────────────────────────────────────────────────────

    def load_master(self, *, queue: ReconciliationQueue | None = None) -> IdentityMaster:
        """Build an `IdentityMaster` from everything currently stored.

        Loads the whole master — a few thousand securities and their windows — because the
        callers that need it (the L1 writer, the query layer) resolve in bulk and a per-symbol
        round trip would dominate their runtime.
        """
        master = IdentityMaster(
            self.load_windows(),
            securities=self.load_securities(),
            listings=self.load_listings(),
            queue=self if queue is None else queue,
            source="identity_store",
        )
        _log.info("identity.master.loaded", securities=len(master.securities))
        return master

    def load_windows(self, isins: Sequence[str] | None = None) -> tuple[SymbolWindow, ...]:
        """Stored symbol windows, all of them or just those of the named ISINs."""
        sql = (
            "SELECT isin, exchange, symbol, series, valid_from, valid_to, source "
            "FROM symbol_history"
        )
        params: tuple[object, ...] = ()
        if isins is not None:
            sql += " WHERE isin = ANY(%s)"
            params = (list(isins),)
        rows = self._conn.execute(sql + " ORDER BY isin, exchange, valid_from", params).fetchall()
        return tuple(
            SymbolWindow(
                isin=str(isin),
                exchange=Exchange(exchange),
                symbol=str(symbol),
                series=None if series is None else str(series),
                valid_from=valid_from,
                valid_to=valid_to,
                source=str(source),
            )
            for isin, exchange, symbol, series, valid_from, valid_to, source in rows
        )

    def load_securities(self) -> tuple[Security, ...]:
        """Every `security_master` row, including the delisted ones (§4.5)."""
        rows = self._conn.execute(
            "SELECT isin, name, primary_exchange, status, face_value_inr, first_seen_date, "
            "last_seen_date FROM security_master ORDER BY isin"
        ).fetchall()
        return tuple(
            Security(
                isin=str(isin),
                name=str(name),
                primary_exchange=Exchange(exchange),
                status=ListingStatus(status),
                face_value_inr=face_value,
                first_seen_date=first_seen,
                last_seen_date=last_seen,
            )
            for isin, name, exchange, status, face_value, first_seen, last_seen in rows
        )

    def load_listings(self) -> tuple[Listing, ...]:
        """Every `exchange_listing` row."""
        rows = self._conn.execute(
            "SELECT isin, exchange, security_code, series, lot_size, face_value_inr, "
            "listing_date, delisting_date, status FROM exchange_listing ORDER BY isin, exchange"
        ).fetchall()
        return tuple(
            Listing(
                isin=str(isin),
                exchange=Exchange(exchange),
                security_code=None if code is None else str(code),
                series=None if series is None else str(series),
                lot_size=lot_size,
                face_value_inr=face_value,
                listing_date=listing_date,
                delisting_date=delisting_date,
                status=ListingStatus(status),
            )
            for (
                isin,
                exchange,
                code,
                series,
                lot_size,
                face_value,
                listing_date,
                delisting_date,
                status,
            ) in rows
        )

    def open_conflicts(self) -> tuple[IdentityConflict, ...]:
        """Everything in the reconciliation queue a human has not yet resolved."""
        rows = self._conn.execute(
            "SELECT kind, exchange, on_date, symbols, isins, detected_by, source, detail "
            "FROM identity_reconciliation WHERE NOT resolved ORDER BY id"
        ).fetchall()
        return tuple(
            IdentityConflict(
                kind=ConflictKind(kind),
                exchange=Exchange(exchange),
                on_date=on_date,
                symbols=tuple(symbols),
                isins=tuple(isins),
                detected_by=DetectedBy(detected_by),
                source=str(source),
                note=str(detail.get("note", "")),
            )
            for kind, exchange, on_date, symbols, isins, detected_by, source, detail in rows
        )

    # ── writes ──────────────────────────────────────────────────────────────────────────────

    def record(self, conflict: IdentityConflict) -> bool:
        """Queue one conflict into `identity_reconciliation`. Satisfies `ReconciliationQueue`.

        Deduplicated by the table's UNIQUE constraint, so a backfill that meets the same bad
        symbol on nine hundred dates leaves one row for one defect, not nine hundred.
        """
        row = self._conn.execute(
            "INSERT INTO identity_reconciliation "
            "(kind, exchange, on_date, symbols, isins, detected_by, source, detail, raised_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (kind, exchange, on_date, symbols, isins) DO NOTHING RETURNING id",
            (
                conflict.kind.value,
                conflict.exchange.value,
                conflict.on_date,
                list(conflict.symbols),
                list(conflict.isins),
                conflict.detected_by.value,
                conflict.source,
                json.dumps({"note": conflict.note}),
                self._clock.now(),
            ),
        ).fetchone()
        return row is not None

    def write_securities(self, securities: Sequence[Security]) -> int:
        """Upsert `security_master` rows; returns how many actually changed.

        `first_seen_date` only ever moves earlier and `last_seen_date` only ever moves later, so
        ingesting an old snapshot after a new one widens the observed window instead of
        rewriting it. `primary_exchange` is set once, on first sight: a security listed on both
        exchanges would otherwise flap with whichever master was ingested last.

        A row whose content is unchanged is not touched at all — not even `updated_at` — which
        is what makes a re-ingest a genuine no-op rather than a no-op with a new timestamp.
        """
        if not securities:
            return 0
        rows = self._conn.execute(
            """
            INSERT INTO security_master AS sm (isin, name, primary_exchange, status,
                                               face_value_inr, first_seen_date, last_seen_date,
                                               created_at, updated_at)
            SELECT t.isin, t.name, t.primary_exchange, t.status, t.face_value_inr,
                   t.first_seen_date, t.last_seen_date, %(now)s::timestamptz, %(now)s::timestamptz
              FROM unnest(%(isin)s::text[], %(name)s::text[], %(exchange)s::text[],
                          %(status)s::text[], %(face_value)s::numeric[], %(first_seen)s::date[],
                          %(last_seen)s::date[])
                AS t(isin, name, primary_exchange, status, face_value_inr,
                     first_seen_date, last_seen_date)
            ON CONFLICT (isin) DO UPDATE
               SET name            = EXCLUDED.name,
                   status          = EXCLUDED.status,
                   face_value_inr  = COALESCE(EXCLUDED.face_value_inr, sm.face_value_inr),
                   first_seen_date = LEAST(sm.first_seen_date, EXCLUDED.first_seen_date),
                   last_seen_date  = GREATEST(sm.last_seen_date, EXCLUDED.last_seen_date),
                   updated_at      = EXCLUDED.updated_at
             WHERE sm.name IS DISTINCT FROM EXCLUDED.name
                OR sm.status IS DISTINCT FROM EXCLUDED.status
                OR (EXCLUDED.face_value_inr IS NOT NULL
                    AND sm.face_value_inr IS DISTINCT FROM EXCLUDED.face_value_inr)
                OR EXCLUDED.first_seen_date < sm.first_seen_date
                OR (EXCLUDED.last_seen_date IS NOT NULL
                    AND (sm.last_seen_date IS NULL
                         OR EXCLUDED.last_seen_date > sm.last_seen_date))
            RETURNING sm.isin
            """,
            {
                "now": self._clock.now(),
                "isin": [s.isin for s in securities],
                "name": [s.name for s in securities],
                "exchange": [s.primary_exchange.value for s in securities],
                "status": [s.status.value for s in securities],
                "face_value": [s.face_value_inr for s in securities],
                "first_seen": [s.first_seen_date for s in securities],
                "last_seen": [s.last_seen_date for s in securities],
            },
        ).fetchall()
        return len(rows)

    def write_listings(self, listings: Sequence[Listing]) -> int:
        """Upsert `exchange_listing` rows; returns how many actually changed."""
        if not listings:
            return 0
        rows = self._conn.execute(
            """
            INSERT INTO exchange_listing AS el (isin, exchange, security_code, series, lot_size,
                                                face_value_inr, listing_date, delisting_date,
                                                status, recorded_at)
            SELECT t.isin, t.exchange, t.security_code, t.series, t.lot_size, t.face_value_inr,
                   t.listing_date, t.delisting_date, t.status, %(now)s::timestamptz
              FROM unnest(%(isin)s::text[], %(exchange)s::text[], %(code)s::text[],
                          %(series)s::text[], %(lot_size)s::integer[], %(face_value)s::numeric[],
                          %(listing_date)s::date[], %(delisting_date)s::date[], %(status)s::text[])
                AS t(isin, exchange, security_code, series, lot_size, face_value_inr,
                     listing_date, delisting_date, status)
            ON CONFLICT (isin, exchange) DO UPDATE
               SET security_code  = COALESCE(EXCLUDED.security_code, el.security_code),
                   series         = EXCLUDED.series,
                   lot_size       = COALESCE(EXCLUDED.lot_size, el.lot_size),
                   face_value_inr = COALESCE(EXCLUDED.face_value_inr, el.face_value_inr),
                   listing_date   = LEAST(el.listing_date, EXCLUDED.listing_date),
                   delisting_date = COALESCE(EXCLUDED.delisting_date, el.delisting_date),
                   status         = EXCLUDED.status,
                   recorded_at    = EXCLUDED.recorded_at
             WHERE el.series IS DISTINCT FROM EXCLUDED.series
                OR el.status IS DISTINCT FROM EXCLUDED.status
                OR (EXCLUDED.security_code IS NOT NULL
                    AND el.security_code IS DISTINCT FROM EXCLUDED.security_code)
                OR (EXCLUDED.lot_size IS NOT NULL
                    AND el.lot_size IS DISTINCT FROM EXCLUDED.lot_size)
                OR (EXCLUDED.face_value_inr IS NOT NULL
                    AND el.face_value_inr IS DISTINCT FROM EXCLUDED.face_value_inr)
                OR (EXCLUDED.listing_date IS NOT NULL
                    AND (el.listing_date IS NULL OR EXCLUDED.listing_date < el.listing_date))
                OR (EXCLUDED.delisting_date IS NOT NULL
                    AND el.delisting_date IS DISTINCT FROM EXCLUDED.delisting_date)
            RETURNING el.isin
            """,
            {
                "now": self._clock.now(),
                "isin": [listing.isin for listing in listings],
                "exchange": [listing.exchange.value for listing in listings],
                "code": [listing.security_code for listing in listings],
                "series": [listing.series for listing in listings],
                "lot_size": [listing.lot_size for listing in listings],
                "face_value": [listing.face_value_inr for listing in listings],
                "listing_date": [listing.listing_date for listing in listings],
                "delisting_date": [listing.delisting_date for listing in listings],
                "status": [listing.status.value for listing in listings],
            },
        ).fetchall()
        return len(rows)

    def apply_history(self, plan: HistoryPlan) -> tuple[int, int]:
        """Execute a `plan_history` decision; returns (inserted, closed).

        The plan is applied exactly as computed — this method contains no policy at all, which
        is the point: every "may we write this" question was already answered by a pure function
        with a unit test behind it.
        """
        inserted = closed = 0
        if plan.inserts:
            rows = self._conn.execute(
                """
                INSERT INTO symbol_history (isin, exchange, symbol, series, valid_from, valid_to,
                                            source, recorded_at)
                SELECT t.isin, t.exchange, t.symbol, t.series, t.valid_from, t.valid_to,
                       t.source, %(now)s::timestamptz
                  FROM unnest(%(isin)s::text[], %(exchange)s::text[], %(symbol)s::text[],
                              %(series)s::text[], %(valid_from)s::date[], %(valid_to)s::date[],
                              %(source)s::text[])
                    AS t(isin, exchange, symbol, series, valid_from, valid_to, source)
                ON CONFLICT (isin, exchange, symbol, valid_from) DO NOTHING
                RETURNING id
                """,
                {
                    "now": self._clock.now(),
                    "isin": [w.isin for w in plan.inserts],
                    "exchange": [w.exchange.value for w in plan.inserts],
                    "symbol": [w.symbol for w in plan.inserts],
                    "series": [w.series for w in plan.inserts],
                    "valid_from": [w.valid_from for w in plan.inserts],
                    "valid_to": [w.valid_to for w in plan.inserts],
                    "source": [w.source for w in plan.inserts],
                },
            ).fetchall()
            inserted = len(rows)

        if plan.closes:
            rows = self._conn.execute(
                """
                UPDATE symbol_history AS sh
                   SET valid_to = t.valid_to
                  FROM unnest(%(isin)s::text[], %(exchange)s::text[], %(symbol)s::text[],
                              %(valid_from)s::date[], %(valid_to)s::date[])
                    AS t(isin, exchange, symbol, valid_from, valid_to)
                 WHERE sh.isin = t.isin AND sh.exchange = t.exchange AND sh.symbol = t.symbol
                   AND sh.valid_from = t.valid_from
                   AND sh.valid_to IS NULL
                RETURNING sh.id
                """,
                {
                    "isin": [w.isin for w in plan.closes],
                    "exchange": [w.exchange.value for w in plan.closes],
                    "symbol": [w.symbol for w in plan.closes],
                    "valid_from": [w.valid_from for w in plan.closes],
                    "valid_to": [w.valid_to for w in plan.closes],
                },
            ).fetchall()
            closed = len(rows)

        return inserted, closed


def _sort_key(window: SymbolWindow) -> tuple[str, str, date, date, str]:
    """`SymbolWindow.sort_key` as a function, for `sorted(..., key=...)`."""
    return window.sort_key


def _normalize(symbol: str) -> str:
    """One spelling of a symbol for the whole platform: stripped and upper-cased.

    NSE's own files disagree with themselves about leading spaces (see `EQUITY_L.csv`'s header),
    and a lookup that misses because of one is indistinguishable from a genuinely unknown
    security — the failure this exists to prevent.
    """
    return symbol.strip().upper()


def _describe(window: SymbolWindow) -> str:
    """A window as one readable phrase, for a conflict note a human will read."""
    end = window.valid_to.isoformat() if window.valid_to else "open"
    return f"{window.isin}[{window.valid_from.isoformat()}..{end}]"


class _Differ(Protocol):
    """The attribute an overlap check compares — `isin` in one direction, `symbol` in the other."""

    def __call__(self, window: SymbolWindow, /) -> str: ...


def _overlapping_pairs(
    group: Sequence[SymbolWindow], differs_on: _Differ
) -> Iterator[tuple[SymbolWindow, SymbolWindow]]:
    """Every pair in `group` that overlaps in time and disagrees on `differs_on`.

    Quadratic in the group size on purpose: a group is the windows sharing one symbol or one
    ISIN, which is a handful even for the most-renamed security in the market.
    """
    for index, left in enumerate(group):
        for right in group[index + 1 :]:
            if differs_on(left) != differs_on(right) and left.overlaps(right):
                yield left, right
