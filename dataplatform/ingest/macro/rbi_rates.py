"""The RBI's "Current Rates" panel → the policy rate and reserve ratios as seen on a capture day.

`https://www.rbi.org.in/Home.aspx` carries a "Current Rates" box: the policy repo rate, the
standing deposit and marginal standing facility rates, the bank rate, the fixed reverse repo rate,
and the CRR and SLR. There is no free, dated, machine-readable *history* of these anywhere probed —
the RBI's Database on the Indian Economy presents a certificate for the wrong hostname and is not
worked around (`ops/gates/macro-probes-2026-10-06.md`) — so this is Tier B forward capture, the
same logic as `constituents_snapshot`: the backtestable window starts the day this first runs,
and the store makes that boundary physical by holding no earlier partition.

**What a fact means.** The panel states the rates *in force*, with no effective date beside them.
So a fact here is a dated observation of state: `period_end = release_date = the capture date`,
frequency `EVENT` (a policy rate has no cadence; forward-filling between observations is the
reader's job). It is never back-dated to the MPC meeting that set it — the meeting date is not on
the page, and inventing it from memory would be the fabrication invariant #8 forbids. The capture
date is a conservative knowable date: the rate was public no later than the day we saw it.

The panel also quotes FBIL's reference rates "as at 1.00pm" and market yields; those are not read
here. FX comes from FBIL's own archive (`fbil.py`), which is dated and deeper.
"""

from __future__ import annotations

import html
import re
from datetime import date
from decimal import InvalidOperation
from typing import Final

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
    "RBI_CURRENT_RATES",
    "RBI_HOME_URL",
    "RBI_RATES_SOURCE_ID",
    "parse_rbi_current_rates",
    "rbi_home_filename",
]

#: The register id whose bytes this parser reads.
RBI_RATES_SOURCE_ID: Final = "rbi_current_rates"

RBI_HOME_URL: Final = "https://www.rbi.org.in/Home.aspx"

#: Panel label → `series_id` subject. Every label must be found: a panel that lost one has changed
#: layout, and a silently missing policy rate is worse than a failed capture.
RBI_CURRENT_RATES: Final[tuple[tuple[str, str], ...]] = (
    ("Policy Repo Rate", "POLICY_REPO_RATE"),
    ("Standing Deposit Facility Rate", "STANDING_DEPOSIT_FACILITY_RATE"),
    ("Marginal Standing Facility Rate", "MARGINAL_STANDING_FACILITY_RATE"),
    ("Bank Rate", "BANK_RATE"),
    ("Fixed Reverse Repo Rate", "FIXED_REVERSE_REPO_RATE"),
    ("CRR", "CASH_RESERVE_RATIO"),
    ("SLR", "STATUTORY_LIQUIDITY_RATIO"),
)

_TAGS: Final = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.S | re.I)
_SPACE: Final = re.compile(r"\s+")


def rbi_home_filename(captured: date) -> str:
    """L0 filename for one capture of the home page."""
    return f"Home_{captured:%Y%m%d}.html"


def parse_rbi_current_rates(
    payload: bytes, *, captured: date, filename: str, l0_key: str | None = None
) -> MacroRelease:
    """Parse the home page's "Current Rates" panel into one release dated `captured`.

    What it does: reduce the page to visible text, cut it at the "Current Rates" heading, and read
    each `<label> : <n>%` pair in `RBI_CURRENT_RATES`.
    What it assumes: `captured` is the date the bytes were fetched (the L0 logical date).
    What it never does: read a rate from outside the panel, or default a missing one.

    Raises `ParseError` when the panel heading or any of its labels is absent, or a value is not a
    percentage — a layout change fails the capture loudly instead of storing a partial panel.
    """
    text = _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", payload.decode("utf-8", "replace"))))
    start = text.find("Current Rates")
    if start < 0:
        raise ParseError("no 'Current Rates' panel on the page", filename=filename)
    panel = text[start : start + 4000]
    end = panel.find("Exchange")
    if end > 0:
        panel = panel[:end]

    facts: list[MacroFact] = []
    for label, subject in RBI_CURRENT_RATES:
        match = re.search(
            rf"(?<![A-Za-z]){re.escape(label)}\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*%", panel
        )
        if match is None:
            raise ParseError(
                f"'Current Rates' panel has no {label!r} percentage", filename=filename
            )
        try:
            value = store_value(match.group(1))
        except (InvalidOperation, ArithmeticError) as error:
            raise ParseError(f"{label}: {error}", filename=filename) from error
        facts.append(
            MacroFact(
                series_id=series_id("IN", "RBI", subject, "RATE"),
                period_start=captured,
                period_end=captured,
                release_date=captured,
                frequency=Frequency.EVENT,
                unit=Unit.PCT,
                value=value,
                source=RBI_RATES_SOURCE_ID,
                l0_key=l0_key,
            )
        )
    return MacroRelease(
        release_date=captured, source=RBI_RATES_SOURCE_ID, facts=tuple(facts), l0_key=l0_key
    )
