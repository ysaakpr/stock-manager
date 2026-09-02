"""D4 query shape (d) — the point-in-time universe as of a historical date (M4.2).

§4.5 names the fourth canonical shape: *point-in-time universe as of a historical date (via
index-constituent history + listing status — kills survivorship bias)*. This module is that shape.

The survivorship trap it exists to close: a backtest that screens *today's* NIFTY 50 (or today's
list of listed securities) over a decade silently drops every name that has since been delisted —
the failures, the acquisitions, the frauds — and keeps only the survivors, which flatters every
result. The fix is two facts, both point-in-time:

* **listing status** — a security is in the universe on `as_of` only if it was *listed and not yet
  delisted* then. A name delisted in 2024 was tradeable in 2021 and must appear in a 2021 universe;
  a name that first listed in 2023 must not. This is the half that keeps the dead names in
  (invariant #8 is about fundamentals; this is the universe-membership analogue for §4.5).
* **index-constituent history** — when the universe is scoped to an index, membership is read from
  the monthly snapshot that was *in force on `as_of`* (M3.9's `membership_asof`), never today's
  list. The accumulated snapshots are exactly what make "who was in NIFTY 50 in March 2023"
  answerable without a look-ahead (§4.1, invariant #7).

The listing calendar is *injected*, not read from the Parquet lake: listing status lives in the
identity master (Postgres `security_master` / `exchange_listing`), not in L1/L2. `ListingCalendar`
is the seam — `store_listing_calendar` adapts an `IdentityStore` for production, and a test builds
an `InMemoryListingCalendar` of hand-set windows so the PIT logic is provable offline (B8). The
index side *is* file-backed and reads through M3.9 directly.

Clockless by construction: `as_of` is always an argument. There is no `datetime.now()` here — a
universe "as of today" is the caller passing today's date, and the same call replays byte-identical
(invariant #11, B10).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from dataplatform.identity.master import ListingStatus
from dataplatform.ingest.indices import membership_asof
from dataplatform.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from dataplatform.identity.master import IdentityStore

__all__ = [
    "InMemoryListingCalendar",
    "ListingCalendar",
    "ListingWindow",
    "PitUniverse",
    "index_membership_asof",
    "pit_universe",
    "store_listing_calendar",
]

_LOG = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ListingWindow:
    """The span of dates one security was tradeable, keyed by ISIN — the atom of a PIT universe.

    What it does: carry the two point-in-time bounds a universe-membership decision needs — the
    first date the security was listed (`listed_from`) and the date it was delisted (`delisted_on`,
    `None` while it still trades) — plus its current `status` for the record.
    What it assumes: both bounds are trading dates in Asia/Kolkata; `listed_from` is knowable (a
    security with no listing date at all cannot be placed in time and is a data gap, not a member).
    What it never does: join on a symbol (invariant #2) — the key is the ISIN.

    Boundary convention (documented so the edge is not a coin toss): the security is tradeable on a
    date `d` when `listed_from <= d` and it is not yet delisted, i.e. `delisted_on is None or
    d < delisted_on`. `delisted_on` is the first date the security was *no longer* tradeable, so a
    universe as of the delisting date itself excludes it. Names delisted strictly after `as_of` are
    kept — that is the survivorship fix.
    """

    isin: str
    listed_from: date
    delisted_on: date | None
    status: ListingStatus

    def tradeable_on(self, on_date: date) -> bool:
        """Whether this security was listed and not yet delisted on `on_date` (see convention)."""
        if on_date < self.listed_from:
            return False
        return self.delisted_on is None or on_date < self.delisted_on


@runtime_checkable
class ListingCalendar(Protocol):
    """Where the PIT universe reads listing status from — the injected seam over the identity store.

    A protocol, not a class, because the source differs by context: production adapts a Postgres
    `IdentityStore` (`store_listing_calendar`); a test builds windows in memory so the survivorship
    logic is provable with no database (B8). Either way the universe builder only ever asks for the
    windows and never reaches into how they were obtained.
    """

    def windows(self) -> Iterable[ListingWindow]:
        """Every listing window the calendar knows — including delisted names (§4.5)."""
        ...


@dataclass(frozen=True, slots=True)
class InMemoryListingCalendar:
    """A `ListingCalendar` over a fixed tuple of windows — the offline/test source.

    Holds the windows verbatim; it is the caller's job that each ISIN appears once. Frozen so a
    universe built from it is reproducible.
    """

    _windows: tuple[ListingWindow, ...]

    def windows(self) -> Iterable[ListingWindow]:
        return self._windows


@dataclass(frozen=True, slots=True)
class PitUniverse:
    """The set of ISINs tradeable as of one historical date — the response to shape (d).

    `isins` is exactly the securities that were listed and not yet delisted on `as_of` (and, when
    the universe was scoped to one or more indices, that the in-force constituent snapshot named).
    It is a set: membership is the whole answer, and downstream (a screen, a backtest's tradeable
    set) intersects against it. Includes later-delisted names and excludes not-yet-listed ones —
    the property that kills survivorship bias.
    """

    as_of: date
    isins: frozenset[str]
    index_slugs: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.isins)

    def __contains__(self, isin: str) -> bool:
        return isin in self.isins


def index_membership_asof(
    index_slugs: Iterable[str], on_date: date, *, data_root: Path | None = None
) -> frozenset[str]:
    """The union of the named indices' memberships in force on `on_date` (M3.9, file-backed).

    Reads, per index, the most recent monthly constituent snapshot whose `as_of` is on or before
    `on_date` — never today's list — via M3.9's `membership_asof`, and unions the members. An index
    with no snapshot in force then contributes nothing (a gap for D7 to explain, not today's list).
    Returns an empty set when `index_slugs` is empty — the caller then screens the whole listed
    market rather than an index subset.
    """
    members: set[str] = set()
    for slug in index_slugs:
        snapshot = membership_asof(slug, on_date, data_root=data_root)
        if snapshot is not None:
            members |= snapshot.members
    return frozenset(members)


def pit_universe(
    as_of: date,
    calendar: ListingCalendar,
    *,
    index_membership: frozenset[str] | None = None,
    index_slugs: Iterable[str] = (),
) -> PitUniverse:
    """The point-in-time universe as of `as_of` — shape (d) (§4.5).

    What it does: keeps every ISIN whose listing window was tradeable on `as_of` (listed, not yet
    delisted), then — when `index_membership` is supplied — intersects with the constituent set the
    index held then, so the result is exactly the securities that both existed and were in scope on
    the date. A name delisted after `as_of` survives the filter (it was tradeable then); a name
    first listed after `as_of` is excluded.
    What it assumes: `index_membership`, when given, was itself read as-of `as_of` (use
    `index_membership_asof`) — this function does not re-read it, so the caller owns that the two
    dates agree. `index_slugs` is carried through only for the record on the result.
    What it never does: invent a member. A universe with no in-force snapshot and an index scope is
    empty, not today's list.

    The intersection order matters for the survivorship guarantee: a security in the index snapshot
    but *delisted before* `as_of` is still dropped (it was not tradeable), and a tradeable security
    *not in* the snapshot is dropped (out of scope) — both are correct.
    """
    tradeable = {window.isin for window in calendar.windows() if window.tradeable_on(as_of)}
    if index_membership is not None:
        tradeable &= index_membership
    universe = PitUniverse(as_of=as_of, isins=frozenset(tradeable), index_slugs=tuple(index_slugs))
    _LOG.info(
        "query.pit_universe",
        as_of=as_of.isoformat(),
        isins=len(universe.isins),
        scoped=index_membership is not None,
        index_slugs=list(universe.index_slugs),
    )
    return universe


def store_listing_calendar(store: IdentityStore) -> InMemoryListingCalendar:
    """Adapt an `IdentityStore` into a `ListingCalendar` — the production listing source.

    What it does: reads `security_master` (for the `first_seen_date` fallback) and every
    `exchange_listing` row, and folds an ISIN's per-exchange listings into one tradeable window: it
    is listed from the earliest listing date across its exchanges (falling back to when we first saw
    it in a snapshot when the exchange gave no listing date), and delisted only once *every*
    exchange has delisted it — the last delisting date wins, because a name still trading on one
    venue is still in the universe.
    What it assumes: the store is loaded from a migrated schema; delisted rows are present (they are
    — `load_securities` keeps them, §4.5).
    What it never does: drop a delisted security. That is the whole point.

    Not exercised by the offline PIT tests (those inject an `InMemoryListingCalendar`); this is the
    real wiring the daily/backtest paths use, kept here so the seam has one obvious production side.
    """
    securities = {security.isin: security for security in store.load_securities()}
    listed_from: dict[str, date] = {}
    delisted_on: dict[str, date | None] = {}
    any_open: dict[str, bool] = {}
    status: dict[str, ListingStatus] = {}

    for listing in store.load_listings():
        isin = listing.isin
        first_seen = securities[isin].first_seen_date if isin in securities else None
        listed = listing.listing_date or first_seen
        if listed is not None:
            listed_from[isin] = min(listed, listed_from.get(isin, listed))
        # A security is delisted only once no exchange still lists it: track whether any listing is
        # open, and the latest delisting date to use when none is.
        if listing.delisting_date is None:
            any_open[isin] = True
        else:
            prior = delisted_on.get(isin)
            delisted_on[isin] = (
                listing.delisting_date if prior is None else max(prior, listing.delisting_date)
            )
        if isin in securities:
            status[isin] = securities[isin].status

    windows: list[ListingWindow] = []
    for isin, listed in listed_from.items():
        delisted = None if any_open.get(isin, False) else delisted_on.get(isin)
        windows.append(
            ListingWindow(
                isin=isin,
                listed_from=listed,
                delisted_on=delisted,
                status=status.get(isin, ListingStatus.ACTIVE),
            )
        )
    return InMemoryListingCalendar(tuple(sorted(windows, key=lambda window: window.isin)))
