"""D7: L2 continuity — adjusted-series steps nothing explains, and partitions that should not exist.

The sentinel (`sentinel.py`) watches each new session's raw close-to-close move. This check watches
the *materialized* L2 history, the series the backtest and the query layer actually read, for the
two defects the 2026-10-05 data-quality audit found there and nothing had flagged:

* **An unexplained step.** `adj_close` moving by more than `threshold`x between consecutive
  sessions of one venue with nothing behind it. Back-adjustment exists to remove exactly these;
  one left in is an unadjusted split (ETF unit splits, pre-2016 splits no feed carried) that a
  momentum signal reads as a -90% return. The audit counted 96 across gaps of five days or less.
* **A retired-ISIN partition.** A partition for an ISIN a D2 lineage edge retired. Its bars belong
  to the survivor's stitched partition; a copy under the old ISIN is the same company twice, and
  its last bar — the ex-date session, which the exchange still prints under the old ISIN — is in
  post-split terms with no factor behind it. The audit found these too; on the server lake, 60.

**How a step is classified** (`StepClass`), in this order:

* `STRUCTURAL` — a recorded merger, demerger, scheme or DVR conversion within `guard_days`. A real
  change in what the security is; the level series keeps the gap by convention (§4.3 rule 3).
* `RECORDED_UNSCALED` — a recorded event the price-adjusted series does not scale by convention:
  a rights issue (no theoretical ex-rights factor yet — Sadhana Nitrochem's 8:1 at par,
  2026-02-18) or a dividend of at least a quarter of the prior close (Strides' ₹500 special,
  2013-12-19, where the total-return leg could not be built). Known, explained, not a defect here.
* `DIVIDEND` — the total-return close does not step: a distribution, reinvested there (Majesco's
  ₹974 on 2020-12-23), absent from the price-adjusted series by convention.
* `LONG_GAP` — the two bars are more than `max_gap_days` apart: a suspension, or months in the
  trade-to-trade series that L2's EQ-only view does not carry. A move across that can be genuine
  (DOLPHIN, UEL), so it is reported, never failed and never adjusted.
* `UNEXPLAINED` — everything else. These fail the check.

A step where the cumulative factor itself changed is still a step if it survives the factor — a
recorded ratio that is wrong, or a second event on the same day — and is classified the same way.

Pure core (`classify_steps`, `L2ContinuityReport`) and a DuckDB/Postgres seam (`scan`) that only
reads; `findings` turns a report into `QualityFinding`s for `persist_findings`, which only the
CLI's `--persist` calls, with the clock injected at that boundary (B10).

    uv run python -m dataplatform.quality.l2_continuity             # report; exit 1 if it fails
    uv run python -m dataplatform.quality.l2_continuity --persist   # also raise quality_flag rows
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

from dataplatform.logging import get_logger
from dataplatform.quality.sentinel import QualityFinding, finding_fingerprint
from dataplatform.store.db import Connection

__all__ = [
    "CHECK_NAME",
    "AdjustedStep",
    "L2ContinuityReport",
    "StepClass",
    "classify_steps",
    "findings",
    "scan",
]

_LOG = get_logger(__name__)

#: The `quality_flag.check_name` this check raises under.
CHECK_NAME: Final = "l2_continuity"

#: The dataset findings are scoped to, so an ERROR gates L2 readers and not the whole market.
_DATASET: Final = "prices_adjusted"

#: A recorded dividend at least this fraction of the raw close before it explains a step.
_LARGE_DIVIDEND: Final = Decimal("0.25")

#: Recorded action types that explain a level step (the factor module's structural breaks).
_STRUCTURAL_TYPES: Final = ("MERGER", "DEMERGER", "SCHEME_OF_ARRANGEMENT", "DVR_CONVERSION")


class StepClass(StrEnum):
    """Why an adjusted-close step is there — or that nothing says why."""

    STRUCTURAL = "STRUCTURAL"
    RECORDED_UNSCALED = "RECORDED_UNSCALED"
    DIVIDEND = "DIVIDEND"
    LONG_GAP = "LONG_GAP"
    UNEXPLAINED = "UNEXPLAINED"


@dataclass(frozen=True, slots=True)
class AdjustedStep:
    """One session-to-session `adj_close` change past the threshold, on one venue of one ISIN.

    `ratio` is `adj_close / prev_adj_close` (above 1 a rise); `tr_ratio` the same for `tr_close`.
    `factor_changed` is whether `cum_price_factor` differs across the pair.
    """

    isin: str
    exchange: str
    prev_date: date
    trade_date: date
    prev_close: Decimal
    close: Decimal
    ratio: Decimal
    tr_ratio: Decimal
    factor_changed: bool
    prev_raw_close: Decimal | None = None

    @property
    def gap_days(self) -> int:
        return (self.trade_date - self.prev_date).days


@dataclass(frozen=True, slots=True)
class L2ContinuityReport:
    """Every step past the threshold, classified, and the retired partitions on disk.

    `passed` is the check: no `UNEXPLAINED` step and no retired partition.
    """

    threshold: Decimal
    max_gap_days: int
    partitions: int
    steps: tuple[tuple[AdjustedStep, StepClass], ...]
    retired_partitions: tuple[str, ...]

    def of_class(self, cls: StepClass) -> tuple[AdjustedStep, ...]:
        return tuple(step for step, c in self.steps if c is cls)

    @property
    def unexplained(self) -> tuple[AdjustedStep, ...]:
        return self.of_class(StepClass.UNEXPLAINED)

    @property
    def passed(self) -> bool:
        return not self.unexplained and not self.retired_partitions


def classify_steps(
    steps: Iterable[AdjustedStep],
    *,
    structural_dates: Mapping[str, Iterable[date]],
    unscaled_dates: Mapping[str, Iterable[date]] | None = None,
    threshold: Decimal,
    max_gap_days: int,
    guard_days: int = 7,
) -> tuple[tuple[AdjustedStep, StepClass], ...]:
    """Classify each step (module docstring, in that order of precedence); pure.

    `structural_dates` maps an ISIN to the ex-dates of its recorded structural breaks;
    `unscaled_dates` to those of its recorded rights issues and large dividends.
    """
    guard = timedelta(days=guard_days)
    lower = 1 / threshold
    out: list[tuple[AdjustedStep, StepClass]] = []
    for step in sorted(steps, key=lambda s: (s.isin, s.exchange, s.trade_date)):
        breaks = structural_dates.get(step.isin, ())
        if any(abs(step.trade_date - d) <= guard for d in breaks):
            cls = StepClass.STRUCTURAL
        elif any(
            abs(step.trade_date - d) <= guard for d in (unscaled_dates or {}).get(step.isin, ())
        ):
            cls = StepClass.RECORDED_UNSCALED
        elif lower <= step.tr_ratio <= threshold:
            cls = StepClass.DIVIDEND
        elif step.gap_days > max_gap_days:
            cls = StepClass.LONG_GAP
        else:
            cls = StepClass.UNEXPLAINED
        out.append((step, cls))
    return tuple(out)


def findings(report: L2ContinuityReport, *, logical_date: date) -> tuple[QualityFinding, ...]:
    """One ERROR finding per unexplained step and per retired partition, for `persist_findings`.

    ERROR on purpose: an unadjusted split in L2 is a wrong return the backtest and every signal
    will trade on (invariant #10). A step's finding is dated by the step's own session, so a
    re-scan finds the flag it raised before rather than stacking another.
    """
    out: list[QualityFinding] = []
    for step in report.unexplained:
        out.append(
            QualityFinding(
                logical_date=step.trade_date,
                check_name=CHECK_NAME,
                severity="ERROR",
                isin=step.isin,
                source=_DATASET,
                observed_value=step.ratio,
                threshold=report.threshold,
                detail={
                    "kind": "unexplained_step",
                    "exchange": step.exchange,
                    "prev_date": step.prev_date.isoformat(),
                    "prev_adj_close": str(step.prev_close),
                    "adj_close": str(step.close),
                    "factor_changed": step.factor_changed,
                },
                fingerprint=finding_fingerprint(
                    f"{CHECK_NAME}:{step.exchange}", step.isin, step.trade_date
                ),
            )
        )
    for isin in report.retired_partitions:
        out.append(
            QualityFinding(
                logical_date=logical_date,
                check_name=CHECK_NAME,
                severity="ERROR",
                isin=isin,
                source=_DATASET,
                detail={"kind": "retired_isin_partition"},
                fingerprint=finding_fingerprint(f"{CHECK_NAME}:retired", isin, logical_date),
            )
        )
    return tuple(out)


# ── the read seam (DuckDB over L2, Postgres for recorded breaks) ─────────────────────────────


def scan(
    conn: Connection | None,
    *,
    survivor_of: Callable[[str], str],
    data_root: Path | None = None,
    threshold: Decimal = Decimal(2),
    max_gap_days: int = 5,
) -> L2ContinuityReport:
    """Scan the materialized L2 for steps past `threshold`x and for retired-ISIN partitions.

    Read-only: one DuckDB window pass over every partition, and (when `conn` is given) one
    Postgres read of the reconciled structural breaks — without it no step is `STRUCTURAL`.
    """
    from dataplatform.store.l2 import (
        materialized_isins,
        open_connection,
        register_adjusted_view,
    )

    partitions = materialized_isins(data_root=data_root)
    retired = tuple(sorted(i for i in partitions if survivor_of(i) != i))
    con = open_connection()
    try:
        register_adjusted_view(con, data_root=data_root)
        rows: Sequence[tuple[object, ...]] = (
            con.execute(
                """
                SELECT isin, exchange, pd, trade_date, pc, adj_close, ptr, tr_close, pf, f
                FROM (
                    SELECT isin, exchange, trade_date, adj_close, tr_close,
                           cum_price_factor AS f,
                           lag(trade_date) OVER w AS pd,
                           lag(adj_close) OVER w AS pc,
                           lag(tr_close) OVER w AS ptr,
                           lag(cum_price_factor) OVER w AS pf
                    FROM prices_adjusted
                    WINDOW w AS (PARTITION BY isin, exchange ORDER BY trade_date)
                )
                WHERE pd IS NOT NULL AND (adj_close > $t * pc OR adj_close * $t < pc)
                """,
                {"t": threshold},
            ).fetchall()
            if partitions
            else []
        )
    finally:
        con.close()
    steps = [
        AdjustedStep(
            isin=str(r[0]),
            exchange=str(r[1]),
            prev_date=_as_date(r[2]),
            trade_date=_as_date(r[3]),
            prev_close=_as_decimal(r[4]),
            close=_as_decimal(r[5]),
            ratio=_as_decimal(r[5]) / _as_decimal(r[4]),
            tr_ratio=_as_decimal(r[7]) / _as_decimal(r[6]),
            factor_changed=r[8] != r[9],
            prev_raw_close=_as_decimal(r[4]) / _as_decimal(r[8]),
        )
        for r in rows
    ]
    structural: dict[str, list[date]] = {}
    if conn is not None and steps:
        for isin, ex_date in conn.execute(
            "SELECT DISTINCT isin, ex_date FROM corporate_actions "
            "WHERE reconciled AND action_type = ANY(%s) AND isin = ANY(%s)",
            (list(_STRUCTURAL_TYPES), sorted({s.isin for s in steps})),
        ).fetchall():
            structural.setdefault(str(isin), []).append(_as_date(ex_date))
    unscaled: dict[str, list[date]] = {}
    if conn is not None and steps:
        prior = {(s.isin, s.trade_date): s.prev_raw_close for s in steps}
        for isin, ex_date, kind, amount in conn.execute(
            "SELECT DISTINCT isin, ex_date, action_type, dividend_amount_inr "
            "FROM corporate_actions WHERE reconciled AND action_type IN ('RIGHTS', 'DIVIDEND') "
            "AND isin = ANY(%s)",
            (sorted({s.isin for s in steps}),),
        ).fetchall():
            day = _as_date(ex_date)
            if kind == "DIVIDEND":
                near = [
                    c
                    for (i, d), c in prior.items()
                    if i == isin and c is not None and abs((d - day).days) <= 7
                ]
                if amount is None or not near or Decimal(amount) < _LARGE_DIVIDEND * near[0]:
                    continue
            unscaled.setdefault(str(isin), []).append(day)
    report = L2ContinuityReport(
        threshold=threshold,
        max_gap_days=max_gap_days,
        partitions=len(partitions),
        steps=classify_steps(
            steps,
            structural_dates=structural,
            unscaled_dates=unscaled,
            threshold=threshold,
            max_gap_days=max_gap_days,
        ),
        retired_partitions=retired,
    )
    _LOG.info(
        "quality.l2_continuity",
        dataset=_DATASET,
        partitions=report.partitions,
        steps=len(report.steps),
        **{c.value.lower(): len(report.of_class(c)) for c in StepClass},
        retired_partitions=len(report.retired_partitions),
        state="GREEN" if report.passed else "RED",
    )
    return report


def _as_date(value: object) -> date:
    if not isinstance(value, date):
        raise TypeError(f"expected a date from the L2 scan, got {type(value).__name__}")
    return value


def _as_decimal(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"expected a Decimal from the L2 scan, got {type(value).__name__}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: print the classified counts and every unexplained step; exit 1 when the check fails."""
    from dataplatform.clock import SystemClock
    from dataplatform.config import get_settings
    from dataplatform.identity.lineage import LineageStore
    from dataplatform.quality.sentinel import persist_findings
    from dataplatform.store.db import connect

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=Decimal, default=Decimal(2))
    parser.add_argument("--max-gap-days", type=int, default=5)
    parser.add_argument("--persist", action="store_true", help="raise quality_flag rows")
    args = parser.parse_args(argv)
    settings = get_settings()
    clock = SystemClock()
    with connect() as conn:
        resolver = LineageStore(conn).load()
        report = scan(
            conn,
            survivor_of=resolver.survivor_of,
            data_root=settings.data_root,
            threshold=args.threshold,
            max_gap_days=args.max_gap_days,
        )
        if args.persist:
            persist_findings(conn, findings(report, logical_date=clock.today()), clock=clock)
            conn.commit()
    print(f"{'partitions':<22} {report.partitions}")
    for cls in StepClass:
        print(f"{cls.value.lower():<22} {len(report.of_class(cls))}")
    print(f"{'retired_partitions':<22} {len(report.retired_partitions)}")
    for step in report.unexplained:
        print(
            f"UNEXPLAINED {step.isin} {step.exchange} {step.prev_date} -> {step.trade_date} "
            f"{step.prev_close} -> {step.close} (x{step.ratio:.4f})"
        )
    for isin in report.retired_partitions:
        print(f"RETIRED {isin}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
