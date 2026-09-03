"""M10.3 — sector-rotation policy, unit-tested offline (EXECUTION_PLAN §7, X2).

The policy's one claim over plain momentum is the *sector gate*: rank sectors by their members'
aggregate momentum, keep the top-K, and hold the top-N names only from those sectors. Every test
here pins one edge of that gate with an **inversion** — a test that fails if the logic is reversed:

* the sector ranking scores a sector by the *mean* of its members' momentum, best first;
* the top-K cut excludes a high-momentum name whose *sector* is weak (the isolation vs plain
  momentum — the single strongest name is dropped when its industry is not in favour);
* widening K lets that name back in;
* the top-N cut keeps only the strongest names across the chosen sectors.

Also pinned — the acceptance criteria that matter most:

* **acceptance #1 (PIT membership).** Sector membership is read point-in-time: a candidate whose
  sector was resolved from a snapshot *not yet knowable* on the session (a future / "today's" map
  applied to a past date) trips the point-in-time guard rather than reaching a decision. Proved both
  with a raw future-dated record and with a source that models ``membership_asof`` semantics (an
  in-force snapshot admits; a future snapshot raises).

No store, no network, no wall clock (B8, invariant #11) — the policy stands alone against an
in-memory source.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backtest.policies.sector_rotation import (
    SectorRotationParameters,
    SectorRotationPolicy,
    SectorRotationRecord,
    rank_sectors,
)
from backtest.replay import SessionContext, SessionDecision
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitContext, PitError
from execution.broker import Exchange, Holding, Margins, Side

SESSION = date(2020, 6, 1)

# Real-shaped ISINs (invariant #2), distinct so ties break deterministically.
S1 = "INE001A01036"  # SPIKE sector — the single strongest name
S2 = "INE002A01018"  # SPIKE sector — a laggard that drags the sector mean down
T1 = "INE009A01021"  # STEADY sector — the strongest of a uniformly-strong sector
T2 = "INE040A01034"  # STEADY sector
T3 = "INE467B01029"  # STEADY sector

SPIKE = "Speculative Spike"
STEADY = "Steady Compounders"


def _rec(
    isin: str,
    *,
    mom: str,
    sector: str,
    price: str = "100",
    knowable: date = SESSION,
) -> SectorRotationRecord:
    return SectorRotationRecord(
        isin=isin,
        momentum=Decimal(mom),
        price=Decimal(price),
        sector=sector,
        knowable_date=knowable,
    )


# SPIKE holds the single strongest name (S1, +0.90) but a weak mean (0.25, dragged by S2 -0.40).
# STEADY is uniformly strong (mean 0.35), so it — not SPIKE — is the top sector, even though it
# holds no single name as strong as S1. This is the whole point of ranking sectors, not names.
_RECORDS = (
    _rec(S1, mom="0.90", sector=SPIKE),
    _rec(S2, mom="-0.40", sector=SPIKE),
    _rec(T1, mom="0.40", sector=STEADY),
    _rec(T2, mom="0.35", sector=STEADY),
    _rec(T3, mom="0.30", sector=STEADY),
)


class _Data:
    """An in-memory ``SectorRotationData``: fixed candidates and a rebalance flag."""

    def __init__(
        self, records: tuple[SectorRotationRecord, ...], *, rebalance: bool = True
    ) -> None:
        self._records = records
        self._rebalance = rebalance

    def is_rebalance(self, session: date) -> bool:
        return self._rebalance

    def signal(self, as_of: date) -> Dataset[SectorRotationRecord]:
        return Dataset.declaring(
            f"sector_rotation@{as_of.isoformat()}",
            self._records,
            knowable_date=lambda record: record.knowable_date,
        )


class _MembershipData(_Data):
    """A source that models ``membership_asof``: it stamps each record's ``knowable_date`` with the
    capture date of the sector snapshot it resolved the sector from.

    ``snapshot_as_of`` is that capture date. When it is on or before the session it is the in-force
    snapshot (the correct point-in-time read) and admits; when it is after the session it is a
    future map — the survivorship trap of applying today's classification to a past date — the guard
    refuses it. The policy never sees the difference except through the guard, which is the point.
    """

    def __init__(self, records: tuple[SectorRotationRecord, ...], *, snapshot_as_of: date) -> None:
        stamped = tuple(
            SectorRotationRecord(
                isin=r.isin,
                momentum=r.momentum,
                price=r.price,
                sector=r.sector,
                knowable_date=snapshot_as_of,
            )
            for r in records
        )
        super().__init__(stamped)


class _FakeBroker:
    """A minimal ``Broker`` read surface: fixed holdings and free cash. Records nothing."""

    def __init__(self, *, cash: Decimal, holdings: tuple[Holding, ...] = ()) -> None:
        self._cash = cash
        self._holdings = holdings

    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def margins(self) -> Margins:
        return Margins(available=self._cash, utilised=Decimal("0"))


def _ctx(session: date, broker: _FakeBroker) -> SessionContext:
    return SessionContext(
        session=session,
        pit=PitContext(as_of=session),
        broker=broker,  # type: ignore[arg-type]  # the fake satisfies the read surface used
        clock=FrozenClock(session),
    )


def _holding(isin: str, quantity: int, price: str = "100") -> Holding:
    return Holding(
        isin=isin, exchange=Exchange.NSE, quantity=quantity, average_price=Decimal(price)
    )


def _bought(decision: SessionDecision) -> set[str]:
    return {order.isin for order in decision.orders if order.side is Side.BUY}


def _sold(decision: SessionDecision) -> set[str]:
    return {order.isin for order in decision.orders if order.side is Side.SELL}


# ── sector scoring: mean of members, best first ───────────────────────────────────────────────────


def test_rank_sectors_scores_by_mean_not_sum_and_is_ordered() -> None:
    """STEADY (mean 0.35) outranks SPIKE (mean 0.25) though SPIKE holds the single strongest name.

    If the aggregate were the *sum* instead of the mean, or if a single strong member decided the
    sector, SPIKE (0.90 + -0.40 = 0.50 sum; 0.90 max) would win — so this ordering is the inversion
    tripwire for "rank sectors by their members' *aggregate* (mean) momentum".
    """
    scores = rank_sectors(_RECORDS)
    assert [s.sector for s in scores] == [STEADY, SPIKE]  # mean order, not sum/max
    assert scores[0].mean_momentum == Decimal("0.35")  # (0.40+0.35+0.30)/3
    assert scores[1].mean_momentum == Decimal("0.25")  # (0.90-0.40)/2
    assert scores[0].member_count == 3
    assert scores[1].member_count == 2


def test_rank_sectors_breaks_ties_by_sector_name() -> None:
    """Two sectors with the identical mean are ordered by sector name — deterministic for replay."""
    records = (
        _rec("INE111A01011", mom="0.20", sector="Zeta"),
        _rec("INE222A01012", mom="0.20", sector="Alpha"),
    )
    assert [s.sector for s in rank_sectors(records)] == ["Alpha", "Zeta"]


# ── the sector gate: top-K sectors, then top-N names within (isolation vs plain momentum) ─────────


def test_top_k_gate_drops_the_strongest_name_when_its_sector_is_weak_inversion() -> None:
    """With top_k=1 the basket is STEADY's names — the +0.90 name S1 is excluded (weak sector).

    Plain momentum on the *same* candidates would hold S1 first (it has the strongest single
    momentum). The sector gate holds STEADY names instead, because SPIKE is not a top sector. This
    is the sector effect in isolation: reverse the gate (pick the strong *name* regardless of
    sector) and S1 comes back — so its absence is the inversion.
    """
    policy = SectorRotationPolicy(_Data(_RECORDS), SectorRotationParameters(top_k=1, top_n=2))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))
    bought = _bought(decision)
    assert S1 not in bought  # the single strongest name, gated out by its weak sector
    assert S2 not in bought
    assert bought <= {T1, T2, T3}
    assert bought  # the top STEADY names are held


def test_widening_top_k_lets_the_strong_name_back_in() -> None:
    """With top_k=2 both sectors are in favour, so the strongest name S1 becomes eligible again."""
    policy = SectorRotationPolicy(_Data(_RECORDS), SectorRotationParameters(top_k=2, top_n=2))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))
    # top-2 names by momentum across both chosen sectors: S1 (0.90) and T1 (0.40).
    assert _bought(decision) == {S1, T1}


def test_top_n_keeps_only_the_strongest_names_across_the_chosen_sectors() -> None:
    """top_k=2, top_n=3: the three strongest across both sectors — S1, T1, T2 — not T3 or S2."""
    policy = SectorRotationPolicy(_Data(_RECORDS), SectorRotationParameters(top_k=2, top_n=3))
    bought = _bought(policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000")))))
    assert bought == {S1, T1, T2}
    assert T3 not in bought and S2 not in bought


def test_holdings_outside_the_basket_are_sold() -> None:
    """A held name that is neither in a top sector nor in the top-N is liquidated in full."""
    held = (_holding(S1, 10), _holding(S2, 10))  # both SPIKE; with top_k=1 the sector is gated out
    policy = SectorRotationPolicy(_Data(_RECORDS), SectorRotationParameters(top_k=1, top_n=2))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"), holdings=held)))
    assert _sold(decision) == {S1, S2}  # the whole out-of-favour sector is exited


# ── point-in-time membership (invariant #7, acceptance #1) ────────────────────────────────────────


def test_a_future_dated_sector_membership_trips_the_guard() -> None:
    """A candidate whose sector became knowable *after* the session is refused (invariant #7).

    This is the structural defence against applying a future sector map to a past decision — the
    look-ahead the sector gate is most prone to. The policy admits through ``ctx.pit``; a
    future-dated record raises before it can rank a single sector.
    """
    future = date(SESSION.year + 1, SESSION.month, SESSION.day)
    leaked = tuple(
        SectorRotationRecord(
            isin=r.isin,
            momentum=r.momentum,
            price=r.price,
            sector=r.sector,
            knowable_date=future,
        )
        for r in _RECORDS
    )
    policy = SectorRotationPolicy(_Data(leaked), SectorRotationParameters(top_k=1, top_n=2))
    with pytest.raises(PitError):
        policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))


def test_membership_asof_in_force_snapshot_admits_but_a_future_snapshot_raises() -> None:
    """``membership_asof`` semantics: the in-force snapshot admits; a future map does not.

    The in-force snapshot's capture date is on or before the session, so its records admit and the
    policy trades. A snapshot captured *after* the session — a static current-day map applied
    backward, the survivorship trap M10.2 exists to close — carries a future ``knowable_date`` and
    the guard refuses it. Same candidates, same policy; only the snapshot date differs, so the raise
    is exactly "no future membership enters a past decision" (acceptance #1).
    """
    in_force = _MembershipData(_RECORDS, snapshot_as_of=date(2020, 5, 1))  # <= SESSION
    future_map = _MembershipData(_RECORDS, snapshot_as_of=date(2026, 9, 1))  # > SESSION

    policy = SectorRotationPolicy(in_force, SectorRotationParameters(top_k=1, top_n=2))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))
    assert _bought(decision), "the in-force snapshot is knowable and admits"

    leaking = SectorRotationPolicy(future_map, SectorRotationParameters(top_k=1, top_n=2))
    with pytest.raises(PitError):
        leaking.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))


# ── whole shares, heartbeat, determinism, validation ──────────────────────────────────────────────


def test_every_order_is_a_whole_number_of_shares() -> None:
    """The allocator produces whole-share orders; nothing fractional reaches the broker."""
    policy = SectorRotationPolicy(_Data(_RECORDS), SectorRotationParameters(top_k=2, top_n=3))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))
    for order in decision.orders:
        assert isinstance(order.quantity, int)
        assert order.quantity > 0


def test_no_orders_off_a_rebalance_session() -> None:
    """A non-rebalance session returns evidence and no orders — the engine writes the heartbeat."""
    policy = SectorRotationPolicy(_Data(_RECORDS, rebalance=False))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))
    assert decision.orders == ()
    assert decision.entries == ()
    assert decision.evidence.items


def test_same_inputs_produce_the_same_decision() -> None:
    """Determinism: two runs with the same inputs return identical orders and entries."""
    params = SectorRotationParameters(top_k=2, top_n=3)
    data = _Data(_RECORDS)
    first = SectorRotationPolicy(data, params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("750000")))
    )
    second = SectorRotationPolicy(data, params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("750000")))
    )
    assert [(o.isin, o.side, o.quantity) for o in first.orders] == [
        (o.isin, o.side, o.quantity) for o in second.orders
    ]
    assert [(e.isin, e.decision) for e in first.entries] == [
        (e.isin, e.decision) for e in second.entries
    ]


def test_empty_candidate_set_sells_everything_and_buys_nothing() -> None:
    """No candidates (no membership in force) → hold nothing: sell the book, buy nothing."""
    held = (_holding(T1, 10),)
    policy = SectorRotationPolicy(_Data(()), SectorRotationParameters(top_k=1, top_n=2))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"), holdings=held)))
    assert _bought(decision) == set()
    assert _sold(decision) == {T1}


def test_a_blank_sector_is_refused() -> None:
    """A candidate with no resolvable industry cannot be a rotation candidate — fails loud."""
    with pytest.raises(ValueError, match="sector must be a non-empty"):
        _rec(S1, mom="0.5", sector="   ")


def test_non_positive_price_is_refused() -> None:
    """An unpriceable name cannot be sized into whole shares — refused, not silently dropped."""
    with pytest.raises(ValueError, match="price must be positive"):
        _rec(S1, mom="0.5", sector=STEADY, price="0")


def test_float_signal_is_refused() -> None:
    """Money and signal are Decimal, never float (CLAUDE.md)."""
    with pytest.raises(TypeError, match="must be a Decimal"):
        SectorRotationRecord(
            isin=S1,
            momentum=0.5,  # type: ignore[arg-type]  # the bug the guard catches
            price=Decimal("100"),
            sector=STEADY,
            knowable_date=SESSION,
        )


def test_non_positive_top_k_and_top_n_are_refused() -> None:
    """The a-priori knobs must be positive — a zero-sector or zero-name basket is nonsense."""
    with pytest.raises(ValueError, match="top_k must be positive"):
        SectorRotationParameters(top_k=0)
    with pytest.raises(ValueError, match="top_n must be positive"):
        SectorRotationParameters(top_n=0)
