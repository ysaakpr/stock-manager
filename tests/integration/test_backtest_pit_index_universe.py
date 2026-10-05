"""The backtest's index universe is the point-in-time membership history, never today's list (DQ-5).

The investable screen (``backtest.run._InvestableUniverse``) used to read the daily constituent
snapshots, which only exist from 2026-09 — so a past date either saw no membership at all or, for
the sector-rotation arm, a membership implied by today's lists. It now reads the DQ-5 history
through the query layer. Each property below is a test that fails if the logic is inverted:

1. **No future constituent leaks into a past date.** A name enters only once its inclusion is both
   effective *and* announced; a leaver stays until its exit is effective. A "today" snapshot that
   already reflects later changes is on disk and must not reach a 2024 decision.
2. **No fallback before coverage.** A date before the history's ``coverage_start`` raises
   ``IndexCoverageError`` — not today's snapshot, not an unscreened set, not an empty one — and so
   does an index with no history, and a covered date on which the history names nobody.
3. **Coverage is read from the history.** A newer build with an earlier coverage start widens what
   a run may cover, with no code change.
4. **The sector map is a label, not a membership filter.** A past member the current-day sector map
   does not name is pooled under ``UNKNOWN_SECTOR``, not dropped; a future member it does name is
   still excluded.
5. **Replay determinism.** The same lake and history give a byte-identical journal and book.

The lake is built through the real seams: raw ``prices_raw`` L1 partitions, the history written by
``write_membership_history`` and a snapshot by ``write_constituents_l1``. No postgres, no network.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.policies.naive_momentum import MomentumParameters
from backtest.rails import UNKNOWN_SECTOR
from backtest.run import (
    INDEX_MEMBERSHIP_IDENTITY,
    IndexCoverageError,
    UniverseParameters,
    _InvestableUniverse,
    _L1MomentumData,
    _L1Reader,
    _L1SectorRotationData,
    backtest_spec,
    run_naive_momentum,
)
from dataplatform.ingest.indices import ConstituentRow, ConstituentSnapshot, write_constituents_l1
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA
from tests.index_history_support import stay, write_index_history

pytestmark = pytest.mark.integration

_PRICE_Q: Final = Decimal("0.0001")

INDEX_SLUG: Final = "niftypit"

KEEP: Final = "INE100A01010"  # a member throughout
LEAVER: Final = "INE200A01010"  # a member until its exit takes effect on 2024-03-01
FUTURE: Final = "INE300A01010"  # announced 2024-02-15, effective 2024-03-01; the top-momentum name
LATE: Final = "INE400A01010"  # effective 2024-01-15 but announced only on 2024-02-10
OUTSIDER: Final = "INE900A01010"  # liquid and listed, never in the index

_NAMES: Final = (KEEP, LEAVER, FUTURE, LATE, OUTSIDER)

#: Monthly momentum per name: FUTURE ranks first, so a leak puts it straight into the basket.
_MONTHLY_GAIN: Final = {
    FUTURE: Decimal("0.10"),
    OUTSIDER: Decimal("0.08"),
    LATE: Decimal("0.06"),
    KEEP: Decimal("0.04"),
    LEAVER: Decimal("0.02"),
}

#: The first session of each month 2023-01 → 2024-04, then one fill-headroom session.
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
    date(2024, 1, 1),
    date(2024, 2, 1),
    date(2024, 3, 1),
    date(2024, 4, 1),
    date(2024, 4, 2),
]

COVERAGE: Final = date(2023, 1, 2)
ANCHOR: Final = date(2024, 6, 3)
TODAY_SNAPSHOT: Final = date(2026, 9, 8)  # a current list: LEAVER gone, FUTURE and LATE in

ANNOUNCED: Final = date(2024, 2, 15)
EFFECTIVE: Final = date(2024, 3, 1)

_FLOOR: Final = Decimal("100000")
_VOLUME: Final = 100_000


def _params() -> UniverseParameters:
    return UniverseParameters(
        index_slug=INDEX_SLUG, median_turnover_floor=_FLOOR, liquidity_lookback_days=365
    )


def _close_of(isin: str, session: date) -> Decimal:
    months = (session.year - 2023) * 12 + session.month - 1
    return Decimal("100") * (Decimal("1") + _MONTHLY_GAIN[isin] * months)


def _write_prices(data_root: Path) -> None:
    for session in _SESSIONS:
        records = []
        for isin in _NAMES:
            close = _close_of(isin, session).quantize(_PRICE_Q)
            records.append(
                {
                    "isin": isin,
                    "exchange": "NSE",
                    "symbol": isin[:6],
                    "series": "EQ",
                    "trade_date": session,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "last": close,
                    "prev_close": close,
                    "total_traded_qty": _VOLUME,
                    "total_traded_value": (close * _VOLUME).quantize(_PRICE_Q),
                    "total_trades": _VOLUME,
                    "deliv_qty": None,
                    "deliv_pct": None,
                }
            )
        path = l1_partition_path(PRICES_RAW_DATASET, session, data_root=data_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(records, schema=PRICES_RAW_SCHEMA), path)


def _write_today_snapshot(data_root: Path) -> None:
    """The list as it stands today — the survivorship source a past date must never see."""
    rows = tuple(
        ConstituentRow(
            isin=isin,
            symbol=isin[:6],
            series="EQ",
            company_name=f"Company {isin[:6]}",
            industry="Test",
        )
        for isin in sorted((KEEP, FUTURE, LATE))
    )
    write_constituents_l1(
        ConstituentSnapshot(
            index_slug=INDEX_SLUG,
            index_name="NIFTY PIT",
            as_of=TODAY_SNAPSHOT,
            rows=rows,
            source="nifty_index_constituents",
        ),
        data_root=data_root,
    )


def _write_history(data_root: Path, *, coverage: date = COVERAGE, anchor: date = ANCHOR) -> None:
    write_index_history(
        data_root,
        INDEX_SLUG,
        coverage_start=coverage,
        anchor=anchor,
        members=(KEEP,),
        stays=(
            stay(
                INDEX_SLUG,
                LEAVER,
                coverage,
                EFFECTIVE,
                knowable_from=coverage,
                exit_knowable=ANNOUNCED,
            ),
            stay(INDEX_SLUG, FUTURE, EFFECTIVE, knowable_from=ANNOUNCED),
            stay(INDEX_SLUG, LATE, date(2024, 1, 15), knowable_from=date(2024, 2, 10)),
        ),
    )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    _write_prices(tmp_path)
    _write_history(tmp_path)
    _write_today_snapshot(tmp_path)
    return tmp_path


def _screen(reader: _L1Reader, lake: Path) -> _InvestableUniverse:
    return _InvestableUniverse(reader, _params(), data_root=lake)


# ── 1. no future constituent reaches a past date ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("on_date", "expected"),
    [
        # LATE is already effective but not yet announced; FUTURE is neither; today's list unseen.
        (date(2024, 2, 1), {KEEP, LEAVER}),
        # LATE announced (and effective); FUTURE announced but not effective; LEAVER still in.
        (date(2024, 2, 20), {KEEP, LEAVER, LATE}),
        # The change takes effect: FUTURE in, LEAVER out.
        (EFFECTIVE, {KEEP, LATE, FUTURE}),
    ],
)
def test_membership_on_a_date_is_announced_and_effective_by_it(
    lake: Path, on_date: date, expected: set[str]
) -> None:
    reader = _L1Reader(data_root=lake)
    try:
        assert _screen(reader, lake).members_asof(on_date) == frozenset(expected)
    finally:
        reader.close()


def test_a_future_constituent_never_enters_a_past_rebalance(lake: Path) -> None:
    """FUTURE has the top momentum and is in today's list; on 2024-02-01 it must not be a candidate.

    The leaver is still a candidate then (survivorship would drop it), and OUTSIDER — liquid,
    listed, never a member — is out. A month later, once the change is effective, FUTURE ranks.
    """
    reader = _L1Reader(data_root=lake)
    try:
        data = _L1MomentumData(reader, _SESSIONS, universe_filter=_screen(reader, lake))
        before = {r.isin for r in data.signal(date(2024, 2, 1)).records}
        after = {r.isin for r in data.signal(EFFECTIVE).records}
    finally:
        reader.close()
    assert before == {KEEP, LEAVER}
    assert after == {KEEP, LATE, FUTURE}


# ── 2. no fallback before coverage, and never a silent empty universe ─────────────────────────


def test_a_date_before_coverage_raises_rather_than_reading_todays_list(lake: Path) -> None:
    reader = _L1Reader(data_root=lake)
    try:
        with pytest.raises(IndexCoverageError) as raised:
            _screen(reader, lake).members_asof(date(2022, 12, 30))
    finally:
        reader.close()
    assert raised.value.coverage_start == COVERAGE
    assert raised.value.index_slug == INDEX_SLUG
    assert "2023-01-02" in str(raised.value)


def test_a_run_whose_window_starts_before_coverage_fails_loud(tmp_path: Path) -> None:
    """Coverage from 2024-01-02: the 2024-02-01 rebalance is fine, but the run starts in 2023."""
    _write_prices(tmp_path)
    _write_history(tmp_path, coverage=date(2024, 2, 15))
    _write_today_snapshot(tmp_path)
    with pytest.raises(IndexCoverageError) as raised:
        run_naive_momentum(
            start=_SESSIONS[0],
            end=_SESSIONS[-1],
            parameters=MomentumParameters(top_n=2),
            data_root=tmp_path,
            adjusted=False,
            universe=_params(),
        )
    assert raised.value.as_of == date(2024, 2, 1)
    assert raised.value.coverage_start == date(2024, 2, 15)


def test_an_index_with_no_history_raises(lake: Path) -> None:
    reader = _L1Reader(data_root=lake)
    try:
        screen = _InvestableUniverse(
            reader, UniverseParameters(index_slug="niftynohistory"), data_root=lake
        )
        with pytest.raises(IndexCoverageError) as raised:
            screen.members_asof(date(2024, 2, 1))
    finally:
        reader.close()
    assert raised.value.coverage_start is None


def test_a_covered_date_with_no_member_raises_rather_than_returning_empty(tmp_path: Path) -> None:
    write_index_history(
        tmp_path,
        INDEX_SLUG,
        coverage_start=COVERAGE,
        anchor=ANCHOR,
        stays=(stay(INDEX_SLUG, KEEP, COVERAGE, date(2023, 6, 1)),),
    )
    reader = _L1Reader(data_root=tmp_path)
    try:
        screen = _screen(reader, tmp_path)
        assert screen.members_asof(date(2023, 3, 1)) == frozenset({KEEP})
        with pytest.raises(IndexCoverageError, match="names no member"):
            screen.members_asof(date(2024, 2, 1))
    finally:
        reader.close()


# ── 3. coverage comes from the history, so extending it widens a run ─────────────────────────


def test_a_newer_build_with_earlier_coverage_extends_what_runs(tmp_path: Path) -> None:
    _write_history(tmp_path, coverage=date(2024, 2, 15), anchor=date(2024, 6, 3))
    reader = _L1Reader(data_root=tmp_path)
    try:
        with pytest.raises(IndexCoverageError):
            _screen(reader, tmp_path).members_asof(date(2024, 2, 1))
        _write_history(tmp_path, coverage=COVERAGE, anchor=date(2024, 7, 1))
        assert _screen(reader, tmp_path).members_asof(date(2024, 2, 1)) == {KEEP, LEAVER}
    finally:
        reader.close()


# ── 4. the sector map labels members; it does not decide who is one ──────────────────────────


def test_sector_map_does_not_filter_membership(lake: Path) -> None:
    """A current-day map naming only today's survivors must not drop LEAVER, nor admit FUTURE."""
    todays_map = {KEEP: "Banks", FUTURE: "IT", LATE: "IT", OUTSIDER: "Autos"}
    reader = _L1Reader(data_root=lake)
    try:
        data = _L1SectorRotationData(
            reader, _SESSIONS, todays_map, universe_filter=_screen(reader, lake)
        )
        records = {r.isin: r.sector for r in data.signal(date(2024, 2, 1)).records}
    finally:
        reader.close()
    assert records == {KEEP: "Banks", LEAVER: UNKNOWN_SECTOR}


# ── 5. replay determinism ─────────────────────────────────────────────────────────────────────


def test_same_lake_and_history_replay_byte_identical(lake: Path) -> None:
    def once() -> tuple[bytes, bytes]:
        run = run_naive_momentum(
            start=_SESSIONS[0],
            end=_SESSIONS[-1],
            parameters=MomentumParameters(top_n=2),
            data_root=lake,
            adjusted=False,
            universe=_params(),
        )
        return run.result.journal_bytes(), run.result.book_bytes()

    first, second = once(), once()
    assert first == second
    assert FUTURE.encode() in first[0]  # the run really traded the post-change basket


def test_a_screened_run_spec_names_the_membership_source() -> None:
    """A ledger persisted under the snapshot-era reading is never resumed for a screened run."""

    def spec(universe: UniverseParameters | None) -> dict[str, str]:
        return backtest_spec(
            "naive_momentum",
            start=_SESSIONS[0],
            end=_SESSIONS[-1],
            parameters=MomentumParameters(top_n=2),
            opening_cash=Decimal("1000000"),
            adjusted=False,
            universe=universe,
        )

    assert spec(_params())["index_membership"] == INDEX_MEMBERSHIP_IDENTITY
    assert "index_membership" not in spec(None)
