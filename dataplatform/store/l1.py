"""L1 canonical writer for `prices_raw` (M1.8, module D4).

This is the join point where a session's parsed bhavcopy (M1.4/M1.5) and its delivery file (M1.6)
become one ISIN-keyed, exchange-tagged parquet partition — the raw price truth the rest of the
platform reads. It writes only what the exchange published: raw traded prices, verbatim, and the
delivery figures joined onto them (invariant #3, enforced by `schemas.assert_raw_only`). Adjusted
series are never stored here; D3 factors applied on read or materialised into L2 own that.

Three properties this module exists to guarantee, each mapped to an acceptance criterion:

* **Idempotent per `(dataset, date)`.** `rebuild_prices_raw_from_l0` reads the L0 payloads back
  (re-checksummed by `L0Store.get`), parses, resolves and writes — and doing it twice produces a
  byte-identical partition. Determinism comes from three choices: rows are sorted by a total key
  before writing, every money value is quantised to a fixed decimal scale, and the file is written
  whole via a staging rename. So "every L1 value is re-derivable from L0" (invariant #1) is not a
  claim but a test that rewrites and diffs the bytes. One partition holds *both* exchanges' rows
  for the date (§4.1 row 4; M3.1's "BSE lands under the same schema as NSE"), so a write is scoped
  to the exchange it names: it replaces that exchange's rows and carries the other exchange's
  through untouched. Whichever exchange is written first, the bytes come out the same, because the
  sort key leads with `exchange`.

* **The delivery join goes through the identity master, never through symbols.** The delivery file
  has no ISIN and NSE symbols are recycled, so a delivery row is resolved to its ISIN via
  `IdentityMaster.resolve(symbol, trade_date)` — the only sanctioned symbol→ISIN path (invariant
  #2) — and then joined to prices on `(isin, series, trade_date)`. The price rows already carry
  their ISIN natively (both bhavcopy eras publish it); the master is engaged for the delivery side.

* **Nothing is dropped silently.** A delivery row whose symbol the master cannot resolve, or which
  resolves but matches no price row that session, is written to the `prices_raw_quarantine` dataset
  with a reason and counted in the returned report — a visible gap, not data loss. An *ambiguous*
  symbol is not quarantined: `IdentityMaster.resolve` raises `AmbiguousSymbolError`, because "we
  know two contradictory ISINs" is a different fact from "we know none", and only the human-review
  queue may settle it.

Offline by construction: this module takes parsed rows (or reads bytes back through `L0Store`) and
never fetches. `write_prices_raw` is the pure rows→partition core; `rebuild_prices_raw_from_l0` is
the L0-driven entry point the backfill (M1.9) and daily pipeline (M1.10) call.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from dataplatform.identity.master import Exchange, IdentityMaster
from dataplatform.ingest.bse import bhavcopy as bse_bhavcopy
from dataplatform.ingest.models import BhavcopyParse, PriceRow, UnidentifiedRow
from dataplatform.ingest.nse import bhavcopy, delivery
from dataplatform.ingest.nse.delivery import DeliveryRow, ResolvedDeliveryRow
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.paths import Layer, l1_partition_path, partition_path
from dataplatform.store.schemas import (
    PRICES_RAW_DATASET,
    PRICES_RAW_QUARANTINE_DATASET,
    PRICES_RAW_QUARANTINE_SCHEMA,
    PRICES_RAW_SCHEMA,
    PriceQuarantineReason,
    PricesRawRow,
    SchemaError,
    assert_raw_only,
    enforce_schema,
)

__all__ = [
    "PRICES_RAW_DATASET",
    "PRICES_RAW_QUARANTINE_DATASET",
    "PricesRawWriteReport",
    "SchemaError",
    "read_prices_raw",
    "rebuild_prices_raw_from_l0",
    "write_prices_raw",
    "write_unidentified_quarantine",
]

_LOG = get_logger(__name__)

_PRICE_Q: Final = Decimal("0.0001")
_PCT_Q: Final = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class PricesRawWriteReport:
    """The outcome of writing one `(prices_raw, date)` partition — the counts a caller must see.

    `path` is the canonical partition; `quarantine_path` is the sidecar dataset partition, present
    only when something was quarantined. The four delivery counts reconcile:
    `delivery_rows == delivery_joined + delivery_unresolved + delivery_orphaned`, so a caller can
    assert no delivery row was lost — the M1.8 "never dropped silently" contract made checkable.
    """

    trade_date: date
    exchange: Exchange
    path: Path
    rows_written: int
    delivery_rows: int
    delivery_joined: int
    delivery_unresolved: int
    delivery_orphaned: int
    quarantine_path: Path | None
    #: Rows of the *other* exchange already in the partition, read back and written out unchanged.
    rows_preserved: int = 0

    @property
    def quarantined(self) -> int:
        """Total delivery rows quarantined (unresolved plus resolved-but-orphaned)."""
        return self.delivery_unresolved + self.delivery_orphaned


def write_prices_raw(
    price_rows: Sequence[PriceRow],
    *,
    exchange: Exchange = Exchange.NSE,
    delivery_rows: Iterable[DeliveryRow] = (),
    unidentified_rows: Iterable[UnidentifiedRow] = (),
    master: IdentityMaster | None = None,
    data_root: Path | None = None,
) -> PricesRawWriteReport:
    """Write one exchange's session of raw prices — with delivery joined in — to its L1 partition.

    What it does: joins the session's delivery figures onto the price rows by `(isin, series,
    trade_date)` after resolving each delivery symbol to an ISIN through `master` (invariant #2),
    builds the `prices_raw` table against the declared schema (invariant #3, drift fails loud), and
    writes it whole to `L1/prices_raw/date=…/part.parquet`. The partition is shared by both
    exchanges, so rows already there for any *other* exchange are read back and written out again
    unchanged; only `exchange`'s own rows are replaced. Delivery rows that cannot be placed are
    written to the quarantine dataset and counted, never dropped.
    What it assumes: all `price_rows` are one exchange's single session — it raises `ValueError`
    otherwise, because a partition is exactly one `(dataset, date)`. `exchange` names that exchange;
    the price rows carry no exchange of their own, so the caller states it. One writer per partition
    at a time: two exchanges' backfills writing the same date concurrently would race the read-back.
    What it never does: store an adjusted price, resolve a symbol by name alone, or drop a delivery
    row. Passing delivery rows without a `master` is a `ValueError`: there is no legal way to place
    them without the identity path.

    Returns a `PricesRawWriteReport` whose delivery counts reconcile to the input delivery count.
    """
    trade_date = _single_session(price_rows)
    delivery_batch = tuple(delivery_rows)
    if delivery_batch and master is None:
        raise ValueError(
            "delivery rows were given without an IdentityMaster; a delivery row has no ISIN and "
            "the only legal symbol→ISIN path is the D2 master (invariant #2)"
        )

    resolved, unresolved = _resolve_delivery(
        delivery_batch, master=master, exchange=exchange, trade_date=trade_date
    )
    by_key = _delivery_index(resolved)

    out_rows = [_price_to_raw(row, exchange=exchange, deliv_index=by_key) for row in price_rows]

    # A resolved delivery key is "joined" iff a price row carried its (isin, series); the rest are
    # orphans (resolved but no matching price) and are quarantined, never dropped.
    used_keys = {(row.isin, row.series) for row in out_rows} & by_key.keys()
    orphaned = [row for key, row in by_key.items() if key not in used_keys]

    path = l1_partition_path(PRICES_RAW_DATASET, trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The partition is one (dataset, date) for both exchanges: the other exchange's rows already on
    # disk ride through unchanged and only this exchange's are replaced. Without this, the first BSE
    # session written would silently overwrite the NSE session for the same date (M3.1).
    preserved = _other_exchange_records(path, exchange)
    records = [_record(row) for row in out_rows] + preserved
    # Total key, leading with exchange, so the partition is byte-identical across re-derivations
    # regardless of parse order — and regardless of which exchange was written first.
    records.sort(key=_record_sort_key)
    table = pa.Table.from_pylist(records, schema=PRICES_RAW_SCHEMA)
    assert_raw_only(table.schema)
    enforce_schema(table, PRICES_RAW_SCHEMA, dataset=PRICES_RAW_DATASET)
    _write_table(table, path)

    unidentified = list(unidentified_rows)
    quarantine_path = _write_quarantine(
        unresolved,
        orphaned,
        unidentified,
        exchange=exchange,
        trade_date=trade_date,
        data_root=data_root,
    )

    delivery_joined = len(used_keys)
    report = PricesRawWriteReport(
        trade_date=trade_date,
        exchange=exchange,
        path=path,
        rows_written=len(out_rows),
        delivery_rows=len(delivery_batch),
        delivery_joined=delivery_joined,
        delivery_unresolved=len(unresolved),
        delivery_orphaned=len(orphaned),
        quarantine_path=quarantine_path,
        rows_preserved=len(preserved),
    )
    _LOG.info(
        "l1.prices_raw_written",
        dataset=PRICES_RAW_DATASET,
        exchange=exchange.value,
        trade_date=trade_date.isoformat(),
        path=str(path),
        rows=report.rows_written,
        rows_preserved=report.rows_preserved,
        delivery_rows=report.delivery_rows,
        delivery_joined=report.delivery_joined,
        delivery_unresolved=report.delivery_unresolved,
        delivery_orphaned=report.delivery_orphaned,
        state="PUBLISHED",
    )
    return report


def rebuild_prices_raw_from_l0(
    store: L0Store,
    bhavcopy_ref: L0Ref,
    *,
    delivery_ref: L0Ref | None = None,
    master: IdentityMaster | None = None,
    exchange: Exchange = Exchange.NSE,
    data_root: Path | None = None,
) -> PricesRawWriteReport:
    """Re-derive one session's `prices_raw` partition straight from its L0 payloads.

    The pipeline entry point and the idempotency guarantee made executable: `L0Store.get` re-hashes
    each payload on the way out, so the rows are derived from bytes that have not changed, and
    running this twice produces a byte-identical partition (acceptance 1). The bhavcopy parser is
    chosen by `exchange` — NSE and BSE publish different files, but both parse to the identical
    `PriceRow` and both dispatch to their era's parser from `bhavcopy_ref.logical_date`, so a BSE
    session lands in L1 under the same schema, ISIN-keyed, as an NSE one (M3.1). The delivery file,
    when given, is parsed and joined; `master` is required whenever `delivery_ref` is given.

    BSE's daily and recent-history sessions are the UDiFF era, which carries ISIN natively; the
    pre-08-Jul-2024 BSE legacy era has no ISIN column and is resolved through the scrip master by
    `bse.bhavcopy.resolve_legacy` on the B1/M1.13-gated backfill path, not here.
    """
    parsed = _parse_bhavcopy_l0(store, bhavcopy_ref, exchange=exchange)
    delivery_batch: tuple[DeliveryRow, ...] = ()
    if delivery_ref is not None:
        delivery_batch = delivery.parse_l0(store, delivery_ref)
    return write_prices_raw(
        parsed.rows,
        exchange=exchange,
        delivery_rows=delivery_batch,
        unidentified_rows=parsed.refused,
        master=master,
        data_root=data_root,
    )


def read_prices_raw(
    trade_date: date, *, data_root: Path | None = None
) -> tuple[dict[str, object], ...]:
    """Read one session's `prices_raw` partition back as plain records, schema-checked.

    Raises `FileNotFoundError` when the partition was never written — an absent partition is a gap
    for D7 to explain, not an empty day. Returns the rows as dicts (money as `Decimal`, dates as
    `date`) rather than a bespoke model: the writer's job is the bytes, and every reader downstream
    wants a different projection.
    """
    path = l1_partition_path(PRICES_RAW_DATASET, trade_date, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(
            f"no {PRICES_RAW_DATASET} partition for {trade_date.isoformat()}: {path}"
        )
    return tuple(pq.read_table(path, schema=PRICES_RAW_SCHEMA).to_pylist())


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _parse_bhavcopy_l0(store: L0Store, ref: L0Ref, *, exchange: Exchange) -> BhavcopyParse:
    """Parse a bhavcopy L0 payload with the parser its exchange requires.

    NSE and BSE ship different files (different columns, and BSE serves the UDiFF era uncompressed),
    but both emit the identical `PriceRow`, so the exchange is the only branch — no caller
    downstream sees which exchange's file it was. Only the NSE legacy era has ever published a
    placeholder ISIN, so the BSE branch reports no refusals rather than being unable to.
    """
    if exchange is Exchange.BSE:
        return BhavcopyParse(rows=bse_bhavcopy.parse_l0(store, ref))
    return bhavcopy.parse_l0_report(store, ref)


def _single_session(price_rows: Sequence[PriceRow]) -> date:
    """The one trade date the price rows share, or a `ValueError` naming the violation."""
    if not price_rows:
        raise ValueError("no price rows to write; a cash session always has some")
    trade_dates = {row.trade_date for row in price_rows}
    if len(trade_dates) > 1:
        raise ValueError(
            "price rows span more than one session: "
            f"{', '.join(sorted(d.isoformat() for d in trade_dates))}; a partition is one date"
        )
    return next(iter(trade_dates))


def _resolve_delivery(
    delivery_batch: Sequence[DeliveryRow],
    *,
    master: IdentityMaster | None,
    exchange: Exchange,
    trade_date: date,
) -> tuple[tuple[ResolvedDeliveryRow, ...], tuple[DeliveryRow, ...]]:
    """Resolve the delivery batch to ISINs through the master (invariant #2), or nothing to do.

    Delegates to `delivery.resolve`, which quarantines an unknown `(symbol, date)` into `unresolved`
    and lets an *ambiguous* one raise from the master. Also guards that the delivery file's own
    session matches the prices' session — a mismatched delivery file joined here would scatter one
    date's delivery figures onto another's prices.
    """
    if not delivery_batch or master is None:
        return (), tuple(delivery_batch)
    stray = sorted({row.trade_date for row in delivery_batch if row.trade_date != trade_date})
    if stray:
        raise ValueError(
            f"delivery rows are for {', '.join(d.isoformat() for d in stray)} but the prices are "
            f"for {trade_date.isoformat()}; delivery joins onto prices within one session only"
        )
    resolution = delivery.resolve(delivery_batch, master, exchange=exchange)
    return resolution.resolved, resolution.unresolved


def _delivery_index(
    resolved: Sequence[ResolvedDeliveryRow],
) -> dict[tuple[str, str], ResolvedDeliveryRow]:
    """Index resolved delivery rows by `(isin, series)` for the join onto prices.

    The delivery parser already guarantees a unique `(symbol, series)` per session; two symbols
    resolving to the same `(isin, series)` would be an identity contradiction, so a collision here
    raises rather than letting one security's delivery figure silently overwrite another's.
    """
    index: dict[tuple[str, str], ResolvedDeliveryRow] = {}
    for row in resolved:
        key = (row.isin, row.series)
        if key in index:
            raise ValueError(
                f"two delivery rows resolved to the same (isin, series) {key!r} in one session — "
                "an identity contradiction the join cannot arbitrate"
            )
        index[key] = row
    return index


def _price_to_raw(
    row: PriceRow,
    *,
    exchange: Exchange,
    deliv_index: dict[tuple[str, str], ResolvedDeliveryRow],
) -> PricesRawRow:
    """Build one output row: the price row's facts plus any delivery for its `(isin, series)`.

    Constructs the validated `PricesRawRow` (ISIN pattern, strict-`Decimal` money) rather than a
    bare record, so a malformed row fails here rather than as a puzzling arrow error at write.
    """
    match = deliv_index.get((row.isin, row.series))
    return PricesRawRow(
        isin=row.isin,
        exchange=exchange.value,
        symbol=row.symbol,
        series=row.series,
        trade_date=row.trade_date,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        last=row.last,
        prev_close=row.prev_close,
        total_traded_qty=row.total_traded_qty,
        total_traded_value=row.total_traded_value,
        total_trades=row.total_trades,
        deliv_qty=match.deliv_qty if match is not None else None,
        deliv_pct=match.deliv_pct if match is not None else None,
    )


def write_unidentified_quarantine(
    rows: Sequence[UnidentifiedRow],
    *,
    exchange: Exchange = Exchange.NSE,
    trade_date: date,
    reason: str = PriceQuarantineReason.ISIN_COLUMN_ABSENT,
    data_root: Path | None = None,
) -> Path | None:
    """Write a session whose rows have no identity at all to `prices_raw_quarantine`, and nowhere
    else.

    What it does: lands one quarantine row per source row, so a pre-ISIN (E1) session is *retained
    and counted* rather than dropped or refused. Returns the partition path, or `None` for an empty
    input.
    What it assumes: `prices_raw` is deliberately left untouched for `trade_date` — these rows
    cannot be keyed, and invariant #2 makes ISIN the only join key, so there is nothing legal to
    write there. It also assumes it owns the date's quarantine partition: the partition is written
    whole, so a session that already has *delivery* rows quarantined must be re-derived through
    `write_prices_raw` instead, which passes both sets in one call.
    What it never does: invent an ISIN, or preserve the prices. The prices stay where they are
    already immutable and re-derivable — the L0 payload. The quarantine row is the honest
    enumeration of what could not be joined, which is the number a coverage claim rests on.
    """
    if not rows:
        return None
    return _write_quarantine(
        (),
        (),
        rows,
        exchange=exchange,
        trade_date=trade_date,
        data_root=data_root,
        unidentified_reason=reason,
    )


def _write_quarantine(
    unresolved: Sequence[DeliveryRow],
    orphaned: Sequence[ResolvedDeliveryRow],
    unidentified: Sequence[UnidentifiedRow] = (),
    *,
    exchange: Exchange,
    trade_date: date,
    data_root: Path | None,
    unidentified_reason: str = PriceQuarantineReason.ISIN_NOT_PUBLISHED,
) -> Path | None:
    """Write the delivery rows that could not be placed to the quarantine dataset, or nothing.

    Returns the quarantine partition path when anything was quarantined, else `None`. Its own
    dataset (`prices_raw_quarantine`), never a second file in the `prices_raw` partition, so a scan
    of `prices_raw` never reads a quarantined row as canonical.
    """
    if not unresolved and not orphaned and not unidentified:
        return None
    records = (
        [
            {
                "symbol": row.symbol,
                "series": row.series,
                "trade_date": row.trade_date,
                "exchange": exchange.value,
                "isin": None,
                "deliv_qty": row.deliv_qty,
                "deliv_pct": _q(row.deliv_pct, _PCT_Q) if row.deliv_pct is not None else None,
                "reason": PriceQuarantineReason.SYMBOL_UNRESOLVED,
            }
            for row in unresolved
        ]
        + [
            {
                "symbol": row.symbol,
                "series": row.series,
                "trade_date": row.trade_date,
                "exchange": exchange.value,
                "isin": row.isin,
                "deliv_qty": row.deliv_qty,
                "deliv_pct": _q(row.deliv_pct, _PCT_Q) if row.deliv_pct is not None else None,
                "reason": PriceQuarantineReason.NO_MATCHING_PRICE,
            }
            for row in orphaned
        ]
        + [
            {
                "symbol": row.symbol,
                "series": row.series,
                "trade_date": row.trade_date,
                "exchange": exchange.value,
                # The literal the exchange published, kept verbatim: "the source said DUMMY" is a
                # fact, and blanking it would leave the row indistinguishable from an unresolved
                # symbol, which is a different failure with a different fix. An *empty* literal is
                # the pre-ISIN era's honest answer — there was no column to quote — and it is
                # stored as NULL, with `reason` carrying the distinction.
                "isin": row.stated_isin or None,
                "deliv_qty": None,
                "deliv_pct": None,
                "reason": unidentified_reason,
            }
            for row in unidentified
        ]
    )
    records.sort(key=lambda rec: (rec["reason"], rec["symbol"], rec["series"]))
    path = partition_path(Layer.L1, PRICES_RAW_QUARANTINE_DATASET, trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=PRICES_RAW_QUARANTINE_SCHEMA)
    enforce_schema(table, PRICES_RAW_QUARANTINE_SCHEMA, dataset=PRICES_RAW_QUARANTINE_DATASET)
    _write_table(table, path)
    _LOG.warning(
        "l1.prices_raw_quarantined",
        dataset=PRICES_RAW_QUARANTINE_DATASET,
        exchange=exchange.value,
        trade_date=trade_date.isoformat(),
        path=str(path),
        unresolved=len(unresolved),
        orphaned=len(orphaned),
    )
    return path


def _q(value: Decimal, quantum: Decimal) -> Decimal:
    """Quantise to a fixed scale with half-up rounding, so stored money is byte-deterministic."""
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _record(row: PricesRawRow) -> dict[str, object]:
    """One validated row as the parquet record the schema expects, money quantised to its scale."""
    return {
        "isin": row.isin,
        "exchange": row.exchange,
        "symbol": row.symbol,
        "series": row.series,
        "trade_date": row.trade_date,
        "open": _q(row.open, _PRICE_Q),
        "high": _q(row.high, _PRICE_Q),
        "low": _q(row.low, _PRICE_Q),
        "close": _q(row.close, _PRICE_Q),
        "last": _q(row.last, _PRICE_Q),
        "prev_close": _q(row.prev_close, _PRICE_Q),
        "total_traded_qty": row.total_traded_qty,
        "total_traded_value": _q(row.total_traded_value, _PRICE_Q),
        "total_trades": row.total_trades,
        "deliv_qty": row.deliv_qty,
        "deliv_pct": _q(row.deliv_pct, _PCT_Q) if row.deliv_pct is not None else None,
    }


def _record_sort_key(record: dict[str, object]) -> tuple[str, str, str, str]:
    """The partition's total order: exchange first, so one exchange's block is unaffected by the
    other's presence, then the key that was already total within an exchange."""
    return (
        str(record["exchange"]),
        str(record["isin"]),
        str(record["symbol"]),
        str(record["series"]),
    )


def _other_exchange_records(path: Path, exchange: Exchange) -> list[dict[str, object]]:
    """The rows already in the partition that belong to any exchange other than `exchange`.

    Read back through the declared schema so a drifted file fails loud here rather than being
    re-serialised. An absent partition contributes nothing — the common case for a first write.
    """
    if not path.exists():
        return []
    stored: list[dict[str, object]] = pq.read_table(path, schema=PRICES_RAW_SCHEMA).to_pylist()
    return [record for record in stored if record["exchange"] != exchange.value]


def _write_table(table: pa.Table, path: Path) -> None:
    """Write a parquet table whole via a staging file, so a partition is never half-written."""
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)
