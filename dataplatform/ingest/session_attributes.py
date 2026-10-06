"""Carry the columns the price parsers dropped into L1 — `price_session_attributes`, from L0 only.

``uv run python -m dataplatform.ingest.session_attributes --from 2016-09-01 --to 2026-10-05``

Catalogue §A11 lists four published columns that never reached L1: the session VWAP (`AVG_PRICE`,
NSE `sec_bhavdata_full`, 2019-10-01 on), the board lot and long instrument name (`NewBrdLotQty`,
`FinInstrmNm`, both UDiFF bhavcopies, 2024-07-08 on) and BSE's ex-event marker (`TDCLOINDI`, BSE
legacy bhavcopy, to 2024-07-05). This re-derives them per session into the sibling dataset declared
in `dataplatform.store.schemas` (why a sibling and not new `prices_raw` columns is recorded there).

Identity, per source, through the same paths the price rows use (invariant #2):

* UDiFF rows carry `ISIN` natively.
* `sec_bhavdata_full` carries none; a VWAP is placed on the ISIN the **same session's NSE
  bhavcopy** states for that `(symbol, series)` — the exchange's own statement for that day, no
  master lookup. A `(symbol, series)` the bhavcopy does not state, or states twice, is counted
  (`vwap_unplaced`) and not written.
* BSE legacy rows are keyed on the scrip code; resolved through the D2 master's scrip index
  (`corp_actions.build_scrip_index`), exactly as `backfill._write_bse_legacy` resolves the prices.
  A marker on a scrip the master does not know is counted (`marker_unresolved`).

Only rows that state at least one attribute are written: a BSE legacy session has a marker on a
handful of rows and nothing on the rest.

What it never does: fetch, write `prices_raw`, compute anything (the VWAP is the file's own), or
let a marker touch a factor — `dataplatform.quality.ex_marker_witness` only reports agreement.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import get_settings
from dataplatform.identity.master import Exchange, IdentityStore
from dataplatform.ingest.backfill import (
    BSE_BHAVCOPY,
    BSE_BHAVCOPY_LEGACY,
    NSE_BHAVCOPY,
    NSE_DELIVERY,
    SOURCE_SETS,
)
from dataplatform.ingest.bse import bhavcopy as bse_bhavcopy
from dataplatform.ingest.corp_actions import build_scrip_index
from dataplatform.ingest.models import ParseError, is_keyable_isin
from dataplatform.ingest.nse import bhavcopy, delivery
from dataplatform.ingest.nse import bhavcopy_udiff as nse_udiff
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.paths import Layer, layer_root, partition_path
from dataplatform.store.schemas import (
    SESSION_ATTRIBUTES_DATASET,
    SESSION_ATTRIBUTES_SCHEMA,
    assert_raw_only,
    enforce_schema,
)

__all__ = [
    "SessionAttribute",
    "SessionAttributesReport",
    "UdiffAttributes",
    "build_session",
    "main",
    "parse_udiff_attributes",
    "read_session_attributes",
    "write_session_attributes",
]

_LOG = get_logger(__name__)

_PRICE_Q: Final = Decimal("0.0001")
_ZIP_MAGIC: Final = b"PK\x03\x04"


@dataclass(frozen=True, slots=True)
class UdiffAttributes:
    """`NewBrdLotQty` and `FinInstrmNm` for one UDiFF row, with the row's own identity."""

    isin: str
    symbol: str
    series: str
    trade_date: date
    board_lot: int | None
    instrument_name: str | None


@dataclass(frozen=True, slots=True)
class SessionAttribute:
    """One `price_session_attributes` row: a price row's key and what was published about it."""

    isin: str
    exchange: str
    symbol: str
    series: str
    trade_date: date
    vwap: Decimal | None = None
    board_lot: int | None = None
    instrument_name: str | None = None
    ex_marker: str | None = None

    def key(self) -> tuple[str, str, str, str]:
        """The partition's total order, the same one `prices_raw` uses."""
        return (self.exchange, self.isin, self.symbol, self.series)


@dataclass(slots=True)
class SessionAttributesReport:
    """Run totals, so the coverage of each attribute is stated rather than discovered."""

    sessions: int = 0
    written: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    failures: list[tuple[date, str]] = field(default_factory=list)


def parse_udiff_attributes(
    payload: bytes, *, filename: str, columns: Sequence[str]
) -> tuple[UdiffAttributes, ...]:
    """Read `NewBrdLotQty` / `FinInstrmNm` from one UDiFF cash bhavcopy (NSE zipped, BSE bare).

    `columns` is the exchange parser's own `UDIFF_COLUMNS`, required to equal the header exactly —
    the same refusal the price parsers make. Rows must be the header's width and cash equity
    (`Sgmt=CM`, `FinInstrmTp=STK`) with a keyable ISIN; anything else is a `ParseError`, because a
    file the price parser would refuse must not contribute attributes either.
    """
    text = _text_of(payload, filename=filename)
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    named = tuple(name.strip() for name in header) if header is not None else ()
    if named != tuple(columns):
        raise ParseError("unexpected UDiFF header", filename=filename, line=1)
    out: list[UdiffAttributes] = []
    for record in reader:
        if not record or not any(value.strip() for value in record):
            continue
        line = reader.line_num
        if len(record) != len(columns):
            raise ParseError(
                f"row has {len(record)} fields, header has {len(columns)}",
                filename=filename,
                line=line,
            )
        row = dict(zip(columns, (value.strip() for value in record), strict=True))
        if row["Sgmt"] != "CM" or row["FinInstrmTp"] != "STK":
            raise ParseError("not a cash-equity row", filename=filename, line=line)
        if not is_keyable_isin(row["ISIN"]):
            raise ParseError(f"ISIN {row['ISIN']!r} is not keyable", filename=filename, line=line)
        lot = row["NewBrdLotQty"]
        if lot and not lot.isdigit():
            raise ParseError(f"NewBrdLotQty is {lot!r}", filename=filename, line=line)
        out.append(
            UdiffAttributes(
                isin=row["ISIN"],
                symbol=row["TckrSymb"],
                series=row["SctySrs"],
                trade_date=date.fromisoformat(row["TradDt"]),
                board_lot=int(lot) if lot else None,
                instrument_name=row["FinInstrmNm"] or None,
            )
        )
    if len({a.trade_date for a in out}) > 1:
        raise ParseError("rows span more than one session", filename=filename)
    return tuple(out)


def _text_of(payload: bytes, *, filename: str) -> str:
    if payload.startswith(_ZIP_MAGIC):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if len(names) != 1:
                raise ParseError(f"archive holds {len(names)} members, not 1", filename=filename)
            payload = archive.read(names[0])
    return payload.decode("utf-8")


def _ref(l0: L0Store, source_set: str, day: date, register: SourceRegister) -> L0Ref | None:
    try:
        request = SOURCE_SETS[source_set].build_request(day, register)
    except ValueError:
        return None  # the set does not serve this era
    if not l0.exists(request.fetch_source, day, request.filename):
        return None
    return l0.ref_for(request.fetch_source, day, request.filename)


def build_session(
    day: date,
    *,
    l0: L0Store,
    register: SourceRegister,
    scrip_index: Mapping[str, str],
    counts: Counter[str] | None = None,
) -> tuple[SessionAttribute, ...]:
    """Every attribute row one session's L0 payloads yield, ordered on the partition key."""
    tally = counts if counts is not None else Counter()
    rows: dict[tuple[str, str, str, str], SessionAttribute] = {}

    def put(attr: SessionAttribute) -> None:
        old = rows.get(attr.key())
        if old is None:
            rows[attr.key()] = attr
            return
        rows[attr.key()] = SessionAttribute(
            isin=old.isin,
            exchange=old.exchange,
            symbol=old.symbol,
            series=old.series,
            trade_date=old.trade_date,
            vwap=old.vwap if old.vwap is not None else attr.vwap,
            board_lot=old.board_lot if old.board_lot is not None else attr.board_lot,
            instrument_name=old.instrument_name or attr.instrument_name,
            ex_marker=old.ex_marker or attr.ex_marker,
        )

    # NSE: the session's bhavcopy states each (symbol, series)'s ISIN; UDiFF also carries lot/name.
    nse_ref = _ref(l0, NSE_BHAVCOPY, day, register)
    stated: dict[tuple[str, str], set[str]] = {}
    if nse_ref is not None:
        for price in bhavcopy.parse_l0_report(l0, nse_ref).rows:
            stated.setdefault((price.symbol, price.series), set()).add(price.isin)
        if nse_ref.source == nse_udiff.UDIFF_SOURCE_ID:
            for udiff in parse_udiff_attributes(
                l0.get(nse_ref), filename=nse_ref.filename, columns=nse_udiff.UDIFF_COLUMNS
            ):
                tally["nse_lot_name"] += 1
                put(_from_udiff(udiff, Exchange.NSE))
    deliv_ref = _ref(l0, NSE_DELIVERY, day, register)
    if deliv_ref is not None and deliv_ref.source == delivery.DELIVERY_SOURCE_ID:
        for vwap in delivery.parse_vwap_l0(l0, deliv_ref):
            if vwap.avg_price is None:
                tally["vwap_absent"] += 1
                continue
            isins = stated.get((vwap.symbol, vwap.series), set())
            if len(isins) != 1:
                tally["vwap_unplaced"] += 1
                continue
            tally["vwap"] += 1
            put(
                SessionAttribute(
                    isin=next(iter(isins)),
                    exchange=Exchange.NSE.value,
                    symbol=vwap.symbol,
                    series=vwap.series,
                    trade_date=day,
                    vwap=vwap.avg_price,
                )
            )

    # BSE: UDiFF lot/name, or the legacy ex-marker through the scrip master.
    bse_ref = _ref(l0, BSE_BHAVCOPY, day, register)
    if bse_ref is not None:
        for udiff in parse_udiff_attributes(
            l0.get(bse_ref), filename=bse_ref.filename, columns=bse_bhavcopy.UDIFF_COLUMNS
        ):
            tally["bse_lot_name"] += 1
            put(_from_udiff(udiff, Exchange.BSE))
    legacy_ref = _ref(l0, BSE_BHAVCOPY_LEGACY, day, register)
    if legacy_ref is not None:
        parsed = bse_bhavcopy.parse_legacy_report(
            l0.get(legacy_ref), filename=legacy_ref.filename, trade_date=day
        )
        for quote in parsed.quotes:
            if not quote.close_indicator:
                continue
            isin = scrip_index.get(quote.scrip_code)
            if isin is None:
                tally["marker_unresolved"] += 1
                continue
            tally["marker"] += 1
            put(
                SessionAttribute(
                    isin=isin,
                    exchange=Exchange.BSE.value,
                    symbol=quote.scrip_name,
                    series=quote.group,
                    trade_date=day,
                    ex_marker=quote.close_indicator,
                )
            )
    return tuple(rows[key] for key in sorted(rows))


def _from_udiff(udiff: UdiffAttributes, exchange: Exchange) -> SessionAttribute:
    return SessionAttribute(
        isin=udiff.isin,
        exchange=exchange.value,
        symbol=udiff.symbol,
        series=udiff.series,
        trade_date=udiff.trade_date,
        board_lot=udiff.board_lot,
        instrument_name=udiff.instrument_name,
    )


def write_session_attributes(
    rows: Sequence[SessionAttribute], *, trade_date: date, data_root: Path | None = None
) -> Path | None:
    """Write one session's attribute partition whole (byte-deterministic); remove it when empty."""
    path = partition_path(Layer.L1, SESSION_ATTRIBUTES_DATASET, trade_date, data_root=data_root)
    if not rows:
        if path.is_file():
            path.unlink()
        return None
    if any(row.trade_date != trade_date for row in rows):
        raise ValueError(f"rows outside session {trade_date.isoformat()}")
    records = [
        {
            "isin": row.isin,
            "exchange": row.exchange,
            "symbol": row.symbol,
            "series": row.series,
            "trade_date": row.trade_date,
            "vwap": row.vwap.quantize(_PRICE_Q, rounding=ROUND_HALF_UP)
            if row.vwap is not None
            else None,
            "board_lot": row.board_lot,
            "instrument_name": row.instrument_name,
            "ex_marker": row.ex_marker,
        }
        for row in sorted(rows, key=SessionAttribute.key)
    ]
    table = pa.Table.from_pylist(records, schema=SESSION_ATTRIBUTES_SCHEMA)
    assert_raw_only(table.schema)
    enforce_schema(table, SESSION_ATTRIBUTES_SCHEMA, dataset=SESSION_ATTRIBUTES_DATASET)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".partial")
    pq.write_table(table, staging, compression="zstd")
    staging.replace(path)
    return path


def read_session_attributes(
    start: date, end: date, *, data_root: Path | None = None
) -> Iterable[dict[str, object]]:
    """Every attribute row in `[start, end]`, partition by partition, as plain records."""
    base = layer_root(Layer.L1, data_root=data_root) / SESSION_ATTRIBUTES_DATASET
    if not base.exists():
        return
    for part in sorted(base.glob("date=*/part.parquet")):
        day = date.fromisoformat(part.parent.name.removeprefix("date="))
        if start <= day <= end:
            yield from pq.read_table(part, schema=SESSION_ATTRIBUTES_SCHEMA).to_pylist()


def _sessions(l0: L0Store, start: date, end: date) -> list[date]:
    """Every date in range with any of the four source payloads in L0."""
    days: set[date] = set()
    for source in (
        nse_udiff.UDIFF_SOURCE_ID,
        "nse_bhavcopy_legacy",
        delivery.DELIVERY_SOURCE_ID,
        bse_bhavcopy.UDIFF_SOURCE_ID,
        bse_bhavcopy.LEGACY_SOURCE_ID,
    ):
        days.update(ref.logical_date for ref in l0.iter_refs(source, start=start, end=end))
    return sorted(days)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Offline: L0 and the identity master (read-only), never the network."""
    parser = argparse.ArgumentParser(prog="session_attributes", description=__doc__)
    parser.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    parser.add_argument("--data-root", type=Path, default=None, help="lake whose L1 is written")
    parser.add_argument("--l0-root", type=Path, default=None, help="lake whose L0 is read")
    args = parser.parse_args(argv)

    settings = get_settings()
    clock: Clock = SystemClock()
    l0 = L0Store(clock=clock, data_root=args.l0_root or settings.data_root)
    data_root = args.data_root or settings.data_root
    register = load_register()
    with connection(settings) as conn:
        master = IdentityStore(conn, clock=clock).load_master()
        conn.rollback()
    scrip_index = build_scrip_index(master, Exchange.BSE)

    report = SessionAttributesReport()
    for day in _sessions(l0, args.start, args.end):
        try:
            rows = build_session(
                day, l0=l0, register=register, scrip_index=scrip_index, counts=report.counts
            )
            write_session_attributes(rows, trade_date=day, data_root=data_root)
        except Exception as exc:  # one bad payload must not end a ten-year walk
            report.failures.append((day, f"{type(exc).__name__}: {exc}"))
            _LOG.error("session_attributes.failed", date=day.isoformat(), error=str(exc))
            continue
        report.sessions += 1
        report.written += len(rows)
        _LOG.info("session_attributes.written", date=day.isoformat(), rows=len(rows))
    print(
        f"session_attributes: {report.sessions} sessions, {report.written} rows, "
        f"{len(report.failures)} failed; {dict(report.counts)}"
    )
    for day, why in report.failures[:10]:
        print(f"  failed {day.isoformat()}: {why}", file=sys.stderr)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
