"""X2 — the splits L2 composes (curated, price-implied) reach the backtest book's share counts.

L2 adjusts prices by three event sources: the feeds' reconciled rows, the sourced rows of
``corpactions.manual_actions`` and the splits ``corpactions.implied`` reads off L1. The book used
to take only the first, so a holding across a curated or implied split kept its pre-split count
and showed the price step as a loss. These tests walk the drivers' real accounting stack
(``tests/unit/test_book_actions``'s ``_Walk``) over events composed by
``store.l2.compose_events`` — the one composition the materializer itself uses — and pin:

* the count after a curated split and after an implied split, with NAV continuous across it;
* an inverted ratio is refused at load, and an inverted rescale shows as a NAV jump;
* a feed split and an added event on the same date are applied once, never twice;
* PIT: a curated row knowable only after its ex-date is refused; nothing applies before the
  ex-date; a structural break scales nothing;
* the run identity names the added events, and leaves a feed-only calendar's unchanged;
* two runs are byte-identical.

Offline: in-memory bars and market; ``compose_lake_events`` over a synthetic L1 in ``tmp_path``.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from backtest.accounting import BookError
from backtest.book_actions import (
    BookActionCalendar,
    RescaleKind,
    RescaleSource,
    ShareRescale,
    UnmodelledAction,
    _to_book_actions,
    added_book_events,
)
from backtest.run_ledger import _actions_identity
from dataplatform.corpactions import MANUAL_SOURCE, ActionType, FaceValueTerms, UnquantifiedTerms
from dataplatform.corpactions.factors import FactorChain, build_chain_for_isin
from dataplatform.corpactions.implied import IMPLIED_SOURCE
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.store.db import Connection
from dataplatform.store.l2 import (
    ComposedEvents,
    RawBar,
    compose_events,
    compose_lake_events,
    materialize_isin,
    read_adjusted,
)
from tests.unit.test_book_actions import _buy, _Scripted, _Walk
from tests.unit.test_implied_splits import GOLDBEES, _Store, _write_goldbees

A = "INE001A01036"


def _sessions(start: date, n: int) -> tuple[date, ...]:
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return tuple(out)


#: Twelve sessions at ₹200, the ex-date at ₹100 with the traded quantity doubled, four after it —
#: what a 2:1 sub-division prints, and enough history for the implied scan's volume median.
SESSIONS = _sessions(date(2023, 12, 4), 17)
EX = SESSIONS[12]
BEFORE, AFTER = Decimal("200"), Decimal("100")


def _prices() -> dict[tuple[str, date], Decimal]:
    return {(A, day): (BEFORE if day < EX else AFTER) for day in SESSIONS}


def _raw_bars() -> tuple[RawBar, ...]:
    return tuple(
        RawBar(
            isin=A,
            exchange="NSE",
            trade_date=day,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=12_000 if day >= EX else 5_000,
        )
        for (_, day), price in sorted(_prices().items())
    )


def _split(
    *, source: str, ex: date = EX, knowable: date | None = None, ratio: tuple[int, int] = (10, 5)
) -> CorporateAction:
    return CorporateAction(
        isin=A,
        ex_date=ex,
        action_type=ActionType.SPLIT,
        terms=FaceValueTerms(from_value=Decimal(ratio[0]), to_value=Decimal(ratio[1])),
        source=source,
        raw_text="test",
        knowable_date=ex - timedelta(days=7) if knowable is None else knowable,
    )


def _compose(
    *, recorded: tuple[CorporateAction, ...] = (), curated: tuple[CorporateAction, ...] = ()
) -> ComposedEvents:
    return compose_events(
        A,
        chain=FactorChain(isin=A, rows=()),  # the persisted chain: empty unless a test says so
        actions=recorded,
        raw_bars=_raw_bars(),
        curated=curated,
    )


def _calendar(
    recorded: tuple[CorporateAction, ...], composed: tuple[ComposedEvents, ...]
) -> BookActionCalendar:
    """What ``load_book_actions`` builds: the feed rows plus what L2 added, in book terms."""
    rows = (*recorded, *added_book_events(recorded, composed))
    return BookActionCalendar(_to_book_actions(rows, lambda isin: (isin,), lambda _: None))


def _walk(calendar: BookActionCalendar | None) -> _Walk:
    walk = _Walk(_prices(), _Scripted({SESSIONS[0]: (_buy(A, 100),)}), calendar, SESSIONS)
    walk.run()
    return walk


def _held(walk: _Walk) -> int:
    position = walk.book.position(A)
    assert position is not None
    assert walk.sim.held_quantity(A) == position.quantity  # the two books agree
    return position.quantity


# ── a held position across a curated split, and across an implied one ─────────────────────────


def test_a_curated_split_doubles_the_held_count_and_keeps_nav_continuous() -> None:
    composed = _compose(curated=(_split(source=MANUAL_SOURCE),))
    assert len(composed.curated) == 1
    assert composed.implied == ()  # the curated split is recorded to the scan: never implied too
    calendar = _calendar((), (composed,))
    assert calendar.counts() == {"SPLIT:curated": 1}

    walk = _walk(calendar)
    assert _held(walk) == 200
    assert walk.nav[EX] == walk.nav[SESSIONS[11]]  # cash-neutral: no step across the ex-date
    assert walk.nav[SESSIONS[-1]] == walk.nav[SESSIONS[11]]


def test_an_implied_split_doubles_the_held_count_and_keeps_nav_continuous() -> None:
    composed = _compose()
    [implied] = composed.implied
    assert implied.ex_date == EX and implied.price_factor == Decimal("0.5")
    calendar = _calendar((), (composed,))
    assert calendar.counts() == {"SPLIT:implied": 1}
    [rescale] = calendar.between(None, date.max)
    assert isinstance(rescale, ShareRescale)
    assert rescale.source is RescaleSource.IMPLIED
    assert rescale.knowable_date == EX  # knowable on the session whose bar shows it

    walk = _walk(calendar)
    assert _held(walk) == 200
    assert walk.nav[EX] == walk.nav[SESSIONS[11]]


def test_without_the_added_events_the_split_is_the_fake_fifty_percent_drawdown() -> None:
    walk = _walk(_calendar((), ()))
    assert _held(walk) == 100
    assert walk.nav[EX] < walk.nav[SESSIONS[11]] - Decimal("9900")


def test_the_book_and_l2_scale_by_reciprocal_factors() -> None:
    # L2 multiplies the pre-ex close by 0.5; the book multiplies the count by 2. Inverted, the
    # count would halve and the pre-ex adjusted close would read 400.
    composed = _compose()
    [row] = composed.chain.rows
    assert row.price_factor == Decimal("0.5") and row.qty_factor == Decimal(2)
    [rescale] = _calendar((), (composed,)).between(None, date.max)
    assert isinstance(rescale, ShareRescale)
    assert rescale.numerator / rescale.denominator == row.qty_factor


# ── an inverted ratio fails ────────────────────────────────────────────────────────────────────


def test_an_added_split_whose_ratio_disagrees_with_l2s_chain_is_refused() -> None:
    curated = _split(source=MANUAL_SOURCE)
    inverted_chain = build_chain_for_isin(A, [_split(source=MANUAL_SOURCE, ratio=(5, 10))])
    composed = ComposedEvents(isin=A, chain=inverted_chain, actions=(curated,), curated=(curated,))
    with pytest.raises(BookError, match=r"L2 adjusts prices by x0\.5"):
        added_book_events((), (composed,))


def test_an_inverted_curated_rescale_is_the_nav_jump_it_should_be() -> None:
    inverted = ShareRescale(
        isin=A,
        ex_date=EX,
        kind=RescaleKind.SPLIT,
        numerator=Decimal(5),
        denominator=Decimal(10),
        source=RescaleSource.CURATED,
        knowable_date=EX,
    )
    walk = _walk(BookActionCalendar([inverted]))
    assert _held(walk) == 50
    assert walk.nav[EX] != walk.nav[SESSIONS[11]]


# ── one fact, one rescale ──────────────────────────────────────────────────────────────────────


def test_a_feed_split_the_persisted_chain_lags_is_not_implied_back_on_top_of_itself() -> None:
    # The feed row is reconciled but `adjustment_factors` has not caught up: L2 implies the very
    # same 2:1 back from L1 on the same ex-date. Applying both would quadruple the count.
    feed = _split(source="nse_corp_actions")
    composed = _compose(recorded=(feed,))
    assert len(composed.implied) == 1
    assert added_book_events((feed,), (composed,)) == ()

    calendar = _calendar((feed,), (composed,))
    assert calendar.counts() == {"SPLIT": 1}
    assert _held(_walk(calendar)) == 200


def test_a_curated_row_a_feed_has_since_published_is_applied_once() -> None:
    feed = _split(source="nse_corp_actions")
    current_chain = build_chain_for_isin(A, [feed])
    composed = compose_events(
        A,
        chain=current_chain,
        actions=(feed,),
        raw_bars=_raw_bars(),
        curated=(_split(source=MANUAL_SOURCE),),
    )
    assert composed.added == ()  # dropped in the feed's favour, and nothing left to imply
    assert _held(_walk(_calendar((feed,), (composed,)))) == 200


def test_a_feed_and_an_added_event_that_compose_on_one_date_are_both_applied() -> None:
    # A recorded 1:1 bonus with an unpublished 2:1 split the same day (INE096L01025's shape):
    # L2's chain carries both, so the book must too — x4, not x2.
    feed = CorporateAction(
        isin=A,
        ex_date=EX,
        action_type=ActionType.BONUS,
        terms={"kind": "ratio", "new_shares": "1", "held_shares": "1"},  # type: ignore[arg-type]
        source="nse_corp_actions",
        raw_text="test",
        knowable_date=EX,
    )
    curated = _split(source=MANUAL_SOURCE)
    composed = compose_events(
        A,
        chain=build_chain_for_isin(A, [feed]),
        actions=(feed,),
        raw_bars=_raw_bars(),
        curated=(curated,),
        infer_splits=False,
    )
    assert added_book_events((feed,), (composed,)) == (curated,)


# ── PIT ────────────────────────────────────────────────────────────────────────────────────────


def test_a_curated_split_knowable_only_after_its_ex_date_is_refused() -> None:
    late = _split(source=MANUAL_SOURCE, knowable=EX + timedelta(days=1))
    composed = _compose(curated=(late,))
    with pytest.raises(ValueError, match="before it was known"):
        _calendar((), (composed,))


def test_nothing_is_applied_before_the_ex_date() -> None:
    walk = _walk(_calendar((), (_compose(curated=(_split(source=MANUAL_SOURCE),)),)))
    before = SESSIONS[11]
    held_before = dict(walk.settled[before])
    for isin, _, quantity in walk.pending[before]:
        held_before[isin] = held_before.get(isin, 0) + quantity
    assert held_before == {A: 100}
    assert walk.settled[EX] == {A: 200}


def test_a_curated_structural_break_scales_nothing() -> None:
    demerger = CorporateAction(
        isin=A,
        ex_date=EX,
        action_type=ActionType.DEMERGER,
        terms=UnquantifiedTerms(),
        source=MANUAL_SOURCE,
        raw_text="test",
        knowable_date=EX - timedelta(days=7),
    )
    composed = compose_events(
        A,
        chain=FactorChain(isin=A, rows=()),
        actions=(),
        raw_bars=_raw_bars(),
        curated=(demerger,),
        infer_splits=False,
    )
    calendar = _calendar((), (composed,))
    assert list(calendar.between(None, date.max)) == [UnmodelledAction(A, EX, "DEMERGER")]
    assert _held(_walk(calendar)) == 100


# ── identity and determinism ───────────────────────────────────────────────────────────────────


def test_the_run_identity_names_the_added_events_and_their_ratio() -> None:
    feed_only = BookActionCalendar(
        [ShareRescale(A, EX, RescaleKind.SPLIT, Decimal(10), Decimal(5))]
    )
    assert _actions_identity(feed_only) == "calendar[1]:SPLIT=1"  # pre-change digests hold

    two = _calendar((), (_compose(curated=(_split(source=MANUAL_SOURCE),)),))
    three = _calendar((), (_compose(curated=(_split(source=MANUAL_SOURCE, ratio=(15, 5)),)),))
    assert _actions_identity(two).startswith("calendar[1]:SPLIT:curated=1;added_rescales[1]:")
    assert _actions_identity(two) != _actions_identity(three)


def test_two_runs_across_an_implied_split_are_byte_identical() -> None:
    first = _Walk(
        _prices(),
        _Scripted({SESSIONS[0]: (_buy(A, 100),)}),
        _calendar((), (_compose(),)),
        SESSIONS,
    ).run()
    second = _Walk(
        _prices(),
        _Scripted({SESSIONS[0]: (_buy(A, 100),)}),
        _calendar((), (_compose(),)),
        SESSIONS,
    ).run()
    assert first.journal_bytes() == second.journal_bytes()
    assert first.book_bytes() == second.book_bytes()


# ── the lake-wide read is the materializer's composition ──────────────────────────────────────


def test_compose_lake_events_returns_what_the_materializer_composes(tmp_path: Path) -> None:
    _write_goldbees(tmp_path)
    [composed] = compose_lake_events(cast(Connection, _Store()), data_root=tmp_path)
    assert composed.isin == GOLDBEES
    [event] = composed.implied
    assert event.as_action().source == IMPLIED_SOURCE

    report = materialize_isin(
        GOLDBEES, chain=FactorChain(isin=GOLDBEES, rows=()), actions=(), data_root=tmp_path
    )
    assert report.implied_splits == composed.implied
    pre_ex = read_adjusted(GOLDBEES, data_root=tmp_path)[-2]
    assert pre_ex.cum_qty_factor == composed.chain.qty_factor_asof(pre_ex.trade_date)

    [rescale] = _calendar((), (composed,)).between(None, date.max)
    assert isinstance(rescale, ShareRescale)
    assert (rescale.numerator, rescale.denominator) == (Decimal(100), Decimal(1))


class _FactorStore:
    """A stand-in Postgres whose only rows are the persisted price factors the prefilter reads."""

    def __init__(self, factors: list[tuple[str, date, Decimal]]) -> None:
        self._factors = factors
        self._rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: object = None) -> _FactorStore:
        self._rows = list(self._factors) if "FROM adjustment_factors WHERE" in sql else []
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


def test_the_prefilter_keeps_every_isin_whose_adjusted_series_steps(tmp_path: Path) -> None:
    from dataplatform.identity.master import Exchange
    from dataplatform.store.l1 import write_prices_raw
    from dataplatform.store.l2 import _step_isins, open_connection
    from tests.unit.test_implied_splits import _row

    flat, recorded, phantom = "INE002A01018", "INE009A01021", "INE062A01020"
    days = SESSIONS[:15]
    for day in days:
        after = day >= EX
        write_prices_raw(
            [
                _row(flat, day, o="50", c="50", qty=10),
                # A raw 2:1 the persisted chain already adjusts: no step left in L2's terms.
                _row(
                    recorded, day, o="100" if after else "200", c="100" if after else "200", qty=10
                ),
                # Flat in raw, but a persisted factor puts a 2x step into the adjusted series.
                _row(phantom, day, o="80", c="80", qty=10),
            ],
            exchange=Exchange.NSE,
            data_root=tmp_path,
        )
    store = _FactorStore([(recorded, EX, Decimal("0.5")), (phantom, EX, Decimal("0.5"))])
    con = open_connection()
    try:
        kept = _step_isins(cast(Connection, store), con, data_root=tmp_path)
    finally:
        con.close()
    # Inverted (the factor divided rather than multiplied), `recorded` would read a 4x step and
    # be kept while `phantom` would still be — so the exact set pins the direction.
    assert kept == frozenset({phantom})
