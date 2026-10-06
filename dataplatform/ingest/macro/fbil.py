"""FBIL's daily INR reference rates → `macro_series`, dated by the benchmark's own publication.

Financial Benchmarks India Ltd administers the official INR reference rates (USD, GBP, EUR, JPY,
AED, IDR; RUB later) and took the benchmark over from the RBI on 2018-07-10. Its public site is an
Angular application over a keyless JSON API at `https://www.fbil.org.in/wasdm/`; the reference-rate
archive is `refrates/fetchfiltered?fromDate=YYYY-MM-DD&toDate=YYYY-MM-DD&authenticated=false`, the
same call the site's own "Reference Rate" card makes for an anonymous visitor. No login: the page
loads reCAPTCHA, but the anonymous data call carries no token and needs none.

**Point-in-time.** Each row carries `processRunDate` and `displayTime` — the session and the
instant FBIL published the rate (13:30 IST on the 2018 rows, 13:00 on the 2026 ones). An
administered benchmark is fixed once and never revised, so `period_end = release_date = the
publication date` and `revision_seq` is 0 forever: Grade A, backfillable over the whole dated
archive. The free website shows a rate a few sessions after publication (a 2026-10-06 request
returned 2026-09-29 as the newest), but the benchmark itself was public at `displayTime` — the
RBI's home page carries the same day's figure "as at 1.00pm" (probe of 2026-10-06) — so the lag
delays our *capture*, not the rate's knowable date. The daily job asks for a trailing window wide
enough to cover that lag.

Rows are quoted as rupees per a stated quantity of foreign currency (`INR / 100 JPY`), and that
quantity is carried into the `series_id` (`INR_PER_100_JPY`) rather than divided out: the published
number is what is stored.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from dataplatform.ingest.macro.models import (
    Frequency,
    MacroFact,
    MacroRelease,
    Unit,
    series_id,
    store_value,
)
from dataplatform.ingest.models import ParseError

__all__ = [
    "FBIL_ARCHIVE_EPOCH",
    "FBIL_SOURCE_ID",
    "fbil_filename",
    "fbil_series_id",
    "fbil_url",
    "parse_fbil_reference_rates",
]

#: The register id whose bytes this parser reads.
FBIL_SOURCE_ID: Final = "fbil_reference_rates"

#: FBIL's first published reference rate (it took the benchmark over from the RBI this day);
#: measured — a 2018-07-09..13 window returns rows from 2018-07-10 only.
FBIL_ARCHIVE_EPOCH: Final = date(2018, 7, 10)

_PAIR: Final = re.compile(r"^INR\s*/\s*(\d+)\s+([A-Z]{3})$")
_STAMP: Final = "%Y-%m-%d %H:%M:%S"


def fbil_url(start: date, end: date) -> str:
    """The archive window URL, inclusive on both ends, exactly as the site's own card builds it."""
    return (
        "https://www.fbil.org.in/wasdm/refrates/fetchfiltered"
        f"?fromDate={start.isoformat()}&toDate={end.isoformat()}&authenticated=false"
    )


def fbil_filename(start: date, end: date) -> str:
    """L0 filename for one window: both ends, so overlapping windows never share a key."""
    return f"refrates_{start:%Y%m%d}_{end:%Y%m%d}.json"


def fbil_series_id(pair: str) -> str:
    """`INR / 1 USD` → `IN.FBIL.INR_PER_USD.REFERENCE`; `INR / 100 JPY` → `…INR_PER_100_JPY…`."""
    match = _PAIR.match(pair.strip())
    if match is None:
        raise ValueError(f"unrecognised FBIL currency pair {pair!r}")
    quantity, currency = match.groups()
    subject = f"INR_PER_{currency}" if quantity == "1" else f"INR_PER_{quantity}_{currency}"
    return series_id("IN", "FBIL", subject, "REFERENCE")


def parse_fbil_reference_rates(
    payload: bytes, *, filename: str, l0_key: str | None = None
) -> tuple[MacroRelease, ...]:
    """Parse one archive window into one release per publication date, oldest first.

    What it does: read every `{processRunDate, subProdName, displayTime, rate}` row, key it by the
    pair's `series_id`, and group the facts by the date FBIL published them.
    What it assumes: the body is the bare JSON array the API returns; `rate` is a JSON number.
    What it never does: date a rate by the fetch, invert or rescale a quote, or keep one of two
    different rates published for the same pair on the same day — that is refused.

    An empty array is a valid answer (a window of holidays, or one before the epoch) and returns an
    empty tuple; the caller decides whether that was expected. Raises `ParseError` for a non-JSON or
    non-array body, a row missing a field, an unknown pair, a `displayTime` on a different day from
    its `processRunDate`, a null rate, or a same-day conflict.
    """
    try:
        rows = json.loads(payload.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ParseError(f"not a JSON body: {error}", filename=filename) from error
    if not isinstance(rows, list):
        raise ParseError(f"expected a JSON array, got {type(rows).__name__}", filename=filename)

    by_day: dict[date, dict[str, MacroFact]] = defaultdict(dict)
    for position, row in enumerate(rows, start=1):
        fact = _fact(row, position=position, filename=filename, l0_key=l0_key)
        prior = by_day[fact.release_date].get(fact.series_id)
        if prior is not None and prior.value != fact.value:
            raise ParseError(
                f"row {position}: {fact.series_id} on {fact.release_date} published twice "
                f"({prior.value} and {fact.value})",
                filename=filename,
            )
        by_day[fact.release_date][fact.series_id] = fact
    return tuple(
        MacroRelease(
            release_date=day,
            source=FBIL_SOURCE_ID,
            facts=tuple(facts[key] for key in sorted(facts)),
            l0_key=l0_key,
        )
        for day, facts in sorted(by_day.items())
    )


def _fact(row: Any, *, position: int, filename: str, l0_key: str | None) -> MacroFact:
    if not isinstance(row, dict):
        raise ParseError(f"row {position} is not an object", filename=filename)
    try:
        session = datetime.strptime(str(row["processRunDate"]), _STAMP).date()
        published = datetime.strptime(str(row["displayTime"]), _STAMP).date()
        sid = fbil_series_id(str(row["subProdName"]))
        raw = row["rate"]
        if raw is None:
            raise ValueError("rate is null")
        value = store_value(str(raw))
    except (KeyError, ValueError, InvalidOperation, ArithmeticError) as error:
        raise ParseError(f"row {position}: {error}", filename=filename) from error
    if published != session:
        raise ParseError(
            f"row {position}: displayTime {published} is not the processRunDate {session}",
            filename=filename,
        )
    return MacroFact(
        series_id=sid,
        period_start=session,
        period_end=session,
        release_date=session,
        frequency=Frequency.DAILY,
        unit=Unit.INR,
        value=value,
        source=FBIL_SOURCE_ID,
        l0_key=l0_key,
    )
