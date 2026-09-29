"""M2.5 — L2 covers every ISIN L1 has EQ bars for, not only the ones a corporate action touched.

`rebuild_invalidated` builds exactly the ISINs a recompute flagged; nothing else ever built one. So
a name with no reconciled action — no split, no bonus, no dividend, no reissue — never got a
partition however long it traded. On the server on 2026-09-07 that was 793 of the 2,716 NSE EQ
names then trading (ADANIGREEN, ADANIENSOL, ETERNAL among them), absent from every L2 reader.

`materialize_missing` is the first-time fill. These tests pin down what it must and must not do:
cover every EQ ISIN and only EQ ISINs; leave what is already on disk byte-for-byte alone (so it is
safe to run again and again); skip a retired ISIN whose bars belong to its survivor's stitched
partition, and build that survivor over its whole chain when it is the one missing.

Offline and deterministic: synthetic rows under `tmp_path`, an empty factor store, no network.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from dataplatform.corpactions.factors import FactorChain
from dataplatform.identity.master import Exchange
from dataplatform.ingest.models import PriceRow
from dataplatform.store import l2_fill
from dataplatform.store.db import Connection
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import (
    PRICES_ADJUSTED_DATASET,
    isins_with_eq_bars,
    materialize_isin,
    materialize_missing,
    materialized_isins,
    open_connection,
    read_adjusted,
    rebuild_truncated,
)
from dataplatform.store.l2_fill import ExtensionCoverage, extension_coverage
from dataplatform.store.paths import l2_isin_partition_path

RELIANCE = "INE002A01018"  # NSE EQ on every session
INFOSYS = "INE009A01021"  # NSE EQ and a trade-to-trade (BE) row the same day — BE must not count
TCS = "INE467B01029"  # BSE only: no EQ series anywhere, so nothing for L2 to cover
HDFC = "INE040A01034"  # NSE EQ; doubles as the "retired" ISIN in the lineage case
SESSIONS = (date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3))


class _EmptyStore:
    """A store with no factors and no reconciled actions — the fill's common case.

    `materialize_missing` reads two tables through the connection it is handed (`load_factor_chain`,
    `load_reconciled_actions`); for a name no corporate action ever touched both are empty, and an
    empty chain adjusts by 1. Standing in for Postgres here keeps the test offline; the DB-backed
    path is exercised in `tests/integration/test_l2_views.py`.
    """

    def execute(self, sql: str, params: object = None) -> _EmptyStore:
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


def _row(isin: str, day: date, *, series: str, close: str) -> PriceRow:
    price = Decimal(close)
    return PriceRow(
        isin=isin,
        symbol=isin[:6],
        series=series,
        trade_date=day,
        open=price,
        high=price,
        low=price,
        close=price,
        last=price,
        prev_close=price,
        total_traded_qty=1_000,
        total_traded_value=price * 1_000,
        total_trades=10,
    )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    for i, day in enumerate(SESSIONS):
        write_prices_raw(
            [
                _row(RELIANCE, day, series="EQ", close=f"{2400 + i}.50"),
                _row(INFOSYS, day, series="EQ", close=f"{1500 + i}.25"),
                _row(INFOSYS, day, series="BE", close="1.00"),
                _row(HDFC, day, series="EQ", close=f"{1600 + i}.00"),
            ],
            exchange=Exchange.NSE,
            data_root=tmp_path,
        )
        write_prices_raw(
            [_row(TCS, day, series="A", close="3800.00")],
            exchange=Exchange.BSE,
            data_root=tmp_path,
        )
    return tmp_path


def _conn() -> Connection:
    return cast(Connection, _EmptyStore())


def test_the_population_is_every_isin_with_an_eq_bar(lake: Path) -> None:
    con = open_connection()
    try:
        assert isins_with_eq_bars(con, data_root=lake) == tuple(sorted((RELIANCE, INFOSYS, HDFC)))
    finally:
        con.close()


def test_a_cold_lake_has_no_population(tmp_path: Path) -> None:
    con = open_connection()
    try:
        assert isins_with_eq_bars(con, data_root=tmp_path) == ()
    finally:
        con.close()
    assert materialized_isins(data_root=tmp_path) == frozenset()


def test_the_fill_covers_what_is_missing_and_only_that(lake: Path) -> None:
    # RELIANCE was built the ordinary way (say, by the invalidation queue) before the fill ran.
    materialize_isin(
        RELIANCE, chain=FactorChain(isin=RELIANCE, rows=()), actions=(), data_root=lake
    )
    reliance = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, RELIANCE, data_root=lake)
    before = reliance.read_bytes()

    report = materialize_missing(_conn(), data_root=lake)

    assert report.candidates == 3
    assert report.already_materialized == 1
    assert report.skipped_retired == 0
    assert sorted(r.isin for r in report.written) == sorted((INFOSYS, HDFC))
    assert materialized_isins(data_root=lake) == frozenset({RELIANCE, INFOSYS, HDFC})
    # Never built for a BSE-only name: no EQ bars means nothing to adjust, so no partition.
    assert not l2_isin_partition_path(PRICES_ADJUSTED_DATASET, TCS, data_root=lake).exists()
    # What was already on disk is not rewritten — the fill is not a rebuild.
    assert reliance.read_bytes() == before


def test_a_filled_name_is_its_raw_eq_series_when_it_has_no_factors(lake: Path) -> None:
    materialize_missing(_conn(), data_root=lake)
    bars = read_adjusted(INFOSYS, data_root=lake)
    # Three EQ sessions, and none of the BE prints: the fill applies the same series scope as the
    # queue's rebuild, so the two paths produce the same partition for the same name.
    assert [b.trade_date for b in bars] == list(SESSIONS)
    assert [b.adj_close for b in bars] == [
        Decimal("1500.25"),
        Decimal("1501.25"),
        Decimal("1502.25"),
    ]
    assert {b.cum_price_factor for b in bars} == {Decimal(1)}


def test_running_the_fill_again_changes_nothing(lake: Path) -> None:
    first = materialize_missing(_conn(), data_root=lake)
    assert len(first.written) == 3
    bytes_after_first = {
        isin: l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=lake).read_bytes()
        for isin in (RELIANCE, INFOSYS, HDFC)
    }

    second = materialize_missing(_conn(), data_root=lake)

    assert second.written == ()
    assert second.already_materialized == 3
    for isin, payload in bytes_after_first.items():
        path = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=lake)
        assert path.read_bytes() == payload


def test_a_retired_isin_is_skipped_and_its_survivor_stitched(lake: Path) -> None:
    """Pretend HDFC's ISIN was retired into RELIANCE's: the D2 lineage says so, so the fill must
    not give the retired ISIN a partition of its own — its bars belong to the survivor's."""

    def survivor_of(isin: str) -> str:
        return RELIANCE if isin == HDFC else isin

    report = materialize_missing(
        _conn(),
        data_root=lake,
        history_for={RELIANCE: (HDFC, RELIANCE)},
        survivor_of=survivor_of,
    )

    assert report.skipped_retired == 1
    assert sorted(r.isin for r in report.written) == sorted((RELIANCE, INFOSYS))
    assert not l2_isin_partition_path(PRICES_ADJUSTED_DATASET, HDFC, data_root=lake).exists()
    # The survivor's partition carries both ISINs' bars, all keyed to the survivor.
    bars = read_adjusted(RELIANCE, data_root=lake)
    assert len(bars) == 2 * len(SESSIONS)
    assert {b.isin for b in bars} == {RELIANCE}


# ── rebuild_truncated (W3): L1 grew backwards under partitions that already exist ─────────────


def _write_session(root: Path, day: date, i: int) -> None:
    write_prices_raw(
        [
            _row(RELIANCE, day, series="EQ", close=f"{2400 + i}.50"),
            _row(INFOSYS, day, series="EQ", close=f"{1500 + i}.25"),
            _row(HDFC, day, series="EQ", close=f"{1600 + i}.00"),
        ],
        exchange=Exchange.NSE,
        data_root=root,
    )


@pytest.fixture
def grown(tmp_path: Path) -> Path:
    """L2 built when L1 began at SESSIONS[1]; then the first session was backfilled into L1 for
    RELIANCE and INFOSYS only — HDFC's history genuinely begins at SESSIONS[1]."""
    for i, day in enumerate(SESSIONS[1:], start=1):
        _write_session(tmp_path, day, i)
    materialize_missing(_conn(), data_root=tmp_path)
    write_prices_raw(
        [
            _row(RELIANCE, SESSIONS[0], series="EQ", close="2400.50"),
            _row(INFOSYS, SESSIONS[0], series="EQ", close="1500.25"),
        ],
        exchange=Exchange.NSE,
        data_root=tmp_path,
    )
    return tmp_path


def _part(root: Path, isin: str) -> Path:
    return l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=root)


def test_a_partition_older_than_its_l1_history_is_found(grown: Path) -> None:
    report = rebuild_truncated(_conn(), data_root=grown, dry_run=True)
    assert report.truncated == {
        RELIANCE: (SESSIONS[1], SESSIONS[0]),
        INFOSYS: (SESSIONS[1], SESSIONS[0]),
    }
    assert HDFC not in report.truncated, "a partition that already spans its L1 is not stale"


def test_a_dry_run_writes_nothing(grown: Path) -> None:
    before = {isin: _part(grown, isin).read_bytes() for isin in (RELIANCE, INFOSYS, HDFC)}
    report = rebuild_truncated(_conn(), data_root=grown, dry_run=True)
    assert report.written == ()
    assert {isin: _part(grown, isin).read_bytes() for isin in before} == before


def test_the_rebuild_extends_to_l1_and_equals_a_fresh_build(grown: Path, tmp_path: Path) -> None:
    hdfc_before = _part(grown, HDFC).read_bytes()
    report = rebuild_truncated(_conn(), data_root=grown)
    assert sorted(r.isin for r in report.written) == sorted((RELIANCE, INFOSYS))
    assert all(r.from_date == SESSIONS[0] for r in report.written)
    assert _part(grown, HDFC).read_bytes() == hdfc_before, "an up-to-date partition was rewritten"

    fresh = tmp_path / "fresh"
    for i, day in enumerate(SESSIONS):
        _write_session(fresh, day, i)
    materialize_missing(_conn(), data_root=fresh)
    assert _part(grown, RELIANCE).read_bytes() == _part(fresh, RELIANCE).read_bytes()


def test_a_second_pass_finds_nothing(grown: Path) -> None:
    rebuild_truncated(_conn(), data_root=grown)
    again = rebuild_truncated(_conn(), data_root=grown)
    assert again.truncated == {} and again.written == ()


def test_truncation_is_judged_over_the_whole_lineage_chain(tmp_path: Path) -> None:
    """RELIANCE's partition is built from its own bars; its chain says HDFC is its earlier ISIN,
    whose bars start sooner — so the stitched series is truncated even though RELIANCE's own is
    not, and the rebuild carries the retired ISIN's bars keyed to the survivor."""
    write_prices_raw(
        [_row(HDFC, SESSIONS[0], series="EQ", close="1600.00")],
        exchange=Exchange.NSE,
        data_root=tmp_path,
    )
    for i, day in enumerate(SESSIONS[1:], start=1):
        write_prices_raw(
            [_row(RELIANCE, day, series="EQ", close=f"{2400 + i}.50")],
            exchange=Exchange.NSE,
            data_root=tmp_path,
        )
    con = open_connection()
    try:
        materialize_isin(
            RELIANCE,
            chain=FactorChain(isin=RELIANCE, rows=()),
            actions=(),
            con=con,
            data_root=tmp_path,
        )
    finally:
        con.close()
    history = {RELIANCE: (HDFC, RELIANCE)}
    assert rebuild_truncated(_conn(), data_root=tmp_path, dry_run=True).truncated == {}
    report = rebuild_truncated(_conn(), data_root=tmp_path, history_for=history)
    assert report.truncated == {RELIANCE: (SESSIONS[1], SESSIONS[0])}
    bars = read_adjusted(RELIANCE, data_root=tmp_path)
    assert [b.trade_date for b in bars] == list(SESSIONS)
    assert {b.isin for b in bars} == {RELIANCE}


# A retired ISIN's partition is left alone by --extend: its chain is only itself, so extending it
# would duplicate the pre-reissue history the survivor's stitched partition carries.


def _retired_into_reliance(isin: str) -> str:
    return RELIANCE if isin == HDFC else isin


_REISSUE = {RELIANCE: (HDFC, RELIANCE)}


@pytest.fixture
def reissued(tmp_path: Path) -> Path:
    """HDFC traded to SESSIONS[1] and was reissued as RELIANCE, which trades from SESSIONS[2]. Both
    got partitions from their own bars while L1 began at SESSIONS[1] (HDFC's is the pre-lineage
    leftover); then HDFC's SESSIONS[0] bar was backfilled — so HDFC's own partition and RELIANCE's
    stitched one are now both truncated, and only the survivor's may be extended."""
    write_prices_raw(
        [_row(HDFC, SESSIONS[1], series="EQ", close="1601.00")],
        exchange=Exchange.NSE,
        data_root=tmp_path,
    )
    write_prices_raw(
        [_row(RELIANCE, SESSIONS[2], series="EQ", close="2402.50")],
        exchange=Exchange.NSE,
        data_root=tmp_path,
    )
    for isin in (HDFC, RELIANCE):
        materialize_isin(
            isin, chain=FactorChain(isin=isin, rows=()), actions=(), data_root=tmp_path
        )
    write_prices_raw(
        [_row(HDFC, SESSIONS[0], series="EQ", close="1600.00")],
        exchange=Exchange.NSE,
        data_root=tmp_path,
    )
    return tmp_path


def test_extend_neither_extends_nor_creates_a_retired_partition(reissued: Path) -> None:
    hdfc_before = _part(reissued, HDFC).read_bytes()
    rebuild_truncated(
        _conn(), data_root=reissued, history_for=_REISSUE, survivor_of=_retired_into_reliance
    )
    assert _part(reissued, HDFC).read_bytes() == hdfc_before, "a retired partition was extended"
    assert [b.trade_date for b in read_adjusted(HDFC, data_root=reissued)] == [SESSIONS[1]]
    assert materialized_isins(data_root=reissued) == frozenset({HDFC, RELIANCE}), "nothing new"


def test_extend_still_extends_the_survivor_over_its_chain(reissued: Path) -> None:
    report = rebuild_truncated(
        _conn(), data_root=reissued, history_for=_REISSUE, survivor_of=_retired_into_reliance
    )
    assert [r.isin for r in report.written] == [RELIANCE], "only the survivor is rebuilt"
    bars = read_adjusted(RELIANCE, data_root=reissued)
    assert [b.trade_date for b in bars] == list(SESSIONS)
    assert {b.isin for b in bars} == {RELIANCE}


def test_a_dry_run_counts_the_skipped_retired_partitions(reissued: Path) -> None:
    report = rebuild_truncated(
        _conn(),
        data_root=reissued,
        history_for=_REISSUE,
        survivor_of=_retired_into_reliance,
        dry_run=True,
    )
    assert report.truncated == {RELIANCE: (SESSIONS[2], SESSIONS[0])}
    assert report.skipped_retired == 1, "one retired partition on disk, one skip"
    assert report.partitions == 2 and report.written == ()


def test_the_extend_cli_passes_the_lineage_and_prints_the_skip_count(
    reissued: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`l2_fill --extend --dry-run` wires the resolver's `survivor_of` through and reports it."""

    class _Resolver:
        survivor_of = staticmethod(_retired_into_reliance)

    class _Session:
        def __enter__(self) -> Connection:
            return _conn()

        def __exit__(self, *exc: object) -> None:
            return None

    class _Settings:
        data_root = reissued

    monkeypatch.setattr(l2_fill, "get_settings", _Settings)
    monkeypatch.setattr(l2_fill, "connect", _Session)
    monkeypatch.setattr(l2_fill, "_history", lambda conn: (dict(_REISSUE), _Resolver()))
    monkeypatch.setattr(
        l2_fill,
        "extension_coverage",
        lambda conn, truncated: ExtensionCoverage(len(truncated), 0, 0, 0, 0, {}),
    )
    before = {isin: _part(reissued, isin).read_bytes() for isin in (HDFC, RELIANCE)}

    assert l2_fill.main(["--extend", "--dry-run"]) == 0

    lines = dict(line.split(maxsplit=1) for line in capsys.readouterr().out.splitlines())
    assert lines["truncated"] == "1"
    assert lines["skipped_retired"] == "1"
    assert lines["written"] == "0"
    assert {isin: _part(reissued, isin).read_bytes() for isin in before} == before


class _RecordingStore:
    """Records each query's parameters and answers every count with 1."""

    def __init__(self) -> None:
        self.params: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> _RecordingStore:
        self.params.append(params)
        return self

    def fetchone(self) -> tuple[int]:
        return (1,)


def test_extension_coverage_measures_the_new_window_only() -> None:
    """The window is `[L1 first, L2 first)` — the bars the extension adds. Inverted, it would be
    empty for every ISIN and report zero coverage over a window it never looked at."""
    store = _RecordingStore()
    coverage = extension_coverage(
        cast(Connection, store), {RELIANCE: (date(2016, 9, 2), date(2011, 6, 22))}
    )
    assert coverage.truncated == 1
    assert coverage.with_factors == 1 and coverage.with_unreconciled_level_action == 1
    assert coverage.new_first_year == {2011: 1}
    for params in store.params:
        assert params[0] == RELIANCE
        assert params[-2:] == (date(2011, 6, 22), date(2016, 9, 2))
