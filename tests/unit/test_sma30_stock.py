"""Research part 3 (backtest.sma30_run / backtest.policies.sma30_stock): the stock-level 30-DMA
features are split-safe, point-in-time, and fire the stated rules on the stated sessions.

An offline lake in ``tmp_path``: raw L1 throughout, L2 by the lake's own writer.

* ``SPLITTER`` rises steadily on its true (adjusted) price and splits 1:2 late in the window. On
  raw closes that halving is a crash through every average — a fake death cross. On the adjusted
  basis nothing happens. ``RAW_SPLITTER`` has no L2 at all: with the store's split factors in force
  it is adjusted on the fly; without them the raw step shows through, which is the inversion the
  split test must catch.
* ``CROSSER`` is flat, then rises: EMA10 crosses SMA30 on the first rising session.
* ``PULLER`` rises, dips to its SMA30 on one session and bounces on the next.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.book_actions import (
    BookActionCalendar,
    RescaleKind,
    ShareRescale,
    current_signal_split_factors,
    signal_split_factors,
)
from backtest.policies.sma30_stock import Entry, Sma30Parameters, Sma30Record, Sma30StockPolicy
from backtest.sma30_run import ARMS, Sma30Features
from dataplatform.corpactions.factors import FactorChain, build_factor_chain
from dataplatform.corpactions.taxonomy import ActionType, FaceValueTerms
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.store.l2 import materialize_isin
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

SPLITTER = "INE094A01015"
RAW_SPLITTER = "INE009A01021"
CROSSER = "INE002A01018"
PULLER = "INE040A01034"
_Q = Decimal("0.01")


def _weekdays(start: date, count: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


SESSIONS = _weekdays(date(2019, 1, 1), 320)
SPLIT_AT = 300
RISE_AT = 280
DIP_AT = 300  # PULLER's pullback session; the bounce is the next one


def _rising(i: int) -> Decimal:
    return (Decimal("100") * Decimal("1.004") ** i).quantize(_Q)


def _puller() -> list[tuple[Decimal, Decimal]]:
    """(close, low) per session: a 0.4%/day rise, a dip onto SMA30 at DIP_AT, a bounce after."""
    closes: list[Decimal] = []
    lows: list[Decimal] = []
    for i in range(len(SESSIONS)):
        if i == DIP_AT:
            prior = sum(closes[-29:], Decimal(0))
            close = (Decimal("1.005") * prior / Decimal("28.995")).quantize(_Q)
            sma = (prior + close) / 30
            closes.append(close)
            lows.append((sma * Decimal("1.01")).quantize(_Q))
        elif i == DIP_AT + 1:
            closes.append((closes[-1] * Decimal("1.02")).quantize(_Q))
            lows.append(closes[-1])
        elif i > DIP_AT + 1:
            closes.append((closes[-1] * Decimal("1.004")).quantize(_Q))
            lows.append(closes[-1])
        else:
            closes.append(_rising(i))
            lows.append(closes[-1])
    return list(zip(closes, lows, strict=True))


PULL = _puller()


def _raw(isin: str, i: int) -> tuple[Decimal, Decimal]:
    if isin in (SPLITTER, RAW_SPLITTER):
        px = _rising(i)
        if i >= SPLIT_AT:
            px = (px / 2).quantize(_Q)
        return px, px
    if isin == CROSSER:
        px = Decimal("100") if i < RISE_AT else _rising(i - RISE_AT + 1)
        return px, px
    return PULL[i]


def _write(root: Path, session: date, rows: list[tuple[str, Decimal, Decimal]]) -> None:
    records = [
        {
            "isin": isin,
            "exchange": "NSE",
            "symbol": isin[:6],
            "series": "EQ",
            "trade_date": session,
            "open": close,
            "high": close,
            "low": low,
            "close": close,
            "last": close,
            "prev_close": close,
            "total_traded_qty": 100_000,
            "total_traded_value": close * 100_000,
            "total_trades": 1000,
            "deliv_qty": None,
            "deliv_pct": None,
        }
        for isin, close, low in rows
    ]
    path = l1_partition_path(PRICES_RAW_DATASET, session, data_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(records, schema=PRICES_RAW_SCHEMA), path)


def _split() -> CorporateAction:
    return CorporateAction(
        isin=SPLITTER,
        ex_date=SESSIONS[SPLIT_AT],
        action_type=ActionType.SPLIT,
        terms=FaceValueTerms(from_value=Decimal("10"), to_value=Decimal("5")),
        source="nse_corp_actions",
        raw_text="FV SPLIT FROM RS.10/- TO RS.5/-",
        knowable_date=SESSIONS[SPLIT_AT],
    )


_RESCALES = BookActionCalendar(
    [
        ShareRescale(SPLITTER, SESSIONS[SPLIT_AT], RescaleKind.SPLIT, Decimal(10), Decimal(5)),
        ShareRescale(RAW_SPLITTER, SESSIONS[SPLIT_AT], RescaleKind.SPLIT, Decimal(10), Decimal(5)),
    ]
)


def _build(root: Path, *, alter_after: int | None = None) -> Path:
    names = (SPLITTER, RAW_SPLITTER, CROSSER, PULLER)
    for i, session in enumerate(SESSIONS):
        rows = []
        for isin in names:
            close, low = _raw(isin, i)
            if alter_after is not None and i > alter_after:
                close = low = (close * (Decimal("0.5") if i % 2 else Decimal("1.7"))).quantize(_Q)
            rows.append((isin, close, low))
        _write(root, session, rows)
    materialize_isin(
        SPLITTER, chain=build_factor_chain((_split(),)), actions=(_split(),), data_root=root
    )
    for isin in (CROSSER, PULLER):
        materialize_isin(isin, chain=FactorChain(isin=isin, rows=()), actions=(), data_root=root)
    return root


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    return _build(tmp_path / "a")


def _load(lake: Path, factors: BookActionCalendar | None) -> dict[date, dict[str, Sma30Record]]:
    with signal_split_factors(factors):
        features = Sma30Features(data_root=lake, split_factors=current_signal_split_factors())
    try:
        features.load(SESSIONS[0], SESSIONS[-1])
        return {s: {r.isin: r for r in features.records(s)} for s in SESSIONS}
    finally:
        features.close()


def test_a_split_is_not_a_death_cross(lake: Path) -> None:
    by_day = _load(lake, _RESCALES)
    for session in SESSIONS[SPLIT_AT - 2 :]:
        for isin in (SPLITTER, RAW_SPLITTER):
            record = by_day[session].get(isin)
            # A steadily rising name never sits below its averages: no row, or a row above 1.
            assert record is None or (record.ema_to_sma >= 1 and record.close_to_sma >= 1), (
                isin,
                session,
                record,
            )


def test_without_split_factors_the_raw_step_shows_through(lake: Path) -> None:
    # The inversion: on raw closes the 1:2 split is a fake crash through SMA30 (L2-less name).
    by_day = _load(lake, None)
    record = by_day[SESSIONS[SPLIT_AT]].get(RAW_SPLITTER)
    assert record is not None and record.close_to_sma < Decimal("0.6")
    # The name with L2 is still read on the adjusted series.
    assert by_day[SESSIONS[SPLIT_AT]].get(SPLITTER) is None


def test_golden_cross_fires_on_the_first_rising_session(lake: Path) -> None:
    by_day = _load(lake, _RESCALES)
    assert CROSSER not in by_day[SESSIONS[RISE_AT - 1]]  # flat: EMA == SMA, no cross, no row
    first = by_day[SESSIONS[RISE_AT]][CROSSER]
    assert first.golden_cross and first.ema_to_sma > 1
    # Fresh for 5 sessions, then no longer a signal (rows above both averages are not kept).
    assert by_day[SESSIONS[RISE_AT + 4]][CROSSER].golden_cross
    assert CROSSER not in by_day[SESSIONS[RISE_AT + 5]]


def test_bounce_fires_on_the_session_after_the_pullback_only(lake: Path) -> None:
    by_day = _load(lake, _RESCALES)
    bounce = by_day[SESSIONS[DIP_AT + 1]][PULLER]
    assert bounce.bounce and bounce.close_to_sma > 1
    dip = by_day[SESSIONS[DIP_AT]].get(PULLER)
    assert dip is None or not dip.bounce
    later = by_day[SESSIONS[DIP_AT + 2]].get(PULLER)
    assert later is None or not later.bounce


def test_no_future_session_changes_a_past_feature(tmp_path: Path) -> None:
    a = _load(_build(tmp_path / "a"), _RESCALES)
    b = _load(_build(tmp_path / "b", alter_after=DIP_AT + 1), _RESCALES)
    for session in SESSIONS[: DIP_AT + 2]:
        assert a[session] == b[session], session
    assert a[SESSIONS[-1]] != b[SESSIONS[-1]]


# ── the rules the policy reads ──────────────────────────────────────────────────────────────────


def _record(**kw: object) -> Sma30Record:
    base: dict[str, object] = {
        "isin": PULLER,
        "price": Decimal("100"),
        "knowable_date": SESSIONS[0],
        "close_to_sma": Decimal("1.01"),
        "ema_to_sma": Decimal("1.01"),
        "golden_cross": False,
        "bounce": False,
        "momentum_12_1": Decimal("0.2"),
        "volatility": Decimal("0.02"),
    }
    base.update(kw)
    return Sma30Record(**base)  # type: ignore[arg-type]


def _policy(**kw: object) -> Sma30StockPolicy:
    return Sma30StockPolicy(None, Sma30Parameters(**kw))  # type: ignore[arg-type]


def test_exit_rules() -> None:
    plain = _policy(entry=Entry.PULLBACK)
    stopped = _policy(entry=Entry.PULLBACK, protective_exit=Decimal("0.03"))
    assert plain.exits(_record(ema_to_sma=Decimal("0.999"))) is not None  # death cross
    below = _record(close_to_sma=Decimal("0.96"))
    assert plain.exits(below) is None
    assert stopped.exits(below) is not None
    assert stopped.exits(_record(close_to_sma=Decimal("0.98"))) is None


def test_entry_rules_follow_the_arm() -> None:
    cross = _policy(entry=Entry.CROSSOVER)
    pull = _policy(entry=Entry.PULLBACK)
    assert cross.enters(_record(golden_cross=True)) and not pull.enters(_record(golden_cross=True))
    assert pull.enters(_record(bounce=True)) and not cross.enters(_record(bounce=True))


def test_arms_are_the_stated_set() -> None:
    labels = [a.label for a in ARMS]
    assert len(labels) == len(set(labels)) == 10
    assert {a.params.regime_ma_days for a in ARMS} == {None, 200, 30}
    assert all(a.params.max_names == 20 and a.params.max_weight == Decimal("0.10") for a in ARMS)
