"""World Bank Indicators API → annual India macro series, dated by the envelope's `lastupdated`.

The register's `worldbank_indicator_api` row (VERIFIED, M11.1) had no parser. This is it.

**The one decision that matters here is the release date, and it is not the year.** The API serves
only the *current* vintage of each annual figure: a revision silently replaces the original and
there is no vintage parameter to ask what was published at the time. The only honest date the
response carries is the envelope's `lastupdated` — the day this vintage of the whole source was
published. So every fact lands with `release_date = lastupdated`, which makes India's 2009 CPI
inflation knowable on 2026-07-13, not in 2010. A backtest reading `read_pit(2019-06-30)` therefore
sees *nothing* from this source, by construction — the store's release-date partitioning enforces
it, rather than a comment asking readers to be careful. Present-day backdrop, never a backtest
input (Grade C, `ops/gates/macro-news-provider-evaluation-2026-09-07.md`).

A later vintage (a new `lastupdated`) lands in a later partition, so both coexist and `read_latest`
collapses to the newer. A *different value under the same `lastupdated`* is refused by
`macro_series.write_release` — that would be the source revising history without saying so, and it
must stop a human rather than silently overwrite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
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
    "WORLDBANK_COUNTRY",
    "WORLDBANK_SERIES",
    "WORLDBANK_SOURCE_ID",
    "WorldBankSeries",
    "parse_worldbank",
    "worldbank_filename",
    "worldbank_url",
]

#: The register id whose bytes this parser reads.
WORLDBANK_SOURCE_ID: Final = "worldbank_indicator_api"

#: India, as the API's ISO-3 path segment.
WORLDBANK_COUNTRY: Final = "IND"

#: One page holds every annual observation since 1960 (66 on the verified fetch). A response that
#: needs a second page is refused rather than silently truncated.
_PER_PAGE: Final = 100


@dataclass(frozen=True, slots=True)
class WorldBankSeries:
    """One WDI indicator and the canonical `series_id` levels and unit it lands under."""

    indicator: str
    subject: str
    measure: str
    unit: Unit

    @property
    def series_id(self) -> str:
        """`IN.WB.<subject>.<measure>` — the World Bank's figure, never merged with another's."""
        return series_id("IN", "WB", self.subject, self.measure)


#: The India series the macro plan names (CPI inflation, GDP growth, current account) and the
#: transmission-channel backdrop around them. Annual, current-vintage only — see the module note.
WORLDBANK_SERIES: Final[tuple[WorldBankSeries, ...]] = (
    WorldBankSeries("FP.CPI.TOTL.ZG", "CPI_INFLATION", "ANNUAL_PCT", Unit.PCT),
    WorldBankSeries("NY.GDP.MKTP.KD.ZG", "GDP_GROWTH", "REAL_ANNUAL_PCT", Unit.PCT),
    WorldBankSeries("NY.GDP.DEFL.KD.ZG", "GDP_DEFLATOR_INFLATION", "ANNUAL_PCT", Unit.PCT),
    WorldBankSeries("NY.GDP.MKTP.CD", "GDP", "CURRENT_USD", Unit.USD),
    WorldBankSeries("BN.CAB.XOKA.GD.ZS", "CURRENT_ACCOUNT", "PCT_OF_GDP", Unit.PCT),
    WorldBankSeries("PA.NUS.FCRF", "INR_PER_USD", "ANNUAL_AVERAGE", Unit.INR),
    WorldBankSeries("FI.RES.TOTL.CD", "TOTAL_RESERVES", "CURRENT_USD", Unit.USD),
    WorldBankSeries("FR.INR.LEND", "LENDING_RATE", "ANNUAL_PCT", Unit.PCT),
)


def worldbank_url(indicator: str, *, country: str = WORLDBANK_COUNTRY) -> str:
    """The keyless JSON URL for one indicator's whole annual history."""
    return (
        f"https://api.worldbank.org/v2/country/{country}/indicator/{indicator}"
        f"?format=json&per_page={_PER_PAGE}"
    )


def worldbank_filename(indicator: str, captured: date, *, country: str = WORLDBANK_COUNTRY) -> str:
    """L0 filename for one capture: the indicator and the capture date, so weeks never collide."""
    return f"{country}_{indicator}_{captured:%Y%m%d}.json"


def parse_worldbank(
    payload: bytes, *, spec: WorldBankSeries, filename: str, l0_key: str | None = None
) -> MacroRelease:
    """Parse one indicator response into a release dated by the envelope's `lastupdated`.

    What it does: read the two-element `[envelope, observations]` array, take `lastupdated` as the
    release date of every fact, and emit one annual fact per year that carries a value.
    What it assumes: the response is one page (`per_page` covers the whole history) and every
    observation is for `spec.indicator`.
    What it never does: date a fact by its year, turn a `null` into a zero (a year with no figure is
    simply absent), or accept a figure for a year that had not ended by `lastupdated`.

    Raises `ParseError` for a non-JSON body, an API error envelope, a multi-page response, an
    observation for another indicator, a malformed year or number, or no observations at all.
    """
    try:
        document = json.loads(payload.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ParseError(f"not a JSON body: {error}", filename=filename) from error
    if not isinstance(document, list) or not document or not isinstance(document[0], dict):
        raise ParseError("expected a [envelope, observations] array", filename=filename)
    envelope: dict[str, Any] = document[0]
    if "message" in envelope:
        raise ParseError(f"API error envelope: {envelope['message']!r}", filename=filename)
    if len(document) != 2 or not isinstance(document[1], list):
        raise ParseError("expected a [envelope, observations] array", filename=filename)
    if int(envelope.get("pages", 1)) != 1:
        raise ParseError(
            f"response has {envelope.get('pages')} pages at per_page={envelope.get('per_page')}; "
            "refusing a partial history",
            filename=filename,
        )
    try:
        released = date.fromisoformat(str(envelope["lastupdated"]))
    except (KeyError, ValueError) as error:
        raise ParseError(
            f"envelope has no usable lastupdated: {error}", filename=filename
        ) from error

    facts: list[MacroFact] = []
    for position, row in enumerate(document[1], start=1):
        indicator = (row.get("indicator") or {}).get("id")
        if indicator != spec.indicator:
            raise ParseError(
                f"observation {position} is for {indicator!r}, not {spec.indicator!r}",
                filename=filename,
            )
        value = row.get("value")
        if value is None:
            continue
        try:
            year = int(str(row["date"]))
            amount = store_value(str(value))
        except (KeyError, ValueError, InvalidOperation, ArithmeticError) as error:
            raise ParseError(
                f"observation {position}: unreadable date/value: {error}", filename=filename
            ) from error
        period_end = date(year, 12, 31)
        if period_end > released:
            raise ParseError(
                f"observation {position} gives {year} a value but the vintage is dated "
                f"{released} — a year cannot be measured before it ends",
                filename=filename,
            )
        facts.append(
            MacroFact(
                series_id=spec.series_id,
                period_start=date(year, 1, 1),
                period_end=period_end,
                release_date=released,
                frequency=Frequency.ANNUAL,
                unit=spec.unit,
                value=amount,
                source=WORLDBANK_SOURCE_ID,
                l0_key=l0_key,
            )
        )
    if not facts:
        raise ParseError(f"no observations with a value for {spec.indicator}", filename=filename)
    return MacroRelease(
        release_date=released, source=WORLDBANK_SOURCE_ID, facts=tuple(facts), l0_key=l0_key
    )
