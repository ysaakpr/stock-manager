"""M7.4 — fundamentals into break conditions and T1/T2 evidence (§5.3 BC1, §5.4, invariant #8).

Organised by the task's three acceptance criteria:

* **Acceptance 1 — the two-consecutive-quarters segment-decline condition evaluates correctly
  against PIT data.** The mechanical evaluator returns BROKEN for two consecutive quarter-over-
  quarter declines, WEAKENED for a single one, INTACT for a flat/rising or too-short series; it
  reads point-in-time (a quarter filed after the decision date does not count, a restatement the
  market had seen does), and it honours *consecutive* — two declines with an unfiled quarter between
  them do not meet the condition.

* **Acceptance 2 — evidence bundles label every fundamental datum with its store and dates.** A PIT
  fact becomes an evidence item stamped with the PIT store name and its `(period_end, filing_date)`;
  a restated datum becomes one stamped with the `RESTATED` store name and its period. A bundle
  carrying both can tell them apart by store.

* **Acceptance 3 — break-condition evaluation cannot read the restated store.** The evaluator reads
  the PIT store only: a declining series written into the *restated* store, with the PIT store flat,
  yields INTACT — the restated decline is invisible to the break condition. The query context the
  evaluator's data comes from (the backtest catalog) has no restated reader at all.

Offline and deterministic: every byte read here is written into `tmp_path` by the test.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final, NamedTuple

import pytest

from analyst.journal.evidence import EvidenceBundle, EvidenceKind
from analyst.journal.models import Actor, Verdict
from analyst.monitor.fundamentals import (
    PIT_STORE,
    QUARTER_GAP_DAYS,
    evaluate_segment_revenue_decline,
    fundamental_evidence,
    pit_evidence_item,
    restated_evidence_item,
    segments_disclosed,
)
from dataplatform.clock import IST
from dataplatform.ingest.screener import SCREENER_SOURCE_ID, SCREENER_SOURCE_TAG
from dataplatform.ingest.xbrl import (
    SEGMENT_CONCEPT,
    Filing,
    FundamentalFact,
    Nature,
    Taxonomy,
)
from dataplatform.query.quarantine import (
    QuarantineError,
    backtest_catalog,
    monitoring_catalog,
)
from dataplatform.store.pit_fundamentals import write_pit
from dataplatform.store.restated import RESTATED_ROOT_NAME, RestatedFundamental, RestatedStore

ISIN: Final = "INE009A01021"
SEGMENT: Final = "Robotics"
FETCHED_AT: Final = datetime(2026, 8, 7, 19, 30, tzinfo=IST)

#: A far-future decision date so every filing written below is knowable, unless a test narrows it.
AFTER_ALL: Final = date(2025, 3, 1)


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────


class _Quarter(NamedTuple):
    """The three dates that place one filing in time — a quarter's period and its filing date."""

    period_start: date
    period_end: date
    filing_date: date


def _segment_filing(
    quarter: _Quarter,
    *,
    value: str,
    filing_id: str,
    isin: str = ISIN,
    segment: str = SEGMENT,
    nature: Nature = Nature.CONSOLIDATED,
) -> Filing:
    """One results filing reporting one segment's revenue for one quarter."""
    fact = FundamentalFact(
        isin=isin,
        period_start=quarter.period_start,
        period_end=quarter.period_end,
        filing_date=quarter.filing_date,
        nature=nature,
        filing_id=filing_id,
        concept=SEGMENT_CONCEPT,
        segment=segment,
        value=Decimal(value),
        source="nse_filings",
        l0_key=f"nse_filings/{quarter.filing_date.isoformat()}/{filing_id}.xml",
    )
    return Filing(
        isin=isin,
        symbol="SEGCO",
        taxonomy=Taxonomy.IND_AS,
        name="Segment Co",
        period_start=quarter.period_start,
        period_end=quarter.period_end,
        filing_date=quarter.filing_date,
        nature=nature,
        filing_id=filing_id,
        source="nse_filings",
        l0_key=f"nse_filings/{quarter.filing_date.isoformat()}/{filing_id}.xml",
        facts=(fact,),
    )


#: The three standard quarters used across the decline tests: each quarter its own filing.
_Q1 = _Quarter(date(2024, 1, 1), date(2024, 3, 31), date(2024, 5, 3))
_Q2 = _Quarter(date(2024, 4, 1), date(2024, 6, 30), date(2024, 8, 5))
_Q3 = _Quarter(date(2024, 7, 1), date(2024, 9, 30), date(2024, 11, 4))


def _write_series(root: Path, values: tuple[str, str, str]) -> None:
    """Write Q1/Q2/Q3 segment revenue with the three given values into the PIT store at `root`."""
    for quarter, value, suffix in zip((_Q1, _Q2, _Q3), values, ("Q1", "Q2", "Q3"), strict=True):
        write_pit(
            _segment_filing(quarter, value=value, filing_id=f"SEGCO-2024{suffix}-CON"),
            data_root=root,
        )


# ── acceptance 1: the condition evaluates correctly against PIT data ──────────────────────────────


def test_two_consecutive_declines_is_broken(tmp_path: Path) -> None:
    """1000 → 900 → 800: two consecutive quarter-over-quarter declines meet BC1 (BROKEN)."""
    _write_series(tmp_path, ("1000.00", "900.00", "800.00"))
    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    assert result.verdict is Verdict.BROKEN
    assert result.trailing_declines == 2
    assert len(result.quarters) == 3
    assert "BROKEN" in result.observed


def test_a_single_decline_is_weakened_not_broken(tmp_path: Path) -> None:
    """1000 → 1100 → 900: only the most recent quarter declines — WEAKENED, the condition unmet."""
    _write_series(tmp_path, ("1000.00", "1100.00", "900.00"))
    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    assert result.verdict is Verdict.WEAKENED
    assert result.trailing_declines == 1


def test_a_rising_series_is_intact(tmp_path: Path) -> None:
    """800 → 900 → 1000: revenue rising, the condition is nowhere near met (INTACT)."""
    _write_series(tmp_path, ("800.00", "900.00", "1000.00"))
    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    assert result.verdict is Verdict.INTACT
    assert result.trailing_declines == 0


def test_a_recovered_decline_does_not_count(tmp_path: Path) -> None:
    """1000 → 800 → 900: a decline two quarters ago has reversed — the trailing run is not two."""
    _write_series(tmp_path, ("1000.00", "800.00", "900.00"))
    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    assert result.verdict is Verdict.INTACT
    assert result.trailing_declines == 0


def test_a_flat_quarter_breaks_the_decline_run(tmp_path: Path) -> None:
    """1000 → 900 → 900: the last step is flat, not a decline; the run of declines is not two."""
    _write_series(tmp_path, ("1000.00", "900.00", "900.00"))
    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    assert result.verdict is Verdict.INTACT
    assert result.trailing_declines == 0


def test_one_quarter_is_intact_for_lack_of_history(tmp_path: Path) -> None:
    """A single filed quarter cannot show a quarter-over-quarter decline — INTACT, not an error."""
    write_pit(
        _segment_filing(_Q1, value="1000.00", filing_id="SEGCO-2024Q1-CON"), data_root=tmp_path
    )
    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    assert result.verdict is Verdict.INTACT
    assert result.trailing_declines == 0
    assert len(result.quarters) == 1


def test_no_data_is_intact(tmp_path: Path) -> None:
    """No filed segment revenue at all: the condition cannot be met, so INTACT with a plain note."""
    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    assert result.verdict is Verdict.INTACT
    assert result.quarters == ()
    assert "cannot be met" in result.observed


def test_a_missing_quarter_prevents_broken(tmp_path: Path) -> None:
    """1200(Q1) → [Jun missing] → 1000(Q3) → 800(Q4): the gap means it is not TWO consecutive
    quarters of decline. The trailing consecutive run is Q3→Q4 only, so the verdict is WEAKENED
    (one decline), never BROKEN across the unfiled quarter."""
    write_pit(
        _segment_filing(_Q1, value="1200.00", filing_id="SEGCO-2024Q1-CON"), data_root=tmp_path
    )
    write_pit(
        _segment_filing(_Q3, value="1000.00", filing_id="SEGCO-2024Q3-CON"), data_root=tmp_path
    )
    write_pit(
        _segment_filing(
            _Quarter(date(2024, 10, 1), date(2024, 12, 31), date(2025, 2, 3)),
            value="800.00",
            filing_id="SEGCO-2024Q4-CON",
        ),
        data_root=tmp_path,
    )
    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    assert result.verdict is Verdict.WEAKENED
    assert result.trailing_declines == 1
    assert len(result.quarters) == 2  # only the consecutive Q3→Q4 pair


def test_point_in_time_a_later_quarter_is_not_yet_knowable(tmp_path: Path) -> None:
    """The inverted-logic guard: BROKEN depends on the third quarter, filed 2024-11-04.

    As of a decision date before that filing, only two quarters are knowable (one decline →
    WEAKENED); as of a date after it, all three are (two declines → BROKEN). A break condition that
    read the future would return BROKEN in both — this proves it does not (invariant #7)."""
    _write_series(tmp_path, ("1000.00", "900.00", "800.00"))

    before_q3 = evaluate_segment_revenue_decline(
        ISIN, SEGMENT, date(2024, 8, 10), data_root=tmp_path
    )
    assert before_q3.verdict is Verdict.WEAKENED
    assert len(before_q3.quarters) == 2

    after_q3 = evaluate_segment_revenue_decline(
        ISIN, SEGMENT, date(2024, 11, 10), data_root=tmp_path
    )
    assert after_q3.verdict is Verdict.BROKEN
    assert len(after_q3.quarters) == 3


def test_a_restatement_the_market_had_seen_changes_the_verdict(tmp_path: Path) -> None:
    """read_latest is used, so a later filing restating Q2 upward is what the verdict rests on.

    Original Q2 = 900 (1000 → 900 → 800 would be BROKEN). A later filing restates Q2 to 1100, so the
    knowable series becomes 1000 → 1100 → 800: only one trailing decline, WEAKENED. The evaluator
    follows the latest-knowable value, not the original — a restatement the market had seen."""
    _write_series(tmp_path, ("1000.00", "900.00", "800.00"))
    # A later filing restates the same Q2 period upward; distinct filing_id, later filing_date.
    write_pit(
        _segment_filing(
            _Quarter(date(2024, 4, 1), date(2024, 6, 30), date(2024, 10, 1)),
            value="1100.00",
            filing_id="SEGCO-2024Q2-CON-R1",
        ),
        data_root=tmp_path,
    )
    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    assert result.verdict is Verdict.WEAKENED
    assert result.trailing_declines == 1


def test_standalone_and_consolidated_are_not_mixed(tmp_path: Path) -> None:
    """`nature` is part of a fact's identity: a consolidated decline is judged on consolidated data.

    Consolidated declines (1000 → 900 → 800, BROKEN); standalone is flat. Asking for each nature
    returns its own verdict — the two are never conflated."""
    _write_series(tmp_path, ("1000.00", "900.00", "800.00"))
    for quarter, suffix in zip((_Q1, _Q2, _Q3), ("Q1", "Q2", "Q3"), strict=True):
        write_pit(
            _segment_filing(
                quarter,
                value="500.00",
                filing_id=f"SEGCO-2024{suffix}-STD",
                nature=Nature.STANDALONE,
            ),
            data_root=tmp_path,
        )

    consolidated = evaluate_segment_revenue_decline(
        ISIN, SEGMENT, AFTER_ALL, nature=Nature.CONSOLIDATED, data_root=tmp_path
    )
    standalone = evaluate_segment_revenue_decline(
        ISIN, SEGMENT, AFTER_ALL, nature=Nature.STANDALONE, data_root=tmp_path
    )
    assert consolidated.verdict is Verdict.BROKEN
    assert standalone.verdict is Verdict.INTACT


def test_an_annual_period_is_excluded_from_the_quarterly_trend(tmp_path: Path) -> None:
    """A full-year segment disclosure is not a quarter and must not enter the QoQ series.

    The three quarters rise (INTACT); an extra annual (12-month) disclosure with a low value would,
    if wrongly treated as a quarter, manufacture a decline. It is excluded, so the verdict stays
    INTACT."""
    _write_series(tmp_path, ("800.00", "900.00", "1000.00"))
    write_pit(
        _segment_filing(
            _Quarter(date(2023, 4, 1), date(2024, 3, 31), date(2024, 5, 30)),
            value="100.00",
            filing_id="SEGCO-FY2024-CON",
        ),
        data_root=tmp_path,
    )
    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    assert result.verdict is Verdict.INTACT


def test_quarter_gap_band_covers_real_indian_quarter_ends() -> None:
    """A sanity check on the consecutiveness band: adjacent Indian quarter ends fall inside it."""
    low, high = QUARTER_GAP_DAYS
    for earlier, later in (
        (date(2024, 3, 31), date(2024, 6, 30)),
        (date(2024, 6, 30), date(2024, 9, 30)),
        (date(2024, 9, 30), date(2024, 12, 31)),
        (date(2024, 12, 31), date(2025, 3, 31)),
    ):
        assert low <= (later - earlier).days <= high
    # And a half-year jump is outside it, so it would end a run rather than count as one quarter.
    assert (date(2024, 9, 30) - date(2024, 3, 31)).days > high


def test_as_evaluation_carries_the_verdict_for_the_journal(tmp_path: Path) -> None:
    """The result renders to a `BreakConditionEvaluation` keyed by the thesis break-condition id."""
    _write_series(tmp_path, ("1000.00", "900.00", "800.00"))
    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    evaluation = result.as_evaluation("BC1")
    assert evaluation.id == "BC1"
    assert evaluation.verdict is Verdict.BROKEN
    assert evaluation.observed == result.observed


def test_segments_disclosed_lists_the_pit_segments(tmp_path: Path) -> None:
    """The evaluator's companion lists the segments a company disclosed, from the PIT store."""
    write_pit(
        _segment_filing(_Q1, value="1000.00", filing_id="SEGCO-2024Q1-CON"), data_root=tmp_path
    )
    write_pit(
        _segment_filing(_Q1, value="500.00", filing_id="SEGCO-2024Q1-CON-EMS", segment="EMS"),
        data_root=tmp_path,
    )
    assert segments_disclosed(ISIN, AFTER_ALL, data_root=tmp_path) == ("EMS", "Robotics")


# ── acceptance 2: evidence bundles label every fundamental datum with its store and dates ─────────


def test_pit_evidence_item_carries_store_and_both_dates(tmp_path: Path) -> None:
    """A PIT fact becomes an evidence item stamped with the PIT store and its two dates."""
    filing = _segment_filing(_Q1, value="1000.00", filing_id="SEGCO-2024Q1-CON")
    item = pit_evidence_item(filing.facts[0])
    assert item.kind is EvidenceKind.FUNDAMENTAL
    assert item.source == PIT_STORE
    assert item.detail["store"] == PIT_STORE
    assert item.detail["period_end"] == "2024-03-31"
    assert item.detail["filing_date"] == "2024-05-03"
    assert item.detail["segment"] == SEGMENT
    assert item.value == Decimal("1000.00")
    # knowable_at is the filing date, tz-aware, so the item stays PIT-auditable.
    assert item.knowable_at is not None
    assert item.knowable_at.date() == date(2024, 5, 3)
    assert item.knowable_at.tzinfo is not None


def test_restated_evidence_item_carries_the_restated_store_label(tmp_path: Path) -> None:
    """A restated datum (read via the monitoring catalog) is labelled with the RESTATED store."""
    _write_restated(tmp_path, ("900.00", "800.00"))
    with monitoring_catalog(data_root=tmp_path) as monitoring:
        data = monitoring.read_restated(ISIN)
    item = restated_evidence_item(data[0])
    assert item.kind is EvidenceKind.FUNDAMENTAL
    assert item.source == RESTATED_ROOT_NAME
    assert item.detail["store"] == RESTATED_ROOT_NAME
    assert item.detail["source"] == SCREENER_SOURCE_TAG
    assert "period" in item.detail


def test_fundamental_evidence_labels_a_mixed_bundle_by_store(tmp_path: Path) -> None:
    """A bundle carrying both a PIT figure and a restated figure tells them apart by store label.

    This is acceptance 2 end-to-end: a T2 monitoring bundle may legitimately hold both a point-in-
    time filing figure and a restated one, and every fundamental item names the store it came from,
    so the model (and the §5.7 evidence pack) never mistakes a restated number for PIT knowledge."""
    pit_filing = _segment_filing(_Q1, value="1000.00", filing_id="SEGCO-2024Q1-CON")
    _write_restated(tmp_path, ("900.00",))
    with monitoring_catalog(data_root=tmp_path) as monitoring:
        restated = monitoring.read_restated(ISIN)

    items = fundamental_evidence(pit=(pit_filing.facts[0],), restated=restated)
    bundle = EvidenceBundle(trading_date=date(2026, 8, 7), actor=Actor.T2, items=items)

    assert len(bundle.items) == 2
    stores = {item.detail["store"] for item in bundle.items}
    assert stores == {PIT_STORE, RESTATED_ROOT_NAME}
    # The bundle content-addresses cleanly with the labelled items (no float leaks into the hash).
    assert bundle.ref().item_count == 2


# ── acceptance 3: break-condition evaluation cannot read the restated store ───────────────────────


def test_break_condition_ignores_a_decline_that_lives_only_in_restated(tmp_path: Path) -> None:
    """The core quarantine test: a decline written into the RESTATED store is invisible to BC1.

    The PIT store shows a rising segment revenue (INTACT). The restated store, for the same ISIN,
    holds a steeply declining series. The evaluator reads the PIT store only, so it returns INTACT —
    the restated decline never reaches the break condition (invariant #8, acceptance 3)."""
    _write_series(tmp_path, ("800.00", "900.00", "1000.00"))  # PIT: rising → INTACT
    _write_restated(tmp_path, ("1000.00", "500.00", "100.00"))  # restated: crashing

    result = evaluate_segment_revenue_decline(ISIN, SEGMENT, AFTER_ALL, data_root=tmp_path)
    assert result.verdict is Verdict.INTACT
    # The restated crash IS on disk — the evaluator simply cannot see it (quarantine, not absence).
    stored = RestatedStore(data_root=tmp_path).read_isin(ISIN)
    assert len(stored) == 3


def test_the_evaluation_context_has_no_restated_reader(tmp_path: Path) -> None:
    """The query context break-condition evaluation draws PIT data from cannot read restated at all.

    A backtest catalog — the PIT/backtest reader — raises `QuarantineError` on any restated read,
    because the capability was never constructed for it. That absence is the structural half of
    acceptance 3: break-condition evaluation runs where there is no receiver for a restated read."""
    _write_restated(tmp_path, ("900.00",))
    with backtest_catalog(data_root=tmp_path) as backtest, pytest.raises(QuarantineError):
        backtest.read_restated(ISIN)


def _write_restated(root: Path, values: tuple[str, ...]) -> None:
    """Write a restated series for ISIN into the RESTATED store — data that must never reach BC1."""
    periods = ("Mar 2024", "Jun 2024", "Sep 2024", "Dec 2024")
    rows = tuple(
        RestatedFundamental(
            isin=ISIN,
            symbol="SEGCO",
            statement="segments",
            metric=f"{SEGMENT} Revenue",
            period=period,
            value=Decimal(value),
            source=SCREENER_SOURCE_TAG,
            l0_source=SCREENER_SOURCE_ID,
            l0_filename="SEGCO.html",
            l0_logical_date=date(2026, 8, 7),
            l0_sha256="a" * 64,
            fetched_at=FETCHED_AT,
        )
        for value, period in zip(values, periods, strict=False)
    )
    RestatedStore(data_root=root).write(rows)
