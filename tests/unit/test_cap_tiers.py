"""Cap tiers (X2, 2026-10-05): the liquidity-rank tier proxy, its PIT read, and the tiered book.

``backtest.cap_tiers`` ranks every NSE EQ equity on its trailing 126-session median close x
quantity and cuts AMFI-style tiers at ranks 100 / 250 / 500; ``SwingCompositePolicy`` with a tier
source and sleeves buys each tier's best names by within-tier composite rank. Each test below is
written to fail if the logic it guards is inverted (boundaries shifted, the window let run past the
decision session, the within-tier rank replaced by the global one, a tier dropped from the book).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.cap_tiers import (
    CAP_TIER_IDENTITY,
    SIZE_LOOKBACK_SESSIONS,
    CapTier,
    LiquidityRankTiers,
    TierMembership,
    TierSleeve,
    assign_tiers,
    describe_sleeves,
    is_ranked_equity,
    tier_for_rank,
)
from backtest.policies.swing_composite import (
    RegimeReading,
    SwingCompositeParameters,
    SwingCompositePolicy,
    SwingRecord,
)
from backtest.replay import SessionContext, SessionDecision
from backtest.run import UniverseParameters
from backtest.sweep import (
    ARMS,
    CAP_TIER_ARMS,
    FOCUSED_MIDCAP,
    FOCUSED_SMALLCAP,
    HIGH_FLOOR,
    LOW_FLOOR,
    MULTI_CAP,
    _arm_spec,
    run_digests,
)
from dataplatform.clock import FrozenClock
from dataplatform.ingest.models import is_isin_check_digit_valid
from dataplatform.query.pit import Dataset, PitContext, PitError
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA
from execution.broker import Exchange, Holding, Margins, Position, Side
from tests.index_history_support import digests_without_index_membership

_PRICE_Q: Final = Decimal("0.0001")
_DIGESTS: Final = Path("tests/fixtures/cap_tiers/arm_digests_43bbb57.json")


def _isin(n: int, prefix: str = "INE") -> str:
    """A synthetic ISIN with a valid ISO 6166 check digit."""
    body = f"{prefix}{n:08d}"
    for digit in "0123456789":
        if is_isin_check_digit_valid(body + digit):
            return body + digit
    raise AssertionError(body)


# ── boundaries ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rank", "tier"),
    [
        (0, None),
        (1, CapTier.LARGE),
        (100, CapTier.LARGE),
        (101, CapTier.MID),
        (250, CapTier.MID),
        (251, CapTier.SMALL),
        (500, CapTier.SMALL),
        (501, None),
    ],
)
def test_tier_boundaries_are_pinned(rank: int, tier: CapTier | None) -> None:
    assert tier_for_rank(rank) is tier


def test_assign_tiers_ranks_largest_first_and_drops_past_rank_500() -> None:
    """Size n+1-i for name i: name 1 is the largest. Inverting the sort puts it past rank 500."""
    sizes = {_isin(i): Decimal(1000 - i) for i in range(1, 601)}
    by_isin = {m.isin: m for m in assign_tiers(sizes, as_of=date(2020, 1, 1))}
    assert len(by_isin) == 500
    assert by_isin[_isin(1)].rank == 1
    assert (by_isin[_isin(100)].tier, by_isin[_isin(101)].tier) == (CapTier.LARGE, CapTier.MID)
    assert (by_isin[_isin(250)].tier, by_isin[_isin(251)].tier) == (CapTier.MID, CapTier.SMALL)
    assert by_isin[_isin(500)].tier is CapTier.SMALL
    assert _isin(501) not in by_isin


def test_assign_tiers_breaks_a_size_tie_by_isin() -> None:
    sizes = {_isin(2): Decimal(5), _isin(1): Decimal(5)}
    ranked = assign_tiers(sizes, as_of=date(2020, 1, 1))
    assert [m.isin for m in ranked] == [_isin(1), _isin(2)]


def test_only_listed_company_equities_are_ranked() -> None:
    assert is_ranked_equity(_isin(7))
    assert not is_ranked_equity(_isin(7, prefix="INF"))  # an ETF / MF unit
    bad = _isin(7)[:-1] + str((int(_isin(7)[-1]) + 1) % 10)
    assert not is_ranked_equity(bad)


# ── the PIT read off a lake ──────────────────────────────────────────────────────────────────────


def _write(root: Path, session: date, rows: list[tuple[str, str, Decimal, int]]) -> None:
    """One L1 prices_raw partition: ``(isin, exchange, close, quantity)`` rows, series EQ."""
    records = []
    for isin, exchange, close, qty in rows:
        price = close.quantize(_PRICE_Q)
        records.append(
            {
                "isin": isin,
                "exchange": exchange,
                "symbol": isin[:6],
                "series": "EQ",
                "trade_date": session,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "last": price,
                "prev_close": price,
                "total_traded_qty": qty,
                "total_traded_value": (close * qty).quantize(_PRICE_Q),
                "total_trades": qty,
                "deliv_qty": None,
                "deliv_pct": None,
            }
        )
    path = l1_partition_path(PRICES_RAW_DATASET, session, data_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(records, schema=PRICES_RAW_SCHEMA), path)


_A, _B, _C = _isin(11), _isin(12), _isin(13)
_ETF = _isin(14, prefix="INF")


def _calendar(n: int) -> list[date]:
    start = date(2020, 1, 1)
    return [start + timedelta(days=i) for i in range(n)]


def _lake(root: Path, calendar: list[date]) -> None:
    """A trades 100/day, B 10/day, C 1/day; on session 0 only, B prints a huge 10,000.

    The ETF trades most of all, and a BSE row for C trades a fortune — neither may be ranked.
    """
    for i, session in enumerate(calendar):
        _write(
            root,
            session,
            [
                (_A, "NSE", Decimal(100), 1),
                (_B, "NSE", Decimal(10_000 if i == 0 else 10), 1),
                (_C, "NSE", Decimal(1), 1),
                (_ETF, "NSE", Decimal(1_000_000), 1),
                (_C, "BSE", Decimal(1_000_000), 1),
            ],
        )


def test_the_window_is_126_sessions_ending_on_the_decision_session(tmp_path: Path) -> None:
    """Two names whose median flips on whether session 0 is inside the window.

    ``quantile_disc(0.5)`` takes the lower middle. Y prints 1,000 on sessions 0 and 125 and 1 on
    124: decided on 125, a 126-session window holds all three (median 1,000) and anything shorter
    drops session 0 (median 1). W prints 1,000 on 0 and 126 and 1 on 125: decided on 126, the
    126-session window starts at 1 (median 1) and anything longer reaches session 0 (median 1,000).
    """
    calendar = _calendar(SIZE_LOOKBACK_SESSIONS + 1)
    y, w = _isin(21), _isin(22)
    prints: dict[int, list[tuple[str, str, Decimal, int]]] = {
        0: [(y, "NSE", Decimal(1000), 1), (w, "NSE", Decimal(1000), 1)],
        124: [(y, "NSE", Decimal(1), 1)],
        125: [(y, "NSE", Decimal(1000), 1), (w, "NSE", Decimal(1), 1)],
        126: [(w, "NSE", Decimal(1000), 1)],
    }
    for i, session in enumerate(calendar):
        _write(tmp_path, session, prints.get(i, [(_A, "NSE", Decimal(5), 1)]))
    tiers = LiquidityRankTiers(calendar, data_root=tmp_path)
    try:
        sizes = tiers.size_measure([calendar[125], calendar[126]])
    finally:
        tiers.close()
    assert sizes[calendar[125]][y] == Decimal(1000)
    assert sizes[calendar[126]][w] == Decimal(1)


def test_a_future_bar_never_changes_a_past_tier(tmp_path: Path) -> None:
    calendar = _calendar(21)
    _lake(tmp_path, calendar[:10])
    past = calendar[9]
    before = LiquidityRankTiers(calendar[:10], data_root=tmp_path)
    before.load([past])
    then = before.tiers(past).records
    before.close()
    # Eleven more sessions in which C trades a fortune — the future overturns the ranking.
    for session in calendar[10:]:
        _write(tmp_path, session, [(_A, "NSE", Decimal(1), 1), (_C, "NSE", Decimal(10**9), 1)])
    after = LiquidityRankTiers(calendar, data_root=tmp_path)
    after.load([past, calendar[-1]])
    assert after.tiers(past).records == then
    assert [m.isin for m in then] == [_A, _B, _C]
    # Non-vacuous: the same source, asked about the future session, does see C first.
    assert after.tiers(calendar[-1]).records[0].isin == _C
    after.close()


def test_etfs_and_other_venues_are_never_ranked(tmp_path: Path) -> None:
    calendar = _calendar(5)
    _lake(tmp_path, calendar)
    tiers = LiquidityRankTiers(calendar, data_root=tmp_path)
    tiers.load([calendar[-1]])
    ranked = tiers.tiers(calendar[-1]).records
    tiers.close()
    assert [m.isin for m in ranked] == [_A, _B, _C]
    assert ranked[2].size == Decimal(1)  # C's BSE fortune never reached its NSE median


def test_tier_records_are_dated_by_their_session_and_guarded(tmp_path: Path) -> None:
    calendar = _calendar(5)
    _lake(tmp_path, calendar)
    tiers = LiquidityRankTiers(calendar, data_root=tmp_path)
    tiers.load([calendar[-1]])
    dataset = tiers.tiers(calendar[-1])
    with pytest.raises(PitError):
        PitContext(as_of=calendar[-2]).admit(dataset)
    assert {m.knowable_date for m in PitContext(as_of=calendar[-1]).admit(dataset)} == {
        calendar[-1]
    }
    with pytest.raises(KeyError):
        tiers.tiers(calendar[0])  # never loaded: no lazily-struck tier
    tiers.close()


# ── the tiered book ──────────────────────────────────────────────────────────────────────────────

_S: Final = date(2020, 6, 1)


def _rec(isin: str, score: int, *, vol: str = "0.02") -> SwingRecord:
    """A candidate whose every leg is ``score``: a higher score ranks higher on the composite."""
    level = Decimal(score) / Decimal(1000)
    return SwingRecord(
        isin=isin,
        high_proximity=Decimal("0.5") + level,
        delivery_share=level,
        momentum_12_1=level,
        volatility=Decimal(vol),
        price=Decimal(100),
        knowable_date=_S,
    )


class _Data:
    def __init__(self, records: tuple[SwingRecord, ...]) -> None:
        self._records = records
        self.rebalance = True

    def is_rebalance(self, session: date) -> bool:
        return self.rebalance

    def signal(self, as_of: date) -> Dataset[SwingRecord]:
        return Dataset.declaring("swing", self._records, knowable_date=lambda r: r.knowable_date)

    def marks(self, as_of: date) -> Dataset[SwingRecord]:
        return Dataset.declaring("marks", self._records, knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        raise AssertionError("no regime gate on these arms")


class _Tiers:
    def __init__(self, tiers: dict[str, CapTier], *, knowable: date = _S) -> None:
        self._tiers = tiers
        self._knowable = knowable

    def tiers(self, as_of: date) -> Dataset[TierMembership]:
        rows = tuple(
            TierMembership(isin, tier, i, Decimal(1), self._knowable)
            for i, (isin, tier) in enumerate(sorted(self._tiers.items()), start=1)
        )
        return Dataset.declaring("tiers", rows, knowable_date=lambda r: r.knowable_date)


class _Broker:
    def __init__(self, holdings: tuple[Holding, ...] = ()) -> None:
        self._holdings = holdings

    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def positions(self) -> tuple[Position, ...]:
        return ()

    def margins(self) -> Margins:
        return Margins(available=Decimal(10_000_000), utilised=Decimal(0))


def _decide(policy: SwingCompositePolicy, broker: _Broker) -> SessionDecision:
    ctx = SessionContext(
        session=_S,
        pit=PitContext(as_of=_S),
        broker=broker,  # type: ignore[arg-type]
        clock=FrozenClock(_S),
    )
    return policy.decide(ctx)


# Large names score highest, then mid, then small — a global top-n is all large.
_L = [_isin(100 + i) for i in range(6)]
_M = [_isin(200 + i) for i in range(6)]
_SM = [_isin(300 + i) for i in range(6)]
_RECORDS = tuple(
    [_rec(x, 900 - i) for i, x in enumerate(_L)]
    + [_rec(x, 600 - i) for i, x in enumerate(_M)]
    + [_rec(x, 300 - i) for i, x in enumerate(_SM)]
)
_TIER_OF = (
    dict.fromkeys(_L, CapTier.LARGE)
    | dict.fromkeys(_M, CapTier.MID)
    | dict.fromkeys(_SM, CapTier.SMALL)
)
_NO_SCREEN = {"exclude_vol_fraction": Decimal(0)}


def _multi(n: int = 2) -> SwingCompositePolicy:
    return SwingCompositePolicy(
        _Data(_RECORDS),
        SwingCompositeParameters(top_n=3 * n, sell_band=9 * n, **_NO_SCREEN),  # type: ignore[arg-type]
        tiers=_Tiers(_TIER_OF),
        sleeves=[TierSleeve(t, n, 3 * n) for t in (CapTier.LARGE, CapTier.MID, CapTier.SMALL)],
    )


def _focused(
    tier: CapTier, n: int = 2, band: int = 3, data: _Data | None = None
) -> SwingCompositePolicy:
    return SwingCompositePolicy(
        data or _Data(_RECORDS),
        SwingCompositeParameters(top_n=n, sell_band=max(band, n), **_NO_SCREEN),  # type: ignore[arg-type]
        tiers=_Tiers(_TIER_OF),
        sleeves=[TierSleeve(tier, n, band)],
    )


def _bought(decision: SessionDecision) -> list[str]:
    return sorted(o.isin for o in decision.orders if o.side is Side.BUY)


def test_multi_cap_holds_all_three_tiers_in_equal_counts() -> None:
    bought = _bought(_decide(_multi(), _Broker()))
    assert bought == sorted([*_L[:2], *_M[:2], *_SM[:2]])


def test_a_focused_book_buys_its_tiers_best_not_the_global_best() -> None:
    assert _bought(_decide(_focused(CapTier.MID), _Broker())) == sorted(_M[:2])
    assert _bought(_decide(_focused(CapTier.SMALL), _Broker())) == sorted(_SM[:2])


def test_a_short_tier_leaves_its_slots_empty_rather_than_lending_them() -> None:
    tiers = {**_TIER_OF, **dict.fromkeys(_SM[1:], CapTier.MID)}  # one small name left
    policy = SwingCompositePolicy(
        _Data(_RECORDS),
        SwingCompositeParameters(top_n=6, sell_band=18, **_NO_SCREEN),  # type: ignore[arg-type]
        tiers=_Tiers(tiers),
        sleeves=[TierSleeve(t, 2, 6) for t in (CapTier.LARGE, CapTier.MID, CapTier.SMALL)],
    )
    assert _bought(_decide(policy, _Broker())) == sorted([*_L[:2], *_M[:2], _SM[0]])


def _held(isin: str) -> Holding:
    return Holding(isin=isin, exchange=Exchange.NSE, quantity=10, average_price=Decimal(100))


def _sold_after_aging(tier: CapTier, held: str, *, n: int = 2, band: int = 3) -> set[str]:
    """Age a holding past min_hold on non-rebalance sessions, then decide a rebalance."""
    data = _Data(_RECORDS)
    policy = _focused(tier, n, band, data)
    broker = _Broker((_held(held),))
    data.rebalance = False
    for _ in range(SwingCompositeParameters().min_hold_sessions):
        _decide(policy, broker)
    data.rebalance = True
    return {o.isin for o in _decide(policy, broker).orders if o.side is Side.SELL}


def test_a_holding_is_judged_on_its_within_tier_rank() -> None:
    """Mid #3 is globally rank 9 — past a band of 3 globally, inside it within the tier."""
    assert _M[2] not in _sold_after_aging(CapTier.MID, _M[2])
    assert _M[3] in _sold_after_aging(CapTier.MID, _M[3])


def test_a_holding_whose_tier_this_book_does_not_hold_is_sold() -> None:
    assert _L[0] in _sold_after_aging(CapTier.MID, _L[0])


def test_a_tier_read_from_the_future_raises() -> None:
    policy = SwingCompositePolicy(
        _Data(_RECORDS),
        SwingCompositeParameters(top_n=2, **_NO_SCREEN),  # type: ignore[arg-type]
        tiers=_Tiers(_TIER_OF, knowable=_S + timedelta(days=1)),
        sleeves=[TierSleeve(CapTier.MID, 2, 6)],
    )
    with pytest.raises(PitError):
        _decide(policy, _Broker())


def test_sleeves_must_agree_with_top_n_and_come_with_a_source() -> None:
    with pytest.raises(ValueError, match="must agree"):
        SwingCompositePolicy(
            _Data(_RECORDS),
            SwingCompositeParameters(top_n=20),
            tiers=_Tiers(_TIER_OF),
            sleeves=[TierSleeve(CapTier.MID, 8, 24)],
        )
    with pytest.raises(ValueError, match="both"):
        SwingCompositePolicy(_Data(_RECORDS), sleeves=[TierSleeve(CapTier.MID, 20, 60)])


# ── the arms and their run identities ────────────────────────────────────────────────────────────


def test_the_arms_are_fixed_as_specified() -> None:
    arms = {arm.label: arm for arm in CAP_TIER_ARMS}
    multi = arms[MULTI_CAP]
    assert multi.cap_tiers == (
        TierSleeve(CapTier.LARGE, 8, 24),
        TierSleeve(CapTier.MID, 8, 24),
        TierSleeve(CapTier.SMALL, 8, 24),
    )
    assert multi.swing == SwingCompositeParameters(top_n=24, sell_band=72)
    assert arms[FOCUSED_MIDCAP].cap_tiers == (TierSleeve(CapTier.MID, 20, 60),)
    assert arms[FOCUSED_SMALLCAP].cap_tiers == (TierSleeve(CapTier.SMALL, 20, 60),)
    for label in (FOCUSED_MIDCAP, FOCUSED_SMALLCAP):
        assert arms[label].swing == SwingCompositeParameters()
    assert not set(arms) & {arm.label for arm in ARMS}


def test_each_cap_tier_arm_has_its_own_run_id() -> None:
    kwargs = {"start": date(2012, 7, 4), "end": date(2026, 8, 31), "floors": (HIGH_FLOOR,)}
    tiered = run_digests(arms=CAP_TIER_ARMS, **kwargs)  # type: ignore[arg-type]
    existing = run_digests(arms=ARMS, **kwargs)  # type: ignore[arg-type]
    assert len(set(tiered.values())) == len(CAP_TIER_ARMS)
    assert not set(tiered.values()) & set(existing.values())
    spec = _arm_spec(
        CAP_TIER_ARMS[1],
        start=date(2012, 7, 4),
        end=date(2026, 8, 31),
        universe=UniverseParameters(median_turnover_floor=HIGH_FLOOR),
        opening_cash=Decimal(1_000_000),
        adjusted=True,
    )
    assert spec["cap_tiers"] == f"{CAP_TIER_IDENTITY}[mid=20/60]"
    assert describe_sleeves(CAP_TIER_ARMS[0].cap_tiers or ()).endswith(
        "[large=8/24,mid=8/24,small=8/24]"
    )


def test_every_existing_arm_keeps_its_run_id_byte_for_byte() -> None:
    """The digests main (43bbb57) gave every arm in ``ARMS`` at both floors, pinned.

    Only the point-in-time index membership key moves them, and it moves every one of them: a
    ledger persisted under the snapshot-era screen is never resumed as today's reading.
    """
    pinned = json.loads(_DIGESTS.read_text())
    window = {"start": date(2012, 7, 4), "end": date(2026, 8, 31)}
    floors = (LOW_FLOOR, HIGH_FLOOR)
    before = digests_without_index_membership(**window, arms=ARMS, floors=floors)
    now = run_digests(**window, arms=ARMS, floors=floors)
    assert {f"{label}|{floor}": digest for (label, floor), digest in before.items()} == pinned
    assert not set(now.values()) & set(pinned.values())
