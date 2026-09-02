"""F&O EOD aggregates (M3.7) - contract rows in L1, per-underlying sentiment aggregates in L2.

The derivation half of the F&O data module. `dataplatform.ingest.nse.fo_bhavcopy` turns the
exchange file into `FoContractRow`s; this module writes those raw contract rows to **L1** and
computes the per-underlying **L2** aggregates the analyst reads as *sentiment context only* - open
interest and its change, the put/call ratio, futures basis versus spot, and a rollover proxy. None
of it is ever traded (EXECUTION_PLAN §4.1 row 12): this module imports nothing from `execution` and
exposes no callable that could place, size, or route an order. It is pure `bytes -> rows -> nums`.

**L1 (`fo_contracts`)** is the raw contract-level truth - every traded contract, values exactly as
published, recomputable from the L0 payload alone (invariant #1, #3). **L2 (`fo_aggregates`)** is
derived and, per acceptance criterion 2, *rebuilds from L1*: `rebuild_l2_from_l1` reads a session's
contract partition back and re-derives the aggregates byte-for-byte, so L2 is never a place data
only *enters* - it is always reconstructible.

Two design points worth stating:

* **Basis needs no cross-source join.** The exchange stamps `UndrlygPric` (the underlier's spot) on
  every derivative row, so futures basis is near-future settlement minus spot, computed *inside this
  one file*. Invariant #2 ("nothing joins on a raw symbol") is therefore never engaged for the
  numbers. The one symbol-to-ISIN step is attaching a *stock* underlier's ISIN to its aggregate so
  the aggregate is ISIN-addressable like everything else - and that goes through the D2 identity
  master, the only sanctioned path. An index underlier (NIFTY, BANKNIFTY, INDIAVIX) is not a
  security and carries no ISIN; its aggregate is keyed by symbol, which is correct, not a violation.
* **Every derived ratio is a fixed-scale `Decimal`.** PCR, basis-percent and the rollover proxy are
  quantized to a defined scale before they are stored, so the parquet is byte-deterministic
  (re-derivation from the same L1 is identical) and a hand check reconciles exactly (criterion 1).
  Money is `Decimal` throughout; a float never touches these values.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, NamedTuple

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from dataplatform.identity.master import Exchange, IdentityMaster
from dataplatform.ingest.models import ISIN_PATTERN
from dataplatform.ingest.nse.fo_bhavcopy import FoContractRow, FoInstrumentType, OptionType
from dataplatform.logging import get_logger
from dataplatform.store.paths import l1_partition_path, l2_partition_path

__all__ = [
    "FO_AGGREGATES_DATASET",
    "FO_CONTRACTS_DATASET",
    "UnderlyingAggregate",
    "UnderlyingKind",
    "build_aggregates",
    "read_l1",
    "read_l2",
    "rebuild_l2_from_l1",
    "write_l1",
    "write_l2",
]

_LOG = get_logger(__name__)

#: L1 dataset name - `data/L1/fo_contracts/date=YYYY-MM-DD/part.parquet` (§4.2).
FO_CONTRACTS_DATASET: Final = "fo_contracts"

#: L2 dataset name - `data/L2/fo_aggregates/date=YYYY-MM-DD/part.parquet` (§4.2).
FO_AGGREGATES_DATASET: Final = "fo_aggregates"

#: Quantum for a price/basis value (four decimal places) and for a ratio (six). Fixing the scale is
#: what makes the derived L2 byte-deterministic and a hand check exact - `1000/800` is stored as
#: exactly `1.250000`, not a float that prints differently on a different machine.
_PRICE_Q: Final = Decimal("0.0001")
_RATIO_Q: Final = Decimal("0.000001")


class UnderlyingKind(StrEnum):
    """Whether an F&O underlier is a stock index or a single stock - the two have different keys.

    A `STOCK` resolves to an ISIN through the identity master; an `INDEX` has none (an index is not
    a security) and is addressed by its symbol. The distinction is read from the contract's
    instrument type (`FUTIDX`/`OPTIDX`/`FUTIVX` are index; `FUTSTK`/`OPTSTK` stock), not guessed.
    """

    INDEX = "INDEX"
    STOCK = "STOCK"


class UnderlyingAggregate(BaseModel):
    """One session's F&O sentiment aggregates for one underlier - the canonical L2 row.

    What it does: carry the derived numbers the analyst reads as context - total open interest and
    its change, the put/call ratio, near-future basis versus spot, and a rollover proxy.
    What it assumes: every input `FoContractRow` shares this underlier and trade date, which
    `build_aggregates` enforces before constructing one.
    What it never does: carry an order, a position, or anything tradeable. The futures fields are
    `None` for an underlier with no futures that session, and the PCR is `None` when there is no
    call open interest to divide by - an absent number is stated as absent, never as a zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date = Field(description="the exchange session these aggregates are for")
    underlying: str = Field(min_length=1, description="the underlier symbol (TckrSymb)")
    underlying_kind: UnderlyingKind = Field(description="INDEX or STOCK")
    isin: str | None = Field(
        default=None,
        pattern=ISIN_PATTERN,
        description="the stock underlier's ISIN via the D2 master; None for an index or unresolved",
    )

    spot: Decimal = Field(ge=0, description="UndrlygPric - the underlier's spot for the session")
    total_oi: int = Field(ge=0, description="open interest summed across every contract")
    total_oi_change: int = Field(description="one-day OI change summed across contracts, signed")
    call_oi: int = Field(ge=0, description="open interest summed across call options")
    put_oi: int = Field(ge=0, description="open interest summed across put options")
    pcr_oi: Decimal | None = Field(
        default=None, ge=0, description="put_oi / call_oi; None when there is no call OI"
    )

    near_expiry: date | None = Field(
        default=None, description="nearest futures expiry; None when the underlier has no futures"
    )
    near_future_price: Decimal | None = Field(
        default=None, ge=0, description="settlement price of the nearest-expiry future"
    )
    basis: Decimal | None = Field(
        default=None,
        description="near_future_price minus spot; signed (a future can trade at a discount)",
    )
    basis_pct: Decimal | None = Field(
        default=None, description="basis as a percent of spot; signed"
    )
    near_month_oi: int | None = Field(
        default=None, ge=0, description="open interest of the nearest-expiry future(s)"
    )
    next_month_oi: int | None = Field(
        default=None, ge=0, description="open interest of the second-nearest-expiry future(s)"
    )
    rollover_pct: Decimal | None = Field(
        default=None,
        ge=0,
        description="rollover proxy: next_month_oi / (near_month_oi + next_month_oi)",
    )


class _FutureFields(NamedTuple):
    """The basis/rollover half of an aggregate - all-`None` when the underlier has no futures."""

    near_expiry: date | None
    near_future_price: Decimal | None
    basis: Decimal | None
    basis_pct: Decimal | None
    near_month_oi: int | None
    next_month_oi: int | None
    rollover_pct: Decimal | None


def build_aggregates(
    rows: Iterable[FoContractRow], *, master: IdentityMaster | None = None
) -> tuple[UnderlyingAggregate, ...]:
    """Derive the per-underlier L2 aggregates from one session's contract rows.

    What it does: groups the rows by underlier and, for each, sums open interest and its change,
    splits option OI into the put/call ratio, and - when the underlier has futures - computes the
    near-future basis versus the in-file spot and a rollover proxy from the near/next expiry OI
    split. Returns the aggregates sorted by underlier symbol, so the output (and the parquet written
    from it) is deterministic.
    What it assumes: `rows` are one session's (a single `trade_date`); it raises `ValueError` if
    they are not, or if one underlier's rows disagree on index-versus-stock - both are
    contradictions a caller must not paper over. When `master` is given, a *stock* underlier's
    symbol is resolved to its ISIN through it (the only sanctioned symbol-to-ISIN path, invariant
    #2); an unresolved symbol leaves `isin` None and is counted, never guessed.
    What it never does: fetch, adjust a price, or emit anything tradeable.
    """
    grouped: dict[str, list[FoContractRow]] = defaultdict(list)
    trade_dates: set[date] = set()
    for row in rows:
        grouped[row.underlying].append(row)
        trade_dates.add(row.trade_date)

    if not grouped:
        return ()
    if len(trade_dates) > 1:
        raise ValueError(
            "contract rows span more than one session: "
            f"{', '.join(sorted(d.isoformat() for d in trade_dates))}; aggregates are per session"
        )
    trade_date = next(iter(trade_dates))

    aggregates = [
        _aggregate_one(underlying, group, trade_date=trade_date, master=master)
        for underlying, group in sorted(grouped.items())
    ]
    _LOG.info(
        "fo_aggregates.built",
        trade_date=trade_date.isoformat(),
        underlyings=len(aggregates),
        contracts=sum(len(g) for g in grouped.values()),
        state="NORMALIZED",
    )
    return tuple(aggregates)


def _aggregate_one(
    underlying: str,
    group: Sequence[FoContractRow],
    *,
    trade_date: date,
    master: IdentityMaster | None,
) -> UnderlyingAggregate:
    """Compute one underlier's aggregates from its contract rows."""
    kinds = {row.is_index_underlying for row in group}
    if len(kinds) > 1:
        raise ValueError(
            f"underlier {underlying!r} has both index and stock contracts, which cannot both be "
            "its underlying - the F&O file is inconsistent for this symbol"
        )
    is_index = next(iter(kinds))
    kind = UnderlyingKind.INDEX if is_index else UnderlyingKind.STOCK

    total_oi = sum(row.open_interest for row in group)
    total_oi_change = sum(row.change_in_oi for row in group)

    options = [row for row in group if row.is_option]
    call_oi = sum(row.open_interest for row in options if row.option_type is OptionType.CE)
    put_oi = sum(row.open_interest for row in options if row.option_type is OptionType.PE)
    pcr_oi = _q(Decimal(put_oi) / Decimal(call_oi), _RATIO_Q) if call_oi > 0 else None

    spot = _spot_of(group)
    futures = _future_fields(group, spot=spot)
    isin = _resolve_isin(underlying, kind, trade_date=trade_date, master=master)

    return UnderlyingAggregate(
        trade_date=trade_date,
        underlying=underlying,
        underlying_kind=kind,
        isin=isin,
        spot=spot,
        total_oi=total_oi,
        total_oi_change=total_oi_change,
        call_oi=call_oi,
        put_oi=put_oi,
        pcr_oi=pcr_oi,
        near_expiry=futures.near_expiry,
        near_future_price=futures.near_future_price,
        basis=futures.basis,
        basis_pct=futures.basis_pct,
        near_month_oi=futures.near_month_oi,
        next_month_oi=futures.next_month_oi,
        rollover_pct=futures.rollover_pct,
    )


def _spot_of(group: Sequence[FoContractRow]) -> Decimal:
    """The underlier's spot for the session.

    The exchange stamps the same `UndrlygPric` on every one of an underlier's rows, so the value is
    read from the nearest-expiry future when there is one (the most canonical carrier) and otherwise
    from the first contract row. Quantized to the price scale so it stores deterministically.
    """
    futures = sorted(
        (row for row in group if row.is_future),
        key=lambda r: (r.expiry, -r.open_interest, r.settle),
    )
    source = futures[0] if futures else group[0]
    return _q(source.underlying_price, _PRICE_Q)


def _future_fields(group: Sequence[FoContractRow], *, spot: Decimal) -> _FutureFields:
    """The basis and rollover fields, or all-`None` when the underlier has no futures that day."""
    futures = [row for row in group if row.is_future]
    if not futures:
        return _FutureFields(None, None, None, None, None, None, None)

    # Open interest per expiry, and a representative settlement price per expiry (the highest-OI
    # future at that expiry - normally the only one). Expiries ascending: [0] is the near month.
    oi_by_expiry: dict[date, int] = defaultdict(int)
    settle_by_expiry: dict[date, tuple[int, Decimal]] = {}
    for row in futures:
        oi_by_expiry[row.expiry] += row.open_interest
        best = settle_by_expiry.get(row.expiry)
        if best is None or row.open_interest > best[0]:
            settle_by_expiry[row.expiry] = (row.open_interest, row.settle)
    expiries = sorted(oi_by_expiry)

    near_expiry = expiries[0]
    near_future_price = _q(settle_by_expiry[near_expiry][1], _PRICE_Q)
    basis = _q(near_future_price - spot, _PRICE_Q)
    basis_pct = _q(basis / spot * Decimal(100), _RATIO_Q) if spot > 0 else None

    near_month_oi = oi_by_expiry[near_expiry]
    next_month_oi = oi_by_expiry[expiries[1]] if len(expiries) > 1 else None
    rollover_pct = (
        _q(Decimal(next_month_oi) / Decimal(near_month_oi + next_month_oi), _RATIO_Q)
        if next_month_oi is not None and (near_month_oi + next_month_oi) > 0
        else None
    )

    return _FutureFields(
        near_expiry=near_expiry,
        near_future_price=near_future_price,
        basis=basis,
        basis_pct=basis_pct,
        near_month_oi=near_month_oi,
        next_month_oi=next_month_oi,
        rollover_pct=rollover_pct,
    )


def _resolve_isin(
    underlying: str,
    kind: UnderlyingKind,
    *,
    trade_date: date,
    master: IdentityMaster | None,
) -> str | None:
    """A stock underlier's ISIN via the D2 master, or None for an index / no master / unresolved.

    The only symbol-to-ISIN step in the F&O module, and it goes through the sanctioned identity path
    (invariant #2). An index is never resolved - it has no ISIN. A stock symbol the master has never
    seen returns None (quarantined-as-unresolved, counted below), while an *ambiguous* symbol raises
    from the master: "we do not know" and "we know two contradictory things" are different facts.
    """
    if kind is UnderlyingKind.INDEX or master is None:
        return None
    isin = master.try_resolve(underlying, trade_date, exchange=Exchange.NSE)
    if isin is None:
        _LOG.warning(
            "fo_aggregates.unresolved_underlying",
            underlying=underlying,
            trade_date=trade_date.isoformat(),
        )
    return isin


def _q(value: Decimal, quantum: Decimal) -> Decimal:
    """Quantize to a fixed scale with half-up rounding, so stored values are byte-deterministic."""
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


# ── L1: raw contract rows ──────────────────────────────────────────────────────────────────────

_L1_SCHEMA: Final = pa.schema(
    [
        pa.field("trade_date", pa.date32(), nullable=False),
        pa.field("underlying", pa.string(), nullable=False),
        pa.field("instrument_type", pa.string(), nullable=False),
        pa.field("expiry", pa.date32(), nullable=False),
        pa.field("strike", pa.decimal128(20, 4), nullable=True),
        pa.field("option_type", pa.string(), nullable=True),
        pa.field("close", pa.decimal128(20, 4), nullable=False),
        pa.field("settle", pa.decimal128(20, 4), nullable=False),
        pa.field("underlying_price", pa.decimal128(20, 4), nullable=False),
        pa.field("open_interest", pa.int64(), nullable=False),
        pa.field("change_in_oi", pa.int64(), nullable=False),
        pa.field("total_traded_qty", pa.int64(), nullable=False),
        pa.field("total_traded_value", pa.decimal128(28, 4), nullable=False),
        pa.field("total_trades", pa.int64(), nullable=False),
        pa.field("isin", pa.string(), nullable=True),
    ]
)


def write_l1(rows: Sequence[FoContractRow], *, data_root: Path | None = None) -> Path:
    """Write one session's raw contract rows to their L1 partition and return the file's path.

    Idempotent per `(dataset, date)`: rows go in file order and the file is written whole to a
    temporary name and renamed over the target, so re-deriving a session from L0 produces the same
    bytes and a crash mid-write cannot leave a half partition readable. Raw values only - invariant
    #3 has nothing to breach here. Raises `ValueError` for an empty batch or rows from more than one
    session (a partition is exactly one date).
    """
    if not rows:
        raise ValueError("no contract rows to write; an F&O session always has some")
    trade_dates = {row.trade_date for row in rows}
    if len(trade_dates) > 1:
        raise ValueError(
            "contract rows span more than one session: "
            f"{', '.join(sorted(d.isoformat() for d in trade_dates))}"
        )
    trade_date = next(iter(trade_dates))

    path = l1_partition_path(FO_CONTRACTS_DATASET, trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [
            {
                "trade_date": row.trade_date,
                "underlying": row.underlying,
                "instrument_type": row.instrument_type.value,
                "expiry": row.expiry,
                "strike": _q(row.strike, _PRICE_Q) if row.strike is not None else None,
                "option_type": row.option_type.value if row.option_type is not None else None,
                "close": _q(row.close, _PRICE_Q),
                "settle": _q(row.settle, _PRICE_Q),
                "underlying_price": _q(row.underlying_price, _PRICE_Q),
                "open_interest": row.open_interest,
                "change_in_oi": row.change_in_oi,
                "total_traded_qty": row.total_traded_qty,
                "total_traded_value": _q(row.total_traded_value, _PRICE_Q),
                "total_trades": row.total_trades,
                "isin": row.isin,
            }
            for row in rows
        ],
        schema=_L1_SCHEMA,
    )
    _write_table(table, path)
    _LOG.info(
        "fo_aggregates.l1_written",
        dataset=FO_CONTRACTS_DATASET,
        trade_date=trade_date.isoformat(),
        path=str(path),
        rows=len(rows),
        state="NORMALIZED",
    )
    return path


def read_l1(trade_date: date, *, data_root: Path | None = None) -> tuple[FoContractRow, ...]:
    """Read one session's contract rows back out of L1.

    Raises `FileNotFoundError` when the partition was never written - an absent partition is a gap
    for D7 to explain, not an empty day.
    """
    path = l1_partition_path(FO_CONTRACTS_DATASET, trade_date, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(
            f"no {FO_CONTRACTS_DATASET} partition for {trade_date.isoformat()}: {path}"
        )
    records = pq.read_table(path, schema=_L1_SCHEMA).to_pylist()
    return tuple(
        FoContractRow(
            trade_date=record["trade_date"],
            underlying=record["underlying"],
            instrument_type=FoInstrumentType(record["instrument_type"]),
            expiry=record["expiry"],
            strike=record["strike"],
            option_type=(
                OptionType(record["option_type"]) if record["option_type"] is not None else None
            ),
            close=record["close"],
            settle=record["settle"],
            underlying_price=record["underlying_price"],
            open_interest=record["open_interest"],
            change_in_oi=record["change_in_oi"],
            total_traded_qty=record["total_traded_qty"],
            total_traded_value=record["total_traded_value"],
            total_trades=record["total_trades"],
            isin=record["isin"],
        )
        for record in records
    )


# ── L2: per-underlier aggregates ─────────────────────────────────────────────────────────────

_L2_SCHEMA: Final = pa.schema(
    [
        pa.field("trade_date", pa.date32(), nullable=False),
        pa.field("underlying", pa.string(), nullable=False),
        pa.field("underlying_kind", pa.string(), nullable=False),
        pa.field("isin", pa.string(), nullable=True),
        pa.field("spot", pa.decimal128(20, 4), nullable=False),
        pa.field("total_oi", pa.int64(), nullable=False),
        pa.field("total_oi_change", pa.int64(), nullable=False),
        pa.field("call_oi", pa.int64(), nullable=False),
        pa.field("put_oi", pa.int64(), nullable=False),
        pa.field("pcr_oi", pa.decimal128(20, 6), nullable=True),
        pa.field("near_expiry", pa.date32(), nullable=True),
        pa.field("near_future_price", pa.decimal128(20, 4), nullable=True),
        pa.field("basis", pa.decimal128(20, 4), nullable=True),
        pa.field("basis_pct", pa.decimal128(20, 6), nullable=True),
        pa.field("near_month_oi", pa.int64(), nullable=True),
        pa.field("next_month_oi", pa.int64(), nullable=True),
        pa.field("rollover_pct", pa.decimal128(20, 6), nullable=True),
    ]
)


def write_l2(aggregates: Sequence[UnderlyingAggregate], *, data_root: Path | None = None) -> Path:
    """Write one session's per-underlier aggregates to their L2 partition and return its path.

    Idempotent and whole-file per `(dataset, date)`, like the L1 write. Raises `ValueError` for an
    empty batch or aggregates spanning more than one session.
    """
    if not aggregates:
        raise ValueError("no aggregates to write")
    trade_dates = {agg.trade_date for agg in aggregates}
    if len(trade_dates) > 1:
        raise ValueError(
            "aggregates span more than one session: "
            f"{', '.join(sorted(d.isoformat() for d in trade_dates))}"
        )
    trade_date = next(iter(trade_dates))

    path = l2_partition_path(FO_AGGREGATES_DATASET, trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [
            {
                "trade_date": agg.trade_date,
                "underlying": agg.underlying,
                "underlying_kind": agg.underlying_kind.value,
                "isin": agg.isin,
                "spot": agg.spot,
                "total_oi": agg.total_oi,
                "total_oi_change": agg.total_oi_change,
                "call_oi": agg.call_oi,
                "put_oi": agg.put_oi,
                "pcr_oi": agg.pcr_oi,
                "near_expiry": agg.near_expiry,
                "near_future_price": agg.near_future_price,
                "basis": agg.basis,
                "basis_pct": agg.basis_pct,
                "near_month_oi": agg.near_month_oi,
                "next_month_oi": agg.next_month_oi,
                "rollover_pct": agg.rollover_pct,
            }
            for agg in aggregates
        ],
        schema=_L2_SCHEMA,
    )
    _write_table(table, path)
    _LOG.info(
        "fo_aggregates.l2_written",
        dataset=FO_AGGREGATES_DATASET,
        trade_date=trade_date.isoformat(),
        path=str(path),
        underlyings=len(aggregates),
        state="PUBLISHED",
    )
    return path


def read_l2(trade_date: date, *, data_root: Path | None = None) -> tuple[UnderlyingAggregate, ...]:
    """Read one session's aggregates back out of L2.

    Raises `FileNotFoundError` when the partition was never written.
    """
    path = l2_partition_path(FO_AGGREGATES_DATASET, trade_date, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(
            f"no {FO_AGGREGATES_DATASET} partition for {trade_date.isoformat()}: {path}"
        )
    records = pq.read_table(path, schema=_L2_SCHEMA).to_pylist()
    return tuple(
        UnderlyingAggregate(
            trade_date=record["trade_date"],
            underlying=record["underlying"],
            underlying_kind=UnderlyingKind(record["underlying_kind"]),
            isin=record["isin"],
            spot=record["spot"],
            total_oi=record["total_oi"],
            total_oi_change=record["total_oi_change"],
            call_oi=record["call_oi"],
            put_oi=record["put_oi"],
            pcr_oi=record["pcr_oi"],
            near_expiry=record["near_expiry"],
            near_future_price=record["near_future_price"],
            basis=record["basis"],
            basis_pct=record["basis_pct"],
            near_month_oi=record["near_month_oi"],
            next_month_oi=record["next_month_oi"],
            rollover_pct=record["rollover_pct"],
        )
        for record in records
    )


def rebuild_l2_from_l1(
    trade_date: date,
    *,
    master: IdentityMaster | None = None,
    data_root: Path | None = None,
) -> Path:
    """Re-derive one session's L2 aggregates from its L1 contract partition and write them.

    This is acceptance criterion 2 made executable: L2 is not a place data only enters - it is
    always reconstructible from L1 (which is itself reconstructible from L0). Reads the contract
    rows back from L1, rebuilds the aggregates, and writes the L2 partition, returning its path.
    Deterministic: the same L1 partition yields byte-identical L2.
    """
    rows = read_l1(trade_date, data_root=data_root)
    aggregates = build_aggregates(rows, master=master)
    return write_l2(aggregates, data_root=data_root)


def _write_table(table: pa.Table, path: Path) -> None:
    """Write a parquet table whole via a staging file, so a partition is never half-written."""
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)
