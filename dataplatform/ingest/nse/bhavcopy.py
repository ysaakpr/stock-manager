"""Dual-parser dispatch for the NSE cash-market bhavcopy across the UDiFF cutover (§4.1, row 2,
"different column schema — dual parser required").

The exchange published the cash bhavcopy in one format until 5 July 2024 and in the UDiFF format
from 8 July 2024 (the first trading day after) — a hard cutover with no overlap: the legacy
`cm{DD}{MON}{YYYY}bhav.csv.zip` URL 404s from 08-Jul-2024 on, and the UDiFF
`BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` URL begins on exactly that date. So a single date
selects exactly one parser, and this module is that selection — the one place that knows the cutover
so no caller has to.

`source_register.yaml` writes the boundary as two touching half-open ranges (`nse_bhavcopy_legacy`
`era.end: 2024-07-08`, `nse_bhavcopy_udiff` `era.start: 2024-07-08`) and the legacy module
(M1.4) deliberately left "which parser owns the boundary date itself" to this dispatcher. It is
resolved here as **UDiFF owns 08-Jul-2024**: that is the day the UDiFF file first exists and the day
the legacy file first does not, verified against the archive when this task froze its fixtures.

Both parsers emit the identical `PriceRow` schema, so what comes out of this dispatcher never tells
a caller which era — or which parser — produced it. That is the invariant the dual parser exists to
protect: one schema, so nothing downstream of D1 branches on era and later reads a decade of one
format and a year of the other without noticing.
"""

from __future__ import annotations

from datetime import date
from typing import Final, Literal

from dataplatform.ingest.models import BhavcopyParse, ParseError, PriceRow
from dataplatform.ingest.nse import bhavcopy_legacy, bhavcopy_udiff
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "CUTOVER",
    "Era",
    "era_of",
    "parse",
    "parse_l0",
]

Era = Literal["legacy", "udiff"]

#: The first UDiFF session. On this date the UDiFF file first exists and the legacy URL first 404s,
#: so it is the boundary itself and it belongs to UDiFF. Equal to `bhavcopy_udiff.UDIFF_ERA_START`
#: and to `bhavcopy_legacy.LEGACY_ERA_END`; asserted below so the three can never drift apart.
CUTOVER: Final = date(2024, 7, 8)

assert CUTOVER == bhavcopy_udiff.UDIFF_ERA_START, "UDiFF era start must equal the cutover"
assert CUTOVER == bhavcopy_legacy.LEGACY_ERA_END, "legacy era end must equal the cutover"


def era_of(trade_date: date) -> Era:
    """Which format the exchange published for `trade_date` — the whole dispatch decision.

    `< CUTOVER` is the legacy `cm…bhav.csv.zip`; `>= CUTOVER` is UDiFF. The boundary date itself is
    UDiFF, because that is the day the UDiFF file first exists and the legacy one first does not.
    """
    return "legacy" if trade_date < CUTOVER else "udiff"


def parse(payload: bytes, *, filename: str, trade_date: date) -> tuple[PriceRow, ...]:
    """Parse one cash bhavcopy with the parser its session's format requires.

    `trade_date` is what selects the parser — the session the file is *for*, which the caller knows
    from the fetch (it is `L0Ref.logical_date` on the real path). The parsed rows carry their own
    date too, and this checks the two agree: a file whose contents disagree with the date it was
    filed under is a mis-served or misrouted payload, and reading it under the wrong date would
    scatter a session across the wrong L1 partition (§4.2). That cross-check is the guard that makes
    "dispatch by date" safe rather than merely convenient.

    Raises `ParseError` (from the chosen parser, or here for the date mismatch) — the two parsers
    never disagree on the row schema, so the caller handles one error type and one row type.
    """
    return parse_report(payload, filename=filename, trade_date=trade_date).rows


def parse_report(payload: bytes, *, filename: str, trade_date: date) -> BhavcopyParse:
    """`parse`, keeping the rows the exchange published without an ISIN rather than discarding them.

    The two eras differ in what they can refuse: only the legacy file has ever carried an ISIN
    placeholder, so the UDiFF branch always reports an empty `refused` — stated here rather than
    left to be inferred, because "no refusals" and "this parser cannot report refusals" are
    different claims. The L1 writer quarantines what comes back (M1.8's "nothing is dropped
    silently"); `parse` is the caller that has nothing to quarantine into.
    """
    if era_of(trade_date) == "legacy":
        parsed = bhavcopy_legacy.parse_report(payload, filename=filename)
    else:
        parsed = BhavcopyParse(rows=bhavcopy_udiff.parse(payload, filename=filename))

    if parsed.rows[0].trade_date != trade_date:
        raise ParseError(
            f"file was dispatched as the {trade_date.isoformat()} session but its rows are dated "
            f"{parsed.rows[0].trade_date.isoformat()}; the payload does not match the date it was "
            "filed under",
            filename=filename,
        )
    return parsed


def parse_l0(store: L0Store, ref: L0Ref) -> tuple[PriceRow, ...]:
    """Parse an L0 payload, choosing the parser from the ref's logical date and re-checksumming.

    The pipeline entry point that does not need the caller to know the era: `L0Ref.logical_date` is
    the session the fetch was for, which is exactly what `parse` dispatches on. `L0Store.get`
    re-hashes the payload on the way out, so no row is derived from bytes that changed under L0.
    """
    return parse(store.get(ref), filename=ref.filename, trade_date=ref.logical_date)


def parse_l0_report(store: L0Store, ref: L0Ref) -> BhavcopyParse:
    """`parse_l0`, keeping the refused rows for the caller that can quarantine them."""
    return parse_report(store.get(ref), filename=ref.filename, trade_date=ref.logical_date)
