"""Face-value changes as NSE's book-closure file abbreviates them — and only those.

The bc member of the NSE PR bundle (``bc<ddmmyyyy>.csv``) states a split or consolidation in a
40-character purpose field, abbreviated in ways the feed parser (`parse_terms`, written for the
CA feeds' long-form subjects) does not read: ``CNSLDATNRE1 TO RS10`` (AvenuesAI, 2026),
``FVSPLT FRM RS 10 TO RE 1``, ``FV SPLT FRM RS 10 TO RS 2``, ``SPLT RS10TORE1``,
``FVS RS.5TORE.1``, ``CONSOLIDATION RE1 TO RS10``, ``CON RE 1TO RS10``. Measured on every bc row in
L0 (2010-01 to 2026-10). This module turns exactly those into `FaceValueTerms`.

Deliberately narrow:

* a face-value keyword (split, sub-division, consolidation, ``FVS``/``FV``) must sit directly in
  front of the two values — ``SPLDIV`` (a special dividend) is not a split;
* a capital reduction (``CAP RED``, ``CAPTL REDN``) is refused even when it states two face values:
  it cancels capital, it is not a re-basing of the same holding, and no price factor follows
  from the face values alone (OCLINDIA's ``CAP RED RS 10 TO RS 2``);
* a purpose with two face-value clauses, or a value missing on either side (``FV SPLT FRM RS 10
  TO``), yields nothing rather than a guess.

Pure: no I/O, no clock.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Final

from dataplatform.corpactions.taxonomy import FaceValueTerms

__all__ = ["face_value_change"]

_KEYWORD: Final = (
    r"(?:FV[\s-]*SPL(?:IT|T)?|FV-?SPTL|(?<=/)SPL|FVSPLT|SPLT|SPLIT|SUB-?DIVISION|FVS|"
    r"CNSLDATN|CONSOLIDATION|CONSOLDTN|CONSOLIDATIN|CON)"
)
_VALUE: Final = r"(?:RS|RE)?\.?\s*(\d+(?:\.\d+)?)(?:/-?)?"
_CLAUSE: Final[re.Pattern[str]] = re.compile(
    rf"{_KEYWORD}[\s\-:.]*(?:FRM|FROM)?\s*{_VALUE}\s*TO\s*{_VALUE}(?![\d])", re.IGNORECASE
)
#: Capital reduction and (debt) partial redemption restate a face value without re-basing a holding.
_NOT_A_REBASING: Final[re.Pattern[str]] = re.compile(
    r"CAP(?:ITA)?T?L?\.?\s*RED|REDM|REDEMPTION", re.IGNORECASE
)


def face_value_change(purpose: str) -> FaceValueTerms | None:
    """The face values a bc purpose states for a split or consolidation, or `None`.

    What it does: finds one ``<keyword> [FRM] <value> TO <value>`` clause and returns it as
    `FaceValueTerms(from_value, to_value)` — a consolidation is a SPLIT whose face value rises
    (Re 1 -> Rs 10: price factor 10). What it assumes: `purpose` is the bc PURPOSE field as
    published. What it never does: read a ratio from anything but the two stated face values,
    accept a capital reduction, or pick one of two clauses.
    """
    if _NOT_A_REBASING.search(purpose):
        return None
    clauses = _CLAUSE.findall(purpose)
    if len(clauses) != 1:
        return None
    [(raw_from, raw_to)] = clauses
    try:
        from_value, to_value = Decimal(raw_from), Decimal(raw_to)
    except InvalidOperation:
        return None
    if from_value <= 0 or to_value <= 0 or from_value == to_value:
        return None
    return FaceValueTerms(from_value=from_value, to_value=to_value)
