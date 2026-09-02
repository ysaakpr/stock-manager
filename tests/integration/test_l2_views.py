"""M2.5 — L2 adjusted views (DuckDB) over L1 Parquet + the M2.4 factor chain.

Every acceptance criterion of the task is a test here:

  1. adjusted series for a stock with a known split matches hand-computed values
     (`test_known_split_matches_hand_computed`, `test_total_return_reinvests_dividend`)
  2. wiping L2 entirely and rebuilding produces identical output
     (`test_wipe_and_rebuild_is_byte_identical`)
  3. L2 build is incremental per ISIN on CA-triggered invalidation, not a full-market rebuild
     (`test_materialize_one_isin_leaves_others_untouched`, and against Postgres
     `test_rebuild_invalidated_drains_only_flagged_isins`)

The offline tests write raw L1 `prices_raw` partitions under `tmp_path`, adjust them with an
in-memory factor chain built from `dataplatform.corpactions.factors`, and read the materialized L2
back through DuckDB — no postgres, no network, deterministic. One test runs the real
M2.4 → M2.5 seam end to end against a scratch Postgres (a corporate action recompute writes the
factors and the invalidation, and `rebuild_invalidated` drains it); it skips loudly if postgres is
unreachable, but it is not the sole evidence for any criterion.

The known split is IRCTC's 2021 1:5 face-value split (₹10 → ₹2, `price_factor = 0.2`), one of the
§4.3 golden cases, with a later cash dividend so the total-return series is exercised too.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.corpactions.factors import FactorChain, build_factor_chain
from dataplatform.corpactions.taxonomy import (
    ActionType,
    DividendKind,
    DividendTerms,
    FaceValueTerms,
)
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.store.l2 import (
    PRICES_ADJUSTED_DATASET,
    AdjustedBar,
    build_adjusted_bars,
    load_factor_chain,
    materialize_isin,
    materialize_isins,
    open_connection,
    read_adjusted,
    read_raw_bars_from_l1,
    rebuild_invalidated,
    register_adjusted_view,
    register_raw_view,
    wipe_adjusted,
)
from dataplatform.store.paths import l1_partition_path, l2_isin_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

# IRCTC and a second, event-free control security.
IRCTC: Final = "INE335Y01020"
CONTROL: Final = "INE009A01021"

SPLIT_EX: Final = date(2021, 10, 19)  # 1:5 face-value split, ₹10 → ₹2
DIV_EX: Final = date(2021, 12, 1)  # ₹10/share cash dividend, on a later session
CLOCK: Final = FrozenClock(datetime(2026, 9, 2, 18, 30, tzinfo=IST))

# The three raw IRCTC sessions the tests adjust: one before the split, one on the split ex-date,
# one on the dividend ex-date (which is also the day the dividend is reinvested against 2021-10-19).
_IRCTC_RAW: Final = [
    (date(2021, 6, 1), Decimal("1000"), 500),
    (SPLIT_EX, Decimal("210"), 400),
    (DIV_EX, Decimal("900"), 300),
]
_CONTROL_RAW: Final = [
    (date(2021, 6, 1), Decimal("100"), 100),
    (DIV_EX, Decimal("120"), 150),
]

_PRICE_Q: Final = Decimal("0.0001")


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


def _write_l1_partition(
    data_root: Path, trade_date: date, rows: list[tuple[str, str, Decimal, int]]
) -> None:
    """Write one raw `prices_raw` L1 partition (one date, several securities) under `data_root`.

    Minimal but schema-true: OHLC are all set to the close and the delivery columns are null, which
    is all the L2 adjuster reads (OHLC x price factor, volume x qty factor). Uses the real
    `PRICES_RAW_SCHEMA` so the DuckDB read exercises the actual on-disk contract.
    """
    records = [
        {
            "isin": isin,
            "exchange": "NSE",
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
        for isin, symbol, close, volume in rows
    ]
    path = l1_partition_path(PRICES_RAW_DATASET, trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=PRICES_RAW_SCHEMA)
    pq.write_table(table, path, compression="snappy", version="2.6")


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A data root whose L1 holds three IRCTC sessions and two control sessions across dates."""
    by_date: dict[date, list[tuple[str, str, Decimal, int]]] = {}
    for d, close, vol in _IRCTC_RAW:
        by_date.setdefault(d, []).append((IRCTC, "IRCTC", close, vol))
    for d, close, vol in _CONTROL_RAW:
        by_date.setdefault(d, []).append((CONTROL, "INFY", close, vol))
    for d, rows in by_date.items():
        _write_l1_partition(tmp_path, d, rows)
    return tmp_path


@pytest.fixture
def irctc_actions() -> tuple[CorporateAction, ...]:
    """IRCTC's reconciled actions: the 1:5 split and a later ₹10 cash dividend."""
    split = CorporateAction(
        isin=IRCTC,
        ex_date=SPLIT_EX,
        action_type=ActionType.SPLIT,
        terms=FaceValueTerms(from_value=Decimal("10"), to_value=Decimal("2")),
        source="nse_corp_actions",
        raw_text="FV SPLIT FROM RS.10/- TO RS.2/-",
        knowable_date=SPLIT_EX,
    )
    dividend = CorporateAction(
        isin=IRCTC,
        ex_date=DIV_EX,
        action_type=ActionType.DIVIDEND,
        terms=DividendTerms(dividend_kind=DividendKind.FINAL, amount_inr=Decimal("10")),
        source="nse_corp_actions",
        raw_text="DIVIDEND RS.10 PER SHARE",
        knowable_date=DIV_EX,
    )
    return (split, dividend)


@pytest.fixture
def irctc_chain(irctc_actions: tuple[CorporateAction, ...]) -> FactorChain:
    """The price/qty factor chain M2.4 builds from IRCTC's actions (split only moves the basis)."""
    return build_factor_chain(irctc_actions)


# ── acceptance 1: a known split matches hand-computed values ───────────────────────────────────


def _by_date(bars: tuple[AdjustedBar, ...]) -> dict[date, AdjustedBar]:
    return {b.trade_date: b for b in bars}


def test_known_split_matches_hand_computed(
    lake: Path, irctc_chain: FactorChain, irctc_actions: tuple[CorporateAction, ...]
) -> None:
    """A 1:5 split back-adjusts pre-split OHLCV by 0.2 (price) and 5 (volume); post-split is raw.

    The whole point of the golden convention: a ₹1000 close before a 1:5 split reads as ₹200 in the
    current share basis, a 500-share volume as 2500, and the price *on and after* the ex-date is
    already post-split so it is left unscaled.
    """
    materialize_isin(IRCTC, chain=irctc_chain, actions=irctc_actions, data_root=lake)
    bars = _by_date(read_adjusted(IRCTC, data_root=lake))

    pre = bars[date(2021, 6, 1)]
    assert pre.adj_close == Decimal("200")  # 1000 x 0.2
    assert pre.adj_open == Decimal("200")
    assert pre.adj_high == Decimal("200")
    assert pre.adj_low == Decimal("200")
    assert pre.adj_volume == Decimal("2500")  # 500 x 5
    assert pre.cum_price_factor == Decimal("0.2")
    assert pre.cum_qty_factor == Decimal("5")

    on_ex = bars[SPLIT_EX]
    assert on_ex.adj_close == Decimal("210")  # price on the ex-date is already post-split
    assert on_ex.adj_volume == Decimal("400")
    assert on_ex.cum_price_factor == Decimal("1")

    post = bars[DIV_EX]
    assert post.adj_close == Decimal("900")  # newest segment, unscaled
    assert post.cum_price_factor == Decimal("1")


def test_total_return_reinvests_dividend(
    lake: Path, irctc_chain: FactorChain, irctc_actions: tuple[CorporateAction, ...]
) -> None:
    """The total-return close reinvests the ₹10 dividend; price-adjusted ignores it (acceptance 4).

    The dividend's ex-date prior close is ₹210, so its back-adjustment factor is (210-10)/210 =
    200/210. Every session *before* the ex-date is scaled by it in the total-return series and by
    nothing in the price-adjusted series; on and after the ex-date the two agree.
    """
    materialize_isin(IRCTC, chain=irctc_chain, actions=irctc_actions, data_root=lake)
    bars = _by_date(read_adjusted(IRCTC, data_root=lake))

    div_factor = Decimal("200") / Decimal("210")

    pre = bars[date(2021, 6, 1)]
    expected_pre_tr = (Decimal("1000") * Decimal("0.2") * div_factor).quantize(_PRICE_Q)
    assert pre.tr_close == expected_pre_tr
    assert pre.tr_close != pre.adj_close  # dividend makes the two diverge before the ex-date

    on_ex = bars[SPLIT_EX]
    assert on_ex.tr_close == (Decimal("210") * div_factor).quantize(_PRICE_Q)  # = 200

    post = bars[DIV_EX]
    assert post.tr_close == Decimal("900")  # on the ex-date the dividend is not yet reinvested
    assert post.tr_close == post.adj_close


def test_total_return_equals_price_adjusted_without_dividends(lake: Path) -> None:
    """An event-free ISIN's total-return series equals its price-adjusted series equals raw.

    The other half of acceptance 4, and the empty-chain case: no factor row means every column is
    the raw close.
    """
    materialize_isin(CONTROL, chain=FactorChain(isin=CONTROL), actions=(), data_root=lake)
    bars = _by_date(read_adjusted(CONTROL, data_root=lake))

    assert bars[date(2021, 6, 1)].adj_close == Decimal("100")
    assert bars[date(2021, 6, 1)].tr_close == Decimal("100")
    assert bars[DIV_EX].adj_close == Decimal("120")
    assert bars[DIV_EX].tr_close == Decimal("120")
    for bar in bars.values():
        assert bar.cum_price_factor == Decimal("1")
        assert bar.cum_qty_factor == Decimal("1")


def test_build_adjusted_bars_is_pure(
    lake: Path, irctc_chain: FactorChain, irctc_actions: tuple[CorporateAction, ...]
) -> None:
    """The adjustment math is a pure function of raw bars + chain + actions — no I/O in the core."""
    raw = read_raw_bars_from_l1(IRCTC, data_root=lake)
    first = build_adjusted_bars(IRCTC, irctc_chain, irctc_actions, raw)
    second = build_adjusted_bars(IRCTC, irctc_chain, irctc_actions, raw)
    assert first == second
    assert {b.trade_date for b in first} == {d for d, _, _ in _IRCTC_RAW}


# ── acceptance 2: wipe and rebuild is identical ────────────────────────────────────────────────


def test_wipe_and_rebuild_is_byte_identical(
    lake: Path, irctc_chain: FactorChain, irctc_actions: tuple[CorporateAction, ...]
) -> None:
    """L2 holds no primary data: deleting it and rebuilding from L1 + factors is byte-identical.

    This is invariant #3 made operational — nothing in L2 is a source of truth, so it must be
    reconstructable exactly. Compares raw file bytes, not just the decoded rows.
    """
    materialize_isin(IRCTC, chain=irctc_chain, actions=irctc_actions, data_root=lake)
    materialize_isin(CONTROL, chain=FactorChain(isin=CONTROL), actions=(), data_root=lake)

    path = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, IRCTC, data_root=lake)
    before = path.read_bytes()

    removed = wipe_adjusted(data_root=lake)
    assert removed == 2
    assert not path.exists()

    materialize_isin(IRCTC, chain=irctc_chain, actions=irctc_actions, data_root=lake)
    assert path.read_bytes() == before


def test_rebuild_after_partial_delete_is_identical(
    lake: Path, irctc_chain: FactorChain, irctc_actions: tuple[CorporateAction, ...]
) -> None:
    """Re-materializing over an existing partition (no wipe) also reproduces the same bytes."""
    materialize_isin(IRCTC, chain=irctc_chain, actions=irctc_actions, data_root=lake)
    path = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, IRCTC, data_root=lake)
    before = path.read_bytes()
    materialize_isin(IRCTC, chain=irctc_chain, actions=irctc_actions, data_root=lake)
    assert path.read_bytes() == before


# ── acceptance 3: incremental per ISIN, not a full-market rebuild ──────────────────────────────


def test_materialize_one_isin_leaves_others_untouched(
    lake: Path, irctc_chain: FactorChain, irctc_actions: tuple[CorporateAction, ...]
) -> None:
    """Rebuilding one ISIN rewrites exactly its partition and touches no other file on disk.

    The core of "incremental per ISIN, not full-market": the CONTROL partition's bytes and mtime are
    unchanged after IRCTC is rebuilt, and no third partition is conjured.
    """
    con = open_connection()
    materialize_isins(
        [(irctc_chain, irctc_actions), (FactorChain(isin=CONTROL), ())],
        con=con,
        data_root=lake,
    )
    control_path = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, CONTROL, data_root=lake)
    control_before = control_path.read_bytes()
    control_mtime = control_path.stat().st_mtime_ns

    # Rebuild only IRCTC.
    materialize_isin(IRCTC, chain=irctc_chain, actions=irctc_actions, con=con, data_root=lake)

    assert control_path.read_bytes() == control_before
    assert control_path.stat().st_mtime_ns == control_mtime

    dataset_root = control_path.parent.parent
    partitions = sorted(p.name for p in dataset_root.glob("isin=*"))
    assert partitions == [f"isin={CONTROL}", f"isin={IRCTC}"]
    con.close()


def test_isin_absent_from_l1_writes_nothing(
    lake: Path, irctc_chain: FactorChain, irctc_actions: tuple[CorporateAction, ...]
) -> None:
    """An ISIN with no L1 history writes no partition and clears any stale one (still identical)."""
    absent = "INE123A01012"
    report = materialize_isin(absent, chain=FactorChain(isin=absent), actions=(), data_root=lake)
    assert report.rows_written == 0
    assert report.path is None
    assert not l2_isin_partition_path(PRICES_ADJUSTED_DATASET, absent, data_root=lake).exists()


def test_adjusted_view_spans_all_partitions(
    lake: Path, irctc_chain: FactorChain, irctc_actions: tuple[CorporateAction, ...]
) -> None:
    """A DuckDB view over the materialized L2 sees every ISIN's partition as one relation (§4.2)."""
    con = open_connection()
    materialize_isins(
        [(irctc_chain, irctc_actions), (FactorChain(isin=CONTROL), ())],
        con=con,
        data_root=lake,
    )
    register_adjusted_view(con, view="adj", data_root=lake)
    isins = {r[0] for r in con.execute("SELECT DISTINCT isin FROM adj").fetchall()}
    assert isins == {IRCTC, CONTROL}
    total = con.execute("SELECT count(*) FROM adj").fetchone()
    assert total is not None and total[0] == len(_IRCTC_RAW) + len(_CONTROL_RAW)

    # The raw-L1 view is the same idea one layer down: every raw session as one relation.
    register_raw_view(con, view="raw", data_root=lake)
    raw_total = con.execute("SELECT count(*) FROM raw WHERE isin = ?", [IRCTC]).fetchone()
    assert raw_total is not None and raw_total[0] == len(_IRCTC_RAW)
    con.close()


# ── the real M2.4 → M2.5 seam against Postgres ─────────────────────────────────────────────────


pytestmark_db = pytest.mark.integration

SCRATCH_DB = f"trading_m2_5_l2_views_{os.getpid()}"
INGESTED_AT = datetime(2026, 9, 2, 18, 30, tzinfo=IST)


def _terms_json(terms: FaceValueTerms | DividendTerms) -> dict[str, object]:
    return terms.model_dump(mode="json")


@pytest.fixture(scope="module")
def scratch_settings() -> Iterator[object]:
    import psycopg

    from dataplatform.config import Settings
    from dataplatform.store.db import connect, with_dbname
    from dataplatform.store.migrate import migrate

    def settings_for(dbname: str) -> Settings:
        return Settings(database_url=with_dbname(Settings().database_url, dbname))

    admin = settings_for("postgres")
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        conn.close()

    migrate(settings_for(SCRATCH_DB), clock=CLOCK)
    yield settings_for(SCRATCH_DB)

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def db_conn(scratch_settings: object) -> Iterator[object]:
    from dataplatform.store.db import connection

    with connection(scratch_settings) as live:  # type: ignore[arg-type]
        try:
            yield live
        finally:
            live.rollback()


def _seed_security(conn: object, isin: str, name: str) -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO security_master "
        "(isin, name, primary_exchange, status, first_seen_date, created_at, updated_at) "
        "VALUES (%s, %s, 'NSE', 'ACTIVE', %s, %s, %s)",
        (isin, name, date(2000, 1, 1), INGESTED_AT, INGESTED_AT),
    )


def _insert_action(conn: object, action: CorporateAction, amount: Decimal | None) -> None:
    from psycopg.types.json import Jsonb

    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO corporate_actions "
        "(isin, ex_date, action_type, ratio_terms, dividend_amount_inr, knowable_date, "
        " source, raw_text, reconciled, recorded_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true, %s)",
        (
            action.isin,
            action.ex_date,
            action.action_type.value,
            Jsonb(_terms_json(action.terms)),  # type: ignore[arg-type]
            amount,
            action.knowable_date,
            action.source,
            action.raw_text,
            INGESTED_AT,
        ),
    )


def test_rebuild_invalidated_drains_only_flagged_isins(
    db_conn: object,
    lake: Path,
    irctc_actions: tuple[CorporateAction, ...],
) -> None:
    """The full seam: a CA recompute writes factors + an invalidation; `rebuild_invalidated` drains.

    Exercises the real M2.4 → M2.5 path: `recompute_isin` (M2.4) persists IRCTC's factor chain and
    records an open `l2_invalidation`, then `rebuild_invalidated` (M2.5) materializes exactly that
    ISIN's L2 from L1 + the persisted factors and marks the row resolved. The control ISIN, never
    invalidated, gets no L2 partition — proof the drain is per flagged ISIN, not a market sweep.
    """
    from dataplatform.corpactions.recompute import recompute_isin

    _seed_security(db_conn, IRCTC, "IRCTC")
    _seed_security(db_conn, CONTROL, "INFY")
    _insert_action(db_conn, irctc_actions[0], None)
    _insert_action(db_conn, irctc_actions[1], Decimal("10"))

    # M2.4: recompute the chain and flag L2 stale.
    result = recompute_isin(db_conn, IRCTC, clock=CLOCK)  # type: ignore[arg-type]
    assert result.factor_rows_written == 1
    assert result.l2_invalidated is True

    # load_factor_chain reads back exactly what the recompute wrote.
    chain = load_factor_chain(db_conn, IRCTC)  # type: ignore[arg-type]
    assert chain.price_factor_asof(date(2021, 6, 1)) == Decimal("0.2")

    # M2.5: drain the queue.
    reports = rebuild_invalidated(db_conn, clock=CLOCK, data_root=lake)  # type: ignore[arg-type]
    assert [r.isin for r in reports] == [IRCTC]
    assert reports[0].rows_written == len(_IRCTC_RAW)

    # The invalidation is resolved, and only after the rebuild wrote.
    open_rows = db_conn.execute(  # type: ignore[attr-defined]
        "SELECT count(*) FROM l2_invalidation WHERE NOT resolved"
    ).fetchone()
    assert open_rows[0] == 0

    # IRCTC materialized to the same values as the offline path; CONTROL never touched.
    bars = _by_date(read_adjusted(IRCTC, data_root=lake))
    assert bars[date(2021, 6, 1)].adj_close == Decimal("200")
    assert not l2_isin_partition_path(PRICES_ADJUSTED_DATASET, CONTROL, data_root=lake).exists()

    # A second drain with nothing open is a no-op.
    assert rebuild_invalidated(db_conn, clock=CLOCK, data_root=lake) == ()  # type: ignore[arg-type]
