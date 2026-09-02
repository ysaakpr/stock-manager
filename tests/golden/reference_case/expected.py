"""Hand-computed expected values for the reference backtest (M4.9) — the literals under test.

Every number here is a literal a human computed from the scenario in ``scenario.py`` and the
published rate card (``execution/costs/rates.yaml``), with the arithmetic shown beside it. The test
runs the real accounting engine and asserts it reproduces each literal to the paisa. Nothing here
calls the engine to *produce* an expected value — that is what makes this a reference and not a
mirror (the same discipline as reference B, ``tests/golden/casebook.py``).

────────────────────────────────────────────────────────────────────────────────────────────────
COST OF ONE BUY (shared cost model, delivery equity, NSE, buy side; brokerage = ₹0 every schedule)

    STT    = round_to_rupee(turnover x 0.001)
    exch   = round_to_paisa(turnover x exch_rate)      NSE: 0.0000325 → 0.0000297 → 0.0000307
    sebi   = round_to_paisa(turnover x 0.000001)
    gst    = round_to_paisa((exch [+ sebi from 2025-04-01]) x 0.18)
    stamp  = round_to_rupee(turnover x 0.00015)        buy side, uniform, from 2020-07-01
    net    = turnover + STT + exch + sebi + gst + stamp     (no DP charge — DP is a sell-side line)

The window crosses four rate regimes; three touch it after 2020-07-01:
    uniform-stamp-duty  2020-07-01 …  exch 0.0000325, gst on {brokerage, exchange}
    true-to-label       2024-10-01 …  exch 0.0000297, gst on {brokerage, exchange}
    current             2025-04-01 …  exch 0.0000307, gst on {brokerage, exchange, sebi}

Per-buy net by turnover and regime (each derived once, then multiplied by the month count):

  IRCTC  T=₹40,000   uniform 40+1.30+0.04+0.23+6 = 47.57  → net 40,047.57
                     ttl     40+1.19+0.04+0.21+6 = 47.44  → net 40,047.44
                     current 40+1.23+0.04+0.23+6 = 47.50  → net 40,047.50
  HDFC Ltd T=₹70,000 uniform 70+2.28+0.07+0.41+11 = 83.76 → net 70,083.76
  HDFC Bank T=₹64,000 uniform 64+2.08+0.06+0.37+10 = 76.51 → net 64,076.51
                     ttl     64+1.90+0.06+0.34+10 = 76.30 → net 64,076.30
                     current 64+1.96+0.06+0.36+10 = 76.38 → net 64,076.38
  RIL pre  T=₹50,000 uniform 50+1.63+0.05+0.29+8 = 59.97  → net 50,059.97
  RIL post T=₹45,000 uniform 45+1.46+0.05+0.26+7 = 53.77  → net 45,053.77
                     ttl     45+1.34+0.05+0.24+7 = 53.63  → net 45,053.63
                     current 45+1.38+0.05+0.26+7 = 53.69  → net 45,053.69

────────────────────────────────────────────────────────────────────────────────────────────────
MONTH COUNTS  (60 instalments, 1st of the month, 2021-05 … 2026-04)

  IRCTC uniform : 6 pre-split (2021-05…10) + 35 post-split (2021-11…2024-09) = 41
  IRCTC ttl     : 6  (2024-10…2025-03)          IRCTC current : 13 (2025-04…2026-04)
  HDFC Ltd      : 27 uniform (2021-05…2023-07)  — all before the merger, all before 2024-10
  HDFC Bank     : 14 uniform (2023-08…2024-09) + 6 ttl + 13 current = 33
  RIL pre       : 27 uniform (2021-05…2023-07)  — before the demerger
  RIL post      : 14 uniform (2023-08…2024-09) + 6 ttl + 13 current = 33

────────────────────────────────────────────────────────────────────────────────────────────────
QUANTITIES  (the corporate actions, applied with the M2-verified ratios)

  IRCTC  : 6x10 = 60 shares, then 1:5 split x5 → 300, then 54x50 = 2,700 → 3,000
  HDFC   : 27x25 = 675 HDFC Ltd, then 42:25 merger → 675x42/25 = 1,134 HDFC Bank,
           then 33x40 = 1,320 → 2,454 HDFC Bank ;  HDFC Ltd → 0
  RIL    : 27x25 = 675, 1:1 demerger keeps 675, then 33x25 = 825 → 1,500
  JFS    : 1:1 on 675 RIL held at the demerger → 675

────────────────────────────────────────────────────────────────────────────────────────────────
COST BASIS  (basis is unmoved by a split; a merger carries it whole; a demerger splits it 10/90)

  IRCTC  = 41x40,047.57 + 6x40,047.44 + 13x40,047.50
         = 1,641,950.37 + 240,284.64 + 520,617.50 = 2,402,852.51
  HDFC Ltd basis at merger = 27x70,083.76 = 1,892,261.52  (carried whole to HDFC Bank)
  HDFC Bank post-merger buys = 14x64,076.51 + 6x64,076.30 + 13x64,076.38
                             = 897,071.14 + 384,457.80 + 832,992.94 = 2,114,521.88
  HDFC Bank = 1,892,261.52 + 2,114,521.88 = 4,006,783.40
  RIL basis at demerger = 27x50,059.97 = 1,351,619.19
    JFS  = 1,351,619.19 x 0.10 = 135,161.919
    RIL retained = 1,351,619.19 - 135,161.919 = 1,216,457.271
  RIL post-demerger buys = 14x45,053.77 + 6x45,053.63 + 13x45,053.69
                         = 630,752.78 + 270,321.78 + 585,697.97 = 1,486,772.53
  RIL  = 1,216,457.271 + 1,486,772.53 = 2,703,229.801
  TOTAL basis = 2,402,852.51 + 4,006,783.40 + 2,703,229.801 + 135,161.919 = 9,248,027.63

────────────────────────────────────────────────────────────────────────────────────────────────
CASHFLOWS AND RETURN

  Total deposited = 60 x 170,000 = 10,200,000
  Free cash = deposited - total basis = 10,200,000 - 9,248,027.63 = 951,972.37
  Terminal market value (2026-04-30):
        IRCTC 3,000x1,100 = 3,300,000
        HDFC  2,454x2,000 = 4,908,000
        RIL   1,500x2,000 = 3,000,000
        JFS     675x  350 =   236,250
        market value      = 11,444,250
  Terminal NAV = 951,972.37 + 11,444,250 = 12,396,222.37
  Unrealized P&L = 11,444,250 - 9,248,027.63 = 2,196,222.37 ;  realized P&L = 0 (no sells)

  XIRR: 60 pay-ins of -170,000 on the 1st of each month + one pay-out of +12,396,222.37 on
  2026-04-30, ACT/365F (matches a spreadsheet XIRR — see backtest/xirr.py). Quantised to 8 places.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tests.golden.reference_case.scenario import (
    HDFC_BANK,
    HDFC_LTD,
    IRCTC,
    JFS,
    RIL,
)

__all__ = ["EXPECTED", "ReferenceExpectations"]


@dataclass(frozen=True, slots=True)
class ReferenceExpectations:
    """The hand-computed truth of the reference case, as literals (see the module docstring)."""

    quantities: dict[str, int]
    cost_basis: dict[str, Decimal]
    total_deposited: Decimal
    total_cost_basis: Decimal
    free_cash: Decimal
    market_value: Decimal
    net_asset_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    xirr: Decimal
    #: ISINs whose positions have been extinguished (the acquired side of the merger).
    zeroed: tuple[str, ...] = field(default=())


EXPECTED = ReferenceExpectations(
    quantities={
        IRCTC: 3000,
        HDFC_BANK: 2454,
        RIL: 1500,
        JFS: 675,
    },
    cost_basis={
        IRCTC: Decimal("2402852.51"),
        HDFC_BANK: Decimal("4006783.40"),
        RIL: Decimal("2703229.801"),
        JFS: Decimal("135161.919"),
    },
    total_deposited=Decimal("10200000"),
    total_cost_basis=Decimal("9248027.63"),
    free_cash=Decimal("951972.37"),
    market_value=Decimal("11444250"),
    net_asset_value=Decimal("12396222.37"),
    unrealized_pnl=Decimal("2196222.37"),
    realized_pnl=Decimal("0"),
    xirr=Decimal("0.07739877"),
    zeroed=(HDFC_LTD,),
)
