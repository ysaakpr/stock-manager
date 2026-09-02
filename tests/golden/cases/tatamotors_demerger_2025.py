"""Tata Motors CV/PV demerger, 2025 (EXECUTION_PLAN §4.3).

Published terms: demerger of Tata Motors into two listed entities — the commercial-vehicles business
and the passenger-vehicles business — with holders entitled to 1 share of the demerged entity for
every 1 share held (1:1). On the Tata Motors line (INE155A01022) the entitlement takes effect around
2025-10-14.

Why this is a structural break, not a return (§4.3 rule 3): a large fraction of the enterprise
leaves the listed line on the ex-date, so the close-to-close fall is the carved-out value, not a
market return. The engine keeps ``price_factor = 1`` (adjusting the remaining line for the demerged
entity needs that entity's price, a different ISIN) and marks the ex-date a structural break, so the
**return series bridges** the gap rather than booking a spurious crash.

Hand-computed adjusted closes (reference B) — unit factor, so adjusted == raw:

    2025-10-13 (cum-demerger)   660.00 x 1.0 = 660.00
    2025-10-14 (ex)             400.00 x 1.0 = 400.00   <- return bridged (business carve-out)
    2025-10-15 (post)           405.00 x 1.0 = 405.00

Note: this file isolates the demerger; the 2024 DVR conversion on the same ISIN is its own file.
"""

from datetime import date
from decimal import Decimal

from tests.golden.casebook import Expectation, GoldenCase, demerger

_ISIN = "INE155A01022"  # Tata Motors
_EX = date(2025, 10, 14)

CASE = GoldenCase(
    case_id="tatamotors_demerger_2025",
    title="Tata Motors CV/PV demerger (2025)",
    isin=_ISIN,
    published_terms="Demerger into commercial-vehicles and passenger-vehicles entities, 1:1 (2025)",
    actions=(
        demerger(
            _ISIN,
            _EX,
            received="1",
            held="1",
            raw_text="COMPOSITE SCHEME OF ARRANGEMENT - DEMERGER OF CV / PV BUSINESS (1:1)",
        ),
    ),
    expectations=(
        Expectation(date(2025, 10, 13), Decimal("660.00"), Decimal("660.00"), "660.00 x 1.0"),
        Expectation(_EX, Decimal("400.00"), Decimal("400.00"), "400.00 x 1.0 (bridged)"),
        Expectation(date(2025, 10, 15), Decimal("405.00"), Decimal("405.00"), "405.00 x 1.0"),
    ),
    bridged_ex_dates=(_EX,),
)
