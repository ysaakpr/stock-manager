"""M9.4 + W0-4 — the backtest benchmarked against the NIFTY total-return series (X2, §5.2).

M9.4's three acceptance criteria are each a test here: the benchmark is a ``TriSeries`` from the
M3.9 pipeline rather than the ad-hoc L1 proxy; the report states which series it actually was; and
the comparison runs through the existing ``compare_to_benchmarks`` path unchanged.

W0-4 adds the half M9.4 could not have: **the published series is preferred over the estimate.**
M9.4 shipped when the source register recorded `nifty_tri_history` FAILED against a stale URL path,
so no published TRI could exist in any lake and `_resolve_benchmark` took a fallback on every run
— quietly, at log level *info*. The tests in the last two sections are the ones that would have
caught that: the resolution order, the warning on each fallback, the report flag, and the strict
mode that refuses to produce a number rather than produce a misattributed one.

The lake is built through the *real* seams: raw ``prices_raw`` L1 partitions under ``tmp_path``, a
computed TRI ingested through ``ingest_tri_from_close``, and a published TRI written through
``write_tri_l1`` — the same on-disk contract production writes, so ``read_tri_series`` inside the
backtest reads exactly what it reads live. No postgres, no network, deterministic.

The fixture is a four-name rising market at monthly rebalances; the 2024-01 rebalance has a
complete twelve-month look-back, so the policy trades and the portfolio XIRR is well defined.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from structlog.testing import capture_logs

from backtest.accounting import PortfolioBook
from backtest.policies.naive_momentum import MomentumParameters
from backtest.run import (
    _BENCHMARK_COMPUTED_TRI,
    _BENCHMARK_L1_PROXY,
    _BENCHMARK_PUBLISHED_TRI,
    BacktestResult,
    BenchmarkSourceError,
    render_benchmark_report,
    require_published_benchmark,
    run_benchmark_report,
    run_naive_momentum,
)
from dataplatform.ingest.indices import (
    TRI_METHOD_PUBLISHED,
    IndexCloseRow,
    TriPoint,
    TriSeries,
    ingest_tri_from_close,
    read_tri_series,
    write_tri_l1,
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

#: The *published* series' level path. Distinct from the computed one on purpose (see
#: `_published_series`): with both in the lake, the benchmark XIRR alone says which one won.
_PUBLISHED_SEED: Final = Decimal("30000")
_PUBLISHED_STEP: Final = Decimal("1.03")


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


def _published_series() -> TriSeries:
    """A *published* TRI for the window — the exchange's own series, not §4.1's estimate.

    Deliberately a different level path from ``_index_closes``' computed series (it steps 3 %, not
    2 % plus a dividend leg) so a test can tell which of the two a run resolved to by looking at
    the benchmark XIRR alone.
    """
    points: list[TriPoint] = []
    level = _PUBLISHED_SEED
    for session in _SESSIONS:
        points.append(
            TriPoint(
                index_slug=BENCH_SLUG,
                index_name=BENCH_NAME,
                as_of=session,
                tri_value=level.quantize(_PRICE_Q),
                method=TRI_METHOD_PUBLISHED,
            )
        )
        level = level * _PUBLISHED_STEP
    return TriSeries(
        index_slug=BENCH_SLUG,
        index_name=BENCH_NAME,
        method=TRI_METHOD_PUBLISHED,
        points=tuple(points),
    )


@pytest.fixture
def lake_with_published_tri(tmp_path: Path) -> Path:
    """A lake holding **both** series — the case the preference order exists to decide."""
    root = _build_lake(tmp_path / "with_published", with_tri=True)
    write_tri_l1(_published_series(), data_root=root)
    return root


@pytest.fixture
def lake_with_published_tri_only(tmp_path: Path) -> Path:
    """A lake holding the published series and no estimate."""
    root = _build_lake(tmp_path / "published_only", with_tri=False)
    write_tri_l1(_published_series(), data_root=root)
    return root


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
    """With only the estimate in the lake, the report says so — and warns, in the heading.

    This test used to assert the report claimed the published endpoint was "session-gated and
    FAILED at C.1". That claim was false — the register carried a stale URL path — so the
    assertion is now that the report names the estimate as an estimate and refuses to call the
    excess excess-over-NIFTY-TRI.
    """
    run = _run(lake_with_tri)
    report = render_benchmark_report(run, benchmark_slug=BENCH_SLUG)

    assert "COMPUTED TRI ESTIMATE" in report
    assert "computed_price_plus_div" in report
    assert "not the published" in report.lower()
    assert "do not quote it as excess over nifty-tri" in report.lower()
    # The remedy is named, so a reader knows the real series is one command away.
    assert "dataplatform.ingest.tri_backfill" in report
    # Excess return re-stated on this benchmark.
    from backtest.run import _pct  # the run's own percentage formatter

    assert "Excess over benchmark" in report
    assert _pct(run.comparison.excess_over_benchmark) in report
    assert _pct(run.comparison.benchmark_xirr) in report


def test_report_states_fallback_when_no_computed_tri(lake_without_tri: Path) -> None:
    """Over a store with neither series the report says the proxy is not a TRI at all.

    The honesty requirement cuts every way: the proxy must never be presented as a total-return
    index, and the old label — "NIFTY-TRI (broad-market TRI proxy from L1)" — did exactly that at
    a glance.
    """
    run = _run(lake_without_tri)
    report = render_benchmark_report(run, benchmark_slug=BENCH_SLUG)

    assert "L1 PROXY — not a total-return index" in report
    assert "not a total-return index at all" in report.lower()
    assert "may be labelled excess over nifty-tri" in report.lower()
    assert "NIFTY-TRI (broad-market TRI proxy" not in report


# ── W0-4: the published series is preferred, the fallbacks are loud ───────────────────────────


def test_the_published_tri_is_preferred_over_the_estimate(lake_with_published_tri: Path) -> None:
    """With both series on disk the run takes the **published** one. Invert this and it fails.

    This is the test M9.4 could not have had. Because the register recorded the endpoint FAILED
    against a stale path, no lake could hold a published series, so the preference was untestable
    and the estimate won by default in every M9/M10/M12 run.
    """
    run = _run(lake_with_published_tri)

    assert run.benchmark_source == _BENCHMARK_PUBLISHED_TRI
    assert run.benchmark_is_published_tri is True
    assert run.benchmark_is_computed_tri is False
    assert run.benchmark_is_proxy is False
    assert run.benchmark_method == TRI_METHOD_PUBLISHED
    assert run.benchmark_provenance == "published TRI"


def test_preferring_the_published_series_changes_the_benchmark_number(
    lake_with_published_tri: Path, lake_with_tri: Path
) -> None:
    """The preference is not cosmetic: it moves the benchmark XIRR, and so the excess.

    Same prices, same policy, same portfolio — the two lakes differ only in whether the published
    series is present alongside the estimate. If `_resolve_benchmark` read the estimate in both
    cases (the pre-W0-4 behaviour), the two benchmark XIRRs would be identical and this fails.
    """
    published = _run(lake_with_published_tri)
    estimate = _run(lake_with_tri)

    assert published.comparison.portfolio_xirr == estimate.comparison.portfolio_xirr
    assert published.comparison.benchmark_xirr != estimate.comparison.benchmark_xirr


def test_the_published_report_claims_no_caveat_it_does_not_need(
    lake_with_published_tri: Path,
) -> None:
    """When the benchmark *is* the published TRI the report says so, without the warnings."""
    run = _run(lake_with_published_tri)
    report = render_benchmark_report(run, benchmark_slug=BENCH_SLUG)

    assert "published TRI" in report
    assert "COMPUTED TRI ESTIMATE" not in report
    assert "L1 PROXY" not in report
    assert "excess over the real benchmark" in report.lower()
    # Still not alpha — the report must not overclaim in the other direction either.
    assert "not alpha" in report.lower() or "still not alpha" in report.lower()


def test_the_estimate_fallback_logs_a_warning(lake_with_tri: Path) -> None:
    """Falling back to the estimate is a WARNING, not an info line.

    It logged at *info* from M9.4 until 2026-09-08 — one line among the thousands an EOD run
    emits, which is a large part of why nobody noticed the benchmark had never once been the real
    series. `structlog.testing.capture_logs` is used rather than `caplog` because the platform
    logs through structlog's own `WriteLoggerFactory`, which never reaches stdlib logging.
    """
    with capture_logs() as entries:
        run = _run(lake_with_tri)
    assert run.benchmark_is_computed_tri is True
    fallbacks = [e for e in entries if e["event"] == "backtest.benchmark_fallback"]
    assert len(fallbacks) == 1
    assert fallbacks[0]["log_level"] == "warning"
    assert fallbacks[0]["source"] == _BENCHMARK_COMPUTED_TRI
    assert "not the exchange's published TRI" in fallbacks[0]["consequence"]
    # And it never claims the published series was resolved.
    assert not [e for e in entries if e.get("source") == _BENCHMARK_PUBLISHED_TRI]


def test_the_proxy_fallback_logs_a_warning(lake_without_tri: Path) -> None:
    """The proxy fallback — the one every M9/M10/M12 run actually took — warns and says why."""
    with capture_logs() as entries:
        run = _run(lake_without_tri)
    assert run.benchmark_is_proxy is True
    fallbacks = [e for e in entries if e["event"] == "backtest.benchmark_fallback"]
    assert len(fallbacks) == 1
    assert fallbacks[0]["log_level"] == "warning"
    assert fallbacks[0]["source"] == _BENCHMARK_L1_PROXY
    assert "not a total-return index of any kind" in fallbacks[0]["consequence"]


def test_the_published_series_resolves_without_a_warning(lake_with_published_tri: Path) -> None:
    """No fallback, no warning — so a warning in a log is a real signal, not background noise."""
    with capture_logs() as entries:
        run = _run(lake_with_published_tri)
    assert run.benchmark_is_published_tri is True
    assert not [e for e in entries if e["event"] == "backtest.benchmark_fallback"]
    sourced = [e for e in entries if e["event"] == "backtest.benchmark_source"]
    assert len(sourced) == 1
    assert sourced[0]["source"] == _BENCHMARK_PUBLISHED_TRI


# ── W0-4: strict mode refuses to produce a misattributed number ───────────────────────────────


def test_strict_mode_raises_when_only_the_estimate_is_available(lake_with_tri: Path) -> None:
    """Under `require_published_benchmark` the estimate is not an acceptable stand-in."""
    with require_published_benchmark(), pytest.raises(BenchmarkSourceError) as raised:
        _run(lake_with_tri)
    message = str(raised.value)
    assert "published total-return series is required" in message
    assert "no published TRI in the store" in message
    assert "tri_backfill" in message  # the remedy, named in the error


def test_strict_mode_raises_when_nothing_is_available(lake_without_tri: Path) -> None:
    with require_published_benchmark(), pytest.raises(BenchmarkSourceError):
        _run(lake_without_tri)


def test_strict_mode_runs_when_the_published_series_covers_the_window(
    lake_with_published_tri_only: Path,
) -> None:
    """Strict mode is not a blanket refusal — with the real series present the run proceeds."""
    with require_published_benchmark():
        run = _run(lake_with_published_tri_only)
    assert run.benchmark_is_published_tri is True


def test_strict_mode_raises_when_the_published_series_starts_too_late(tmp_path: Path) -> None:
    """A published series that misses the first cashflow is not coverage, and says which date.

    Valuing a deposit needs an index level in force on its date; a series starting later would
    silently value the opening cashflow at its own first level, flattering or penalising the
    benchmark by however far the index had already moved.
    """
    root = _build_lake(tmp_path / "late", with_tri=False)
    late = _published_series()
    write_tri_l1(
        TriSeries(
            index_slug=BENCH_SLUG,
            index_name=BENCH_NAME,
            method=TRI_METHOD_PUBLISHED,
            points=late.points[6:],
        ),
        data_root=root,
    )
    with require_published_benchmark(), pytest.raises(BenchmarkSourceError) as raised:
        _run(root)
    assert "after the run's first cashflow" in str(raised.value)


def test_strictness_does_not_leak_out_of_its_block(lake_without_tri: Path) -> None:
    """The ContextVar is reset on exit, so one strict run cannot make the next one strict."""
    with require_published_benchmark(), pytest.raises(BenchmarkSourceError):
        _run(lake_without_tri)
    run = _run(lake_without_tri)
    assert run.benchmark_is_proxy is True


def test_run_benchmark_report_end_to_end(lake_with_published_tri_only: Path) -> None:
    """The one-call report entrypoint runs the backtest and renders the published-TRI report."""
    report = run_benchmark_report(
        start=_SESSIONS[0],
        end=_SESSIONS[-1],
        parameters=MomentumParameters(top_n=2),
        data_root=lake_with_published_tri_only,
        adjusted=False,
        benchmark_slug=BENCH_SLUG,
    )
    assert "published TRI" in report
    assert TRI_METHOD_PUBLISHED in report
    assert "Excess over benchmark" in report
