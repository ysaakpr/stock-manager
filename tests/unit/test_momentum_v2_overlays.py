"""M16.1 — momentum v2's absolute-momentum (A1), residual-ranking (A3), profitability (A4) switches.

All three are off by default. The tests here pin, for each: that it is off in D13 and absent from
D13's run fingerprint; the rule itself, each with an inverted twin that fails if the rule is
flipped; that its input goes through the PIT guard (a future-dated repo rate, filing or price is
refused, never used); and that the L1 seam serves exactly what the policy asks for. The replay
section pins that the switches-off policy reproduces the pre-M16.1 journal, book and rails
byte-for-byte — four digests struck at origin/main 523882e, before any M16.1 code existed.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from analyst.journal.models import Decision
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.cash_interest import HAIRCUT, load_repo_rate_schedule
from backtest.policies.momentum_v2 import (
    D13_ABS_MOM,
    D13_PROFIT_FILTER,
    D13_RESID_MOM,
    PAPER_RATIFIED_2026_09_06,
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
)
from backtest.policies.momentum_v2_overlays import (
    ABSOLUTE_MOMENTUM_HORIZON_DAYS,
    PROFITABILITY_MAX_STALENESS_DAYS,
    ProfitabilityReading,
    RepoRateReading,
    ResidualScore,
    residual_scores,
)
from backtest.policies.residual_momentum import MarketSession, ResidualMomentumPanel
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap
from backtest.replay import ReplayEngine, ReplayResult, SessionDecision
from backtest.run import (
    _LOOKBACK_DAYS,
    _MONTH_DAYS,
    BacktestError,
    _AccountingBroker,
    _L1MomentumV2Data,
    _L1Profitability,
    _PitFact,
    _V2OverlayInputs,
)
from dataplatform.clock import FrozenClock
from dataplatform.ingest.xbrl import Nature
from dataplatform.query.pit import Dataset, PitError
from execution.broker import Exchange, Holding, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SimBroker
from tests.rails_support import marks_from
from tests.unit.test_buy_sizing_ceiling import NAMES, PRICE, RAILS, _Market
from tests.unit.test_momentum_v2_daily_regime import (
    _OPENING,
    _REBALANCES,
    _SESSIONS,
    _Broker,
    _ctx,
    _held,
    _replay,
    _ScriptedData,
)

D13 = PAPER_RATIFIED_2026_09_06
REBALANCE = date(2024, 1, 1)
NEXT = date(2024, 1, 2)
A, B, C, D = NAMES[0], NAMES[1], NAMES[2], NAMES[3]
#: 6.50 % repo: the cash hurdle over the 12-1 span is (1.06)^(335/365) - 1, about 5.49 %.
REPO = Decimal("0.065")


def _params(**overrides: object) -> MomentumV2Parameters:
    """A top-2 book banded to 3, no regime filter: only the overlay under test acts."""
    fields = {"top_n": 2, "sell_band": 3, "use_12_1": True, **overrides}
    return MomentumV2Parameters(**fields)  # type: ignore[arg-type]


class _Data:
    """One rebalance, scripted candidates, and every overlay seam — each optionally future-dated."""

    def __init__(
        self,
        momenta: dict[str, str],
        *,
        repo: Decimal = REPO,
        residual: dict[str, str | None] | None = None,
        profits: dict[str, tuple[str | None, int]] | None = None,
        leak: str | None = None,
    ) -> None:
        self.momenta = momenta
        self.repo = repo
        self.residual = residual or {}
        self.profits = profits or {}
        self.leak = leak

    def _when(self, as_of: date, seam: str) -> date:
        return as_of + timedelta(days=1) if self.leak == seam else as_of

    def is_rebalance(self, session: date) -> bool:
        return session == REBALANCE

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        records = tuple(
            MomentumV2Record(
                isin=isin,
                momentum_0_12=Decimal("0.9"),  # A1 must read the 12-1 return, never this
                momentum_12_1=Decimal(momentum),
                price=PRICE,
                volatility=Decimal("0.2"),
                knowable_date=self._when(as_of, "signal"),
            )
            for isin, momentum in self.momenta.items()
        )
        return Dataset.declaring(f"m@{as_of}", records, knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        reading = RegimeReading(
            index_level=Decimal("105"), moving_average=Decimal("100"), knowable_date=as_of
        )
        return Dataset.declaring(f"r@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)

    def repo_rate(self, as_of: date) -> Dataset[RepoRateReading]:
        effective = self._when(as_of, "repo") if self.leak == "repo" else date(2023, 2, 8)
        reading = RepoRateReading(self.repo, effective, effective)
        return Dataset.declaring(
            f"repo@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date
        )

    def residual_momentum(self, as_of: date) -> Dataset[ResidualScore]:
        late = self.leak == "residual"
        when = as_of + timedelta(days=1) if late else as_of - timedelta(days=1)
        scores = tuple(
            ResidualScore(isin, None if s is None else Decimal(s), when)
            for isin, s in self.residual.items()
        )
        return Dataset.declaring(f"res@{as_of}", scores, knowable_date=lambda r: r.knowable_date)

    def profitability(self, as_of: date) -> Dataset[ProfitabilityReading]:
        readings = tuple(
            ProfitabilityReading(
                isin,
                None if ttm is None else Decimal(ttm),
                as_of - timedelta(days=age) if self.leak != "filing" else as_of + timedelta(days=1),
            )
            for isin, (ttm, age) in self.profits.items()
        )
        return Dataset.declaring(f"pat@{as_of}", readings, knowable_date=lambda r: r.knowable_date)


def _decide(
    params: MomentumV2Parameters, data: _Data, broker: _Broker | None = None
) -> SessionDecision:
    return MomentumV2Policy(data, params).decide(_ctx(REBALANCE, broker or _Broker()))


def _bought(decision: SessionDecision) -> set[str]:
    return {o.isin for o in decision.orders if o.side is Side.BUY}


def _sold(decision: SessionDecision) -> set[str]:
    return {o.isin for o in decision.orders if o.side is Side.SELL}


# ── parameters and fingerprint ───────────────────────────────────────────────────────────────────


def test_the_overlays_are_off_by_default_and_d13_leaves_them_off() -> None:
    for params in (MomentumV2Parameters(), D13):
        assert not params.absolute_momentum
        assert not params.residual_ranking
        assert not params.profitability_filter


def test_d13s_repr_and_so_its_run_fingerprint_is_unchanged_with_the_overlays_explicitly_off() -> (
    None
):
    # The string D13 rendered before M16.1 (and before M14.5) — the run identity
    # (backtest.run_ledger). If it moves, every persisted D13 run is orphaned.
    before = (
        "MomentumV2Parameters(top_n=20, use_12_1=True, sell_band=30, regime_filter=True, "
        "vol_scaled=True, redeploy_next_session=True, vol_target_annual=None, "
        "assumed_correlation=Decimal('0.3'), regime_ma_days=200, "
        "buy_budget_fraction=Decimal('0.98'), sleeve=<Sleeve.TACTICAL: 'TACTICAL'>, "
        "parking_sleeve=<Sleeve.CASH: 'CASH'>)"
    )
    explicit_off = replace(
        D13, absolute_momentum=False, residual_ranking=False, profitability_filter=False
    )
    assert repr(D13) == before
    assert repr(explicit_off) == before


def test_each_preset_is_d13_with_exactly_its_one_switch_and_its_own_fingerprint() -> None:
    assert replace(D13, absolute_momentum=True) == D13_ABS_MOM
    assert replace(D13, residual_ranking=True) == D13_RESID_MOM
    assert replace(D13, profitability_filter=True) == D13_PROFIT_FILTER
    renders = {repr(p) for p in (D13, D13_ABS_MOM, D13_RESID_MOM, D13_PROFIT_FILTER)}
    assert len(renders) == 4
    assert repr(D13_ABS_MOM).endswith(", absolute_momentum=True)")
    assert repr(D13_RESID_MOM).endswith(", residual_ranking=True)")
    assert repr(D13_PROFIT_FILTER).endswith(", profitability_filter=True)")


class _NoOverlays:
    """A source with only the D13 surface — what the paper job hands the policy."""

    def is_rebalance(self, session: date) -> bool:
        return False

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        return Dataset.declaring("m", (), knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        return Dataset.declaring("r", (), knowable_date=lambda r: r.knowable_date)


@pytest.mark.parametrize(
    ("preset", "seam"),
    [
        (D13_ABS_MOM, "AbsoluteMomentumData"),
        (D13_RESID_MOM, "ResidualRankingData"),
        (D13_PROFIT_FILTER, "ProfitabilityData"),
    ],
)
def test_a_switch_on_against_a_source_without_its_seam_is_refused_up_front(
    preset: MomentumV2Parameters, seam: str
) -> None:
    with pytest.raises(TypeError, match=seam):
        MomentumV2Policy(_NoOverlays(), preset)
    MomentumV2Policy(_NoOverlays(), D13)  # D13 asks for nothing new


# ── A1: absolute momentum ────────────────────────────────────────────────────────────────────────


def test_the_hurdle_is_repo_less_the_haircut_compounded_over_the_12_1_span() -> None:
    assert ABSOLUTE_MOMENTUM_HORIZON_DAYS == _LOOKBACK_DAYS - _MONTH_DAYS  # the L1 source's span
    reading = RepoRateReading(REPO, date(2023, 2, 8), date(2023, 2, 8))
    expected = (1 + float(REPO - HAIRCUT)) ** (335 / 365) - 1
    assert math.isclose(float(reading.hurdle), expected, abs_tol=1e-8)
    assert Decimal("0.054") < reading.hurdle < Decimal("0.055")
    higher = RepoRateReading(Decimal("0.08"), date(2023, 2, 8), date(2023, 2, 8))
    assert higher.hurdle > reading.hurdle
    with pytest.raises(TypeError):
        RepoRateReading(0.065, date(2023, 2, 8), date(2023, 2, 8))  # type: ignore[arg-type]


#: A ranks first and clears the hurdle; B ranks second at +3 %, under it; C would be next.
_A1_MOMENTA = {A: "0.5", B: "0.03", C: "0.01"}


def test_a1_leaves_a_failing_slot_in_cash_without_backfilling() -> None:
    decision = _decide(_params(absolute_momentum=True), _Data(_A1_MOMENTA))
    assert _bought(decision) == {A}  # neither B (fails) nor C (would be the backfill)
    (buy,) = decision.orders
    # A's slot is half the book: with B's slot in cash, A is not sized to the whole budget.
    assert buy.quantity * PRICE <= Decimal("50000")
    assert buy.quantity * PRICE > Decimal("40000")


def test_without_a1_the_failing_name_is_bought_inversion() -> None:
    decision = _decide(_params(), _Data(_A1_MOMENTA))
    assert _bought(decision) == {A, B}
    assert sum(o.quantity * PRICE for o in decision.orders) > Decimal("90000")


def test_a1_never_backfills_with_a_passing_name_below_the_basket() -> None:
    # Ranked on 0-12 (+90 % for every name, so ISIN order): A, B chosen, C next. B fails the hurdle
    # on its 12-1 return; C (+10 %) clears it — and must still not be promoted into B's slot.
    data = _Data({A: "0.5", B: "0.03", C: "0.10"})
    decision = _decide(_params(absolute_momentum=True, use_12_1=False), data)
    assert _bought(decision) == {A}
    assert _bought(_decide(_params(use_12_1=False), data)) == {A, B}  # the slot exists without A1


def test_a1_under_a_vol_target_scales_both_caps_together() -> None:
    # One passing name at 0.2 monthly vol is ~69 % a year; a 34.64 % target halves the basket,
    # and A1's half-book slot halves it again: A is sized to about a quarter of the book.
    decision = _decide(
        _params(absolute_momentum=True, vol_target_annual=Decimal("0.3464")), _Data(_A1_MOMENTA)
    )
    (buy,) = decision.orders
    assert buy.isin == A
    assert Decimal("20000") < buy.quantity * PRICE <= Decimal("25000")


def test_a1_with_every_name_above_the_hurdle_buys_the_whole_basket() -> None:
    momenta = {A: "0.5", B: "0.3", C: "0.01"}
    with_a1 = _decide(_params(absolute_momentum=True), _Data(momenta))
    without = _decide(_params(), _Data(momenta))
    assert with_a1.orders == without.orders


def test_a1_is_strict_a_return_equal_to_the_hurdle_does_not_hold() -> None:
    hurdle = RepoRateReading(REPO, date(2023, 2, 8), date(2023, 2, 8)).hurdle
    decision = _decide(_params(absolute_momentum=True), _Data({A: "0.5", B: str(hurdle)}))
    assert _bought(decision) == {A}
    above = hurdle + Decimal("0.00000001")
    assert _bought(_decide(_params(absolute_momentum=True), _Data({A: "0.5", B: str(above)}))) == {
        A,
        B,
    }


def test_a1_reads_the_12_1_return_even_when_ranking_on_0_12() -> None:
    # Every record's 0-12 return is +90 %; B's 12-1 return is +3 %. A1 must still refuse B.
    decision = _decide(_params(use_12_1=False, absolute_momentum=True), _Data(_A1_MOMENTA))
    assert B not in _bought(decision)


def test_a1_sells_a_held_failing_name_even_inside_the_sell_band() -> None:
    broker = _Broker(holdings=_held(B))
    assert B in _sold(_decide(_params(absolute_momentum=True), _Data(_A1_MOMENTA), broker))


def test_without_a1_a_held_name_inside_the_band_is_kept_inversion() -> None:
    broker = _Broker(holdings=_held(B))
    assert B not in _sold(_decide(_params(), _Data(_A1_MOMENTA), broker))


def test_a_higher_repo_rate_raises_the_bar() -> None:
    momenta = {A: "0.5", B: "0.06"}
    assert B in _bought(_decide(_params(absolute_momentum=True), _Data(momenta)))
    high = _Data(momenta, repo=Decimal("0.075"))  # hurdle ~6.4 %
    assert B not in _bought(_decide(_params(absolute_momentum=True), high))


def test_a1_evidence_names_the_held_failing_names_it_asks_to_sell() -> None:
    broker = _Broker(holdings=_held(B, D))  # B: in the band, fails the hurdle; D: outside the band
    decision = _decide(_params(absolute_momentum=True), _Data(_A1_MOMENTA), broker)
    (item,) = [i for i in decision.evidence.items if i.label == "absolute_momentum_hurdle"]
    assert item.detail["selling"] == B  # D is sold by the band, not by A1
    assert "intended" in (item.text or "")


def test_a1_files_the_hurdle_and_the_cash_slots_as_evidence() -> None:
    decision = _decide(_params(absolute_momentum=True), _Data(_A1_MOMENTA))
    (item,) = [i for i in decision.evidence.items if i.label == "absolute_momentum_hurdle"]
    assert item.detail["in_cash"] == B
    assert item.detail["slots_in_cash"] == "1"
    assert item.detail["repo_rate"] == str(REPO)


def test_a1_redeploy_never_fills_the_cash_slots() -> None:
    params = _params(absolute_momentum=True, redeploy_next_session=True)
    data = _Data(_A1_MOMENTA)
    policy = MomentumV2Policy(data, params)
    sold = policy.decide(_ctx(REBALANCE, _Broker(holdings=_held(D))))  # D left the band: sold
    assert D in _sold(sold)
    pending = policy.pending
    assert pending is not None and set(pending) == {A}
    assert sum(pending.values()) < 1  # B's slot is cash, and stays cash tomorrow
    # Next session: ₹50,000 free and A held at ₹50,000 — A already fills its half-book slot.
    half = Holding(isin=A, exchange=Exchange.NSE, quantity=500, average_price=PRICE)
    nxt = policy.decide(_ctx(NEXT, _Broker(cash="50000", holdings=(half,))))
    assert not nxt.orders
    # With A short of its slot the redeploy buys A — only up to half the book.
    short = Holding(isin=A, exchange=Exchange.NSE, quantity=200, average_price=PRICE)
    policy.resume(pending)
    topped = policy.decide(_ctx(NEXT, _Broker(cash="80000", holdings=(short,))))
    assert _bought(topped) == {A}
    (buy,) = topped.orders
    assert (buy.quantity + 200) * PRICE <= Decimal("50000")  # half of ₹1,00,000 capital


def test_without_a1_the_redeploy_spends_the_freed_cash_inversion() -> None:
    params = _params(redeploy_next_session=True)
    policy = MomentumV2Policy(_Data(_A1_MOMENTA), params)
    policy.decide(_ctx(REBALANCE, _Broker(holdings=_held(D))))
    assert policy.pending is not None and sum(policy.pending.values()) == 1
    assert policy.decide(_ctx(NEXT, _Broker(cash="100000"))).orders


def test_a_future_dated_repo_rate_trips_the_guard() -> None:
    with pytest.raises(PitError):
        _decide(_params(absolute_momentum=True), _Data(_A1_MOMENTA, leak="repo"))


def test_the_l1_repo_seam_serves_the_rate_in_force_and_never_the_next_one() -> None:
    schedule = load_repo_rate_schedule()
    source = _L1MomentumV2Data.__new__(_L1MomentumV2Data)
    source._repo_rates = schedule
    change = schedule.changes[-1]
    previous = schedule.changes[-2]
    (eve,) = source.repo_rate(change.effective_from - timedelta(days=1)).records
    (day,) = source.repo_rate(change.effective_from).records
    assert eve.repo_rate == previous.repo_rate and eve.knowable_date == previous.effective_from
    assert day.repo_rate == change.repo_rate and day.knowable_date == change.effective_from


# ── A3: residual-momentum ranking ────────────────────────────────────────────────────────────────

#: 12-1 says A > B > C; the residual score says C > B > A.
_A3_MOMENTA = {A: "0.5", B: "0.4", C: "0.3"}
_A3_RESIDUAL: dict[str, str | None] = {A: "-1.0", B: "0.5", C: "2.0"}


def test_a3_ranks_on_the_residual_score() -> None:
    data = _Data(_A3_MOMENTA, residual=_A3_RESIDUAL)
    decision = _decide(_params(top_n=1, sell_band=1, residual_ranking=True), data)
    assert _bought(decision) == {C}
    (table,) = [i for i in decision.evidence.items if i.label == "residual_momentum"]
    assert table.isin == C and table.value == Decimal("2.0")
    assert "residual momentum +2.0 (H1)" in (decision.entries[0].rationale or "")


def test_without_a3_the_12_1_leader_is_bought_inversion() -> None:
    data = _Data(_A3_MOMENTA, residual=_A3_RESIDUAL)
    assert _bought(_decide(_params(top_n=1, sell_band=1), data)) == {A}


def test_a3_leaves_out_a_name_the_panel_excluded() -> None:
    data = _Data(_A3_MOMENTA, residual={A: None, B: "0.5", C: "0.4"})
    decision = _decide(_params(top_n=1, sell_band=1, residual_ranking=True), data)
    assert _bought(decision) == {B}
    held_c = _decide(
        _params(top_n=1, sell_band=1, residual_ranking=True),
        _Data(_A3_MOMENTA, residual={A: "1.0", B: "0.5"}),  # C has no score at all
        _Broker(holdings=_held(C)),
    )
    assert C in _sold(held_c)  # an unrankable holding is outside every band


def test_a3_never_zero_fills_an_excluded_score() -> None:
    # Every scored name is negative: a None read as 0 would put A first.
    data = _Data(_A3_MOMENTA, residual={A: None, B: "-0.5", C: "-1.0"})
    decision = _decide(_params(top_n=1, sell_band=1, residual_ranking=True), data)
    assert _bought(decision) == {B}
    assert residual_scores(
        [ResidualScore(A, None, REBALANCE), ResidualScore(B, Decimal("-0.5"), REBALANCE)]
    ) == {B: Decimal("-0.5")}


def test_a_future_dated_residual_score_trips_the_guard() -> None:
    data = _Data(_A3_MOMENTA, residual=_A3_RESIDUAL, leak="residual")
    with pytest.raises(PitError):
        _decide(_params(residual_ranking=True), data)


def _market(sessions: list[date]) -> list[MarketSession]:
    level = Decimal("100")
    out = []
    for n, session in enumerate(sessions):
        level = level * (Decimal("1.001") if n % 3 else Decimal("0.998"))
        out.append(MarketSession(session, level.quantize(Decimal("0.0001")), session))
    return out


def test_the_l1_residual_seam_is_the_h1_panel_on_the_signal_closes() -> None:
    sessions = [date(2022, 1, 3) + timedelta(days=n) for n in range(400)]
    panel = ResidualMomentumPanel(_market(sessions))
    as_of = sessions[300]
    trend = {s: Decimal("50") * Decimal("1.002") ** n for n, s in enumerate(sessions)}
    flat = {s: Decimal("50") + Decimal(n % 2) / 10 for n, s in enumerate(sessions)}

    def closes(session: date) -> dict[str, Decimal]:
        return {A: trend[session], B: flat[session]}

    source = _L1MomentumV2Data.__new__(_L1MomentumV2Data)
    source._residual_panel = panel
    source._signal_closes = closes
    source._calendar = frozenset()
    source._signals = {
        as_of: tuple(
            MomentumV2Record(
                A if i == 0 else B, Decimal(0), Decimal(0), PRICE, Decimal("0.2"), as_of
            )
            for i in range(2)
        )
    }
    by_isin = {s.isin: s for s in source.residual_momentum(as_of).records}
    window = panel.window(as_of)
    for isin, path in ((A, trend), (B, flat)):
        direct = panel.score(as_of, {s: float(path[s]) for s in window})
        assert by_isin[isin].score == direct  # the panel's score, not a re-definition
        assert by_isin[isin].knowable_date == window[-1] < as_of
    assert by_isin[A].score is not None and by_isin[A].score > by_isin[B].score  # type: ignore[operator]

    # A close on the decision session itself (or later) never reaches the score.
    trend[as_of] = Decimal("1")
    trend[sessions[301]] = Decimal("1")
    again = {s.isin: s for s in source.residual_momentum(as_of).records}
    assert again[A].score == by_isin[A].score


def test_the_l1_seams_refuse_to_answer_without_their_input() -> None:
    source = _L1MomentumV2Data.__new__(_L1MomentumV2Data)
    source._repo_rates = None
    source._residual_panel = None
    source._profitability = None
    for call in (source.repo_rate, source.residual_momentum, source.profitability):
        with pytest.raises(BacktestError):
            call(date(2024, 1, 1))


def test_a_run_with_every_overlay_off_loads_no_overlay_input() -> None:
    inputs = _V2OverlayInputs.for_parameters(D13, through=date(2024, 1, 1), data_root=None)
    assert inputs == _V2OverlayInputs()
    with_a1 = _V2OverlayInputs.for_parameters(D13_ABS_MOM, through=date(2024, 1, 1), data_root=None)
    assert with_a1.repo_rates is not None and with_a1.profitability is None


# ── A4: profitability filter ─────────────────────────────────────────────────────────────────────

_A4_MOMENTA = {A: "0.5", B: "0.4", C: "0.3", D: "0.2"}
#: A: a loss; B: profitable but stale; C: profitable and fresh; D: no reading at all.
_A4_PROFITS: dict[str, tuple[str | None, int]] = {
    A: ("-10", 30),
    B: ("10", PROFITABILITY_MAX_STALENESS_DAYS + 1),
    C: ("10", 30),
}


def test_a4_keeps_only_fresh_profitable_names() -> None:
    data = _Data(_A4_MOMENTA, profits=_A4_PROFITS)
    decision = _decide(_params(top_n=3, sell_band=4, profitability_filter=True), data)
    assert _bought(decision) == {C}
    (item,) = [i for i in decision.evidence.items if i.label == "profitability_eligible"]
    assert item.value == Decimal(1) and item.detail["offered"] == "4"


def test_without_a4_the_unprofitable_names_are_bought_inversion() -> None:
    data = _Data(_A4_MOMENTA, profits=_A4_PROFITS)
    assert _bought(_decide(_params(top_n=3, sell_band=4), data)) == {A, B, C}


@pytest.mark.parametrize(
    ("ttm", "age", "eligible"),
    [
        ("0.01", PROFITABILITY_MAX_STALENESS_DAYS, True),
        ("0.01", PROFITABILITY_MAX_STALENESS_DAYS + 1, False),
        ("0", 1, False),
        ("-0.01", 1, False),
        (None, 1, False),  # fewer than four consecutive quarters
    ],
)
def test_a4_boundaries(ttm: str | None, age: int, eligible: bool) -> None:
    as_of = date(2024, 1, 1)
    reading = ProfitabilityReading(
        A, None if ttm is None else Decimal(ttm), as_of - timedelta(days=age)
    )
    assert reading.eligible(as_of) is eligible


def test_a4_sells_a_held_name_that_turned_unprofitable() -> None:
    data = _Data(_A4_MOMENTA, profits=_A4_PROFITS)
    broker = _Broker(holdings=_held(A))
    assert A in _sold(
        _decide(_params(top_n=3, sell_band=4, profitability_filter=True), data, broker)
    )
    assert A not in _sold(_decide(_params(top_n=3, sell_band=4), data, broker))


def test_a_future_dated_filing_trips_the_guard() -> None:
    data = _Data(_A4_MOMENTA, profits=_A4_PROFITS, leak="filing")
    with pytest.raises(PitError):
        _decide(_params(profitability_filter=True), data)


def _pat(isin: str, quarter_end: date, filed: date, value: str) -> _PitFact:
    return _PitFact(
        isin=isin,
        period_start=quarter_end - timedelta(days=90),
        period_end=quarter_end,
        filing_date=filed,
        filing_id=f"{isin}-{quarter_end}",
        nature=Nature.CONSOLIDATED,
        concept="profit_after_tax",
        segment=None,
        value=Decimal(value),
    )


_QUARTERS = (date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31))


def test_the_l1_profitability_source_sums_four_knowable_quarters() -> None:
    facts = [_pat(A, q, q + timedelta(days=45), "10") for q in _QUARTERS]
    facts += [_pat(B, q, q + timedelta(days=45), "-30") for q in _QUARTERS]
    facts += [_pat(C, q, q + timedelta(days=45), "5") for q in _QUARTERS[1:]]  # three quarters
    source = _L1Profitability(facts)
    as_of = date(2024, 3, 1)
    readings = {r.isin: r for r in source.readings(as_of, frozenset({A, B, C}))}
    assert readings[A].earnings_ttm == Decimal("40")
    assert readings[B].earnings_ttm == Decimal("-120")
    assert readings[C].earnings_ttm is None
    assert readings[A].knowable_date == _QUARTERS[-1] + timedelta(days=45)
    assert [r.isin for r in source.readings(as_of, frozenset({A}))] == [A]


def test_the_l1_profitability_source_never_reads_a_later_filing() -> None:
    # The fourth quarter is filed after the decision date: the TTM cannot be formed yet, and the
    # restated (later) first quarter is invisible too.
    facts = [_pat(A, q, q + timedelta(days=45), "10") for q in _QUARTERS]
    facts.append(_pat(A, _QUARTERS[0], date(2024, 6, 1), "-500"))  # a restatement, filed later
    source = _L1Profitability(facts)
    (early,) = source.readings(date(2024, 2, 1), frozenset({A}))
    assert early.earnings_ttm is None and early.knowable_date <= date(2024, 2, 1)
    (later,) = source.readings(date(2024, 3, 1), frozenset({A}))
    assert later.earnings_ttm == Decimal("40")
    (restated,) = source.readings(date(2024, 6, 1), frozenset({A}))
    assert restated.earnings_ttm == Decimal("-470")


# ── the real stack: replay digests ───────────────────────────────────────────────────────────────

#: :func:`tests.unit.test_momentum_v2_daily_regime._replay` per configuration, struck at
#: origin/main 523882e — before any M16.1 code. Journal, book and rails byte for byte: D13 itself
#: (its 10-name book holds every scripted name), a top-3 D13 that rotates and redeploys, the same
#: under a volatility target (the exposure path A1 shares), and the naive top-3.
_PRE_M16_1_DIGESTS = {
    "d13": (D13, "67d887030dd04994c4f675992924b893a108030d0b3d6270dd40963daf879b28"),
    "d13_top3": (
        replace(D13, top_n=3, sell_band=5),
        "673f5c736d0d70a716df77be2e4fcd505191d31ac115a24d0a36b73b8e57035f",
    ),
    "d13_top3_vol_target": (
        replace(D13, top_n=3, sell_band=5, vol_target_annual=Decimal("0.15")),
        "b206a5ab5d6bbca688955f86b90a9418eae9f43331d7bc0b49dfb93da4759dc0",
    ),
    "naive_top3": (
        MomentumV2Parameters(top_n=3),
        "c0fbd79e4acbaf2d4cbe159e45e73551deb3ed99d3bffca63dc0c05e653a1939",
    ),
}


@pytest.mark.parametrize("name", sorted(_PRE_M16_1_DIGESTS))
def test_switches_off_reproduces_the_pre_m16_1_replay_byte_for_byte(name: str) -> None:
    params, digest = _PRE_M16_1_DIGESTS[name]
    explicit_off = replace(
        params, absolute_momentum=False, residual_ranking=False, profitability_filter=False
    )
    assert _replay(params).digest() == digest
    assert _replay(explicit_off).digest() == digest


class _OverlayScriptedData(_ScriptedData):
    """The M14.5 scripted world, its 12-1 returns shifted down so some leaders trail cash, plus
    every overlay seam: residual scores that invert the 12-1 order, and half the names unprofitable.
    """

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        records = tuple(
            replace(r, momentum_12_1=r.momentum_12_1 - Decimal("0.75"))
            for r in super().signal(as_of).records
        )
        return Dataset.declaring(f"m@{as_of}", records, knowable_date=lambda r: r.knowable_date)

    def repo_rate(self, as_of: date) -> Dataset[RepoRateReading]:
        reading = RepoRateReading(REPO, date(2023, 2, 8), date(2023, 2, 8))
        return Dataset.declaring(
            f"repo@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date
        )

    def residual_momentum(self, as_of: date) -> Dataset[ResidualScore]:
        scores = tuple(
            ResidualScore(r.isin, -r.momentum_12_1, as_of - timedelta(days=1))
            for r in self.signal(as_of).records
        )
        return Dataset.declaring(f"res@{as_of}", scores, knowable_date=lambda r: r.knowable_date)

    def profitability(self, as_of: date) -> Dataset[ProfitabilityReading]:
        readings = tuple(
            ProfitabilityReading(isin, Decimal(1 if n % 2 else -1), as_of - timedelta(days=40))
            for n, isin in enumerate(NAMES)
        )
        return Dataset.declaring(f"pat@{as_of}", readings, knowable_date=lambda r: r.knowable_date)


def _replay_overlays(params: MomentumV2Parameters) -> ReplayResult:
    clock = FrozenClock(_SESSIONS[0])
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_Market(_SESSIONS),
        opening_cash=_OPENING,
    )
    book = PortfolioBook()
    book.deposit(_SESSIONS[0], _OPENING)
    broker = _AccountingBroker(sim, book, corporate_actions=BookActionCalendar())
    rail_policy = BacktestRailPolicy(
        policy_id="test-ratified-caps",
        version=1,
        rails=RAILS,
        sectors=SectorMap(source="test", sha256="test", by_isin={i: i[3:6] for i in NAMES}),
        provenance="test",
    )
    prices = {(isin, day): PRICE for isin in NAMES for day in _SESSIONS}
    return ReplayEngine(
        policy=MomentumV2Policy(_OverlayScriptedData(), params, order_caps=RAILS),
        broker=broker,
        clock=clock,
        sessions=_SESSIONS,
        rails=RailGate(rail_policy, marks_from(prices)),
    ).run()


_TOP3 = replace(D13, top_n=3, sell_band=5)


def _first_rebalance_buys(result: ReplayResult) -> set[str]:
    first = min(e.trading_date for e in result.journal if e.decision is Decision.BUY)
    assert first in _REBALANCES
    buys = [e for e in result.journal if e.decision is Decision.BUY and e.trading_date == first]
    return {e.isin for e in buys if e.isin is not None}


@pytest.mark.parametrize(
    "switch", ["absolute_momentum", "residual_ranking", "profitability_filter"]
)
def test_each_switch_changes_the_replay_and_replays_deterministically(switch: str) -> None:
    base = _replay_overlays(_TOP3)
    switched = {switch: True}
    on = _replay_overlays(replace(_TOP3, **switched))  # type: ignore[arg-type]
    assert on.digest() != base.digest()
    assert on.digest() == _replay_overlays(replace(_TOP3, **switched)).digest()  # type: ignore[arg-type]


def test_the_replayed_overlays_buy_what_each_rule_says() -> None:
    base = _first_rebalance_buys(_replay_overlays(_TOP3))
    assert len(base) == 3
    a1 = _first_rebalance_buys(_replay_overlays(replace(_TOP3, absolute_momentum=True)))
    assert a1 < base and a1  # the leaders trailing cash are not replaced by anyone
    a3 = _first_rebalance_buys(_replay_overlays(replace(_TOP3, residual_ranking=True)))
    assert a3.isdisjoint(base)  # the residual key inverts the ranking
    a4 = _first_rebalance_buys(_replay_overlays(replace(_TOP3, profitability_filter=True)))
    assert all(NAMES.index(isin) % 2 for isin in a4)
