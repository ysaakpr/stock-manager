"""PE timing driver — the standalone monthly states, the switch recount and the D13 cross-check.

Synthetic series only: nothing here reads the lake or prints a study figure.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.ma_timing import IndexPoint, IndexSeries
from backtest.pe_timing import (
    StudyError,
    _invested,
    _Signals,
    _switches,
    compare_reference,
    pe_states,
)
from backtest.policies.momentum_v2 import ValuationGate
from backtest.sweep import PE_TIMING_ARMS
from backtest.valuation_gate import PePoint, ValuationSource


def _weekdays(start: date, n: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _series(days: list[date]) -> IndexSeries:
    points = tuple(IndexPoint(d, d, Decimal("100")) for d in days)
    return IndexSeries(slug="t", name="T", points=points, spellings={"T": tuple(days)})


def test_pe_states_strike_at_month_end_and_carry_between() -> None:
    days = _weekdays(date(2024, 1, 1), 70)  # January to early April
    # P/E falls through January, rises through February, falls again in March.
    values = [Decimal(30 - i) if d.month == 1 else Decimal(10 + i) for i, d in enumerate(days)]
    values = [v if d.month != 3 else Decimal(5) for v, d in zip(values, days, strict=True)]
    source = ValuationSource(
        [PePoint(d, d, v) for d, v in zip(days, values, strict=True)], mean_days=5
    )
    decided = pe_states(_series(days), source)
    jan_end = max(d for d in days if d.month == 1)
    feb_end = max(d for d in days if d.month == 2)
    assert decided[days[0]] == (None, None)  # nothing struck before the first month end
    assert decided[jan_end][0] is True  # falling: below its trailing mean — cheap
    mid_feb = next(d for d in days if d.month == 2 and d.day >= 15)
    assert decided[mid_feb][0] is True  # carried until the next month end
    assert decided[feb_end][0] is False  # rising: above its mean — rich (the inversion)
    # A state is struck only from points knowable by the session: the reading equals the source's.
    assert decided[feb_end][1] == source.reading(feb_end).trailing_mean


def test_pe_states_fail_loud_on_a_missing_month_end_pe() -> None:
    days = _weekdays(date(2024, 1, 1), 45)
    jan_end = max(d for d in days if d.month == 1)
    points = [PePoint(d, d, Decimal("20")) for d in days if d != jan_end]
    with pytest.raises(Exception, match="has no figure for"):
        pe_states(_series(days), ValuationSource(points, mean_days=3))


@pytest.mark.parametrize(
    ("gate", "price_on", "cheap", "invested"),
    [
        (ValuationGate.ENTRY, True, False, True),
        (ValuationGate.ENTRY, False, True, False),
        (ValuationGate.REPLACE, False, True, True),
        (ValuationGate.REPLACE, True, False, False),
        (ValuationGate.BOTH, True, True, True),
        (ValuationGate.BOTH, True, False, False),
        (ValuationGate.BOTH, False, True, False),
    ],
)
def test_the_recount_invests_as_the_policy_does(
    gate: ValuationGate, price_on: bool, cheap: bool, invested: bool
) -> None:
    assert _invested(gate, price_on, cheap) is invested


def test_the_switch_recount() -> None:
    months = [date(2024, m, 1) for m in range(1, 7)]
    price = dict(zip(months, [True, False, False, True, True, True], strict=True))
    cheap = dict(zip(months, [True, True, False, False, True, False], strict=True))
    signals = _Signals(
        rebalances=tuple(months),
        sessions=tuple(months),
        price_on=price,
        cheap={(21, False): cheap},
        tri={},
    )
    d13 = _switches(signals, None, (21, False))
    assert (d13.parks, d13.reentries, d13.parked_months, d13.blocked_months) == (1, 1, 2, 0)
    entry = _switches(signals, ValuationGate.ENTRY, (21, False))
    assert (entry.parks, entry.reentries, entry.blocked_months) == (1, 1, 2)  # Apr and Jun rich
    replace_ = _switches(signals, ValuationGate.REPLACE, (21, False))
    assert (replace_.parks, replace_.reentries, replace_.parked_months) == (2, 1, 3)
    both = _switches(signals, ValuationGate.BOTH, (21, False))
    assert (both.parks, both.reentries, both.parked_months) == (2, 1, 4)


def test_eighteen_arms_with_distinct_labels() -> None:
    labels = [arm.label for arm in PE_TIMING_ARMS]
    assert len(labels) == len(set(labels)) == 18
    assert sum(1 for label in labels if label.endswith("(spliced P/E)")) == 9


def _record(**override: str) -> dict[str, str]:
    run = {
        "fold": "F1",
        "role": "test",
        "floor": "100000000",
        "digest": "d",
        "summary_sha256": "s",
        "ledger_sha256": "l",
        "nav_sha256": "n",
    }
    return {**run, **override}


def test_d13_must_match_the_earlier_frozen_record(tmp_path: Path) -> None:
    reference = tmp_path / "frozen.json"
    reference.write_text(json.dumps({"runs": [_record()]}))
    assert compare_reference({"runs": [_record()]}, reference) == ["F1 test @100000000: identical"]
    moved = compare_reference({"runs": [_record(ledger_sha256="x")]}, reference)
    assert moved == ["F1 test @100000000: DIFFERS: ledger_sha256"]
    with pytest.raises(StudyError, match="not in"):
        compare_reference({"runs": [_record(fold="F2")]}, reference)
