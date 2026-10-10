"""M17.13 — the suspended-holding rule (owner decision 2026-10-10).

A held name that is still listed but has no bar on a session that otherwise printed normally is
SUSPENDED for that session: marked at its last traded close, journaled ``SUSPENDED_HOLDING``, its
sells held over until it prints, its outcomes scored at the last close with ``suspended`` set. A
session whose market data is broadly missing is red data, never a list of suspensions.

Offline throughout: the M17.7 job harness (`tests.unit.test_fm_job`), an in-memory `FmMarket`, and
the production `LakeSuspendedNames` over that market's prints. Each test fails if the rule it
pins is inverted — the "without the rule" runs show what the job did before M17.13.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from analyst.fundmanager.books import (
    SUSPENDED_EXIT_HELD_EVENT,
    SUSPENDED_HOLDING_EVENT,
    UNFILLED_SUSPENDED_EVENT,
    BookError,
    LastTraded,
)
from analyst.fundmanager.bundle import Holding, ManagerBook
from analyst.fundmanager.job import SKIPPED_DATA_RED_EVENT
from analyst.fundmanager.render import render_holdings
from analyst.fundmanager.scoreboard import (
    MARK_EVENT,
    OUTCOME_EVENT,
    DecisionAction,
    DecisionOutcome,
    ScoredDecision,
    build_scoreboard,
    inputs_from_journal,
    resolve_outcome,
    suspended_holdings_on,
)
from analyst.fundmanager.stops import STOP_EXIT_EVENT
from analyst.journal.models import Decision
from backtest.fm_job import RunOutcome
from backtest.fm_world import (
    COVERAGE_LOOKBACK_SESSIONS,
    SUSPENSION_COVERAGE_FLOOR,
    LakeSuspendedNames,
    printed_normally,
)
from dataplatform.clock import IST
from dataplatform.status.queries import read_managers_status
from tests.fm_books_support import Bar, FmMarket
from tests.fm_scoreboard_support import mini_roster
from tests.unit.commons_screen_world import NAMES, SECTOR
from tests.unit.test_commons_sheets import _isin
from tests.unit.test_fm_job import (
    BENCH,
    CALENDAR,
    FLAT,
    HELD,
    Builder,
    Desk,
    FakeWorld,
    PerManagerLLM,
    ScriptedRunner,
    Verdict,
    _decision,
    _verdict,
    scripted_commons,
    staged,
)

#: Enough extra names that one or two missing ones leave the session's coverage above the floor.
EXTRA = tuple(_isin(600 + k) for k in range(30))
UNIVERSE = (*NAMES, *EXTRA)
TV = Decimal("10000000000")
D = date  # brevity in the session tables below
BOUGHT, FILLED, LAST, SUSP1, SUSP2, BACK = (
    D(2026, 10, 5),
    D(2026, 10, 6),
    D(2026, 10, 7),
    D(2026, 10, 8),
    D(2026, 10, 9),
    D(2026, 10, 12),
)


# ── the world: an FmMarket and the production suspension test over its prints ───────────────────


class FmPrints:
    """The three `_L1Reader` reads `LakeSuspendedNames` makes, over an `FmMarket`'s bars."""

    def __init__(self, market: FmMarket) -> None:
        self.market = market

    def closes_on(self, session: date) -> dict[str, Decimal]:
        return {i: b.close for (i, d), b in self.market.bars.items() if d == session}

    def restricted_close(self, isin: str, session: date) -> None:
        return None

    def last_prints(self, upto: date) -> dict[str, date]:
        out: dict[str, date] = {}
        for isin, day in self.market.bars:
            if day <= upto and day > out.get(isin, date.min):
                out[isin] = day
        return out


def suspended_names(market: FmMarket) -> LakeSuspendedNames:
    return LakeSuspendedNames(
        reader=FmPrints(market),  # type: ignore[arg-type]
        sessions_before=lambda day, n: [s for s in CALENDAR if s < day][-n:],
        adjusted=market.close,
    )


def market_without(
    gaps: Mapping[str, Sequence[date]], overrides: Mapping[tuple[str, date], Bar] | None = None
) -> FmMarket:
    """Every UNIVERSE name flat at 100 every session, less the ``gaps`` (no bar at all)."""
    bars = {(i, d): Bar(FLAT, FLAT, TV) for i in UNIVERSE for d in CALENDAR}
    bars.update(overrides or {})
    for isin, days in gaps.items():
        for day in days:
            bars.pop((isin, day), None)
    return FmMarket(CALENDAR, bars, {i: SECTOR.get(i, "Tech") for i in UNIVERSE})


def world_of(market: FmMarket, *, rule: bool = True) -> FakeWorld:
    return FakeWorld(market, suspended_names=suspended_names(market) if rule else None)


@dataclass
class ViewingRunner(ScriptedRunner):
    """`ScriptedRunner` that also keeps the book view each manager was shown."""

    views: dict[date, Any] = field(default_factory=dict)

    def __call__(
        self, mandate: Any, session: date, commons: Any, book: Any, *a: Any, **k: Any
    ) -> Any:
        self.views[session] = book
        return super().__call__(mandate, session, commons, book, *a, **k)


def _buy_held(
    runner_script: dict[Any, Any], manager: str, *, stop: int = 4, horizon: int = 20
) -> None:
    runner_script[(manager, BOUGHT)] = [
        _verdict(_decision(HELD, "BUY", stop_pct=stop, horizon_sessions=horizon), quantity=500)
    ]


def _run(desk: Desk, days: Sequence[date], world: FakeWorld, runner: Any, **kw: Any) -> list[Any]:
    roster = kw.pop("roster", mini_roster())
    return [
        desk.run(
            day,
            world,
            Builder(scripted_commons),
            PerManagerLLM({}),
            runner=runner,
            roster=roster,
            start=day == days[0],
            **kw,
        )
        for day in days
    ]


def _assert_rebuilds(desk: Desk, results: Sequence[Any]) -> None:
    roster = mini_roster()
    for result in results:
        assert result.scoreboard is not None and result.scoreboard_error is None
        assert result.rebuilt_digest == result.scoreboard.digest()
    rebuilt = build_scoreboard(roster, inputs_from_journal(desk.entries(), roster))
    assert rebuilt.canonical_bytes() == results[-1].scoreboard.canonical_bytes()


# ── the single-name-versus-market test ───────────────────────────────────────────────────────────


def test_printed_normally_is_coverage_against_the_recent_median() -> None:
    usual = [100, 100, 98, 102, 100]
    assert printed_normally(90, usual)  # exactly the floor
    assert not printed_normally(89, usual)
    assert not printed_normally(0, usual) and not printed_normally(100, [])
    assert Decimal("0.90") == SUSPENSION_COVERAGE_FLOOR and COVERAGE_LOOKBACK_SESSIONS == 5


def test_lake_suspended_names_suspends_one_name_and_never_a_thin_session() -> None:
    market = market_without({HELD: [SUSP1]})
    names = suspended_names(market)
    assert names.last_traded(HELD, SUSP1) == LastTraded(LAST, FLAT, FLAT)
    assert names.last_traded(HELD, LAST) is None  # it printed
    assert names.last_traded(NAMES[0], SUSP1) is None  # it printed
    # most of the market missing on the session: a market-wide gap, nothing is suspended
    thin = market_without({i: [SUSP1] for i in UNIVERSE[:-3]})
    assert not suspended_names(thin).printed_normally(SUSP1)
    assert suspended_names(thin).last_traded(HELD, SUSP1) is None

    class Gone:
        def last_traded(self, isin: str, session: date) -> LastTraded | None:
            return LastTraded(LAST, FLAT, FLAT)

    delisted = LakeSuspendedNames(
        reader=FmPrints(market),  # type: ignore[arg-type]
        sessions_before=lambda day, n: [s for s in CALENDAR if s < day][-n:],
        adjusted=market.close,
        delisted=Gone(),
    )
    assert delisted.last_traded(HELD, SUSP1) is None  # a delisting is not a suspension


# ── 1. a held name missing on a normal session ───────────────────────────────────────────────────


def test_a_held_name_missing_on_a_normal_session_is_marked_at_its_last_close(
    tmp_path: Path,
) -> None:
    roster = mini_roster()
    manager, control = roster.managers[0].id, roster.controls[0].id
    market = market_without({HELD: [SUSP1, SUSP2]})
    script: dict[Any, Any] = {}
    _buy_held(script, manager)
    runner = ViewingRunner(script)
    desk = Desk(tmp_path)
    days = [BOUGHT, FILLED, LAST, SUSP1, SUSP2, BACK]
    results = _run(desk, days, world_of(market), runner)

    assert all(r.outcome is RunOutcome.COMPLETED for r in results)
    lines = desk.events(SUSPENDED_HOLDING_EVENT)
    assert [(e.case_id, e.isin, e.trading_date) for e in lines] == [
        (manager, HELD, SUSP1),
        (manager, HELD, SUSP2),
    ]
    assert [e.payload["sessions_suspended"] for e in lines] == ["1", "2"]
    assert {e.payload["last_trade_date"] for e in lines} == {LAST.isoformat()}
    assert all(e.decision is Decision.HEARTBEAT for e in lines)

    def mark(book: str, day: date) -> dict[str, Any]:
        (line,) = desk.events(MARK_EVENT, book=book, on=day)
        return dict(json.loads(line.payload["record"]))

    # marked at the last close: the same NAV as the session it last printed, one position held
    for day in (SUSP1, SUSP2):
        assert mark(manager, day)["positions"] == 1
        assert mark(manager, day)["nav"] == mark(manager, LAST)["nav"]
    # the other book is unaffected: its marks every session, never a suspension line
    assert all(desk.events(MARK_EVENT, book=control, on=d) for d in days)
    assert not desk.events(SUSPENDED_HOLDING_EVENT, book=control)
    # the name printed again: the suspension is over and the book marks normally
    assert (
        not desk.events(SUSPENDED_HOLDING_EVENT, on=BACK) and mark(manager, BACK)["positions"] == 1
    )
    # the manager was shown the holding as suspended, and only while it was
    assert runner.views[SUSP1].holding(HELD).suspended_since == LAST
    assert runner.views[SUSP2].holding(HELD).suspended_since == LAST
    assert runner.views[BACK].holding(HELD).suspended_since is None
    # the digest says so
    digest = (tmp_path / "digest" / f"digest-{SUSP2.isoformat()}.md").read_text()
    assert f"| {manager} | {HELD} | held, not trading since {LAST.isoformat()} | 2 |" in digest
    assert (
        "Suspended holdings"
        not in (tmp_path / "digest" / f"digest-{BACK.isoformat()}.md").read_text()
    )
    _assert_rebuilds(desk, results)


def test_without_the_rule_the_same_gap_still_fails_the_job_loudly(tmp_path: Path) -> None:
    roster = mini_roster()
    market = market_without({HELD: [SUSP1]})
    script: dict[Any, Any] = {}
    _buy_held(script, roster.managers[0].id)
    desk = Desk(tmp_path)
    _run(desk, [BOUGHT, FILLED, LAST], world_of(market, rule=False), ScriptedRunner(script))
    with pytest.raises(BookError, match="neither delisted nor suspended"):
        desk.run(
            SUSP1,
            world_of(market, rule=False),
            Builder(scripted_commons),
            PerManagerLLM({}),
            runner=ScriptedRunner(script),
            roster=roster,
        )


def test_status_managers_lists_the_suspended_holding(tmp_path: Path) -> None:
    roster = mini_roster()
    manager = roster.managers[0].id
    market = market_without({HELD: [SUSP1]})
    script: dict[Any, Any] = {}
    _buy_held(script, manager)
    desk = Desk(tmp_path)
    _run(desk, [BOUGHT, FILLED, LAST, SUSP1], world_of(market), ScriptedRunner(script))
    body = read_managers_status(
        desk.entries(), roster=roster, as_of=datetime(2026, 10, 9, 8, 0, tzinfo=IST)
    )
    assert [
        (h.book_id, h.isin, h.last_trade_date, h.sessions_suspended)
        for h in body.suspended_holdings
    ] == [(manager, HELD, LAST, 1)]
    assert body.managers[0].suspended_resolved_decisions == 0
    session, lines = suspended_holdings_on(desk.entries(), roster)
    assert session == SUSP1 and len(lines) == 1


# ── 2. a broadly missing session ─────────────────────────────────────────────────────────────────


def test_a_broadly_missing_session_is_red_and_the_interlock_stops_it(tmp_path: Path) -> None:
    roster = mini_roster()
    market = market_without({i: [SUSP1] for i in UNIVERSE})  # nothing printed
    script: dict[Any, Any] = {}
    _buy_held(script, roster.managers[0].id)
    desk = Desk(tmp_path)
    _run(desk, [BOUGHT, FILLED, LAST], world_of(market), ScriptedRunner(script))
    red = desk.run(
        SUSP1,
        world_of(market),
        Builder(lambda _: pytest.fail("no Commons on a red day")),
        PerManagerLLM({}),
        runner=ScriptedRunner(script),
        roster=roster,
        gate=lambda _: Verdict(False, "nse_bhavcopy is MISSING for 2026-10-08"),
    )
    assert red.outcome is RunOutcome.SKIPPED_DATA_RED
    assert {e.case_id for e in desk.events(SKIPPED_DATA_RED_EVENT, on=SUSP1)} == {
        b.id for b in roster.books
    }
    assert not desk.events(MARK_EVENT, on=SUSP1)
    assert not desk.events(SUSPENDED_HOLDING_EVENT)


def test_a_thin_session_past_a_green_gate_suspends_nothing_and_marks_nothing(
    tmp_path: Path,
) -> None:
    # The gate should never be green on such a day; if it is, a held gap is not a suspension.
    roster = mini_roster()
    market = market_without({i: [SUSP1] for i in UNIVERSE[:-4]})
    script: dict[Any, Any] = {}
    _buy_held(script, roster.managers[0].id)
    desk = Desk(tmp_path)
    _run(desk, [BOUGHT, FILLED, LAST], world_of(market), ScriptedRunner(script))
    with pytest.raises(BookError, match="neither delisted nor suspended"):
        desk.run(
            SUSP1,
            world_of(market),
            Builder(scripted_commons),
            PerManagerLLM({}),
            runner=ScriptedRunner(script),
            roster=roster,
        )
    assert not desk.events(MARK_EVENT, on=SUSP1)
    assert not desk.events(SUSPENDED_HOLDING_EVENT)


# ── 3. no pretend trades ─────────────────────────────────────────────────────────────────────────


def test_a_stop_exit_on_a_suspended_name_waits_unfilled_and_fills_when_it_prints(
    tmp_path: Path,
) -> None:
    roster = mini_roster()
    manager = roster.managers[0].id
    breach = Bar(Decimal("85"), Decimal("80"), TV)  # SUSP1 closes below the 96 stop
    stopped, gone1, gone2, back, fill = (
        D(2026, 10, 8),
        D(2026, 10, 9),
        D(2026, 10, 12),
        D(2026, 10, 13),
        D(2026, 10, 14),
    )
    market = market_without(
        {HELD: [gone1, gone2]},
        {
            (HELD, stopped): breach,
            (HELD, back): Bar(Decimal("81"), Decimal("80"), TV),
            (HELD, fill): Bar(Decimal("79"), Decimal("79"), TV),
        },
    )
    script: dict[Any, Any] = {}
    _buy_held(script, manager)
    desk = Desk(tmp_path)
    days = [BOUGHT, FILLED, LAST, stopped, gone1, gone2, back, fill]
    world = world_of(market)
    results = _run(desk, days, world, ScriptedRunner(script))
    assert all(r.outcome is RunOutcome.COMPLETED for r in results)

    exits = [e for e in staged(desk, manager) if e.payload.get("event") == STOP_EXIT_EVENT]
    assert [e.trading_date for e in exits] == [stopped, back]  # once on the breach, once on print
    assert exits[1].payload["exit_parent"] == exits[0].orders_ref
    (unfilled,) = desk.events(UNFILLED_SUSPENDED_EVENT, book=manager)
    assert unfilled.trading_date == gone1 and unfilled.isin == HELD
    assert unfilled.payload["order_event"] == STOP_EXIT_EVENT
    assert unfilled.payload["reoffered"] == "true"
    held_over = desk.events(SUSPENDED_EXIT_HELD_EVENT, book=manager)
    assert [e.trading_date for e in held_over] == [gone1, gone2]
    assert all(e.decision is Decision.DEFERRED for e in held_over)

    # never filled while suspended: the position is held through both gaps, gone after the fill
    def positions(day: date) -> int:
        (line,) = desk.events(MARK_EVENT, book=manager, on=day)
        return int(json.loads(line.payload["record"])["positions"])

    assert [positions(d) for d in (gone1, gone2, back, fill)] == [1, 1, 1, 0]
    (recon,) = [
        e
        for e in desk.events("RECONCILIATION", book=manager, on=fill)
        if e.payload.get("fills") == "1"
    ]
    assert recon.payload["positions"] == "0"
    _assert_rebuilds(desk, results)


def test_without_the_rule_a_stop_exit_on_a_gap_is_lost_or_fails(tmp_path: Path) -> None:
    """The inversion of the test above: with no suspension source the rejected stop exit is not
    held over (and the gap itself fails the job) — so the re-offer above is the rule's doing."""
    roster = mini_roster()
    manager = roster.managers[0].id
    market = market_without(
        {HELD: [D(2026, 10, 9)]}, {(HELD, D(2026, 10, 8)): Bar(Decimal("85"), Decimal("80"), TV)}
    )
    script: dict[Any, Any] = {}
    _buy_held(script, manager)
    desk = Desk(tmp_path)
    _run(desk, [BOUGHT, FILLED, LAST, SUSP1], world_of(market, rule=False), ScriptedRunner(script))
    with pytest.raises(BookError):
        desk.run(
            D(2026, 10, 9),
            world_of(market, rule=False),
            Builder(scripted_commons),
            PerManagerLLM({}),
            runner=ScriptedRunner(script),
            roster=roster,
        )
    assert not desk.events(UNFILLED_SUSPENDED_EVENT)


def test_a_manager_sell_of_a_suspended_name_is_held_and_a_buy_with_no_bar_never_stages(
    tmp_path: Path,
) -> None:
    roster = mini_roster()
    manager = roster.managers[0].id
    other = EXTRA[0]  # also dark on SUSP1, never held
    market = market_without({HELD: [SUSP1, SUSP2], other: [SUSP1]})
    script: dict[Any, Any] = {}
    _buy_held(script, manager)
    script[(manager, SUSP1)] = [
        _verdict(
            _decision(HELD, "SELL", what_changed={"kind": "BETTER_USE", "text": "a stronger name"})
        ),
        _verdict(_decision(other, "BUY", stop_pct=4), quantity=100),
    ]
    desk = Desk(tmp_path)
    days = [BOUGHT, FILLED, LAST, SUSP1, SUSP2, BACK, D(2026, 10, 13)]
    results = _run(desk, days, world_of(market), ScriptedRunner(script))
    assert all(r.outcome is RunOutcome.COMPLETED for r in results)

    held_over = desk.events(SUSPENDED_EXIT_HELD_EVENT, book=manager)
    assert [(e.trading_date, e.isin) for e in held_over] == [(SUSP1, HELD), (SUSP2, HELD)]
    sells = [e for e in staged(desk, manager) if e.decision is Decision.SELL]
    assert [(e.trading_date, e.isin, e.payload["quantity"]) for e in sells] == [(BACK, HELD, "500")]
    # the buy of a name with no bar: journaled UNPRICED, never staged, never filled
    (unpriced,) = desk.events("UNPRICED", book=manager)
    assert unpriced.isin == other and unpriced.trading_date == SUSP1
    assert not [e for e in staged(desk, manager) if e.isin == other]


# ── 4. scoring a decision that resolves during a suspension ──────────────────────────────────────


def test_a_decision_resolving_during_a_suspension_is_scored_at_the_last_close(
    tmp_path: Path,
) -> None:
    roster = mini_roster()
    manager = roster.managers[0].id
    # bought at 100 on BOUGHT; last prints at 110 on LAST; dark on SUSP1, when its horizon ends
    market = market_without({HELD: [SUSP1]}, {(HELD, LAST): Bar(FLAT, Decimal("110"), TV)})
    script: dict[Any, Any] = {}
    _buy_held(script, manager, horizon=3)
    desk = Desk(tmp_path)
    results = _run(
        desk, [BOUGHT, FILLED, LAST, SUSP1, SUSP2], world_of(market), ScriptedRunner(script)
    )

    (line,) = desk.events(OUTCOME_EVENT, book=manager)
    outcome = DecisionOutcome.model_validate_json(line.payload["record"])
    assert outcome.resolved_on == SUSP1 and outcome.suspended
    assert outcome.name_last_traded == LAST and outcome.name_return == Decimal("0.10000000")
    final = results[-1].scoreboard
    assert final is not None and final.managers[0].primary is not None
    assert final.managers[0].primary.suspended_resolved_decisions == 1
    assert final.managers[0].primary.resolved_decisions == 1
    digest = (tmp_path / "digest" / f"digest-{SUSP2.isoformat()}.md").read_text()
    assert "Resolved while suspended" in digest
    _assert_rebuilds(desk, results)


@dataclass
class _Prices:
    closes: Mapping[tuple[str, date], Decimal]

    def adjusted_close(self, isin: str, session: date) -> Decimal | None:
        return self.closes.get((isin, session))

    def bench_level(self, session: date) -> Decimal | None:
        return BENCH.level(session)


def test_resolve_outcome_flags_suspension_only_when_the_name_is_suspended() -> None:
    decision = ScoredDecision(
        book_id="FM-SWING-10L",
        decided_on=BOUGHT,
        isin=HELD,
        action=DecisionAction.HOLD,
        horizon_sessions=3,
        p_beat_bench=Decimal("0.6"),
        edge_type="TREND_LEADER",
    )
    calendar = [d for d in CALENDAR if BOUGHT <= d <= SUSP2]
    closes = {(HELD, BOUGHT): FLAT, (HELD, LAST): Decimal("110")}

    class Suspended:
        def last_traded(self, isin: str, session: date) -> LastTraded | None:
            if (isin, session) in closes:
                return None
            return LastTraded(LAST, Decimal("110"), Decimal("110"))

    scored = resolve_outcome(
        decision, calendar=calendar, as_of=SUSP1, prices=_Prices(closes), suspended=Suspended()
    )
    assert scored is not None and scored.suspended and scored.name_return == Decimal("0.10000000")
    # the name printed on the resolution session: no flag, its own close
    printed = {**closes, (HELD, SUSP1): Decimal("120")}
    plain = resolve_outcome(
        decision, calendar=calendar, as_of=SUSP1, prices=_Prices(printed), suspended=Suspended()
    )
    assert plain is not None and not plain.suspended and plain.name_last_traded is None


# ── 5. the manager's view ────────────────────────────────────────────────────────────────────────


def test_the_rendered_view_says_suspended_since_with_no_price_or_pnl() -> None:
    holding = Holding(
        isin=HELD,
        sector="Tech",
        weight_pct=Decimal("5.00"),
        sessions_held=4,
        opening_thesis="a thesis",
        invalidation=(),
        stop_price=Decimal("96"),
        suspended_since=LAST,
    )
    block = render_holdings(
        ManagerBook("FM-SWING-10L", Decimal("1000000"), Decimal("95"), (holding,)), {}
    )
    assert f"SUSPENDED: held, not trading since {LAST.isoformat()}" in block
    for word in ("cost", "p&l", "pnl", "profit", "loss", "paid", "entry price", "average", "96"):
        assert word not in block.lower()
    live = dataclasses.replace(holding, suspended_since=None)
    plain = render_holdings(
        ManagerBook("FM-SWING-10L", Decimal("1000000"), Decimal("95"), (live,)), {}
    )
    assert "SUSPENDED" not in plain
