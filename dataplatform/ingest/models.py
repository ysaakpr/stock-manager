"""The canonical shape of one parsed market-data row, shared by every ingestion parser.

NSE published its cash-market bhavcopy in one format until 8 July 2024 and in the UDiFF/ISO-20022
format after it (§4.1, "different column schema — dual parser required"). Two parsers are
unavoidable; two *schemas* would not be — and would be the more expensive mistake, because every
consumer downstream of D1 would then carry an era branch, and the day one branch is forgotten a
backtest silently reads a decade of one era and a year of the other. So the eras converge here:
`bhavcopy_legacy` (M1.4) and `bhavcopy_udiff` (M1.5) both emit `PriceRow`, and a caller cannot
tell from a row which file it came out of.

The model is the schema contract rather than a convenience wrapper, which is why it is strict on
three axes that have bitten this kind of pipeline before:

* **No floats reach it.** Price fields are `strict=True` `Decimal`, so a float, a string or an
  int is a `ValidationError` at construction, not a rounding error discovered in a P&L
  reconciliation months later (CLAUDE.md "Money: Decimal, never float"). A parser converts text
  to `Decimal` itself and hands over the exact object.
* **Infinities and NaN are not numbers.** `allow_inf_nan=False`: a corrupt field that happens to
  spell `Infinity` is a parse failure, not a price that compares greater than everything.
* **Extra fields are forbidden and rows are frozen.** An era-specific column cannot be smuggled
  through as an extra attribute (which is exactly how "identical schema" quietly stops being
  true), and a row cannot be edited after the parser vouched for it.

`isin` is required and validated, not optional: ISIN is the only join key in this system
(invariant #2), and a row that reached L1 without one would have to be joined on a symbol. Both
bhavcopy eras carry ISIN natively. Sources that do not (BSE's legacy file, the delivery file) must
resolve through the D2 identity master *before* they can build one of these.

What this module never does: adjust a price. `PriceRow` is raw traded data exactly as the exchange
published it (invariant #3); adjustment factors live in D3 and are applied on read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ISIN_PATTERN",
    "BhavcopyParse",
    "IngestError",
    "ParseError",
    "PreIsinPriceRow",
    "Price",
    "PriceRow",
    "Quantity",
    "UnidentifiedRow",
    "is_isin_check_digit_valid",
    "is_keyable_isin",
]

#: An ISIN as ISO 6166 defines it: two-letter country code, nine alphanumerics, one check digit.
#: Indian equities are `INE…`/`INF…`/`IN9…`, but the pattern stays general — a Singapore-domiciled
#: line on an Indian exchange is a real thing and is not this parser's business to reject.
ISIN_PATTERN: Final = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"

_ISIN_LITERAL: Final = re.compile(ISIN_PATTERN)


def is_isin_check_digit_valid(isin: str) -> bool:
    """Whether the ISIN's last character is the ISO 6166 check digit of the rest.

    Letters become their base-36 values (`A` = 10 … `Z` = 35), the digits are concatenated, and the
    Luhn sum over the whole string must be divisible by ten. Assumes a twelve-character
    upper-case ISIN; anything else is not an ISIN and is False. Never consults a master: this is
    arithmetic on the string, so it can say "not any security's ISIN" but never "this security's".
    """
    if not re.fullmatch(r"[A-Z0-9]{12}", isin):
        return False
    digits = "".join(str(int(character, 36)) for character in isin)
    total = 0
    for position, character in enumerate(reversed(digits)):
        value = int(character)
        if position % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def is_keyable_isin(value: str) -> bool:
    """Whether a source's ISIN literal can be a join key: the ISO 6166 shape *and* its check digit.

    The shape alone let `IN9232101012` (NSE `SPARC`, series `E1`, 2012-10-09..11) into L1 as a
    key — twelve well-formed characters that are no security's ISIN, because the check digit is
    wrong. A key that names nobody is worse than no key: it can never join, and nothing downstream
    can tell it from a real security with a short history. Never consults a master.
    """
    return _ISIN_LITERAL.match(value) is not None and is_isin_check_digit_valid(value)


#: A price or a rupee amount. `strict` keeps floats out by construction; `ge=0` and
#: `allow_inf_nan=False` keep a mis-parsed field from becoming a plausible-looking number.
#: Zero is legal: the `IL`/`IT` odd-lot series really do publish `LAST` as `0.0` on a session
#: where nothing traded in that window, and rejecting it would fail on real exchange files.
Price = Annotated[Decimal, Field(ge=0, strict=True, allow_inf_nan=False)]

#: A traded quantity or a trade count. Integral by nature in the cash market — the exchange
#: reports whole shares and whole trades — so `int` is exact here and carries no float hazard.
#: Strict, so a float that lost precision on the way in cannot round itself into a share count.
Quantity = Annotated[int, Field(ge=0, strict=True)]


class IngestError(Exception):
    """Base for every ingestion failure, so a caller can catch D1 without catching the world."""


class ParseError(IngestError):
    """A source file could not be turned into rows, named precisely enough to act on.

    Carries the file it failed on and, when the failure is attributable to one record, the
    physical line number inside it. Both go in the message too: an operator reading the alert or
    the sync-state `last_error` gets "which file, which line" without loading anything.

    Never raised for a row that is merely *surprising* — an implausible price is a D7 quality
    finding about data we did parse. This is for input that is not the format it claims to be.
    """

    def __init__(self, message: str, *, filename: str, line: int | None = None) -> None:
        located = f"{filename}:{line}" if line is not None else filename
        super().__init__(f"{located}: {message}")
        self.filename = filename
        self.line = line


class PriceRow(BaseModel):
    """One security's traded session on one exchange date — the canonical D1 output row.

    What it does: carry exactly the facts a bhavcopy publishes about one instrument for one
    session, in the types the rest of the platform is allowed to compute with.
    What it assumes: the parser that built it has already checked the file's structure, so a
    `PriceRow` that exists is a row the source really published.
    What it never does: hold an adjusted price, a derived field, or a value the source did not
    state. `series` in particular is kept verbatim (`EQ`, `BE`, `BZ`, `SM`, `ST`, the whole tail
    of debt and odd-lot series) and is *not* filtered here — which rows a strategy may look at is
    a query concern, and a parser that dropped them would make L0 no longer replayable into L1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(
        pattern=ISIN_PATTERN,
        description="ISO 6166 identifier — the only legitimate join key (invariant #2)",
    )
    symbol: str = Field(min_length=1, description="exchange ticker on `trade_date`, as published")
    series: str = Field(min_length=1, description="NSE series: EQ, BE, BZ, SM, ST, N1…; verbatim")
    trade_date: date = Field(description="the exchange session this row is about (Asia/Kolkata)")

    open: Price = Field(description="first traded price of the session")
    high: Price = Field(description="highest traded price of the session")
    low: Price = Field(description="lowest traded price of the session")
    close: Price = Field(description="closing price as the exchange published it, unadjusted")
    last: Price = Field(description="last traded price; 0 on series where nothing traded late")
    prev_close: Price = Field(description="previous session's close, unadjusted, as published")

    total_traded_qty: Quantity = Field(description="shares traded in the session (TOTTRDQTY)")
    total_traded_value: Price = Field(description="turnover in rupees (TOTTRDVAL)")
    total_trades: Quantity | None = Field(
        description=(
            "number of trades executed (TOTALTRADES); None only for a pre-2011-06-22 row the "
            "pre-ISIN resolver admitted — that era did not publish the column, and absence is not 0"
        ),
    )


class UnidentifiedRow(BaseModel):
    """A row the exchange published with a placeholder where the ISIN belongs.

    What it does: keeps everything the row *did* state — symbol, series, session and the literal
    the ISIN column carried — so the refusal can be enumerated rather than counted.
    What it assumes: the row is otherwise well formed. A corrupt field is a `ParseError`; this
    type is for the narrower fact that the exchange said this instrument has no ISIN.
    What it never does: become a `PriceRow`. ISIN is the only join key (invariant #2), so a row
    without one cannot be keyed, and inventing one from the symbol is the exact defect the
    identity master exists to prevent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, description="exchange ticker on `trade_date`, as published")
    series: str = Field(min_length=1, description="NSE series, verbatim")
    trade_date: date = Field(description="the exchange session this row is about (Asia/Kolkata)")
    stated_isin: str = Field(description="the literal the ISIN column held, e.g. 'DUMMY'")
    line: int = Field(ge=1, description="1-based line in the source file, for the operator")


class PreIsinPriceRow(BaseModel):
    """One E1 (pre-2011-06-22) NSE bhavcopy row with its prices kept — and still no identity.

    What it does: carry everything the eleven-column pre-ISIN bhavcopy states about one
    `(symbol, series)` on one session, so the identity resolver (`dataplatform.identity.pre_isin`)
    can test the exchange's own `PREVCLOSE` chain and, where the evidence admits it, a resolved row
    can be written to `prices_raw` without re-reading L0.
    What it assumes: the parser has validated the file's structure.
    What it never does: carry an ISIN. The era published none; an ISIN is attached only by the
    resolver, and only to a row it can prove, by building a `PriceRow`. There is no
    `total_trades` either — `TOTALTRADES` did not exist yet, and absence is not zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, description="exchange ticker on `trade_date`, as published")
    series: str = Field(min_length=1, description="NSE series, verbatim")
    trade_date: date = Field(description="the exchange session this row is about (Asia/Kolkata)")
    open: Price
    high: Price
    low: Price
    close: Price = Field(description="closing price as the exchange published it, unadjusted")
    last: Price
    prev_close: Price = Field(description="the exchange's previous close — CA-adjusted on ex-dates")
    total_traded_qty: Quantity
    total_traded_value: Price = Field(description="turnover in rupees (TOTTRDVAL)")
    line: int = Field(ge=1, description="1-based line in the source file, for the operator")


@dataclass(frozen=True, slots=True)
class BhavcopyParse:
    """One session's bhavcopy, split into the rows that have an identity and the ones that do not.

    `rows` and `refused` reconcile to the file: every data row in the payload is in exactly one of
    them, which is what lets a caller assert that nothing was dropped (the M1.8 "never silently"
    contract, and the same shape as `bse.bhavcopy.LegacyResolution`).
    """

    rows: tuple[PriceRow, ...]
    refused: tuple[UnidentifiedRow, ...] = ()

    def __len__(self) -> int:
        """Every data row the payload held — the honest count for "rows parsed" in a log line."""
        return len(self.rows) + len(self.refused)
