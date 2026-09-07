"""M4.1 — the query service's two canonical shapes over the DuckDB/Parquet lake (§4.5).

Every acceptance criterion of the task is a test here:

  1. shape (a) returns a correct multi-year adjusted series for a CA-heavy ISIN
     (`test_adjusted_series_ca_heavy_multi_year`, `test_adjusted_series_window_and_derive_primary`)
  2. shape (b) returns a complete cross-section for a date, both exchanges deduped to primary
     (`test_cross_section_complete_and_deduped_to_primary`,
     `test_cross_section_falls_back_when_primary_dark`)
  3. documented timing on the full store, with the measurement method stated
     (`test_benchmark_full_store_series_and_cross_section`)

The correctness tests build the lake through the *real* seam: raw `prices_raw` L1 partitions under
`tmp_path`, adjusted to L2 by the M2.5 materializer (`materialize_isin`) from an M2.4 factor chain,
then read back through `QueryService`. No postgres, no network, deterministic.

The benchmark (criterion 3) is separate. There is no real 10-year store on this host — the 10-year
backfill is `NEEDS_GO` and parked (AGENTIC_CONTEXT B1), and `data/` is never committed — so the
benchmark synthesises a *full-scale* L2 store and times the two shapes against it.

Benchmark corpus and method
---------------------------
* Corpus: ``BENCH_ISINS`` (=200) ISINs, each with ``BENCH_SESSIONS`` (=2520 ≈ 10 trading years)
  daily bars on **both** NSE and BSE — i.e. ~1.0M adjusted rows across 200 per-ISIN Parquet
  partitions, the same on-disk layout M2.5 materializes. Full 10-year *depth* per name; breadth is
  200 names (NSE lists ≈2000 equities), and the cross-section scan is linear in total rows, so the
  full-breadth store extrapolates by the row-count ratio.
* Metric: wall time via ``time.perf_counter`` around a single ``QueryService`` call, one warm-up
  call discarded (DuckDB view registration / OS page cache), then the **median of 5** timed runs.
* Both shapes are asserted under ``BENCH_BUDGET_S`` (=5.0 s) — a deliberately generous "sane time"
  ceiling that still catches an accidental full-market scan on the per-ISIN series path. The
  measured medians are printed as ``query.benchmark`` and captured into the task's state record.

The CA-heavy ISIN is a 1:5 face-value split then a 1:1 bonus then a cash dividend across four years,
so the earliest segment carries a combined ``0.2 * 0.5 = 0.1`` price factor — a value that flips if
either adjustment is inverted.
"""

from __future__ import annotations

import statistics
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataplatform.corpactions.factors import FactorChain, build_factor_chain
from dataplatform.corpactions.taxonomy import (
    ActionType,
    DividendKind,
    DividendTerms,
    FaceValueTerms,
    RatioTerms,
)
from dataplatform.identity.master import Exchange
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.query import (
    AdjustedSeriesRequest,
    CrossSectionRequest,
    QueryService,
)
from dataplatform.store.l2 import (
    PRICES_ADJUSTED_DATASET,
    PRICES_ADJUSTED_SCHEMA,
    materialize_isin,
)
from dataplatform.store.paths import l1_partition_path, l2_isin_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

pytestmark = pytest.mark.integration

_PRICE_Q: Final = Decimal("0.0001")


# ── shared L1 writer (dual-exchange, unlike test_l2_views' NSE-only helper) ────────────────────


def _write_l1_partition(
    data_root: Path,
    trade_date: date,
    rows: list[tuple[str, str, str, Decimal, int]],
) -> None:
    """Write one raw `prices_raw` L1 partition (one date, several isin/exchange rows).

    Rows are ``(isin, exchange, symbol, close, volume)``. OHLC are all the close (all the L2
    adjuster reads is OHLC x price factor) and ``total_traded_value = close * volume`` — the metric
    primary selection scores exchanges on. Uses the real `PRICES_RAW_SCHEMA` so the DuckDB read
    exercises the actual on-disk contract.
    """
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


# ── acceptance 1: shape (a) — a CA-heavy multi-year adjusted series ─────────────────────────────

CA_ISIN: Final = "INE335Y01020"
SPLIT_EX: Final = date(2021, 10, 19)  # 1:5 face-value split, ₹10 → ₹2  → price factor 0.2
BONUS_EX: Final = date(2022, 8, 1)  # 1:1 bonus                        → price factor 0.5
DIV_EX: Final = date(2023, 3, 1)  # ₹10/share cash dividend

# One raw close of ₹1000 on every session, so the adjusted close *is* the cumulative price factor
# x 1000 and can be read straight off. Sessions span four calendar years around the two price
# events, so the series is genuinely multi-year.
_CA_SESSIONS: Final = [
    date(2019, 6, 3),
    date(2020, 6, 1),
    date(2021, 6, 1),
    SPLIT_EX,
    date(2022, 1, 3),
    BONUS_EX,
    date(2023, 1, 2),
    DIV_EX,
]
_CA_RAW_CLOSE: Final = Decimal("1000")


@pytest.fixture
def ca_lake(tmp_path: Path) -> Path:
    """A lake whose L1 holds the CA-heavy ISIN's four-year daily history on NSE."""
    for session in _CA_SESSIONS:
        _write_l1_partition(tmp_path, session, [(CA_ISIN, "NSE", "IRCTC", _CA_RAW_CLOSE, 500)])
    return tmp_path


@pytest.fixture
def ca_actions() -> tuple[CorporateAction, ...]:
    """The CA-heavy ISIN's reconciled actions: 1:5 split, 1:1 bonus, then a ₹10 cash dividend."""
    split = CorporateAction(
        isin=CA_ISIN,
        ex_date=SPLIT_EX,
        action_type=ActionType.SPLIT,
        terms=FaceValueTerms(from_value=Decimal("10"), to_value=Decimal("2")),
        source="nse_corp_actions",
        raw_text="FV SPLIT FROM RS.10/- TO RS.2/-",
        knowable_date=SPLIT_EX,
    )
    bonus = CorporateAction(
        isin=CA_ISIN,
        ex_date=BONUS_EX,
        action_type=ActionType.BONUS,
        terms=RatioTerms(new_shares=Decimal("1"), held_shares=Decimal("1")),
        source="nse_corp_actions",
        raw_text="BONUS 1:1",
        knowable_date=BONUS_EX,
    )
    dividend = CorporateAction(
        isin=CA_ISIN,
        ex_date=DIV_EX,
        action_type=ActionType.DIVIDEND,
        terms=DividendTerms(dividend_kind=DividendKind.FINAL, amount_inr=Decimal("10")),
        source="nse_corp_actions",
        raw_text="DIVIDEND RS.10 PER SHARE",
        knowable_date=DIV_EX,
    )
    return (split, bonus, dividend)


@pytest.fixture
def ca_chain(ca_actions: tuple[CorporateAction, ...]) -> FactorChain:
    """The price/qty factor chain M2.4 builds from the CA-heavy ISIN's actions."""
    return build_factor_chain(ca_actions)


def test_adjusted_series_ca_heavy_multi_year(
    ca_lake: Path, ca_chain: FactorChain, ca_actions: tuple[CorporateAction, ...]
) -> None:
    """Shape (a) returns the whole four-year series, ordered, with the right combined adjustment.

    Two price events compound: before both, the ₹1000 close reads at 1000 x 0.2 x 0.5 = ₹100;
    between the split ex-date and the bonus ex-date only the 0.5 bonus factor is still ahead, so
    ₹500; on and after the bonus ex-date the newest segment is unscaled, ₹1000. A test that fails if
    either adjustment is dropped or inverted (invariant #3 correctness).
    """
    materialize_isin(CA_ISIN, chain=ca_chain, actions=ca_actions, data_root=ca_lake)

    with QueryService(data_root=ca_lake) as svc:
        series = svc.adjusted_series(AdjustedSeriesRequest(isin=CA_ISIN, primary=Exchange.NSE))

    # Complete and in trade-date order across the four years.
    assert [p.trade_date for p in series.points] == _CA_SESSIONS
    assert series.first == _CA_SESSIONS[0]
    assert series.last == _CA_SESSIONS[-1]
    assert series.primary is Exchange.NSE
    assert all(p.isin == CA_ISIN for p in series.points)
    assert all(p.exchange is Exchange.NSE and not p.fell_back for p in series.points)

    adj = {p.trade_date: p.adj_close for p in series.points}
    assert adj[date(2019, 6, 3)] == Decimal("100")  # 1000 x 0.2 x 0.5
    assert adj[date(2021, 6, 1)] == Decimal("100")
    assert adj[SPLIT_EX] == Decimal("500")  # split behind, only the 1:1 bonus ahead
    assert adj[date(2022, 1, 3)] == Decimal("500")
    assert adj[BONUS_EX] == Decimal("1000")  # newest segment, unscaled
    assert adj[DIV_EX] == Decimal("1000")

    # The cumulative price factor carried on the row explains each level.
    cum = {p.trade_date: p.cum_price_factor for p in series.points}
    assert cum[date(2019, 6, 3)] == Decimal("0.1")
    assert cum[SPLIT_EX] == Decimal("0.5")
    assert cum[BONUS_EX] == Decimal("1")


def test_adjusted_series_window_and_derive_primary(
    ca_lake: Path, ca_chain: FactorChain, ca_actions: tuple[CorporateAction, ...]
) -> None:
    """A windowed request keeps only in-window sessions, and the primary derives from L1 liquidity.

    No `primary=` is pinned here, so the service must name the primary itself (there is only NSE
    liquidity → NSE) without any future data leaking in (invariant #7).
    """
    materialize_isin(CA_ISIN, chain=ca_chain, actions=ca_actions, data_root=ca_lake)

    with QueryService(data_root=ca_lake) as svc:
        series = svc.adjusted_series(
            AdjustedSeriesRequest(isin=CA_ISIN, start=date(2021, 1, 1), end=date(2022, 6, 30))
        )

    assert series.primary is Exchange.NSE
    assert [p.trade_date for p in series.points] == [
        date(2021, 6, 1),
        SPLIT_EX,
        date(2022, 1, 3),
    ]


def test_adjusted_series_absent_isin_is_empty_not_error(ca_lake: Path) -> None:
    """An ISIN with no materialized L2 partition yields an empty series — a gap, not a crash."""
    with QueryService(data_root=ca_lake) as svc:
        series = svc.adjusted_series(
            AdjustedSeriesRequest(isin="INE000000019", primary=Exchange.NSE)
        )
    assert series.points == ()
    assert series.first is None and series.last is None


# ── acceptance 2: shape (b) — a complete cross-section, deduped to primary ──────────────────────

XS_DATE: Final = date(2023, 1, 12)
_XS_WINDOW: Final = [XS_DATE - timedelta(days=n) for n in range(6, 0, -1)] + [XS_DATE]

# Three securities. A: dual-listed, NSE the more liquid → NSE primary, both print on XS_DATE.
# B: dual-listed, NSE the more liquid → NSE primary, but NSE is dark on XS_DATE → BSE falls back.
# C: single-listed NSE only.
XS_A: Final = "INE111A01011"
XS_B: Final = "INE222B01012"
XS_C: Final = "INE333C01013"


@pytest.fixture
def xs_lake(tmp_path: Path) -> Path:
    """A lake whose L1 holds a window of sessions for three securities, materialized to L2."""
    for session in _XS_WINDOW:
        rows: list[tuple[str, str, str, Decimal, int]] = []
        # A: NSE more liquid on every session (higher volume → higher turnover); both print always.
        rows.append((XS_A, "NSE", "AAA", Decimal("300"), 10_000))
        rows.append((XS_A, "BSE", "AAA", Decimal("300"), 1_000))
        # B: NSE more liquid across the window, but NSE does not print on XS_DATE itself.
        if session != XS_DATE:
            rows.append((XS_B, "NSE", "BBB", Decimal("200"), 8_000))
        rows.append((XS_B, "BSE", "BBB", Decimal("200"), 500))
        # C: NSE only.
        rows.append((XS_C, "NSE", "CCC", Decimal("150"), 4_000))
        _write_l1_partition(tmp_path, session, rows)

    for isin in (XS_A, XS_B, XS_C):
        materialize_isin(isin, chain=FactorChain(isin=isin), actions=(), data_root=tmp_path)
    return tmp_path


def test_cross_section_complete_and_deduped_to_primary(xs_lake: Path) -> None:
    """Shape (b) returns exactly one row per ISIN that traded, each on its primary exchange.

    Both A's NSE and BSE bars exist for XS_DATE; the cross-section must collapse them to A's primary
    (NSE, the more liquid) — never a double count. The result is complete: every ISIN that printed
    on the date is present, once.
    """
    with QueryService(data_root=xs_lake) as svc:
        xs = svc.cross_section(CrossSectionRequest(trade_date=XS_DATE))

    assert xs.trade_date == XS_DATE
    # Complete: one row per ISIN that traded, no double count of the dual-listed names.
    assert xs.isins == {XS_A, XS_B, XS_C}
    assert len(xs.rows) == 3

    by_isin = {r.isin: r for r in xs.rows}
    # A: primary NSE, printed on NSE → NSE row, no fallback.
    assert by_isin[XS_A].primary is Exchange.NSE
    assert by_isin[XS_A].exchange is Exchange.NSE
    assert by_isin[XS_A].fell_back is False
    assert by_isin[XS_A].adj_close == Decimal("300")
    # C: single-listed NSE.
    assert by_isin[XS_C].exchange is Exchange.NSE
    assert by_isin[XS_C].fell_back is False


def test_cross_section_falls_back_when_primary_dark(xs_lake: Path) -> None:
    """When the primary did not print that session, the other exchange's bar shows, flagged.

    B's primary is NSE by window liquidity, but NSE is dark on XS_DATE; the row must be BSE's bar
    with `fell_back=True` — a visible fact, never a silent swap, and never a dropped ISIN.
    """
    with QueryService(data_root=xs_lake) as svc:
        xs = svc.cross_section(CrossSectionRequest(trade_date=XS_DATE))

    b = {r.isin: r for r in xs.rows}[XS_B]
    assert b.primary is Exchange.NSE  # decided from the window, not this one dark session
    assert b.exchange is Exchange.BSE
    assert b.fell_back is True
    assert b.adj_close == Decimal("200")


# ── a stitched name: the survivor's L2 history sits under ISINs L1 files elsewhere ──────────────

XS_RETIRED: Final = "INE444D01014"  # traded the first three sessions, then was reissued as…
XS_SURVIVOR: Final = "INE444D01022"  # …this ISIN, which L1 knows only from the fourth session on
_XS_REISSUE: Final = _XS_WINDOW[3]


@pytest.fixture
def stitched_lake(tmp_path: Path) -> Path:
    """A survivor whose L2 partition carries its predecessor's sessions (the D2 lineage stitch)."""
    for session in _XS_WINDOW:
        isin = XS_RETIRED if session < _XS_REISSUE else XS_SURVIVOR
        _write_l1_partition(tmp_path, session, [(isin, "NSE", "DDD", Decimal("50"), 2_000)])
    materialize_isin(
        XS_SURVIVOR,
        chain=FactorChain(isin=XS_SURVIVOR),
        actions=(),
        data_root=tmp_path,
        history_isins=(XS_RETIRED, XS_SURVIVOR),
    )
    return tmp_path


def test_cross_section_on_a_stitched_session_takes_the_only_venue_as_primary(
    stitched_lake: Path,
) -> None:
    """On a session L1 files under the retired ISIN, the survivor's bar is still in the day.

    The liquidity scan reads L1 by ISIN and finds nothing for the survivor before the reissue; the
    map used to have no entry and `canonical_daily` refused the whole day (GOLDIAM, 2017-10-03, on
    the server on 2026-09-07 — the ten-year backtest died there). One venue printed, so it is the
    primary; nothing was decided by liquidity, and nothing had to be.
    """
    early = _XS_WINDOW[1]
    with QueryService(data_root=stitched_lake) as svc:
        xs = svc.cross_section(CrossSectionRequest(trade_date=early))
    (row,) = xs.rows
    assert row.isin == XS_SURVIVOR
    assert row.primary is Exchange.NSE
    assert row.fell_back is False
    assert row.adj_close == Decimal("50")


def test_adjusted_series_of_a_stitched_name_spans_the_reissue(stitched_lake: Path) -> None:
    with QueryService(data_root=stitched_lake) as svc:
        series = svc.adjusted_series(AdjustedSeriesRequest(isin=XS_SURVIVOR))
    assert series.primary is Exchange.NSE
    assert [pt.trade_date for pt in series.points] == _XS_WINDOW
    assert {pt.isin for pt in series.points} == {XS_SURVIVOR}


def test_cross_section_supplied_primary_map_skips_derivation(xs_lake: Path) -> None:
    """A caller holding the day's primary map (M3.2) can pass it and pin the dedup exchange.

    Pinning B's primary to BSE (contrived) makes B's BSE bar the primary print, not a fallback —
    proof the supplied map, not L1 liquidity, drives the dedup on this path.
    """
    supplied = {XS_A: Exchange.NSE, XS_B: Exchange.BSE, XS_C: Exchange.NSE}
    with QueryService(data_root=xs_lake) as svc:
        xs = svc.cross_section(CrossSectionRequest(trade_date=XS_DATE, primary_by_isin=supplied))
    b = {r.isin: r for r in xs.rows}[XS_B]
    assert b.primary is Exchange.BSE
    assert b.exchange is Exchange.BSE
    assert b.fell_back is False


# ── acceptance 3: benchmark on a full-scale synthetic store ─────────────────────────────────────

BENCH_ISINS: Final = 200
BENCH_SESSIONS: Final = 2520  # ≈ 10 trading years
BENCH_BUDGET_S: Final = 5.0  # "sane time" ceiling per query
_BENCH_START: Final = date(2015, 1, 1)


def _bench_isin(i: int) -> str:
    """A schema-valid synthetic ISIN: 'IN' + 'E' + 6 digits + '01' + check digit (12 chars)."""
    return f"INE{i:06d}01{i % 10}"


def _bench_dates() -> list[date]:
    """`BENCH_SESSIONS` consecutive calendar dates — a stand-in trading calendar for the bench."""
    return [_BENCH_START + timedelta(days=n) for n in range(BENCH_SESSIONS)]


def _write_synthetic_l2_partition(
    data_root: Path,
    isin: str,
    dates: list[date],
    price_cols: dict[str, pa.Array],
) -> None:
    """Write one ISIN's L2 partition directly, both exchanges, matching `PRICES_ADJUSTED_SCHEMA`.

    The price/factor columns are shared across ISINs (built once by the caller); only the `isin`
    column changes, so generating a full-scale corpus costs one table assemble + one write per ISIN
    rather than regenerating a million Decimals per name. This is a benchmark corpus, not a
    correctness fixture — the adjustment math is proven by the acceptance-1/2 tests through the real
    materializer; here we only need a realistically-sized, schema-true set of Parquet partitions to
    time reads against.
    """
    n = len(dates) * 2
    table = pa.table(
        {
            "isin": pa.array([isin] * n, type=pa.string()),
            "exchange": price_cols["exchange"],
            "trade_date": price_cols["trade_date"],
            "adj_open": price_cols["adj_open"],
            "adj_high": price_cols["adj_high"],
            "adj_low": price_cols["adj_low"],
            "adj_close": price_cols["adj_close"],
            "adj_volume": price_cols["adj_volume"],
            "tr_close": price_cols["tr_close"],
            "cum_price_factor": price_cols["cum_price_factor"],
            "cum_qty_factor": price_cols["cum_qty_factor"],
        },
        schema=PRICES_ADJUSTED_SCHEMA,
    )
    path = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="snappy", version="2.6")


@pytest.fixture(scope="module")
def bench_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A full-scale synthetic L2 store: BENCH_ISINS ISINs x BENCH_SESSIONS sessions x 2 venues."""
    data_root = tmp_path_factory.mktemp("bench_store")
    dates = _bench_dates()
    n = len(dates)

    # Columns shared by every ISIN. NSE and BSE blocks stacked; prices are constant (the read cost
    # is what we measure, not the values). Factors are 1 (already "current basis").
    price = Decimal("250.0000")
    vol = Decimal("100000.0000")
    one = Decimal("1")
    date_col = pa.array(dates + dates, type=pa.date32())
    exch_col = pa.array(["NSE"] * n + ["BSE"] * n, type=pa.string())
    price_col = pa.array([price] * (2 * n), type=pa.decimal128(28, 4))
    vol_col = pa.array([vol] * (2 * n), type=pa.decimal128(38, 4))
    factor_price = pa.array([one] * (2 * n), type=pa.decimal128(38, 18))
    factor_qty = pa.array([one] * (2 * n), type=pa.decimal128(38, 18))
    price_cols: dict[str, pa.Array] = {
        "exchange": exch_col,
        "trade_date": date_col,
        "adj_open": price_col,
        "adj_high": price_col,
        "adj_low": price_col,
        "adj_close": price_col,
        "adj_volume": vol_col,
        "tr_close": price_col,
        "cum_price_factor": factor_price,
        "cum_qty_factor": factor_qty,
    }

    for i in range(BENCH_ISINS):
        _write_synthetic_l2_partition(data_root, _bench_isin(i), dates, price_cols)
    return data_root


def _median_seconds(call: object, runs: int = 5) -> float:
    """Median wall time (s) of `call()` over `runs` timed runs, after one discarded warm-up."""
    assert callable(call)
    call()  # warm-up: view registration + OS page cache
    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        call()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def test_benchmark_full_store_series_and_cross_section(
    bench_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both shapes return in a sane time against a full-scale (~1.0M-row, 10-year-deep) store.

    Method: median of 5 `time.perf_counter`-timed `QueryService` calls after one warm-up, corpus =
    BENCH_ISINS x BENCH_SESSIONS x 2 venues. The series ISIN is queried across its full 10-year
    depth (one partition, ~5k rows), and the cross-section across the full breadth on a mid-window
    date (all partitions scanned, deduped to primary). Both are asserted under BENCH_BUDGET_S; the
    medians are printed as `query.benchmark` and captured into the task state record for the record.
    """
    total_rows = BENCH_ISINS * BENCH_SESSIONS * 2
    mid_date = _BENCH_START + timedelta(days=BENCH_SESSIONS // 2)
    series_isin = _bench_isin(BENCH_ISINS // 2)
    primary_map = {_bench_isin(i): Exchange.NSE for i in range(BENCH_ISINS)}

    with QueryService(data_root=bench_store) as svc:

        def run_series() -> None:
            out = svc.adjusted_series(AdjustedSeriesRequest(isin=series_isin, primary=Exchange.NSE))
            assert len(out.points) == BENCH_SESSIONS

        def run_cross_section() -> None:
            out = svc.cross_section(
                CrossSectionRequest(trade_date=mid_date, primary_by_isin=primary_map)
            )
            assert len(out.rows) == BENCH_ISINS

        series_s = _median_seconds(run_series)
        cross_s = _median_seconds(run_cross_section)

    with capsys.disabled():
        print(
            f"\nquery.benchmark corpus={BENCH_ISINS} isins x {BENCH_SESSIONS} sessions x 2 venues "
            f"= {total_rows:,} rows | method=median-of-5 perf_counter (1 warm-up discarded) | "
            f"series(a)={series_s * 1000:.1f}ms cross_section(b)={cross_s * 1000:.1f}ms | "
            f"budget={BENCH_BUDGET_S:.1f}s"
        )

    assert series_s < BENCH_BUDGET_S, f"shape (a) too slow: {series_s:.3f}s"
    assert cross_s < BENCH_BUDGET_S, f"shape (b) too slow: {cross_s:.3f}s"
