"""M9.3 — the momentum backtest constrained to an investable, liquid universe (X2, §7).

Every acceptance criterion of the task is a test here:

  1. the rebalance universe is as-of index membership intersected with a stated liquidity floor,
     point-in-time (``test_universe_is_membership_intersect_liquidity``,
     ``test_membership_screen_is_pit_safe``, ``test_liquidity_screen_is_pit_safe``)
  2. an illiquid name below the floor is excluded on a date it would otherwise rank into the top-N
     (``test_illiquid_top_ranker_is_excluded``)
  3. the run completes and the report states the universe-size and XIRR/turnover delta vs M9.2
     (``test_constrained_run_completes_and_report_states_deltas``)

The lake is built through the *real* seam: raw ``prices_raw`` L1 partitions under ``tmp_path``
with a per-name turnover engineered to bracket the liquidity floor, and real M3.9 constituents
snapshots written through ``write_constituents_l1`` so ``membership_asof`` reads the same on-disk
contract production does. No postgres, no network, deterministic.

The fixture is a six-name market at one rebalance whose twelve-month look-back window is complete:

  * ``STAR``      — highest momentum (+200 %), a member of the index, but **illiquid** (median
    turnover below the floor). The name a raw rank picks and the filter must drop.
  * ``NONMEMBER`` — liquid, +150 % momentum, but **outside the index snapshot**. The name the
    membership screen must drop even though it is perfectly liquid.
  * ``MEMB_A..D`` — liquid index members at +100 / +50 / +20 / +10 %.

With ``top_n = 2`` the raw (full-universe) rank is ``{STAR, NONMEMBER}`` — both excluded by the
filter for different reasons — and the investable rank is ``{MEMB_A, MEMB_B}``. That the decision
moves is the whole point of the task.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.policies.naive_momentum import MomentumParameters
from backtest.run import (
    UniverseParameters,
    _InvestableUniverse,
    _L1MomentumData,
    _L1Reader,
    render_universe_report,
    run_naive_momentum,
    run_universe_report,
)
from dataplatform.corpactions.factors import FactorChain
from dataplatform.ingest.indices import (
    ConstituentRow,
    ConstituentSnapshot,
    write_constituents_l1,
)
from dataplatform.store.l2 import materialize_isin
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

pytestmark = pytest.mark.integration

_PRICE_Q: Final = Decimal("0.0001")

# ── the fixture universe ─────────────────────────────────────────────────────────────────────────

INDEX_SLUG: Final = "niftytest"
INDEX_NAME: Final = "NIFTY Test"

STAR: Final = "INE335Y01020"  # highest momentum, index member, illiquid — the filter must drop it
NONMEMBER: Final = "INE900A01010"  # liquid, high momentum, outside the index — membership drops it
MEMB_A: Final = "INE100A01010"
MEMB_B: Final = "INE200A01010"
MEMB_C: Final = "INE300A01010"
MEMB_D: Final = "INE400A01010"

#: The four liquid index members, keyed to their close level *after* the look-back reference.
LIQUID_MEMBERS: Final = {
    MEMB_A: Decimal("200"),  # +100 %
    MEMB_B: Decimal("150"),  # +50 %
    MEMB_C: Decimal("120"),  # +20 %
    MEMB_D: Decimal("110"),  # +10 %
}

_BASE: Final = Decimal("100")  # every name's close on and before the look-back reference
_STAR_LEVEL: Final = Decimal("300")  # +200 %
_NONMEMBER_LEVEL: Final = Decimal("250")  # +150 %

#: One session per month (each a rebalance) plus a trailing fill-headroom session. The look-back
#: reference for the 2024-02-01 rebalance is 2023-02-01 (a full twelve months back), so that
#: window is complete; earlier rebalances have no twelve-month history and rank nothing.
_SESSIONS: Final = [
    date(2023, 1, 2),
    date(2023, 2, 1),
    date(2023, 3, 1),
    date(2023, 4, 3),
    date(2023, 5, 2),
    date(2023, 6, 1),
    date(2023, 7, 3),
    date(2023, 8, 1),
    date(2023, 9, 1),
    date(2023, 10, 2),
    date(2023, 11, 1),
    date(2023, 12, 1),
    date(2024, 1, 1),
    date(2024, 2, 1),
    date(2024, 2, 2),  # fill-headroom session, never itself replayed
]

REF_SESSION: Final = date(2023, 2, 1)  # look-back reference for the 2024-02-01 rebalance
REBALANCE: Final = date(2024, 2, 1)  # the rebalance whose window is complete

#: The liquidity floor for the fixture (small, so the arithmetic is readable). A liquid name prints
#: turnover far above it every session; STAR prints far below it through the rebalance.
_FLOOR: Final = Decimal("100000")
_LIQUID_VOLUME: Final = 100_000  # close * this is >= 100 * 100000 = 1e7, well above the floor
_STAR_VOLUME: Final = 1  # close * this is <= 300, far below the floor — STAR is illiquid


def _universe_params() -> UniverseParameters:
    """The fixture's a-priori universe screen: this index, this floor, a one-year median window."""
    return UniverseParameters(
        index_slug=INDEX_SLUG,
        median_turnover_floor=_FLOOR,
        liquidity_lookback_days=365,
    )


def _close_of(isin: str, session: date) -> Decimal:
    """A name's raw close: ``_BASE`` on and before the reference, its target level after it."""
    if session <= REF_SESSION:
        return _BASE
    if isin == STAR:
        return _STAR_LEVEL
    if isin == NONMEMBER:
        return _NONMEMBER_LEVEL
    return LIQUID_MEMBERS[isin]


def _volume_of(isin: str, session: date) -> int:
    """A name's traded quantity: STAR is illiquid through the rebalance, everyone else is liquid.

    STAR turns liquid only *after* the rebalance (from the fill-headroom session on), so the
    liquidity screen — which measures a window ending on the decision date — still sees it illiquid
    on 2024-02-01. That is the point-in-time property ``test_liquidity_screen_is_pit_safe`` asserts.
    """
    if isin == STAR:
        return _LIQUID_VOLUME if session > REBALANCE else _STAR_VOLUME
    return _LIQUID_VOLUME


def _write_l1_partition(data_root: Path, trade_date: date, isins: list[str]) -> None:
    """Write one raw NSE ``prices_raw`` L1 partition for ``isins`` on ``trade_date``.

    OHLC are all the close; the traded quantity is the name's engineered volume and
    ``total_traded_value = close * volume`` is the turnover the liquidity median screens against.
    Uses the real ``PRICES_RAW_SCHEMA`` so the DuckDB read exercises the on-disk contract.
    """
    records = []
    for isin in isins:
        close = _close_of(isin, trade_date)
        volume = _volume_of(isin, trade_date)
        records.append(
            {
                "isin": isin,
                "exchange": "NSE",
                "symbol": isin[:6],
                "series": "EQ",
                "trade_date": trade_date,
                "open": close.quantize(_PRICE_Q),
                "high": close.quantize(_PRICE_Q),
                "low": close.quantize(_PRICE_Q),
                "close": close.quantize(_PRICE_Q),
                "last": close.quantize(_PRICE_Q),
                "prev_close": close.quantize(_PRICE_Q),
                "total_traded_qty": volume,
                "total_traded_value": (close * volume).quantize(_PRICE_Q),
                "total_trades": volume,
                "deliv_qty": None,
                "deliv_pct": None,
            }
        )
    path = l1_partition_path(PRICES_RAW_DATASET, trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=PRICES_RAW_SCHEMA)
    pq.write_table(table, path, compression="snappy", version="2.6")


def _write_snapshot(data_root: Path, as_of: date, isins: list[str]) -> None:
    """Write one immutable M3.9 index-constituents snapshot through the real writer."""
    rows = tuple(
        ConstituentRow(
            isin=isin,
            symbol=isin[:6],
            series="EQ",
            company_name=f"Company {isin[:6]}",
            industry="Test",
        )
        for isin in isins
    )
    snapshot = ConstituentSnapshot(
        index_slug=INDEX_SLUG,
        index_name=INDEX_NAME,
        as_of=as_of,
        rows=tuple(sorted(rows, key=lambda row: row.isin)),
        source="nifty_index_constituents",
    )
    write_constituents_l1(snapshot, data_root=data_root)


_ALL_NAMES: Final = [STAR, NONMEMBER, *LIQUID_MEMBERS]
#: The index members: STAR and the four liquid members — everyone except NONMEMBER.
_MEMBERS: Final = [STAR, *LIQUID_MEMBERS]


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """An L1 lake of six names plus an as-of index snapshot in force for the 2024-02-01 date."""
    for session in _SESSIONS:
        _write_l1_partition(tmp_path, session, _ALL_NAMES)
    # A snapshot captured before the rebalance, naming every member but NONMEMBER — the membership
    # in force on 2024-02-01.
    _write_snapshot(tmp_path, date(2024, 1, 1), _MEMBERS)
    # No corporate actions in this fixture, so every name's L2 adjusted series is its raw series
    # (identity factor chain) — the store the M9.2-state adjusted signal reads.
    for isin in _ALL_NAMES:
        materialize_isin(
            isin, chain=FactorChain(isin=isin, rows=()), actions=(), data_root=tmp_path
        )
    return tmp_path


def _top_n_isins(data: _L1MomentumData, as_of: date, n: int) -> list[str]:
    """The top-``n`` ISINs the policy would hold — highest momentum, ties broken by ISIN."""
    ranked = sorted(data.signal(as_of).records, key=lambda r: (-r.momentum, r.isin))
    return [r.isin for r in ranked[:n]]


def _universe_isins(data: _L1MomentumData, as_of: date) -> set[str]:
    """The ISINs in the rebalance candidate set (the investable universe) as of ``as_of``."""
    return {r.isin for r in data.signal(as_of).records}


# ── acceptance 1: universe = as-of membership ∩ liquidity floor, PIT-safe ─────────────────────────


def test_universe_is_membership_intersect_liquidity(lake: Path) -> None:
    """The constrained universe is exactly the liquid index members — the two screens intersected.

    NONMEMBER is liquid but outside the index (dropped by membership); STAR is an index member but
    illiquid (dropped by the floor); the four liquid members survive both. So the investable
    universe is ``{MEMB_A..D}`` — neither screen alone yields that set, only their intersection.
    """
    reader = _L1Reader(data_root=lake)
    try:
        full = _L1MomentumData(reader, _SESSIONS)
        constrained = _L1MomentumData(
            reader,
            _SESSIONS,
            universe_filter=_InvestableUniverse(reader, _universe_params(), data_root=lake),
        )
        full_universe = _universe_isins(full, REBALANCE)
        investable = _universe_isins(constrained, REBALANCE)
    finally:
        reader.close()

    # The unconstrained universe is every name that traded — including the illiquid and the
    # non-member; the constrained universe is only the liquid members.
    assert full_universe == set(_ALL_NAMES)
    assert investable == set(LIQUID_MEMBERS)
    assert NONMEMBER not in investable  # dropped by the membership screen (it is liquid)
    assert STAR not in investable  # dropped by the liquidity screen (it is a member)


def test_membership_screen_is_pit_safe(lake: Path) -> None:
    """A membership snapshot captured *after* the decision date never enters that decision.

    A later snapshot adds a new name; ``members_asof`` on the rebalance date must still return the
    earlier snapshot's membership, not the future one — no survivorship / look-ahead leak (inv. #7).
    """
    newco = "INE999A01010"
    _write_snapshot(lake, date(2024, 3, 1), [*_MEMBERS, newco])  # a future snapshot

    reader = _L1Reader(data_root=lake)
    try:
        screen = _InvestableUniverse(reader, _universe_params(), data_root=lake)
        members_now = screen.members_asof(REBALANCE)
        members_future = screen.members_asof(date(2024, 3, 1))
    finally:
        reader.close()

    assert members_now == frozenset(_MEMBERS)
    assert newco not in (members_now or frozenset())  # the future addition did not leak back
    assert members_future is not None and newco in members_future  # but is seen once in force


def test_liquidity_screen_is_pit_safe(lake: Path) -> None:
    """The turnover median reads only the window ending on the decision date — no future leak.

    STAR is illiquid through the rebalance and turns liquid only afterwards. The screen as of the
    rebalance must exclude it, and the median it computes over the decision-ending window must be
    below the floor — even though STAR's *later* session (2024-02-02) prints turnover far above it.
    Reading that future session would flip the verdict; the screen must not, so post-decision
    liquidity cannot make a name investable in the past (invariant #7).
    """
    reader = _L1Reader(data_root=lake)
    try:
        screen = _InvestableUniverse(reader, _universe_params(), data_root=lake)
        liquid_now = screen.liquid_asof(REBALANCE)
        # The median STAR turnover the screen actually reads at the decision, and — for contrast —
        # STAR's turnover on its post-decision session, which the decision window must not see.
        at_decision = reader.median_turnover_over(REBALANCE - timedelta(days=365), REBALANCE)
        after_decision = reader.median_turnover_over(date(2024, 2, 2), date(2024, 2, 2))
    finally:
        reader.close()

    assert STAR not in liquid_now  # illiquid as of the decision date
    assert set(LIQUID_MEMBERS) <= liquid_now  # the liquid members clear the floor throughout
    assert at_decision[STAR] < _FLOOR  # the decision-window median is below the floor
    assert after_decision[STAR] >= _FLOOR  # yet STAR is liquid *after* the decision — not counted


# ── acceptance 2: an illiquid top-ranker is excluded on a date it would otherwise rank in ─────────


def test_illiquid_top_ranker_is_excluded(lake: Path) -> None:
    """STAR ranks into the top-2 on the full universe and is excluded by the liquidity floor.

    This is the behavioural consequence of the filter: on the raw full universe STAR's +200 %
    momentum ranks it first, so it *would* be bought; the investable universe drops it for failing
    the median-turnover floor, and the top-2 becomes the two strongest liquid members instead.
    """
    reader = _L1Reader(data_root=lake)
    try:
        full = _L1MomentumData(reader, _SESSIONS)
        constrained = _L1MomentumData(
            reader,
            _SESSIONS,
            universe_filter=_InvestableUniverse(reader, _universe_params(), data_root=lake),
        )
        full_top2 = _top_n_isins(full, REBALANCE, 2)
        constrained_top2 = _top_n_isins(constrained, REBALANCE, 2)
    finally:
        reader.close()

    assert STAR in full_top2  # it would otherwise rank into the top-N
    assert STAR not in constrained_top2  # excluded by the floor
    assert constrained_top2 == [MEMB_A, MEMB_B]  # the two strongest liquid members take its place


# ── acceptance 3: the run completes and the report states universe-size + deltas vs M9.2 ──────────


def test_constrained_run_completes_and_report_states_deltas(lake: Path) -> None:
    """Both runs complete with no PIT violation and the report states the universe-size and deltas.

    A ``PitError`` anywhere in the walk raises out of ``run_naive_momentum``; the runs returning is
    the no-look-ahead evidence (invariant #7). The constrained run holds fewer names (the filter
    shrank the universe), so it differs from the baseline and the report has real deltas to state.
    """
    params = MomentumParameters(top_n=2)
    baseline = run_naive_momentum(
        start=_SESSIONS[0],
        end=_SESSIONS[-1],
        parameters=params,
        data_root=lake,
        adjusted=True,
        universe=None,
    )
    constrained = run_naive_momentum(
        start=_SESSIONS[0],
        end=_SESSIONS[-1],
        parameters=params,
        data_root=lake,
        adjusted=True,
        universe=_universe_params(),
    )

    assert baseline.sessions == constrained.sessions > 0
    assert baseline.universe_filtered is False and constrained.universe_filtered is True
    # The filter shrank the universe: the constrained run ranks strictly fewer names per rebalance.
    assert constrained.mean_universe < baseline.mean_universe
    # The de-corruption changed the portfolio (STAR/NONMEMBER dropped), so the runs differ.
    assert baseline.result.digest() != constrained.result.digest()

    report = render_universe_report(
        baseline, constrained, universe=_universe_params(), membership_present=True
    )
    assert "M9.3" in report
    assert "universe size" in report.lower()
    assert "Portfolio XIRR" in report
    assert "Turnover" in report
    assert "Total costs" in report
    assert str(constrained.mean_universe) in report
    assert constrained.result.digest() in report


def test_run_universe_report_end_to_end(lake: Path) -> None:
    """``run_universe_report`` runs both backtests and renders a report that names the thresholds.

    It also probes ``membership_asof`` and reports honestly that this store *does* carry a snapshot,
    so the full intersection applied (the ``membership_present`` branch of the report).
    """
    report = run_universe_report(
        start=_SESSIONS[0],
        end=_SESSIONS[-1],
        parameters=MomentumParameters(top_n=2),
        universe=_universe_params(),
        data_root=lake,
    )
    assert "# M9.3" in report
    assert INDEX_SLUG in report
    assert "full intersection" in report  # membership was present in this store
