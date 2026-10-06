"""Fixture point-in-time index membership histories for backtest tests (DQ-5's public writer).

A backtest's investable screen reads index membership from the stored history and fails loud on a
date the history does not cover (``backtest.run.IndexCoverageError``). A fixture lake that applies
the screen therefore needs a history on disk, written through the same
``write_membership_history`` production uses, so the read side is the real on-disk contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from backtest.run import UniverseParameters
from backtest.run_ledger import run_digest
from backtest.sweep import _DEFAULT_OPENING_CASH, Arm, _arm_spec
from dataplatform.ingest.index_history import (
    HistoryBuild,
    IndexHistory,
    MembershipInterval,
    write_membership_history,
)

__all__ = ["digests_without_index_membership", "stay", "write_index_history"]


def stay(
    index_slug: str,
    isin: str,
    effective_from: date,
    effective_to: date | None = None,
    *,
    knowable_from: date | None = None,
    exit_knowable: date | None = None,
) -> MembershipInterval:
    """One member's stay: effective ``[effective_from, effective_to)``, announced ``knowable_from``.

    ``knowable_from`` defaults to ``effective_from`` (a stay known the day it starts); pass an
    earlier date for an announced-ahead inclusion, a later one for a stay announced after the fact.
    """
    return MembershipInterval(
        index_slug=index_slug,
        isin=isin,
        effective_from=effective_from,
        effective_to=effective_to,
        knowable_from=knowable_from if knowable_from is not None else effective_from,
        exit_knowable=exit_knowable if exit_knowable is not None else effective_to,
        entry_basis="fixture",
        exit_basis=None if effective_to is None else "fixture",
    )


def write_index_history(
    data_root: Path,
    index_slug: str,
    *,
    coverage_start: date,
    anchor: date,
    members: Iterable[str] = (),
    stays: Iterable[MembershipInterval] = (),
) -> None:
    """Write one index's membership history build (anchored at ``anchor``) under ``data_root``.

    ``members`` are in the index from ``coverage_start`` with no exit, as a history records a name
    already present at its coverage start; ``stays`` are any further, hand-set intervals.
    """
    intervals = tuple(
        sorted(
            (
                *(
                    stay(index_slug, isin, coverage_start, knowable_from=coverage_start)
                    for isin in members
                ),
                *stays,
            ),
            key=lambda i: (i.isin, i.effective_from),
        )
    )
    history = IndexHistory(
        index_slug=index_slug,
        anchor_date=anchor,
        anchor_l0_key=f"fixture/{index_slug}/{anchor.isoformat()}",
        coverage_start=coverage_start,
        expected_size=len(set(members)),
        intervals=intervals,
        residuals=(),
        segments=(),
        events_applied=0,
    )
    write_membership_history(
        HistoryBuild(
            as_of=anchor,
            listing_l0_key="fixture/listing.html",
            releases_considered=0,
            releases_in_l0=0,
            releases_missing=(),
            histories={index_slug: history},
            events=(),
        ),
        data_root=data_root,
    )


def digests_without_index_membership(
    *, start: date, end: date, arms: Sequence[Arm], floors: Sequence[Decimal]
) -> dict[tuple[str, Decimal], str]:
    """``sweep.run_digests`` with the ``index_membership`` spec key removed.

    The key deliberately moved every screened run's digest (``backtest.run``,
    ``INDEX_MEMBERSHIP_IDENTITY``). A pin taken before it existed is checked against this, so the
    pin still proves that *nothing else* in the specification moved. The ``holding_marks`` key
    (``HOLDING_MARKS_IDENTITY``, held names marked and sold in BE/BZ once they leave EQ) moved every
    digest the same deliberate way, and is removed for the same reason.
    """
    out: dict[tuple[str, Decimal], str] = {}
    for floor in floors:
        universe = UniverseParameters(median_turnover_floor=floor)
        for arm in arms:
            spec = _arm_spec(
                arm,
                start=start,
                end=end,
                universe=universe,
                opening_cash=_DEFAULT_OPENING_CASH,
                adjusted=True,
            )
            spec.pop("index_membership")
            spec.pop("holding_marks")
            out[(arm.label, floor)] = run_digest(spec)
    return out
