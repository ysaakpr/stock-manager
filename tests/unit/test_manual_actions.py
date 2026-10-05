"""Curated corporate actions and explained moves (``dataplatform.corpactions.manual_actions``).

The file is a hand transcription, so these tests hold it to its sources: every row is sourced and
dated, every quoted line is in L0 where the lake is on this host, and every action's ex-date is
the one NSE's own book-closure line states. The seams are pinned too: a curated event a feed has
since published is dropped in the feed's favour, a curated break marks the chain without scaling
it, and an explained move covers its own session only — never a neighbour.
"""

from __future__ import annotations

import csv
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.corpactions.factors import (
    FactorChain,
    FactorError,
    PricePoint,
    return_series,
    with_events,
)
from dataplatform.corpactions.manual_actions import (
    MANUAL_SOURCE,
    CuratedAction,
    ExplainedMove,
    ManualActions,
    ManualActionsError,
    load_manual_actions,
)
from dataplatform.corpactions.merger_terms import TermSource
from dataplatform.corpactions.taxonomy import (
    ActionType,
    DividendKind,
    DividendTerms,
    FaceValueTerms,
    RatioTerms,
    UnquantifiedTerms,
)
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.quality.l2_continuity import AdjustedStep, StepClass, classify_steps
from dataplatform.store.l2 import curated_actions

_LAKE = Path("/home/ubuntu/stock-manager/data/L0")
STOCK = "INE002A01018"


def _action(
    action_type: ActionType,
    terms: FaceValueTerms | RatioTerms | UnquantifiedTerms | DividendTerms,
    *,
    ex_date: date = date(2020, 6, 1),
    source: str = MANUAL_SOURCE,
) -> CorporateAction:
    return CorporateAction(
        isin=STOCK,
        ex_date=ex_date,
        action_type=action_type,
        terms=terms,
        source=source,
        raw_text="x",
        knowable_date=ex_date,
    )


# ── the file itself ──────────────────────────────────────────────────────────────────────────


def test_the_curated_file_loads_identically_twice() -> None:
    assert load_manual_actions() == load_manual_actions()


def test_every_row_is_sourced_checked_and_knowable_no_later_than_its_ex_date() -> None:
    curated = load_manual_actions()
    assert curated.actions and curated.explained_moves
    for row in curated.actions:
        assert row.sources and all(s.quote for s in row.sources), row.isin
        assert row.knowable_date <= row.ex_date, row.isin
        assert row.checked >= row.knowable_date, row.isin
        assert row.as_action().source == MANUAL_SOURCE
    for move in curated.explained_moves:
        assert move.sources and move.reason and move.kind == "MARKET_MOVE", move.isin


def _sources() -> list[tuple[str, TermSource]]:
    curated = load_manual_actions()
    rows: list[CuratedAction | ExplainedMove] = [*curated.actions, *curated.explained_moves]
    return [(r.isin, s) for r in rows for s in r.sources]


@pytest.mark.parametrize(("isin", "source"), _sources(), ids=[i for i, _ in _sources()])
def test_every_quoted_line_is_in_l0_where_the_lake_is_here(isin: str, source: TermSource) -> None:
    path = _LAKE / source.l0_key
    if not path.is_file():
        pytest.skip(f"L0 object {source.l0_key} is not on this host (no lake, e.g. CI)")
    assert source.member is not None and source.line is not None
    lines = zipfile.ZipFile(path).read(source.member).decode("latin-1").splitlines()
    assert 1 <= source.line <= len(lines), (isin, source.line)
    line = " ".join(lines[source.line - 1].split())
    missing = [f.strip() for f in source.quote.split("...") if f.strip() not in line]
    assert not missing, f"{isin}: not on line {source.line}: {missing}"


def _bc_actions() -> list[tuple[CuratedAction, TermSource]]:
    return [
        (row, s)
        for row in load_manual_actions().actions
        for s in row.sources
        if s.member is not None and s.member.lower().startswith("bc")
    ]


@pytest.mark.parametrize(
    ("row", "source"), _bc_actions(), ids=[f"{r.isin}-{r.action_type}" for r, _ in _bc_actions()]
)
def test_every_ex_date_is_the_one_the_book_closure_line_states(
    row: CuratedAction, source: TermSource
) -> None:
    """Bc columns: SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,...

    Dates are dd/mm/yyyy up to 2025-10-01 and yyyy-mm-dd in the lowercase `bc<ddmmyyyy>` era after.
    """
    [fields] = list(csv.reader([source.quote]))
    assert _bc_date(fields[6]) == row.ex_date, row.isin
    if row.record_date is not None:
        assert _bc_date(fields[3]) == row.record_date, row.isin


def _bc_date(text: str) -> date:
    value = text.strip()
    fmt = "%Y-%m-%d" if "-" in value else "%d/%m/%Y"
    return datetime.strptime(value, fmt).date()


def test_two_events_on_one_ex_date_compose_into_one_factor() -> None:
    """DPSC: BONUS 22:1 and SPLIT Rs 10 -> Re 1 on 2011-12-15 are 1/230 together."""
    rows = [a.as_action() for a in load_manual_actions().actions_for("INE360C01024")]
    chain = with_events(FactorChain(isin="INE360C01024", rows=()), rows)
    [row] = chain.rows
    assert row.price_factor * 230 == pytest.approx(Decimal(1), abs=Decimal("1e-20"))
    # Inverted, this is 230 rather than 1/230.
    assert row.price_factor < 1


_GOOD = """\
actions:
  - isin: INE018I01017
    company: A
    action_type: BONUS
    new_shares: 1
    held_shares: 1
    ex_date: 2016-03-09
    knowable_date: 2016-03-01
    checked: 2026-10-05
    sources:
      - l0_key: x
        quote: q
explained_moves:
  - isin: INE528G01035
    company: B
    trade_date: 2020-03-06
    kind: MARKET_MOVE
    reason: r
    checked: 2026-10-05
    sources:
      - l0_key: y
        quote: q
"""


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ("new_shares: 1", "new_shares: 1.0"),
        ("action_type: BONUS", "action_type: DIVIDEND"),
        ("isin: INE018I01017", "isin: NOT-AN-ISIN"),
        (
            "    checked: 2026-10-05\n    sources:\n      - l0_key: x",
            "    sources:\n      - l0_key: x",
        ),
        ("      - l0_key: x\n        quote: q", "      - l0_key: x"),
        ("kind: MARKET_MOVE", "kind: DATA_ERROR"),
    ],
)
def test_a_malformed_file_is_refused(tmp_path: Path, patch: str, message: str) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(_GOOD)
    assert len(load_manual_actions(good).actions) == 1
    assert _GOOD.count(patch) == 1
    bad = tmp_path / "bad.yaml"
    bad.write_text(_GOOD.replace(patch, message))
    with pytest.raises(ManualActionsError):
        load_manual_actions(bad)


def test_a_duplicate_action_is_refused(tmp_path: Path) -> None:
    block = _GOOD.split("explained_moves:")[0].removeprefix("actions:\n")
    path = tmp_path / "dup.yaml"
    path.write_text("actions:\n" + block + block)
    with pytest.raises(ManualActionsError, match="twice"):
        load_manual_actions(path)


def test_a_move_cannot_be_explained_on_a_curated_actions_own_session(tmp_path: Path) -> None:
    path = tmp_path / "both.yaml"
    path.write_text(
        _GOOD.replace("isin: INE528G01035", "isin: INE018I01017").replace(
            "trade_date: 2020-03-06", "trade_date: 2016-03-09"
        )
    )
    with pytest.raises(ManualActionsError, match="cannot share"):
        load_manual_actions(path)


# ── the seams: feed precedence, the chain, the check ─────────────────────────────────────────


def test_a_curated_event_a_feed_has_since_published_is_dropped_for_the_feeds() -> None:
    split = _action(ActionType.SPLIT, FaceValueTerms(from_value=Decimal(10), to_value=Decimal(5)))
    bonus = _action(ActionType.BONUS, RatioTerms(new_shares=Decimal(1), held_shares=Decimal(25)))
    published = _action(
        ActionType.SPLIT,
        FaceValueTerms(from_value=Decimal(10), to_value=Decimal(5)),
        source="nse_corp_actions",
    )
    # A different type on the same day (KTIL: the feed's bonus, the curated split) is kept.
    assert curated_actions(STOCK, [bonus], [split]) == (split,)
    assert curated_actions(STOCK, [published], [split]) == ()
    with pytest.raises(ValueError, match="passed for"):
        curated_actions("INE009A01021", [], [split])


def test_a_curated_break_marks_the_chain_and_scales_nothing() -> None:
    ex = date(2016, 1, 20)
    demerger = _action(ActionType.DEMERGER, UnquantifiedTerms(), ex_date=ex)
    chain = with_events(FactorChain(isin=STOCK, rows=()), [demerger])
    assert chain.structural_break_dates() == frozenset({ex})
    assert chain.price_factor_asof(ex - timedelta(days=1)) == Decimal(1)
    prices = [
        PricePoint(date=ex - timedelta(days=1), close=Decimal("2065.55")),
        PricePoint(date=ex, close=Decimal("882.15")),
    ]
    [point] = return_series(chain, prices)
    assert point.ret is None and point.bridged


def test_with_events_still_refuses_what_is_neither_a_price_event_nor_a_break() -> None:
    terms = DividendTerms(dividend_kind=DividendKind.INTERIM, amount_inr=Decimal(4))
    dividend = _action(ActionType.DIVIDEND, terms)
    with pytest.raises(FactorError):
        with_events(FactorChain(isin=STOCK, rows=()), [dividend])


def _step(day: date) -> AdjustedStep:
    return AdjustedStep(
        isin=STOCK,
        exchange="NSE",
        prev_date=day - timedelta(days=1),
        trade_date=day,
        prev_close=Decimal("36.80"),
        close=Decimal("16.15"),
        ratio=Decimal("16.15") / Decimal("36.80"),
        tr_ratio=Decimal("16.15") / Decimal("36.80"),
        factor_changed=False,
    )


def test_an_explained_move_covers_its_own_session_and_no_other() -> None:
    day = date(2020, 3, 6)
    classified = {
        step.trade_date: cls
        for step, cls in classify_steps(
            [_step(day), _step(day + timedelta(days=3))],
            structural_dates={},
            explained_dates={STOCK: (day,)},
            threshold=Decimal(2),
            max_gap_days=5,
        )
    }
    assert classified == {
        day: StepClass.EXPLAINED_MOVE,
        day + timedelta(days=3): StepClass.UNEXPLAINED,
    }


def test_the_check_reads_curated_breaks_and_moves_without_postgres(tmp_path: Path) -> None:
    from dataplatform.quality.l2_continuity import scan

    curated = ManualActions(actions=(), explained_moves=())
    report = scan(None, survivor_of=lambda i: i, data_root=tmp_path, curated=curated)
    assert report.passed and report.partitions == 0
    real = load_manual_actions()
    assert real.structural_dates() == {
        "INE069A01017": (date(2016, 1, 20),),
        "INE429C01035": (date(2017, 5, 25),),
    }
    assert set(real.explained_dates()) == {
        "INE111B01023",
        "INE247G01024",
        "INE483S01020",
        "INE528G01035",
    }


def test_a_targeted_rebuild_refuses_a_retired_isin_and_names_its_survivor() -> None:
    from typing import cast

    from dataplatform.store.db import Connection
    from dataplatform.store.l2 import rebuild_isins

    survivors = {"INE096L01017": "INE096L01025"}
    with pytest.raises(ValueError, match="INE096L01017 -> INE096L01025"):
        rebuild_isins(
            cast(Connection, None),
            ["INE096L01017"],
            survivor_of=lambda i: survivors.get(i, i),
        )
