"""India VIX spot history from NSE Indices' historical-data endpoint → daily OHLC series.

The F&O bhavcopy's `FUTIVX` is a *future* on India VIX and only reaches back to the UDiFF era
(2024-07-08). The spot index itself is served by the same `niftyindices.com` endpoint the site's
"Historical Data" page uses for every index, `POST /BackPage/getHistoricaldatatabletoString`, with
the `cinfo` body `tri_request_body` already builds. Probed 2026-10-06: `INDIA VIX` answers with
`{INDEX_NAME, HistoricalDate, OPEN, HIGH, LOW, CLOSE}` rows; a window in 2020-03 returns rows, one
in 2014-10 returns `[]`, so this endpoint's depth starts somewhere between those — deeper history is
in `ind_close_all` (the M11.2 campaign), whose "India VIX" row first appears between 2013-10-01 and
2014-10-01.

**Same series as the close-all snapshot.** The close lands as `IN.NSE.INDIA_VIX.CLOSE`, the id
`index_valuation` gives the "India VIX" row of `ind_close_all`: one index, one series, whichever
NSE file it was read from. If the two ever disagree on a session, `macro_series.write_release`
refuses the second write — two NSE files stating different closes for one session is a finding for
a human, not a tie to break silently.

Point-in-time: a session's level is disseminated at its close, so `period_end = release_date = the
session` and `revision_seq` is 0 (Grade A).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from decimal import InvalidOperation
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
    "INDIA_VIX_REQUEST_NAME",
    "INDIA_VIX_SOURCE_ID",
    "INDIA_VIX_URL",
    "india_vix_filename",
    "parse_india_vix_history",
]

#: The register id whose bytes this parser reads.
INDIA_VIX_SOURCE_ID: Final = "nifty_india_vix_history"

#: The name the endpoint wants, in CAPS (it echoes back "India VIX" or "INDIA VIX").
INDIA_VIX_REQUEST_NAME: Final = "INDIA VIX"

#: The endpoint the site's "Historical Data" page posts to; index and window travel in the body.
INDIA_VIX_URL: Final = "https://niftyindices.com/BackPage/getHistoricaldatatabletoString"

_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("OPEN", "OPEN"),
    ("HIGH", "HIGH"),
    ("LOW", "LOW"),
    ("CLOSE", "CLOSE"),
)


def india_vix_filename(start: date, end: date) -> str:
    """L0 filename for one window."""
    return f"india_vix_{start:%Y%m%d}_{end:%Y%m%d}.json"


def parse_india_vix_history(
    payload: bytes, *, filename: str, l0_key: str | None = None
) -> tuple[MacroRelease, ...]:
    """Parse one window into one release per session, oldest first.

    What it does: read each row's `HistoricalDate` (`DD Mon YYYY`) and its OPEN/HIGH/LOW/CLOSE
    strings into `IN.NSE.INDIA_VIX.<measure>` facts dated to the session.
    What it never does: accept a row for another index, read `-` or a blank as zero, or keep two
    rows for one session.

    `[]` (a window before the endpoint's depth) returns an empty tuple. Raises `ParseError` for a
    non-JSON body, an HTML block page wearing a 200 (this endpoint answers text/html even on
    success, so only the shape can tell), a row for another index, or a malformed date or number.
    """
    try:
        rows = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ParseError(f"not a JSON body: {error}", filename=filename) from error
    if not isinstance(rows, list):
        raise ParseError(f"expected a JSON array, got {type(rows).__name__}", filename=filename)

    sessions: dict[date, list[MacroFact]] = defaultdict(list)
    for position, row in enumerate(rows, start=1):
        session, facts = _row(row, position=position, filename=filename, l0_key=l0_key)
        if session in sessions:
            raise ParseError(f"row {position}: session {session} appears twice", filename=filename)
        sessions[session] = facts
    return tuple(
        MacroRelease(
            release_date=session,
            source=INDIA_VIX_SOURCE_ID,
            facts=tuple(facts),
            l0_key=l0_key,
        )
        for session, facts in sorted(sessions.items())
        if facts
    )


def _row(
    row: Any, *, position: int, filename: str, l0_key: str | None
) -> tuple[date, list[MacroFact]]:
    if not isinstance(row, dict):
        raise ParseError(f"row {position} is not an object", filename=filename)
    name = str(row.get("INDEX_NAME") or "").strip()
    if name.upper() != INDIA_VIX_REQUEST_NAME:
        raise ParseError(f"row {position} is for {name!r}, not India VIX", filename=filename)
    try:
        session = datetime.strptime(str(row["HistoricalDate"]).strip(), "%d %b %Y").date()
    except (KeyError, ValueError) as error:
        raise ParseError(
            f"row {position}: bad HistoricalDate: {error}", filename=filename
        ) from error
    facts: list[MacroFact] = []
    for column, measure in _COLUMNS:
        text = str(row.get(column) or "").strip()
        if text in {"", "-"}:
            continue
        try:
            value = store_value(text)
        except (InvalidOperation, ArithmeticError) as error:
            raise ParseError(f"row {position}: {column}={text!r}", filename=filename) from error
        facts.append(
            MacroFact(
                series_id=series_id("IN", "NSE", "India VIX", measure),
                period_start=session,
                period_end=session,
                release_date=session,
                frequency=Frequency.DAILY,
                unit=Unit.INDEX,
                value=value,
                source=INDIA_VIX_SOURCE_ID,
                l0_key=l0_key,
            )
        )
    return session, facts
