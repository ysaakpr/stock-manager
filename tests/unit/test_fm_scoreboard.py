"""M17.6 — the scoreboard: pre-registration §6 and Amendment 1 (f), from persisted state only.

Each test here fails against an implementation with the comparison inverted, a threshold moved or
the Brier formula flipped:

* the §6 rule at every boundary (+3.0 pp, -3.0 pp, bench drawdown + 5 pp, Brier 0.25, 30 resolved);
* the Brier score of known forecasts, and an unbalanced set where flipping the outcome changes it;
* a manager ahead of its control has a positive excess and passes; the mirror image clear-fails;
* the window: 62 sessions are in progress, 63 decide, an inconclusive result extends to 126;
* acceptance 1: a real run of a manager, its control and the bench, scored in-process, equals the
  scoreboard rebuilt from the journal alone, byte for byte;
* `GET /status/managers` and the daily digest carry no rationale, prompt or secret.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from analyst.fundmanager import (
    ControlMandate,
    ManagerMandate,
    Roster,
    StyleMandate,
    load_roster,
)
from analyst.fundmanager.books import BookError, BookOrder, FundBook
from analyst.fundmanager.controls import BenchBook, ControlBook, RankedTargets, record_mark
from analyst.fundmanager.digest import digest_path, render_digest, write_digest
from analyst.fundmanager.mirror import (
    MirrorState,
    divergence_entry,
    plan_mirror,
    settle_plan,
    target_weights,
)
from analyst.fundmanager.scoreboard import (
    BRIER_BAR,
    DECISION_EVENT,
    MIN_RESOLVED,
    S0_EVENT,
    WINDOW_SESSIONS,
    BookMark,
    ControlBuy,
    DecisionAction,
    DecisionOutcome,
    LastTraded,
    ModelCall,
    OutcomeError,
    OutcomeReason,
    Phase,
    RailRefusal,
    Scoreboard,
    ScoreboardError,
    ScoreboardInputs,
    ScoredDecision,
    ScoreVerdict,
    apply_rule,
    brier_score,
    build_scoreboard,
    decision_payload,
    inputs_from_journal,
    mark_book,
    max_drawdown_pp,
    outcome_entry,
    resolve_outcome,
    scored_decision_from_entry,
    shrunk_probability,
    todays_decisions,
)
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve, TokenSpend
from backtest.book_actions import BookActionCalendar
from backtest.cash_interest import load_repo_rate_schedule
from backtest.fm_circuit import NoCircuitData
from backtest.fm_paper import M17PaperAccount
from dataplatform.clock import FrozenClock
from dataplatform.status.api import app, clock_source, m17_journal_source, m17_roster_source
from execution.broker import Side
from execution.recon import RecordingAlerter
from tests.fm_books_support import FmMarket, ListJournal, open_book, switch_at, weekdays
from tests.fm_scoreboard_support import (
    DictBench,
    MarketPrices,
    compounding,
    drifting_market,
    flat_marks,
    isin,
    mini_roster,
    shortlist_of,
)

MANAGER, CONTROL, BENCH = "FM-SWING-BRK-10L", "CTRL-FM-SWING-BRK-10L", "BENCH-N500"
MIRROR, MIRROR_CONTROL, STYLE = "FM-SWING-BRK-1CR", "CTRL-FM-SWING-BRK-1CR", "STYLE-FM-SWING-BRK"
CAPITAL = Decimal(1000000)
SESSIONS = weekdays(date(2025, 1, 1), 160)
S0 = SESSIONS[21]
#: Written into every scripted rationale: it must never reach the status page or the digest.
RATIONALE_MARKER = "PRIVATE-RATIONALE-7f3a"


# ── the pure rule, at every boundary ─────────────────────────────────────────────────────────────


def _rule(
    excess: str = "5", dd: str = "4", bench_dd: str = "4", brier: str | None = "0.16", n: int = 40
) -> ScoreVerdict:
    verdict, _ = apply_rule(
        excess_vs_control_pp=Decimal(excess),
        manager_max_drawdown_pp=Decimal(dd),
        bench_max_drawdown_pp=Decimal(bench_dd),
        brier=None if brier is None else Decimal(brier),
        resolved=n,
    )
    return verdict


def test_excess_over_control_passes_at_plus_three_and_not_a_hair_below() -> None:
    assert _rule(excess="3.0") is ScoreVerdict.PASS
    assert _rule(excess="2.9999") is ScoreVerdict.INCONCLUSIVE
    assert _rule(excess="10") is ScoreVerdict.PASS


def test_excess_over_control_clear_fails_at_minus_three_and_not_a_hair_above() -> None:
    assert _rule(excess="-3.0") is ScoreVerdict.CLEAR_FAIL
    assert _rule(excess="-2.9999") is ScoreVerdict.INCONCLUSIVE
    # A losing manager with a perfect forecast record still fails on excess.
    assert _rule(excess="-8", brier="0.01") is ScoreVerdict.CLEAR_FAIL


def test_drawdown_may_exceed_the_bench_by_five_points_and_no_more() -> None:
    assert _rule(dd="11", bench_dd="6") is ScoreVerdict.PASS
    assert _rule(dd="11.0001", bench_dd="6") is ScoreVerdict.INCONCLUSIVE
    # A shallower drawdown than the bench is never what blocks a pass.
    assert _rule(dd="1", bench_dd="20") is ScoreVerdict.PASS


def test_brier_must_be_strictly_under_a_quarter_and_a_quarter_fails() -> None:
    assert _rule(brier="0.2499") is ScoreVerdict.PASS
    assert _rule(brier=str(BRIER_BAR)) is ScoreVerdict.CLEAR_FAIL
    assert _rule(brier="0.40") is ScoreVerdict.CLEAR_FAIL


def test_the_brier_leg_needs_thirty_resolved_decisions_either_way() -> None:
    assert MIN_RESOLVED == 30
    assert _rule(n=30) is ScoreVerdict.PASS
    assert _rule(n=29) is ScoreVerdict.INCONCLUSIVE, "29 resolved cannot pass"
    assert _rule(brier="0.40", n=29) is ScoreVerdict.INCONCLUSIVE, (
        "29 resolved cannot fail on Brier"
    )
    assert _rule(brier="0.40", n=30) is ScoreVerdict.CLEAR_FAIL
    assert _rule(brier=None, n=0) is ScoreVerdict.INCONCLUSIVE


def test_brier_is_the_mean_squared_error_of_p_against_the_outcome() -> None:
    assert brier_score([(Decimal("0.8"), True)]) == Decimal("0.04")
    assert brier_score([(Decimal("0.8"), False)]) == Decimal("0.64")
    assert brier_score([(Decimal("0.5"), True), (Decimal("0.5"), False)]) == Decimal("0.25")
    # Unbalanced: three right at 0.9, one wrong. Flipping o (or p) would give 0.61, not 0.21.
    pairs = [(Decimal("0.9"), True)] * 3 + [(Decimal("0.9"), False)]
    assert brier_score(pairs) == Decimal("0.21")
    assert brier_score([]) is None


def test_the_shrunk_forecast_is_half_p_and_half_the_base_rate() -> None:
    assert shrunk_probability(Decimal("0.9"), Decimal("0.5")) == Decimal("0.70")
    assert shrunk_probability(Decimal("0.2"), Decimal("0.6")) == Decimal("0.40")


def test_max_drawdown_is_the_worst_fall_from_a_running_peak() -> None:
    values = [Decimal(v) for v in ("100", "120", "90", "130", "117")]
    assert max_drawdown_pp(values) == Decimal(25)
    assert max_drawdown_pp([Decimal(1), Decimal(2), Decimal(3)]) == 0


# ── the scoreboard over synthetic marks ─────────────────────────────────────────────────────────


def _path(
    book_id: str,
    sessions: Sequence[date],
    *,
    final_pct: str,
    trough_pct: str = "0",
    capital: Decimal = CAPITAL,
) -> list[BookMark]:
    """Marks falling linearly to ``-trough_pct`` by session 20, then rising to ``final_pct``."""
    trough = capital * (1 - Decimal(trough_pct) / 100)
    final = capital * (1 + Decimal(final_pct) / 100)
    marks: list[BookMark] = []
    for i, session in enumerate(sessions):
        if i < 20:
            nav = capital + (trough - capital) * Decimal(i + 1) / 20
        else:
            span = len(sessions) - 20
            nav = trough + (final - trough) * Decimal(i - 19) / span
        marks.append(
            BookMark(
                book_id=book_id,
                session=session,
                nav=nav.quantize(Decimal("0.000001")),
                cash=Decimal(0),
                positions=0,
                turnover=Decimal(0),
                costs=Decimal(0),
                interest=Decimal(0),
            )
        )
    return marks


def _decisions(
    sessions: Sequence[date], *, n: int, p: str = "0.9", beats: int | None = None, horizon: int = 5
) -> tuple[list[ScoredDecision], list[DecisionOutcome]]:
    beats = n if beats is None else beats
    decisions: list[ScoredDecision] = []
    outcomes: list[DecisionOutcome] = []
    for i in range(n):
        decision = ScoredDecision(
            book_id=MANAGER,
            decided_on=sessions[i],
            isin=isin(i + 1),
            action=DecisionAction.PASS,
            horizon_sessions=horizon,
            p_beat_bench=Decimal(p),
            edge_type="MOMENTUM",
        )
        decisions.append(decision)
        outcomes.append(
            DecisionOutcome(
                decision_key=decision.key,
                book_id=MANAGER,
                isin=decision.isin,
                decided_on=decision.decided_on,
                resolved_on=sessions[i + horizon],
                sessions=horizon,
                name_return=Decimal("0.02") if i < beats else Decimal("-0.02"),
                bench_return=Decimal(0),
                reason=OutcomeReason.HORIZON,
            )
        )
    return decisions, outcomes


def _with_companions(marks: Sequence[BookMark], window: Sequence[date]) -> tuple[BookMark, ...]:
    """``marks`` plus flat marks for the mini roster's other books (the mirror, its control, the
    style book), which every session of a window needs and these tests do not script."""
    return (*marks, *flat_marks(mini_roster(), window, exclude={MANAGER, CONTROL, BENCH}))


def _board(
    *,
    sessions: int = WINDOW_SESSIONS,
    manager: str = "5",
    control: str = "1",
    manager_trough: str = "4",
    bench_trough: str = "4",
    n: int = 40,
    p: str = "0.6",
    beats: int | None = None,
) -> Scoreboard:
    window = SESSIONS[21 : 21 + sessions]
    decisions, outcomes = _decisions(window, n=n, p=p, beats=beats)
    inputs = ScoreboardInputs(
        s0=window[0],
        marks=_with_companions(
            (
                *_path(MANAGER, window, final_pct=manager, trough_pct=manager_trough),
                *_path(CONTROL, window, final_pct=control),
                *_path(BENCH, window, final_pct="2", trough_pct=bench_trough),
            ),
            window,
        ),
        decisions=tuple(decisions),
        outcomes=tuple(outcomes),
    )
    return build_scoreboard(mini_roster(), inputs)


def test_a_manager_ahead_of_its_control_has_positive_excess_and_passes() -> None:
    board = _board(manager="5", control="1")
    score, mirror = board.book_scores
    assert (score.book_id, score.control_id) == (MANAGER, CONTROL)
    assert score.primary is not None
    assert score.primary.excess_vs_control_pp == Decimal("4.0000")
    assert score.verdict is ScoreVerdict.PASS and score.phase is Phase.FINAL
    # the flat mirror ties its flat control: no excess, so it does not pass
    assert mirror.verdict is not ScoreVerdict.PASS
    assert board.passed == 1 and board.k_of_n == "1 of 2 books passed"


def test_the_mirror_image_is_a_clear_fail() -> None:
    board = _board(manager="1", control="5")
    score = board.book_scores[0]
    assert score.primary is not None
    assert score.primary.excess_vs_control_pp == Decimal("-4.0000")
    assert score.verdict is ScoreVerdict.CLEAR_FAIL
    assert board.k_of_n == "0 of 2 books passed"


def test_a_drawdown_past_the_bench_plus_five_blocks_the_pass() -> None:
    assert _board(manager_trough="9", bench_trough="4").book_scores[0].verdict is ScoreVerdict.PASS
    assert (
        _board(manager_trough="9.01", bench_trough="4").book_scores[0].primary.verdict  # type: ignore[union-attr]
        is ScoreVerdict.INCONCLUSIVE
    )


def test_brier_in_the_scoreboard_is_the_formula_and_flipping_outcomes_fails() -> None:
    # 30 decisions at p = 0.9, 24 right: (24 x 0.01 + 6 x 0.81) / 30 = 0.17 -> pass.
    good = _board(n=30, p="0.9", beats=24).book_scores[0]
    assert good.primary is not None and good.primary.brier == Decimal("0.17000000")
    assert good.verdict is ScoreVerdict.PASS
    # The same forecasts with the outcomes the other way round: (6 x 0.01 + 24 x 0.81) / 30 = 0.65.
    bad = _board(n=30, p="0.9", beats=6).book_scores[0]
    assert bad.primary is not None and bad.primary.brier == Decimal("0.65000000")
    assert bad.verdict is ScoreVerdict.CLEAR_FAIL


def test_thirty_resolved_decisions_pass_and_twenty_nine_do_not() -> None:
    assert _board(n=30).book_scores[0].verdict is ScoreVerdict.PASS
    score = _board(n=29).book_scores[0]
    assert score.primary is not None and score.primary.resolved_decisions == 29
    assert score.primary.verdict is ScoreVerdict.INCONCLUSIVE
    assert score.phase is Phase.EXTENSION, "an inconclusive window runs its one extension"


def test_a_decision_resolving_after_the_window_does_not_count() -> None:
    window = SESSIONS[21 : 21 + WINDOW_SESSIONS + 10]
    decisions, outcomes = _decisions(window, n=30)
    late = ScoredDecision(
        book_id=MANAGER,
        decided_on=window[60],
        isin=isin(99),
        action=DecisionAction.BUY,
        horizon_sessions=5,
        p_beat_bench=Decimal("0.6"),
        edge_type="MOMENTUM",
    )
    late_outcome = DecisionOutcome(
        decision_key=late.key,
        book_id=MANAGER,
        isin=late.isin,
        decided_on=late.decided_on,
        resolved_on=window[65],
        sessions=5,
        name_return=Decimal("0.1"),
        bench_return=Decimal(0),
        reason=OutcomeReason.HORIZON,
    )
    inputs = ScoreboardInputs(
        s0=window[0],
        marks=_with_companions(
            (
                *_path(MANAGER, window, final_pct="5"),
                *_path(CONTROL, window, final_pct="1"),
                *_path(BENCH, window, final_pct="2"),
            ),
            window,
        ),
        decisions=(*decisions, late),
        outcomes=(*outcomes, late_outcome),
    )
    score = build_scoreboard(mini_roster(), inputs).book_scores[0]
    assert score.primary is not None
    assert score.primary.resolved_decisions == 30
    assert score.primary.secondary.buys == 1


def test_sixty_two_sessions_are_in_progress_and_sixty_three_decide() -> None:
    early = _board(sessions=WINDOW_SESSIONS - 1).book_scores[0]
    assert early.verdict is ScoreVerdict.IN_PROGRESS and early.phase is Phase.PRIMARY
    assert early.primary is not None and not early.primary.complete
    done = _board(sessions=WINDOW_SESSIONS).book_scores[0]
    assert done.primary is not None and done.primary.complete and done.primary.sessions == 63


def test_an_inconclusive_window_extends_once_to_126_sessions() -> None:
    mid = _board(sessions=100, manager="2", control="1").book_scores[0]
    assert mid.primary is not None and mid.primary.verdict is ScoreVerdict.INCONCLUSIVE
    assert mid.phase is Phase.EXTENSION and mid.verdict is ScoreVerdict.IN_PROGRESS
    final = _board(sessions=126, manager="2", control="1").book_scores[0]
    assert final.phase is Phase.FINAL and final.extension is not None
    assert final.extension.sessions == 126
    assert final.verdict is final.extension.verdict is ScoreVerdict.INCONCLUSIVE


def test_no_s0_means_nothing_counts() -> None:
    window = SESSIONS[21:90]
    inputs = ScoreboardInputs(s0=None, marks=tuple(_path(MANAGER, window, final_pct="9")))
    board = build_scoreboard(mini_roster(), inputs)
    assert board.book_scores[0].verdict is ScoreVerdict.NOT_STARTED
    assert board.k_of_n == "0 of 2 books passed"


def test_the_full_roster_reports_k_of_8_books() -> None:
    board = build_scoreboard(load_roster(), ScoreboardInputs(s0=None))
    assert board.k_of_n == "0 of 8 books passed"
    assert len(board.book_scores) == 8 and len(board.managers) == 4
    assert len(board.style_books) == 4 and not board.graduation.met


def test_a_missing_mark_or_a_duplicate_is_loud() -> None:
    window = SESSIONS[21:90]
    marks = _with_companions(
        (
            *_path(MANAGER, window, final_pct="5"),
            *_path(CONTROL, window[:-1], final_pct="1"),
            *_path(BENCH, window, final_pct="2"),
        ),
        window,
    )
    with pytest.raises(ScoreboardError, match="no mark"):
        build_scoreboard(mini_roster(), ScoreboardInputs(s0=window[0], marks=marks))
    dup = (*marks, marks[0])
    with pytest.raises(ScoreboardError, match="two marks"):
        build_scoreboard(mini_roster(), ScoreboardInputs(s0=window[0], marks=dup))


def test_secondary_metrics_amendment_1f() -> None:
    window = SESSIONS[21 : 21 + WINDOW_SESSIONS]
    specs = [
        # p, beat, base rate, regime, tier, edge, in shortlist
        ("0.9", True, "0.5", "RISK-ON", "LARGE", "MOMENTUM", True),
        ("0.7", False, "0.5", "RISK-ON", "MID", "MOMENTUM", False),
        ("0.6", True, None, "NEUTRAL", "SMALL", "EVENT", False),
    ]
    decisions: list[ScoredDecision] = []
    outcomes: list[DecisionOutcome] = []
    for i, (p, beat, base, regime, tier, edge, listed) in enumerate(specs):
        d = ScoredDecision(
            book_id=MANAGER,
            decided_on=window[i],
            isin=isin(i + 1),
            action=DecisionAction.BUY,
            horizon_sessions=10 if i else 30,
            p_beat_bench=Decimal(p),
            edge_type=edge,
            base_rate_p=None if base is None else Decimal(base),
            regime=regime,
            cap_tier=tier,
            in_shortlist=listed,
        )
        decisions.append(d)
        outcomes.append(
            DecisionOutcome(
                decision_key=d.key,
                book_id=MANAGER,
                isin=d.isin,
                decided_on=d.decided_on,
                resolved_on=window[i + 10],
                sessions=10,
                name_return=Decimal("0.05") if beat else Decimal("-0.01"),
                bench_return=Decimal("0.01"),
                reason=OutcomeReason.HORIZON,
            )
        )
    inputs = ScoreboardInputs(
        s0=window[0],
        marks=_with_companions(
            (
                *_path(MANAGER, window, final_pct="5"),
                *_path(CONTROL, window, final_pct="1"),
                *_path(BENCH, window, final_pct="2"),
            ),
            window,
        ),
        decisions=tuple(decisions),
        outcomes=tuple(outcomes),
        refusals=(
            RailRefusal(book_id=MANAGER, session=window[1], rails=("MAX_SECTOR", "MAX_POSITION")),
        ),
        calls=(
            ModelCall(
                book_id=MANAGER,
                session=window[0],
                model="claude-opus-5-5",
                tokens_in=100,
                tokens_out=20,
                cost_inr=Decimal("1.50"),
            ),
        ),
        control_buys=(
            ControlBuy(book_id=CONTROL, session=window[0], isin=isin(9), cap_tier="LARGE"),
        ),
    )
    secondary = build_scoreboard(mini_roster(), inputs).book_scores[0].primary.secondary  # type: ignore[union-attr]
    # Shrunk Brier over the two with a base rate: p' = 0.7 (hit), 0.6 (miss): (0.09 + 0.36) / 2.
    assert secondary.shrunk_brier == Decimal("0.22500000") and secondary.shrunk_brier_n == 2
    assert secondary.buy_hit_rate_p_gt_half.n == 3 and secondary.buy_hit_rate_p_gt_half.hits == 2
    assert [(g.label, g.n) for g in secondary.excess_by_regime] == [("NEUTRAL", 1), ("RISK-ON", 2)]
    risk_on = secondary.excess_by_regime[1]
    assert risk_on.mean_excess_pp == Decimal("1.0000")  # (+4 pp, -2 pp) / 2
    edges = {g.label: (g.n, g.hits) for g in secondary.hit_rate_by_edge_type}
    assert edges == {"EVENT": (1, 1), "MOMENTUM": (2, 1)}
    assert secondary.cap_tier_mix_buys == {"LARGE": 1, "MID": 1, "SMALL": 1}
    assert secondary.cap_tier_mix_control_buys == {"LARGE": 1}
    assert secondary.outside_shortlist_buys == 2 and secondary.outside_shortlist_excess.n == 2
    assert secondary.rail_refusals == {"MAX_POSITION": 1, "MAX_SECTOR": 1}
    assert (secondary.model_calls, secondary.tokens_in, secondary.model_cost_inr) == (
        1,
        100,
        Decimal("1.50"),
    )
    assert secondary.buys_horizon_in_band == 2  # 10 and 10 inside 5-20; 30 is not
    low, middle, high = secondary.confidence_terciles
    assert (low.n, middle.n, high.n) == (1, 1, 1) and high.hits == 1 and middle.hits == 0


# ── outcomes ─────────────────────────────────────────────────────────────────────────────────────


def _buy(on: date, horizon: int = 5) -> ScoredDecision:
    return ScoredDecision(
        book_id=MANAGER,
        decided_on=on,
        isin=isin(1),
        action=DecisionAction.BUY,
        horizon_sessions=horizon,
        p_beat_bench=Decimal("0.6"),
        edge_type="MOMENTUM",
    )


def test_a_decision_resolves_when_its_horizon_has_elapsed_and_not_before() -> None:
    market = drifting_market(SESSIONS, [isin(1)], drift={isin(1): Decimal("0.01")})
    bench = DictBench(compounding(SESSIONS, Decimal("0.001")))
    prices = MarketPrices(market, bench)
    d = _buy(SESSIONS[30], horizon=5)
    assert resolve_outcome(d, calendar=SESSIONS, as_of=SESSIONS[34], prices=prices) is None
    outcome = resolve_outcome(d, calendar=SESSIONS, as_of=SESSIONS[35], prices=prices)
    assert outcome is not None and outcome.resolved_on == SESSIONS[35]
    assert outcome.reason is OutcomeReason.HORIZON and outcome.sessions == 5
    assert outcome.beat, "a name compounding 1 %/day beats a bench at 0.1 %/day"
    assert outcome.name_return > outcome.bench_return > 0


def test_a_buy_exited_early_resolves_at_its_exit() -> None:
    market = drifting_market(SESSIONS, [isin(1)], drift={isin(1): Decimal("-0.01")})
    prices = MarketPrices(market, DictBench(compounding(SESSIONS, Decimal("0.001"))))
    d = _buy(SESSIONS[30], horizon=20)
    outcome = resolve_outcome(
        d, calendar=SESSIONS, as_of=SESSIONS[33], prices=prices, exited_on=SESSIONS[33]
    )
    assert outcome is not None and outcome.reason is OutcomeReason.EXITED
    assert outcome.sessions == 3 and not outcome.beat
    # A PASS is never "exited": it resolves on its horizon only.
    passed = d.model_copy(update={"action": DecisionAction.PASS})
    assert (
        resolve_outcome(
            passed, calendar=SESSIONS, as_of=SESSIONS[33], prices=prices, exited_on=SESSIONS[33]
        )
        is None
    )


def test_a_due_decision_without_a_price_is_loud() -> None:
    market = drifting_market(SESSIONS, [isin(1)], drift={isin(1): Decimal(0)})
    del market.bars[(isin(1), SESSIONS[35])]
    prices = MarketPrices(market, DictBench(compounding(SESSIONS, Decimal(0))))
    with pytest.raises(OutcomeError, match="missing"):
        resolve_outcome(_buy(SESSIONS[30]), calendar=SESSIONS, as_of=SESSIONS[40], prices=prices)


class Delisted:
    """A `DelistedNames` where ``name`` last traded on ``last`` and is delisted after it."""

    def __init__(self, market: FmMarket, name: str, last: date) -> None:
        self.market, self.name, self.last = market, name, last

    def last_traded(self, isin_: str, session: date) -> LastTraded | None:
        if isin_ != self.name or session <= self.last:
            return None
        close = self.market.close(isin_, self.last)
        assert close is not None
        return LastTraded(self.last, close, close)


def _delisting_market(last: date) -> FmMarket:
    market = drifting_market(SESSIONS, [isin(1)], drift={isin(1): Decimal("0.01")})
    for session in SESSIONS:
        if session > last:
            del market.bars[(isin(1), session)]
    return market


def test_a_name_that_delists_before_resolving_is_scored_at_its_last_traded_close() -> None:
    last = SESSIONS[32]
    market = _delisting_market(last)
    bench = DictBench(compounding(SESSIONS, Decimal("0.001")))
    prices = MarketPrices(market, bench)
    d = _buy(SESSIONS[30], horizon=5)
    with pytest.raises(OutcomeError, match="missing"):  # without the listing record: loud
        resolve_outcome(d, calendar=SESSIONS, as_of=SESSIONS[35], prices=prices)
    outcome = resolve_outcome(
        d,
        calendar=SESSIONS,
        as_of=SESSIONS[35],
        prices=prices,
        delisted=Delisted(market, isin(1), last),
    )
    assert outcome is not None
    assert outcome.resolved_on == SESSIONS[35] and outcome.name_last_traded == last
    n0, n1 = market.close(isin(1), SESSIONS[30]), market.close(isin(1), last)
    b0, b1 = bench.level(SESSIONS[30]), bench.level(SESSIONS[35])
    assert n0 is not None and n1 is not None and b0 is not None and b1 is not None
    assert outcome.name_return == (n1 / n0 - 1).quantize(Decimal("0.00000001"))
    # The bench runs to the resolution session, not to the name's last print.
    assert outcome.bench_return == (b1 / b0 - 1).quantize(Decimal("0.00000001"))
    assert DecisionOutcome.model_validate_json(outcome.record_json()) == outcome


def test_a_gap_in_a_listed_name_is_still_loud() -> None:
    market = drifting_market(SESSIONS, [isin(1)], drift={isin(1): Decimal(0)})
    del market.bars[(isin(1), SESSIONS[35])]
    prices = MarketPrices(market, DictBench(compounding(SESSIONS, Decimal(0))))
    listed = Delisted(market, isin(2), SESSIONS[0])  # some other name delisted; isin(1) listed
    with pytest.raises(OutcomeError, match="missing"):
        resolve_outcome(
            _buy(SESSIONS[30]),
            calendar=SESSIONS,
            as_of=SESSIONS[40],
            prices=prices,
            delisted=listed,
        )


def test_a_held_delisted_name_is_marked_at_its_last_traded_close(tmp_path: Path) -> None:
    last = SESSIONS[24]
    market = _delisting_market(last)
    clock = FrozenClock(SESSIONS[20])
    book, account = open_book(
        MANAGER,
        market=market,
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=ListJournal(),
    )
    clock.freeze_at(SESSIONS[20])
    book.execute(SESSIONS[20])
    book.decide(SESSIONS[20], [BookOrder(isin(1), Side.BUY, 10, "buy")])
    for session in SESSIONS[21:25]:
        clock.freeze_at(session)
        book.execute(session)
    assert account.quantities() == {isin(1): 10}
    after = SESSIONS[26]
    with pytest.raises(BookError, match="cannot be marked"):
        mark_book(book, after, execution=None)
    mark = mark_book(book, after, execution=None, delisted=Delisted(market, isin(1), last))
    close = market.close(isin(1), last)
    assert close is not None
    assert mark.nav == account.cash_value + close * 10 and mark.positions == 1


# ── the decision adapter ────────────────────────────────────────────────────────────────────────


def _decision_entry(decision: ScoredDecision, *, clock: FrozenClock) -> JournalEntry:
    is_trade = decision.action in (DecisionAction.BUY, DecisionAction.SELL, DecisionAction.TRIM)
    return JournalEntry(
        ts=clock.now(),
        trading_date=decision.decided_on,
        case_id=decision.book_id,
        actor=Actor.T2,
        decision=(
            Decision.BUY
            if decision.action is DecisionAction.BUY
            else Decision.SELL
            if is_trade
            else Decision.HOLD
        ),
        isin=decision.isin,
        sleeve=Sleeve.TACTICAL,
        rationale=f"{RATIONALE_MARKER}: scripted {decision.action.value} of {decision.isin}",
        model="claude-opus-5-5",
        tokens=TokenSpend(tokens_in=1000, tokens_out=200, cost_inr=Decimal("2.25")),
        payload=decision_payload(decision),
    )


def test_the_decision_adapter_round_trips_every_scored_field() -> None:
    decision = ScoredDecision(
        book_id=MANAGER,
        decided_on=S0,
        isin=isin(3),
        action=DecisionAction.BUY,
        horizon_sessions=12,
        p_beat_bench=Decimal("0.62"),
        target_weight=Decimal("0.08"),
        expected_excess_pct=Decimal("2.5"),
        edge_type="EARNINGS",
        base_rate_p=Decimal("0.54"),
        regime="RISK-ON",
        cap_tier="MID",
        in_shortlist=False,
        stop_pct=Decimal("7.5"),
    )
    entry = _decision_entry(decision, clock=FrozenClock(S0))
    assert entry.payload["event"] == DECISION_EVENT
    assert scored_decision_from_entry(entry) == decision
    broken = entry.model_copy(update={"payload": {**entry.payload, "p_beat_bench": "high"}})
    with pytest.raises(ScoreboardError, match="malformed"):
        scored_decision_from_entry(broken)


# ── acceptance 1: live == rebuilt from the journal, byte for byte ────────────────────────────────


class _Run:
    """One scripted M17 run of FM-SWING-BRK: its primary book (scripted decisions), its mirror
    (driven by `analyst.fundmanager.mirror`), both controls, its style book and the bench, scored
    in-process."""

    def __init__(self, tmp: Path, sessions: int = 70) -> None:
        self.roster = mini_roster(MANAGER)
        names = [isin(n) for n in range(1, 31)]
        drift = {name: Decimal(k - 15) * Decimal("0.0004") for k, name in enumerate(names, 1)}
        self.market = drifting_market(SESSIONS, names, drift=drift)
        self.bench_levels = DictBench(compounding(SESSIONS, Decimal("0.0003")))
        prices = MarketPrices(self.market, self.bench_levels)
        self.clock = FrozenClock(SESSIONS[20])
        self.journal = ListJournal()
        switch = switch_at(tmp, self.clock)
        self.manager, self.manager_account = open_book(
            MANAGER, market=self.market, clock=self.clock, kill_switch=switch, journal=self.journal
        )
        control_book, self.control_account = open_book(
            CONTROL, market=self.market, clock=self.clock, kill_switch=switch, journal=self.journal
        )
        control_mandate = self.roster.get(CONTROL)
        assert isinstance(control_mandate, ControlMandate)
        self.control = ControlBook(control_mandate, control_book, self.roster.rails)
        self.mirror, self.mirror_account = open_book(
            MIRROR, market=self.market, clock=self.clock, kill_switch=switch, journal=self.journal
        )
        self.mirror_state = MirrorState()
        mirror_control_book, _ = open_book(
            MIRROR_CONTROL,
            market=self.market,
            clock=self.clock,
            kill_switch=switch,
            journal=self.journal,
        )
        mirror_control_mandate = self.roster.get(MIRROR_CONTROL)
        assert isinstance(mirror_control_mandate, ControlMandate)
        self.mirror_control = ControlBook(
            mirror_control_mandate, mirror_control_book, self.roster.rails
        )
        style_book, _ = open_book(
            STYLE, market=self.market, clock=self.clock, kill_switch=switch, journal=self.journal
        )
        style_mandate = self.roster.get(STYLE)
        assert isinstance(style_mandate, StyleMandate)
        self.style = ControlBook(style_mandate, style_book, self.roster.rails)
        bench_mandate = self.roster.benches[0]
        self.bench = BenchBook(bench_mandate, journal=self.journal, clock=self.clock)
        self.bench.open(self.bench_levels, SESSIONS[20])

        marks: list[BookMark] = []
        decisions: list[ScoredDecision] = []
        outcomes: list[DecisionOutcome] = []
        refusals: list[RailRefusal] = []
        calls: list[ModelCall] = []
        control_buys: list[ControlBuy] = []
        exits: dict[str, date] = {}
        tiers = {
            name: ("LARGE" if i < 10 else "MID" if i < 20 else "SMALL")
            for i, name in enumerate(names)
        }
        run = SESSIONS[21 : 21 + sessions]
        for day, session in enumerate(run):
            self.clock.freeze_at(session)
            if day == 0:
                self._journal_s0(session)
            m_exec = self.manager.execute(session)
            c_exec = self.control.book.execute(session)
            others = {
                book.book_id: book.execute(session)
                for book in (self.mirror, self.mirror_control.book, self.style.book)
            }
            for fill in m_exec.fills:
                if (
                    fill.side is Side.SELL
                    and self.manager_account.quantities().get(fill.isin, 0) == 0
                ):
                    exits[fill.isin] = session

            today = self._script(day, session, names)
            orders: list[BookOrder] = []
            for d in today:
                self.journal.append(_decision_entry(d, clock=self.clock))
                decisions.append(d)
                calls.append(
                    ModelCall(
                        book_id=MANAGER,
                        session=session,
                        model="claude-opus-5-5",
                        tokens_in=1000,
                        tokens_out=200,
                        cost_inr=Decimal("2.25"),
                    )
                )
                if d.action is DecisionAction.BUY:
                    quantity = 1500 if day == 0 and d.isin == names[29] else 800
                    orders.append(BookOrder(d.isin, Side.BUY, quantity, f"{RATIONALE_MARKER} buy"))
                elif d.action is DecisionAction.SELL:
                    held = self.manager_account.quantities().get(d.isin, 0)
                    orders.append(BookOrder(d.isin, Side.SELL, held, f"{RATIONALE_MARKER} sell"))
            report = self.manager.decide(session, orders)
            # the mirror follows the primary's post-decision weights, through its own rails
            targets = target_weights(self.manager, report, session)
            plan = plan_mirror(self.mirror, targets, session, self.mirror_state, max_positions=15)
            mirror_report = self.mirror.decide(session, plan.orders)
            entry, evidence = divergence_entry(
                self.mirror,
                targets,
                settle_plan(plan, mirror_report, self.mirror),
                mirror_report,
                session,
            )
            self.journal.append(entry, evidence=evidence)
            ranked = names[day % 7 :] + names[: day % 7]
            shortlist = shortlist_of(session, list(reversed(ranked)))
            done = self.control.run(session, shortlist, tiers)
            mirror_done = self.mirror_control.run(session, shortlist, tiers)
            style_list = RankedTargets(
                trading_date=session,
                isins=tuple(names[:15]),
                source="commons_screens",
                digest_label="screens_digest",
                digest="d" * 64,
                rule="test style list",
            )
            style_done = self.style.run_ranked(session, style_list, tiers)
            for book_id, refused in (
                (MANAGER, report.refused),
                (MIRROR, mirror_report.refused),
                (CONTROL, done.report.refused),
                (MIRROR_CONTROL, mirror_done.report.refused),
                (STYLE, style_done.report.refused),
            ):
                refusals += [
                    RailRefusal(
                        book_id=book_id,
                        session=session,
                        isin=order.isin,
                        rails=tuple(r.value for r in verdict.breached_rails),
                    )
                    for order, verdict in refused
                ]
            control_buys += done.buys
            control_buys += mirror_done.buys
            marks.append(
                record_mark(mark_book(self.manager, session, execution=m_exec), self.manager)
            )
            marks.append(
                record_mark(
                    mark_book(self.control.book, session, execution=c_exec), self.control.book
                )
            )
            for book in (self.mirror, self.mirror_control.book, self.style.book):
                marks.append(
                    record_mark(mark_book(book, session, execution=others[book.book_id]), book)
                )
            marks.append(self.bench.mark(self.bench_levels, session))
            resolved = {o.decision_key for o in outcomes}
            for d in decisions:
                if d.key in resolved:
                    continue
                outcome = resolve_outcome(
                    d,
                    calendar=SESSIONS,
                    as_of=session,
                    prices=prices,
                    exited_on=exits.get(d.isin) if d.action is DecisionAction.BUY else None,
                )
                if outcome is not None:
                    entry, evidence = outcome_entry(outcome, clock=self.clock)
                    self.journal.append(entry, evidence=evidence)
                    outcomes.append(outcome)
        self.last = run[-1]
        self.live_inputs = ScoreboardInputs(
            s0=run[0],
            marks=tuple(marks),
            decisions=tuple(decisions),
            outcomes=tuple(outcomes),
            refusals=tuple(refusals),
            calls=tuple(calls),
            control_buys=tuple(control_buys),
        )
        self.live = build_scoreboard(self.roster, self.live_inputs)

    def _journal_s0(self, session: date) -> None:
        for book in self.roster.books:
            evidence = EvidenceBundle(
                case_id=book.id,
                trading_date=session,
                actor=Actor.SYSTEM,
                items=(
                    EvidenceItem(
                        kind=EvidenceKind.POLICY,
                        source="roster",
                        label="mandate_hash",
                        text="x" * 64,
                    ),
                ),
            )
            self.journal.append(
                JournalEntry(
                    ts=self.clock.now(),
                    trading_date=session,
                    case_id=book.id,
                    actor=Actor.SYSTEM,
                    decision=Decision.HEARTBEAT,
                    evidence_snapshot_ref=evidence.ref().ref,
                    payload={"event": S0_EVENT, "mandate_hash": "x" * 64},
                ),
                evidence=evidence,
            )

    @staticmethod
    def _script(day: int, session: date, names: list[str]) -> list[ScoredDecision]:
        """A deterministic stand-in for the manager: five buys on S0, one sell, a daily PASS."""

        def make(
            name: str, action: DecisionAction, p: str, horizon: int, **extra: Any
        ) -> ScoredDecision:
            return ScoredDecision(
                book_id=MANAGER,
                decided_on=session,
                isin=name,
                action=action,
                horizon_sessions=horizon,
                p_beat_bench=Decimal(p),
                edge_type=extra.pop("edge_type", "MOMENTUM"),
                **extra,
            )

        out: list[ScoredDecision] = []
        if day == 0:
            for k, name in enumerate(names[25:]):
                out.append(
                    make(
                        name,
                        DecisionAction.BUY,
                        "0.8",
                        20 if k == 0 else 10,
                        base_rate_p=Decimal("0.55"),
                        regime="RISK-ON",
                        cap_tier="SMALL",
                        in_shortlist=k % 2 == 0,
                        target_weight=Decimal("0.08"),
                    )
                )
        if day == 15:
            out.append(make(names[25], DecisionAction.SELL, "0.3", 5, edge_type="NONE"))
        watched = names[day % 25]
        out.append(
            make(watched, DecisionAction.PASS, "0.7" if day % 2 else "0.3", 5, regime="NEUTRAL")
        )
        return out


@pytest.fixture(scope="module")
def m17_run(tmp_path_factory: pytest.TempPathFactory) -> _Run:
    return _Run(tmp_path_factory.mktemp("m17run"))


def _persisted(entries: Sequence[JournalEntry]) -> list[JournalEntry]:
    """The entries as a store hands them back: serialised and re-validated, nothing shared."""
    return [JournalEntry.model_validate_json(e.model_dump_json()) for e in entries]


def test_the_scoreboard_rebuilt_from_the_journal_equals_the_live_one_byte_for_byte(
    m17_run: _Run,
) -> None:
    rebuilt_inputs = inputs_from_journal(_persisted(m17_run.journal.entries), m17_run.roster)
    rebuilt = build_scoreboard(m17_run.roster, rebuilt_inputs)
    assert rebuilt.canonical_bytes() == m17_run.live.canonical_bytes()
    assert rebuilt.digest() == m17_run.live.digest()

    # The run is substantive, not an empty board that trivially matches.
    score, mirror = m17_run.live.book_scores
    assert (score.book_id, mirror.book_id) == (MANAGER, MIRROR)
    # Brier is the manager's, once: both books read the same figure from the same decisions
    assert mirror.primary is not None and score.primary is not None
    assert mirror.primary.brier == score.primary.brier is not None
    assert mirror.primary.resolved_decisions == score.primary.resolved_decisions
    (result,) = m17_run.live.managers
    assert result.decisions == len(m17_run.live_inputs.decisions)
    assert mirror.primary.secondary.manager_turnover_x > 0, "the mirror traded"
    assert score.primary is not None and score.primary.complete
    assert score.verdict is not ScoreVerdict.IN_PROGRESS or score.phase is Phase.EXTENSION
    assert score.primary.resolved_decisions >= MIN_RESOLVED
    secondary = score.primary.secondary
    assert secondary.rail_refusals.get("MAX_POSITION") == 1, "the oversized S0 buy was refused"
    assert secondary.cap_tier_mix_control_buys and secondary.control_turnover_x > 0
    assert secondary.manager_cost_drag_bp > 0 and secondary.model_calls > 0
    assert m17_run.live_inputs.outcomes and any(
        o.reason is OutcomeReason.EXITED for o in m17_run.live_inputs.outcomes
    )


def test_every_input_reads_back_out_of_the_journal_exactly(m17_run: _Run) -> None:
    rebuilt = inputs_from_journal(_persisted(m17_run.journal.entries), m17_run.roster)
    live = m17_run.live_inputs
    assert rebuilt.s0 == live.s0
    assert rebuilt.marks == live.marks
    assert rebuilt.decisions == live.decisions
    assert rebuilt.outcomes == live.outcomes
    assert sorted(rebuilt.refusals, key=repr) == sorted(live.refusals, key=repr)
    assert rebuilt.calls == live.calls
    assert rebuilt.control_buys == live.control_buys


def test_a_tampered_journal_changes_the_scoreboard(m17_run: _Run) -> None:
    """The equality above is not vacuous: one flipped outcome moves the bytes."""
    entries = _persisted(m17_run.journal.entries)
    for i, entry in enumerate(entries):
        if entry.payload.get("event") == "DECISION_OUTCOME":
            record = DecisionOutcome.model_validate_json(entry.payload["record"])
            flipped = record.model_copy(
                update={
                    "name_return": record.bench_return - (record.name_return - record.bench_return)
                }
            )
            entries[i] = entry.model_copy(
                update={"payload": {**entry.payload, "record": flipped.record_json()}}
            )
            break
    tampered = build_scoreboard(m17_run.roster, inputs_from_journal(entries, m17_run.roster))
    assert tampered.canonical_bytes() != m17_run.live.canonical_bytes()


def test_the_last_marks_agree_with_the_persisted_books(m17_run: _Run, tmp_path: Path) -> None:
    """The books leg: each account restored from its persisted document marks as journaled."""
    last = m17_run.last
    by_key = {(m.book_id, m.session): m for m in m17_run.live_inputs.marks}
    for book, account in (
        (m17_run.manager, m17_run.manager_account),
        (m17_run.control.book, m17_run.control_account),
    ):
        restored = M17PaperAccount.restore(
            account.to_document(),
            account_id=account.account_id,
            market=m17_run.market,
            kill_switch=switch_at(tmp_path, m17_run.clock),
            clock=m17_run.clock,
            schedule=load_repo_rate_schedule(),
            corporate_actions=BookActionCalendar(),
            circuit=NoCircuitData(),
            book_digest=account.book_digest,
            alerter=RecordingAlerter(),
        )
        again = mark_book(replace_account(book, restored), last, execution=None)
        journaled = by_key[(book.book_id, last)]
        assert (again.nav, again.cash, again.positions) == (
            journaled.nav,
            journaled.cash,
            journaled.positions,
        )


def replace_account(book: FundBook, account: M17PaperAccount) -> FundBook:
    return FundBook(
        book_id=book.book_id,
        rails=book.rails,
        account=account,
        market=book.market,
        journal=ListJournal(),
        kill_switch=book.kill_switch,
        clock=book.clock,
    )


# ── GET /status/managers and the digest ─────────────────────────────────────────────────────────


def _client(entries: Sequence[JournalEntry], roster: Roster, clock: FrozenClock) -> TestClient:
    app.dependency_overrides[m17_journal_source] = lambda: list(entries)
    app.dependency_overrides[m17_roster_source] = lambda: roster
    app.dependency_overrides[clock_source] = lambda: clock
    return TestClient(app)


@pytest.fixture
def _clean_overrides() -> Any:
    yield
    app.dependency_overrides.clear()


@pytest.mark.usefixtures("_clean_overrides")
def test_status_managers_serves_books_decisions_and_the_scoreboard(m17_run: _Run) -> None:
    client = _client(_persisted(m17_run.journal.entries), m17_run.roster, m17_run.clock)
    response = client.get("/status/managers")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scoreboard_digest"] == m17_run.live.digest()
    assert body["scoreboard_error"] is None
    assert body["k_of_n"] == m17_run.live.k_of_n
    assert {b["book_id"] for b in body["books"]} == {
        MANAGER,
        CONTROL,
        MIRROR,
        MIRROR_CONTROL,
        STYLE,
        BENCH,
    }
    assert body["decisions_session"] == m17_run.last.isoformat()
    assert [m["book_id"] for m in body["managers"]] == [MANAGER, MIRROR]
    assert {m["manager_id"] for m in body["managers"]} == {"FM-SWING-BRK"}
    assert [r["manager_id"] for r in body["manager_results"]] == ["FM-SWING-BRK"]
    assert [b["style_id"] for b in body["style_books"]] == [STYLE]
    assert body["graduation"]["primary_passes_needed"] == 2
    pass_line = [d for d in body["decisions"] if d["event"] == DECISION_EVENT]
    assert pass_line and pass_line[0]["action"] == "PASS" and pass_line[0]["p_beat_bench"]
    # What it never carries: a rationale, a prompt, or a credential-shaped field.
    text = response.text
    assert RATIONALE_MARKER not in text
    for forbidden in ("rationale", "prompt", "password", "secret", "api_key", "dsn"):
        assert forbidden not in text.lower(), forbidden


@pytest.mark.usefixtures("_clean_overrides")
def test_status_managers_reports_an_unscoreable_journal_instead_of_guessing(m17_run: _Run) -> None:
    entries = [
        e
        for e in _persisted(m17_run.journal.entries)
        if not (
            e.case_id == BENCH
            and e.payload.get("event") == "BOOK_MARK"
            and e.trading_date == SESSIONS[26]  # inside the primary window
        )
    ]
    body = _client(entries, m17_run.roster, m17_run.clock).get("/status/managers").json()
    assert body["scoreboard_digest"] is None and "no mark" in body["scoreboard_error"]
    assert body["managers"] == []


def test_status_managers_is_a_declared_get_route() -> None:
    from fastapi.routing import APIRoute

    (route,) = [r for r in app.routes if isinstance(r, APIRoute) and r.path == "/status/managers"]
    assert route.methods == {"GET"} and route.response_model is not None


def test_the_daily_digest_is_written_under_the_injected_directory(
    m17_run: _Run, tmp_path: Path
) -> None:
    entries = _persisted(m17_run.journal.entries)
    session, lines = todays_decisions(entries, m17_run.roster)
    assert session == m17_run.last
    target = tmp_path / "campaign" / "m17"
    path = write_digest(m17_run.live, session, lines, directory=target)
    assert path == digest_path(target, session) == target / f"digest-{session.isoformat()}.md"
    text = path.read_text(encoding="utf-8")
    assert text == render_digest(m17_run.live, session, lines)
    assert m17_run.live.k_of_n in text and MANAGER in text and BENCH in text
    assert MIRROR in text and STYLE in text and "Graduation floor" in text
    assert RATIONALE_MARKER not in text
    assert not list(target.glob(".*.tmp")), "no half-written page is left behind"


def test_the_digest_default_directory_is_campaign_m17() -> None:
    from analyst.fundmanager.digest import DIGEST_DIR

    assert Path.home() / "campaign" / "m17" == DIGEST_DIR


def test_managers_and_controls_in_the_mini_roster_are_typed() -> None:
    roster = mini_roster()
    assert isinstance(roster.get(MANAGER), ManagerMandate)
    assert isinstance(roster.get(CONTROL), ControlMandate)
