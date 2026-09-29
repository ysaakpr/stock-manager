"""H2 — price-band-hit avoidance: the band hits a decision may read, and the filter over them (X2).

Pre-registered in ``ops/studies/preregistration-signals-2026-09-29.md`` §3 and fixed there: no
*new* buy of any name that hit its upper or lower daily price band in any of the last
:data:`BAND_HIT_LOOKBACK_SESSIONS` (5) sessions, from the ``bh`` member of the NSE PR bundle,
knowable on the bundle's own publication date. Existing holdings are not force-sold. Nothing here
may be tuned: the lookback, the source and the rule are the hypothesis.

Three pieces, separable on purpose so H3 (H1 + H2) reuses the filter without this module's reader:

* :func:`band_hit_blocked` — **pure**. Given the hits a decision may see, the decision date and the
  lookback window, the ISINs no new buy may touch. It raises on a hit not yet knowable on the
  decision date rather than filtering it, like ``PitContext.admit``: a silent drop would turn a
  leak into a plausible short answer.
* :func:`resolve_session` — **pure**. One session's symbol-keyed ``bh`` rows against that same
  session's bhavcopy ``(symbol, series) → ISIN``. Unresolved and ambiguous rows are returned, never
  guessed.
* :class:`BandHitIndex` — the read path the backtest uses: every bundle in L0 for a window, parsed,
  resolved and held **in memory**. Nothing is written to L1, L2 or Postgres.

**Why a same-session exchange-file join respects invariant #2.** ISIN is the only join key, and a
symbol is not an identity: NSE reuses symbols, renames them, and moves a name between series. What
makes a symbol dangerous is joining it through a table from *another* date — a current listing
snapshot (``EQUITY_L``) maps today's owner of a symbol, not the session's, which is survivorship
bias and, for a renamed or reused symbol, the wrong company. The bhavcopy for session *d* is NSE's
own record of which ISIN traded under ``(symbol, series)`` on *d*; the ``bh`` file for *d* is NSE's
record of which ``(symbol, series)`` hit a band on *d*. Both are the exchange's statements about the
same session, so the join is an identity lookup inside one exchange-day, and nothing outside that
day is consulted. The bhavcopy's rows are what L1 ``prices_raw`` carries for that date (NSE rows
only), so that is where the lookup reads. A pair that maps to two ISINs on one session is
*ambiguous* and unresolved; a pair absent from that session's bhavcopy (L1 starts 2011-06-22, and
debt, SME and trade-for-trade series are often not in it) is unresolved. Both are counted per year
and reported.

**Point-in-time.** A hit on session *d* is knowable from ``PrBundle.publication_date`` — derived
from the payload, never from a clock. The bundle is end-of-day, as is every close the swing policy
decides on, so a hit on *d* may block a buy decided on *d*, and not earlier. The four bundles
``PrBundle`` refuses to date (``dataplatform.ingest.nse.pr_bundle.bh``) are counted, not recovered.
A session with no readable ``bh`` leaves no hits for that session: the filter blocks less, it never
invents a hit.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol

import duckdb

from dataplatform.clock import FrozenClock
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle import (
    PR_BUNDLE_SOURCE_ID,
    BandHitRow,
    BandSide,
    MemberKind,
    PrBundle,
    parse_bh_bundle,
)
from dataplatform.logging import get_logger
from dataplatform.query.pit import Dataset, PitError
from dataplatform.store.l0 import L0Store
from dataplatform.store.l2 import open_connection
from dataplatform.store.paths import l1_partition_path

__all__ = [
    "BAND_HIT_AVOIDANCE_IDENTITY",
    "BAND_HIT_BLOCK_RATIONALE",
    "BAND_HIT_LOOKBACK_SESSIONS",
    "BandHit",
    "BandHitData",
    "BandHitIndex",
    "SessionListing",
    "YearResolution",
    "band_hit_blocked",
    "lookback_window",
    "resolve_session",
]

_LOG = get_logger(__name__)

#: The pre-registered lookback: a hit in any of the last five sessions, the decision session
#: included, blocks a new buy. Fixed by the pre-registration; never a parameter.
BAND_HIT_LOOKBACK_SESSIONS: Final = 5

#: What a run's specification records when H2 is on, so its digest differs from the baseline's
#: while every run without it keeps the digest it already has.
BAND_HIT_AVOIDANCE_IDENTITY: Final = (
    f"H2:{PR_BUNDLE_SOURCE_ID}.bh:lookback={BAND_HIT_LOOKBACK_SESSIONS}:same-session-bhavcopy"
)

#: How a buy this filter blocked opens its journal line. A no-op is still a decision (invariant
#: #9), and this is the prefix a count of blocked buys reads.
BAND_HIT_BLOCK_RATIONALE: Final = "band-hit avoidance (H2)"

#: One session's bhavcopy identity table: ``(symbol, series) → ISIN``, or ``None`` when the pair
#: named more than one ISIN that session.
type SessionListing = Mapping[tuple[str, str], str | None]


@dataclass(frozen=True, slots=True)
class BandHit:
    """One resolved band hit: which ISIN, on which session, knowable when, on which side."""

    isin: str
    session: date
    knowable_date: date
    side: BandSide
    symbol: str
    series: str


class BandHitData(Protocol):
    """The seam the swing policy reads H2 through (injected, so tests need no lake)."""

    def window(self, as_of: date) -> tuple[date, ...]:
        """The lookback sessions for a decision on ``as_of``, ``as_of`` included."""

    def band_hits(self, as_of: date) -> Dataset[BandHit]:
        """The hits inside ``window(as_of)`` that are knowable on ``as_of``, as a PIT dataset."""


def lookback_window(
    calendar: Sequence[date], as_of: date, sessions: int = BAND_HIT_LOOKBACK_SESSIONS
) -> tuple[date, ...]:
    """The last ``sessions`` trading sessions on or before ``as_of``, ascending.

    Assumes ``calendar`` is the ascending trading calendar. Counts trading sessions, never
    calendar days, so a holiday does not shorten the lookback. Shorter than ``sessions`` only at
    the calendar's own start.
    """
    eligible = [session for session in calendar if session <= as_of]
    return tuple(eligible[-sessions:])


def band_hit_blocked(
    hits: Iterable[BandHit], *, as_of: date, window: Collection[date]
) -> frozenset[str]:
    """The ISINs no new buy may touch on ``as_of``: those with a band hit in ``window``.

    What it does: returns the ISIN of every hit whose session is in ``window`` — upper or lower
    band alike, as the pre-registration says.
    What it assumes: ``window`` is :func:`lookback_window` for ``as_of``.
    What it never does: use a hit not yet knowable on ``as_of``. Such a hit raises ``PitError``
    rather than being skipped — the caller built the wrong query, and a quiet skip would hide it.
    Never sells anything: what the caller does with a blocked *holding* is not this function's
    business, and the pre-registration says it is kept.
    """
    blocked: set[str] = set()
    for hit in hits:
        if hit.knowable_date > as_of:
            raise PitError(
                f"band hit for {hit.isin} on {hit.session.isoformat()} is knowable only from "
                f"{hit.knowable_date.isoformat()} and must not reach a decision on "
                f"{as_of.isoformat()} (invariant #7)"
            )
        if hit.session in window:
            blocked.add(hit.isin)
    return frozenset(blocked)


def resolve_session(
    rows: Sequence[BandHitRow], listing: SessionListing
) -> tuple[tuple[BandHit, ...], tuple[tuple[BandHitRow, str], ...]]:
    """Resolve one session's ``bh`` rows through that same session's bhavcopy.

    Returns ``(hits, unresolved)``, where each unresolved row carries why: ``absent`` (the pair is
    not in the session's bhavcopy) or ``ambiguous`` (it names more than one ISIN). Assumes every row
    and ``listing`` describe the same session; raises ``ValueError`` if the rows do not agree on
    one. Never consults any other date's listing, and never guesses.
    """
    sessions = {row.session for row in rows}
    if len(sessions) > 1:
        raise ValueError(f"rows from more than one session: {sorted(sessions)}")
    hits: list[BandHit] = []
    unresolved: list[tuple[BandHitRow, str]] = []
    for row in rows:
        key = (row.symbol, row.series)
        if key not in listing:
            unresolved.append((row, "absent"))
            continue
        isin = listing[key]
        if isin is None:
            unresolved.append((row, "ambiguous"))
            continue
        hits.append(
            BandHit(
                isin=isin,
                session=row.session,
                knowable_date=row.publication_date,
                side=row.side,
                symbol=row.symbol,
                series=row.series,
            )
        )
    return tuple(hits), tuple(unresolved)


@dataclass(slots=True)
class YearResolution:
    """How well one calendar year's ``bh`` rows resolved to ISINs — the report H2 owes."""

    year: int
    bundles: int = 0
    undated_bundles: int = 0
    bundles_without_bh: int = 0
    sessions_without_bhavcopy: int = 0
    rows: int = 0
    resolved: int = 0
    rows_eq: int = 0
    resolved_eq: int = 0
    unresolved_by_series: Counter[str] = field(default_factory=Counter)
    ambiguous: int = 0

    @property
    def unresolved(self) -> int:
        return self.rows - self.resolved

    @property
    def rate(self) -> Decimal | None:
        """Share of rows resolved; ``None`` for a year with no rows."""
        return _share(self.resolved, self.rows)

    @property
    def rate_eq(self) -> Decimal | None:
        """Share of ``EQ``-series rows resolved — the series the swing universe trades."""
        return _share(self.resolved_eq, self.rows_eq)


def _share(part: int, whole: int) -> Decimal | None:
    return (Decimal(part) / Decimal(whole)).quantize(Decimal("0.0001")) if whole else None


class BandHitIndex:
    """Every resolved band hit of a window, in memory, answering :class:`BandHitData`.

    What it does: holds ``session → hits`` and serves, for a decision on ``as_of``, the hits of the
    lookback window that are knowable by then, declared by their publication date.
    What it assumes: ``calendar`` is the ascending trading calendar the replay walks.
    What it never does: write anywhere, read a clock, or serve a hit whose publication is after
    ``as_of`` — the construction excludes it and ``PitContext.admit`` would refuse it anyway.
    """

    def __init__(
        self,
        hits: Iterable[BandHit],
        calendar: Sequence[date],
        *,
        resolution: Mapping[int, YearResolution] | None = None,
    ) -> None:
        by_session: dict[date, list[BandHit]] = defaultdict(list)
        for hit in hits:
            by_session[hit.session].append(hit)
        self._hits = {
            session: tuple(sorted(found, key=lambda h: (h.isin, h.side.value)))
            for session, found in by_session.items()
        }
        self._calendar = tuple(calendar)
        self.resolution: dict[int, YearResolution] = dict(resolution or {})

    def window(self, as_of: date) -> tuple[date, ...]:
        return lookback_window(self._calendar, as_of)

    def band_hits(self, as_of: date) -> Dataset[BandHit]:
        records = tuple(
            hit
            for session in self.window(as_of)
            for hit in self._hits.get(session, ())
            if hit.knowable_date <= as_of
        )
        return Dataset.declaring(
            f"band_hits@{as_of.isoformat()}", records, knowable_date=lambda h: h.knowable_date
        )

    @classmethod
    def from_lake(
        cls,
        *,
        start: date,
        end: date,
        calendar: Sequence[date],
        data_root: Path | None = None,
    ) -> BandHitIndex:
        """Read every PR bundle in ``[start, end]`` from L0 and resolve it against L1, in memory.

        ``start`` should reach back far enough to fill the first decision's lookback; the caller
        owns that margin. Each payload is re-hashed on the way in (``L0Store.get``, invariant #1).
        Nothing is written; the result lives only as long as the run that built it.
        """
        # The store's clock stamps writes only, and this path never writes; frozen at the window's
        # end so nothing here can observe the wall clock.
        store = L0Store(clock=FrozenClock(end), data_root=data_root)
        con = open_connection()
        hits: list[BandHit] = []
        resolution: dict[int, YearResolution] = {}
        try:
            for ref in store.iter_refs(PR_BUNDLE_SOURCE_ID, start=start, end=end):
                year = resolution.setdefault(
                    ref.logical_date.year, YearResolution(ref.logical_date.year)
                )
                year.bundles += 1
                try:
                    bundle = PrBundle(store.get(ref), filename=ref.filename)
                except ParseError as exc:
                    year.undated_bundles += 1
                    _LOG.warning(
                        "band_hits.bundle_undated",
                        source=PR_BUNDLE_SOURCE_ID,
                        date=ref.logical_date.isoformat(),
                        filename=ref.filename,
                        error=str(exc),
                        state="SKIPPED",
                    )
                    continue
                with bundle:
                    if not bundle.has(MemberKind.BH):
                        year.bundles_without_bh += 1
                        continue
                    parsed = parse_bh_bundle(bundle, l0_key=ref.key)
                listing = _session_listing(con, parsed.publication_date, data_root=data_root)
                if listing is None:
                    year.sessions_without_bhavcopy += 1
                    listing = {}
                resolved, unresolved = resolve_session(parsed.rows, listing)
                hits.extend(resolved)
                year.rows += len(parsed.rows)
                year.resolved += len(resolved)
                year.rows_eq += sum(1 for row in parsed.rows if row.series == "EQ")
                year.resolved_eq += sum(1 for hit in resolved if hit.series == "EQ")
                for row, why in unresolved:
                    year.unresolved_by_series[row.series] += 1
                    year.ambiguous += why == "ambiguous"
        finally:
            con.close()
        for year in resolution.values():
            _LOG.info(
                "band_hits.resolved",
                source=PR_BUNDLE_SOURCE_ID,
                year=year.year,
                bundles=year.bundles,
                undated=year.undated_bundles,
                rows=year.rows,
                resolved=year.resolved,
                ambiguous=year.ambiguous,
                sessions_without_bhavcopy=year.sessions_without_bhavcopy,
                state="VALIDATED",
            )
        return cls(hits, calendar, resolution=resolution)


def _session_listing(
    con: duckdb.DuckDBPyConnection, session: date, *, data_root: Path | None
) -> SessionListing | None:
    """That session's NSE bhavcopy ``(symbol, series) → ISIN`` from L1, or ``None`` if absent."""
    path = l1_partition_path("prices_raw", session, data_root=data_root)
    if not path.exists():
        return None
    rows = con.execute(
        "SELECT DISTINCT symbol, series, isin FROM read_parquet($path) "
        "WHERE exchange = 'NSE' AND trade_date = $session",
        {"path": str(path), "session": session},
    ).fetchall()
    listing: dict[tuple[str, str], str | None] = {}
    for symbol, series, isin in rows:
        key = (str(symbol), str(series))
        listing[key] = str(isin) if key not in listing else None
    return listing
