"""D7's quarantine reader and its step-change rule (2026-09-06 audit, finding N3).

`prices_raw_quarantine` had a writer and no reader: 1.8 M rows across 2,467 partitions, one grep
away from invisible, with no endpoint, rule, threshold or alert. This suite pins the two halves of
the fix, and the second matters more than the first:

* **Counting** is exercised against a real parquet written through the declared schema under
  `tmp_path`, so the hive layout and the absent-dataset case are both facts rather than mocks.
* **Judging** is exercised over synthetic series, because the rule has to be shown *firing* and
  the real lake has no step change in ten years — which is the rule working, not the rule being
  untested. A rule whose only evidence is that it stayed quiet is a rule nobody has tested.

Every test here is written so the plausible wrong implementation fails it: alerting on the level
(the ~30 % of delivery rows that cannot resolve today) rather than on the change, using a mean
that one bad session drags upward, flagging the first sessions of a series that have no history
to be a step away from, or answering "clean" for a lake it never read.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataplatform.quality.quarantine import (
    DEFAULT_MIN_ROWS,
    DEFAULT_WINDOW,
    QuarantineCount,
    read_quarantine,
    step_changes,
)
from dataplatform.store.paths import Layer, layer_root
from dataplatform.store.schemas import (
    PRICES_RAW_QUARANTINE_DATASET,
    PRICES_RAW_QUARANTINE_SCHEMA,
    PriceQuarantineReason,
)

SESSION: Final = date(2026, 8, 7)
NSE: Final = "NSE"
UNRESOLVED: Final = PriceQuarantineReason.SYMBOL_UNRESOLVED
ORPHANED: Final = PriceQuarantineReason.NO_MATCHING_PRICE


def _write_partition(root: Path, trade_date: date, rows: list[tuple[str, str, str]]) -> None:
    """One quarantine partition, written through the declared schema like `store/l1.py` does."""
    partition = (
        layer_root(Layer.L1, data_root=root)
        / PRICES_RAW_QUARANTINE_DATASET
        / f"date={trade_date.isoformat()}"
    )
    partition.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [
            {
                "symbol": symbol,
                "series": "EQ",
                "trade_date": trade_date,
                "exchange": exchange,
                "isin": None,
                "deliv_qty": None,
                "deliv_pct": None,
                "reason": reason,
            }
            for symbol, exchange, reason in rows
        ],
        schema=PRICES_RAW_QUARANTINE_SCHEMA,
    )
    pq.write_table(table, partition / "part.parquet")


def _series(counts: list[int], *, reason: str = UNRESOLVED) -> list[QuarantineCount]:
    """One `(exchange, reason)` series, one session per count, consecutive dates."""
    return [
        QuarantineCount(
            trade_date=SESSION + timedelta(days=index), exchange=NSE, reason=reason, rows=rows
        )
        for index, rows in enumerate(counts)
    ]


# ── reading the lake ─────────────────────────────────────────────────────────────────────────


def test_counts_by_session_exchange_and_reason(tmp_path: Path) -> None:
    _write_partition(
        tmp_path,
        SESSION,
        [("A", NSE, UNRESOLVED), ("B", NSE, UNRESOLVED), ("C", NSE, ORPHANED)],
    )
    _write_partition(tmp_path, SESSION + timedelta(days=1), [("D", NSE, UNRESOLVED)])

    report = read_quarantine(SESSION, SESSION + timedelta(days=1), data_root=tmp_path)

    assert report.rows == 4
    assert report.partitions_read == 2
    assert report.totals() == {UNRESOLVED: 3, ORPHANED: 1}
    assert [(c.trade_date, c.reason, c.rows) for c in report.counts] == [
        (SESSION, ORPHANED, 1),
        (SESSION, UNRESOLVED, 2),
        (SESSION + timedelta(days=1), UNRESOLVED, 1),
    ]


def test_a_partition_outside_the_range_is_not_read(tmp_path: Path) -> None:
    """The range bounds the answer — otherwise a "last 90 days" view silently reports a decade."""
    _write_partition(tmp_path, SESSION, [("A", NSE, UNRESOLVED)])
    _write_partition(tmp_path, SESSION + timedelta(days=30), [("B", NSE, UNRESOLVED)])

    report = read_quarantine(SESSION, SESSION + timedelta(days=1), data_root=tmp_path)

    assert report.rows == 1
    assert report.partitions_read == 1


def test_a_lake_with_no_quarantine_answers_zero_over_zero(tmp_path: Path) -> None:
    """Zero rows is a real and good state. Zero rows over *zero partitions* is a different one.

    Both are reported, because "we found nothing wrong" and "we checked nothing" rendering
    identically is the mistake the gap report already refuses to make.
    """
    report = read_quarantine(SESSION, SESSION, data_root=tmp_path)

    assert report.rows == 0
    assert report.partitions_read == 0
    assert "0 partition(s)" in report.summary()


def test_an_inverted_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="runs forwards"):
        read_quarantine(SESSION, SESSION - timedelta(days=1), data_root=tmp_path)


# ── the rule: a step change, never the level ─────────────────────────────────────────────────


def test_a_steady_high_level_is_never_a_step() -> None:
    """The whole design decision. ~30 % of delivery rows cannot resolve today and that is known.

    A rule that fired on the level would fire every session, be muted inside a week, and then be
    watching nothing. This is the test that fails if someone "improves" it into a threshold.
    """
    steady = _series([1000] * (DEFAULT_WINDOW * 3))

    assert step_changes(steady) == ()


def test_a_doubling_against_the_trailing_median_is_a_step() -> None:
    counts = _series([100] * DEFAULT_WINDOW + [250])

    (step,) = step_changes(counts)

    assert step.trade_date == counts[-1].trade_date
    assert step.rows == 250
    assert step.baseline == Decimal("100")
    assert step.multiple == Decimal("2.5")
    assert "step change, not the known level" in step.detail()


def test_a_session_with_no_history_behind_it_is_never_a_step() -> None:
    """Calling the first session of a series a step change is an artefact of where data starts."""
    counts = _series([10_000] + [100] * (DEFAULT_WINDOW - 1))

    assert step_changes(counts) == ()


def test_one_bad_session_does_not_raise_the_bar_for_the_next() -> None:
    """A median, not a mean — the reason the rule keeps working the day after an incident.

    With a mean, a single 20x session lifts the baseline enough to hide a genuine repeat the very
    next day, which is exactly when someone is watching.
    """
    counts = _series([100] * DEFAULT_WINDOW + [10_000, 10_000])

    steps = step_changes(counts)

    assert len(steps) == 2, "the repeat must still be a step"
    assert all(step.baseline == Decimal("100") for step in steps)


def test_small_counts_do_not_trip_a_multiplicative_rule() -> None:
    """Two rows becoming four is a doubling and is not news. The floor is what says so."""
    counts = _series([1] * DEFAULT_WINDOW + [DEFAULT_MIN_ROWS - 1])

    assert step_changes(counts) == ()


def test_each_reason_is_judged_against_its_own_history() -> None:
    """A quiet reason spiking must not be hidden by a loud one, and vice versa."""
    loud = _series([10_000] * (DEFAULT_WINDOW + 1), reason=UNRESOLVED)
    quiet = _series([100] * DEFAULT_WINDOW + [900], reason=ORPHANED)

    (step,) = step_changes(loud + quiet)

    assert step.reason == ORPHANED
    assert step.rows == 900


def test_nothing_then_something_is_the_strongest_step_not_an_edge_case() -> None:
    """A series that refused nothing for a month and now refuses thousands is the clearest break.

    It has no ratio to report, which is why `multiple` is None here — and why skipping a zero
    baseline "to avoid dividing by zero" would silently discard the most obvious signal the rule
    can see.
    """
    counts = _series([0] * DEFAULT_WINDOW + [5_000])

    (step,) = step_changes(counts)

    assert step.rows == 5_000
    assert step.baseline == Decimal("0")
    assert step.multiple is None
    assert "quarantined nothing until now" in step.detail()


def test_a_zero_baseline_still_respects_the_floor() -> None:
    """Nothing to one row is not news either. The floor applies whatever the baseline is."""
    counts = _series([0] * DEFAULT_WINDOW + [DEFAULT_MIN_ROWS - 1])

    assert step_changes(counts) == ()


# ── findings: what a step becomes when the daily job sees one ────────────────────────────────


def test_a_step_becomes_a_warn_finding_scoped_to_the_quarantine_dataset() -> None:
    """WARN, not ERROR — a slice of a session missing is news, not a reason to stop trading.

    Invariant #10 counts only ERROR flags, so raising this at ERROR would halt the whole platform
    on a delivery-join regression that leaves every price row intact.
    """
    from dataplatform.quality.quarantine import STEP_CHECK_NAME, findings_from_steps

    (step,) = step_changes(_series([100] * DEFAULT_WINDOW + [500]))
    (finding,) = findings_from_steps([step])

    assert finding.check_name == STEP_CHECK_NAME
    assert finding.severity == "WARN"
    assert finding.source == PRICES_RAW_QUARANTINE_DATASET
    assert finding.observed_value == Decimal(500)
    assert finding.threshold == Decimal(100)
    assert finding.detail["reason"] == UNRESOLVED
    assert finding.isin is None


def test_two_reasons_stepping_on_one_session_are_two_findings() -> None:
    """The fingerprint is per `(exchange, reason, date)`, so one session can raise more than one.

    Folding them into one flag per date would let a second, different break hide behind the first.
    """
    from dataplatform.quality.quarantine import findings_from_steps

    both = _series([100] * DEFAULT_WINDOW + [500], reason=UNRESOLVED) + _series(
        [100] * DEFAULT_WINDOW + [500], reason=ORPHANED
    )
    findings = findings_from_steps(step_changes(both))

    assert len(findings) == 2
    assert len({finding.fingerprint for finding in findings}) == 2
