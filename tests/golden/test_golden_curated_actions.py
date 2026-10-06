"""Golden cases for curated corporate actions — sourced events no feed carries, adjusted by hand.

Neither CA feed carries these events, and the price detector (`corpactions.implied`) rightly
refuses them: the ex-day moved off a clean multiple, or two events share the ex-date. Their terms
are transcribed into `corpactions/manual_actions.yaml` from NSE's own book-closure file (Bc*.csv in
L0), and `store.l2.materialize_isin` composes them into the chain. The bars below are frozen from
L1 (NSE EQ, verbatim); the expected adjusted closes are literals worked out by hand from the
published terms — and the test reads the terms from the repo file, so the file is what is tested:

MINDTREE (INE018I01017), BONUS 1:1, ex 2016-03-09 (Bc010316.csv:15). ``price_factor = 1/2``:

    2016-03-04 (pre)    1589.90 x 0.5 =  794.9500
    2016-03-08 (pre)    1546.35 x 0.5 =  773.1750
    2016-03-09 (ex)      684.30 x 1   =  684.3000

DPSC (INE360C01024, ex-date bars under INE360C01016), BONUS 22:1 and SPLIT Rs 10 -> Re 1 on one
ex-date, 2011-12-15 (Bc071211.csv:67). ``price_factor = 1/23 x 1/10 = 1/230``:

    2011-12-13 (pre)    2557.15 / 230 =   11.1180   (11.11804...)
    2011-12-14 (pre)    2623.55 / 230 =   11.4067   (11.40673...)
    2011-12-15 (ex)       12.50 x 1   =   12.5000

KTIL (INE096L01025, ex-date bars under INE096L01017), the feed's BONUS 1:25 plus the curated
SPLIT Rs 10 -> Rs 5 on the same ex-date, 2016-08-11 (Bc080816.csv:616).
``price_factor = 25/26 x 5/10 = 25/52``:

    2016-08-09 (pre)     540.45 x 25/52 = 259.8317  (259.83173...)
    2016-08-10 (pre)     542.90 x 25/52 = 261.0096  (261.00961...)
    2016-08-11 (ex)      266.50 x 1     = 266.5000

AXISNIFTY (INF846K01ZL0), unit SPLIT Rs 100 -> Rs 10, ex 2020-07-23 (Bc160720.csv:355): the ex-day
printed at the +20% band, so the step is 8.33x and no clean multiple. ``price_factor = 1/10``:

    2020-07-21 (pre)    1136.50 x 0.1 =  113.6500
    2020-07-22 (pre)    1139.80 x 0.1 =  113.9800
    2020-07-23 (ex)      136.80 x 1   =  136.8000

Invert the convention and MINDTREE's 2016-03-08 reads 3,092.70, DPSC's 2011-12-14 603,416.50,
KTIL's 2016-08-10 1,129.23, AXISNIFTY's 2020-07-22 11,398.00 — none matches, which is the
inversion guard. Offline: L1 is written under `tmp_path`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.corpactions.factors import FactorChain, build_chain_for_isin
from dataplatform.corpactions.taxonomy import ActionType, RatioTerms
from dataplatform.identity.master import Exchange
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.ingest.models import PriceRow
from dataplatform.quality.l2_continuity import scan
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import materialize_isin, read_adjusted

MINDTREE = "INE018I01017"
DPSC_OLD, DPSC = "INE360C01016", "INE360C01024"
KTIL_OLD, KTIL = "INE096L01017", "INE096L01025"
AXISNIFTY = "INF846K01ZL0"

#: `(isin, trade_date, open, high, low, close, total_traded_qty)`, NSE EQ, frozen from L1.
_BARS: tuple[tuple[str, str, str, str, str, str, int], ...] = (
    (MINDTREE, "2016-03-04", "1577.9000", "1609.0000", "1556.0000", "1589.9000", 818848),
    (MINDTREE, "2016-03-08", "1544.0000", "1605.0000", "1525.0000", "1546.3500", 1346046),
    (MINDTREE, "2016-03-09", "740.0000", "740.0000", "680.0500", "684.3000", 2246769),
    (MINDTREE, "2016-03-10", "694.0000", "695.6500", "666.0000", "670.4500", 1009523),
    (DPSC_OLD, "2011-12-12", "2770.0000", "2790.9000", "2584.0000", "2616.4000", 1259),
    (DPSC_OLD, "2011-12-13", "2602.0000", "2675.0000", "2501.0000", "2557.1500", 1688),
    (DPSC_OLD, "2011-12-14", "2599.0000", "2655.0000", "2470.0000", "2623.5500", 1780),
    (DPSC_OLD, "2011-12-15", "12.5000", "12.5000", "12.5000", "12.5000", 268),
    (DPSC, "2011-12-16", "13.7500", "13.7500", "13.2000", "13.7500", 68713),
    (KTIL_OLD, "2016-08-09", "557.0000", "558.8500", "535.8000", "540.4500", 12072),
    (KTIL_OLD, "2016-08-10", "545.5500", "569.0000", "520.2500", "542.9000", 20535),
    (KTIL_OLD, "2016-08-11", "267.6500", "272.3000", "256.2000", "266.5000", 13287),
    (KTIL, "2016-08-12", "269.9500", "275.7500", "241.0000", "246.3000", 22795),
    (AXISNIFTY, "2020-07-21", "1120.0000", "1155.0000", "1109.0100", "1136.5000", 654),
    (AXISNIFTY, "2020-07-22", "1136.2000", "1144.6000", "1123.1000", "1139.8000", 152),
    (AXISNIFTY, "2020-07-23", "132.8000", "136.8000", "125.3500", "136.8000", 4195),
)

#: The lineage chain each partition is built over (oldest first), as `l2_fill` passes it.
_HISTORY: dict[str, tuple[str, ...]] = {
    MINDTREE: (MINDTREE,),
    DPSC: (DPSC_OLD, DPSC),
    KTIL: (KTIL_OLD, KTIL),
    AXISNIFTY: (AXISNIFTY,),
}

#: `(isin, date, expected adjusted close)` — the literals above.
_EXPECTED: tuple[tuple[str, str, str], ...] = (
    (MINDTREE, "2016-03-04", "794.9500"),
    (MINDTREE, "2016-03-08", "773.1750"),
    (MINDTREE, "2016-03-09", "684.3000"),
    (DPSC, "2011-12-13", "11.1180"),
    (DPSC, "2011-12-14", "11.4067"),
    (DPSC, "2011-12-15", "12.5000"),
    (KTIL, "2016-08-09", "259.8317"),
    (KTIL, "2016-08-10", "261.0096"),
    (KTIL, "2016-08-11", "266.5000"),
    (AXISNIFTY, "2020-07-21", "113.6500"),
    (AXISNIFTY, "2020-07-22", "113.9800"),
    (AXISNIFTY, "2020-07-23", "136.8000"),
)

#: KTIL's 1:25 bonus is in the BSE feed (reconciled); only its split is curated.
_KTIL_BONUS = CorporateAction(
    isin=KTIL,
    ex_date=date(2016, 8, 11),
    action_type=ActionType.BONUS,
    terms=RatioTerms(new_shares=Decimal(1), held_shares=Decimal(25)),
    source="bse_corp_actions",
    raw_text="Bonus issue 1:25",
    knowable_date=date(2016, 8, 8),
)


def _recorded(isin: str) -> tuple[CorporateAction, ...]:
    return (_KTIL_BONUS,) if isin == KTIL else ()


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    by_date: dict[date, list[PriceRow]] = {}
    for isin, day, o, h, low, c, qty in _BARS:
        trade_date = date.fromisoformat(day)
        by_date.setdefault(trade_date, []).append(
            PriceRow(
                isin=isin,
                symbol=isin,
                series="EQ",
                trade_date=trade_date,
                open=Decimal(o),
                high=Decimal(h),
                low=Decimal(low),
                close=Decimal(c),
                last=Decimal(c),
                prev_close=Decimal(c),
                total_traded_qty=qty,
                total_traded_value=Decimal(c) * qty,
                total_trades=1,
            )
        )
    for rows in by_date.values():
        write_prices_raw(rows, exchange=Exchange.NSE, data_root=tmp_path)
    return tmp_path


def _build(lake: Path, isin: str, *, curated: tuple[CorporateAction, ...] | None = None) -> None:
    recorded = _recorded(isin)
    materialize_isin(
        isin,
        chain=build_chain_for_isin(isin, recorded) if recorded else FactorChain(isin=isin, rows=()),
        actions=recorded,
        data_root=lake,
        history_isins=_HISTORY[isin],
        curated=curated,
    )


@pytest.mark.parametrize(("isin", "day", "expected"), _EXPECTED)
def test_adjusted_closes_match_hand_computed(
    lake: Path, isin: str, day: str, expected: str
) -> None:
    _build(lake, isin)
    by_date = {b.trade_date: b for b in read_adjusted(isin, data_root=lake)}
    bar = by_date[date.fromisoformat(day)]
    assert bar.adj_close == Decimal(expected)
    # adj = raw x factor, row by row, with the factor that was actually applied on the row.
    raw = next(Decimal(r[5]) for r in _BARS if r[1] == day and r[0] in _HISTORY[isin])
    assert bar.adj_close == (raw * bar.cum_price_factor).quantize(Decimal("0.0001"))


@pytest.mark.parametrize("isin", list(_HISTORY))
def test_the_curated_file_is_what_clears_the_continuity_check(lake: Path, isin: str) -> None:
    # 1.9x, not the default 2x: KTIL's step with only the feed's bonus applied is 1.96x.
    threshold = Decimal("1.9")
    _build(lake, isin, curated=())
    without = scan(None, survivor_of=lambda i: i, data_root=lake, threshold=threshold)
    assert [s.isin for s in without.unexplained] == [isin]
    _build(lake, isin)
    assert scan(None, survivor_of=lambda i: i, data_root=lake, threshold=threshold).passed
