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

from dataplatform.corpactions.ex_date_rule import (
    T1_SETTLEMENT_FROM,
    ExDateRule,
    derive_ex_date,
)
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
        assert move.sources and move.reason, move.isin
        assert move.kind in {"MARKET_MOVE", "UNSOURCED_ACTION", "UNSOURCED_PRICE_STEP"}, move.isin


def _sources() -> list[tuple[str, TermSource]]:
    curated = load_manual_actions()
    rows: list[CuratedAction | ExplainedMove] = [
        *curated.actions,
        *curated.dividends,
        *curated.explained_moves,
    ]
    return [(r.isin, s) for r in rows for s in r.sources]


@pytest.mark.parametrize(("isin", "source"), _sources(), ids=[i for i, _ in _sources()])
def test_every_quoted_line_is_in_l0_where_the_lake_is_here(isin: str, source: TermSource) -> None:
    path = _LAKE / source.l0_key
    if not path.is_file():
        pytest.skip(f"L0 object {source.l0_key} is not on this host (no lake, e.g. CI)")
    assert source.line is not None
    # A PR bundle or bhavcopy is quoted from a member of its zip; a JSON payload from the file.
    raw = path.read_bytes() if source.member is None else zipfile.ZipFile(path).read(source.member)
    lines = raw.decode("latin-1").splitlines()
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
    if row.ex_date_rule is not None:
        # EX_DT blank on the line: the one derived case (T+1, ex = record), and only then.
        assert not fields[6].strip(), f"{row.isin}: the line states an ex-date; transcribe it"
        assert row.ex_date_rule is ExDateRule.T1_RECORD_DATE, row.isin
        assert row.record_date is not None and row.record_date >= T1_SETTLEMENT_FROM, row.isin
        assert row.ex_date == row.record_date == _bc_date(fields[3]), row.isin
        return
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


# ── the one derived term: a T+1 ex-date from a stated record date ────────────────────────────

_DERIVED = """\
actions:
  - isin: INE483S01038
    company: AvenuesAI
    filed_against_isin: INE483S01020
    action_type: SPLIT
    from_value: 1
    to_value: 10
    ex_date_derived: T1_RECORD_DATE
    record_date: 2026-10-13
    knowable_date: 2026-10-12
    checked: 2026-11-01
    sources:
      - l0_key: x
        quote: EQ,CCAVENUE,AvenuesAI Limited,2026-10-13,,,,,,CNSLDATNRE1 TO RS10
"""


def test_a_t1_split_with_a_blank_ex_date_derives_it_from_the_record_date(tmp_path: Path) -> None:
    path = tmp_path / "derived.yaml"
    path.write_text(_DERIVED)
    [row] = load_manual_actions(path).actions
    assert row.ex_date == row.record_date == date(2026, 10, 13)
    assert row.ex_date_rule is ExDateRule.T1_RECORD_DATE
    # A stated row carries no rule, so the two can never be confused.
    [stated] = load_manual_actions(_write(tmp_path)).actions
    assert stated.ex_date_rule is None


def _write(tmp_path: Path) -> Path:
    good = tmp_path / "good.yaml"
    good.write_text(_GOOD)
    return good


@pytest.mark.parametrize(
    ("patch", "replacement", "reason"),
    [
        # a stated ex-date beside the rule: a derived date must never look stated
        (
            "    record_date: 2026-10-13\n",
            "    record_date: 2026-10-13\n    ex_date: 2026-10-13\n",
            "never both",
        ),
        # T+2 era: the lag varied, never derive
        ("record_date: 2026-10-13", "record_date: 2023-01-25", "does not apply"),
        ("record_date: 2026-10-13", "record_date: 2016-03-10", "does not apply"),
        # a record date on a weekend is no session
        ("record_date: 2026-10-13", "record_date: 2026-10-17", "does not apply"),
        # no record date stated: nothing to derive from
        ("    record_date: 2026-10-13\n", "", "needs the record_date"),
        # only a split/consolidation
        (
            "    action_type: SPLIT\n    from_value: 1\n    to_value: 10\n",
            "    action_type: BONUS\n    new_shares: 1\n    held_shares: 1\n",
            "only a SPLIT",
        ),
        ("ex_date_derived: T1_RECORD_DATE", "ex_date_derived: GUESSED", "ex_date_derived"),
    ],
)
def test_the_derived_ex_date_is_refused_outside_its_rule(
    tmp_path: Path, patch: str, replacement: str, reason: str
) -> None:
    assert _DERIVED.count(patch) == 1
    path = tmp_path / "bad.yaml"
    path.write_text(_DERIVED.replace(patch, replacement))
    with pytest.raises(ManualActionsError, match=reason):
        load_manual_actions(path)


def test_the_t1_rule_starts_on_2023_01_27_and_needs_a_session() -> None:
    always = lambda _d: True  # noqa: E731
    assert T1_SETTLEMENT_FROM == date(2023, 1, 27)
    derived = derive_ex_date(date(2023, 1, 27), is_session=always)
    assert derived is not None and derived.ex_date == date(2023, 1, 27)
    assert derive_ex_date(date(2023, 1, 26), is_session=always) is None
    assert derive_ex_date(date(2026, 10, 13), is_session=lambda _d: False) is None
    assert derive_ex_date(None, is_session=always) is None


def test_avenuesai_consolidates_ten_into_one_and_closes_the_step() -> None:
    """Re 1 -> Rs 10: x10 on every bar before 2026-10-13, so 13.83 reads 138.30 against 130.05."""
    [row] = load_manual_actions().actions_for("INE483S01038")
    assert row.ex_date == date(2026, 10, 13) and row.ex_date_rule is ExDateRule.T1_RECORD_DATE
    assert row.filed_against_isin == "INE483S01020"
    chain = with_events(FactorChain(isin="INE483S01038", rows=()), [row.as_action()])
    [factor] = chain.rows
    # Inverted (a split, not a consolidation) this is 0.1 and the step becomes x94.
    assert factor.price_factor == Decimal(10) and factor.qty_factor == Decimal("0.1")
    adjusted = Decimal("13.83") * chain.price_factor_asof(date(2026, 10, 12))
    assert adjusted == Decimal("138.30")
    assert chain.price_factor_asof(date(2026, 10, 19)) == Decimal(1)
    assert Decimal("0.9") < Decimal("130.05") / adjusted < Decimal("1.0")


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


def test_an_unsourced_step_is_its_own_class_not_an_explained_move() -> None:
    day = date(2006, 9, 12)
    classified = {
        step.trade_date: cls
        for step, cls in classify_steps(
            [_step(day), _step(day + timedelta(days=3))],
            structural_dates={},
            explained_dates={},
            unsourced_dates={STOCK: (day,)},
            threshold=Decimal(2),
            max_gap_days=5,
        )
    }
    # exact session only, like an explained move — and never classed as one
    assert classified == {
        day: StepClass.UNSOURCED,
        day + timedelta(days=3): StepClass.UNEXPLAINED,
    }


def test_an_unsourced_step_passes_the_check_but_raises_a_warn_finding() -> None:
    from dataplatform.quality.l2_continuity import L2ContinuityReport, findings

    day = date(2006, 9, 12)
    report = L2ContinuityReport(
        threshold=Decimal(2),
        max_gap_days=5,
        partitions=1,
        steps=((_step(day), StepClass.UNSOURCED),),
        retired_partitions=(),
    )
    assert report.passed and report.unsourced == (_step(day),)
    [finding] = findings(report, logical_date=day)
    assert finding.severity == "WARN"
    assert finding.isin == STOCK and finding.logical_date == day
    assert finding.detail["kind"] == "unsourced_action_step"
    # an explained move raises nothing: the WARN is what tells the two apart on the status API
    explained = L2ContinuityReport(
        threshold=Decimal(2),
        max_gap_days=5,
        partitions=1,
        steps=((_step(day), StepClass.EXPLAINED_MOVE),),
        retired_partitions=(),
    )
    assert findings(explained, logical_date=day) == ()


def test_the_cli_prints_each_unsourced_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dataplatform.quality import l2_continuity

    day = date(2006, 9, 12)
    report = l2_continuity.L2ContinuityReport(
        threshold=Decimal(2),
        max_gap_days=5,
        partitions=1,
        steps=((_step(day), StepClass.UNSOURCED),),
        retired_partitions=(),
    )
    monkeypatch.setattr(l2_continuity, "scan", lambda *_a, **_k: report)
    monkeypatch.setattr("dataplatform.store.db.connect", _NullConnection)
    monkeypatch.setattr("dataplatform.identity.lineage.LineageStore", lambda _conn: _NullLineage())
    assert l2_continuity.main([]) == 0
    out = capsys.readouterr().out
    assert f"UNSOURCED {STOCK} NSE 2006-09-11 -> 2006-09-12" in out
    assert "quarantined before 2006-09-12" in out
    assert "unsourced              1" in out


class _NullConnection:
    def __enter__(self) -> _NullConnection:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _NullLineage:
    def load(self) -> _NullLineage:
        return self

    def survivor_of(self, isin: str) -> str:
        return isin


def test_the_check_reads_curated_breaks_and_moves_without_postgres(tmp_path: Path) -> None:
    from dataplatform.quality.l2_continuity import scan

    curated = ManualActions(actions=(), explained_moves=())
    report = scan(None, survivor_of=lambda i: i, data_root=tmp_path, curated=curated)
    assert report.passed and report.partitions == 0
    real = load_manual_actions()
    assert real.structural_dates() == {
        "INE069A01017": (date(2016, 1, 20),),
        "INE133B01019": (date(2010, 5, 13),),
        "INE429C01035": (date(2017, 5, 25),),
        "INE966H01019": (date(2010, 4, 15),),
    }
    assert set(real.explained_dates()) == {
        "INE043A01012",
        "INE111B01023",
        "INE247G01024",
        "INE483S01020",
        "INE528G01035",
    }
    # the unsourced basis steps are not explained moves: their own class, and a quarantine
    assert real.unsourced_windows() == {
        "INE230A01023": date(2006, 9, 12),
        "INE640C01011": date(2006, 2, 13),
        "INE780C01023": date(2008, 9, 8),
    }
    assert not set(real.unsourced_dates()) & set(real.explained_dates())


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
