"""Three shapes the pre-2016 BSE legacy archive publishes that the parser used to fail a session on.

Found by the 2026-10-06 deep backfill (2006-2016, `ops/gates/bse-deep-backfill-2026-10-06.md`); each
test pins the lines verbatim from the L0 payload and fails on the pre-fix parser:

1. A blank `PREVCLOSE` — an instrument's first session (ten sessions in January 2012). The line is
   quarantined with `prev_close_absent`; the rest of the session lands.
2. A blank `SC_NAME` (531364 on 2011-05-05, 526225 on 2013-10-31). The scrip code stands in for the
   symbol label; ISIN, the key, is untouched.
3. A second member in the archive beside `EQ{DDMMYY}.CSV` (a `.dbf`, a nested zip, an archiver's
   `.url`). The named member is read; an archive with no such member is still refused.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
import pytest

from dataplatform.clock import FrozenClock
from dataplatform.identity.master import IdentityMaster, ListingStatus
from dataplatform.ingest import backfill, source_register
from dataplatform.ingest.bse import bhavcopy, scrip_master
from dataplatform.ingest.bse.scrip_master import BseScrip
from dataplatform.ingest.models import ParseError
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import read_prices_raw
from dataplatform.store.paths import Layer, partition_path
from dataplatform.store.schemas import PRICES_RAW_QUARANTINE_DATASET, PriceQuarantineReason

HEADER: Final = ",".join(bhavcopy.LEGACY_COLUMNS)
SESSION: Final = date(2012, 1, 6)
FILENAME: Final = "EQ060112_CSV.ZIP"

#: Verbatim from EQ060112_CSV.ZIP: an ordinary row, and line 1090 (a new IDFC bond, no PREVCLOSE).
ORDINARY: Final = (
    "500002,ABB LTD.    ,A ,Q,700.00,710.00,695.00,705.00,705.00,701.00,10,100,70500.00,"
)
FIRST_SESSION: Final = (
    "961720,IDFCBD1SR2  ,F ,B,6000.00,6000.00,6000.00,6000.00,6000.00,,1,2,12000.00,"
)
#: Verbatim from EQ050511_CSV.ZIP: SC_NAME is empty.
NAMELESS: Final = "531364,,T ,Q,23.25,25.30,23.25,25.30,25.30,24.10,24,8432,211575.00,"


def _csv(*rows: str) -> str:
    return "\r\n".join([HEADER, *rows]) + "\r\n"


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def test_a_blank_prev_close_is_quarantined_not_fatal() -> None:
    parsed = bhavcopy.parse_legacy_text_report(
        _csv(ORDINARY, FIRST_SESSION), filename=FILENAME, trade_date=SESSION
    )

    assert [quote.scrip_code for quote in parsed.quotes] == ["500002"]
    (line,) = parsed.quarantined
    assert (line.scrip_code, line.group, line.line) == ("961720", "F", 3)
    assert line.reason == PriceQuarantineReason.PREV_CLOSE_ABSENT


def test_a_blank_sc_name_resolves_under_its_scrip_code() -> None:
    (quote,) = bhavcopy.parse_legacy_text(
        _csv(NAMELESS), filename="EQ050511_CSV.ZIP", trade_date=date(2011, 5, 5)
    )
    resolution = bhavcopy.resolve_legacy([quote], {"531364": "INE000A01016"})

    (row,) = resolution.resolved
    assert (row.isin, row.symbol, row.close) == ("INE000A01016", "531364", Decimal("25.30"))


@pytest.mark.parametrize(
    "stray",
    ["BD021111.dbf", "eq060611_csv.zip", "Archive created by free jZip.url"],
)
def test_the_named_member_is_read_when_an_archive_carries_a_stray(stray: str) -> None:
    payload = _zip({"EQ060112.CSV": _csv(ORDINARY).encode(), stray: b"not a bhavcopy"})

    parsed = bhavcopy.parse_legacy_report(payload, filename=FILENAME, trade_date=SESSION)

    assert [quote.scrip_code for quote in parsed.quotes] == ["500002"]


def test_an_archive_without_its_named_member_is_still_refused() -> None:
    payload = _zip({"OTHER.CSV": _csv(ORDINARY).encode(), "X.dbf": b"x"})

    with pytest.raises(ParseError, match="exactly one member"):
        bhavcopy.parse_legacy_report(payload, filename=FILENAME, trade_date=SESSION)


def _master(codes: tuple[str, ...]) -> IdentityMaster:
    scrips = [
        BseScrip(
            scrip_code=code,
            symbol=f"S{code}",
            name=f"SCRIP {code}",
            isin=isin,
            status=ListingStatus.ACTIVE,
            group="A",
            face_value_inr=Decimal(10),
        )
        for code, isin in zip(codes, ("INE117A01022", "INE002A01018"), strict=False)
    ]
    derived = scrip_master.derive_master(scrips, snapshot_date=SESSION)
    return IdentityMaster(derived.windows, securities=derived.securities, listings=derived.listings)


def test_the_session_lands_with_each_refusal_under_its_own_reason(tmp_path: Path) -> None:
    """One write owns the partition, so both reasons must reach it in that write."""
    unknown = "532999,UNKNOWN CO  ,B ,Q,10.00,10.00,10.00,10.00,10.00,10.00,1,1,10.00,"
    l0 = L0Store(clock=FrozenClock(SESSION), data_root=tmp_path)
    ref = l0.put(
        bhavcopy.LEGACY_SOURCE_ID,
        SESSION,
        FILENAME,
        _zip({"EQ060112.CSV": _csv(ORDINARY, FIRST_SESSION, unknown).encode()}),
    )
    source_set = backfill.SOURCE_SETS[backfill.BSE_BHAVCOPY_LEGACY]
    ctx = backfill.WriteContext(
        l0=l0, data_root=tmp_path, master=_master(("500002",)), register=source_register.load()
    )
    source_set.write(source_set.parse(l0, ref), ctx)

    assert [row["isin"] for row in read_prices_raw(SESSION, data_root=tmp_path)] == ["INE117A01022"]
    quarantine = partition_path(
        Layer.L1, PRICES_RAW_QUARANTINE_DATASET, SESSION, data_root=tmp_path
    )
    records = {(r["symbol"], r["reason"]) for r in pq.read_table(quarantine).to_pylist()}
    assert records == {
        ("961720", PriceQuarantineReason.PREV_CLOSE_ABSENT),
        ("532999", PriceQuarantineReason.SCRIP_UNRESOLVED),
    }
