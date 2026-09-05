"""The MTO file: NSE's older per-security delivery report, and the only route to pre-2019 delivery.

`sec_bhavdata_full` (M1.6) is the modern delivery file, and the archive serves it only from
2019-09-30. Before that date the same facts live in `MTO_DDMMYYYY.DAT` — "Security Wise Delivery
Position - Compulsory Rolling Settlement" — which the archive serves back past the start of this
platform's price history. Without it, 755 of 2,461 price sessions would carry no delivery figure at
all, which is 31% of the history and skewed entirely to the oldest end, where a long backtest needs
it most.

**The two sources agree exactly.** On 2024-06-20, a session both serve, all 2,339 `(symbol, series)`
keys present in both report an identical delivery quantity — 100.00%, not "close" — and MTO carries
179 keys `sec_bhavdata_full` leaves blank while lacking none that it fills. So MTO is not a
degraded fallback for the old era; it is at least as complete, and the eras can be joined without a
seam in the data. `tests/unit/test_mto.py` holds that comparison against both real files so it stays
true rather than remaining a claim in a docstring.

Format, which is fixed-shape rather than a CSV with a header row::

    Security Wise Delivery Position - Compulsory Rolling Settlement
    10,MTO,15032017,510347036,0001673
    Trade Date <15-MAR-2017>,Settlement Type <N>,Settlement No <...>,Settlement Date <...>
    Record Type,Sr No,Name of Security,Quantity Traded,Deliverable Quantity...,% of Deliverable...
    20,1,20MICRONS,EQ,44483,29791,66.97

Record type `10` is the header (date and totals), `20` is a security. Note the fourth column header
says "Name of Security" but a data row spends *two* fields on it — symbol then series — so a `20`
record carries seven fields against the header's six. Splitting on the header count would silently
shear the series off every row.

What this module never does: infer a missing figure, treat the file's own totals as advisory, or
accept a record whose stated trade date disagrees with the date the caller asked for. A delivery
number attached to the wrong session is a look-ahead leak in a backtest, not a cosmetic error.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.delivery import DeliveryRow
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "MTO_SOURCE_ID",
    "parse",
    "parse_l0",
]

_LOG = get_logger(__name__)

#: The Source Register id this parser reads. Distinct from `nse_sec_bhavdata_full`: a different URL,
#: a different format and a different era, so it gets its own row and its own verification evidence.
MTO_SOURCE_ID: Final = "nse_mto"

#: Record type markers in column 0.
_HEADER_RECORD: Final = "10"
_SECURITY_RECORD: Final = "20"

#: Fields in a `20` record: type, serial, symbol, series, traded qty, delivered qty, delivered %.
_SECURITY_FIELDS: Final = 7

_TRADE_DATE: Final = re.compile(r"Trade Date\s*<(\d{2})-([A-Za-z]{3})-(\d{4})>")
_MONTHS: Final = {
    m: i
    for i, m in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}


def parse(
    payload: bytes, *, filename: str, trade_date: date | None = None
) -> tuple[DeliveryRow, ...]:
    """Parse one MTO file into `DeliveryRow`s — the same shape `sec_bhavdata_full` yields.

    Deliberately returns the *same* model as the modern file so everything downstream — the
    identity resolution, the L1 join, the reconciliation contract — is one code path with one set
    of tests, rather than two that could drift at the era boundary.

    `trade_date`, when given, is checked against the date the file states about itself and a
    mismatch is a `ParseError`. The archive is addressed by date in the URL, so this is the check
    that a wrong or stale file cannot quietly become another session's delivery figures.

    Raises `ParseError` for anything that is not this format: an unreadable body, no header record,
    an unparseable trade date, a security record with the wrong field count, a non-numeric
    quantity, or a duplicate `(symbol, series)`.
    """
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = payload.decode("latin-1")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ParseError("file is empty", filename=filename)

    stated = _stated_date(lines, filename=filename)
    if trade_date is not None and stated != trade_date:
        raise ParseError(
            f"file states trade date {stated.isoformat()} but was fetched as "
            f"{trade_date.isoformat()}; a delivery figure on the wrong session is a look-ahead "
            "leak, not a cosmetic mismatch",
            filename=filename,
        )

    rows: list[DeliveryRow] = []
    seen: set[tuple[str, str]] = set()
    for number, line in enumerate(lines, start=1):
        fields = [field.strip() for field in line.split(",")]
        if fields[0] != _SECURITY_RECORD:
            continue
        if len(fields) != _SECURITY_FIELDS:
            raise ParseError(
                f"line {number}: a security record has {len(fields)} fields, expected "
                f"{_SECURITY_FIELDS} (type, serial, symbol, series, traded, delivered, percent)",
                filename=filename,
            )
        _, _, symbol, series, _traded, delivered, percent = fields
        if not symbol or not series:
            raise ParseError(
                f"line {number}: security record has no symbol/series", filename=filename
            )
        key = (symbol, series)
        if key in seen:
            raise ParseError(
                f"line {number}: {symbol}/{series} appears twice; the file's key is "
                "(symbol, series) and two delivery figures for one key cannot both be right",
                filename=filename,
            )
        seen.add(key)
        rows.append(
            DeliveryRow(
                symbol=symbol,
                series=series,
                trade_date=stated,
                deliv_qty=_integer(
                    delivered, column="deliverable quantity", line=number, filename=filename
                ),
                deliv_pct=_decimal(
                    percent, column="percent delivered", line=number, filename=filename
                ),
            )
        )

    if not rows:
        raise ParseError(
            "no security records; the file has a header but no delivery positions",
            filename=filename,
        )
    _LOG.info(
        "mto.parsed",
        source=MTO_SOURCE_ID,
        filename=filename,
        trade_date=stated.isoformat(),
        rows=len(rows),
        state="VALIDATED",
    )
    return tuple(rows)


def parse_l0(store: L0Store, ref: L0Ref) -> tuple[DeliveryRow, ...]:
    """Parse the stored payload, re-verifying its checksum on the way in (`L0Store.get`)."""
    return parse(store.get(ref), filename=ref.filename, trade_date=ref.logical_date)


def _stated_date(lines: list[str], *, filename: str) -> date:
    """The session the file says it is about, from its own header — never from the filename.

    Two places state it: the `10` record's third field (`DDMMYYYY`) and the `Trade Date <...>` line.
    The `10` record is preferred because it is machine-shaped; the prose line is the fallback.
    """
    for line in lines[:6]:
        fields = [field.strip() for field in line.split(",")]
        if fields[0] == _HEADER_RECORD and len(fields) >= 3 and len(fields[2]) == 8:
            stamp = fields[2]
            try:
                return date(int(stamp[4:8]), int(stamp[2:4]), int(stamp[0:2]))
            except ValueError as exc:
                raise ParseError(
                    f"header record has an unreadable date {stamp!r}", filename=filename
                ) from exc
    for line in lines[:6]:
        found = _TRADE_DATE.search(line)
        if found:
            day, mon, year = found.groups()
            month = _MONTHS.get(mon.upper())
            if month is None:
                raise ParseError(f"unknown month {mon!r} in the trade-date line", filename=filename)
            return date(int(year), month, int(day))
    raise ParseError(
        "no header record and no 'Trade Date <...>' line; the file does not state its session",
        filename=filename,
    )


def _integer(value: str, *, column: str, line: int, filename: str) -> int | None:
    """A count, or None when the file wrote a dash — never a `-` silently becoming a zero."""
    if value in ("-", ""):
        return None
    if not value.isdigit():
        raise ParseError(
            f"line {line}: {column} {value!r} is not a whole number", filename=filename
        )
    return int(value)


def _decimal(value: str, *, column: str, line: int, filename: str) -> Decimal | None:
    """A percentage as an exact `Decimal` — never a float (CLAUDE.md)."""
    if value in ("-", ""):
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ParseError(
            f"line {line}: {column} {value!r} is not a decimal", filename=filename
        ) from exc
