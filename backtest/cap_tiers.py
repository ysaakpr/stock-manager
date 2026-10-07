"""Market-cap tiers for a backtest, struck point-in-time from a liquidity rank (X2, 2026-10-05).

AMFI classifies a stock as large / mid / small cap by its rank on six-month average full market
cap: ranks 1-100 large, 101-250 mid, 251 and below small. A backtest that wants those tiers on a
past date needs a *dated* shares-outstanding series for every name, and the lake does not have one
across the history a strategy is measured on: the PR bundle's ``mcap`` member (Issue Size) starts
2024-02-01, the ``ffix`` member's ISSUE_CAP covers only index members over 2010-01-04..2013-04-30,
and XBRL filings (``pit_fundamentals``) start 2018-05. Splicing those together would put a
structural break in the size measure exactly where the source changes, and a ranking learns breaks.

So the size measure here is a **proxy, and it is named as one**: a name's median daily traded
value (close x quantity) over the trailing :data:`SIZE_LOOKBACK_SESSIONS` NSE sessions, ranked
among every NSE ``EQ`` equity (``INE`` ISIN, valid check digit) that printed on the decision
session. The tiers it yields are **liquidity-rank tiers that proxy AMFI cap tiers** — never "market
cap" in a report. 126 sessions is AMFI's own six-month averaging window, so the only change from
the AMFI rule is the quantity ranked. How closely the proxy tracks real market cap where the lake
does carry it is measured, not assumed: ``ops/studies/cap-tier-size-measure-2026-10-05.md``.

The median is struck over the sessions in the window on which the name traded — the same rule as
the M9.3 liquidity floor (``backtest.run._L1Reader.median_turnover_over``), so the two screens
cannot disagree about what a session without a print means.

Point-in-time (invariant #7): a decision session's tiers read bars on or before that session and
nothing after it, and every membership record is dated by that session. Today's market cap and
today's index membership are never read for a past date.

What this module never does: read a clock, key on a symbol, or rank on a figure from after the
decision session.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

import duckdb

from dataplatform.ingest.models import is_isin_check_digit_valid
from dataplatform.logging import get_logger
from dataplatform.query import default_price_quarantine
from dataplatform.query.pit import Dataset
from dataplatform.store.l2 import open_connection

__all__ = [
    "CAP_TIER_IDENTITY",
    "SIZE_LOOKBACK_SESSIONS",
    "TIER_RANKS",
    "CapTier",
    "CapTierData",
    "LiquidityRankTiers",
    "TierMembership",
    "TierSleeve",
    "assign_tiers",
    "describe_sleeves",
    "is_ranked_equity",
    "tier_for_rank",
]

_LOG = get_logger(__name__)

#: AMFI's six-month averaging window, in NSE sessions.
SIZE_LOOKBACK_SESSIONS: Final = 126

#: What a run spec records for an arm whose candidates are split into these tiers. Versioned, so a
#: change to the size measure or the boundaries can never resume a run made under the old one.
CAP_TIER_IDENTITY: Final = "liquidity_rank_tiers/v1"


class CapTier(StrEnum):
    """An AMFI-style size tier — here, by liquidity rank (module docstring)."""

    LARGE = "large"
    MID = "mid"
    SMALL = "small"


#: Each tier's inclusive rank range. Ranks past the last bound belong to no tier.
TIER_RANKS: Final[tuple[tuple[CapTier, int, int], ...]] = (
    (CapTier.LARGE, 1, 100),
    (CapTier.MID, 101, 250),
    (CapTier.SMALL, 251, 500),
)

_EQUITY_ISIN: Final = re.compile(r"INE[A-Z0-9]{8}[0-9]")


def is_ranked_equity(isin: str) -> bool:
    """Whether ``isin`` is a listed Indian company's security this ranking admits.

    ``INE`` with a valid ISO 6166 check digit. ETFs and mutual-fund units (``INF``) print in NSE's
    ``EQ`` series with some of the largest traded values in the market; ranked alongside stocks
    they would take large-cap slots no stock fund could hold.
    """
    return bool(_EQUITY_ISIN.fullmatch(isin)) and is_isin_check_digit_valid(isin)


def tier_for_rank(rank: int) -> CapTier | None:
    """The tier of a 1-based size rank, or None past rank 500 (or for a non-positive rank)."""
    for tier, first, last in TIER_RANKS:
        if first <= rank <= last:
            return tier
    return None


@dataclass(frozen=True, slots=True)
class TierMembership:
    """One name's size rank and tier on one decision session.

    ``size`` is the trailing median traded value in rupees; ``knowable_date`` is the session whose
    close completed the window, so the record is admitted only on or after it.
    """

    isin: str
    tier: CapTier
    rank: int
    size: Decimal
    knowable_date: date


def assign_tiers(size: Mapping[str, Decimal], *, as_of: date) -> tuple[TierMembership, ...]:
    """Rank ``size`` descending (ties by ISIN) and return every name ranked inside a tier.

    Assumes ``size`` holds exactly the population to rank on ``as_of`` — already narrowed to
    :func:`is_ranked_equity` names that printed that session. Never ranks a non-positive size.
    """
    ordered = sorted(
        ((isin, value) for isin, value in size.items() if value > 0),
        key=lambda item: (-item[1], item[0]),
    )
    out: list[TierMembership] = []
    for rank, (isin, value) in enumerate(ordered, start=1):
        tier = tier_for_rank(rank)
        if tier is None:
            break
        out.append(TierMembership(isin, tier, rank, value, as_of))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class TierSleeve:
    """One tier's share of a tiered book: how many names it buys, and its sell band.

    ``top_n`` names are bought from the tier by within-tier composite rank; a holding in the tier
    is sold once its within-tier rank leaves the top ``sell_band`` (the swing composite's
    hysteresis, applied inside the tier).
    """

    tier: CapTier
    top_n: int
    sell_band: int

    def __post_init__(self) -> None:
        if self.top_n <= 0:
            raise ValueError(f"top_n must be positive, got {self.top_n}")
        if self.sell_band < self.top_n:
            raise ValueError(f"sell_band {self.sell_band} must be at least top_n {self.top_n}")


def describe_sleeves(sleeves: Sequence[TierSleeve]) -> str:
    """The run-spec value of a tiered arm: the identity, then each sleeve in order."""
    parts = ",".join(f"{s.tier.value}={s.top_n}/{s.sell_band}" for s in sleeves)
    return f"{CAP_TIER_IDENTITY}[{parts}]"


@runtime_checkable
class CapTierData(Protocol):
    """Where a tiered policy reads each decision session's tiers — guardable, like every read."""

    def tiers(self, as_of: date) -> Dataset[TierMembership]:
        """The tier memberships struck as of ``as_of``."""


class LiquidityRankTiers:
    """:class:`CapTierData` off the L1 lake: the proxy size measure, ranked per decision session.

    ``calendar`` is the NSE session calendar (``_L1Reader.all_sessions``); the trailing window is
    counted in its sessions, so a holiday never shortens it. :meth:`load` computes every requested
    session in one windowed query; :meth:`tiers` serves only what was loaded and refuses the rest,
    because a tier computed lazily on a session nobody planned for is a tier nobody audited.
    """

    _VIEW = "cap_tier_prices_raw"

    def __init__(
        self,
        calendar: Sequence[date],
        *,
        data_root: Path | None = None,
        con: duckdb.DuckDBPyConnection | None = None,
    ) -> None:
        self._calendar = tuple(calendar)
        self._index = {session: i for i, session in enumerate(self._calendar)}
        self._owned = con is None
        self._con = con if con is not None else open_connection()
        # D22: a quarantined ISIN's pre-step bars never place it in a tier.
        default_price_quarantine().register_raw_view(
            self._con, view=self._VIEW, data_root=data_root
        )
        self._tiers: dict[date, tuple[TierMembership, ...]] = {}

    def close(self) -> None:
        if self._owned:
            self._con.close()

    def load(self, sessions: Iterable[date]) -> None:
        """Strike the tiers of every session in ``sessions`` not loaded yet (one query)."""
        wanted = sorted(set(sessions) - set(self._tiers))
        if not wanted:
            return
        unknown = [s for s in wanted if s not in self._index]
        if unknown:
            raise ValueError(f"{unknown[0].isoformat()} is not an NSE session of the calendar")
        sizes = self.size_measure(wanted)
        for session in wanted:
            self._tiers[session] = assign_tiers(sizes.get(session, {}), as_of=session)
        _LOG.info(
            "cap_tiers.loaded",
            sessions=len(wanted),
            first=wanted[0].isoformat(),
            last=wanted[-1].isoformat(),
            measure=CAP_TIER_IDENTITY,
        )

    def size_measure(self, sessions: Sequence[date]) -> dict[date, dict[str, Decimal]]:
        """Each session's ranked population and size: median close x quantity over the window.

        For every equity name that traded on the session: the median (``quantile_disc``, an
        observed value, never an interpolation) of its daily close x quantity over the trailing
        :data:`SIZE_LOOKBACK_SESSIONS` calendar sessions ending on it, counting only the sessions
        on which it traded. Reads no bar after the latest requested session.
        """
        if not sessions:
            return {}
        first = self._index[min(sessions)] - (SIZE_LOOKBACK_SESSIONS - 1)
        lo = self._calendar[max(first, 0)]
        hi = max(sessions)
        self._con.execute("CREATE OR REPLACE TEMP TABLE cap_tier_cal (session DATE, idx INTEGER)")
        self._con.executemany(
            "INSERT INTO cap_tier_cal VALUES (?, ?)",
            [(s, i) for s, i in self._index.items() if lo <= s <= hi],
        )
        self._con.execute("CREATE OR REPLACE TEMP TABLE cap_tier_want (session DATE)")
        self._con.executemany("INSERT INTO cap_tier_want VALUES (?)", [(s,) for s in sessions])
        rows = self._con.execute(
            f"""
            WITH bars AS (
                SELECT p.isin, c.idx, p.trade_date,
                       p.close * p.total_traded_qty AS traded
                FROM {self._VIEW} p JOIN cap_tier_cal c ON p.trade_date = c.session
                WHERE p.exchange = 'NSE' AND p.series = 'EQ' AND p.close > 0
                  AND p.total_traded_qty > 0 AND p.isin LIKE 'INE%'
                  AND p.trade_date BETWEEN $lo AND $hi
            ),
            windowed AS (
                SELECT isin, trade_date,
                       quantile_disc(traded, 0.5) OVER (
                           PARTITION BY isin ORDER BY idx
                           RANGE BETWEEN {SIZE_LOOKBACK_SESSIONS - 1} PRECEDING AND CURRENT ROW
                       ) AS size
                FROM bars
            )
            SELECT w.trade_date, w.isin, w.size
            FROM windowed w JOIN cap_tier_want q ON w.trade_date = q.session
            """,
            {"lo": lo, "hi": hi},
        ).fetchall()
        out: dict[date, dict[str, Decimal]] = {}
        for session, isin, size in rows:
            if not is_ranked_equity(str(isin)):
                continue
            day = out.setdefault(session, {})
            if isin in day:
                raise ValueError(f"{isin} prints twice in NSE EQ on {session.isoformat()}")
            day[str(isin)] = Decimal(size)
        return out

    def tiers(self, as_of: date) -> Dataset[TierMembership]:
        if as_of not in self._tiers:
            raise KeyError(f"cap tiers were not loaded for {as_of.isoformat()}")
        return Dataset.declaring(
            f"cap_tiers@{as_of.isoformat()}",
            self._tiers[as_of],
            knowable_date=lambda record: record.knowable_date,
        )
