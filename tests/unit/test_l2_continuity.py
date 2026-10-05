"""D7 — L2 continuity: unexplained adjusted-close steps and retired-ISIN partitions fail the check.

The 2026-10-05 audit found 96 short-gap >2x steps and 60 retired-ISIN partitions in L2 that no
check had flagged. These tests pin the classification down so each class lands where it belongs —
and fail if it is inverted: an unexplained step that passed, or an explained one that failed,
would each break an assertion here. Offline: synthetic L2 under `tmp_path`, no Postgres.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from dataplatform.corpactions.factors import FactorChain
from dataplatform.identity.master import Exchange
from dataplatform.ingest.models import PriceRow
from dataplatform.quality.l2_continuity import (
    CHECK_NAME,
    AdjustedStep,
    StepClass,
    classify_steps,
    findings,
    scan,
)
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import materialize_isin

STOCK = "INE002A01018"
RETIRED = "INE296A01016"
SURVIVOR = "INE296A01024"


def _step(
    *, ratio: str, tr_ratio: str | None = None, gap: int = 1, day: date = date(2020, 1, 2)
) -> AdjustedStep:
    r = Decimal(ratio)
    return AdjustedStep(
        isin=STOCK,
        exchange="NSE",
        prev_date=day - timedelta(days=gap),
        trade_date=day,
        prev_close=Decimal(100),
        close=Decimal(100) * r,
        ratio=r,
        tr_ratio=r if tr_ratio is None else Decimal(tr_ratio),
        factor_changed=False,
    )


def _classify(step: AdjustedStep, breaks: tuple[date, ...] = ()) -> StepClass:
    [(_, cls)] = classify_steps(
        [step], structural_dates={STOCK: breaks}, threshold=Decimal(2), max_gap_days=5
    )
    return cls


def test_a_short_gap_step_with_nothing_behind_it_is_unexplained() -> None:
    assert _classify(_step(ratio="0.1")) is StepClass.UNEXPLAINED
    assert _classify(_step(ratio="10")) is StepClass.UNEXPLAINED


def test_a_recorded_structural_break_explains_a_step() -> None:
    assert _classify(_step(ratio="0.35"), (date(2020, 1, 2),)) is StepClass.STRUCTURAL
    # ...but only near its own ex-date.
    assert _classify(_step(ratio="0.35"), (date(2020, 3, 2),)) is StepClass.UNEXPLAINED


def test_a_recorded_rights_issue_or_large_dividend_is_recorded_unscaled() -> None:
    [(_, cls)] = classify_steps(
        [_step(ratio="0.3")],
        structural_dates={},
        unscaled_dates={STOCK: (date(2020, 1, 2),)},
        threshold=Decimal(2),
        max_gap_days=5,
    )
    assert cls is StepClass.RECORDED_UNSCALED


def test_a_step_the_total_return_series_does_not_show_is_a_dividend() -> None:
    assert _classify(_step(ratio="0.0124", tr_ratio="1.01")) is StepClass.DIVIDEND


def test_a_step_across_a_long_gap_is_classified_not_failed() -> None:
    assert _classify(_step(ratio="13.4", gap=222)) is StepClass.LONG_GAP


def _write(root: Path, rows: list[tuple[str, date, str, int]]) -> None:
    by_date: dict[date, list[PriceRow]] = {}
    for isin, day, close, qty in rows:
        c = Decimal(close)
        by_date.setdefault(day, []).append(
            PriceRow(
                isin=isin,
                symbol=isin[:6],
                series="EQ",
                trade_date=day,
                open=c,
                high=c,
                low=c,
                close=c,
                last=c,
                prev_close=c,
                total_traded_qty=qty,
                total_traded_value=c * qty,
                total_trades=1,
            )
        )
    for day_rows in by_date.values():
        write_prices_raw(day_rows, exchange=Exchange.NSE, data_root=root)


def _sessions(n: int) -> list[date]:
    out: list[date] = []
    day = date(2019, 12, 2)
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def test_scan_flags_an_unadjusted_split_and_a_retired_partition(tmp_path: Path) -> None:
    days = _sessions(13)
    rows = [(STOCK, d, "1000", 5_000) for d in days[:-1]] + [(STOCK, days[-1], "100", 80_000)]
    rows += [(i, days[0], "50", 10) for i in (RETIRED, SURVIVOR)]
    _write(tmp_path, rows)
    for isin in (STOCK, RETIRED, SURVIVOR):
        materialize_isin(
            isin,
            chain=FactorChain(isin=isin, rows=()),
            actions=(),
            data_root=tmp_path,
            infer_splits=False,  # L2 as it was before implied splits: the step is still there
        )

    def survivor_of(isin: str) -> str:
        return SURVIVOR if isin == RETIRED else isin

    report = scan(None, survivor_of=survivor_of, data_root=tmp_path)
    assert not report.passed
    assert [s.trade_date for s in report.unexplained] == [days[-1]]
    assert report.retired_partitions == (RETIRED,)
    flagged = findings(report, logical_date=date(2026, 10, 5))
    assert {f.check_name for f in flagged} == {CHECK_NAME}
    assert {f.severity for f in flagged} == {"ERROR"}
    assert sorted(f.isin or "" for f in flagged) == sorted((STOCK, RETIRED))
    assert len({f.fingerprint for f in flagged}) == 2

    # Rebuilt the way L2 is now built, the split is adjusted and the check passes.
    materialize_isin(STOCK, chain=FactorChain(isin=STOCK, rows=()), actions=(), data_root=tmp_path)
    (tmp_path / "L2" / "prices_adjusted" / f"isin={RETIRED}" / "part.parquet").unlink()
    healed = scan(None, survivor_of=survivor_of, data_root=tmp_path)
    assert healed.passed
    assert healed.steps == ()


def test_scan_of_an_empty_lake_passes(tmp_path: Path) -> None:
    report = scan(None, survivor_of=lambda i: i, data_root=tmp_path)
    assert report.passed
    assert report.partitions == 0
