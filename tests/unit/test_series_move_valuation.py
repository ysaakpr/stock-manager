"""A held name NSE moves off EQ is marked and sold in the series it really trades in (X2).

The backtest reads NSE series ``EQ`` only (``backtest.run._L1Reader._SCOPE``). When NSE moves a
still-trading name into trade-for-trade (BE/BZ) it keeps printing, under the same ISIN, in that
series. Before this fix a holding in it was marked at its last EQ close for as long as it stayed
out, its trailing stop could never fire, and its exit was rejected for want of a reference bar.
And a face-value split's successor that printed only in BE — PC Jeweller, 2024-12-16..30 — had no
close at all, so every NAV sampler skipped those sessions: the equity curve lost rows.

The fixture lake, on a weekday calendar:

* ``MOVER`` trends up in EQ and is bought; from ``MOVE`` it prints only in **BE**, 40% lower.
* ``BE_ONLY`` prints only in BE, and trends hardest of all — if a BE bar ever reached a buy it
  would rank first. It must never be bought.
* ``STEADY``/``FLAT`` are plain EQ names.
* the PC Jeweller shape: ``SPLITTER`` is held, splits 1:10 into a new ISIN ``SUCCESSOR`` on
  ``SPLIT_ON`` (a book action), and the successor prints only in BE for five sessions.

Every test here fails on the EQ-only code (main at 9bb5d7d): the stop never fires because the mark
stays frozen, the successor's sessions are missing from the NAV path, and ``_HoldingMarks`` /
``ReferenceBar.exit_only`` do not exist.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.book_actions import (
    BookActionCalendar,
    RescaleKind,
    ShareRescale,
    corporate_actions_in_force,
)
from backtest.policies.swing_composite import SwingCompositeParameters
from backtest.run import BacktestResult, _HoldingMarks, _L1Market, _L1Reader, run_swing_composite
from dataplatform.clock import FrozenClock
from dataplatform.ingest.indices import TRI_METHOD_PUBLISHED, TriPoint, TriSeries, write_tri_l1
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA
from execution.broker import Exchange, OrderRequest, OrderStatus, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker

_Q: Final = Decimal("0.0001")


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


CALENDAR: Final = _weekdays(date(2012, 1, 2), 320)
START: Final = CALENDAR[260]
MOVE: Final = CALENDAR[290]
SPLIT_ON: Final = CALENDAR[290]
BACK_TO_EQ: Final = CALENDAR[295]

MOVER: Final = "INE001A01011"
STEADY: Final = "INE002A01019"
FLAT: Final = "INE003A01017"
BE_ONLY: Final = "INE004A01015"
SPLITTER: Final = "INE005A01012"
SUCCESSOR: Final = "INE005A01020"


def _row(isin: str, series: str, close: Decimal, session: date) -> dict[str, object]:
    price, volume = close.quantize(_Q), 100_000
    return {
        "isin": isin,
        "exchange": "NSE",
        "symbol": isin[:6],
        "series": series,
        "trade_date": session,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "last": price,
        "prev_close": price,
        "total_traded_qty": volume,
        "total_traded_value": (price * volume).quantize(_Q),
        "total_trades": volume,
        "deliv_qty": volume // 2,
        "deliv_pct": Decimal("50"),
    }


def mover_close(i: int) -> Decimal:
    """``MOVER``'s close on ``CALENDAR[i]``: a steady climb, 40% lower once it is in BE."""
    close = Decimal(100) * Decimal("1.004") ** i
    return close * Decimal("0.6") if CALENDAR[i] >= MOVE else close


def splitter_close(i: int) -> Decimal:
    """The pre-split name's close; the successor prints a tenth of it (a 1:10 split)."""
    return Decimal(100) * Decimal("1.004") ** i


def _write(root: Path, session: date, rows: list[dict[str, object]]) -> None:
    path = l1_partition_path(PRICES_RAW_DATASET, session, data_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=PRICES_RAW_SCHEMA), path)


def _index(root: Path) -> None:
    points = tuple(
        TriPoint(
            index_slug="nifty50",
            index_name="Nifty 50",
            as_of=session,
            tri_value=Decimal(10_000 + i),
            method=TRI_METHOD_PUBLISHED,
        )
        for i, session in enumerate(CALENDAR)
    )
    series = TriSeries(
        index_slug="nifty50", index_name="Nifty 50", method=TRI_METHOD_PUBLISHED, points=points
    )
    write_tri_l1(series, data_root=root)


@pytest.fixture
def mover_lake(tmp_path: Path) -> Path:
    for i, session in enumerate(CALENDAR):
        rows = [
            _row(MOVER, "BE" if session >= MOVE else "EQ", mover_close(i), session),
            _row(STEADY, "EQ", Decimal(100) + Decimal(i) / 10, session),
            _row(FLAT, "EQ", Decimal(50), session),
        ]
        if i >= 280:
            rows.append(_row(BE_ONLY, "BE", Decimal(300) * Decimal("1.01") ** i, session))
        _write(tmp_path, session, rows)
    _index(tmp_path)
    return tmp_path


@pytest.fixture
def split_lake(tmp_path: Path) -> Path:
    for i, session in enumerate(CALENDAR):
        rows = [
            _row(STEADY, "EQ", Decimal(100) + Decimal(i) / 10, session),
            _row(FLAT, "EQ", Decimal(50), session),
        ]
        if session < SPLIT_ON:
            rows.append(_row(SPLITTER, "EQ", splitter_close(i), session))
        else:
            series = "BE" if session < BACK_TO_EQ else "EQ"
            rows.append(_row(SUCCESSOR, series, splitter_close(i) / 10, session))
        _write(tmp_path, session, rows)
    _index(tmp_path)
    return tmp_path


@pytest.fixture
def unaffected_lake(tmp_path: Path) -> Path:
    """The mover lake with the mover's fall printed in EQ: no held name ever leaves EQ.

    ``BE_ONLY`` still prints in BE, so the restricted read is on disk and exercised — and must
    change nothing, because nothing held is in it.
    """
    for i, session in enumerate(CALENDAR):
        rows = [
            _row(MOVER, "EQ", mover_close(i), session),
            _row(STEADY, "EQ", Decimal(100) + Decimal(i) / 10, session),
            _row(FLAT, "EQ", Decimal(50), session),
        ]
        if i >= 280:
            rows.append(_row(BE_ONLY, "BE", Decimal(300) * Decimal("1.01") ** i, session))
        _write(tmp_path, session, rows)
    _index(tmp_path)
    return tmp_path


_PARAMS: Final = SwingCompositeParameters(top_n=2, sell_band=3)


def _run(root: Path) -> BacktestResult:
    return run_swing_composite(
        start=START, end=CALENDAR[-1], parameters=_PARAMS, data_root=root, adjusted=False
    )


@pytest.fixture
def mover_run(mover_lake: Path) -> BacktestResult:
    return _run(mover_lake)


@pytest.fixture
def split_run(split_lake: Path) -> Iterator[BacktestResult]:
    split = ShareRescale(
        isin=SUCCESSOR,
        ex_date=SPLIT_ON,
        kind=RescaleKind.SPLIT,
        numerator=Decimal(10),
        denominator=Decimal(1),
        carried_from=SPLITTER,
    )
    with corporate_actions_in_force(BookActionCalendar([split]), apply_to_book=True):
        yield _run(split_lake)


# ── the fixture's own premises ───────────────────────────────────────────────────────────────────


def test_the_mover_is_bought_in_eq_before_it_moves(mover_run: BacktestResult) -> None:
    """Without this the suite could pass vacuously: the mover must be held when it leaves EQ."""
    buys = [e for e in mover_run.result.journal if e.isin == MOVER and e.decision.value == "BUY"]
    assert buys and all(e.trading_date < MOVE for e in buys)


def test_the_splitter_is_held_into_its_split(split_run: BacktestResult) -> None:
    buys = [e for e in split_run.result.journal if e.isin == SPLITTER and e.decision.value == "BUY"]
    assert buys and all(e.trading_date < SPLIT_ON for e in buys)


# ── a held name that moves EQ -> BE ──────────────────────────────────────────────────────────────


def test_a_held_name_moved_to_be_is_marked_at_its_be_close(mover_run: BacktestResult) -> None:
    """On the move session the NAV carries the mover at the 40%-lower BE close, not the EQ one.

    Frozen at its last EQ close, the book would read the mover ~0.4% *up* that session; at its BE
    close it is ~40% down. The stop's own rationale names the mark it read.
    """
    nav = dict(mover_run.nav_path)
    before, on_move = nav[CALENDAR[289]], nav[MOVE]
    assert on_move < before * Decimal("0.95")
    stop = next(
        e
        for e in mover_run.result.journal
        if e.isin == MOVER and e.decision.value == "SELL" and e.trading_date == MOVE
    )
    assert f"trailing stop: {mover_close(290).quantize(_Q)}" in (stop.rationale or "")


def test_a_held_name_moved_to_be_is_sold_at_the_be_bar(mover_run: BacktestResult) -> None:
    """The exit fills on the next session's BE bar, net of the one shared cost model's charges.

    Priced off the frozen EQ close it would net ~1.67x as much; at cost basis, more again. The
    proceeds sit just under quantity x BE open: slippage and charges, nothing else.
    """
    assert mover_run.ledger is not None
    (sell,) = (t for t in mover_run.ledger.trades if t.isin == MOVER and t.side is Side.SELL)
    assert sell.trade_date == CALENDAR[291]
    gross_at_be_open = mover_close(291).quantize(_Q) * sell.quantity
    assert gross_at_be_open * Decimal("0.97") < sell.net_amount < gross_at_be_open
    assert MOVER not in {h["isin"] for h in mover_run.book.holdings}


def test_a_name_trading_only_in_be_is_never_bought(mover_run: BacktestResult) -> None:
    """The steepest trend in the lake, but only ever in BE: no order, no fill, no holding."""
    assert all(e.isin != BE_ONLY for e in mover_run.result.journal)
    assert mover_run.ledger is not None
    assert all(t.isin != BE_ONLY for t in mover_run.ledger.trades)


def test_the_equity_curve_has_a_row_every_session_across_a_series_move(
    mover_run: BacktestResult,
) -> None:
    sessions = [s for s in CALENDAR if s >= START]
    assert [d for d, _ in mover_run.nav_path] == sessions[: mover_run.sessions]


# ── the PC Jeweller shape: a split successor that prints only in BE ──────────────────────────────


def test_a_split_successor_trading_in_be_leaves_no_gap_in_the_equity_curve(
    split_run: BacktestResult,
) -> None:
    """Root cause of PC Jeweller's lost December 2024 rows: every sampler skipped a session on
    which a held name had no EQ close seen yet, and the successor had none until it reached EQ."""
    dates = [d for d, _ in split_run.nav_path]
    assert len(dates) == split_run.sessions
    assert all(s in dates for s in CALENDAR[290:295])


def test_a_split_successor_is_marked_at_its_be_close(split_run: BacktestResult) -> None:
    """The NAV moves with the successor's BE prints — its ~0.4% daily climb shows in the curve."""
    nav = dict(split_run.nav_path)
    assert nav[CALENDAR[291]] > nav[CALENDAR[290]] > nav[CALENDAR[289]]


# ── the holdings-only seams, unit by unit ────────────────────────────────────────────────────────


@pytest.fixture
def reader(mover_lake: Path) -> Iterator[_L1Reader]:
    reader = _L1Reader(data_root=mover_lake)
    try:
        yield reader
    finally:
        reader.close()


def test_the_eq_cross_section_never_carries_a_be_name(reader: _L1Reader) -> None:
    """Signal, universe and sizing read this — and it stays EQ."""
    assert set(reader.closes_on(MOVE)) == {STEADY, FLAT}


def test_marks_overlay_a_be_close_only_for_a_held_name(reader: _L1Reader) -> None:
    held: list[str] = []
    marks = _HoldingMarks(reader, lambda: held)
    assert set(marks(MOVE)) == {STEADY, FLAT}
    held.append(MOVER)
    assert marks(MOVE)[MOVER] == mover_close(290).quantize(_Q)
    assert BE_ONLY not in marks(MOVE)
    assert marks.restricted_marks == 1


def test_marks_never_read_a_later_session(reader: _L1Reader) -> None:
    """A mark on ``d`` is struck from ``d``'s own partition: no later session is opened.

    The session before the move the mover is in EQ; the BE row that follows it is not visible.
    """
    opened: list[date] = []
    real = reader._partition

    def spy(session: date) -> str:
        opened.append(session)
        return real(session)

    reader._partition = spy  # type: ignore[method-assign]
    marks = _HoldingMarks(reader, lambda: [MOVER])
    assert marks(CALENDAR[289])[MOVER] == mover_close(289).quantize(_Q)
    assert set(opened) == {CALENDAR[289]}
    assert marks(MOVE)[MOVER] == mover_close(290).quantize(_Q)
    assert set(opened) == {CALENDAR[289], MOVE}


@dataclass(frozen=True)
class _Pos:
    isin: str
    average_price: Decimal


def test_a_held_name_with_no_print_anywhere_keeps_its_last_close(reader: _L1Reader) -> None:
    """No bar in any series: unchanged — its last close seen, else its average cost; a row still.

    ``AFTER_LAKE`` has no partition on disk, so nothing printed in any series that session.
    """
    never_printed = "INE999A01010"
    marks = _HoldingMarks(reader, lambda: [MOVER, never_printed])
    marks.nav_prices(MOVE, [])  # sees the mover's BE close
    after_lake = CALENDAR[-1] + timedelta(days=7)
    positions = [_Pos(MOVER, Decimal(1)), _Pos(never_printed, Decimal(7))]
    prices = marks.nav_prices(after_lake, positions)  # type: ignore[arg-type]
    assert prices == {MOVER: mover_close(290).quantize(_Q), never_printed: Decimal(7)}


def test_the_market_serves_a_be_bar_only_to_a_holder(reader: _L1Reader) -> None:
    held: list[str] = []
    market = _L1Market(reader, CALENDAR, held=lambda: held)
    with pytest.raises(NoReferenceBarError):
        market.reference_bar(MOVER, MOVE)
    with pytest.raises(NoReferenceBarError):
        market.reference_bar(BE_ONLY, MOVE)
    held.append(MOVER)
    bar = market.reference_bar(MOVER, MOVE)
    assert bar.exit_only and bar.open == mover_close(290).quantize(_Q)
    assert not market.reference_bar(STEADY, MOVE).exit_only
    with pytest.raises(NoReferenceBarError):  # EQ-only without a holdings view, as before
        _L1Market(reader, CALENDAR).reference_bar(MOVER, MOVE)


class _OneBarMarket:
    def __init__(self, bar: ReferenceBar) -> None:
        self._bar = bar

    def next_session(self, after: date) -> date:
        return self._bar.session

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        return self._bar


def test_the_broker_refuses_a_buy_on_an_exit_only_bar() -> None:
    """Adding to a held name in BE is a buy too: the segment the account may enter is EQ."""
    day = date(2024, 7, 2)
    bar = ReferenceBar(
        isin=MOVER,
        session=day,
        exchange=Exchange.NSE,
        open=Decimal(100),
        vwap=Decimal(100),
        traded_value=Decimal(10_000_000),
        exit_only=True,
    )
    sim = SimBroker(
        clock=FrozenClock(day - timedelta(days=1)),
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_OneBarMarket(bar),
        opening_cash=Decimal(1_000_000),
    )
    sim.place(OrderRequest(isin=MOVER, side=Side.BUY, quantity=10))
    (filled,) = sim.execute_session(day)
    assert filled.status is OrderStatus.REJECTED
    assert "exit-only" in (filled.reason or "")
    assert sim.cash == Decimal(1_000_000)


def test_an_eq_bar_still_fills_a_buy() -> None:
    """``exit_only`` defaults off: every existing market's bars buy exactly as before."""
    day = date(2024, 7, 2)
    bar = ReferenceBar(
        isin=STEADY,
        session=day,
        exchange=Exchange.NSE,
        open=Decimal(100),
        vwap=Decimal(100),
        traded_value=Decimal(10_000_000),
    )
    sim = SimBroker(
        clock=FrozenClock(day - timedelta(days=1)),
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_OneBarMarket(bar),
        opening_cash=Decimal(1_000_000),
    )
    sim.place(OrderRequest(isin=STEADY, side=Side.BUY, quantity=10))
    (filled,) = sim.execute_session(day)
    assert filled.status is OrderStatus.COMPLETE


# ── unaffected runs are byte-identical ───────────────────────────────────────────────────────────

#: ``ReplayResult.digest()`` (sha256 of journal + book + rail policy) and the NAV path the EQ-only
#: code (main at 9bb5d7d) produced on ``unaffected_lake`` — computed there and pinned here.
UNAFFECTED_REPLAY_DIGEST: Final = "d90da05aa96aa094e454351f645a6ce868617407993fe13a01b08a8d687af627"
UNAFFECTED_NAV_SHA: Final = "6eda66866da46667284857bc055fee0aeedfe0865c2ba906570abd3c085de451"


def _nav_sha(run: BacktestResult) -> str:
    text = "\n".join(f"{d.isoformat()} {v}" for d, v in run.nav_path)
    return hashlib.sha256(text.encode()).hexdigest()


def test_a_run_with_no_held_series_move_is_byte_identical(unaffected_lake: Path) -> None:
    """Journal, book and equity curve match the pre-fix code byte for byte.

    The fix only ever acts on a held name with no EQ print that session; when there is none the
    overlay is empty and every consumer reads the EQ cross-section it always read.
    """
    run = _run(unaffected_lake)
    assert run.result.digest() == UNAFFECTED_REPLAY_DIGEST
    assert _nav_sha(run) == UNAFFECTED_NAV_SHA
