"""The M4 gate's hand-computed reference backtest case (M4.9).

``scenario`` holds the inputs and the engine run (:func:`build_reference_book`); ``expected`` holds
the hand-computed literals (:data:`EXPECTED`). The test in ``../test_reference_case.py`` asserts the
engine reproduces the literals to the paisa.
"""

from __future__ import annotations

from tests.golden.reference_case.expected import EXPECTED, ReferenceExpectations
from tests.golden.reference_case.scenario import (
    HDFC_BANK,
    HDFC_LTD,
    INSTALMENT,
    IRCTC,
    JFS,
    RIL,
    TERMINAL_DATE,
    TERMINAL_PRICES,
    build_reference_book,
    instalment_dates,
)

__all__ = [
    "EXPECTED",
    "HDFC_BANK",
    "HDFC_LTD",
    "INSTALMENT",
    "IRCTC",
    "JFS",
    "RIL",
    "TERMINAL_DATE",
    "TERMINAL_PRICES",
    "ReferenceExpectations",
    "build_reference_book",
    "instalment_dates",
]
