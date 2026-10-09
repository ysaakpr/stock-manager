"""M17.6 — the control books and BENCH-N500 (pre-registration §3).

* ``CTRL-<manager>`` is equal weight across the top *N* of the session's shortlist (*N* = the
  manager's max positions), rebalanced weekly (swing) or every 21 sessions (positional), on the
  same rails, costs and capital as its manager — and never calls a model;
* a name that leaves the top *N* is sold, a buy that cannot fit yet waits, a buy the rails refuse is
  journaled and dropped, never resized;
* ``BENCH-N500`` resolves the roster's ``nifty500-tri-proxy`` to the backtests' TRI reader, is
  bought at the close before S0, marks ``capital * level / base`` and never splices two methods.
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from analyst.fundmanager import ControlMandate, load_roster
from analyst.fundmanager.books import BookError
from analyst.fundmanager.controls import (
    BENCHMARK_INDEX_SLUGS,
    CONTROL_BUY_BUDGET_FRACTION,
    BenchBook,
    BenchmarkUnavailableError,
    BenchState,
    ControlBook,
    ControlError,
    ControlSession,
    ControlState,
    LakeTriBenchmark,
    rebalance_due,
)
from analyst.fundmanager.scoreboard import MARK_EVENT, BookMark
from analyst.journal.models import Decision
from backtest.fm_paper import M17PaperAccount
from dataplatform.clock import FrozenClock
from dataplatform.ingest.indices import TRI_METHOD_COMPUTED, TriPoint, TriSeries, write_tri_l1
from execution.broker import Side
from tests.fm_books_support import FmMarket, ListJournal, open_book, switch_at, weekdays
from tests.fm_scoreboard_support import DictBench, drifting_market, isin, shortlist_of

REPO = Path(__file__).resolve().parents[2]
SESSIONS = weekdays(date(2025, 1, 1), 70)
D0 = SESSIONS[20]  # a full participation lookback behind it
NAMES = [isin(n) for n in range(1, 31)]
FLAT = {name: Decimal(0) for name in NAMES}


def _ctrl(book_id: str) -> ControlMandate:
    mandate = load_roster().get(book_id)
    assert isinstance(mandate, ControlMandate)
    return mandate


def _control(
    book_id: str = "CTRL-FM-SWING-10L",
    *,
    market: FmMarket | None = None,
    tmp_path: Path,
    clock: FrozenClock,
    journal: ListJournal | None = None,
) -> tuple[ControlBook, M17PaperAccount, ListJournal, FmMarket]:
    roster = load_roster()
    mandate = _ctrl(book_id)
    journal = ListJournal() if journal is None else journal
    market = drifting_market(SESSIONS, NAMES, drift=FLAT) if market is None else market
    book, account = open_book(
        book_id, market=market, clock=clock, kill_switch=switch_at(tmp_path, clock), journal=journal
    )
    return ControlBook(mandate, book, roster.rails), account, journal, market


def _session(
    control: ControlBook, clock: FrozenClock, session: date, names: list[str]
) -> ControlSession:
    clock.freeze_at(session)
    control.book.execute(session)
    return control.run(session, shortlist_of(session, names), dict.fromkeys(names, "LARGE"))


# ── the cadence ─────────────────────────────────────────────────────────────────────────────────


def test_the_roster_controls_mirror_their_managers_and_name_their_cadence() -> None:
    roster = load_roster()
    for manager in roster.managers:
        control = roster.control_for(manager.id)
        assert control.shortlist_top_n == manager.max_positions
        assert control.opening_capital_inr == manager.opening_capital_inr


def test_weekly_cadence_rebalances_on_the_first_session_of_a_new_iso_week() -> None:
    mandate = _ctrl("CTRL-FM-SWING-10L")
    monday = date(2025, 1, 6)
    state = ControlState(last_rebalance=monday)
    assert rebalance_due(mandate, ControlState(), monday, sessions_since=None)
    for later in range(1, 5):  # Tue..Fri, same ISO week
        assert not rebalance_due(
            mandate, state, monday + timedelta(days=later), sessions_since=None
        )
    assert rebalance_due(mandate, state, monday + timedelta(days=7), sessions_since=None)
    # A holiday Monday: the Tuesday is the week's first session, so it rebalances.
    assert rebalance_due(mandate, state, monday + timedelta(days=8), sessions_since=None)


def test_positional_cadence_is_every_21_sessions_exactly() -> None:
    mandate = _ctrl("CTRL-FM-POS-10L")
    state = ControlState(last_rebalance=date(2025, 1, 6))
    later = date(2025, 3, 1)
    assert not rebalance_due(mandate, state, later, sessions_since=20)
    assert rebalance_due(mandate, state, later, sessions_since=21)
    with pytest.raises(ControlError):
        rebalance_due(mandate, state, later, sessions_since=None)


# ── equal weight over the top N ─────────────────────────────────────────────────────────────────


def test_the_control_buys_equal_weight_across_exactly_the_top_n(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    control, account, journal, _ = _control(tmp_path=tmp_path, clock=clock)
    ranked = list(reversed(NAMES))  # 30 names on the list; N = 15
    done = _session(control, clock, D0, ranked)

    assert done.rebalanced
    bought = [o.isin for o in done.orders if o.side is Side.BUY]
    assert bought == ranked[:15]
    each = Decimal(1000000) * CONTROL_BUY_BUDGET_FRACTION / 15
    assert {o.quantity for o in done.orders} == {int(each // 100)}
    assert [o.isin for o, _ in done.report.staged] == ranked[:15]
    assert all(b.cap_tier == "LARGE" for b in done.buys) and len(done.buys) == 15

    # They fill at the next open, through the paper account, and the book reconciles.
    nxt = SESSIONS[21]
    clock.freeze_at(nxt)
    executed = control.book.execute(nxt)
    assert executed.recon is not None and executed.recon.ok
    assert set(account.quantities()) == set(ranked[:15])
    rebalance_lines = [e for e in journal.entries if e.payload.get("event") == "CONTROL_REBALANCE"]
    assert len(rebalance_lines) == 1
    assert rebalance_lines[0].payload["targets"].split(",") == ranked[:15]


def test_the_control_sells_what_left_the_top_n_and_waits_for_the_slot(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    control, account, _, _ = _control(tmp_path=tmp_path, clock=clock)
    first = NAMES[:15]
    _session(control, clock, D0, first + NAMES[15:])
    # Run the week out without a rebalance.
    i = SESSIONS.index(D0) + 1
    while SESSIONS[i].isocalendar()[:2] == D0.isocalendar()[:2]:
        done = _session(control, clock, SESSIONS[i], first)
        assert not done.rebalanced
        i += 1
    # Next week: one name drops out, a new one enters at position 15.
    new_top = [*NAMES[:14], NAMES[20]]
    week2 = SESSIONS[i]
    done = _session(control, clock, week2, new_top)
    assert done.rebalanced
    assert [(o.isin, o.side) for o in done.orders] == [(NAMES[14], Side.SELL)]
    assert done.state.pending == (NAMES[20],), "the book is full until the sale fills"
    # The sale fills at the next open; settlement frees the cash; the new name is then bought.
    for session in SESSIONS[i + 1 : i + 4]:
        done = _session(control, clock, session, new_top)
        if done.buys:
            break
    assert [b.isin for b in done.buys] == [NAMES[20]]
    clock.freeze_at(SESSIONS[SESSIONS.index(session) + 1])
    control.book.execute(SESSIONS[SESSIONS.index(session) + 1])
    assert set(account.quantities()) == set(new_top)


def test_drift_inside_the_band_is_left_alone_and_outside_it_is_trimmed(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    # One name doubles over the first week; the others stay flat.
    drift = dict(FLAT)
    drift[NAMES[0]] = Decimal("0.2")
    market = drifting_market(SESSIONS, NAMES, drift=drift)
    control, _, _, _ = _control(tmp_path=tmp_path, clock=clock, market=market)
    top = NAMES[:15]
    _session(control, clock, D0, top)
    i = SESSIONS.index(D0) + 1
    while SESSIONS[i].isocalendar()[:2] == D0.isocalendar()[:2]:
        _session(control, clock, SESSIONS[i], top)
        i += 1
    done = _session(control, clock, SESSIONS[i], top)
    assert done.rebalanced
    sides = {(o.isin, o.side) for o in done.orders}
    assert (NAMES[0], Side.SELL) in sides, "the runaway name is trimmed back to equal weight"
    assert all(o.isin == NAMES[0] for o in done.orders if o.side is Side.SELL)
    assert not [o for o in done.orders if o.side is Side.BUY and o.isin != NAMES[0]]


def test_a_buy_the_rails_refuse_is_journaled_and_dropped_never_resized(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    # Six names in one sector: equal weight is 6 x 6.5 % = 39 % against the 30 % sector cap.
    sectors = {name: ("BANKS" if i < 6 else f"S{i}") for i, name in enumerate(NAMES)}
    market = drifting_market(SESSIONS, NAMES, drift=FLAT, sectors=sectors)
    control, _, journal, _ = _control(tmp_path=tmp_path, clock=clock, market=market)
    done = _session(control, clock, D0, NAMES[:15])
    blocks = [e for e in journal.entries if e.decision is Decision.RAIL_BLOCK]
    assert blocks and all(e.payload["rails"] == "MAX_SECTOR" for e in blocks)
    refused = {o.isin for o, _ in done.report.refused}
    assert refused and refused.isdisjoint(done.state.pending)
    staged = {o.isin: o.quantity for o, _ in done.report.staged}
    assert len(set(staged.values())) == 1, "no order was resized to squeeze past the rail"


def test_no_shortlist_means_no_rebalance_and_the_rebalance_stays_due(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    control, _, journal, _ = _control(tmp_path=tmp_path, clock=clock)
    control.book.execute(D0)
    done = control.run(D0, None, {})
    assert not done.rebalanced and done.orders == () and done.state.last_rebalance is None
    assert journal.entries[-1].decision is Decision.HOLD
    nxt = SESSIONS[21]
    done = _session(control, clock, nxt, NAMES)
    assert done.rebalanced


def test_a_shortlist_for_another_session_is_refused(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    control, _, _, _ = _control(tmp_path=tmp_path, clock=clock)
    with pytest.raises(ControlError, match="shortlist of"):
        control.run(D0, shortlist_of(SESSIONS[19], NAMES), {})


def test_a_tampered_shortlist_is_refused(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    control, _, _, _ = _control(tmp_path=tmp_path, clock=clock)
    good = shortlist_of(D0, NAMES)
    forged = good.model_copy(update={"entries": tuple(reversed(good.entries))})
    with pytest.raises(ValueError, match="does not reproduce its digest"):
        control.run(D0, forged, {})


def test_control_state_round_trips_and_continues_identically(tmp_path: Path) -> None:
    state = ControlState(
        last_rebalance=D0,
        targets=(NAMES[0], NAMES[1]),
        target_value=Decimal("65333.33"),
        cap_tiers={NAMES[0]: "LARGE", NAMES[1]: None},
        pending=(NAMES[1],),
    )
    assert ControlState.from_document(state.to_document()) == state


def test_the_control_path_never_calls_a_model(tmp_path: Path) -> None:
    """No LLM: controls.py imports no model client, and no line a control writes names a model.

    (The model client is reachable transitively through ``analyst.monitor`` from every A9 user, so
    the import graph alone cannot prove this; the module's own imports and its output can.)
    """
    tree = ast.parse((REPO / "analyst/fundmanager/controls.py").read_text(encoding="utf-8"))
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names
    }
    assert not [m for m in imported if m.startswith(("analyst.llm", "anthropic", "subprocess"))]
    clock = FrozenClock(D0)
    control, _, journal, _ = _control(tmp_path=tmp_path, clock=clock)
    for session in SESSIONS[20:30]:
        _session(control, clock, session, NAMES[:15])
    assert journal.entries
    assert all(e.model is None and e.tokens is None for e in journal.entries)


# ── BENCH-N500 ──────────────────────────────────────────────────────────────────────────────────


def test_the_roster_bench_label_resolves_to_the_nifty500_tri_series() -> None:
    (bench,) = load_roster().benches
    assert BENCHMARK_INDEX_SLUGS[bench.benchmark] == "nifty500"


def test_the_bench_buys_the_close_before_s0_and_marks_capital_times_level_over_base() -> None:
    (mandate,) = load_roster().benches
    clock = FrozenClock(SESSIONS[21])
    journal = ListJournal()
    levels = DictBench({SESSIONS[20]: Decimal("20000"), SESSIONS[21]: Decimal("20500")})
    bench = BenchBook(mandate, journal=journal, clock=clock)
    state = bench.open(levels, SESSIONS[20])
    assert state == BenchState(SESSIONS[20], Decimal("20000"), "published")
    mark = bench.mark(levels, SESSIONS[21])
    # 10,00,000 x 20500 / 20000: a rise in the index is a rise in the bench, never the reverse.
    assert mark.nav == Decimal("1025000.000000")
    assert mark.nav > mandate.opening_capital_inr
    (entry,) = journal.entries
    assert entry.payload["event"] == MARK_EVENT and entry.case_id == "BENCH-N500"
    assert BookMark.model_validate_json(entry.payload["record"]) == mark
    assert BenchState.from_document(state.to_document()) == state


def test_the_bench_never_splices_methods_or_guesses_a_missing_level() -> None:
    (mandate,) = load_roster().benches
    clock = FrozenClock(SESSIONS[22])
    bench = BenchBook(mandate, journal=ListJournal(), clock=clock)
    published = DictBench({SESSIONS[20]: Decimal("100"), SESSIONS[21]: Decimal("101")})
    bench.open(published, SESSIONS[20])
    with pytest.raises(BenchmarkUnavailableError, match="never splices"):
        bench.mark(
            DictBench({SESSIONS[21]: Decimal("101")}, method=TRI_METHOD_COMPUTED), SESSIONS[21]
        )
    with pytest.raises(BenchmarkUnavailableError, match="no published level"):
        bench.mark(published, SESSIONS[22])
    with pytest.raises(ControlError):
        bench.mark(published, SESSIONS[20])  # not after the purchase


def _write_series(root: Path, slug: str, days: list[date], method: str) -> None:
    points = tuple(
        TriPoint(
            index_slug=slug,
            index_name="NIFTY 500",
            as_of=day,
            tri_value=Decimal(1000 + i),
            price_close=Decimal(1000 + i) if method == TRI_METHOD_COMPUTED else None,
            method=method,
        )
        for i, day in enumerate(days)
    )
    write_tri_l1(
        TriSeries(index_slug=slug, index_name="NIFTY 500", method=method, points=points),
        data_root=root,
    )


def test_the_lake_bench_reads_the_backtests_tri_series_point_in_time(tmp_path: Path) -> None:
    days = SESSIONS[:5]
    _write_series(tmp_path, "nifty500", days, TRI_METHOD_COMPUTED)
    lake = LakeTriBenchmark("nifty500-tri-proxy", through=days[2], data_root=tmp_path)
    assert lake.method == TRI_METHOD_COMPUTED
    assert lake.level(days[2]) == Decimal(1002)
    assert lake.level(days[3]) is None, "a level after `through` is never read"
    with pytest.raises(BenchmarkUnavailableError, match="no 'nifty500' series"):
        LakeTriBenchmark(
            "nifty500-tri-proxy", through=days[2], method="published", data_root=tmp_path
        )


def test_an_empty_lake_or_an_unknown_label_is_loud(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkUnavailableError, match="cannot be marked"):
        LakeTriBenchmark("nifty500-tri-proxy", through=D0, data_root=tmp_path)
    with pytest.raises(BenchmarkUnavailableError, match="unknown benchmark label"):
        LakeTriBenchmark("nifty50", through=D0, data_root=tmp_path)


def test_a_held_name_without_a_close_cannot_be_rebalanced(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    control, _, _, market = _control(tmp_path=tmp_path, clock=clock)
    _session(control, clock, D0, NAMES[:15])
    nxt = SESSIONS[21]
    clock.freeze_at(nxt)
    control.book.execute(nxt)
    later = next(s for s in SESSIONS if s.isocalendar()[:2] != D0.isocalendar()[:2] and s > D0)
    for session in SESSIONS[22 : SESSIONS.index(later)]:
        _session(control, clock, session, NAMES[:15])
    del market.bars[(NAMES[0], later)]
    clock.freeze_at(later)
    control.book.execute(later)
    with pytest.raises(BookError, match="no close"):
        control.run(later, shortlist_of(later, NAMES[:15]), {})
