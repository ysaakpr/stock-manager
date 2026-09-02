"""M4.2 — screen filters (shape c) and the point-in-time universe (shape d) over the D4 lake (§4.5).

Every acceptance criterion of the task is a test here:

  1. a universe as of a past date includes names later delisted and excludes names not yet listed
     (`test_pit_universe_keeps_later_delisted_drops_not_yet_listed`,
      `test_pit_universe_scoped_to_index_history_asof`)
  2. screen filters compose and return correct sets against a hand-verified small case
     (`test_screen_filters_compose_hand_verified`, `test_screen_scoped_to_pit_universe`)
  3. the fundamentals join surface structurally cannot accept a restated source
     (`test_fundamentals_join_refuses_restated_source`,
      `test_pit_fundamentals_filters_future_filings`)

The lake is built through the real seams: raw `prices_raw` L1 partitions under `tmp_path`, adjusted
to L2 by the M2.5 materializer, read back through `QueryService`; constituent history via
M3.9's `write_constituents_l1`. The listing calendar is injected in memory — listing status lives in
the identity master (Postgres), not the Parquet lake, so the survivorship logic is proven offline
here and `store_listing_calendar` is the production adapter (unit-covered by construction). No
postgres, no network, deterministic (B8).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataplatform.corpactions.factors import FactorChain
from dataplatform.identity.master import Exchange, ListingStatus
from dataplatform.ingest.indices import ConstituentRow, ConstituentSnapshot, write_constituents_l1
from dataplatform.query import (
    FundamentalDatum,
    InMemoryListingCalendar,
    ListingWindow,
    PitFundamentals,
    QuarantineError,
    QueryService,
    all_of,
    any_of,
    between,
    fundamental,
    ge,
    gt,
    metric,
    pit_universe,
)
from dataplatform.store.l2 import materialize_isin
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

pytestmark = pytest.mark.integration

_PRICE_Q: Final = Decimal("0.0001")


# ── shared L1 writer (one session, several isin/exchange rows) ─────────────────────────────────


def _write_l1_partition(
    data_root: Path, trade_date: date, rows: list[tuple[str, str, str, Decimal, int]]
) -> None:
    """Write one raw `prices_raw` L1 partition. Rows are (isin, exchange, symbol, close, volume)."""
    records = [
        {
            "isin": isin,
            "exchange": exchange,
            "symbol": symbol,
            "series": "EQ",
            "trade_date": trade_date,
            "open": close.quantize(_PRICE_Q),
            "high": close.quantize(_PRICE_Q),
            "low": close.quantize(_PRICE_Q),
            "close": close.quantize(_PRICE_Q),
            "last": close.quantize(_PRICE_Q),
            "prev_close": close.quantize(_PRICE_Q),
            "total_traded_qty": volume,
            "total_traded_value": (close * volume).quantize(_PRICE_Q),
            "total_trades": volume,
            "deliv_qty": None,
            "deliv_pct": None,
        }
        for isin, exchange, symbol, close, volume in rows
    ]
    path = l1_partition_path(PRICES_RAW_DATASET, trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=PRICES_RAW_SCHEMA)
    pq.write_table(table, path, compression="snappy", version="2.6")


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# acceptance 1 — the point-in-time universe (shape d)
# ═══════════════════════════════════════════════════════════════════════════════════════════════

# Four securities with hand-set listing windows, and one decision date between them.
U_LIVE: Final = "INE111A01011"  # listed 2015, still trading                  → in
U_DELISTED_LATER: Final = "INE222B01012"  # listed 2016, delisted 2024        → in (survivorship)
U_NOT_YET: Final = "INE333C01013"  # first listed 2023                        → out (future)
U_GONE_EARLY: Final = "INE444D01014"  # listed 2015, delisted 2019           → out (already gone)

AS_OF: Final = date(2021, 6, 1)


def _universe_calendar() -> InMemoryListingCalendar:
    """The four listing windows above, as an injected in-memory calendar."""
    return InMemoryListingCalendar(
        (
            ListingWindow(U_LIVE, date(2015, 1, 1), None, ListingStatus.ACTIVE),
            ListingWindow(
                U_DELISTED_LATER, date(2016, 1, 1), date(2024, 3, 15), ListingStatus.DELISTED
            ),
            ListingWindow(U_NOT_YET, date(2023, 7, 1), None, ListingStatus.ACTIVE),
            ListingWindow(U_GONE_EARLY, date(2015, 1, 1), date(2019, 5, 2), ListingStatus.DELISTED),
        )
    )


def test_pit_universe_keeps_later_delisted_drops_not_yet_listed() -> None:
    """The universe as of a past date keeps names delisted *after* it and drops ones not yet listed.

    This is the survivorship-bias killer of §4.5: a name delisted in 2024 was tradeable in 2021 and
    must appear in a 2021 universe; a name first listed in 2023 must not; a name already gone by
    2021 must not. A universe read off today's list alone would get all three wrong.
    """
    universe = pit_universe(AS_OF, _universe_calendar())

    assert universe.as_of == AS_OF
    assert universe.isins == {U_LIVE, U_DELISTED_LATER}
    assert U_DELISTED_LATER in universe  # later-delisted → kept
    assert U_NOT_YET not in universe  # not yet listed → excluded
    assert U_GONE_EARLY not in universe  # already delisted → excluded


def test_pit_universe_boundary_dates() -> None:
    """The listed/delisted boundary is as documented: listed inclusive, delisted exclusive."""
    calendar = InMemoryListingCalendar(
        (ListingWindow(U_LIVE, date(2020, 1, 1), date(2020, 12, 31), ListingStatus.DELISTED),)
    )
    assert U_LIVE in pit_universe(date(2020, 1, 1), calendar)  # listed_from is inclusive
    assert U_LIVE in pit_universe(date(2020, 12, 30), calendar)
    assert U_LIVE not in pit_universe(date(2020, 12, 31), calendar)  # delisting date is exclusive
    assert U_LIVE not in pit_universe(date(2019, 12, 31), calendar)  # before listing


def test_pit_universe_scoped_to_index_history_asof(tmp_path: Path) -> None:
    """Scoping to an index reads the constituent snapshot in force then — not today's list (M3.9).

    Two monthly snapshots accumulate: in 2021 the index held LIVE and DELISTED_LATER; a later 2023
    snapshot dropped DELISTED_LATER and added NOT_YET. A 2021 universe scoped to the index must
    reflect the *2021* membership intersected with who was tradeable then — so DELISTED_LATER is in
    (in the index and trading) and NOT_YET is out (not in the 2021 snapshot, and not yet listed).
    """
    _write_index_snapshot(
        tmp_path, "nifty50", date(2021, 1, 1), [(U_LIVE, "LIVE"), (U_DELISTED_LATER, "DEL")]
    )
    _write_index_snapshot(
        tmp_path, "nifty50", date(2023, 8, 1), [(U_LIVE, "LIVE"), (U_NOT_YET, "NEW")]
    )

    with QueryService(data_root=tmp_path) as svc:
        universe = svc.pit_universe(AS_OF, _universe_calendar(), index_slugs=["nifty50"])

    assert universe.isins == {U_LIVE, U_DELISTED_LATER}
    assert universe.index_slugs == ("nifty50",)


def _write_index_snapshot(
    data_root: Path, slug: str, as_of: date, members: list[tuple[str, str]]
) -> None:
    """Write one immutable monthly constituent snapshot through M3.9's real L1 writer."""
    rows = tuple(
        ConstituentRow(isin=isin, symbol=symbol, series="EQ", company_name=symbol, industry="Test")
        for isin, symbol in sorted(members)
    )
    snapshot = ConstituentSnapshot(
        index_slug=slug,
        index_name=slug.upper(),
        as_of=as_of,
        rows=rows,
        source="nifty_index_constituents",
    )
    write_constituents_l1(snapshot, data_root=data_root)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# acceptance 2 — screen filters compose (shape c), hand-verified
# ═══════════════════════════════════════════════════════════════════════════════════════════════

SCR_DATE: Final = date(2023, 1, 12)

# Five securities on SCR_DATE, each single-listed NSE so the cross-section is one row per ISIN.
# (isin, adj_close, adj_volume, delivery_pct, roe) — the last two joined as flows/fundamentals.
S1: Final = "INE111A01011"  # close 300, vol 50k, deliv 60%, roe 20%
S2: Final = "INE222B01012"  # close 120, vol 90k, deliv 55%, roe  8%
S3: Final = "INE333C01013"  # close 800, vol 40k, deliv 70%, roe 25%
S4: Final = "INE444D01014"  # close 250, vol  5k, deliv 30%, roe 18%
S5: Final = "INE555E01015"  # close 200, vol 70k, deliv 45%, roe  5%

_SCREEN_ROWS: Final = {
    S1: (Decimal("300"), 50_000, Decimal("0.60"), Decimal("0.20")),
    S2: (Decimal("120"), 90_000, Decimal("0.55"), Decimal("0.08")),
    S3: (Decimal("800"), 40_000, Decimal("0.70"), Decimal("0.25")),
    S4: (Decimal("250"), 5_000, Decimal("0.30"), Decimal("0.18")),
    S5: (Decimal("200"), 70_000, Decimal("0.45"), Decimal("0.05")),
}


@pytest.fixture
def screen_lake(tmp_path: Path) -> Path:
    """A lake with the five screen securities' bar on SCR_DATE, materialized to L2."""
    rows = [
        (isin, "NSE", f"SYM{i}", close, volume)
        for i, (isin, (close, volume, _d, _r)) in enumerate(_SCREEN_ROWS.items())
    ]
    _write_l1_partition(tmp_path, SCR_DATE, rows)
    for isin in _SCREEN_ROWS:
        materialize_isin(isin, chain=FactorChain(isin=isin), actions=(), data_root=tmp_path)
    return tmp_path


def _flows() -> dict[str, dict[str, Decimal]]:
    return {isin: {"deliv_pct": d} for isin, (_c, _v, d, _r) in _SCREEN_ROWS.items()}


class _PitRoe:
    """A minimal PIT fundamentals source: one ROE datum per ISIN, all filed pre-SCR_DATE."""

    point_in_time = True

    def data(self) -> Iterable[FundamentalDatum]:
        return [
            FundamentalDatum(isin=isin, metric="roe", value=roe, filing_date=date(2022, 11, 1))
            for isin, (_c, _v, _d, roe) in _SCREEN_ROWS.items()
        ]


def test_screen_filters_compose_hand_verified(screen_lake: Path) -> None:
    """A composed screen returns exactly the hand-computed set over price, flow and fundamental.

    Screen: adj_close in [150, 500] AND adj_volume >= 20k AND (delivery >= 50% OR roe >= 15%).
    Walk it by hand:
      S1 300✓ 50k✓ (60%✓)                → in
      S2 120✗ (out on price)              → out
      S3 800✗ (out on price)              → out
      S4 250✓ 5k✗ (out on volume)        → out
      S5 200✓ 70k✓ (45%✗ or 5%✗ → ✗)     → out
    Only S1. A screen that dropped a clause, flipped a bound, or joined the wrong metric fails this.
    """
    price_band = between(metric("adj_close"), Decimal("150"), Decimal("500"))
    liquid = ge(metric("adj_volume"), Decimal("20000"))
    quality = any_of(
        ge(metric("deliv_pct"), Decimal("0.50")),
        ge(fundamental("roe"), Decimal("0.15")),
    )
    screen = all_of(price_band, liquid, quality)

    fundamentals = PitFundamentals.from_source(_PitRoe(), as_of=SCR_DATE)
    with QueryService(data_root=screen_lake) as svc:
        matched = svc.screen(
            SCR_DATE,
            screen,
            primary_by_isin=dict.fromkeys(_SCREEN_ROWS, Exchange.NSE),
            flows=_flows(),
            fundamentals=fundamentals,
        )

    assert matched == {S1}


def test_screen_composition_operators(screen_lake: Path) -> None:
    """The `&`/`|`/`~` operators compose the same predicates — a different cut, hand-verified.

    Screen: roe >= 15% AND NOT (delivery >= 60%). By hand:
      S1 roe20✓ deliv60✗(NOT)  → out
      S3 roe25✓ deliv70✗(NOT)  → out
      S4 roe18✓ deliv30 ✓(NOT) → in
    Others fail the roe clause. Only S4.
    """
    screen = ge(fundamental("roe"), Decimal("0.15")) & ~ge(metric("deliv_pct"), Decimal("0.60"))
    fundamentals = PitFundamentals.from_source(_PitRoe(), as_of=SCR_DATE)
    with QueryService(data_root=screen_lake) as svc:
        matched = svc.screen(
            SCR_DATE,
            screen,
            primary_by_isin=dict.fromkeys(_SCREEN_ROWS, Exchange.NSE),
            flows=_flows(),
            fundamentals=fundamentals,
        )
    assert matched == {S4}


def test_screen_missing_metric_fails_closed(screen_lake: Path) -> None:
    """A screen over an unresolved metric excludes the name rather than raising or matching it."""
    screen = gt(metric("deliv_pct"), Decimal("0"))  # no flows passed → deliv_pct unknown for all
    with QueryService(data_root=screen_lake) as svc:
        matched = svc.screen(
            SCR_DATE, screen, primary_by_isin=dict.fromkeys(_SCREEN_ROWS, Exchange.NSE)
        )
    assert matched == frozenset()


def test_screen_scoped_to_pit_universe(screen_lake: Path) -> None:
    """Screening scoped to a PIT universe (shape d) never returns a name outside the universe.

    The price band [150, 500] alone matches S1(300), S4(250) and S5(200); narrowing to a universe of
    {S1, S3} (a stand-in PIT set) must intersect it down to {S1} — S4 and S5 are dropped for being
    outside the universe, and S3(800) is dropped by the screen. Proof (c) and (d) compose.
    """
    calendar = InMemoryListingCalendar(
        (
            ListingWindow(S1, date(2015, 1, 1), None, ListingStatus.ACTIVE),
            ListingWindow(S3, date(2015, 1, 1), None, ListingStatus.ACTIVE),
        )
    )
    universe = pit_universe(SCR_DATE, calendar)
    screen = between(metric("adj_close"), Decimal("150"), Decimal("500"))
    with QueryService(data_root=screen_lake) as svc:
        matched = svc.screen(
            SCR_DATE,
            screen,
            primary_by_isin=dict.fromkeys(_SCREEN_ROWS, Exchange.NSE),
            universe=universe.isins,
        )
    assert matched == {S1}  # S4/S5 outside the universe; S3 fails the price band


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# acceptance 3 — the fundamentals join surface accepts only PIT-tagged sources
# ═══════════════════════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class _RestatedScreener:
    """A restated (Screener-style) fundamentals source: monitoring-only, NOT point-in-time."""

    point_in_time: bool = False

    def data(self) -> Iterable[FundamentalDatum]:
        # Note: a restated figure has no honest filing_date; this is here only to prove the source
        # is refused *before* any datum is read.
        return [
            FundamentalDatum(isin=S1, metric="roe", value=Decimal("0.99"), filing_date=SCR_DATE)
        ]


def test_fundamentals_join_refuses_restated_source() -> None:
    """A restated source cannot be turned into a `PitFundamentals` — the structural quarantine (#8).

    `PitFundamentals` is the only type the screen join accepts, and its sole data-carrying
    constructor (`from_source`) raises on a non-PIT source. There is no path from a restated source
    to something the screen will take: the quarantine is enforced by the types, not by a convention
    a caller must remember (invariant #8, §7).
    """
    with pytest.raises(QuarantineError, match="restated"):
        PitFundamentals.from_source(_RestatedScreener())


def test_pit_fundamentals_filters_future_filings() -> None:
    """A PIT source is accepted, and a filing not yet public on `as_of` is dropped (no look-ahead).

    Two ROE filings for S1: one filed before the decision date, one after. Built as-of the decision
    date, only the knowable one survives — the within-join point-in-time guard M4.3 generalises
    (invariant #7).
    """

    class _TwoFilings:
        point_in_time = True

        def data(self) -> Iterable[FundamentalDatum]:
            return [
                FundamentalDatum(S1, "roe", Decimal("0.10"), filing_date=date(2022, 11, 1)),
                FundamentalDatum(S1, "roe", Decimal("0.30"), filing_date=date(2023, 6, 1)),
            ]

    view = PitFundamentals.from_source(_TwoFilings(), as_of=SCR_DATE)
    assert view.metrics_for(S1) == {"roe": Decimal("0.10")}  # the future filing is excluded

    unfiltered = PitFundamentals.from_source(_TwoFilings())
    assert unfiltered.metrics_for(S1) == {"roe": Decimal("0.30")}  # latest wins with no as_of
