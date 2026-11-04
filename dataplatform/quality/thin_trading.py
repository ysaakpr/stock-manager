"""D7: the thinly-traded label — how often a listed equity actually trades, per ISIN and venue.

Some securities go weeks or months without a single trade: a trade-to-trade stint, an exchange
suspension, or simply nobody dealing. The owner's ruling (2026-11-01, backlog M13.8) is that such
names are *marked*, and whether to consider them is the strategy's decision. So this module
computes, once a day after the EOD pipeline, for every (ISIN, venue) that traded in the trailing
window:

* the share of sessions it traded in the trailing `WINDOW_SESSIONS` (~one year of sessions);
* its longest no-trade gap inside that window, in sessions and in calendar days;
* the series of its latest bar, whether that series is trade-to-trade, and the identity master's
  listing status for the venue.

A name is **thinly traded** when its traded ratio is below `THIN_RATIO_BELOW` or its longest gap
is at least `THIN_GAP_SESSIONS` sessions (the thresholds and the distribution behind them are in
`ops/runbooks/thin-trading.md`).

**A capture hole is never a no-trade session.** A session counts as observed for a venue only when
our own capture of that venue's bhavcopy is `PUBLISHED` in `sync_state` *and* L1 holds rows for the
venue that day, and — per ISIN — when L1 did not refuse the ISIN's row into `prices_raw_quarantine`
and, on NSE, when the exchange's own PR bundle (`pr_security_marks`, a second file from the same
exchange) does not show the ISIN trading on a session our bhavcopy lacks it. Anything else is a
hole: it is excluded from the ratio's denominator and it *breaks* a gap rather than extending it,
because a session we did not see cannot be evidence that nobody traded. Holes are counted per row,
so a gap that is our fault is reported, never labelled.

What this never does: quarantine, exclude, or change a price, a factor or a total return. It is a
label only — information for the query layer and strategies (`dataplatform.query.thin_trading`)
and Ops (`/status/thin-trading`). It is deliberately not part of anything the M17 managers read.
"""

from __future__ import annotations

import argparse
import bisect
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Final, Protocol

import duckdb

from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import TradingCalendar, trading_calendar
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection
from dataplatform.store.paths import Layer, layer_root

__all__ = [
    "BSE_REGULAR_SERIES",
    "BSE_T2T_SERIES",
    "MIN_OBSERVED_SESSIONS",
    "NSE_REGULAR_SERIES",
    "NSE_T2T_SERIES",
    "THIN_GAP_SESSIONS",
    "THIN_RATIO_BELOW",
    "VENUE_CAPTURE_SOURCES",
    "WINDOW_SESSIONS",
    "IsinActivity",
    "ThinTradingError",
    "ThinTradingLabel",
    "ThinTradingRun",
    "VenueActivity",
    "label_isin",
    "latest_published_session",
    "resolve_successors",
    "run_thin_trading",
    "run_thin_trading_job",
    "trailing_sessions",
    "write_labels",
]

log = get_logger(__name__)

#: The trailing window: about one year of NSE sessions (~248 a year), long enough that a quarterly
#: trade-to-trade stint shows and short enough that a name which has recovered stops being marked.
WINDOW_SESSIONS: Final = 250

#: Thinly traded when it traded on fewer than 80% of the sessions observed. The live distribution
#: (label of 2026-11-03, `ops/runbooks/thin-trading.md`) has no cliff: 80% of NSE-listed names trade
#: on at least 99.6% of sessions and the rest fall away gradually. 0.8 — one session in five with no
#: trade at all — marks 301 of 3,643 NSE-listed names (8%) and 843 of 2,503 BSE-only ones (34%).
THIN_RATIO_BELOW: Final = Decimal("0.80")

#: ...or when one no-trade gap ran 20 sessions (four trading weeks) or more: where short gaps
#: (holidays, a quiet fortnight) end and verified suspensions, trade-to-trade stints and months of
#: no trades begin. It adds only 18 names the ratio misses — the owner's shape: a name that trades
#: normally except for one long silence.
THIN_GAP_SESSIONS: Final = 20

#: Below this many observed sessions (a name listed in the last month) there is no evidence either
#: way, so the row is written with `thin = False` and `insufficient_history = True`.
MIN_OBSERVED_SESSIONS: Final = 20

#: The NSE equity series. Regular rolling settlement (EQ, and SM for the SME platform) and the
#: trade-to-trade series a name is moved into for surveillance or non-compliance (BE, BZ; ST, SZ
#: on SME). Debt, InvIT/REIT, rights-entitlement and auxiliary series (BL block, IL, ...) are not
#: equity trading and never count as a traded session.
NSE_REGULAR_SERIES: Final = frozenset({"EQ", "SM"})
NSE_T2T_SERIES: Final = frozenset({"BE", "BZ", "ST", "SZ"})

#: The BSE equity groups. BSE files its group letter in the series column: A, B, X, M (SME), MS,
#: P (permitted from another exchange) trade in rolling settlement; T, XT, MT, TS and the Z/ZP
#: non-compliance groups are trade-to-trade. F, G (debt), E (ETFs), IF (InvITs) and R (rights
#: entitlements) are not equity listings.
BSE_REGULAR_SERIES: Final = frozenset({"A", "B", "X", "M", "MS", "P"})
BSE_T2T_SERIES: Final = frozenset({"T", "XT", "MT", "TS", "Z", "ZP"})

_EQUITY_SERIES: Final[Mapping[str, frozenset[str]]] = {
    "NSE": NSE_REGULAR_SERIES | NSE_T2T_SERIES,
    "BSE": BSE_REGULAR_SERIES | BSE_T2T_SERIES,
}
_T2T_SERIES: Final[Mapping[str, frozenset[str]]] = {"NSE": NSE_T2T_SERIES, "BSE": BSE_T2T_SERIES}

#: The `sync_state` sources whose PUBLISHED row means we captured that venue's whole session.
VENUE_CAPTURE_SOURCES: Final[Mapping[str, tuple[str, ...]]] = {
    "NSE": ("nse_bhavcopy", "nse_bhavcopy_pre_isin"),
    "BSE": ("bse_bhavcopy", "bse_bhavcopy_legacy"),
}

_RATIO_QUANTUM: Final = Decimal("0.0001")

#: ISINs of Indian companies' securities; only these carry issuer + security type in characters
#: 4-9, which is what the identity-seam check compares.
_COMPANY_SHARE_PREFIX: Final = "INE"


class ThinTradingError(RuntimeError):
    """No label could be computed: no published session, or the calendar does not cover it."""


@dataclass(frozen=True, slots=True)
class VenueActivity:
    """What the lake says about one ISIN on one venue.

    `traded` is every session in the window with an equity bar; `first_bar` is the earliest bar the
    lake holds for the pair at all (the listing proxy: before it the name was not listed there, so
    a session then is no no-trade session); `holes` are this ISIN's own capture holes on the venue
    (a row L1 refused, a trade the PR bundle saw and our bhavcopy did not); `last_series` is the
    series of the latest bar.
    """

    exchange: str
    traded: frozenset[date]
    first_bar: date
    last_series: str
    holes: frozenset[date] = frozenset()

    @property
    def trade_to_trade(self) -> bool:
        """Whether the latest bar was in a trade-to-trade series of this venue."""
        return self.last_series in _T2T_SERIES.get(self.exchange, frozenset())


@dataclass(frozen=True, slots=True)
class IsinActivity:
    """One ISIN's venues — the input `label_isin` decides on."""

    isin: str
    venues: tuple[VenueActivity, ...]
    listing_status: str | None = None
    #: Sessions on which another ISIN of the same issuer and security type (the ISIN's first nine
    #: characters, e.g. ``INE05BN01``) traded — the evidence for `ThinTradingLabel.identity_seam`.
    sibling_traded: frozenset[date] = frozenset()


@dataclass(frozen=True, slots=True)
class ThinTradingLabel:
    """One (as-of session, ISIN) label. Information only — it changes no price.

    The headline fields count a session as traded when the ISIN traded on *any* venue it is listed
    on: an investor can deal on either exchange, and a name NSE stopped carrying while BSE traded
    it daily is not illiquid. `nse_traded_ratio` / `bse_traded_ratio` give each venue alone (None
    where the ISIN has no bar on it); `nse_series` / `bse_series` are each venue's latest series.
    """

    as_of: date
    isin: str
    window_start: date
    sessions_observed: int
    sessions_traded: int
    capture_holes: int
    traded_ratio: Decimal
    nse_traded_ratio: Decimal | None
    bse_traded_ratio: Decimal | None
    longest_gap_sessions: int
    longest_gap_days: int
    longest_gap_from: date | None
    longest_gap_to: date | None
    current_gap_sessions: int
    last_traded: date | None
    nse_series: str | None
    bse_series: str | None
    trade_to_trade: bool
    listing_status: str | None
    insufficient_history: bool
    identity_seam: bool
    thin: bool
    reasons: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        """A JSON-safe dict (dates ISO, ratios as strings so no float ever carries them)."""
        return {
            "as_of": self.as_of.isoformat(),
            "isin": self.isin,
            "window_start": self.window_start.isoformat(),
            "sessions_observed": self.sessions_observed,
            "sessions_traded": self.sessions_traded,
            "capture_holes": self.capture_holes,
            "traded_ratio": str(self.traded_ratio),
            "nse_traded_ratio": _str(self.nse_traded_ratio),
            "bse_traded_ratio": _str(self.bse_traded_ratio),
            "longest_gap_sessions": self.longest_gap_sessions,
            "longest_gap_days": self.longest_gap_days,
            "longest_gap_from": _iso(self.longest_gap_from),
            "longest_gap_to": _iso(self.longest_gap_to),
            "current_gap_sessions": self.current_gap_sessions,
            "last_traded": _iso(self.last_traded),
            "nse_series": self.nse_series,
            "bse_series": self.bse_series,
            "trade_to_trade": self.trade_to_trade,
            "listing_status": self.listing_status,
            "insufficient_history": self.insufficient_history,
            "identity_seam": self.identity_seam,
            "thin": self.thin,
            "reasons": list(self.reasons),
        }


_Gap = tuple[int, int, date | None, date | None]


def _gap_order(gap: _Gap) -> tuple[int, int]:
    return (gap[0], gap[1])


def _iso(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _ratio(traded: int, observed: int) -> Decimal:
    if not observed:
        return Decimal(0)
    return (Decimal(traded) / Decimal(observed)).quantize(_RATIO_QUANTUM, ROUND_HALF_EVEN)


def _identity_seam(
    longest: _Gap,
    counted: Sequence[date],
    own_traded: frozenset[date],
    sibling_traded: frozenset[date],
) -> bool:
    """Whether the longest gap is a reissue the lineage table has no edge for.

    The shape: another ISIN of the same issuer and security type traded inside the gap, and the two
    never traded on the same session in the window — the old ISIN stops, the new one carries on
    (BSE's scrip-code era having filed the scrip's history under one of them). Two share classes
    that trade side by side (a DVR beside the ordinary share) overlap, so they are no seam. It is
    our identity seam, not a stretch nobody traded: reported (`identity_seam`), never labelled.
    """
    length, _, start, end = longest
    if length < THIN_GAP_SESSIONS or start is None or end is None:
        return False
    window = frozenset(counted)
    if own_traded & sibling_traded & window:
        return False
    return any(start <= d <= end for d in sibling_traded & window)


def trailing_sessions(calendar: TradingCalendar, as_of: date, count: int) -> list[date]:
    """The `count` NSE sessions ending at `as_of` (inclusive), plus the one session before them.

    The extra leading session is the boundary a gap that starts on the window's first session is
    measured from; it is never itself counted. Raises `ThinTradingError` when `as_of` is not a
    session or the calendar cannot reach far enough back.
    """
    if not calendar.is_session(as_of):
        raise ThinTradingError(f"{as_of.isoformat()} is not an NSE session")
    # 1.6 calendar days per session is generous for Indian holiday density.
    start = as_of - timedelta(days=int((count + 1) * 1.6) + 10)
    sessions = calendar.expected_sessions(start, as_of)
    if len(sessions) < count + 1:
        raise ThinTradingError(
            f"the calendar has {len(sessions)} sessions from {start.isoformat()} to "
            f"{as_of.isoformat()}; the window needs {count + 1}"
        )
    return sessions[-(count + 1) :]


def _venue_ratio(
    venue: VenueActivity, window: Sequence[date], captured: frozenset[date]
) -> Decimal:
    days = [d for d in window if d >= venue.first_bar]
    observed = [d for d in days if d in venue.traded or (d in captured and d not in venue.holes)]
    return _ratio(sum(1 for d in observed if d in venue.traded), len(observed))


def label_isin(
    activity: IsinActivity,
    *,
    as_of: date,
    sessions: Sequence[date],
    captured: Mapping[str, frozenset[date]],
) -> ThinTradingLabel | None:
    """Label one ISIN from its venues' activity over `sessions` (as `trailing_sessions` returns).

    What it does: walks the window session by session, from the ISIN's first bar on any venue. A
    session with a bar on any venue the ISIN was listed on is traded. Otherwise, a session that any
    of those venues' capture did not cover (`captured`), or that is one of the ISIN's own holes on
    one of them, is a *hole*: out of the denominator, and it ends any gap in progress, since we
    cannot say nobody traded on a day we did not see. Any other session is an observed no-trade
    session and extends the current gap. A gap's calendar length runs between the sessions either
    side of it (the previous bar and the next, as a dividend's reinvestment base is measured), or
    to `as_of` while it is still open.
    What it assumes: `sessions[0]` is the boundary session before the window and `sessions[-1]` is
    `as_of`; `captured` maps each venue to the sessions we captured for it.
    What it never does: count a session before a venue's first bar against that venue, or return a
    label for an ISIN with no bar on or before `as_of` (it returns `None`).
    """
    if not sessions or sessions[-1] != as_of:
        raise ThinTradingError("sessions must end at as_of")
    venues = [v for v in activity.venues if v.first_bar <= as_of]
    if not venues:
        return None
    window = list(sessions[1:])
    start_index = bisect.bisect_left(window, min(v.first_bar for v in venues))
    counted = window[start_index:]
    if not counted:
        return None

    observed = traded = holes = 0
    # (sessions, calendar days, first no-trade session, last no-trade session) of the longest gap.
    longest: _Gap = (0, 0, None, None)
    run_len = 0
    run_from: date | None = None
    run_to: date | None = None
    last_traded: date | None = None
    # The session before a gap bounds it: the previous bar, a hole, or the session before the
    # counted span when the gap opens the window.
    before_run = sessions[start_index]

    for day in counted:
        listed = [v for v in venues if v.first_bar <= day]
        if any(day in v.traded for v in listed):
            kind = "traded"
        elif any(
            day not in captured.get(v.exchange, frozenset()) or day in v.holes for v in listed
        ):
            kind = "hole"
        else:
            if run_len == 0:
                run_from = day
            run_len += 1
            run_to = day
            observed += 1
            continue
        if run_len:
            longest = max(
                longest, (run_len, (day - before_run).days, run_from, run_to), key=_gap_order
            )
        run_len, run_from, run_to = 0, None, None
        before_run = day
        if kind == "traded":
            observed += 1
            traded += 1
            last_traded = day
        else:
            holes += 1
    current_gap = run_len
    if run_len:
        # Still open at as_of: measured to as_of, the days since the last bar (or hole).
        longest = max(
            longest, (run_len, (as_of - before_run).days, run_from, run_to), key=_gap_order
        )

    ratio = _ratio(traded, observed)
    insufficient = observed < MIN_OBSERVED_SESSIONS
    own = frozenset().union(*(v.traded for v in venues))
    seam = _identity_seam(longest, counted, own, activity.sibling_traded)
    reasons: list[str] = []
    if not insufficient and not seam:
        if ratio < THIN_RATIO_BELOW:
            reasons.append("ratio")
        if longest[0] >= THIN_GAP_SESSIONS:
            reasons.append("gap")
    by_venue = {v.exchange: v for v in venues}
    nse, bse = by_venue.get("NSE"), by_venue.get("BSE")
    return ThinTradingLabel(
        as_of=as_of,
        isin=activity.isin,
        window_start=counted[0],
        sessions_observed=observed,
        sessions_traded=traded,
        capture_holes=holes,
        traded_ratio=ratio,
        nse_traded_ratio=None
        if nse is None
        else _venue_ratio(nse, window, captured.get("NSE", frozenset())),
        bse_traded_ratio=None
        if bse is None
        else _venue_ratio(bse, window, captured.get("BSE", frozenset())),
        longest_gap_sessions=longest[0],
        longest_gap_days=longest[1],
        longest_gap_from=longest[2],
        longest_gap_to=longest[3],
        current_gap_sessions=current_gap,
        last_traded=last_traded,
        nse_series=None if nse is None else nse.last_series,
        bse_series=None if bse is None else bse.last_series,
        trade_to_trade=any(v.trade_to_trade for v in venues),
        listing_status=activity.listing_status,
        insufficient_history=insufficient,
        identity_seam=seam,
        thin=bool(reasons),
        reasons=tuple(reasons),
    )


# ── reading the lake and the store ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ThinTradingRun:
    """What one run computed and wrote, for the job log and the CLI."""

    as_of: date
    labels: tuple[ThinTradingLabel, ...]
    venue_holes: Mapping[str, tuple[date, ...]] = field(default_factory=dict)
    written: int = 0

    def thin(self) -> list[ThinTradingLabel]:
        """The labels marked thinly traded."""
        return [lab for lab in self.labels if lab.thin]


def _partition_files(dataset: str, days: Iterable[date], *, data_root: Path | None) -> list[str]:
    root = layer_root(Layer.L1, data_root=data_root) / dataset
    files = [root / f"date={d.isoformat()}" / "part.parquet" for d in days]
    return [str(f) for f in files if f.exists()]


def _captured_sessions(
    conn: Connection, *, window: Sequence[date], l1_venue_dates: Mapping[str, set[date]]
) -> dict[str, frozenset[date]]:
    """Per venue: sessions whose capture is PUBLISHED in `sync_state` and present in L1."""
    out: dict[str, frozenset[date]] = {}
    for exchange, sources in VENUE_CAPTURE_SOURCES.items():
        rows = conn.execute(
            "SELECT DISTINCT logical_date FROM sync_state "
            "WHERE state = 'PUBLISHED' AND source = ANY(%s) AND logical_date BETWEEN %s AND %s",
            (list(sources), window[0], window[-1]),
        ).fetchall()
        published = {r[0] for r in rows}
        out[exchange] = frozenset(published & l1_venue_dates.get(exchange, set()))
    return out


def _gather(
    conn: Connection, *, as_of: date, sessions: Sequence[date], data_root: Path | None
) -> tuple[list[IsinActivity], dict[str, frozenset[date]]]:
    """Every ISIN with an equity bar in the window, and each venue's captured sessions."""
    window = list(sessions[1:])
    files = _partition_files("prices_raw", window, data_root=data_root)
    if not files:
        raise ThinTradingError(f"no prices_raw partition in the window ending {as_of.isoformat()}")
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT isin, exchange, series, trade_date FROM read_parquet($files, "
            "hive_partitioning = false) WHERE trade_date BETWEEN $start AND $end",
            {"files": files, "start": window[0], "end": as_of},
        ).fetchall()
        q_files = _partition_files("prices_raw_quarantine", window, data_root=data_root)
        quarantined = (
            con.execute(
                "SELECT isin, exchange, trade_date FROM read_parquet($files, "
                "hive_partitioning = false, union_by_name = true) WHERE isin IS NOT NULL",
                {"files": q_files},
            ).fetchall()
            if q_files
            else []
        )
        pr_files = _partition_files("pr_security_marks", window, data_root=data_root)
        pr_rows = (
            con.execute(
                "SELECT DISTINCT isin, session FROM read_parquet($files, hive_partitioning = false)"
                " WHERE trades > 0",
                {"files": pr_files},
            ).fetchall()
            if pr_files
            else []
        )
        first_bars = _first_bars(con, data_root=data_root)
    finally:
        con.close()

    # A reissued ISIN (split, face-value change) traded under its predecessor until the change, and
    # BSE's scrip-code era files a scrip's whole history under today's ISIN. Read alone, the
    # successor shows years of bars, then a hole until its reissue: an identity seam of ours, not
    # a stretch nobody traded. So every bar is counted under the end of its lineage chain.
    canonical = resolve_successors(_lineage_edges(conn, as_of=as_of))

    def resolve(isin: str) -> str:
        return canonical.get(isin, isin)

    venue_dates: dict[str, set[date]] = defaultdict(set)
    traded: dict[tuple[str, str], set[date]] = defaultdict(set)
    last: dict[tuple[str, str], tuple[date, str]] = {}
    for isin, exchange, series, day in rows:
        venue_dates[exchange].add(day)
        if series not in _EQUITY_SERIES.get(exchange, frozenset()):
            continue
        key = (resolve(isin), exchange)
        traded[key].add(day)
        prior = last.get(key)
        # A same-day move between series leaves two bars on one session; the regular series
        # names it, so a name is trade-to-trade only when that is all it traded in.
        regular = series not in _T2T_SERIES.get(exchange, frozenset())
        if prior is None or day > prior[0] or (day == prior[0] and regular):
            last[key] = (day, series)
    captured = _captured_sessions(conn, window=window, l1_venue_dates=venue_dates)

    holes: dict[tuple[str, str], set[date]] = defaultdict(set)
    for isin, exchange, day in quarantined:
        holes[(resolve(isin), exchange)].add(day)
    nse_window = set(window)
    for isin, day in pr_rows:
        key = (resolve(isin), "NSE")
        if day in nse_window and day not in traded.get(key, set()):
            holes[key].add(day)

    first: dict[tuple[str, str], date] = {}
    for (isin, exchange), day in first_bars.items():
        key = (resolve(isin), exchange)
        first[key] = min(day, first.get(key, day))
    status = _listing_status(conn)
    by_isin: dict[str, list[VenueActivity]] = defaultdict(list)
    for (isin, exchange), days in sorted(traded.items()):
        by_isin[isin].append(
            VenueActivity(
                exchange=exchange,
                traded=frozenset(days),
                first_bar=first.get((isin, exchange), min(days)),
                last_series=last[(isin, exchange)][1],
                holes=frozenset(holes.get((isin, exchange), set())),
            )
        )
    # For a company's shares (INE...) the first nine characters are issuer and security type
    # (INE05BN01...): two ISINs sharing them are the same kind of share of one company, as an old
    # and a reissued ISIN are. Fund units (INF...) share a prefix across schemes, so never count.
    days_by_isin: dict[str, set[date]] = defaultdict(set)
    for (isin, _), days in traded.items():
        days_by_isin[isin].update(days)
    by_issuer: dict[str, list[str]] = defaultdict(list)
    for isin in by_isin:
        if isin.startswith(_COMPANY_SHARE_PREFIX):
            by_issuer[isin[:9]].append(isin)

    def sibling_days(isin: str) -> frozenset[date]:
        out: set[date] = set()
        if not isin.startswith(_COMPANY_SHARE_PREFIX):
            return frozenset()
        for other in by_issuer[isin[:9]]:
            if other != isin:
                out |= days_by_isin[other]
        return frozenset(out)

    activities = [
        IsinActivity(
            isin=isin,
            venues=tuple(venues),
            listing_status=status.get(isin),
            sibling_traded=sibling_days(isin),
        )
        for isin, venues in sorted(by_isin.items())
    ]
    return activities, captured


def _first_bars(
    con: duckdb.DuckDBPyConnection, *, data_root: Path | None
) -> dict[tuple[str, str], date]:
    """The earliest bar of every (ISIN, venue) in all of L1 — the listing proxy."""
    root = layer_root(Layer.L1, data_root=data_root) / "prices_raw"
    rows = con.execute(
        "SELECT isin, exchange, min(trade_date) FROM read_parquet($glob, hive_partitioning = false)"
        " GROUP BY isin, exchange",
        {"glob": str(root / "date=*" / "part.parquet")},
    ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def _lineage_edges(conn: Connection, *, as_of: date) -> list[tuple[str, str]]:
    """(predecessor, successor) edges effective on or before `as_of` — point in time by the edge's
    own date, so a label for a past session never stitches a reissue that came later."""
    rows = conn.execute(
        "SELECT predecessor_isin, successor_isin FROM isin_lineage WHERE effective_date <= %s",
        (as_of,),
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def resolve_successors(edges: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Map every predecessor ISIN to the end of its lineage chain (A→B→C maps A and B to C).

    A predecessor with more than one successor (a demerger's fan-out) is left unmapped: its bars
    belong to no single successor, so it keeps its own label. A cycle is never followed round.
    """
    successors: dict[str, set[str]] = defaultdict(set)
    for predecessor, successor in edges:
        successors[predecessor].add(successor)
    single = {p: next(iter(s)) for p, s in successors.items() if len(s) == 1}
    out: dict[str, str] = {}
    for start in single:
        seen = {start}
        node = single[start]
        while node in single and single[node] not in seen:
            seen.add(node)
            node = single[node]
        out[start] = node
    return out


def _listing_status(conn: Connection) -> dict[str, str]:
    """The identity master's current status per ISIN (ACTIVE, SUSPENDED or DELISTED)."""
    rows = conn.execute("SELECT isin, status FROM security_master").fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


# ── writing and running ────────────────────────────────────────────────────────────────────

_COLUMNS: Final = (
    "as_of, isin, window_start, sessions_observed, sessions_traded, capture_holes, traded_ratio, "
    "nse_traded_ratio, bse_traded_ratio, longest_gap_sessions, longest_gap_days, "
    "longest_gap_from, longest_gap_to, current_gap_sessions, last_traded, nse_series, bse_series, "
    "trade_to_trade, listing_status, insufficient_history, identity_seam, thin, reasons, "
    "computed_at"
)
_PLACEHOLDERS: Final = ", ".join(["%s"] * len(_COLUMNS.split(",")))


def write_labels(
    conn: Connection, as_of: date, labels: Sequence[ThinTradingLabel], *, computed_at: datetime
) -> int:
    """Replace the `thin_trading_label` rows of `as_of` with `labels`, in one transaction.

    A rerun of the same session replaces its rows rather than adding to them, so the table holds
    exactly one label per (session, ISIN). Earlier sessions are never touched: they are the
    point-in-time history a backtest reads. Commits; returns the number of rows written.
    """
    if any(lab.as_of != as_of for lab in labels):
        raise ThinTradingError(f"every label written for {as_of.isoformat()} must carry that as_of")
    with conn.transaction():
        conn.execute("DELETE FROM thin_trading_label WHERE as_of = %s", (as_of,))
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO thin_trading_label ({_COLUMNS}) VALUES ({_PLACEHOLDERS})",
                [
                    (
                        lab.as_of,
                        lab.isin,
                        lab.window_start,
                        lab.sessions_observed,
                        lab.sessions_traded,
                        lab.capture_holes,
                        lab.traded_ratio,
                        lab.nse_traded_ratio,
                        lab.bse_traded_ratio,
                        lab.longest_gap_sessions,
                        lab.longest_gap_days,
                        lab.longest_gap_from,
                        lab.longest_gap_to,
                        lab.current_gap_sessions,
                        lab.last_traded,
                        lab.nse_series,
                        lab.bse_series,
                        lab.trade_to_trade,
                        lab.listing_status,
                        lab.insufficient_history,
                        lab.identity_seam,
                        lab.thin,
                        list(lab.reasons),
                        computed_at,
                    )
                    for lab in labels
                ],
            )
    conn.commit()
    return len(labels)


def latest_published_session(conn: Connection, calendar: TradingCalendar, today: date) -> date:
    """The newest NSE session on or before `today` whose bhavcopy is PUBLISHED.

    The label is as of the last session we actually hold, so a run on an evening the EOD pipeline
    failed labels the previous session again rather than counting today as a no-trade day.
    """
    row = conn.execute(
        "SELECT max(logical_date) FROM sync_state WHERE source = 'nse_bhavcopy' "
        "AND state = 'PUBLISHED' AND logical_date <= %s",
        (today,),
    ).fetchone()
    newest: date | None = None if row is None else row[0]
    if newest is None:
        raise ThinTradingError(f"no PUBLISHED nse_bhavcopy session on or before {today}")
    while not calendar.is_session(newest):
        newest -= timedelta(days=1)
    return newest


def run_thin_trading(
    conn: Connection,
    *,
    clock: Clock,
    data_root: Path | None = None,
    as_of: date | None = None,
    dry_run: bool = False,
) -> ThinTradingRun:
    """Compute (and unless `dry_run`, store) the label for one session — the job's whole body.

    What it does: picks the session (`as_of`, or the newest published NSE session), reads the
    window's L1 bars, quarantine and PR-bundle trades plus `sync_state`'s capture record, labels
    every (ISIN, venue) with an equity bar in the window, and replaces that session's rows in
    `thin_trading_label`. Logs the counts per venue and every market-wide capture hole.
    What it assumes: the store is migrated through 0025.
    What it never does: fetch, or write anything but `thin_trading_label`. No price, factor,
    quarantine or total return depends on this table.
    """
    calendar = trading_calendar()
    session = (
        as_of if as_of is not None else latest_published_session(conn, calendar, clock.today())
    )
    sessions = trailing_sessions(calendar, session, WINDOW_SESSIONS)
    activities, captured = _gather(conn, as_of=session, sessions=sessions, data_root=data_root)
    labels: list[ThinTradingLabel] = []
    for activity in activities:
        label = label_isin(activity, as_of=session, sessions=sessions, captured=captured)
        if label is not None:
            labels.append(label)
    window = sessions[1:]
    venue_holes = {
        exchange: tuple(d for d in window if d not in days) for exchange, days in captured.items()
    }
    for exchange, missing in venue_holes.items():
        if missing:
            log.warning(
                "quality.thin_trading.capture_holes",
                exchange=exchange,
                as_of=session.isoformat(),
                sessions=len(missing),
                first=missing[0].isoformat(),
                last=missing[-1].isoformat(),
            )
    written = 0
    if not dry_run:
        written = write_labels(conn, session, labels, computed_at=clock.now())
    run = ThinTradingRun(
        as_of=session, labels=tuple(labels), venue_holes=venue_holes, written=written
    )
    log.info(
        "quality.thin_trading.labelled",
        as_of=session.isoformat(),
        labelled=len(labels),
        thin=sum(1 for lab in labels if lab.thin),
        trade_to_trade=sum(1 for lab in labels if lab.trade_to_trade),
        with_capture_holes=sum(1 for lab in labels if lab.capture_holes),
        dry_run=dry_run,
    )
    return run


class _JobContext(Protocol):
    """What the job body needs of `scheduler.registry.JobContext`.

    Structural rather than imported: the registry imports every job's owner (the M17 desk among
    them), and the query layer re-exports this module, so importing the registry here would put
    `analyst.fundmanager` on the import graph of everything that reads the query layer —
    `tests/unit/test_commons_isolation.py` forbids exactly that.
    """

    @property
    def clock(self) -> Clock: ...

    @property
    def settings(self) -> Settings: ...


def run_thin_trading_job(context: _JobContext) -> None:
    """The scheduled job body: one `run_thin_trading` against the configured lake and store."""
    from dataplatform.store.db import connect

    with connect(context.settings) as conn:
        run_thin_trading(conn, clock=context.clock, data_root=context.settings.data_root)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: label one session (default the newest published), or `--dry-run` it and print."""
    from dataplatform.clock import SystemClock
    from dataplatform.config import get_settings
    from dataplatform.store.db import connect

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--dry-run", action="store_true", help="compute and print; write nothing")
    args = parser.parse_args(argv)
    settings = get_settings()
    with connect(settings) as conn:
        run = run_thin_trading(
            conn,
            clock=SystemClock(settings.tzinfo),
            data_root=settings.data_root,
            as_of=args.as_of,
            dry_run=args.dry_run,
        )
    thin = sorted(run.thin(), key=lambda lab: (lab.traded_ratio, lab.isin))
    for lab in thin:
        print(
            f"{lab.isin} ratio={lab.traded_ratio} nse={lab.nse_traded_ratio} "
            f"bse={lab.bse_traded_ratio} gap={lab.longest_gap_sessions}s/{lab.longest_gap_days}d "
            f"nse_series={lab.nse_series} bse_series={lab.bse_series} "
            f"status={lab.listing_status} holes={lab.capture_holes} {','.join(lab.reasons)}"
        )
    print(
        f"as_of={run.as_of} labelled={len(run.labels)} thin={len(thin)} written={run.written} "
        f"venue_holes={ {k: len(v) for k, v in run.venue_holes.items()} }"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
