"""D2: turning NSE's published identity files into the master's rows.

Two files, both under §4.1's "Symbol / ISIN master" row, both fetched to L0 by D1 before a byte
of this module runs:

* `EQUITY_L.csv` — every equity listed on NSE *today*: symbol, name, series, listing date, paid-up
  and face value, market lot, ISIN. It is a snapshot with no history in it whatsoever.
* `symbolchange.csv` — every rename NSE has published since 1999, as
  `company, old symbol, new symbol, effective date`, with no header.

Neither file states when a symbol *started*, which is the thing the identity master exists to
know. So the history is reconstructed by walking the rename chain backwards from each current
symbol: `ZYDUSLIFE` is current from 2022-03-07 because that is the day `CADILAHC` became it, and
`CADILAHC` therefore ran from its listing date to 2022-03-06. Chains are followed as far as they
go — 13 NSE securities have been renamed three times or more.

Two things this module is deliberately careful about.

**It never invents coverage.** For 52 of the 2,886 windows in the 2026-08-08 snapshot, NSE's
`DATE OF LISTING` falls *after* the date the oldest symbol in the chain stopped being used — the
listing date is the current entity's, not the original's. Those windows are clamped to end where
they end rather than back-dated to a listing date that means something else, and every one is
reported. A resolve for an earlier date then returns "unknown", which is true, instead of an
ISIN that happens to be right.

**It never overwrites history.** A rename appends a window and closes the previous one; that is
the only mutation `plan_history` permits, and it is decided in `master.py` by a pure function
before any SQL runs.

Nothing here opens a socket. The fetch is D1's (`dataplatform.ingest.fetcher`), the raw bytes are
L0's, and this module takes decoded text — which is what makes the parser testable against the
frozen fixtures in `tests/fixtures/nse_equity_list/` with no network at all (B8).
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.identity.master import (
    DetectedBy,
    Exchange,
    HistoryRefusal,
    IdentityConflict,
    IdentityError,
    IdentityStore,
    Listing,
    ListingStatus,
    Security,
    SymbolWindow,
    WriteCounts,
    detect_conflicts,
    plan_history,
)
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection, connection

__all__ = [
    "EQUITY_LIST_COLUMNS",
    "NSE_EQUITY_LIST_SOURCE",
    "NSE_SYMBOL_CHANGE_SOURCE",
    "ClampedWindow",
    "DerivedMaster",
    "EquityListRow",
    "IdentityIngestReport",
    "IdentityParseError",
    "SymbolChange",
    "derive_master",
    "ingest_snapshot",
    "parse_equity_list",
    "parse_symbol_changes",
]

_log = get_logger(__name__)

#: Source ids, matching `dataplatform/ingest/source_register.yaml` where a row exists. They are
#: written into `symbol_history.source`, so a window can always be traced to the file that
#: produced it — the current symbol comes from the equity list, older ones from the change file.
NSE_EQUITY_LIST_SOURCE: Final = "nse_equity_list"
NSE_SYMBOL_CHANGE_SOURCE: Final = "nse_symbol_change"

#: `EQUITY_L.csv`'s header, with the source's own leading spaces stripped. Asserted rather than
#: assumed: a silently reordered or renamed column would otherwise load names into ISINs.
EQUITY_LIST_COLUMNS: Final[tuple[str, ...]] = (
    "SYMBOL",
    "NAME OF COMPANY",
    "SERIES",
    "DATE OF LISTING",
    "PAID UP VALUE",
    "MARKET LOT",
    "ISIN NUMBER",
    "FACE VALUE",
)

#: The same shape `0001_init.sql`'s `isin` domain enforces, checked here so a bad row names its
#: symbol and line number instead of surfacing as a constraint violation on a 2,400-row insert.
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

#: NSE writes dates as `06-OCT-2008`. Parsed from an explicit table rather than `%b`, whose month
#: names come from the process locale — a platform whose date parsing depends on an environment
#: variable is one bad container away from silently mis-dating a decade of history.
_MONTHS: Final[Mapping[str, int]] = {
    name: number
    for number, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}

#: Renames are followed no further than this. Real chains reach 5; anything beyond this is a
#: cycle the `seen` set failed to catch, and looping forever inside an ingest is worse than
#: failing loudly.
_MAX_CHAIN: Final = 32


class IdentityParseError(IdentityError):
    """A source file is not the shape this parser was written against. Never recoverable here."""


@dataclass(frozen=True, slots=True)
class EquityListRow:
    """One line of `EQUITY_L.csv` — a security as NSE lists it today."""

    symbol: str
    name: str
    series: str
    listing_date: date
    isin: str
    paid_up_value_inr: Decimal | None
    face_value_inr: Decimal | None
    market_lot: int | None


@dataclass(frozen=True, slots=True, order=True)
class SymbolChange:
    """One rename: `old_symbol` became `new_symbol`, effective `effective_date` inclusive."""

    effective_date: date
    old_symbol: str
    new_symbol: str
    company_name: str


@dataclass(frozen=True, slots=True)
class ClampedWindow:
    """A symbol window whose start had to be clamped, and why — reported, never hidden.

    Returned as data rather than raised because it is a property of NSE's files, not a bug:
    `DATE OF LISTING` is the *current* entity's listing date, so for a security renamed after a
    scheme of arrangement it can post-date the rename it is supposed to precede.
    """

    isin: str
    symbol: str
    listing_date: date
    clamped_to: date


@dataclass(frozen=True, slots=True)
class DerivedMaster:
    """Everything one snapshot pair says, in the master's own vocabulary. Nothing is stored yet."""

    securities: tuple[Security, ...]
    listings: tuple[Listing, ...]
    windows: tuple[SymbolWindow, ...]
    clamped: tuple[ClampedWindow, ...] = ()
    renames_applied: int = 0
    unlinked_changes: int = 0


@dataclass(frozen=True, slots=True)
class IdentityIngestReport:
    """What one ingest saw and what it changed. Every count is zero on an idempotent re-run."""

    snapshot_date: date
    exchange: Exchange
    securities_seen: int
    renames_applied: int
    unlinked_changes: int
    counts: WriteCounts
    clamped: tuple[ClampedWindow, ...] = ()
    conflicts: tuple[IdentityConflict, ...] = ()
    refusals: tuple[HistoryRefusal, ...] = ()

    @property
    def is_clean(self) -> bool:
        """No ambiguous identity and no source/store disagreement. The condition for trusting it."""
        return not self.conflicts and not self.refusals

    @property
    def changed_nothing(self) -> bool:
        """True when the store already held exactly this — what idempotence looks like."""
        return self.counts.total == 0


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def parse_equity_list(text: str) -> tuple[EquityListRow, ...]:
    """Parse `EQUITY_L.csv` into rows, or raise.

    What it does: checks the header against `EQUITY_LIST_COLUMNS` (stripping the source's own
    leading spaces), then parses every line, normalizing symbols and validating ISIN shape.
    What it assumes: the text is already decoded and is one whole file — a truncated download is
    a D1 problem and reaches here as a short row, which raises.
    What it never does: skip a bad line. A row this parser cannot read is a security that would
    silently vanish from the universe, so it raises with the line number instead.
    """
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = tuple(column.strip().upper() for column in next(reader))
    except StopIteration:
        raise IdentityParseError("EQUITY_L.csv is empty") from None
    if header != EQUITY_LIST_COLUMNS:
        raise IdentityParseError(
            f"EQUITY_L.csv header changed: expected {EQUITY_LIST_COLUMNS}, got {header}"
        )

    rows: list[EquityListRow] = []
    for line_number, raw in enumerate(reader, start=2):
        if not any(field.strip() for field in raw):
            continue
        if len(raw) != len(EQUITY_LIST_COLUMNS):
            raise IdentityParseError(
                f"EQUITY_L.csv line {line_number}: expected {len(EQUITY_LIST_COLUMNS)} fields, "
                f"got {len(raw)}"
            )
        symbol, name, series, listing, paid_up, lot, isin, face_value = (f.strip() for f in raw)
        where = f"EQUITY_L.csv line {line_number}"
        rows.append(
            EquityListRow(
                symbol=symbol.upper(),
                name=name,
                series=series.upper(),
                listing_date=_parse_nse_date(listing, where=where),
                isin=_validate_isin(isin, where=f"{where} (symbol {symbol})"),
                paid_up_value_inr=_parse_decimal(paid_up, where=where),
                face_value_inr=_parse_decimal(face_value, where=where),
                market_lot=_parse_int(lot, where=where),
            )
        )

    _log.info("identity.parsed.equity_list", rows=len(rows))
    return tuple(rows)


def parse_symbol_changes(text: str) -> tuple[SymbolChange, ...]:
    """Parse `symbolchange.csv` into renames, sorted oldest first.

    What it does: reads the four unlabelled fields NSE publishes — company, old symbol, new
    symbol, effective date — tolerating the header row NSE sometimes prefixes the file with.
    What it assumes: the effective date is the first date the *new* symbol is in force, which is
    how NSE's own bhavcopies behave across every rename in the golden set.
    What it never does: drop an unparseable line. The file is the only record that a rename ever
    happened; a line skipped here is a symbol that resolves to the wrong company forever.
    """
    reader = csv.reader(io.StringIO(text, newline=""))
    changes: list[SymbolChange] = []
    for line_number, raw in enumerate(reader, start=1):
        if not any(field.strip() for field in raw):
            continue
        if len(raw) != 4:
            raise IdentityParseError(
                f"symbolchange.csv line {line_number}: expected 4 fields, got {len(raw)}"
            )
        company, old_symbol, new_symbol, effective = (field.strip() for field in raw)
        if line_number == 1 and not _looks_like_nse_date(effective):
            # NSE ships the file both with and without an `SM_NAME,SM_KEY_SYMBOL,...` header.
            continue
        changes.append(
            SymbolChange(
                effective_date=_parse_nse_date(
                    effective, where=f"symbolchange.csv line {line_number}"
                ),
                old_symbol=old_symbol.upper(),
                new_symbol=new_symbol.upper(),
                company_name=company,
            )
        )

    _log.info("identity.parsed.symbol_changes", rows=len(changes))
    return tuple(sorted(changes))


# ── derivation ───────────────────────────────────────────────────────────────────────────────


def derive_master(
    rows: Iterable[EquityListRow],
    changes: Iterable[SymbolChange] = (),
    *,
    snapshot_date: date,
    exchange: Exchange = Exchange.NSE,
    equity_list_source: str = NSE_EQUITY_LIST_SOURCE,
    change_source: str = NSE_SYMBOL_CHANGE_SOURCE,
) -> DerivedMaster:
    """Turn one snapshot plus the rename history into securities, listings and symbol windows.

    What it does: emits one `Security` and one `Listing` per equity-list row, then walks each
    security's rename chain backwards to produce its full `symbol_history`. The current symbol's
    window is left open (`valid_to is None`); every earlier one ends the day before the rename
    that replaced it.
    What it assumes: the snapshot and the change file were fetched close enough together that
    every current symbol's latest rename is present in both. A rename published *after* the
    snapshot simply produces no window until the next snapshot, which is correct.
    What it never does: date a window from anything but the source files. Where the listing date
    contradicts the rename chain the window is clamped and reported (`DerivedMaster.clamped`),
    never widened to cover dates nothing in the files supports.
    """
    by_new_symbol: dict[str, list[SymbolChange]] = {}
    change_count = 0
    for change in sorted(changes):
        by_new_symbol.setdefault(change.new_symbol, []).append(change)
        change_count += 1

    securities: list[Security] = []
    listings: list[Listing] = []
    windows: list[SymbolWindow] = []
    clamped: list[ClampedWindow] = []
    applied: set[SymbolChange] = set()

    for row in rows:
        securities.append(
            Security(
                isin=row.isin,
                name=row.name,
                primary_exchange=exchange,
                status=ListingStatus.ACTIVE,
                first_seen_date=snapshot_date,
                last_seen_date=snapshot_date,
                face_value_inr=row.face_value_inr,
            )
        )
        listings.append(
            Listing(
                isin=row.isin,
                exchange=exchange,
                status=ListingStatus.ACTIVE,
                series=row.series or None,
                lot_size=row.market_lot,
                face_value_inr=row.face_value_inr,
                listing_date=row.listing_date,
            )
        )
        windows.extend(
            _walk_chain(
                row,
                by_new_symbol,
                exchange=exchange,
                equity_list_source=equity_list_source,
                change_source=change_source,
                clamped=clamped,
                applied=applied,
            )
        )

    # A rename whose new symbol is in no current chain describes a security this snapshot does
    # not list — a delisting, or one of the debt and mutual-fund symbols the file also carries.
    # It cannot become a `symbol_history` row (there is no ISIN to hang it on) and is counted
    # rather than dropped quietly.
    unlinked = change_count - len(applied)
    _log.info(
        "identity.derived",
        snapshot_date=snapshot_date.isoformat(),
        exchange=exchange.value,
        securities=len(securities),
        windows=len(windows),
        clamped=len(clamped),
        renames_applied=len(applied),
        unlinked_changes=unlinked,
    )
    return DerivedMaster(
        securities=tuple(securities),
        listings=tuple(listings),
        windows=tuple(windows),
        clamped=tuple(clamped),
        renames_applied=len(applied),
        unlinked_changes=unlinked,
    )


def _walk_chain(
    row: EquityListRow,
    by_new_symbol: Mapping[str, Sequence[SymbolChange]],
    *,
    exchange: Exchange,
    equity_list_source: str,
    change_source: str,
    clamped: list[ClampedWindow],
    applied: set[SymbolChange],
) -> list[SymbolWindow]:
    """Every symbol one ISIN has traded under, newest first, from the rename chain.

    The walk is backwards because that is the only direction the files support: the snapshot
    knows today's symbol, and each rename says what today's symbol used to be.
    """
    out: list[SymbolWindow] = []
    symbol = row.symbol
    end: date | None = None
    source = equity_list_source
    series: str | None = row.series or None
    seen: set[tuple[str, date]] = set()

    for _ in range(_MAX_CHAIN):
        applicable = [
            change
            for change in by_new_symbol.get(symbol, ())
            if (end is None or change.effective_date <= end)
            and (symbol, change.effective_date) not in seen
        ]
        if not applicable:
            start = row.listing_date
            if end is not None and start > end:
                clamped.append(
                    ClampedWindow(isin=row.isin, symbol=symbol, listing_date=start, clamped_to=end)
                )
                start = end
            out.append(
                SymbolWindow(
                    exchange=exchange,
                    symbol=symbol,
                    valid_from=start,
                    valid_to=end,
                    isin=row.isin,
                    series=series,
                    source=source,
                )
            )
            return out

        change = applicable[-1]  # the most recent rename into this symbol at or before `end`
        seen.add((symbol, change.effective_date))
        applied.add(change)
        out.append(
            SymbolWindow(
                exchange=exchange,
                symbol=symbol,
                valid_from=change.effective_date,
                valid_to=end,
                isin=row.isin,
                series=series,
                source=source,
            )
        )
        symbol = change.old_symbol
        end = change.effective_date - timedelta(days=1)
        # Only the current window's series is known; NSE publishes no history of series changes,
        # and asserting today's EQ for a 2011 window would be a fact we do not have.
        series = None
        source = change_source

    raise IdentityParseError(
        f"rename chain for ISIN {row.isin} ({row.symbol}) exceeded {_MAX_CHAIN} hops — "
        "symbolchange.csv contains a cycle"
    )


# ── ingest ───────────────────────────────────────────────────────────────────────────────────


def ingest_snapshot(
    conn: Connection,
    *,
    equity_list: str,
    symbol_changes: str = "",
    snapshot_date: date | None = None,
    clock: Clock | None = None,
    exchange: Exchange = Exchange.NSE,
) -> IdentityIngestReport:
    """Ingest one identity snapshot into the master. Does not commit.

    What it does: parses both files, derives the full symbol history, detects every ambiguity
    against what is *already* stored as well as within the snapshot, writes the master rows, and
    queues each conflict into `identity_reconciliation`.
    What it assumes: one writer at a time, and a caller that owns the transaction — commit is the
    caller's call, exactly as `dataplatform.store.db.connection` intends.
    What it never does: raise on an ambiguous symbol. The conflict row and the exception would
    then be fighting over the same transaction, and one bad symbol would cost the other 2,396
    securities their update. Ambiguity is recorded here and raised at the point it actually
    matters — `IdentityMaster.resolve`, where a caller is about to act on the wrong ISIN. The
    report says `is_clean` and the CLI exits non-zero on it.
    """
    clock = SystemClock() if clock is None else clock
    snapshot_date = clock.today() if snapshot_date is None else snapshot_date

    derived = derive_master(
        parse_equity_list(equity_list),
        parse_symbol_changes(symbol_changes) if symbol_changes.strip() else (),
        snapshot_date=snapshot_date,
        exchange=exchange,
    )

    store = IdentityStore(conn, clock=clock)
    stored = store.load_windows()
    plan = plan_history(stored, derived.windows)
    # Judged on what the store will hold once the plan lands, not on the union of before and
    # after — see `HistoryPlan.applied_to`.
    conflicts = detect_conflicts(
        plan.applied_to(stored), source=NSE_EQUITY_LIST_SOURCE, detected_by=DetectedBy.INGEST
    )

    securities = store.write_securities(derived.securities)
    listings = store.write_listings(derived.listings)
    inserted, closed = store.apply_history(plan)
    queued = sum(1 for conflict in conflicts if store.record(conflict))

    counts = WriteCounts(
        securities=securities,
        listings=listings,
        windows_inserted=inserted,
        windows_closed=closed,
        conflicts_queued=queued,
    )
    report = IdentityIngestReport(
        snapshot_date=snapshot_date,
        exchange=exchange,
        securities_seen=len(derived.securities),
        renames_applied=derived.renames_applied,
        unlinked_changes=derived.unlinked_changes,
        counts=counts,
        clamped=derived.clamped,
        conflicts=conflicts,
        refusals=plan.refusals,
    )

    _log.info(
        "identity.ingested",
        source=NSE_EQUITY_LIST_SOURCE,
        snapshot_date=snapshot_date.isoformat(),
        exchange=exchange.value,
        securities=securities,
        listings=listings,
        windows_inserted=inserted,
        windows_closed=closed,
        conflicts=len(conflicts),
        refusals=len(plan.refusals),
        clamped=len(derived.clamped),
    )
    for refusal in plan.refusals:
        _log.warning(
            "identity.history.refused",
            isin=refusal.stored.isin,
            symbol=refusal.stored.symbol,
            reason=refusal.reason,
        )
    return report


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _parse_nse_date(text: str, *, where: str) -> date:
    """`06-OCT-2008` → `date(2008, 10, 6)`. Locale-independent by construction."""
    parts = text.strip().split("-")
    if len(parts) != 3:
        raise IdentityParseError(f"{where}: {text!r} is not a DD-MON-YYYY date")
    day, month, year = parts
    try:
        return date(int(year), _MONTHS[month.strip().upper()], int(day))
    except (KeyError, ValueError) as error:
        raise IdentityParseError(f"{where}: {text!r} is not a DD-MON-YYYY date ({error})") from None


def _looks_like_nse_date(text: str) -> bool:
    """Whether a field could be a `DD-MON-YYYY` date — used only to spot a header row."""
    parts = text.strip().split("-")
    return len(parts) == 3 and parts[1].strip().upper() in _MONTHS


def _validate_isin(text: str, *, where: str) -> str:
    """The ISIN, upper-cased, or a loud failure naming the row it came from."""
    isin = text.strip().upper()
    if not _ISIN.match(isin):
        raise IdentityParseError(f"{where}: {text!r} is not an ISIN")
    return isin


def _parse_decimal(text: str, *, where: str) -> Decimal | None:
    """A money field as `Decimal` (never float — CLAUDE.md), or `None` when the source is blank."""
    if not text.strip():
        return None
    try:
        return Decimal(text.strip())
    except InvalidOperation:
        raise IdentityParseError(f"{where}: {text!r} is not a number") from None


def _parse_int(text: str, *, where: str) -> int | None:
    """A count field as `int`, or `None` when the source is blank."""
    if not text.strip():
        return None
    try:
        return int(text.strip())
    except ValueError:
        raise IdentityParseError(f"{where}: {text!r} is not an integer") from None


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """Ingest a snapshot from files on disk. Never fetches — D1 owns the network.

    Exits non-zero when the ingest was not clean, so a scheduled refresh that met an ambiguous
    symbol fails visibly instead of leaving a queue row nobody looks at.
    """
    parser = argparse.ArgumentParser(prog="identity.ingest", description="Ingest the NSE master.")
    parser.add_argument("--equity-list", type=Path, required=True, help="EQUITY_L.csv")
    parser.add_argument("--symbol-changes", type=Path, help="symbolchange.csv")
    parser.add_argument(
        "--snapshot-date",
        type=date.fromisoformat,
        help="the date the snapshot describes (default: today, from the injected clock)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="parse, derive and report; roll back at the end"
    )
    args = parser.parse_args(argv)

    changes = "" if args.symbol_changes is None else args.symbol_changes.read_text(encoding="utf-8")
    with connection() as conn:
        try:
            report = ingest_snapshot(
                conn,
                equity_list=args.equity_list.read_text(encoding="utf-8"),
                symbol_changes=changes,
                snapshot_date=args.snapshot_date,
            )
        except IdentityError as error:
            conn.rollback()
            print(f"identity ingest failed: {error}", file=sys.stderr)
            return 2
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

    print(
        f"identity master {'(dry run) ' if args.dry_run else ''}"
        f"{report.snapshot_date.isoformat()} {report.exchange.value}: "
        f"{report.securities_seen} securities, {report.counts.securities} changed, "
        f"{report.counts.windows_inserted} windows inserted, "
        f"{report.counts.windows_closed} closed"
    )
    for chain in report.clamped:
        print(
            f"  clamped: {chain.symbol} ({chain.isin}) listed {chain.listing_date} > "
            f"{chain.clamped_to}"
        )
    for refusal in report.refusals:
        print(f"  refused: {refusal.reason}", file=sys.stderr)
    for conflict in report.conflicts:
        print(f"  AMBIGUOUS: {conflict}", file=sys.stderr)
    return 0 if report.is_clean else 1


if __name__ == "__main__":
    sys.exit(main())
