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

from datetime import date
from decimal import Decimal
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ISIN_PATTERN",
    "IngestError",
    "ParseError",
    "Price",
    "PriceRow",
    "Quantity",
]

#: An ISIN as ISO 6166 defines it: two-letter country code, nine alphanumerics, one check digit.
#: Indian equities are `INE…`/`INF…`/`IN9…`, but the pattern stays general — a Singapore-domiciled
#: line on an Indian exchange is a real thing and is not this parser's business to reject.
ISIN_PATTERN: Final = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"

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
    total_trades: Quantity = Field(description="number of trades executed (TOTALTRADES)")
