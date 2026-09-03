"""L2 adjusted views (M2.5, module D4): back-adjusted OHLCV per ISIN, over L1 + factors.

§4.2 calls L2 *"adjusted series (via factors), … Parquet views / materialized DuckDB, fully
recomputable, rebuilt retroactively when a new CA lands"*, and §4.3 rule 1 is categorical:
*"adjusted series = raw x cumulative factor, derived on read/materialization, never primary."*
This module is that materialization. It reads one ISIN's raw OHLCV out of L1 through DuckDB,
multiplies it by the cumulative factors M2.4 computed (prices by ``cum_price_factor``, volume by
``cum_qty_factor``), and writes the result to a per-ISIN L2 Parquet partition — the hot path the
query layer (M4.1) and the backtest (M2.9) read. Two derived series are exposed side by side:

* **price-adjusted** OHLCV — splits and bonuses only; a cash dividend is *not* a change in the
  share basis and so does not move these columns (the factor convention in
  ``dataplatform.corpactions.factors``).
* **total-return** close — the same back-adjustment with cash dividends reinvested; it equals the
  price-adjusted close exactly in the absence of dividends and diverges by the compounded yield
  otherwise (§4.3, M2.4 acceptance 4).

Three properties, each mapped to an acceptance criterion of the task:

* **The math lives in exactly one place.** Every adjusted value is ``raw x factor`` where the
  factor comes from the M2.4 chain (``dataplatform.corpactions.factors``) — this module never
  re-implements the adjustment arithmetic, the same reason there is one cost model (invariant #4).
  So "adjusted series for a known split matches hand-computed values" (acceptance 1) is a property
  of the factor chain that L2 merely carries onto disk.

* **Fully recomputable — deletable and byte-identical on rebuild.** L2 holds no primary data:
  ``wipe_adjusted`` removes it entirely and ``materialize_isin`` rebuilds it from L1 + factors
  alone. The write is deterministic (rows sorted by a total key, every value quantised to a fixed
  scale, the file written whole via a staging rename), so a rebuild is byte-for-byte the previous
  file (acceptance 2). This is invariant #3 made operational: nothing here is a source of truth.

* **Incremental per ISIN, never a full-market rebuild.** The unit of L2 is one ISIN's partition
  (``L2/prices_adjusted/isin=…/part.parquet``), because a corporate action re-scales exactly one
  ISIN's history (§4.3 rule 2) and back-adjustment rewrites that ISIN whole but touches no other.
  ``rebuild_invalidated`` drains the ``l2_invalidation`` queue M2.4's recompute writes and rebuilds
  only the flagged ISINs, marking each resolved — never rescanning the market (acceptance 3).

Determinism and the injected clock: the on-disk bytes depend only on L1 and the factors, never on
when the build ran; ``rebuild_invalidation`` stamps ``resolved_at`` from an injected ``Clock``
(B10). Money and factors are ``Decimal`` throughout — a float in an adjusted price is a bug.
Offline by construction: DuckDB reads local Parquet, nothing fetches.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING, Final

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from dataplatform.clock import Clock
from dataplatform.corpactions.factors import FactorChain, FactorRow, total_return_series
from dataplatform.corpactions.reconcile import load_reconciled_actions
from dataplatform.ingest.models import ISIN_PATTERN
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection
from dataplatform.store.paths import (
    DEFAULT_PART_FILENAME,
    Layer,
    l2_isin_partition_path,
    layer_root,
)
from dataplatform.store.schemas import PRICES_RAW_DATASET

if TYPE_CHECKING:
    # Annotation-only, kept off the runtime import graph exactly as factors.py does: importing
    # `ingest.corp_actions` eagerly here would deepen the corpactions↔ingest cycle. At runtime this
    # module only forwards the actions it is handed into `total_return_series`.
    from dataplatform.corpactions.factors import PricePoint
    from dataplatform.ingest.corp_actions import CorporateAction

__all__ = [
    "PRICES_ADJUSTED_DATASET",
    "PRICES_ADJUSTED_SCHEMA",
    "AdjustedBar",
    "L2WriteReport",
    "RawBar",
    "build_adjusted_bars",
    "load_factor_chain",
    "materialize_isin",
    "materialize_isins",
    "open_connection",
    "read_adjusted",
    "read_raw_bars_from_l1",
    "rebuild_invalidated",
    "register_adjusted_view",
    "register_raw_view",
    "wipe_adjusted",
]

_LOG = get_logger(__name__)

#: L2 dataset — `data/L2/prices_adjusted/isin=<ISIN>/part.parquet` (§4.2, partitioned per ISIN).
PRICES_ADJUSTED_DATASET: Final = "prices_adjusted"

#: Quanta that make the write byte-deterministic (same rationale as L1's `schemas._PRICE_Q`): a
#: fixed decimal scale means `200.0` and `200.00` land as the identical stored integer, so a rebuild
#: from the same L1 + factors is byte-equal. Prices/returns at four places; the cumulative factors
#: at eighteen, because a factor can be non-terminating (a 2:1 bonus's 1/3) and must round the same
#: way every rebuild.
_PRICE_Q: Final = Decimal("0.0001")
_VOLUME_Q: Final = Decimal("0.0001")
_FACTOR_Q: Final = Decimal("0.000000000000000001")

#: The `prices_adjusted` Parquet schema — declared once, enforced on every write. Every price column
#: is an *adjusted* one (this is L2, where invariant #3 says adjusted series belong); the grain is
#: `(isin, exchange, trade_date)` because one ISIN can trade on both NSE and BSE and each venue's
#: raw series adjusts on its own. `cum_price_factor`/`cum_qty_factor` are carried for audit: the
#: factor each row was multiplied by, so a reader can see *why* a 2019 close reads as it does.
PRICES_ADJUSTED_SCHEMA: Final = pa.schema(
    [
        pa.field("isin", pa.string(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("trade_date", pa.date32(), nullable=False),
        pa.field("adj_open", pa.decimal128(28, 4), nullable=False),
        pa.field("adj_high", pa.decimal128(28, 4), nullable=False),
        pa.field("adj_low", pa.decimal128(28, 4), nullable=False),
        pa.field("adj_close", pa.decimal128(28, 4), nullable=False),
        pa.field("adj_volume", pa.decimal128(38, 4), nullable=False),
        pa.field("tr_close", pa.decimal128(28, 4), nullable=False),
        pa.field("cum_price_factor", pa.decimal128(38, 18), nullable=False),
        pa.field("cum_qty_factor", pa.decimal128(38, 18), nullable=False),
    ]
)


class RawBar(BaseModel):
    """One venue's raw OHLCV for one ISIN on one session — the input read from L1.

    A typed carrier rather than a bare tuple (the "no bare dicts as interfaces" rule): the field
    order out of a SELECT is exactly the kind of thing that silently transposes `open` and `close`.
    `close` is the raw traded close from L1 (invariant #3, never adjusted). `volume` is
    `total_traded_qty`; it may be zero (a listed security with no trades that session), so it is the
    one non-price field allowed to be non-positive.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN)
    exchange: str = Field(min_length=1)
    trade_date: date
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)


class AdjustedBar(BaseModel):
    """One ISIN's back-adjusted OHLCV for one session on one venue — the canonical L2 row.

    What it does: carry the price-adjusted OHLC (`raw x cum_price_factor`), the qty-adjusted volume
    (`raw x cum_qty_factor`), the total-return close (dividends reinvested), and the two cumulative
    factors that produced them.
    What it assumes: the factors are the M2.4 chain's, so the values are re-derivable from L1 + that
    chain and nothing here is primary.
    What it never does: hold a raw price (that is L1's job) — every price column is adjusted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN)
    exchange: str = Field(min_length=1)
    trade_date: date
    adj_open: Decimal = Field(gt=0, description="open x cum_price_factor, current share basis")
    adj_high: Decimal = Field(gt=0)
    adj_low: Decimal = Field(gt=0)
    adj_close: Decimal = Field(gt=0, description="split/bonus-adjusted close; no dividend effect")
    adj_volume: Decimal = Field(ge=0, description="total_traded_qty x cum_qty_factor")
    tr_close: Decimal = Field(gt=0, description="total-return close: adj_close with dividends in")
    cum_price_factor: Decimal = Field(gt=0)
    cum_qty_factor: Decimal = Field(gt=0)


@dataclass(frozen=True, slots=True)
class L2WriteReport:
    """The outcome of materializing one ISIN's L2 partition — the counts a caller checks.

    `path` is the written partition, or `None` when the ISIN had no L1 history to adjust (in which
    case any stale partition was removed, so a rebuild is still identical: absent). `from_date`/
    `to_date` bound the series written; both are `None` for an empty ISIN.
    """

    isin: str
    path: Path | None
    rows_written: int
    from_date: date | None
    to_date: date | None


def open_connection() -> duckdb.DuckDBPyConnection:
    """A fresh in-memory DuckDB connection for reading the Parquet lake.

    DuckDB is the query engine over L1/L2 Parquet (§4.5); an in-memory connection holds no state
    between calls, so callers that read many ISINs may pass one connection to amortise startup, and
    callers that read one may let the materializer open its own.
    """
    return duckdb.connect(":memory:")


def read_raw_bars_from_l1(
    isin: str, *, con: duckdb.DuckDBPyConnection | None = None, data_root: Path | None = None
) -> tuple[RawBar, ...]:
    """Read one ISIN's full raw OHLCV history out of L1 through DuckDB — a view over L1 Parquet.

    Scans every `prices_raw` date partition and filters to `isin` (invariant #2 — the join key is
    the ISIN the row carries, never a symbol), which is the columnar scan DuckDB exists for
    (§4.5(a), "adjusted OHLCV series per ISIN across years"). Returns the bars ordered by
    `(exchange, trade_date)`; an ISIN absent from L1 returns an empty tuple, not an error — a
    security with no price history yet is a gap, not a failure.
    """
    files = _l1_partition_files(data_root=data_root)
    if not files:
        return ()
    owns = con is None
    con = open_connection() if con is None else con
    try:
        # Scope to the EQ (regular-market) series: a name also carries block (BL), trade-to-trade
        # (BE/BZ) and special-settlement (T0) rows for the same (exchange, trade_date), and pulling
        # them all in would give the adjusted builder two closes for one date — a spurious
        # "duplicate price". The EQ series is the one price history L2 adjusts, matching the query
        # layer and the backtest reader (both filter series='EQ').
        rows = con.execute(
            "SELECT isin, exchange, trade_date, open, high, low, close, total_traded_qty "
            "FROM read_parquet($files) WHERE isin = $isin AND series = 'EQ' "
            "ORDER BY exchange, trade_date",
            {"files": [str(f) for f in files], "isin": isin},
        ).fetchall()
    finally:
        if owns:
            con.close()
    return tuple(
        RawBar(
            isin=str(r[0]),
            exchange=str(r[1]),
            trade_date=r[2],
            open=r[3],
            high=r[4],
            low=r[5],
            close=r[6],
            volume=int(r[7]),
        )
        for r in rows
    )


def build_adjusted_bars(
    isin: str,
    chain: FactorChain,
    actions: Iterable[CorporateAction],
    raw_bars: Sequence[RawBar],
) -> tuple[AdjustedBar, ...]:
    """Turn one ISIN's raw OHLCV into its back-adjusted L2 bars — pure, no I/O.

    What it does: for each venue's series, multiplies OHLC by `chain.price_factor_asof(date)` and
    volume by `chain.qty_factor_asof(date)` (the split/bonus back-adjustment), and computes the
    total-return close through `total_return_series`, which reinvests cash dividends on top of the
    same price events (§4.3, M2.4 acceptance 4). The math is entirely M2.4's; this only carries it.

    What it assumes: `chain` and `actions` are the *consistent* output of one M2.4 recompute for
    `isin` — `chain` is the persisted factor chain, `actions` its reconciled corporate actions — so
    the price events `total_return_series` derives from `actions` agree with `chain`. It assumes
    L1 covers the trading day before every cash-dividend ex-date; if it does not,
    `total_return_series` raises `FactorError` rather than silently dropping the distribution.

    What it never does: re-implement the factor arithmetic, invent a factor, or read I/O.
    """
    out: list[AdjustedBar] = []
    # One ISIN can trade on both venues; each venue's raw series is a distinct time line and adjusts
    # on its own (same chain, but the price points and dividend-reinvestment base differ), so the
    # per-date uniqueness `total_return_series` requires holds within a venue, not across.
    for exchange, group in groupby(
        sorted(raw_bars, key=lambda b: (b.exchange, b.trade_date)), key=lambda b: b.exchange
    ):
        bars = list(group)
        prices: list[PricePoint] = [_price_point(b) for b in bars]
        tr_by_date = {p.date: p.adj_close for p in total_return_series(actions, prices)}
        for bar in bars:
            cum_price = chain.price_factor_asof(bar.trade_date)
            cum_qty = chain.qty_factor_asof(bar.trade_date)
            out.append(
                AdjustedBar(
                    isin=isin,
                    exchange=exchange,
                    trade_date=bar.trade_date,
                    adj_open=bar.open * cum_price,
                    adj_high=bar.high * cum_price,
                    adj_low=bar.low * cum_price,
                    adj_close=bar.close * cum_price,
                    adj_volume=Decimal(bar.volume) * cum_qty,
                    tr_close=tr_by_date[bar.trade_date],
                    cum_price_factor=cum_price,
                    cum_qty_factor=cum_qty,
                )
            )
    # `groupby` ran over input sorted by `(exchange, trade_date)`, so `out` is already in that
    # total-key order — the deterministic order the writer relies on for byte-identical rebuilds.
    return tuple(out)


def materialize_isin(
    isin: str,
    *,
    chain: FactorChain,
    actions: Iterable[CorporateAction],
    con: duckdb.DuckDBPyConnection | None = None,
    data_root: Path | None = None,
) -> L2WriteReport:
    """Materialize one ISIN's L2 adjusted partition from L1 + its factor chain.

    The incremental unit (acceptance 3): reads the ISIN's raw history from L1 through DuckDB, builds
    its adjusted bars, and writes exactly one file — `L2/prices_adjusted/isin=<isin>/part.parquet` —
    touching no other ISIN. Fully recomputable (acceptance 2): the file is a deterministic function
    of L1 and the chain, written whole via a staging rename, so rebuilding it is byte-identical.

    An ISIN with no L1 history writes nothing and removes any stale partition, so its rebuilt state
    (absent) is still identical to a fresh build.
    """
    if chain.isin != isin:
        raise ValueError(
            f"factor chain is for {chain.isin!r} but materializing {isin!r}; a chain is per ISIN"
        )
    raw_bars = read_raw_bars_from_l1(isin, con=con, data_root=data_root)
    path = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=data_root)
    if not raw_bars:
        removed = _remove_partition(path)
        _LOG.info(
            "l2.prices_adjusted_empty",
            dataset=PRICES_ADJUSTED_DATASET,
            isin=isin,
            stale_removed=removed,
            state="EMPTY",
        )
        return L2WriteReport(isin=isin, path=None, rows_written=0, from_date=None, to_date=None)

    # `build_adjusted_bars` emits rows already ordered by the total key `(exchange, trade_date)`, so
    # the partition is byte-identical across rebuilds regardless of the order L1 was read in.
    bars = build_adjusted_bars(isin, chain, actions, raw_bars)
    table = _bars_to_table(bars)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_table(table, path)

    dates = [b.trade_date for b in bars]
    report = L2WriteReport(
        isin=isin,
        path=path,
        rows_written=len(bars),
        from_date=min(dates),
        to_date=max(dates),
    )
    _LOG.info(
        "l2.prices_adjusted_written",
        dataset=PRICES_ADJUSTED_DATASET,
        isin=isin,
        path=str(path),
        rows=report.rows_written,
        from_date=report.from_date.isoformat() if report.from_date else None,
        to_date=report.to_date.isoformat() if report.to_date else None,
        state="PUBLISHED",
    )
    return report


def materialize_isins(
    chains_and_actions: Iterable[tuple[FactorChain, Iterable[CorporateAction]]],
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    data_root: Path | None = None,
) -> tuple[L2WriteReport, ...]:
    """Materialize several ISINs, each from its own chain and actions, reusing one connection.

    Each ISIN is still an independent per-ISIN write (acceptance 3): materializing a subset leaves
    every other ISIN's partition untouched on disk. Opens one DuckDB connection for the batch unless
    the caller passes one.
    """
    owns = con is None
    con = open_connection() if con is None else con
    try:
        return tuple(
            materialize_isin(chain.isin, chain=chain, actions=actions, con=con, data_root=data_root)
            for chain, actions in chains_and_actions
        )
    finally:
        if owns:
            con.close()


def read_adjusted(
    isin: str, *, con: duckdb.DuckDBPyConnection | None = None, data_root: Path | None = None
) -> tuple[AdjustedBar, ...]:
    """Read one ISIN's materialized L2 partition back through DuckDB — the hot path.

    Raises `FileNotFoundError` when the partition was never materialized (a gap for the query layer
    to explain, not an empty series). Returns the bars ordered by `(exchange, trade_date)`.
    """
    path = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(
            f"no {PRICES_ADJUSTED_DATASET} partition for {isin}: {path} (materialize it first)"
        )
    owns = con is None
    con = open_connection() if con is None else con
    try:
        rows = con.execute(
            "SELECT isin, exchange, trade_date, adj_open, adj_high, adj_low, adj_close, "
            "adj_volume, tr_close, cum_price_factor, cum_qty_factor "
            "FROM read_parquet($file) ORDER BY exchange, trade_date",
            {"file": str(path)},
        ).fetchall()
    finally:
        if owns:
            con.close()
    return tuple(
        AdjustedBar(
            isin=str(r[0]),
            exchange=str(r[1]),
            trade_date=r[2],
            adj_open=r[3],
            adj_high=r[4],
            adj_low=r[5],
            adj_close=r[6],
            adj_volume=r[7],
            tr_close=r[8],
            cum_price_factor=r[9],
            cum_qty_factor=r[10],
        )
        for r in rows
    )


def register_raw_view(
    con: duckdb.DuckDBPyConnection,
    *,
    view: str = "prices_raw",
    data_root: Path | None = None,
) -> str:
    """Create a DuckDB view over all L1 `prices_raw` partitions on `con`, and return its name.

    The "DuckDB views over L1 Parquet" of §4.2: the query layer (M4.1) reads raw prices as one
    relation without knowing the partition layout. A no-op relation (empty) when L1 has no
    partitions yet, so registering the view never fails on a cold lake.
    """
    files = _l1_partition_files(data_root=data_root)
    con.execute(f"CREATE OR REPLACE VIEW {_ident(view)} AS {_parquet_relation(files)}")
    return view


def register_adjusted_view(
    con: duckdb.DuckDBPyConnection,
    *,
    view: str = "prices_adjusted",
    data_root: Path | None = None,
) -> str:
    """Create a DuckDB view over all materialized L2 `prices_adjusted` partitions, return its name.

    The materialized-L2 half of §4.2 ("materialized DuckDB … for hot paths"): the adjusted series of
    the whole universe as one relation, so a cross-sectional query does not open each ISIN file by
    hand. Empty when nothing has been materialized yet.
    """
    files = _l2_partition_files(data_root=data_root)
    con.execute(f"CREATE OR REPLACE VIEW {_ident(view)} AS {_parquet_relation(files)}")
    return view


def wipe_adjusted(*, data_root: Path | None = None) -> int:
    """Delete the entire L2 `prices_adjusted` dataset; return how many partition files were removed.

    L2 holds no primary data (invariant #3), so it is deletable at any time and rebuilds identically
    from L1 + factors (acceptance 2). Used by the "wipe and rebuild" recompute path and by an
    operator reclaiming space. Removing an absent dataset is a no-op (returns 0), never an error.
    """
    root = layer_root(Layer.L2, data_root=data_root) / PRICES_ADJUSTED_DATASET
    if not root.exists():
        return 0
    files = list(root.glob(f"isin=*/{DEFAULT_PART_FILENAME}"))
    removed = 0
    for file in files:
        file.unlink()
        removed += 1
    for partition_dir in root.glob("isin=*"):
        if partition_dir.is_dir() and not any(partition_dir.iterdir()):
            partition_dir.rmdir()
    if not any(root.iterdir()):
        root.rmdir()
    _LOG.info("l2.prices_adjusted_wiped", dataset=PRICES_ADJUSTED_DATASET, files_removed=removed)
    return removed


# ── the DB-driven recompute seam (drains the M2.4 invalidation queue) ────────────────────────────


def load_factor_chain(conn: Connection, isin: str) -> FactorChain:
    """Read one ISIN's persisted factor chain out of `adjustment_factors` (M2.4's output).

    The L2 materializer's "+ factors" input: the chain M2.4's recompute wrote, read back verbatim,
    so L2 is a function of L1 and the recorded factors. Rows come back ordered by ex-date, the order
    `FactorChain` assumes. An ISIN with no factor rows yields an empty chain — a valid one whose
    adjusted series equals its raw series.
    """
    rows = conn.execute(
        "SELECT ex_date, price_factor, qty_factor, cum_price_factor, cum_qty_factor, "
        "structural_break FROM adjustment_factors WHERE isin = %s ORDER BY ex_date",
        (isin,),
    ).fetchall()
    return FactorChain(
        isin=isin,
        rows=tuple(
            FactorRow(
                isin=isin,
                ex_date=r[0],
                price_factor=r[1],
                qty_factor=r[2],
                cum_price_factor=r[3],
                cum_qty_factor=r[4],
                structural_break=r[5],
            )
            for r in rows
        ),
    )


def rebuild_invalidated(
    conn: Connection,
    *,
    clock: Clock,
    con: duckdb.DuckDBPyConnection | None = None,
    data_root: Path | None = None,
) -> tuple[L2WriteReport, ...]:
    """Drain the `l2_invalidation` queue and rebuild exactly the flagged ISINs' L2 partitions.

    §4.3 rule 2's other half: M2.4's recompute rewrites `adjustment_factors` for an ISIN and records
    an open `l2_invalidation` row; this reads those rows, rebuilds each ISIN's L2 from L1 + its
    fresh factors, and marks the row resolved (stamped from the injected `Clock`, B10). Incremental
    per ISIN, never a full-market rebuild (acceptance 3): an ISIN with no open invalidation is not
    touched. Bad/stale L2 must never become a decision (invariant #10), so an invalidation is only
    resolved after its rebuild has actually written.

    What it assumes: the caller owns the transaction and commits it, as M2.4's recompute does — so a
    CA ingest, its recompute, and the L2 rebuild it triggers can share one commit boundary. Returns
    one report per rebuilt ISIN, in ISIN order.
    """
    isins = [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT isin FROM l2_invalidation WHERE NOT resolved ORDER BY isin"
        ).fetchall()
    ]
    if not isins:
        return ()
    now = clock.now()
    owns = con is None
    con = open_connection() if con is None else con
    reports: list[L2WriteReport] = []
    try:
        for isin in isins:
            chain = load_factor_chain(conn, isin)
            actions = load_reconciled_actions(conn, isin=isin)
            reports.append(
                materialize_isin(isin, chain=chain, actions=actions, con=con, data_root=data_root)
            )
            conn.execute(
                "UPDATE l2_invalidation SET resolved = true, resolved_at = %s "
                "WHERE isin = %s AND NOT resolved",
                (now, isin),
            )
    finally:
        if owns:
            con.close()
    _LOG.info(
        "l2.invalidations_drained",
        dataset=PRICES_ADJUSTED_DATASET,
        isins=len(reports),
        rows=sum(r.rows_written for r in reports),
        state="RESOLVED",
    )
    return tuple(reports)


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _price_point(bar: RawBar) -> PricePoint:
    """The `PricePoint` `total_return_series` consumes for one raw bar (close only)."""
    from dataplatform.corpactions.factors import PricePoint

    return PricePoint(date=bar.trade_date, close=bar.close)


def _l1_partition_files(*, data_root: Path | None) -> list[Path]:
    """Every `prices_raw` partition file, sorted — the L1 relation DuckDB reads."""
    root = layer_root(Layer.L1, data_root=data_root) / PRICES_RAW_DATASET
    if not root.exists():
        return []
    return sorted(root.glob(f"date=*/{DEFAULT_PART_FILENAME}"))


def _l2_partition_files(*, data_root: Path | None) -> list[Path]:
    """Every materialized `prices_adjusted` partition file, sorted."""
    root = layer_root(Layer.L2, data_root=data_root) / PRICES_ADJUSTED_DATASET
    if not root.exists():
        return []
    return sorted(root.glob(f"isin=*/{DEFAULT_PART_FILENAME}"))


def _parquet_relation(files: Sequence[Path]) -> str:
    """A DuckDB relation SQL over `files`, or an empty-but-typed relation when there are none.

    An empty lake must still register a view without error, so with no files the relation is a
    `SELECT … WHERE false` shaped like the schema rather than a `read_parquet` over nothing.
    """
    if not files:
        return "SELECT * FROM (SELECT NULL) WHERE false"
    listed = ", ".join(f"'{_sql_literal(str(f))}'" for f in files)
    return f"SELECT * FROM read_parquet([{listed}])"


def _bars_to_table(bars: Sequence[AdjustedBar]) -> pa.Table:
    """Build the schema-checked Arrow table for a non-empty, pre-sorted list of adjusted bars."""
    table = pa.Table.from_pylist(
        [
            {
                "isin": b.isin,
                "exchange": b.exchange,
                "trade_date": b.trade_date,
                "adj_open": _q(b.adj_open, _PRICE_Q),
                "adj_high": _q(b.adj_high, _PRICE_Q),
                "adj_low": _q(b.adj_low, _PRICE_Q),
                "adj_close": _q(b.adj_close, _PRICE_Q),
                "adj_volume": _q(b.adj_volume, _VOLUME_Q),
                "tr_close": _q(b.tr_close, _PRICE_Q),
                "cum_price_factor": _q(b.cum_price_factor, _FACTOR_Q),
                "cum_qty_factor": _q(b.cum_qty_factor, _FACTOR_Q),
            }
            for b in bars
        ],
        schema=PRICES_ADJUSTED_SCHEMA,
    )
    if not table.schema.equals(PRICES_ADJUSTED_SCHEMA, check_metadata=False):
        raise ValueError(  # pragma: no cover - the pylist is built against the schema above
            f"{PRICES_ADJUSTED_DATASET} table schema drifted from the declared schema"
        )
    return table


def _remove_partition(path: Path) -> bool:
    """Remove a stale L2 partition file and its now-empty directory; report whether one existed."""
    if not path.exists():
        return False
    path.unlink()
    if path.parent.is_dir() and not any(path.parent.iterdir()):
        path.parent.rmdir()
    return True


def _q(value: Decimal, quantum: Decimal) -> Decimal:
    """Quantise to a fixed scale with half-up rounding, so stored values are byte-deterministic."""
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _write_table(table: pa.Table, path: Path) -> None:
    """Write a parquet table whole via a staging file, so a partition is never half-written.

    Identical shape to L1's writer (`store.l1._write_table`): snappy, parquet 2.6, staging rename —
    so an L2 file re-derived from the same inputs is byte-for-byte the previous one (acceptance 2).
    """
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)


def _ident(name: str) -> str:
    """Quote a DuckDB identifier (a view name) so it cannot inject SQL."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _sql_literal(value: str) -> str:
    """Escape a single-quoted SQL string literal (a file path in a `read_parquet` list)."""
    return value.replace("'", "''")
