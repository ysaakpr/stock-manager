"""The unsourced-step price quarantine (M13.4, D22): no bar before the step reaches a decision.

EIH's adjusted series falls x0.223 on 2006-09-12 with no factor behind it (the second, unstated
event beside its 1:2 bonus). The quarantine drops every EIH bar dated *before* 2006-09-12 from
the decision path (`QueryService`) and the backtest's readers (`backtest.run._L1Reader`), and
keeps the step session and everything after. These tests fail if the quarantine is removed (the
pre-step bar comes back) or inverted (the post-step bars go instead), and a control ISIN shows
nothing else is touched. Offline: the lake is written under `tmp_path`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from backtest.run import _L1Reader
from dataplatform.corpactions import ManualActions, load_manual_actions
from dataplatform.corpactions.factors import FactorChain, build_chain_for_isin
from dataplatform.corpactions.taxonomy import ActionType, RatioTerms
from dataplatform.identity.master import Exchange
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.ingest.models import PriceRow
from dataplatform.query import (
    AdjustedSeriesRequest,
    CrossSectionRequest,
    PriceQuarantine,
    QueryService,
    default_price_quarantine,
)
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import materialize_isin

EIH = "INE230A01023"
CONTROL = "INE002A01018"
STEP = date(2006, 9, 12)
BEFORE, AFTER = date(2006, 9, 11), date(2006, 9, 13)

#: `(isin, day, close, qty)`, NSE EQ. EIH's closes are L1's own; CONTROL is any unquarantined name.
_BARS: tuple[tuple[str, date, str, int], ...] = (
    (EIH, date(2006, 9, 8), "815.7000", 133582),
    (EIH, BEFORE, "812.1000", 163827),
    (EIH, STEP, "120.8500", 3148158),
    (EIH, AFTER, "117.9500", 971158),
    (CONTROL, date(2006, 9, 8), "1000.0000", 1000),
    (CONTROL, BEFORE, "1001.0000", 1000),
    (CONTROL, STEP, "1002.0000", 1000),
    (CONTROL, AFTER, "1003.0000", 1000),
)

_QUARANTINE = PriceQuarantine(first_sessions={EIH: STEP})


# ── the rule itself ────────────────────────────────────────────────────────────────────────────


def test_the_quarantine_withholds_only_the_bars_before_the_step() -> None:
    assert not _QUARANTINE.admits(EIH, BEFORE)
    assert _QUARANTINE.admits(EIH, STEP)  # the first bar of the new basis is kept
    assert _QUARANTINE.admits(EIH, AFTER)
    assert _QUARANTINE.admits(CONTROL, BEFORE)


def test_the_repo_quarantine_is_the_curated_files_three_unsourced_steps() -> None:
    expected = {
        "INE230A01023": date(2006, 9, 12),
        "INE640C01011": date(2006, 2, 13),
        "INE780C01023": date(2008, 9, 8),
    }
    assert dict(default_price_quarantine().first_sessions) == expected
    assert (
        dict(PriceQuarantine.from_manual_actions(load_manual_actions()).first_sessions) == expected
    )
    # a market move is a real return and is never quarantined
    assert "INE043A01012" not in default_price_quarantine().first_sessions
    empty = PriceQuarantine.from_manual_actions(ManualActions(actions=(), explained_moves=()))
    assert empty.sql_admits() == "TRUE"


def test_the_sql_predicate_agrees_with_admits() -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE bars(isin VARCHAR, trade_date DATE)")
    con.executemany("INSERT INTO bars VALUES (?, ?)", [(i, d) for i, d, _, _ in _BARS])
    kept = con.execute(
        f"SELECT isin, trade_date FROM bars WHERE {_QUARANTINE.sql_admits()} ORDER BY 1, 2"
    ).fetchall()
    assert kept == sorted((i, d) for i, d, _, _ in _BARS if _QUARANTINE.admits(i, d))
    assert (EIH, BEFORE) not in kept and (EIH, STEP) in kept


def test_a_malformed_key_is_refused() -> None:
    with pytest.raises(ValueError, match="not an ISIN"):
        PriceQuarantine(first_sessions={"EIH' OR TRUE --": STEP})


# ── the decision path: QueryService ────────────────────────────────────────────────────────────


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    by_date: dict[date, list[PriceRow]] = {}
    for isin, day, close, qty in _BARS:
        price = Decimal(close)
        by_date.setdefault(day, []).append(
            PriceRow(
                isin=isin,
                symbol=isin,
                series="EQ",
                trade_date=day,
                open=price,
                high=price,
                low=price,
                close=price,
                last=price,
                prev_close=price,
                total_traded_qty=qty,
                total_traded_value=price * qty,
                total_trades=1,
            )
        )
    for rows in by_date.values():
        write_prices_raw(rows, exchange=Exchange.NSE, data_root=tmp_path)
    bonus = CorporateAction(
        isin=EIH,
        ex_date=STEP,
        action_type=ActionType.BONUS,
        terms=RatioTerms(new_shares=Decimal(1), held_shares=Decimal(2)),
        source="bse_corp_actions",
        raw_text="Bonus issue 1:2",
        knowable_date=STEP,
    )
    materialize_isin(
        EIH,
        chain=build_chain_for_isin(EIH, (bonus,)),
        actions=(bonus,),
        data_root=tmp_path,
        history_isins=(EIH,),
    )
    materialize_isin(
        CONTROL,
        chain=FactorChain(isin=CONTROL, rows=()),
        actions=(),
        data_root=tmp_path,
        history_isins=(CONTROL,),
    )
    return tmp_path


def _series_dates(lake: Path, quarantine: PriceQuarantine, isin: str) -> list[date]:
    with QueryService(data_root=lake, quarantine=quarantine) as service:
        series = service.adjusted_series(AdjustedSeriesRequest(isin=isin))
    return [p.trade_date for p in series.points]


def _cross_isins(lake: Path, quarantine: PriceQuarantine, day: date) -> set[str]:
    with QueryService(data_root=lake, quarantine=quarantine) as service:
        cross = service.cross_section(
            CrossSectionRequest(
                trade_date=day, primary_by_isin={EIH: Exchange.NSE, CONTROL: Exchange.NSE}
            )
        )
    return {row.isin for row in cross.rows}


def test_the_adjusted_series_starts_at_the_step(lake: Path) -> None:
    assert _series_dates(lake, _QUARANTINE, EIH) == [STEP, AFTER]
    # removed: the pre-step bars, on the far side of the phantom x0.223, are back
    assert _series_dates(lake, PriceQuarantine(), EIH)[:2] == [date(2006, 9, 8), BEFORE]
    assert len(_series_dates(lake, _QUARANTINE, CONTROL)) == 4


def test_the_cross_section_has_no_quarantined_bar_before_the_step(lake: Path) -> None:
    assert _cross_isins(lake, _QUARANTINE, BEFORE) == {CONTROL}
    assert _cross_isins(lake, _QUARANTINE, STEP) == {EIH, CONTROL}
    assert _cross_isins(lake, _QUARANTINE, AFTER) == {EIH, CONTROL}
    assert _cross_isins(lake, PriceQuarantine(), BEFORE) == {EIH, CONTROL}


def test_the_query_service_quarantines_by_default(lake: Path) -> None:
    with QueryService(data_root=lake) as service:
        series = service.adjusted_series(AdjustedSeriesRequest(isin=EIH))
    assert series.first == STEP


# ── the backtest universe: _L1Reader (universe, signal sizing, fills, marks, liquidity) ─────────


@pytest.fixture
def reader(lake: Path) -> Iterator[_L1Reader]:
    r = _L1Reader(data_root=lake, quarantine=_QUARANTINE)
    yield r
    r.close()


def test_no_close_fill_bar_or_liquidity_before_the_step(reader: _L1Reader) -> None:
    assert EIH not in reader.closes_on(BEFORE)
    assert EIH not in reader.reference_bars_on(BEFORE)
    assert EIH not in reader.most_liquid_on(BEFORE, 10)
    assert EIH not in reader.median_turnover_over(date(2006, 9, 1), BEFORE)
    assert EIH not in reader.last_prints(BEFORE)
    for day in (STEP, AFTER):
        assert EIH in reader.closes_on(day)
        assert EIH in reader.reference_bars_on(day)
    assert CONTROL in reader.closes_on(BEFORE)


def test_the_listing_window_opens_on_the_step(reader: _L1Reader) -> None:
    windows = {w.isin: w for w in reader.listing_windows()}
    assert windows[EIH].listed_from == STEP  # inverted, it would open 2006-09-08 and close early
    assert windows[CONTROL].listed_from == date(2006, 9, 8)
    # the market calendar is every name's, not EIH's: no session disappears
    assert reader.all_sessions() == (date(2006, 9, 8), BEFORE, STEP, AFTER)


def test_without_the_quarantine_the_reader_would_see_the_pre_step_bar(lake: Path) -> None:
    """The control for the tests above: the fixture really does hold an EIH bar before the step."""
    r = _L1Reader(data_root=lake, quarantine=PriceQuarantine())
    try:
        assert r.closes_on(BEFORE)[EIH] == Decimal("812.1000")
        assert {w.isin: w for w in r.listing_windows()}[EIH].listed_from == date(2006, 9, 8)
    finally:
        r.close()


def test_the_backtest_reader_quarantines_by_default(lake: Path) -> None:
    r = _L1Reader(data_root=lake)
    try:
        assert EIH not in r.closes_on(BEFORE) and EIH in r.closes_on(STEP)
    finally:
        r.close()


def test_a_cold_lake_still_registers_the_quarantined_views(tmp_path: Path) -> None:
    con = duckdb.connect()
    _QUARANTINE.register_raw_view(con, view="raw_q", data_root=tmp_path)
    _QUARANTINE.register_adjusted_view(con, view="adj_q", data_root=tmp_path)
    assert con.execute("SELECT count(*) FROM raw_q").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM adj_q").fetchone() == (0,)


def test_the_quarantined_raw_view_drops_the_pre_step_bars(lake: Path) -> None:
    con = duckdb.connect()
    _QUARANTINE.register_raw_view(con, view="raw_q", data_root=lake)
    days = con.execute(
        "SELECT trade_date FROM raw_q WHERE isin = $isin ORDER BY 1", {"isin": EIH}
    ).fetchall()
    assert [d for (d,) in days] == [STEP, AFTER]


# ── lineage: a quarantined ISIN retired into a survivor ────────────────────────────────────────

SURVIVOR = "INE230A01031"  # a hypothetical reissue of EIH's ISIN


def test_with_a_resolver_the_quarantine_follows_the_isin_into_its_survivor() -> None:
    curated = load_manual_actions()
    rekeyed = PriceQuarantine.from_manual_actions(
        curated, survivor_of=lambda isin: SURVIVOR if isin == EIH else isin
    )
    # the survivor's stitched partition holds EIH's pre-step bars under SURVIVOR
    assert not rekeyed.admits(SURVIVOR, BEFORE)
    assert rekeyed.admits(SURVIVOR, STEP)
    assert not rekeyed.admits(EIH, BEFORE)  # and the retired ISIN stays covered in L1
    # without the resolver the survivor is not named — which is why the build refuses below
    assert PriceQuarantine.from_manual_actions(curated).admits(SURVIVOR, BEFORE)


def test_two_windows_on_one_survivor_keep_the_later_step() -> None:
    curated = load_manual_actions()  # EIH 2006-09-12, JM Financial 2008-09-08
    both = PriceQuarantine.from_manual_actions(
        curated,
        survivor_of=lambda isin: SURVIVOR if isin in {EIH, "INE780C01023"} else isin,
    )
    assert both.first_sessions[SURVIVOR] == date(2008, 9, 8)


def test_stitching_a_quarantined_isin_into_an_unnamed_survivor_fails_loud(lake: Path) -> None:
    from dataplatform.store.l2 import QuarantineLineageError

    with pytest.raises(QuarantineLineageError, match=f"re-key .* to the survivor {SURVIVOR}"):
        materialize_isin(
            SURVIVOR,
            chain=FactorChain(isin=SURVIVOR, rows=()),
            actions=(),
            data_root=lake,
            history_isins=(EIH, SURVIVOR),
        )
    # the quarantined ISIN's own partition, and an unrelated stitch, still build
    materialize_isin(
        EIH, chain=FactorChain(isin=EIH, rows=()), actions=(), data_root=lake, history_isins=(EIH,)
    )
    materialize_isin(
        CONTROL,
        chain=FactorChain(isin=CONTROL, rows=()),
        actions=(),
        data_root=lake,
        history_isins=("INE002A01026", CONTROL),
    )
