"""M9.4 — the backtest benchmarked against M3.9's computed TRI (X2, §5.2).

Every acceptance criterion of the task is a test here:

  1. the backtest benchmark is the M3.9 ``TriSeries`` (computed TRI), not the ad-hoc L1 proxy
     (``test_benchmark_is_m39_computed_tri``, ``test_computed_tri_differs_from_l1_proxy``)
  2. the report states the benchmark provenance (computed, not licensed) and re-states excess
     return on it (``test_report_states_provenance_and_excess``)
  3. the comparison runs through the existing ``compare_to_benchmarks`` path unchanged
     (``test_benchmark_xirr_is_the_unchanged_compare_path``)

The lake is built through the *real* seams: raw ``prices_raw`` L1 partitions under ``tmp_path``,
and a real M3.9 computed TRI ingested through ``ingest_tri_from_close`` — the same
``compute_tri`` → ``write_tri_l1`` path production uses — so ``read_tri_series`` inside the backtest
reads the same on-disk contract. No postgres, no network, deterministic.

The fixture is a four-name rising market at monthly rebalances; the 2024-01 rebalance has a
complete twelve-month look-back, so the policy trades and the portfolio XIRR is well defined. The
computed TRI is a NIFTY-50 index level rising over the window, seeded to a published close of
20000 with a 1.5 % dividend yield — §4.1's estimate, not the licensed feed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.accounting import PortfolioBook
from backtest.policies.naive_momentum import MomentumParameters
from backtest.run import (
    _BENCHMARK_COMPUTED_TRI,
    _BENCHMARK_L1_PROXY,
    BacktestResult,
    render_benchmark_report,
    run_benchmark_report,
    run_naive_momentum,
)
from dataplatform.ingest.indices import (
    IndexCloseRow,
    ingest_tri_from_close,
    read_tri_series,
)
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

pytestmark = pytest.mark.integration

_PRICE_Q: Final = Decimal("0.0001")

# ── the fixture universe ─────────────────────────────────────────────────────────────────────────

BENCH_SLUG: Final = "nifty50"
BENCH_NAME: Final = "Nifty 50"

NAME_A: Final = "INE100A01010"
NAME_B: Final = "INE200A01010"
NAME_C: Final = "INE300A01010"
NAME_D: Final = "INE400A01010"
_NAMES: Final = [NAME_A, NAME_B, NAME_C, NAME_D]

_BASE: Final = Decimal("100")  # every name's close on and before the look-back reference

#: Each name's close *after* the reference — a spread of twelve-month returns so momentum ranks.
_LEVELS: Final = {
    NAME_A: Decimal("200"),  # +100 %
    NAME_B: Decimal("160"),  # +60 %
    NAME_C: Decimal("140"),  # +40 %
    NAME_D: Decimal("120"),  # +20 %
}

#: One session per month (each a rebalance) plus a trailing fill-headroom session. The 2024-01
#: rebalance's look-back reference is 2023-01 (a full twelve months back), so its window is full.
_SESSIONS: Final = [
    date(2023, 1, 2),
    date(2023, 2, 1),
    date(2023, 3, 1),
    date(2023, 4, 3),
    date(2023, 5, 2),
    date(2023, 6, 1),
    date(2023, 7, 3),
    date(2023, 8, 1),
    date(2023, 9, 1),
    date(2023, 10, 2),
    date(2023, 11, 1),
    date(2023, 12, 1),
    date(2024, 1, 2),
    date(2024, 2, 1),  # fill-headroom session, never itself replayed
]

REF_SESSION: Final = date(2023, 1, 2)  # look-back reference for the 2024-01 rebalance
_VOLUME: Final = 100_000  # every name trades liquidly

#: The published NIFTY-50 index level per session — seeded to 20000 and compounding ~2 %/session.
_TRI_SEED_CLOSE: Final = Decimal("20000")
_TRI_STEP: Final = Decimal("1.02")
_TRI_YIELD: Final = Decimal("1.5")  # a 1.5 % dividend yield — the estimated dividend leg


def _close_of(isin: str, session: date) -> Decimal:
    """A name's raw close: ``_BASE`` on and before the reference, its target level after it."""
    if session <= REF_SESSION:
        return _BASE
    return _LEVELS[isin]


def _write_l1_partition(data_root: Path, trade_date: date) -> None:
    """Write one raw NSE ``prices_raw`` L1 partition for every name on ``trade_date``."""
    records = []
    for isin in _NAMES:
        close = _close_of(isin, trade_date)
        records.append(
            {
                "isin": isin,
                "exchange": "NSE",
                "symbol": isin[:6],
                "series": "EQ",
                "trade_date": trade_date,
                "open": close.quantize(_PRICE_Q),
                "high": close.quantize(_PRICE_Q),
                "low": close.quantize(_PRICE_Q),
                "close": close.quantize(_PRICE_Q),
                "last": close.quantize(_PRICE_Q),
                "prev_close": close.quantize(_PRICE_Q),
                "total_traded_qty": _VOLUME,
                "total_traded_value": (close * _VOLUME).quantize(_PRICE_Q),
                "total_trades": _VOLUME,
                "deliv_qty": None,
                "deliv_pct": None,
            }
        )
    path = l1_partition_path(PRICES_RAW_DATASET, trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=PRICES_RAW_SCHEMA)
    pq.write_table(table, path, compression="snappy", version="2.6")


def _index_closes() -> list[IndexCloseRow]:
    """The NIFTY-50 price-index close series compute_tri seeds the computed TRI from."""
    rows: list[IndexCloseRow] = []
    level = _TRI_SEED_CLOSE
    for session in _SESSIONS:
        rows.append(
            IndexCloseRow(
                index_name=BENCH_NAME,
                index_date=session,
                close=level.quantize(_PRICE_Q),
                div_yield=_TRI_YIELD,
            )
        )
        level = level * _TRI_STEP
    return rows


def _build_lake(root: Path, *, with_tri: bool) -> Path:
    """Write the four-name price lake under ``root``, optionally with the ingested computed TRI."""
    root.mkdir(parents=True, exist_ok=True)
    for session in _SESSIONS:
        _write_l1_partition(root, session)
    if with_tri:
        ingest_tri_from_close(_index_closes(), index_slug=BENCH_SLUG, data_root=root)
    return root


@pytest.fixture
def lake_with_tri(tmp_path: Path) -> Path:
    """An L1 lake of four names *and* an ingested M3.9 computed TRI for the whole window."""
    # A distinct subdirectory so a test that also builds the no-TRI lake does not share this store.
    return _build_lake(tmp_path / "with_tri", with_tri=True)


@pytest.fixture
def lake_without_tri(tmp_path: Path) -> Path:
    """The same four-name lake but with no computed TRI — the pre-M9.4 L1-proxy fallback path."""
    return _build_lake(tmp_path / "without_tri", with_tri=False)


def _run(lake: Path) -> BacktestResult:
    """Run the naive-momentum backtest over the fixture window against the computed TRI slug."""
    return run_naive_momentum(
        start=_SESSIONS[0],
        end=_SESSIONS[-1],
        parameters=MomentumParameters(top_n=2),
        data_root=lake,
        adjusted=False,  # raw L1 signal — this fixture ships no materialized L2
        benchmark_slug=BENCH_SLUG,
    )


# ── acceptance 1: the benchmark is M3.9's computed TRI, not the ad-hoc L1 proxy ──────────────


def test_benchmark_is_m39_computed_tri(lake_with_tri: Path) -> None:
    """When the store holds M3.9's computed TRI, the run uses it — stamped computed, not licensed.

    The benchmark source is the M3.9 computed-TRI path and the series method is
    ``computed_price_plus_div`` (§4.1's estimate), which is exactly the series
    ``ingest_tri_from_close`` wrote — proving the ad-hoc L1 proxy is no longer the benchmark.
    """
    run = _run(lake_with_tri)

    assert run.benchmark_source == _BENCHMARK_COMPUTED_TRI
    assert run.benchmark_is_computed_tri is True
    assert run.benchmark_method == "computed_price_plus_div"
    assert run.benchmark_index_name == BENCH_NAME


def test_l1_proxy_only_when_no_computed_tri(lake_without_tri: Path) -> None:
    """With no computed TRI in the store the run falls back to the L1 proxy and records it honestly.

    The fallback is not silent: ``benchmark_source`` is the proxy, so the report can state plainly
    that the M3.9 computed TRI was not available and the proxy stood in.
    """
    run = _run(lake_without_tri)

    assert run.benchmark_source == _BENCHMARK_L1_PROXY
    assert run.benchmark_is_computed_tri is False


def test_computed_tri_differs_from_l1_proxy(lake_with_tri: Path, lake_without_tri: Path) -> None:
    """Wiring the computed TRI actually changes the benchmark number vs the ad-hoc L1 proxy.

    Same policy, same window, same portfolio (the price lakes are identical) — only the benchmark
    series differs. The computed TRI rises ~2 %/session plus a dividend leg; the L1 proxy tracks the
    four names' price relatives. The two benchmark XIRRs must differ, so the benchmark genuinely
    moved from the proxy to the M3.9 series (acceptance 1), while the portfolio XIRR is unchanged.
    """
    with_tri = _run(lake_with_tri)
    proxy = _run(lake_without_tri)

    # The portfolio is identical — only the benchmark leg changed.
    assert with_tri.comparison.portfolio_xirr == proxy.comparison.portfolio_xirr
    assert with_tri.comparison.benchmark_xirr != proxy.comparison.benchmark_xirr


# ── acceptance 3: the comparison runs through the existing compare_to_benchmarks path unchanged ───


def test_benchmark_xirr_is_the_unchanged_compare_path(lake_with_tri: Path) -> None:
    """The run's benchmark XIRR equals compare_to_benchmarks fed the ingested computed TRI directly.

    Rebuilding the investor's single opening cashflow into a fresh ``PortfolioBook`` and calling the
    *unchanged* ``compare_to_benchmarks`` with the exact ``TriSeries`` ``read_tri_series`` returns
    reproduces the run's benchmark XIRR to the last place — proof the run flowed the M3.9 series
    through that path with no bespoke benchmark maths of its own (acceptance 3).
    """
    run = _run(lake_with_tri)

    series = read_tri_series(BENCH_SLUG, run.terminal, data_root=lake_with_tri)
    assert series is not None and series.method == "computed_price_plus_div"

    book = PortfolioBook()
    book.deposit(run.start, run.opening_cash)  # the one external cashflow the run makes
    expected = book.compare_to_benchmarks(run.terminal, {}, benchmark=series, theme=series)

    assert run.comparison.benchmark_xirr == expected.benchmark_xirr


# ── acceptance 2: the report states provenance (computed, not licensed) and re-states excess ──────


def test_report_states_provenance_and_excess(lake_with_tri: Path) -> None:
    """The M9.4 report names the computed TRI, denies the licensed feed, and re-states excess."""
    run = _run(lake_with_tri)
    report = render_benchmark_report(run, benchmark_slug=BENCH_SLUG)

    assert "M9.4" in report
    # Provenance: computed, and explicitly not the licensed feed.
    assert "computed" in report.lower()
    assert "computed_price_plus_div" in report
    assert "not the licensed" in report.lower() or "not the exchange" in report.lower()
    assert "FAILED at C.1" in report
    # Excess return re-stated on this benchmark.
    from backtest.run import _pct  # the run's own percentage formatter

    assert "Excess over benchmark" in report
    assert _pct(run.comparison.excess_over_benchmark) in report
    assert _pct(run.comparison.benchmark_xirr) in report


def test_report_states_fallback_when_no_computed_tri(lake_without_tri: Path) -> None:
    """Over a store with no computed TRI the report states the L1-proxy fallback plainly.

    The honesty requirement cuts both ways: when the computed TRI is absent the report must say the
    proxy stood in and that the computed-TRI wiring is proven on the fixture — never present the
    proxy as the licensed or the computed series.
    """
    run = _run(lake_without_tri)
    report = render_benchmark_report(run, benchmark_slug=BENCH_SLUG)

    assert "L1 proxy" in report or "L1** proxy" in report or "**L1** proxy" in report
    assert "not the licensed" in report.lower()
    assert "test_backtest_benchmark.py" in report


def test_run_benchmark_report_end_to_end(lake_with_tri: Path) -> None:
    """The one-call report entrypoint runs the backtest and renders the computed-TRI report."""
    report = run_benchmark_report(
        start=_SESSIONS[0],
        end=_SESSIONS[-1],
        parameters=MomentumParameters(top_n=2),
        data_root=lake_with_tri,
        adjusted=False,
        benchmark_slug=BENCH_SLUG,
    )
    assert "M9.4" in report
    assert "computed_price_plus_div" in report
    assert "Excess over benchmark" in report
