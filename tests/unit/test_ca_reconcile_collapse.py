"""M2.3/M2.4 — two feeds' rows of one reconciled event are one action, not two.

`corporate_actions` keeps one row per `(isin, ex_date, action_type, source)`, so an event both
exchanges published is two rows, both marked `reconciled = true`. `load_reconciled_actions` is the
door the factor chain and the total-return series read through, and until this suite existed it
handed both rows on: `build_factor_chain` multiplies every action sharing an ex-date, so a 10→2
split NSE and BSE agreed on became `0.2 x 0.2`, and a ₹5 dividend both reported was reinvested
twice. Measured on the server on 2026-09-07, the first store to hold both feeds: 548 split/bonus
events on 412 ISINs squared, IRCTC's pre-split bars adjusted to ~165 instead of ~826.

The laptop never showed it because it holds one feed. `test_ca_reconcile.py` proves agreement
collapses to one `ReconciledAction` in memory; this proves the *database* door collapses the same
way, and that a factor built through it is the event's factor once.

Offline and deterministic: no network, no Postgres. The fake connection speaks only the reconciled
SELECT and raises on anything else.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest

from dataplatform.corpactions import (
    ActionType,
    FaceValueTerms,
    RatioTerms,
    ReconcileError,
    UnquantifiedTerms,
    build_factor_chain,
    load_reconciled_actions,
)
from dataplatform.corpactions.reconcile import collapse_reconciled_rows
from dataplatform.corpactions.taxonomy import DividendKind, DividendTerms
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.store.db import Connection

NSE = "nse_corp_actions"
BSE = "bse_corp_actions"
IRCTC = "INE335Y01020"
SPLIT_EX = date(2021, 10, 28)


def _ca(
    *,
    source: str,
    ex_date: date,
    action_type: ActionType,
    terms: object,
    raw_text: str,
    isin: str = IRCTC,
    record_date: date | None = None,
    knowable_date: date | None = None,
) -> CorporateAction:
    return CorporateAction(
        isin=isin,
        ex_date=ex_date,
        action_type=action_type,
        terms=cast("Any", terms),
        source=source,
        raw_text=raw_text,
        knowable_date=ex_date if knowable_date is None else knowable_date,
        record_date=record_date,
        source_ref="IRCTC:EQ" if source == NSE else "542830",
        l0_key=f"{source}/2021-09-01/{raw_text[:6]}",
    )


def _split(source: str, ex: date = SPLIT_EX, *, to: str = "2") -> CorporateAction:
    return _ca(
        source=source,
        ex_date=ex,
        action_type=ActionType.SPLIT,
        terms=FaceValueTerms(from_value=Decimal(10), to_value=Decimal(to)),
        raw_text=f"Face Value Split From Rs 10 To Rs {to} ({source})",
    )


def _dividend(source: str, ex: date, kind: DividendKind, amount: str = "5") -> CorporateAction:
    return _ca(
        source=source,
        ex_date=ex,
        action_type=ActionType.DIVIDEND,
        terms=DividendTerms(dividend_kind=kind, amount_inr=Decimal(amount)),
        raw_text=f"Dividend Rs {amount} ({source})",
    )


# ── the pure collapse ────────────────────────────────────────────────────────────────────────


def test_two_feeds_rows_of_one_split_collapse_to_one_action() -> None:
    collapsed = collapse_reconciled_rows([_split(BSE), _split(NSE)])
    assert len(collapsed) == 1
    (action,) = collapsed
    assert action.action_type is ActionType.SPLIT
    assert action.terms == FaceValueTerms(from_value=Decimal(10), to_value=Decimal(2))


def test_the_factor_built_through_the_collapse_is_the_events_factor_once() -> None:
    """The bug as the server showed it: 0.04 / 25 for a 10→2 split both exchanges reported."""
    chain = build_factor_chain(collapse_reconciled_rows([_split(BSE), _split(NSE)]))
    (row,) = chain.rows
    assert row.price_factor == Decimal("0.2")
    assert row.qty_factor == Decimal(5)


def test_a_named_dividend_kind_wins_over_an_unspecified_one() -> None:
    ex = date(2025, 11, 21)
    collapsed = collapse_reconciled_rows(
        [_dividend(NSE, ex, DividendKind.UNSPECIFIED), _dividend(BSE, ex, DividendKind.INTERIM)]
    )
    (action,) = collapsed
    assert isinstance(action.terms, DividendTerms)
    assert action.terms.dividend_kind is DividendKind.INTERIM
    assert action.terms.amount_inr == Decimal(5)


def test_a_stated_ratio_fills_a_silent_feed() -> None:
    silent = _ca(
        source=BSE,
        ex_date=SPLIT_EX,
        action_type=ActionType.SPLIT,
        terms=UnquantifiedTerms(),
        raw_text="Sub Division of Equity shares",
    )
    (action,) = collapse_reconciled_rows([silent, _split(NSE)])
    assert action.terms == FaceValueTerms(from_value=Decimal(10), to_value=Decimal(2))
    assert action.source == NSE, "the row that stated the numbers carries the event"


def test_ex_dates_within_tolerance_are_one_event_dated_the_earlier() -> None:
    later = _split(BSE, SPLIT_EX + (date(2021, 10, 29) - SPLIT_EX))
    (action,) = collapse_reconciled_rows([later, _split(NSE)])
    assert action.ex_date == SPLIT_EX


def test_different_types_on_one_date_are_two_events() -> None:
    bonus = _ca(
        source=NSE,
        ex_date=SPLIT_EX,
        action_type=ActionType.BONUS,
        terms=RatioTerms(new_shares=Decimal(1), held_shares=Decimal(1)),
        raw_text="Bonus 1:1",
    )
    collapsed = collapse_reconciled_rows([_split(NSE), bonus])
    assert sorted(a.action_type.value for a in collapsed) == ["BONUS", "SPLIT"]


def test_a_single_feeds_row_passes_through_unchanged() -> None:
    only = _split(NSE)
    assert collapse_reconciled_rows([only]) == (only,)


def test_two_dividends_a_month_apart_stay_two_events() -> None:
    a, b = date(2025, 2, 20), date(2025, 3, 20)
    rows = [
        _dividend(NSE, a, DividendKind.INTERIM, "3"),
        _dividend(BSE, a, DividendKind.INTERIM, "3"),
        _dividend(NSE, b, DividendKind.FINAL, "1"),
        _dividend(BSE, b, DividendKind.FINAL, "1"),
    ]
    collapsed = collapse_reconciled_rows(rows)
    assert [(x.ex_date, x.terms.amount_inr) for x in collapsed] == [  # type: ignore[union-attr]
        (a, Decimal(3)),
        (b, Decimal(1)),
    ]


def test_two_reconciled_rows_that_contradict_are_a_defect_not_a_guess() -> None:
    with pytest.raises(ReconcileError, match="INE335Y01020"):
        collapse_reconciled_rows([_split(BSE, to="1"), _split(NSE, to="2")])


def test_three_feeds_are_a_malformed_store() -> None:
    third = _split(NSE).model_copy(update={"source": "third_feed"})
    with pytest.raises(ReconcileError, match="3 sources"):
        collapse_reconciled_rows([_split(BSE), _split(NSE), third])


# ── the database door ────────────────────────────────────────────────────────────────────────


class _FakeConn:
    """Answers the reconciled SELECT with the rows it was given; anything else is a drift."""

    def __init__(self, rows: Sequence[CorporateAction]) -> None:
        self._rows = rows
        self._result: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: Sequence[object] = ()) -> _FakeConn:
        if "FROM corporate_actions" not in sql or "reconciled = true" not in sql:
            raise AssertionError(f"unexpected SQL: {sql}")
        rows = self._rows
        if "AND isin = %s" in sql:
            rows = [r for r in rows if r.isin == params[0]]
        self._result = [
            (
                r.isin,
                r.ex_date,
                r.action_type.value,
                r.terms.model_dump(mode="json"),
                r.record_date,
                r.announcement_date,
                r.knowable_date,
                r.source,
                r.source_ref,
                r.raw_text,
                r.l0_key,
            )
            for r in sorted(rows, key=lambda r: (r.isin, r.ex_date, r.action_type.value, r.source))
        ]
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._result


def test_load_reconciled_actions_returns_one_action_per_event() -> None:
    conn = cast("Connection", _FakeConn([_split(BSE), _split(NSE)]))
    actions = load_reconciled_actions(conn, isin=IRCTC)
    assert len(actions) == 1
    (row,) = build_factor_chain(actions).rows
    assert (row.price_factor, row.qty_factor) == (Decimal("0.2"), Decimal(5))
