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

from collections.abc import Callable, Iterable, Mapping, Sequence
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
from dataplatform.corpactions.factors import (
    FactorChain,
    FactorError,
    FactorRow,
    total_return_series,
    with_events,
    with_price_events,
)
from dataplatform.corpactions.implied import ImpliedSplit, SessionBar, detect_implied_splits
from dataplatform.corpactions.manual_actions import default_manual_actions
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
    "L2FillReport",
    "L2RebuildReport",
    "L2TruncatedReport",
    "L2WriteReport",
    "RawBar",
    "build_adjusted_bars",
    "curated_actions",
    "implied_splits",
    "isins_with_eq_bars",
    "load_factor_chain",
    "materialize_isin",
    "materialize_isins",
    "materialize_missing",
    "materialized_isins",
    "open_connection",
    "preload_raw_bars",
    "prune_retired",
    "read_adjusted",
    "read_raw_bars_from_l1",
    "rebuild_all",
    "rebuild_invalidated",
    "rebuild_isins",
    "rebuild_truncated",
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
    `to_date` bound the series written; both are `None` for an empty ISIN. `implied_splits` are
    the share-basis changes read off L1 that no feed published (`corpactions.implied`), each
    carried into the partition's `cum_price_factor`; `curated` are the hand-transcribed, sourced
    actions (`corpactions.manual_actions`) composed in the same way.
    """

    isin: str
    path: Path | None
    rows_written: int
    from_date: date | None
    to_date: date | None
    implied_splits: tuple[ImpliedSplit, ...] = ()
    curated: tuple[CorporateAction, ...] = ()


@dataclass(frozen=True, slots=True)
class L2FillReport:
    """What one first-time fill (`materialize_missing`) did.

    `candidates` is every ISIN with EQ bars in L1 — the population L2 must cover. Each one was
    either already on disk, retired by a lineage edge (its bars live in the survivor's stitched
    partition), or written here; the three counts sum to `candidates`.
    """

    candidates: int
    already_materialized: int
    skipped_retired: int
    written: tuple[L2WriteReport, ...]

    @property
    def rows_written(self) -> int:
        return sum(r.rows_written for r in self.written)


@dataclass(frozen=True, slots=True)
class L2TruncatedReport:
    """What one `rebuild_truncated` pass found and (unless a dry run) rebuilt.

    `truncated` maps each ISIN whose partition starts after its L1 EQ history to
    `(L2 first date, L1 first date)` — the evidence, kept whether or not anything was written.
    `skipped_retired` counts the partitions on disk that belong to an ISIN a lineage edge retired:
    never judged, never rebuilt (their history is the survivor's). `written` is empty on a dry run.
    """

    partitions: int
    truncated: Mapping[str, tuple[date, date]]
    skipped_retired: int
    written: tuple[L2WriteReport, ...]

    @property
    def rows_written(self) -> int:
        return sum(r.rows_written for r in self.written)


@dataclass(frozen=True, slots=True)
class L2RebuildReport:
    """What one full `rebuild_all` pass did.

    `candidates` is every ISIN with EQ bars in L1, every lineage survivor and every partition that
    was on disk after the prune; each was either retired by a lineage edge
    (`skipped_retired`, its bars in the survivor's stitched partition) or rebuilt (`written`).
    `pruned_retired` are the retired ISINs whose stale partitions were removed from disk.
    """

    candidates: int
    skipped_retired: int
    written: tuple[L2WriteReport, ...]
    pruned_retired: tuple[str, ...]

    @property
    def rows_written(self) -> int:
        return sum(r.rows_written for r in self.written)

    @property
    def implied_splits(self) -> tuple[ImpliedSplit, ...]:
        return tuple(s for r in self.written for s in r.implied_splits)

    @property
    def curated(self) -> tuple[CorporateAction, ...]:
        return tuple(a for r in self.written for a in r.curated)


def open_connection() -> duckdb.DuckDBPyConnection:
    """A fresh in-memory DuckDB connection for reading the Parquet lake.

    DuckDB is the query engine over L1/L2 Parquet (§4.5); an in-memory connection holds no state
    between calls, so callers that read many ISINs may pass one connection to amortise startup, and
    callers that read one may let the materializer open its own.
    """
    return duckdb.connect(":memory:")


#: The per-connection preload `preload_raw_bars` fills and `read_raw_bars_from_l1` consults: the
#: EQ bars of a named set of ISINs, read out of L1 in one pass, and the set itself.
_PRELOAD_BARS_TABLE: Final = "l1_eq_bars_preload"
_PRELOAD_ISINS_TABLE: Final = "l1_eq_bars_preload_isins"

#: The columns a raw bar is read with, in the order `RawBar` is built from them — one spelling
#: shared by the per-ISIN scan and the preload, so the two can never drift apart.
_RAW_BAR_COLUMNS: Final = "isin, exchange, trade_date, open, high, low, close, total_traded_qty"


def preload_raw_bars(
    con: duckdb.DuckDBPyConnection, isins: Iterable[str], *, data_root: Path | None = None
) -> int:
    """Read the listed ISINs' EQ bars out of L1 in one pass and keep them on `con` for later reads.

    Why: `read_raw_bars_from_l1` scans every `prices_raw` date partition to find one ISIN. L1 is
    partitioned by date, not ISIN, so a rebuild over N ISINs opened every partition N times —
    measured 2026-09-07 on the server: ~3,450 invalidated ISINs over 2,475 partitions, ~0.45 s each,
    half an hour to extract 3.5 M rows that a single pass reads in seconds. The same quadratic
    shape the PIT writer had (M10.4), and the same fix: batch the read, not the unit of work.

    What it guarantees: the bytes do not change. The preload holds the same columns with their
    parquet types intact, and a preloaded read applies the same `series = 'EQ'` filter and the same
    `(exchange, trade_date)` order as the scan, so the `RawBar`s are equal and the L2 partition
    built from them is identical. An ISIN outside the preload falls back to the scan, so a caller
    that preloads a subset is still correct — only slower for the rest.

    Replaces any earlier preload on `con`; returns the number of bars now held. Both tables are
    temporary to the connection and vanish with it.
    """
    wanted = sorted(set(isins))
    con.execute(f"CREATE OR REPLACE TEMP TABLE {_PRELOAD_ISINS_TABLE} (isin VARCHAR)")
    if wanted:
        con.executemany(f"INSERT INTO {_PRELOAD_ISINS_TABLE} VALUES (?)", [(i,) for i in wanted])
    files = _l1_partition_files(data_root=data_root)
    if files:
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE {_PRELOAD_BARS_TABLE} AS "
            f"SELECT {_RAW_BAR_COLUMNS} FROM read_parquet($files) WHERE series = 'EQ' "
            f"AND isin IN (SELECT isin FROM {_PRELOAD_ISINS_TABLE})",
            {"files": [str(f) for f in files]},
        )
    else:
        # A cold lake: covered ISINs must read as empty, exactly as the scan would report them.
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE {_PRELOAD_BARS_TABLE} (isin VARCHAR, exchange VARCHAR, "
            "trade_date DATE, open DECIMAL(18, 4), high DECIMAL(18, 4), low DECIMAL(18, 4), "
            "close DECIMAL(18, 4), total_traded_qty BIGINT)"
        )
    row = con.execute(f"SELECT count(*) FROM {_PRELOAD_BARS_TABLE}").fetchone()
    count = 0 if row is None else int(row[0])
    _LOG.info(
        "l2.raw_bars_preloaded",
        dataset=PRICES_ADJUSTED_DATASET,
        isins=len(wanted),
        rows=count,
        partitions=len(files),
    )
    return count


def _preload_covers(con: duckdb.DuckDBPyConnection, isin: str) -> bool:
    """Whether `con` carries a preload (`preload_raw_bars`) that includes `isin`."""
    present = con.execute(
        "SELECT 1 FROM duckdb_tables() WHERE table_name = $name AND temporary",
        {"name": _PRELOAD_ISINS_TABLE},
    ).fetchone()
    if present is None:
        return False
    covered = con.execute(
        f"SELECT 1 FROM {_PRELOAD_ISINS_TABLE} WHERE isin = $isin", {"isin": isin}
    ).fetchone()
    return covered is not None


def read_raw_bars_from_l1(
    isin: str, *, con: duckdb.DuckDBPyConnection | None = None, data_root: Path | None = None
) -> tuple[RawBar, ...]:
    """Read one ISIN's full raw OHLCV history out of L1 through DuckDB — a view over L1 Parquet.

    Scans every `prices_raw` date partition and filters to `isin` (invariant #2 — the join key is
    the ISIN the row carries, never a symbol), which is the columnar scan DuckDB exists for
    (§4.5(a), "adjusted OHLCV series per ISIN across years"). Returns the bars ordered by
    `(exchange, trade_date)`; an ISIN absent from L1 returns an empty tuple, not an error — a
    security with no price history yet is a gap, not a failure.

    When `con` carries a preload that covers `isin` (`preload_raw_bars`), the bars come from it
    instead and no partition is opened; the rows, their types and their order are the same.
    """
    owns = con is None
    con = open_connection() if con is None else con
    try:
        if not owns and _preload_covers(con, isin):
            rows = con.execute(
                f"SELECT {_RAW_BAR_COLUMNS} FROM {_PRELOAD_BARS_TABLE} WHERE isin = $isin "
                "ORDER BY exchange, trade_date",
                {"isin": isin},
            ).fetchall()
        else:
            files = _l1_partition_files(data_root=data_root)
            if not files:
                return ()
            # Scope to the EQ (regular-market) series: a name also carries block (BL),
            # trade-to-trade (BE/BZ) and special-settlement (T0) rows for the same (exchange,
            # trade_date), and pulling them all in would give the adjusted builder two closes for
            # one date — a spurious "duplicate price". The EQ series is the one price history L2
            # adjusts, matching the query layer and the backtest reader (both filter series='EQ').
            rows = con.execute(
                f"SELECT {_RAW_BAR_COLUMNS} FROM read_parquet($files) "
                "WHERE isin = $isin AND series = 'EQ' ORDER BY exchange, trade_date",
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
        # The price-adjusted series (splits/bonuses) uses only chain.*_factor_asof and never a
        # dividend, so it must be produced unconditionally. The total-return leg needs cash-dividend
        # amounts and can legitimately fail — a dividend stated as a percent of face value (no rupee
        # amount to reinvest) or one with no prior close. When it does, fall back tr_close to the
        # price-adjusted close (dividends simply not reinvested) and log it, rather than let a
        # dividend gap block a name's split adjustment. Decouples price-adjust from total-return.
        try:
            tr_by_date = {p.date: p.adj_close for p in total_return_series(actions, prices)}
        except FactorError as exc:
            _LOG.warning(
                "l2.total_return_unavailable",
                isin=isin,
                exchange=exchange,
                reason=str(exc)[:140],
                fallback="tr_close = adj_close (dividends not reinvested)",
            )
            tr_by_date = {}
        for bar in bars:
            cum_price = chain.price_factor_asof(bar.trade_date)
            cum_qty = chain.qty_factor_asof(bar.trade_date)
            adj_close = bar.close * cum_price
            out.append(
                AdjustedBar(
                    isin=isin,
                    exchange=exchange,
                    trade_date=bar.trade_date,
                    adj_open=bar.open * cum_price,
                    adj_high=bar.high * cum_price,
                    adj_low=bar.low * cum_price,
                    adj_close=adj_close,
                    adj_volume=Decimal(bar.volume) * cum_qty,
                    tr_close=tr_by_date.get(bar.trade_date, adj_close),
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
    history_isins: Sequence[str] | None = None,
    infer_splits: bool = True,
    curated: Sequence[CorporateAction] | None = None,
) -> L2WriteReport:
    """Materialize one ISIN's L2 adjusted partition from L1 + its factor chain.

    The incremental unit (acceptance 3): reads the ISIN's raw history from L1 through DuckDB, builds
    its adjusted bars, and writes exactly one file — `L2/prices_adjusted/isin=<isin>/part.parquet` —
    touching no other ISIN. Fully recomputable (acceptance 2): the file is a deterministic function
    of L1 and the chain, written whole via a staging rename, so rebuilding it is byte-identical.

    An ISIN with no L1 history writes nothing and removes any stale partition, so its rebuilt state
    (absent) is still identical to a fresh build.

    `history_isins` is the ISIN's lineage chain oldest-first (`LineageResolver.chain_to`), for a
    security whose earlier history sits under ISINs a reissue retired. Omit it and only `isin`'s
    own bars are read, which is right for the ~90% of names that were never reissued.

    `infer_splits` composes into the chain the share-basis changes L1 shows and no feed published
    (`implied_splits`: ETF unit splits, pre-2016 equity splits neither feed's history reaches), so
    the partition carries no unadjusted step for them. Each is logged with its evidence and listed
    on the report; the partition's `cum_price_factor` is the factor actually applied, so
    `adj = raw x cum_price_factor` holds row by row either way.

    `curated` are the sourced actions no feed carries (`corpactions.manual_actions`); `None` reads
    the repo's curated file for `isin` *and every ISIN in `history_isins`*, each re-keyed to
    `isin` (`_curated_for_chain`). They are composed in before the implied-split scan — a curated
    split is a recorded event to it, so the same step is never adjusted twice — and one a feed has
    since published (same ex-date and type) is skipped in the feed's favour (`curated_actions`).
    """
    if chain.isin != isin:
        raise ValueError(
            f"factor chain is for {chain.isin!r} but materializing {isin!r}; a chain is per ISIN"
        )
    # An ISIN that was reissued holds only the history since the reissue; the rest is under the
    # ISINs it retired (D2 lineage). Reading the whole chain is what lets a look-back cross the
    # boundary: IRCTC's L1 under INE335Y01020 starts 2021-10-29, and a twelve-month momentum
    # signal read from that alone is measuring a three-day-old security. The bars are keyed to
    # `isin` on the way out, so the partition is the survivor's however many ISINs fed it — and
    # the spans do not overlap, so the per-(exchange, date) uniqueness the adjuster needs holds.
    sources = (isin,) if history_isins is None else tuple(history_isins)
    raw_bars = tuple(
        bar
        for source in sources
        for bar in read_raw_bars_from_l1(source, con=con, data_root=data_root)
    )
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

    actions = tuple(actions)
    manual = curated_actions(
        isin,
        actions,
        _curated_for_chain(isin, sources) if curated is None else tuple(curated),
    )
    if manual:
        chain = with_events(chain, manual)
        actions = actions + manual
    implied = implied_splits(isin, chain, actions, raw_bars) if infer_splits else ()
    if implied:
        extra = tuple(s.as_action() for s in implied)
        chain = with_price_events(chain, extra)
        actions = actions + extra
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
        implied_splits=implied,
        curated=manual,
    )
    _LOG.info(
        "l2.prices_adjusted_written",
        dataset=PRICES_ADJUSTED_DATASET,
        isin=isin,
        path=str(path),
        rows=report.rows_written,
        from_date=report.from_date.isoformat() if report.from_date else None,
        to_date=report.to_date.isoformat() if report.to_date else None,
        implied_splits=len(implied),
        curated_actions=len(manual),
        state="PUBLISHED",
    )
    return report


def _curated_for_chain(isin: str, sources: Sequence[str]) -> tuple[CorporateAction, ...]:
    """The repo's curated actions for every ISIN whose bars feed `isin`'s partition, keyed to it.

    A curated row is keyed to the ISIN whose partition carried its ex-date when it was written.
    Once a lineage edge retires that ISIN its bars move into the survivor's stitched partition and
    its own partition is pruned, so a lookup by the survivor alone would drop the row and leave its
    step unadjusted (UTISXN50: curated on INF789F1AHR6, 2021-02-17, whose 8.33x print the implied
    scan cannot match). Each row from a retired ISIN is re-keyed to `isin` with
    `filed_against_isin` naming where it was filed, the shape a feed action resolved through the
    lineage has; a row already keyed to `isin` is unchanged, so a partition with no history is
    built exactly as before.
    """
    found: list[CorporateAction] = []
    for source in dict.fromkeys((*sources, isin)):
        for row in default_manual_actions().actions_for(source):
            action = row.as_action()
            if source != isin:
                action = action.model_copy(
                    update={
                        "isin": isin,
                        "filed_against_isin": action.filed_against_isin or source,
                    }
                )
            found.append(action)
    return tuple(sorted(found, key=lambda a: (a.ex_date, a.action_type.value)))


def curated_actions(
    isin: str,
    recorded: Iterable[CorporateAction],
    curated: Sequence[CorporateAction],
) -> tuple[CorporateAction, ...]:
    """The curated actions for `isin` that the recorded ones do not already state.

    What it does: drops any curated action whose `(ex_date, action_type)` a recorded (feed) action
    shares — once a feed publishes the event, the feed's row is the one that counts, and composing
    both would apply it twice — logging each one it drops, and each one it keeps.

    What it never does: read a file or a database (the caller passes the curated rows), or drop a
    recorded action.
    """
    have = {(a.ex_date, a.action_type) for a in recorded}
    kept: list[CorporateAction] = []
    for action in curated:
        if action.isin != isin:
            raise ValueError(f"curated action for {action.isin} passed for {isin}")
        if (action.ex_date, action.action_type) in have:
            _LOG.warning(
                "l2.curated_action_superseded",
                isin=isin,
                ex_date=action.ex_date.isoformat(),
                action_type=action.action_type.value,
                state="SKIPPED",
            )
            continue
        _LOG.info(
            "l2.curated_action",
            isin=isin,
            ex_date=action.ex_date.isoformat(),
            action_type=action.action_type.value,
            l0_key=action.l0_key,
        )
        kept.append(action)
    return tuple(kept)


def implied_splits(
    isin: str,
    chain: FactorChain,
    actions: Iterable[CorporateAction],
    raw_bars: Sequence[RawBar],
) -> tuple[ImpliedSplit, ...]:
    """The share-basis changes in `raw_bars` that `chain` and `actions` leave unexplained.

    What it does: puts each venue's bars into the recorded chain's terms (so a recorded split is
    no step) and runs `corpactions.implied.detect_implied_splits` over them. An event two venues
    both show is kept once; two venues that disagree on the multiple for one date cancel it — a
    venue disagreement is a question for a human, not a factor. Logs each event it keeps.

    What it never does: write anything, or read anything but its arguments.
    """
    recorded = tuple(actions)
    found: dict[date, list[ImpliedSplit]] = {}
    for exchange, group in groupby(
        sorted(raw_bars, key=lambda b: (b.exchange, b.trade_date)), key=lambda b: b.exchange
    ):
        sessions = [
            SessionBar(
                trade_date=b.trade_date,
                open=b.open * (f := chain.price_factor_asof(b.trade_date)),
                close=b.close * f,
                volume=Decimal(b.volume) * chain.qty_factor_asof(b.trade_date),
                raw_close=b.close,
            )
            for b in group
        ]
        for event in detect_implied_splits(isin, sessions, recorded):
            found.setdefault(event.ex_date, []).append(event)
            _LOG.info(
                "l2.implied_split",
                isin=isin,
                exchange=exchange,
                ex_date=event.ex_date.isoformat(),
                from_value=str(event.from_value),
                to_value=str(event.to_value),
                close_ratio=str(round(event.close_ratio, 4)),
                open_ratio=str(round(event.open_ratio, 4)),
                volume_ratio=None
                if event.volume_ratio is None
                else str(round(event.volume_ratio, 2)),
            )
    kept: list[ImpliedSplit] = []
    for ex_date, events in sorted(found.items()):
        if len({e.price_factor for e in events}) > 1:
            _LOG.warning(
                "l2.implied_split_venue_disagreement",
                isin=isin,
                ex_date=ex_date.isoformat(),
                factors=sorted(str(e.price_factor) for e in events),
                state="SKIPPED",
            )
            continue
        kept.append(events[0])
    return tuple(kept)


def materialize_isins(
    chains_and_actions: Iterable[tuple[FactorChain, Iterable[CorporateAction]]],
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    data_root: Path | None = None,
    history_for: Mapping[str, Sequence[str]] | None = None,
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
            materialize_isin(
                chain.isin,
                chain=chain,
                actions=actions,
                con=con,
                data_root=data_root,
                history_isins=None if history_for is None else history_for.get(chain.isin),
            )
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
    history_for: Mapping[str, Sequence[str]] | None = None,
    survivor_of: Callable[[str], str] | None = None,
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

    `history_for` maps an ISIN to its D2 lineage chain, so a security whose earlier history sits
    under ISINs a reissue retired is rebuilt over the whole chain rather than the stub since the
    reissue. Omit it and every ISIN is rebuilt from its own bars alone, as before.

    `survivor_of` (the D2 lineage) marks the ISINs a reissue retired. A flagged retired ISIN is
    resolved without being built — its bars are the survivor's — and any partition it still has is
    removed: building it would put one company in L2 twice, the second copy unadjusted (on
    2026-10-05, 60 such partitions, BAJFINANCE's INE296A01016 a -90% "day" on its split).
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
        # One pass over L1 for every ISIN this drain touches — and the retired ISINs whose history
        # they inherit — instead of a whole-lake scan per ISIN (`preload_raw_bars`).
        wanted = set(isins)
        if history_for is not None:
            for isin in isins:
                wanted.update(history_for.get(isin, ()))
        preload_raw_bars(con, wanted, data_root=data_root)
        for isin in isins:
            if survivor_of is not None and survivor_of(isin) != isin:
                removed = _remove_partition(
                    l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=data_root)
                )
                _LOG.info(
                    "l2.retired_invalidation_resolved",
                    dataset=PRICES_ADJUSTED_DATASET,
                    isin=isin,
                    survivor=survivor_of(isin),
                    stale_removed=removed,
                    state="SKIPPED",
                )
                conn.execute(
                    "UPDATE l2_invalidation SET resolved = true, resolved_at = %s "
                    "WHERE isin = %s AND NOT resolved",
                    (now, isin),
                )
                continue
            chain = load_factor_chain(conn, isin)
            actions = load_reconciled_actions(conn, isin=isin)
            reports.append(
                materialize_isin(
                    isin,
                    chain=chain,
                    actions=actions,
                    con=con,
                    data_root=data_root,
                    history_isins=None if history_for is None else history_for.get(isin),
                )
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


# ── the first-time fill: every ISIN L1 has bars for and nothing ever built ────────────────────


def isins_with_eq_bars(
    con: duckdb.DuckDBPyConnection, *, data_root: Path | None = None
) -> tuple[str, ...]:
    """Every ISIN with at least one EQ bar in L1, sorted — the population L2 must cover.

    One columnar pass over `prices_raw` reading two columns, scoped by the same `series = 'EQ'`
    filter the materializer applies: an ISIN listed here has bars L2 would adjust, one absent here
    (a BSE-only name, a name that only ever traded in BE) would materialize to nothing.
    """
    files = _l1_partition_files(data_root=data_root)
    if not files:
        return ()
    rows = con.execute(
        "SELECT DISTINCT isin FROM read_parquet($files) WHERE series = 'EQ' ORDER BY isin",
        {"files": [str(f) for f in files]},
    ).fetchall()
    return tuple(str(r[0]) for r in rows)


def materialized_isins(*, data_root: Path | None = None) -> frozenset[str]:
    """The ISINs that have a `prices_adjusted` partition on disk right now."""
    return frozenset(_isin_of_partition(path) for path in _l2_partition_files(data_root=data_root))


def materialize_missing(
    conn: Connection,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    data_root: Path | None = None,
    history_for: Mapping[str, Sequence[str]] | None = None,
    survivor_of: Callable[[str], str] | None = None,
) -> L2FillReport:
    """Materialize every ISIN that has EQ bars in L1 and no L2 partition yet — a first-time fill.

    Why this exists: `rebuild_invalidated` builds exactly the ISINs a corporate-action recompute
    flagged, and nothing else ever built one. So a name that never split, paid or was reissued
    never got a partition, however long it traded. Measured on the server 2026-09-07: 793 of the
    2,716 NSE EQ names trading that month had no L2 partition — ADANIGREEN, ADANIENSOL and
    ETERNAL among them — and were invisible to every reader of L2 (the query layer, the backtest).

    What it does: takes the ISINs with EQ bars in L1; drops those already on disk and those a
    lineage edge retired (`survivor_of(isin) != isin` — their bars belong to the survivor's
    stitched partition, which is built here if it is the one missing); preloads the rest's bars in
    one pass; and materializes each from L1 + its persisted factor chain. For the common case that
    chain is empty and adjusted equals raw. Each write is still one ISIN's partition
    (acceptance 3), and nothing already on disk is touched — rebuilding is the queue's job.

    What it assumes: `survivor_of` and `history_for` come from the D2 lineage
    (`LineageResolver.survivor_of`, `chain_to`); without them every ISIN is built from its own
    bars, which is right for a lake with no reissues.

    What it never does: invent a factor from a recorded action. An ISIN whose corporate actions
    never reached the factor chain (single-source, unquantified, unresolved identity) is
    materialized without them, exactly as the queue would have left it — only a share-basis change
    L1 itself evidences (`implied_splits`, the same in every build path) is composed in.
    """
    owns = con is None
    con = open_connection() if con is None else con
    try:
        candidates = isins_with_eq_bars(con, data_root=data_root)
        present = materialized_isins(data_root=data_root)
        missing: list[str] = []
        retired = 0
        for isin in candidates:
            if isin in present:
                continue
            if survivor_of is not None and survivor_of(isin) != isin:
                retired += 1
                continue
            missing.append(isin)
        reports: list[L2WriteReport] = []
        if missing:
            wanted = set(missing)
            if history_for is not None:
                for isin in missing:
                    wanted.update(history_for.get(isin, ()))
            preload_raw_bars(con, wanted, data_root=data_root)
            for isin in missing:
                reports.append(
                    materialize_isin(
                        isin,
                        chain=load_factor_chain(conn, isin),
                        actions=load_reconciled_actions(conn, isin=isin),
                        con=con,
                        data_root=data_root,
                        history_isins=None if history_for is None else history_for.get(isin),
                    )
                )
    finally:
        if owns:
            con.close()
    report = L2FillReport(
        candidates=len(candidates),
        already_materialized=len(candidates) - len(missing) - retired,
        skipped_retired=retired,
        written=tuple(reports),
    )
    _LOG.info(
        "l2.missing_materialized",
        dataset=PRICES_ADJUSTED_DATASET,
        candidates=report.candidates,
        already_materialized=report.already_materialized,
        skipped_retired=report.skipped_retired,
        written=len(report.written),
        rows=report.rows_written,
        state="PUBLISHED",
    )
    return report


def rebuild_truncated(
    conn: Connection,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    data_root: Path | None = None,
    history_for: Mapping[str, Sequence[str]] | None = None,
    survivor_of: Callable[[str], str] | None = None,
    dry_run: bool = False,
) -> L2TruncatedReport:
    """Rebuild every L2 partition whose adjusted series starts later than the L1 history under it.

    Why this exists: L1 grew *backwards* (W1 put 2011-06-22 → 2016-09-01 prices into `prices_raw`)
    and neither door that writes L2 notices. `rebuild_invalidated` builds what a corporate-action
    recompute flagged, and `materialize_missing` builds only ISINs with no partition — so every
    existing partition kept starting at 2016-09-02 over an L1 that reaches five years further.

    What it does: one columnar pass each over L1 (first EQ date per ISIN) and L2 (first date per
    partition); a partition is truncated when the earliest EQ bar of any ISIN in its lineage chain
    (`history_for`, oldest-first; the ISIN alone otherwise) predates the partition's first row.
    Each truncated ISIN is rebuilt through `materialize_isin` from L1 + its persisted factor chain
    — the same unit, the same bytes a fresh build would write. Idempotent: after one pass nothing
    is truncated, so a second pass writes nothing. A partition whose ISIN a lineage edge retired
    (`survivor_of(isin) != isin`) is skipped and counted, as `materialize_missing` skips it: its
    chain is only itself, so extending it would duplicate the pre-reissue history the survivor's
    stitched partition already carries — one company twice to any direct L2 reader.
    What it assumes: the factor chain is already what it should be (`adjustment_factors` holds
    ex-dates back to 2000 from the ISIN-keyed BSE actions); this reads it and never extends it.
    What it never does: create a partition that does not exist (that is `materialize_missing`),
    touch a partition that is not truncated or is retired, or invent a factor for an unreconciled
    action. Nor does it delete a retired partition already on disk — it only stops growing one.
    """
    owns = con is None
    con = open_connection() if con is None else con
    try:
        l2_files = _l2_partition_files(data_root=data_root)
        l1_files = _l1_partition_files(data_root=data_root)
        truncated: dict[str, tuple[date, date]] = {}
        retired = 0
        if l2_files and l1_files:
            l2_first = dict(
                con.execute(
                    "SELECT isin, min(trade_date) FROM read_parquet($files) GROUP BY isin",
                    {"files": [str(f) for f in l2_files]},
                ).fetchall()
            )
            l1_first = dict(
                con.execute(
                    "SELECT isin, min(trade_date) FROM read_parquet($files) "
                    "WHERE series = 'EQ' GROUP BY isin",
                    {"files": [str(f) for f in l1_files]},
                ).fetchall()
            )
            for isin, starts in sorted(l2_first.items()):
                if survivor_of is not None and survivor_of(isin) != isin:
                    retired += 1
                    continue
                chain = (isin,) if history_for is None else history_for.get(isin, (isin,))
                earliest = [l1_first[i] for i in chain if i in l1_first]
                if earliest and min(earliest) < starts:
                    truncated[str(isin)] = (starts, min(earliest))
        reports: list[L2WriteReport] = []
        if truncated and not dry_run:
            wanted = set(truncated)
            if history_for is not None:
                for isin in truncated:
                    wanted.update(history_for.get(isin, ()))
            preload_raw_bars(con, wanted, data_root=data_root)
            for isin in truncated:
                reports.append(
                    materialize_isin(
                        isin,
                        chain=load_factor_chain(conn, isin),
                        actions=load_reconciled_actions(conn, isin=isin),
                        con=con,
                        data_root=data_root,
                        history_isins=None if history_for is None else history_for.get(isin),
                    )
                )
    finally:
        if owns:
            con.close()
    report = L2TruncatedReport(
        partitions=len(l2_files),
        truncated=truncated,
        skipped_retired=retired,
        written=tuple(reports),
    )
    _LOG.info(
        "l2.truncated_rebuilt",
        dataset=PRICES_ADJUSTED_DATASET,
        partitions=report.partitions,
        truncated=len(report.truncated),
        skipped_retired=report.skipped_retired,
        written=len(report.written),
        rows=report.rows_written,
        dry_run=dry_run,
        state="PLANNED" if dry_run else "PUBLISHED",
    )
    return report


def prune_retired(
    survivor_of: Callable[[str], str], *, data_root: Path | None = None
) -> tuple[str, ...]:
    """Remove every L2 partition whose ISIN a lineage edge retired; return those ISINs, sorted.

    Why this exists: a retired ISIN's bars belong to its survivor's stitched partition, and both
    first-time fill and `--extend` already skip it — but neither removed one already on disk. The
    ones there were built from the ISIN's own bars before the lineage named it retired, so they
    duplicate the survivor's history and, worse, carry the reissue split unadjusted: the exchange
    prints the ex-date session under the old ISIN and moves to the new one the next day, so the
    old ISIN's last bar is in post-split terms with no factor behind it (BAJFINANCE INE296A01016,
    2016-09-07 11,393.30 → 2016-09-08 1,162.80). Measured on the server lake 2026-10-05: 60.

    L2 is derived (invariant #3), so removal is always safe and the survivor's partition is
    untouched; L0 and L1 are never read or written here. Idempotent: a second call removes nothing.
    """
    pruned: list[str] = []
    for path in _l2_partition_files(data_root=data_root):
        isin = _isin_of_partition(path)
        if survivor_of(isin) == isin:
            continue
        _remove_partition(path)
        pruned.append(isin)
        _LOG.info(
            "l2.retired_partition_pruned",
            dataset=PRICES_ADJUSTED_DATASET,
            isin=isin,
            survivor=survivor_of(isin),
            state="REMOVED",
        )
    _LOG.info("l2.retired_pruned", dataset=PRICES_ADJUSTED_DATASET, pruned=len(pruned))
    return tuple(sorted(pruned))


def rebuild_all(
    conn: Connection,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    data_root: Path | None = None,
    history_for: Mapping[str, Sequence[str]] | None = None,
    survivor_of: Callable[[str], str] | None = None,
    batch_size: int = 500,
) -> L2RebuildReport:
    """Rebuild every L2 partition from L1 + its factor chain, and prune the retired ones.

    Why this exists: the other doors each rewrite a subset — the queue what a recompute flagged,
    the fill what is absent, `--extend` what starts late — so a change in how a partition is
    *built* (here: price-implied splits) reaches none of the partitions already on disk. This is
    the one pass that brings all of them to what a fresh build would write.

    What it does: prunes retired partitions (`prune_retired`), then for every non-retired ISIN with
    EQ bars in L1, every lineage survivor and every partition on disk, rebuilds its partition
    through `materialize_isin` over its lineage chain, preloading L1 `batch_size` ISINs at a time
    so memory stays bounded. Byte-identical to a wipe followed by a fresh build, and idempotent.

    What it never does: write L0, L1 or Postgres — it reads `adjustment_factors` and
    `corporate_actions` and writes only L2.
    """
    owns = con is None
    con = open_connection() if con is None else con
    try:
        pruned = () if survivor_of is None else prune_retired(survivor_of, data_root=data_root)
        # Every ISIN a fresh build could write: its own EQ bars, a survivor whose EQ history is
        # only its chain's (INE0OPA01027 trades BE alone; its EQ years are its predecessor's), and
        # anything already on disk — which `materialize_isin` removes when nothing feeds it.
        candidates = tuple(
            sorted(
                set(isins_with_eq_bars(con, data_root=data_root))
                | set(history_for or {})
                | materialized_isins(data_root=data_root)
            )
        )
        live = [i for i in candidates if survivor_of is None or survivor_of(i) == i]
        reports: list[L2WriteReport] = []
        for start in range(0, len(live), batch_size):
            batch = live[start : start + batch_size]
            wanted = set(batch)
            if history_for is not None:
                for isin in batch:
                    wanted.update(history_for.get(isin, ()))
            preload_raw_bars(con, wanted, data_root=data_root)
            for isin in batch:
                reports.append(
                    materialize_isin(
                        isin,
                        chain=load_factor_chain(conn, isin),
                        actions=load_reconciled_actions(conn, isin=isin),
                        con=con,
                        data_root=data_root,
                        history_isins=None if history_for is None else history_for.get(isin),
                    )
                )
    finally:
        if owns:
            con.close()
    report = L2RebuildReport(
        candidates=len(candidates),
        skipped_retired=len(candidates) - len(live),
        written=tuple(reports),
        pruned_retired=pruned,
    )
    _LOG.info(
        "l2.rebuilt_all",
        dataset=PRICES_ADJUSTED_DATASET,
        candidates=report.candidates,
        skipped_retired=report.skipped_retired,
        written=len(report.written),
        rows=report.rows_written,
        implied_splits=len(report.implied_splits),
        curated_actions=len(report.curated),
        pruned_retired=len(report.pruned_retired),
        state="PUBLISHED",
    )
    return report


def rebuild_isins(
    conn: Connection,
    isins: Sequence[str],
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    data_root: Path | None = None,
    history_for: Mapping[str, Sequence[str]] | None = None,
    survivor_of: Callable[[str], str] | None = None,
) -> tuple[L2WriteReport, ...]:
    """Rebuild exactly the named ISINs' partitions, as `rebuild_all` would write them.

    For a change that reaches a known handful of ISINs — a curated action added to
    `corpactions.manual_actions` — without a twelve-minute pass over every partition. Each ISIN is
    rebuilt through `materialize_isin` over its lineage chain, byte-identical to what
    `rebuild_all` writes for it; no other partition is touched.

    What it never does: build a lineage-retired ISIN (its bars are its survivor's — name the
    survivor instead; it raises `ValueError` rather than guess), or write L0, L1 or Postgres.
    """
    wanted = tuple(dict.fromkeys(isins))
    if survivor_of is not None:
        retired = [i for i in wanted if survivor_of(i) != i]
        if retired:
            raise ValueError(
                "lineage-retired ISINs have no partition of their own; rebuild their survivors: "
                + ", ".join(f"{i} -> {survivor_of(i)}" for i in retired)
            )
    owns = con is None
    con = open_connection() if con is None else con
    try:
        load = set(wanted)
        if history_for is not None:
            for isin in wanted:
                load.update(history_for.get(isin, ()))
        preload_raw_bars(con, load, data_root=data_root)
        reports = tuple(
            materialize_isin(
                isin,
                chain=load_factor_chain(conn, isin),
                actions=load_reconciled_actions(conn, isin=isin),
                con=con,
                data_root=data_root,
                history_isins=None if history_for is None else history_for.get(isin),
            )
            for isin in wanted
        )
    finally:
        if owns:
            con.close()
    _LOG.info(
        "l2.rebuilt_isins",
        dataset=PRICES_ADJUSTED_DATASET,
        isins=len(reports),
        rows=sum(r.rows_written for r in reports),
        curated_actions=sum(len(r.curated) for r in reports),
        state="PUBLISHED",
    )
    return reports


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _isin_of_partition(path: Path) -> str:
    """The ISIN an L2 partition file belongs to, read back off its `isin=<ISIN>` directory."""
    return path.parent.name.removeprefix("isin=")


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
