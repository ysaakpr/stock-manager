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

* `STRUCTURAL` — a recorded merger, demerger, scheme or DVR conversion dated inside the step's
  gap or within `guard_days` of either end (from the reconciled store, or a curated, sourced one
  in `corpactions.manual_actions`). A real change in what the security is; the level series keeps
  the gap by convention (§4.3 rule 3). Inside the gap since 2026-11-01: a demerger often suspends
  the stock for a fortnight (Raymond, 2025-05-13 -> 05-28 around its 05-14 ex-date), and the old
  seven-days-from-the-step window missed it. `RECORDED_UNSCALED` is matched the same way.
* `RECORDED_UNSCALED` — a recorded event the price-adjusted series does not scale by convention:
  a rights issue (no theoretical ex-rights factor yet — Sadhana Nitrochem's 8:1 at par,
  2026-02-18) or a dividend of at least a quarter of the prior close (Strides' ₹500 special,
  2013-12-19, where the total-return leg could not be built). Known, explained, not a defect here.
* `EXPLAINED_MOVE` — a genuine market move documented in the curated allowlist
  (`corpactions.manual_actions`, `explained_moves`) for this exact ISIN and session, with its
  reason and source: YES Bank on the 2020-03-06 moratorium, Financial Technologies on the NSEL
  suspension. Never adjusted — a factor there would erase a real loss from every backtest.
* `UNSOURCED` — a share-basis step the curated file records but cannot adjust, because no L0
  object states its terms (`explained_moves` of an `UNSOURCED_*` kind: EIH 2006-09-12, JM
  Financial 2008-09-08, Shah Alloys 2006-02-13). Matched on the exact ISIN and session. It does
  not fail the check (the step is known and documented), but it is *not* explained: the level
  series is wrong across it, so `findings` raises a WARN for each one, and the query layer
  quarantines the ISIN's bars before it (D22). Never adjusted — that would invent a ratio.
* `DIVIDEND` — the total-return close does not step: a distribution, reinvested there (Majesco's
  ₹974 on 2020-12-23), absent from the price-adjusted series by convention.
* `SHARE_BASIS` — a step that looks like a change in share basis nothing explains: one across a
  D2 lineage reissue (any gap), or one across a long gap of at most
  `LONG_GAP_SHARE_BASIS_MAX_DAYS` at a clean split multiple (`CLEAN_MULTIPLES`, within
  `corpactions.implied`'s bands). AvenuesAI's 2026-10-12 -> 10-19
  consolidation (x9.40 over a seven-day suspension) was silently `LONG_GAP` before 2026-11-01.
  Never adjusted from price alone — a suspension can hide a real move — so it fails the check
  (ERROR, kind `share_basis_step`) and the share-basis guard (`quality.share_basis`) quarantines
  the ISIN's bars before it until a recorded or admitted factor explains it.
* `LONG_GAP` — the two bars are more than `max_gap_days` apart: a suspension, or months in the
  trade-to-trade series that L2's EQ-only view does not carry. A move across that can be genuine
  (DOLPHIN, UEL), so it is reported, never failed and never adjusted — unless it is
  `SHARE_BASIS`-shaped.
* `UNEXPLAINED` — everything else. These fail the check.

A step where the cumulative factor itself changed is still a step if it survives the factor — a
recorded ratio that is wrong, or a second event on the same day — and is classified the same way.

Pure core (`classify_steps`, `L2ContinuityReport`) and a DuckDB/Postgres seam (`scan`) that only
reads; `findings` turns a report into `QualityFinding`s for `persist_findings`, which only the
CLI's `--persist` calls, with the clock injected at that boundary (B10).

**A flag closes when a later scan no longer raises it.** `--persist` hands `persist_findings` the
report's `resolution_scope`, so every open `l2_continuity` flag the scan covered and did not raise
again is resolved — a curation or a rebuild answered it. Until 2026-10-23 nothing closed them: the
14 ERROR flags raised at 22:21 IST on 2026-10-05, 33 minutes after commit `29ce597` curated all 14
steps, were still open weeks later against a lake that scanned 0 unexplained. A scan over some
ISINs (`--isin`) covers those ISINs only and never closes a flag about another; a scan at a
non-default threshold or gap covers nothing, since it cannot say what the default one would raise.

    uv run python -m dataplatform.quality.l2_continuity             # report; exit 1 if it fails
    uv run python -m dataplatform.quality.l2_continuity --persist   # raise new flags, close stale
    uv run python -m dataplatform.quality.l2_continuity --persist --isin INE...  # those ISINs only
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

from dataplatform.corpactions.implied import CANDIDATE_MULTIPLES
from dataplatform.corpactions.manual_actions import ManualActions, default_manual_actions
from dataplatform.logging import get_logger
from dataplatform.quality.sentinel import FindingScope, QualityFinding, finding_fingerprint
from dataplatform.store.db import Connection

__all__ = [
    "CHECK_NAME",
    "CLEAN_MULTIPLES",
    "DEFAULT_MAX_GAP_DAYS",
    "DEFAULT_THRESHOLD",
    "LONG_GAP_MULTIPLE_TOLERANCE",
    "LONG_GAP_SHARE_BASIS_MAX_DAYS",
    "LONG_GAP_WIDE_MULTIPLE_TOLERANCE",
    "AdjustedStep",
    "L2ContinuityReport",
    "StepClass",
    "classify_steps",
    "clean_multiple",
    "findings",
    "scan",
    "share_basis_reason",
]

_LOG = get_logger(__name__)

#: The `quality_flag.check_name` this check raises under.
CHECK_NAME: Final = "l2_continuity"

#: The dataset findings are scoped to, so an ERROR gates L2 readers and not the whole market.
_DATASET: Final = "prices_adjusted"

#: The check as configured: a step past 2x either way, between bars at most five days apart. A
#: scan run any other way is a probe — it may raise flags, but it closes none (`resolution_scope`).
DEFAULT_THRESHOLD: Final = Decimal(2)
DEFAULT_MAX_GAP_DAYS: Final = 5

#: The date a retired-partition finding's fingerprint carries. The finding is about the partition,
#: not a day, so every scan must find the flag the first one raised rather than open another.
_RETIRED_FINGERPRINT_DATE: Final = date(1970, 1, 1)

#: A recorded dividend at least this fraction of the raw close before it explains a step.
_LARGE_DIVIDEND: Final = Decimal("0.25")

#: The share multiples a split or consolidation re-bases at (`corpactions.implied`'s candidates).
CLEAN_MULTIPLES: Final[tuple[Decimal, ...]] = tuple(sorted({a / b for a, b in CANDIDATE_MULTIPLES}))

#: How far a long-gap step may sit from a clean multiple and still read as a share-basis change:
#: `corpactions.implied`'s own bands, 3% below 5x and 6% from 5x up (AvenuesAI: x9.40 against 10
#: over seven days, 5.97%). Measured 2026-11-01 over the whole lake: at a flat 10% the small
#: multiples (2, 2.5, 3, 4) between them cover most ratios from 1.8 to 5.5, and 432 illiquid
#: long gaps matched — noise, not evidence.
LONG_GAP_MULTIPLE_TOLERANCE: Final = Decimal("0.03")
LONG_GAP_WIDE_MULTIPLE_TOLERANCE: Final = Decimal("0.06")
_WIDE_FROM: Final = Decimal(5)

#: The longest gap a price-only share-basis suspicion is raised across. An exchange suspends a
#: stock for a consolidation or a reissue from the record date until the new ISIN is credited —
#: days to a few weeks (AvenuesAI: 7). Past a month the gap is illiquidity, trade-to-trade months
#: off the EQ view, or a suspension for cause, where a clean-looking ratio is as likely a genuine
#: move; those stay `LONG_GAP`. A step across a reissue is `SHARE_BASIS` at any gap.
LONG_GAP_SHARE_BASIS_MAX_DAYS: Final = 30

#: Recorded action types that explain a level step (the factor module's structural breaks).
_STRUCTURAL_TYPES: Final = ("MERGER", "DEMERGER", "SCHEME_OF_ARRANGEMENT", "DVR_CONVERSION")


class StepClass(StrEnum):
    """Why an adjusted-close step is there — or that nothing says why."""

    STRUCTURAL = "STRUCTURAL"
    RECORDED_UNSCALED = "RECORDED_UNSCALED"
    EXPLAINED_MOVE = "EXPLAINED_MOVE"
    UNSOURCED = "UNSOURCED"
    DIVIDEND = "DIVIDEND"
    SHARE_BASIS = "SHARE_BASIS"
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

    `passed` is the check: no `UNEXPLAINED` step and no retired partition. An `UNSOURCED` step
    keeps it passing and is reported as a WARN finding instead.
    """

    threshold: Decimal
    max_gap_days: int
    partitions: int
    steps: tuple[tuple[AdjustedStep, StepClass], ...]
    retired_partitions: tuple[str, ...]
    #: The ISINs the scan was limited to; `None` for a scan of every partition.
    isins: frozenset[str] | None = None
    #: Partition ISIN → the effective dates of the lineage reissues its history stitches.
    reissue_dates: Mapping[str, tuple[date, ...]] = field(default_factory=dict)

    def of_class(self, cls: StepClass) -> tuple[AdjustedStep, ...]:
        return tuple(step for step, c in self.steps if c is cls)

    @property
    def unexplained(self) -> tuple[AdjustedStep, ...]:
        return self.of_class(StepClass.UNEXPLAINED)

    @property
    def unsourced(self) -> tuple[AdjustedStep, ...]:
        return self.of_class(StepClass.UNSOURCED)

    @property
    def share_basis(self) -> tuple[AdjustedStep, ...]:
        return self.of_class(StepClass.SHARE_BASIS)

    @property
    def passed(self) -> bool:
        return not self.unexplained and not self.share_basis and not self.retired_partitions

    @property
    def resolution_scope(self) -> FindingScope | None:
        """The open flags `persist_findings` may close if this scan did not raise them again.

        Every `l2_continuity` flag for a full scan; those about the scanned ISINs for a partial
        one; none for a scan run at a non-default threshold or gap, which cannot speak for the
        check as configured — it would close a 2x step's flag just for looking at 3x.
        """
        if self.threshold != DEFAULT_THRESHOLD or self.max_gap_days != DEFAULT_MAX_GAP_DAYS:
            return None
        return FindingScope(check_name=CHECK_NAME, isins=self.isins)


def classify_steps(
    steps: Iterable[AdjustedStep],
    *,
    structural_dates: Mapping[str, Iterable[date]],
    unscaled_dates: Mapping[str, Iterable[date]] | None = None,
    explained_dates: Mapping[str, Iterable[date]] | None = None,
    unsourced_dates: Mapping[str, Iterable[date]] | None = None,
    reissue_dates: Mapping[str, Iterable[date]] | None = None,
    threshold: Decimal,
    max_gap_days: int,
    guard_days: int = 7,
) -> tuple[tuple[AdjustedStep, StepClass], ...]:
    """Classify each step (module docstring, in that order of precedence); pure.

    `structural_dates` maps an ISIN to the ex-dates of its recorded structural breaks;
    `unscaled_dates` to those of its recorded rights issues and large dividends;
    `explained_dates` to the sessions the curated allowlist documents as market moves — matched
    on the step's own session exactly, never within a guard window, so it cannot cover another;
    `unsourced_dates` to the curated unsourced share-basis steps, matched the same exact way;
    `reissue_dates` to the effective dates of the D2 lineage edges whose bars the ISIN's
    partition stitches — a step whose pair straddles one is `SHARE_BASIS` whatever its gap.
    """
    guard = timedelta(days=guard_days)
    lower = 1 / threshold
    out: list[tuple[AdjustedStep, StepClass]] = []
    for step in sorted(steps, key=lambda s: (s.isin, s.exchange, s.trade_date)):
        # A recorded event explains the step when it falls inside the pair's gap, or within
        # `guard_days` of either end: Raymond's 2025-05-14 demerger sits inside a 15-day gap
        # (05-13 -> 05-28), two weeks from the step's own session.
        low, high = step.prev_date - guard, step.trade_date + guard
        breaks = structural_dates.get(step.isin, ())
        if any(low < d <= high for d in breaks):
            cls = StepClass.STRUCTURAL
        elif any(low < d <= high for d in (unscaled_dates or {}).get(step.isin, ())):
            cls = StepClass.RECORDED_UNSCALED
        elif step.trade_date in (unsourced_dates or {}).get(step.isin, ()):
            cls = StepClass.UNSOURCED
        elif step.trade_date in (explained_dates or {}).get(step.isin, ()):
            cls = StepClass.EXPLAINED_MOVE
        elif lower <= step.tr_ratio <= threshold:
            cls = StepClass.DIVIDEND
        elif any(
            step.prev_date < d <= step.trade_date for d in (reissue_dates or {}).get(step.isin, ())
        ) or (
            max_gap_days < step.gap_days <= LONG_GAP_SHARE_BASIS_MAX_DAYS
            and clean_multiple(step.ratio) is not None
        ):
            cls = StepClass.SHARE_BASIS
        elif step.gap_days > max_gap_days:
            cls = StepClass.LONG_GAP
        else:
            cls = StepClass.UNEXPLAINED
        out.append((step, cls))
    return tuple(out)


def clean_multiple(ratio: Decimal) -> Decimal | None:
    """The clean split multiple `ratio` (either way up) sits within tolerance of, or `None`."""
    if ratio <= 0:
        return None
    size = ratio if ratio >= 1 else 1 / ratio
    for multiple in CLEAN_MULTIPLES:
        band = (
            LONG_GAP_WIDE_MULTIPLE_TOLERANCE
            if multiple >= _WIDE_FROM
            else LONG_GAP_MULTIPLE_TOLERANCE
        )
        if abs(size / multiple - 1) <= band:
            return multiple
    return None


def share_basis_reason(step: AdjustedStep, reissue_dates: Iterable[date] = ()) -> str:
    """Why a `SHARE_BASIS` step is one, in words for the Ops page and the guard's state."""
    across = [d for d in reissue_dates if step.prev_date < d <= step.trade_date]
    if across:
        return (
            f"x{step.ratio:.4f} across the D2 reissue effective {across[0].isoformat()}; "
            "no recorded or admitted factor explains it"
        )
    multiple = clean_multiple(step.ratio)
    return (
        f"x{step.ratio:.4f} across a {step.gap_days}-day gap, at a {multiple}:1 re-basing; no "
        "recorded or admitted factor explains it, and a long gap is never adjusted from price alone"
    )


def findings(report: L2ContinuityReport, *, logical_date: date) -> tuple[QualityFinding, ...]:
    """ERROR per unexplained step and retired partition, WARN per unsourced step.

    ERROR on purpose: an unadjusted split in L2 is a wrong return the backtest and every signal
    will trade on (invariant #10). An `UNSOURCED` step is that same wrong return, but known and
    quarantined in the query layer (D22), so it is a WARN (kind `unsourced_action_step`): visible
    on the status API until a source lets it be adjusted, without turning the check red. A step's
    finding is dated by the step's own session, so a re-scan finds the flag it raised before
    rather than stacking another; a retired partition's is not dated at all, for the same reason.
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
    for step in report.share_basis:
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
                    "kind": "share_basis_step",
                    "exchange": step.exchange,
                    "prev_date": step.prev_date.isoformat(),
                    "prev_adj_close": str(step.prev_close),
                    "adj_close": str(step.close),
                    "gap_days": step.gap_days,
                    "reason": share_basis_reason(step, report.reissue_dates.get(step.isin, ())),
                    "quarantined_before": step.trade_date.isoformat(),
                },
                fingerprint=finding_fingerprint(
                    f"{CHECK_NAME}:share_basis:{step.exchange}", step.isin, step.trade_date
                ),
            )
        )
    for step in report.unsourced:
        out.append(
            QualityFinding(
                logical_date=step.trade_date,
                check_name=CHECK_NAME,
                severity="WARN",
                isin=step.isin,
                source=_DATASET,
                observed_value=step.ratio,
                threshold=report.threshold,
                detail={
                    "kind": "unsourced_action_step",
                    "exchange": step.exchange,
                    "prev_date": step.prev_date.isoformat(),
                    "prev_adj_close": str(step.prev_close),
                    "adj_close": str(step.close),
                    "quarantined_before": step.trade_date.isoformat(),
                },
                fingerprint=finding_fingerprint(
                    f"{CHECK_NAME}:unsourced:{step.exchange}", step.isin, step.trade_date
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
                fingerprint=finding_fingerprint(
                    f"{CHECK_NAME}:retired", isin, _RETIRED_FINGERPRINT_DATE
                ),
            )
        )
    return tuple(out)


# ── the read seam (DuckDB over L2, Postgres for recorded breaks) ─────────────────────────────


def scan(
    conn: Connection | None,
    *,
    survivor_of: Callable[[str], str],
    data_root: Path | None = None,
    threshold: Decimal = DEFAULT_THRESHOLD,
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
    curated: ManualActions | None = None,
    isins: Iterable[str] | None = None,
    reissues: Iterable[tuple[str, str, date]] = (),
) -> L2ContinuityReport:
    """Scan the materialized L2 for steps past `threshold`x and for retired-ISIN partitions.

    Read-only: one DuckDB window pass over every partition — or only `isins`' partitions, when
    given, which the report records as its scope — and (when `conn` is given) one Postgres read
    of the reconciled structural breaks; without it only a curated break makes a step
    `STRUCTURAL`. `curated` (default: the repo's file) supplies the curated breaks and the
    explained-move allowlist. `reissues` are the D2 lineage edges ``(predecessor, successor,
    effective_date)`` (`LineageResolver.edges`); each is keyed to the successor's survivor, whose
    partition stitches the step, so a step across one is `SHARE_BASIS`.
    """
    curated = default_manual_actions() if curated is None else curated
    scope = None if isins is None else frozenset(isins)
    from dataplatform.store.l2 import (
        materialized_isins,
        open_connection,
        register_adjusted_view,
    )

    partitions = materialized_isins(data_root=data_root)
    if scope is not None:
        partitions = partitions & scope
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
                    WHERE $every OR list_contains($isins, isin)
                    WINDOW w AS (PARTITION BY isin, exchange ORDER BY trade_date)
                )
                WHERE pd IS NOT NULL AND (adj_close > $t * pc OR adj_close * $t < pc)
                """,
                {"t": threshold, "every": scope is None, "isins": sorted(scope or ())},
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
    # A curated date is keyed to the ISIN it was filed under; once a lineage edge retires that
    # ISIN its bars are scanned in the survivor's partition, so the date is looked up there.
    structural: dict[str, list[date]] = {}
    for isin, days in curated.structural_dates().items():
        structural.setdefault(survivor_of(isin), []).extend(days)
    explained: dict[str, list[date]] = {}
    for isin, days in curated.explained_dates().items():
        explained.setdefault(survivor_of(isin), []).extend(days)
    unsourced: dict[str, list[date]] = {}
    for isin, days in curated.unsourced_dates().items():
        unsourced.setdefault(survivor_of(isin), []).extend(days)
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
    reissue_dates: dict[str, list[date]] = {}
    for _predecessor, successor, effective in reissues:
        reissue_dates.setdefault(survivor_of(successor), []).append(effective)
    report = L2ContinuityReport(
        threshold=threshold,
        max_gap_days=max_gap_days,
        partitions=len(partitions),
        steps=classify_steps(
            steps,
            structural_dates=structural,
            unscaled_dates=unscaled,
            explained_dates=explained,
            unsourced_dates=unsourced,
            reissue_dates=reissue_dates,
            threshold=threshold,
            max_gap_days=max_gap_days,
        ),
        retired_partitions=retired,
        isins=scope,
        reissue_dates={k: tuple(sorted(v)) for k, v in reissue_dates.items()},
    )
    _LOG.info(
        "quality.l2_continuity",
        dataset=_DATASET,
        partitions=report.partitions,
        steps=len(report.steps),
        **{c.value.lower(): len(report.of_class(c)) for c in StepClass},
        retired_partitions=len(report.retired_partitions),
        # WARN, not GREEN, while an unsourced step stands: passing, but a known wrong level series.
        state="RED" if not report.passed else ("WARN" if report.unsourced else "GREEN"),
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
    """CLI: print the counts, every unexplained and unsourced step; exit 1 when the check fails."""
    from dataplatform.clock import SystemClock
    from dataplatform.config import get_settings
    from dataplatform.identity.lineage import LineageStore
    from dataplatform.quality.sentinel import persist_findings
    from dataplatform.store.db import connect

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=Decimal, default=DEFAULT_THRESHOLD)
    parser.add_argument("--max-gap-days", type=int, default=DEFAULT_MAX_GAP_DAYS)
    parser.add_argument(
        "--persist",
        action="store_true",
        help="raise quality_flag rows, and close the open ones this scan covered and did not raise",
    )
    parser.add_argument(
        "--isin",
        action="append",
        default=None,
        help="scan only this ISIN's partition (repeatable); --persist then closes its flags only",
    )
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
            isins=args.isin,
            reissues=resolver.edges(),
        )
        if args.persist:
            counts = persist_findings(
                conn,
                findings(report, logical_date=clock.today()),
                clock=clock,
                resolve_scope=report.resolution_scope,
            )
            conn.commit()
            closing = (
                "none (non-default threshold or gap)"
                if report.resolution_scope is None
                else str(counts.resolved)
            )
            print(
                f"persisted: {counts.written} raised, {counts.skipped} already open, "
                f"{closing} closed"
            )
    print(f"{'partitions':<22} {report.partitions}")
    for cls in StepClass:
        print(f"{cls.value.lower():<22} {len(report.of_class(cls))}")
    print(f"{'retired_partitions':<22} {len(report.retired_partitions)}")
    for step in report.unexplained:
        print(
            f"UNEXPLAINED {step.isin} {step.exchange} {step.prev_date} -> {step.trade_date} "
            f"{step.prev_close} -> {step.close} (x{step.ratio:.4f})"
        )
    for step in report.share_basis:
        print(
            f"SHARE_BASIS {step.isin} {step.exchange} {step.prev_date} -> {step.trade_date} "
            f"{step.prev_close} -> {step.close} (x{step.ratio:.4f}): "
            f"{share_basis_reason(step, report.reissue_dates.get(step.isin, ()))}"
        )
    for step in report.unsourced:
        print(
            f"UNSOURCED {step.isin} {step.exchange} {step.prev_date} -> {step.trade_date} "
            f"{step.prev_close} -> {step.close} (x{step.ratio:.4f}) "
            f"quarantined before {step.trade_date}"
        )
    for isin in report.retired_partitions:
        print(f"RETIRED {isin}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
