"""Golden cases for the seven pre-2011 steps the 2006-2011 L2 extension exposes (M13.4).

The l1-widen rehearsal (ops/gates/l1-widen-2026-10-06.md §5) extended L2 back to 2006 and
`quality.l2_continuity` went from 0 to 7 unexplained steps. Each is now a row in
`corpactions/manual_actions.yaml`, sourced from L0. These tests rebuild each step from bars frozen
from L1 (NSE EQ, verbatim) and read the rows from the repo file, so the file is what is tested:

    ISIN          step        raw ratio  row in the file                     class after
    INE314A01017  2011-06-08  x0.493     CMC BONUS 1:1 (Bc030611.csv:366)     no step left
    INE966H01019  2010-04-15  x0.244     Zee News DEMERGER (Bc310310.csv:54)  STRUCTURAL
    INE133B01019  2010-05-13  x0.441     Kesar Ent DEMERGER (Bc060510.csv:89) STRUCTURAL
    INE043A01012  2011-06-20  x0.378     GTL MARKET_MOVE (Pd200611.csv:567)   EXPLAINED_MOVE
    INE640C01011  2006-02-13  x0.487     Shah Alloys UNSOURCED_PRICE_STEP     UNSOURCED (WARN)
    INE230A01023  2006-09-12  x0.223 *   EIH UNSOURCED_ACTION                 UNSOURCED (WARN)
    INE780C01023  2008-09-08  x0.120 *   JM Financial UNSOURCED_ACTION        UNSOURCED (WARN)

    * after the bonus the BSE feed already carries (EIH 1:2, JM Financial 3:2), which is applied.

CMC is the one price factor. ``price_factor = 1/2`` before 2011-06-08:

    2011-06-06 (pre)    2524.85 x 0.5 = 1262.4250
    2011-06-07 (pre)    2483.60 x 0.5 = 1241.8000
    2011-06-08 (ex)     1223.80 x 1   = 1223.8000   (ex-day -1.45% after the bonus)

Invert the factor (x2) and 2011-06-07 reads 4,967.20 and the step grows to 4.06x: the inversion
guard below builds exactly that and asserts the continuity check fails. The UNSOURCED_* rows
apply no factor at all — EIH's 2006-09-11 stays at the feed bonus's 812.10 x 2/3 = 541.4000 — which
is what "no ratio invented" means here. Offline: L1 is written under `tmp_path`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.corpactions.factors import FactorChain, build_chain_for_isin
from dataplatform.corpactions.manual_actions import ManualActions, load_manual_actions
from dataplatform.corpactions.taxonomy import ActionType, FaceValueTerms, RatioTerms
from dataplatform.identity.master import Exchange
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.ingest.models import PriceRow
from dataplatform.quality.l2_continuity import StepClass, findings, scan
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import materialize_isin, read_adjusted

CMC = "INE314A01017"
ZEENEWS = "INE966H01019"
KESARENT = "INE133B01019"
GTL = "INE043A01012"
SHAHALLOYS = "INE640C01011"
EIH = "INE230A01023"
JMFIN = "INE780C01023"

#: `(isin, trade_date, open, high, low, close, total_traded_qty)`, NSE EQ, frozen from L1.
_BARS: tuple[tuple[str, str, str, str, str, str, int], ...] = (
    (CMC, "2011-06-06", "2549.0000", "2577.6000", "2482.0000", "2524.8500", 82172),
    (CMC, "2011-06-07", "2556.0000", "2560.0000", "2461.3500", "2483.6000", 86204),
    (CMC, "2011-06-08", "1250.0000", "1250.0000", "1217.1500", "1223.8000", 19959),
    (CMC, "2011-06-09", "1223.0500", "1280.0000", "1223.0500", "1261.1500", 59909),
    (ZEENEWS, "2010-04-12", "72.0000", "76.4000", "71.5000", "75.8000", 3994398),
    (ZEENEWS, "2010-04-13", "78.0000", "78.0000", "74.6000", "75.3500", 3671174),
    (ZEENEWS, "2010-04-15", "38.9000", "38.9000", "11.3000", "18.3500", 132090996),
    (ZEENEWS, "2010-04-16", "18.5000", "18.7000", "17.4500", "17.9000", 19161022),
    (KESARENT, "2010-05-11", "129.7000", "131.9500", "125.6000", "127.6500", 24037),
    (KESARENT, "2010-05-12", "129.9000", "133.3000", "127.3500", "129.7000", 32005),
    (KESARENT, "2010-05-13", "84.4000", "84.4000", "49.0000", "57.2500", 508706),
    (KESARENT, "2010-05-14", "55.0000", "62.0000", "55.0000", "58.0500", 160332),
    (GTL, "2011-06-16", "404.9500", "411.5000", "398.5500", "408.0000", 92902),
    (GTL, "2011-06-17", "405.0000", "407.9500", "314.5000", "338.3000", 1188289),
    (GTL, "2011-06-20", "307.8500", "328.8000", "124.1500", "127.9500", 51153620),
    (GTL, "2011-06-21", "125.0000", "148.7000", "121.0000", "123.3000", 35218173),
    (SHAHALLOYS, "2006-02-08", "335.0000", "340.0000", "334.8000", "337.8000", 8412),
    (SHAHALLOYS, "2006-02-10", "340.0000", "359.0000", "339.9000", "347.8500", 78074),
    (SHAHALLOYS, "2006-02-13", "176.0000", "177.0000", "161.5000", "169.3500", 16975),
    (SHAHALLOYS, "2006-02-14", "170.5000", "179.7500", "170.0000", "174.5000", 35149),
    (EIH, "2006-09-08", "779.7000", "820.0000", "775.0000", "815.7000", 133582),
    (EIH, "2006-09-11", "820.0000", "855.1000", "802.0000", "812.1000", 163827),
    (EIH, "2006-09-12", "124.7000", "124.7000", "111.0000", "120.8500", 3148158),
    (EIH, "2006-09-13", "123.0000", "123.9500", "117.2500", "117.9500", 971158),
    (JMFIN, "2008-09-04", "1370.0000", "1469.0000", "1351.0000", "1453.9000", 31689),
    (JMFIN, "2008-09-05", "1436.0000", "1480.0000", "1385.0000", "1402.2000", 35984),
    (JMFIN, "2008-09-08", "66.0000", "67.3500", "61.9000", "67.3500", 166230),
    (JMFIN, "2008-09-09", "70.0000", "79.3500", "69.4000", "71.4500", 3341483),
)

#: `isin -> (step session, the class the curated file gives it)`; None: the factor removes it.
_STEPS: dict[str, tuple[date, StepClass | None]] = {
    CMC: (date(2011, 6, 8), None),
    ZEENEWS: (date(2010, 4, 15), StepClass.STRUCTURAL),
    KESARENT: (date(2010, 5, 13), StepClass.STRUCTURAL),
    GTL: (date(2011, 6, 20), StepClass.EXPLAINED_MOVE),
    SHAHALLOYS: (date(2006, 2, 13), StepClass.UNSOURCED),
    EIH: (date(2006, 9, 12), StepClass.UNSOURCED),
    JMFIN: (date(2008, 9, 8), StepClass.UNSOURCED),
}


def _feed_bonus(isin: str, day: date, new: int, held: int, knowable: date) -> CorporateAction:
    return CorporateAction(
        isin=isin,
        ex_date=day,
        action_type=ActionType.BONUS,
        terms=RatioTerms(new_shares=Decimal(new), held_shares=Decimal(held)),
        source="bse_corp_actions",
        raw_text=f"Bonus issue {new}:{held}",
        knowable_date=knowable,
    )


#: The bonuses the BSE feed already stores on the EIH and JM Financial ex-dates (applied).
_RECORDED: dict[str, tuple[CorporateAction, ...]] = {
    EIH: (_feed_bonus(EIH, date(2006, 9, 12), 1, 2, date(2006, 9, 12)),),
    JMFIN: (_feed_bonus(JMFIN, date(2008, 9, 8), 3, 2, date(2008, 9, 8)),),
}

_NOTHING_CURATED = ManualActions(actions=(), explained_moves=())


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
    recorded = _RECORDED.get(isin, ())
    materialize_isin(
        isin,
        chain=build_chain_for_isin(isin, recorded) if recorded else FactorChain(isin=isin, rows=()),
        actions=recorded,
        data_root=lake,
        history_isins=(isin,),
        curated=curated,
    )


def _adjusted(lake: Path, isin: str) -> dict[date, Decimal]:
    return {b.trade_date: b.adj_close for b in read_adjusted(isin, data_root=lake)}


@pytest.mark.parametrize("isin", list(_STEPS))
def test_each_step_is_unexplained_without_the_file_and_explained_with_it(
    lake: Path, isin: str
) -> None:
    day, expected = _STEPS[isin]
    _build(lake, isin, curated=())
    without = scan(None, survivor_of=lambda i: i, data_root=lake, curated=_NOTHING_CURATED)
    assert [(s.isin, s.trade_date) for s in without.unexplained] == [(isin, day)]

    _build(lake, isin)
    report = scan(None, survivor_of=lambda i: i, data_root=lake)
    assert report.passed, [(s.isin, s.trade_date) for s in report.unexplained]
    classes = {(s.isin, s.trade_date): cls for s, cls in report.steps}
    if expected is None:
        assert (isin, day) not in classes  # the factor removed the step altogether
    else:
        assert classes[(isin, day)] is expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [("2011-06-06", "1262.4250"), ("2011-06-07", "1241.8000"), ("2011-06-08", "1223.8000")],
)
def test_cmc_adjusted_closes_match_hand_computed(lake: Path, day: str, expected: str) -> None:
    _build(lake, CMC)
    assert _adjusted(lake, CMC)[date.fromisoformat(day)] == Decimal(expected)


def test_cmc_bonus_leaves_an_ordinary_ex_day() -> None:
    """The ratio fits the step: 1223.80 against 2483.60 x 1/2 is -1.45%, not a share-basis jump."""
    [row] = [a for a in load_manual_actions().actions_for(CMC) if a.is_price_event]
    assert row.terms == RatioTerms(new_shares=Decimal(1), held_shares=Decimal(1))
    residual = Decimal("1223.80") / (Decimal("2483.60") * Decimal(1) / Decimal(2))
    assert Decimal("0.95") < residual < Decimal("1.05")


def test_an_inverted_cmc_factor_fails_the_check(lake: Path) -> None:
    """Applying the bonus the wrong way (x2 instead of x1/2) leaves a 4x step — and is caught."""
    [row] = [a for a in load_manual_actions().actions_for(CMC) if a.is_price_event]
    inverted = CorporateAction(
        isin=CMC,
        ex_date=row.ex_date,
        action_type=ActionType.SPLIT,
        # a 1 -> 2 consolidation halves the share count: price factor 2, the bonus's reciprocal
        terms=FaceValueTerms(from_value=Decimal(1), to_value=Decimal(2)),
        source=row.as_action().source,
        raw_text="inverted on purpose",
        knowable_date=row.knowable_date,
    )
    _build(lake, CMC, curated=(inverted,))
    adjusted = _adjusted(lake, CMC)
    assert adjusted[date(2011, 6, 7)] == Decimal("4967.2000")
    report = scan(None, survivor_of=lambda i: i, data_root=lake)
    assert [(s.isin, s.trade_date) for s in report.unexplained] == [(CMC, date(2011, 6, 8))]


@pytest.mark.parametrize(
    ("isin", "day", "expected"),
    [
        (EIH, "2006-09-11", "541.4000"),  # 812.10 x 2/3: the feed bonus only
        (JMFIN, "2008-09-05", "560.8800"),  # 1402.20 x 2/5: the feed bonus only
        (SHAHALLOYS, "2006-02-10", "347.8500"),  # nothing at all
        (GTL, "2011-06-17", "338.3000"),  # a market move is never adjusted
    ],
)
def test_an_acknowledged_step_scales_nothing(
    lake: Path, isin: str, day: str, expected: str
) -> None:
    _build(lake, isin)
    assert _adjusted(lake, isin)[date.fromisoformat(day)] == Decimal(expected)


def test_the_unsourced_rows_are_the_three_without_stated_terms() -> None:
    kinds = {m.isin: m.kind for m in load_manual_actions().explained_moves}
    assert {i for i, k in kinds.items() if k == "UNSOURCED_ACTION"} == {EIH, JMFIN}
    assert kinds[SHAHALLOYS] == "UNSOURCED_PRICE_STEP"  # price-only evidence: its own code
    assert kinds[GTL] == "MARKET_MOVE"


@pytest.mark.parametrize("isin", [SHAHALLOYS, EIH, JMFIN])
def test_each_unsourced_step_raises_a_warn_finding_on_its_session(lake: Path, isin: str) -> None:
    day, _ = _STEPS[isin]
    _build(lake, isin)
    report = scan(None, survivor_of=lambda i: i, data_root=lake)
    warns = [f for f in findings(report, logical_date=day) if f.isin == isin]
    assert [(f.severity, f.logical_date, f.detail["kind"]) for f in warns] == [
        ("WARN", day, "unsourced_action_step")
    ]
