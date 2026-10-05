"""D2: ISIN lineage for exchange-traded fund units — the edge a unit split leaves behind.

`lineage.derive_edges` stitches equity reissues by issuer code, and deliberately refuses `INF`:
an AMC issues dozens of schemes under one issuer code, so grouping by it splices unrelated funds.
A fund's unit split still retires its ISIN, though. GOLDBEES traded as INF732E01102 to 2019-12-19
and as INF204KB17I5 from 2019-12-20; UTI's Sensex Next 50 ETF as INF789F1AHR6 to 2021-02-18 and
INF789F1AUU3 from 2021-02-19. Without an edge the survivor's history starts at the split and the
retired ISIN's L2 partition is a second, orphaned copy of the same units.

**What it does.** Groups by the one thing a fund keeps across the split — its NSE trading symbol
and series, as the bhavcopy printed them on each session — and proposes one candidate per adjacent
pair of ISINs under that symbol. A candidate becomes an edge only when *every* one of these holds:

* **Consecutive sessions.** The old ISIN's last session is the NSE session immediately before the
  new ISIN's first (`gap_sessions == 0`). A unit that stopped printing for a day is not proven to
  be the same unit when it comes back under another ISIN.
* **The ISINs end and begin there.** The old ISIN prints on no NSE session after the switch, the
  new one on none before it, under any symbol. Otherwise the two lived concurrently.
* **One to one.** No other candidate claims the same predecessor or the same successor.
* **A unit-basis event on the switch.** A SPLIT or BONUS for that symbol and series with its
  ex-date on the old ISIN's last two sessions or the new ISIN's first, read from NSE's own
  book-closure file (`Bc<ddmmyy>.csv` inside the daily `PR` bundle in L0) or, keyed by the old
  ISIN, from the L0 corporate-action feed. NSE dated these three ways, all measured over L1:
  ex-date on the old ISIN's last session (2014-2022, most of them), on the new ISIN's first
  (2023 onward), and — once, on 2021-02-17, for five HDFC and UTI ETFs — on the old ISIN's
  second-to-last session, the old ISIN printing two post-split sessions before the switch.

**What it assumes.** L1 `prices_raw` holds the NSE bhavcopy rows (the switch is read from the ISIN
each session printed) and L0 holds the `nse_pr_bundle` archives for the switch dates.

**What it never does.** Accept an edge without the corroborating event — price continuity alone
also fits an AMC transfer or a scheme merger, and those are not a split. Resolve a symbol to an
ISIN through today's mapping: a `Bc` row's symbol is matched to the ISIN the bhavcopy carried
for that symbol on the switch dates, which is the only ISIN it could have meant. Fetch anything.
"""

from __future__ import annotations

import argparse
import re
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

import duckdb

from dataplatform.clock import Clock, SystemClock
from dataplatform.corpactions.taxonomy import ActionType
from dataplatform.identity.lineage import LineageEdge, read_corroboration
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle.bc import BcRow, parse_bc_bundle
from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID, MemberKind, PrBundle
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Store
from dataplatform.store.l2 import open_connection, register_raw_view

__all__ = [
    "FundEdgeVerdict",
    "FundIsinSwitch",
    "FundSurvey",
    "RejectReason",
    "UnitEvent",
    "derive_fund_edges",
    "evaluate_switches",
    "index_unit_events",
    "read_bc_unit_events",
    "read_fund_switches",
    "survey",
    "unit_event_type",
]

_LOG = get_logger(__name__)

#: `INF` is a mutual-fund unit — every ETF NSE lists. Equity reissues are `lineage.derive_edges`'.
_FUND_PREFIX: Final = "INF"

#: Only the NSE bhavcopy prints the ISIN as of each session (see `lineage.read_equity_spans`).
_SPAN_EXCHANGE: Final = "NSE"

#: How far before the switch a `Bc` broadcast is looked for. NSE announces a unit split about a
#: week ahead and re-broadcasts it daily to the ex-date; a month is several broadcasts of margin.
_BC_LOOKBACK: Final = timedelta(days=30)

#: A unit sub-division as the `Bc` file abbreviates it: `FV SPLIT RS.10 TO RS.7`, `FV SPLT FR
#: RS100 TO RS10`, `FVSPLTFRM 761.25TO76.125`, `SUB-DIVISION`. `parse_terms.classify` reads the
#: feed's prose and recognises none of the abbreviated forms, so this narrow pattern exists here.
_SPLIT_RE: Final = re.compile(r"F\.?\s*V\.?\s*SPL|SPLIT|SPLT|SUB-?\s*DIVISION")

#: A bonus issue: `BONUS 1:1`, `BON-1:25`. Matched as a word so it is never part of another one.
_BONUS_RE: Final = re.compile(r"\bBON(?:US)?\b")


class RejectReason(StrEnum):
    """Why a candidate switch is not an edge — each a different condition, so each is counted."""

    #: The old ISIN's last session is not the session right before the new ISIN's first.
    SESSION_GAP = "session_gap"
    #: The old ISIN printed again after the switch, or the new one before it, under any symbol.
    NOT_SEQUENTIAL = "not_sequential"
    #: Another candidate claims the same predecessor or the same successor.
    NOT_ONE_TO_ONE = "not_one_to_one"
    #: A book-closure event sits on the switch, but it is not a SPLIT or a BONUS.
    EVENT_NOT_A_UNIT_REBASE = "event_not_a_unit_rebase"
    #: No event of any kind on the switch for that symbol and series.
    NO_CORROBORATING_EVENT = "no_corroborating_event"


@dataclass(frozen=True, slots=True)
class FundIsinSwitch:
    """One NSE symbol and series moving from one INF ISIN to the next, as L1 printed it."""

    symbol: str
    series: str
    predecessor_isin: str
    successor_isin: str
    #: The old ISIN's last session under this symbol and series.
    predecessor_last: date
    #: The new ISIN's first session under this symbol and series.
    successor_first: date
    #: The old ISIN's last NSE session under any symbol — later than `predecessor_last` means the
    #: old ISIN went on trading after the switch.
    predecessor_last_anywhere: date
    #: The new ISIN's first NSE session under any symbol.
    successor_first_anywhere: date


@dataclass(frozen=True, slots=True)
class UnitEvent:
    """One `Bc` row on a fund symbol: what NSE said would happen to the units, and when."""

    symbol: str
    series: str
    ex_date: date
    purpose: str
    #: SPLIT or BONUS when the purpose names exactly that; None for any other event.
    action: ActionType | None
    #: The earliest bundle that broadcast it, and that bundle's L0 key.
    knowable_date: date
    l0_key: str | None


@dataclass(frozen=True, slots=True)
class FundEdgeVerdict:
    """A candidate switch, and either the edge it became or why it did not."""

    switch: FundIsinSwitch
    gap_sessions: int
    edge: LineageEdge | None
    reason: RejectReason | None
    #: The event that corroborated the edge, or the non-unit event a rejection found.
    event: UnitEvent | None
    #: `"bc"` or `"feed"` for an accepted edge; None otherwise.
    evidence_source: str | None = None

    @property
    def accepted(self) -> bool:
        """Whether this candidate became an edge."""
        return self.edge is not None


def unit_event_type(purpose: str) -> ActionType | None:
    """SPLIT or BONUS when a `Bc` purpose names exactly one of them, SPLIT when both, else None.

    A compound `BON-1:25/SPLIT RS10TORS.5` re-bases the units twice; the split is what retires an
    ISIN, as in `lineage.corroborating_type`. `CHANGE IN ATTRIBUTE`, a dividend, an AGM or an
    empty string is None — none of them changes the unit count.
    """
    text = purpose.upper()
    if _SPLIT_RE.search(text):
        return ActionType.SPLIT
    if _BONUS_RE.search(text):
        return ActionType.BONUS
    return None


def read_fund_switches(
    *, con: duckdb.DuckDBPyConnection | None = None, data_root: Path | None = None
) -> tuple[tuple[FundIsinSwitch, ...], tuple[date, ...]]:
    """Every adjacent INF ISIN pair under one NSE symbol and series in L1, and the NSE sessions.

    Within each `(symbol, series)` the ISINs it carried are ordered by first session, and each
    consecutive pair whose spans do not overlap is a candidate — nothing more is decided here.
    Sessions are every distinct NSE trade date in L1, the calendar `gap_sessions` is counted on.
    """
    owns = con is None
    con = open_connection() if con is None else con
    try:
        register_raw_view(con, view="prices_raw", data_root=data_root)
        rows = con.execute(
            """
            WITH nse AS (
                SELECT isin, symbol, series, trade_date FROM prices_raw
                 WHERE exchange = $exchange AND substr(isin, 1, 3) = $prefix
            ),
            anywhere AS (
                SELECT isin, min(trade_date) AS first_any, max(trade_date) AS last_any
                  FROM nse GROUP BY isin
            ),
            spans AS (
                SELECT symbol, series, isin, min(trade_date) AS first_date,
                       max(trade_date) AS last_date
                  FROM nse GROUP BY symbol, series, isin
            ),
            ordered AS (
                SELECT *, lead(isin) OVER w AS next_isin, lead(first_date) OVER w AS next_first
                  FROM spans
                WINDOW w AS (PARTITION BY symbol, series ORDER BY first_date, isin)
            )
            SELECT o.symbol, o.series, o.isin, o.next_isin, o.last_date, o.next_first,
                   a.last_any, b.first_any
              FROM ordered o
              JOIN anywhere a ON a.isin = o.isin
              JOIN anywhere b ON b.isin = o.next_isin
             WHERE o.next_isin IS NOT NULL AND o.next_first > o.last_date
             ORDER BY o.next_first, o.symbol, o.series
            """,
            {"exchange": _SPAN_EXCHANGE, "prefix": _FUND_PREFIX},
        ).fetchall()
        sessions = con.execute(
            "SELECT DISTINCT trade_date FROM prices_raw WHERE exchange = $exchange "
            "ORDER BY trade_date",
            {"exchange": _SPAN_EXCHANGE},
        ).fetchall()
    finally:
        if owns:
            con.close()
    switches = tuple(FundIsinSwitch(*r) for r in rows)
    _LOG.info("fund_lineage.switches_read", switches=len(switches), sessions=len(sessions))
    return switches, tuple(s[0] for s in sessions)


def index_unit_events(rows: Iterable[BcRow]) -> Mapping[tuple[str, str, date], UnitEvent]:
    """Key `Bc` rows by `(symbol, series, ex_date)`, keeping each event's earliest broadcast.

    NSE re-broadcasts one event on every session up to its ex-date, so the same row arrives many
    times; the earliest is when it became knowable. A row with no ex-date (a debt redemption) is
    not an event on a session and is dropped. When two different purposes share a key, a unit
    re-basing one wins: a dividend on the same ex-date does not undo a split.
    """
    found: dict[tuple[str, str, date], UnitEvent] = {}
    for row in rows:
        if row.ex_date is None:
            continue
        key = (row.symbol, row.series, row.ex_date)
        event = UnitEvent(
            symbol=row.symbol,
            series=row.series,
            ex_date=row.ex_date,
            purpose=row.purpose,
            action=unit_event_type(row.purpose),
            knowable_date=row.knowable_date,
            l0_key=row.l0_key,
        )
        held = found.get(key)
        if held is None or _prefer(event, held):
            found[key] = event
    return found


def _prefer(new: UnitEvent, held: UnitEvent) -> bool:
    """Whether `new` should replace `held` for one key: a unit event first, then earliest seen."""
    if (new.action is None) != (held.action is None):
        return new.action is not None
    return new.knowable_date < held.knowable_date


def read_bc_unit_events(
    switches: Iterable[FundIsinSwitch],
    *,
    clock: Clock | None = None,
    data_root: Path | None = None,
) -> tuple[Mapping[tuple[str, str, date], UnitEvent], tuple[str, ...]]:
    """The `Bc` events on the switches' symbols, read from the L0 `PR` bundles around each switch.

    Returns `(events, unreadable)`. Only bundles dated in a switch's window are opened — from
    `_BC_LOOKBACK` before the old ISIN's last session to the new ISIN's first — and only rows for
    a switching symbol are kept. A bundle whose `Bc` member does not parse is named in
    `unreadable` and logged, not raised: the same event is re-broadcast on every session before
    its ex-date, so one malformed file costs nothing that its neighbours do not carry, and a
    switch whose every broadcast is unreadable is rejected for want of evidence, never accepted.
    A checksum failure is not caught — bytes that changed under L0 are a defect to stop on.
    """
    store = L0Store(clock=SystemClock() if clock is None else clock, data_root=data_root)
    wanted = tuple(switches)
    symbols = {s.symbol for s in wanted}
    days: set[date] = set()
    for s in wanted:
        day = s.predecessor_last - _BC_LOOKBACK
        while day <= s.successor_first:
            days.add(day)
            day += timedelta(days=1)

    rows: list[BcRow] = []
    unreadable: list[str] = []
    opened = 0
    if days:
        for ref in store.iter_refs(PR_BUNDLE_SOURCE_ID, start=min(days), end=max(days)):
            if ref.logical_date not in days:
                continue
            try:
                with PrBundle.from_l0(store, ref) as bundle:
                    if not bundle.has(MemberKind.BC):
                        continue
                    opened += 1
                    rows.extend(
                        r for r in parse_bc_bundle(bundle, l0_key=ref.key) if r.symbol in symbols
                    )
            except ParseError as exc:
                unreadable.append(ref.key)
                _LOG.warning(
                    "fund_lineage.bc_unreadable", l0_key=ref.key, error=str(exc), state="SKIPPED"
                )
    events = index_unit_events(rows)
    _LOG.info(
        "fund_lineage.bc_read",
        bundles=opened,
        unreadable=len(unreadable),
        rows=len(rows),
        events=len(events),
    )
    return events, tuple(unreadable)


def evaluate_switches(
    switches: Iterable[FundIsinSwitch],
    sessions: Sequence[date],
    bc_events: Mapping[tuple[str, str, date], UnitEvent],
    feed: Mapping[tuple[str, date], ActionType],
) -> tuple[FundEdgeVerdict, ...]:
    """Decide every candidate — a pure function, no I/O. See the module docstring for the rules.

    The conditions are checked in a fixed order and the first that fails is the reason, so a
    rejected candidate always names the most basic thing wrong with it. `feed` is the ISIN-keyed
    L0 corporate-action map (`lineage.read_corroboration`), consulted on the old ISIN.
    """
    rows = tuple(switches)
    by_predecessor = Counter(s.predecessor_isin for s in rows)
    by_successor = Counter(s.successor_isin for s in rows)
    verdicts: list[FundEdgeVerdict] = []
    for s in rows:
        gap = _sessions_between(sessions, s.predecessor_last, s.successor_first)
        verdicts.append(_verdict(s, gap, sessions, bc_events, feed, by_predecessor, by_successor))
    _LOG.info(
        "fund_lineage.evaluated",
        candidates=len(verdicts),
        accepted=sum(1 for v in verdicts if v.accepted),
        **{f"rejected_{r.value}": sum(1 for v in verdicts if v.reason is r) for r in RejectReason},
    )
    return tuple(verdicts)


def _verdict(
    s: FundIsinSwitch,
    gap: int,
    sessions: Sequence[date],
    bc_events: Mapping[tuple[str, str, date], UnitEvent],
    feed: Mapping[tuple[str, date], ActionType],
    by_predecessor: Counter[str],
    by_successor: Counter[str],
) -> FundEdgeVerdict:
    """One candidate's verdict; the first failing condition is the reason."""

    def reject(reason: RejectReason, event: UnitEvent | None = None) -> FundEdgeVerdict:
        return FundEdgeVerdict(switch=s, gap_sessions=gap, edge=None, reason=reason, event=event)

    if gap != 0:
        return reject(RejectReason.SESSION_GAP)
    if (
        s.predecessor_last_anywhere != s.predecessor_last
        or s.successor_first_anywhere != s.successor_first
    ):
        return reject(RejectReason.NOT_SEQUENTIAL)
    if by_predecessor[s.predecessor_isin] > 1 or by_successor[s.successor_isin] > 1:
        return reject(RejectReason.NOT_ONE_TO_ONE)

    window = _event_window(sessions, s)
    events = [e for d in window if (e := bc_events.get((s.symbol, s.series, d))) is not None]
    unit = next((e for e in events if e.action is not None), None)
    action: ActionType | None = None if unit is None else unit.action
    source = None if unit is None else "bc"
    if action is None:
        action = next((a for d in window if (a := feed.get((s.predecessor_isin, d)))), None)
        source = None if action is None else "feed"
    if action is None:
        if events:
            return reject(RejectReason.EVENT_NOT_A_UNIT_REBASE, events[0])
        return reject(RejectReason.NO_CORROBORATING_EVENT)

    edge = LineageEdge(
        predecessor_isin=s.predecessor_isin,
        successor_isin=s.successor_isin,
        effective_date=s.successor_first,
        gap_sessions=0,
        symbol_at_change=s.symbol,
        corroborating_action=action,
    )
    return FundEdgeVerdict(
        switch=s, gap_sessions=0, edge=edge, reason=None, event=unit, evidence_source=source
    )


def _event_window(sessions: Sequence[date], s: FundIsinSwitch) -> tuple[date, ...]:
    """The old ISIN's last two NSE sessions and the new ISIN's first — the only ex-dates accepted.

    Only called once `gap_sessions == 0`, so these are three consecutive sessions.
    """
    i = bisect_left(sessions, s.predecessor_last)
    before = (sessions[i - 1],) if 0 < i < len(sessions) else ()
    return (*before, s.predecessor_last, s.successor_first)


def _sessions_between(sessions: Sequence[date], after: date, before: date) -> int:
    """NSE sessions strictly between two dates, from the L1 session calendar."""
    return max(0, bisect_left(sessions, before) - bisect_right(sessions, after))


@dataclass(frozen=True, slots=True)
class FundSurvey:
    """Every candidate's verdict, and the `PR` bundles that could not be read for evidence."""

    verdicts: tuple[FundEdgeVerdict, ...]
    unreadable_bundles: tuple[str, ...]

    @property
    def edges(self) -> tuple[LineageEdge, ...]:
        """The accepted edges, by effective date."""
        return tuple(v.edge for v in self.verdicts if v.edge is not None)


def survey(*, clock: Clock | None = None, data_root: Path | None = None) -> FundSurvey:
    """Read L1 and L0 and judge every INF ISIN switch in the lake. Writes nothing."""
    switches, sessions = read_fund_switches(data_root=data_root)
    bc, unreadable = read_bc_unit_events(switches, clock=clock, data_root=data_root)
    feed = read_corroboration(data_root=data_root)
    return FundSurvey(
        verdicts=evaluate_switches(switches, sessions, bc, feed), unreadable_bundles=unreadable
    )


def derive_fund_edges(
    *, clock: Clock | None = None, data_root: Path | None = None
) -> tuple[LineageEdge, ...]:
    """The accepted fund edges only — what the lineage rebuild writes beside the equity ones."""
    return survey(clock=clock, data_root=data_root).edges


def main(argv: Sequence[str] | None = None) -> int:
    """Print every candidate switch with its verdict and evidence. Read-only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted-only", action="store_true", help="list accepted edges only")
    args = parser.parse_args(argv)
    result = survey()
    for v in result.verdicts:
        if args.accepted_only and not v.accepted:
            continue
        s = v.switch
        verdict = "ACCEPT" if v.accepted else f"REJECT {v.reason}"
        evidence = "-"
        if v.event is not None:
            evidence = f"{v.event.ex_date} {v.event.purpose!r} ({v.event.l0_key})"
        elif v.evidence_source == "feed" and v.edge is not None:
            evidence = f"feed {v.edge.corroborating_action}"
        print(
            f"{s.symbol:<12} {s.series:<3} {s.predecessor_isin} -> {s.successor_isin} "
            f"{s.predecessor_last} | {s.successor_first} gap={v.gap_sessions} "
            f"{verdict:<32} {evidence}"
        )
    counts = Counter("accepted" if v.accepted else str(v.reason) for v in result.verdicts)
    for key, count in sorted(counts.items()):
        print(f"  {key:<28} {count}")
    print(f"  unreadable_bundles           {len(result.unreadable_bundles)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
