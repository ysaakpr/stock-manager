"""A10 · M17.6 — the M17 scoreboard: pre-registration §6 and Amendment 1 (f), from persisted state.

The scoreboard is a pure function of what the M17 run persisted: the per-session **book marks**
(every manager, control and the bench writes one each close), the **decisions** the managers
journaled, the **outcomes** those decisions resolved to, the rail refusals and model calls the
journal already holds, and the roster. `inputs_from_journal` reads every one of them back out of
the journal stream, so the scoreboard the daily job computed in-process and the one rebuilt later
from the journal are the same bytes (`Scoreboard.canonical_bytes`) — acceptance 1.

**The §6 rule**, per manager, over the window S0 .. S0 + 62 (63 sessions):

- *pass* — all three: excess over its control >= +3.0 pp; max drawdown <= BENCH-N500's + 5 pp;
  Brier score of ``p_beat_bench`` < 0.25 over >= 30 resolved decisions;
- *clear fail* — excess over control <= -3.0 pp, or Brier >= 0.25 on >= 30 resolved decisions;
- otherwise *inconclusive*, which runs the one pre-registered extension to S0 + 125 (126 sessions)
  under the same rule, and its verdict is final. There is no second extension.

The result is reported as "*k* of 4 passed", never as a lone winner. Every threshold is a module
constant compared on the exact `Decimal`; the reported numbers are quantised for display only, so a
value a hair under a threshold is never rounded across it.

**Definitions this module fixes** (a priori, stated once):

- *Return* is the book's mark at the close of the window's last session over its opening capital
  (the S0 open: every book starts all cash), after costs and cash interest because the mark is the
  paper account's own cash plus its positions at the close.
- *Max drawdown* is the largest peak-to-trough fall of the series (opening capital, then every
  close mark in the window), in percentage points of the peak.
- *A decision is resolved* once its ``horizon_sessions`` have elapsed: its outcome compares the
  name's adjusted close-to-close return from the decision session to the session ``horizon``
  sessions later against BENCH-N500's over the same sessions. A ``BUY`` whose position is fully
  sold earlier resolves at that exit session instead (``OutcomeReason.EXITED``). The outcome is
  journaled when it resolves (`resolve_outcome` → `outcome_entry`), so the scoreboard never reads
  a price. "Beat" is strict: a tie is a miss.
- *A name that delists* before its decision resolves (M17.7) is scored at its **last traded
  close**: the name's return runs from the decision session to that close, the bench's to the
  resolution session as usual — the capital stayed in the name at that value, which is how the
  paper path carries it. Likewise `mark_book` values a held, delisted name at its last traded raw
  close, exactly as the backtests' NAV path carries a name that stopped printing (its last-known
  close, never zero, never a guess), until a corporate action converts it (a curated cash exit is
  booked by the account and leaves cash). Only a name whose listing has *ended* is treated so
  (`DelistedNames`); a listed name with a missing close is still a loud `OutcomeError` /
  `BookError`, because a gap is a data fault, not a delisting.
- *Brier* = mean of ``(p - o)^2`` with ``o`` = 1 if the name beat the bench, else 0, over the
  window's resolved decisions (decided in the window and resolved by its last session). Every
  decision carries ``p_beat_bench``, so every resolved decision counts, whatever its action.
- *Shrunk Brier* (Amendment 1 f) uses ``0.5·p + 0.5·base_rate`` — the weight is fixed here.

What it never does: read a clock, a price or the network; let one manager's decisions into another
manager's score; or change the §6 rule with an Amendment 1 (f) metric — those are secondary only.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from analyst.fundmanager.books import (
    PAPER_MODE,
    BookError,
    DelistedNames,
    ExecutionReport,
    FundBook,
    LastTraded,
)
from analyst.fundmanager.mandate import (
    BenchMandate,
    ControlMandate,
    ManagerMandate,
    Roster,
)
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import ISIN_PATTERN, Actor, Decision, JournalEntry
from dataplatform.clock import Clock

__all__ = [
    "BRIER_BAR",
    "CONTROL_REBALANCE_EVENT",
    "DECISION_EVENT",
    "DRAWDOWN_SLACK_PP",
    "EXCESS_FAIL_PP",
    "EXCESS_PASS_PP",
    "EXTENSION_SESSIONS",
    "MARK_EVENT",
    "MIN_RESOLVED",
    "OUTCOME_EVENT",
    "S0_EVENT",
    "SCOREBOARD_VERSION",
    "SHRINK_WEIGHT",
    "WINDOW_SESSIONS",
    "BookMark",
    "BookSummary",
    "ControlBuy",
    "DecisionAction",
    "DecisionLine",
    "DecisionOutcome",
    "DelistedNames",
    "GroupStat",
    "LastTraded",
    "ManagerScore",
    "ModelCall",
    "OutcomeError",
    "OutcomePrices",
    "OutcomeReason",
    "Phase",
    "RailRefusal",
    "RuleChecks",
    "ScoreVerdict",
    "Scoreboard",
    "ScoreboardError",
    "ScoreboardInputs",
    "ScoredDecision",
    "SecondaryMetrics",
    "WindowScore",
    "apply_rule",
    "brier_score",
    "build_scoreboard",
    "decision_payload",
    "inputs_from_journal",
    "mark_book",
    "mark_entry",
    "max_drawdown_pp",
    "outcome_entry",
    "resolve_outcome",
    "scored_decision_from_entry",
    "shrunk_probability",
    "todays_decisions",
]

# ── the pre-registered numbers (§6, §8 Amendment 1 f) ────────────────────────────────────────────

#: The scoring window: S0 .. S0 + 62.
WINDOW_SESSIONS: Final = 63
#: The one pre-registered extension: S0 .. S0 + 125.
EXTENSION_SESSIONS: Final = 126
#: Pass needs excess over the control at or above this; clear fail is at or below its negative.
EXCESS_PASS_PP: Final = Decimal("3.0")
EXCESS_FAIL_PP: Final = Decimal("-3.0")
#: Pass needs max drawdown at most BENCH-N500's plus this.
DRAWDOWN_SLACK_PP: Final = Decimal("5")
#: Pass needs Brier strictly below this; clear fail is at or above it. 0.25 is "always 50 %".
BRIER_BAR: Final = Decimal("0.25")
#: The Brier leg of the rule only speaks with at least this many resolved decisions.
MIN_RESOLVED: Final = 30
#: Amendment 1 (f): the weight on the model's p in the shrunk forecast; the rest is the base rate.
SHRINK_WEIGHT: Final = Decimal("0.5")

SCOREBOARD_VERSION: Final = "m17-scoreboard/1"

#: ``payload.event`` values this module writes or reads on the journal.
MARK_EVENT: Final = "BOOK_MARK"
OUTCOME_EVENT: Final = "DECISION_OUTCOME"
#: The manager decision line (M17.4 writes it; `scored_decision_from_entry` reads it).
DECISION_EVENT: Final = "FM_DECISION"
#: M17.7's ``--start S0`` line, journaled in each book's stream beside its mandate hash: the first
#: one's ``trading_date`` is S0.
S0_EVENT: Final = "M17_S0"
#: A control book's rebalance line (`analyst.fundmanager.controls`): targets and their cap tiers.
CONTROL_REBALANCE_EVENT: Final = "CONTROL_REBALANCE"
#: The staged-order line `FundBook` writes (books.py ``_stage``).
_STAGED_EVENT: Final = "STAGED"

_CONTEXT: Final = Context(prec=34, rounding=ROUND_HALF_EVEN)
_PP: Final = Decimal("0.0001")
_SCORE: Final = Decimal("0.00000001")
_RUPEE: Final = Decimal("0.000001")
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_HUNDRED: Final = Decimal(100)
_BP: Final = Decimal(10000)
_HALF: Final = Decimal("0.5")
_UNKNOWN: Final = "UNKNOWN"


class ScoreboardError(ValueError):
    """The persisted state cannot be scored as it stands — always loud, never a quiet zero."""


class OutcomeError(ScoreboardError):
    """A decision is due to resolve but a price it needs is missing."""


# ── the records the scoreboard reads ─────────────────────────────────────────────────────────────


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def record_json(self) -> str:
        """Canonical JSON of the record: what goes into a journal payload, as one string."""
        return _canonical(self.model_dump(mode="json")).decode("ascii")


class BookMark(_Record):
    """One book's mark at one session's close: what the book is worth and what the session cost.

    ``nav`` is cash (settled or in settlement) plus every position at the session's raw close, so
    it is after costs and interest by construction. ``turnover`` is the gross fill value of the
    session's fills, ``costs`` their cost-model charges, ``interest`` the idle-cash credit.
    """

    book_id: str = Field(min_length=1)
    session: date
    nav: Decimal = Field(ge=0)
    cash: Decimal
    positions: int = Field(ge=0)
    turnover: Decimal = Field(ge=0)
    costs: Decimal = Field(ge=0)
    interest: Decimal = Field(ge=0)


class DecisionAction(StrEnum):
    """A manager's action on one name (pre-registration §4 step 4)."""

    BUY = "BUY"
    TRIM = "TRIM"
    SELL = "SELL"
    HOLD = "HOLD"
    PASS = "PASS"
    WATCH = "WATCH"


class ScoredDecision(_Record):
    """The narrow, typed slice of one manager decision the scoreboard scores.

    The decision journal schema is M17.4's; this is only what §6 and Amendment 1 (f) need from it.
    ``base_rate_p`` is the decision's base-rate cell probability, ``regime`` and ``cap_tier`` the
    state and tier the decision was made in, ``in_shortlist`` whether the name was on the session's
    mechanical shortlist.
    """

    book_id: str = Field(min_length=1)
    decided_on: date
    isin: str = Field(pattern=ISIN_PATTERN)
    action: DecisionAction
    horizon_sessions: int = Field(ge=1)
    p_beat_bench: Decimal = Field(ge=0, le=1)
    target_weight: Decimal | None = None
    expected_excess_pct: Decimal | None = None
    edge_type: str = Field(min_length=1)
    base_rate_p: Decimal | None = Field(default=None, ge=0, le=1)
    regime: str | None = None
    cap_tier: str | None = None
    in_shortlist: bool | None = None
    stop_pct: Decimal | None = None

    @property
    def key(self) -> str:
        """One decision per (book, session, name, action): the join key to its outcome."""
        return decision_key(self.book_id, self.decided_on, self.isin, self.action)


def decision_key(book_id: str, decided_on: date, isin: str, action: DecisionAction) -> str:
    return f"{book_id}|{decided_on.isoformat()}|{isin}|{action.value}"


class OutcomeReason(StrEnum):
    HORIZON = "HORIZON"
    """``horizon_sessions`` elapsed."""
    EXITED = "EXITED"
    """A BUY's position was fully sold before its horizon."""


class DecisionOutcome(_Record):
    """What one decision resolved to: the name's and the bench's return over the same sessions."""

    decision_key: str = Field(min_length=1)
    book_id: str = Field(min_length=1)
    isin: str = Field(pattern=ISIN_PATTERN)
    decided_on: date
    resolved_on: date
    sessions: int = Field(ge=1)
    name_return: Decimal
    bench_return: Decimal
    reason: OutcomeReason
    #: The name's last traded session when it had delisted before ``resolved_on`` (its return is
    #: measured to that close); None for a name that printed on ``resolved_on``.
    name_last_traded: date | None = None

    @property
    def beat(self) -> bool:
        """Strictly beat the bench. A tie is a miss."""
        return self.name_return > self.bench_return

    @property
    def excess_pp(self) -> Decimal:
        return (self.name_return - self.bench_return) * _HUNDRED


class RailRefusal(_Record):
    """One order the M17 rails refused, and every rail that refused it."""

    book_id: str = Field(min_length=1)
    session: date
    isin: str | None = None
    rails: tuple[str, ...] = Field(min_length=1)


class ModelCall(_Record):
    """One model call a manager journaled, with its tokens and journaled cost estimate."""

    book_id: str = Field(min_length=1)
    session: date
    model: str = Field(min_length=1)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost_inr: Decimal = Field(ge=0)


class ControlBuy(_Record):
    """One BUY a control book staged, with the cap tier of the name at its rebalance."""

    book_id: str = Field(min_length=1)
    session: date
    isin: str = Field(pattern=ISIN_PATTERN)
    cap_tier: str | None = None


@dataclass(frozen=True, slots=True)
class ScoreboardInputs:
    """Everything the scoreboard reads. Built live by the daily job, or by `inputs_from_journal`."""

    s0: date | None
    marks: tuple[BookMark, ...] = ()
    decisions: tuple[ScoredDecision, ...] = ()
    outcomes: tuple[DecisionOutcome, ...] = ()
    refusals: tuple[RailRefusal, ...] = ()
    calls: tuple[ModelCall, ...] = ()
    control_buys: tuple[ControlBuy, ...] = ()


# ── the outputs ──────────────────────────────────────────────────────────────────────────────────


class ScoreVerdict(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    """No S0 journaled yet: nothing counts."""
    IN_PROGRESS = "IN_PROGRESS"
    """The window (or the extension) has not closed; the numbers are provisional."""
    PASS = "PASS"
    CLEAR_FAIL = "CLEAR_FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class Phase(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PRIMARY = "PRIMARY"
    """Inside S0 .. S0 + 62."""
    EXTENSION = "EXTENSION"
    """The primary window was inconclusive; inside S0 .. S0 + 125."""
    FINAL = "FINAL"


class RuleChecks(_Record):
    """Each leg of the §6 rule, so a reader sees which one decided the verdict."""

    excess_pass: bool
    drawdown_pass: bool
    brier_pass: bool
    excess_fail: bool
    brier_fail: bool


class GroupStat(_Record):
    """One group's count, hits and mean excess (pp) — a tercile, a regime, an edge type."""

    label: str
    n: int
    hits: int
    hit_rate: Decimal | None
    mean_excess_pp: Decimal | None


class SecondaryMetrics(_Record):
    """§6's secondary metrics and Amendment 1 (f)'s additions. Reported, never selected on."""

    excess_vs_bench_pp: Decimal
    buy_hit_rate_p_gt_half: GroupStat
    confidence_terciles: tuple[GroupStat, ...]
    manager_turnover_x: Decimal
    control_turnover_x: Decimal
    manager_cost_drag_bp: Decimal
    control_cost_drag_bp: Decimal
    buys: int
    outside_shortlist_buys: int
    outside_shortlist_buy_share: Decimal | None
    outside_shortlist_excess: GroupStat
    rail_refusals: dict[str, int]
    control_rail_refusals: dict[str, int]
    model_calls: int
    tokens_in: int
    tokens_out: int
    model_cost_inr: Decimal
    horizon_band: str
    buys_horizon_in_band: int
    buys_horizon_in_band_share: Decimal | None
    # Amendment 1 (f)
    shrunk_brier: Decimal | None
    shrunk_brier_n: int
    excess_by_regime: tuple[GroupStat, ...]
    cap_tier_mix_buys: dict[str, int]
    cap_tier_mix_control_buys: dict[str, int]
    hit_rate_by_edge_type: tuple[GroupStat, ...]


class WindowScore(_Record):
    """One manager over one window (primary or extension): the §6 numbers and its verdict."""

    label: str
    start: date
    end: date
    sessions: int
    complete: bool
    manager_return_pct: Decimal
    control_return_pct: Decimal
    bench_return_pct: Decimal
    excess_vs_control_pp: Decimal
    manager_max_drawdown_pp: Decimal
    control_max_drawdown_pp: Decimal
    bench_max_drawdown_pp: Decimal
    brier: Decimal | None
    resolved_decisions: int
    checks: RuleChecks
    verdict: ScoreVerdict
    secondary: SecondaryMetrics


class ManagerScore(_Record):
    manager_id: str
    control_id: str
    phase: Phase
    verdict: ScoreVerdict
    primary: WindowScore | None
    extension: WindowScore | None


class BookSummary(_Record):
    """A book's latest mark: what the status page and the digest show per book."""

    book_id: str
    kind: str
    opening_capital_inr: Decimal
    latest_session: date | None
    nav: Decimal | None
    cash: Decimal | None
    positions: int | None
    return_pct: Decimal | None


class Scoreboard(_Record):
    """The whole M17 scoreboard as of the latest mark. Its canonical bytes are its identity."""

    version: str
    preregistration: str
    s0: date | None
    as_of: date | None
    sessions_elapsed: int
    books: tuple[BookSummary, ...]
    managers: tuple[ManagerScore, ...]
    passed: int
    k_of_n: str

    def canonical_bytes(self) -> bytes:
        return _canonical(self.model_dump(mode="json"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


# ── the pure rule ────────────────────────────────────────────────────────────────────────────────


def brier_score(pairs: Iterable[tuple[Decimal, bool]]) -> Decimal | None:
    """Mean of ``(p - o)^2`` over (forecast, outcome) pairs; ``o`` is 1 for True. None if empty."""
    items = list(pairs)
    if not items:
        return None
    with localcontext(_CONTEXT):
        total = sum(((p - (_ONE if hit else _ZERO)) ** 2 for p, hit in items), _ZERO)
        return total / Decimal(len(items))


def shrunk_probability(p: Decimal, base_rate: Decimal) -> Decimal:
    """Amendment 1 (f): ``SHRINK_WEIGHT·p + (1 - SHRINK_WEIGHT)·base_rate``."""
    with localcontext(_CONTEXT):
        return SHRINK_WEIGHT * p + (_ONE - SHRINK_WEIGHT) * base_rate


def max_drawdown_pp(values: Sequence[Decimal]) -> Decimal:
    """The largest peak-to-trough fall of ``values``, in percentage points of the peak (>= 0)."""
    worst = _ZERO
    peak: Decimal | None = None
    with localcontext(_CONTEXT):
        for value in values:
            if peak is None or value > peak:
                peak = value
            if peak > _ZERO:
                fall = (peak - value) / peak * _HUNDRED
                if fall > worst:
                    worst = fall
    return worst


def apply_rule(
    *,
    excess_vs_control_pp: Decimal,
    manager_max_drawdown_pp: Decimal,
    bench_max_drawdown_pp: Decimal,
    brier: Decimal | None,
    resolved: int,
) -> tuple[ScoreVerdict, RuleChecks]:
    """The §6 pass / clear fail / inconclusive rule on exact values."""
    enough = resolved >= MIN_RESOLVED and brier is not None
    checks = RuleChecks(
        excess_pass=excess_vs_control_pp >= EXCESS_PASS_PP,
        drawdown_pass=manager_max_drawdown_pp <= bench_max_drawdown_pp + DRAWDOWN_SLACK_PP,
        brier_pass=enough and brier is not None and brier < BRIER_BAR,
        excess_fail=excess_vs_control_pp <= EXCESS_FAIL_PP,
        brier_fail=enough and brier is not None and brier >= BRIER_BAR,
    )
    if checks.excess_pass and checks.drawdown_pass and checks.brier_pass:
        return ScoreVerdict.PASS, checks
    if checks.excess_fail or checks.brier_fail:
        return ScoreVerdict.CLEAR_FAIL, checks
    return ScoreVerdict.INCONCLUSIVE, checks


# ── building the scoreboard ──────────────────────────────────────────────────────────────────────


def build_scoreboard(roster: Roster, inputs: ScoreboardInputs) -> Scoreboard:
    """The scoreboard of every roster manager from ``inputs`` alone.

    Raises `ScoreboardError` for a mark, decision or outcome that names a book the roster does not
    hold, two marks for one book and session, two decisions or outcomes under one key, or a book
    with no mark on a session the window needs.
    """
    known = {book.id for book in roster.books}
    marks: dict[str, dict[date, BookMark]] = defaultdict(dict)
    for mark in inputs.marks:
        if mark.book_id not in known:
            raise ScoreboardError(f"a mark for {mark.book_id!r}, which is not an M17 book")
        if mark.session in marks[mark.book_id]:
            raise ScoreboardError(f"two marks for {mark.book_id} on {mark.session.isoformat()}")
        marks[mark.book_id][mark.session] = mark
    decisions = _unique(inputs.decisions, "decision", lambda d: d.key)
    outcomes = _unique(inputs.outcomes, "outcome", lambda o: o.decision_key)
    for outcome in outcomes.values():
        if outcome.decision_key not in decisions:
            raise ScoreboardError(f"an outcome for {outcome.decision_key}, which was never decided")

    bench = _the_bench(roster)
    s0 = inputs.s0
    calendar = (
        sorted({m.session for m in inputs.marks if m.session >= s0}) if s0 is not None else []
    )
    scores: list[ManagerScore] = []
    for manager in roster.managers:
        control = roster.control_for(manager.id)
        scores.append(
            _manager_score(
                manager, control, bench, s0, calendar, marks, decisions, outcomes, inputs
            )
        )
    passed = sum(1 for score in scores if score.verdict is ScoreVerdict.PASS)
    latest = max((m.session for m in inputs.marks), default=None)
    return Scoreboard(
        version=SCOREBOARD_VERSION,
        preregistration=roster.preregistration,
        s0=s0,
        as_of=latest,
        sessions_elapsed=len(calendar),
        books=tuple(_book_summary(book, marks.get(book.id, {})) for book in roster.books),
        managers=tuple(scores),
        passed=passed,
        k_of_n=f"{passed} of {len(scores)} passed",
    )


def _unique[T](items: Iterable[T], what: str, key: Any) -> dict[str, T]:
    out: dict[str, T] = {}
    for item in items:
        k = key(item)
        if k in out:
            raise ScoreboardError(f"two {what}s under one key {k}")
        out[k] = item
    return out


def _the_bench(roster: Roster) -> BenchMandate:
    if len(roster.benches) != 1:
        raise ScoreboardError(
            f"the scoreboard needs exactly one bench, roster has {roster.benches}"
        )
    return roster.benches[0]


def _book_summary(
    book: ManagerMandate | ControlMandate | BenchMandate, marks: Mapping[date, BookMark]
) -> BookSummary:
    capital = book.opening_capital_inr
    if not marks:
        return BookSummary(
            book_id=book.id,
            kind=book.kind.value,
            opening_capital_inr=capital,
            latest_session=None,
            nav=None,
            cash=None,
            positions=None,
            return_pct=None,
        )
    last = marks[max(marks)]
    return BookSummary(
        book_id=book.id,
        kind=book.kind.value,
        opening_capital_inr=capital,
        latest_session=last.session,
        nav=last.nav,
        cash=last.cash,
        positions=last.positions,
        return_pct=_q(_return_pct(last.nav, capital), _PP),
    )


def _manager_score(
    manager: ManagerMandate,
    control: ControlMandate,
    bench: BenchMandate,
    s0: date | None,
    calendar: Sequence[date],
    marks: Mapping[str, Mapping[date, BookMark]],
    decisions: Mapping[str, ScoredDecision],
    outcomes: Mapping[str, DecisionOutcome],
    inputs: ScoreboardInputs,
) -> ManagerScore:
    if s0 is None or not calendar:
        verdict = ScoreVerdict.NOT_STARTED if s0 is None else ScoreVerdict.IN_PROGRESS
        phase = Phase.NOT_STARTED if s0 is None else Phase.PRIMARY
        return ManagerScore(
            manager_id=manager.id,
            control_id=control.id,
            phase=phase,
            verdict=verdict,
            primary=None,
            extension=None,
        )

    def window(label: str, length: int) -> WindowScore:
        sessions = calendar[:length]
        return _window_score(
            label,
            sessions,
            complete=len(calendar) >= length,
            manager=manager,
            control=control,
            bench=bench,
            marks=marks,
            decisions=decisions,
            outcomes=outcomes,
            inputs=inputs,
        )

    primary = window("primary", WINDOW_SESSIONS)
    if not primary.complete:
        return ManagerScore(
            manager_id=manager.id,
            control_id=control.id,
            phase=Phase.PRIMARY,
            verdict=ScoreVerdict.IN_PROGRESS,
            primary=primary,
            extension=None,
        )
    if primary.verdict is not ScoreVerdict.INCONCLUSIVE:
        return ManagerScore(
            manager_id=manager.id,
            control_id=control.id,
            phase=Phase.FINAL,
            verdict=primary.verdict,
            primary=primary,
            extension=None,
        )
    extension = window("extension", EXTENSION_SESSIONS)
    return ManagerScore(
        manager_id=manager.id,
        control_id=control.id,
        phase=Phase.FINAL if extension.complete else Phase.EXTENSION,
        verdict=extension.verdict if extension.complete else ScoreVerdict.IN_PROGRESS,
        primary=primary,
        extension=extension,
    )


def _nav_series(
    book_id: str,
    capital: Decimal,
    sessions: Sequence[date],
    marks: Mapping[str, Mapping[date, BookMark]],
) -> list[Decimal]:
    by_session = marks.get(book_id, {})
    series = [capital]
    for session in sessions:
        mark = by_session.get(session)
        if mark is None:
            raise ScoreboardError(
                f"{book_id} has no mark on {session.isoformat()}; every M17 book marks every "
                "session of the window, so the scoreboard cannot be struck without it"
            )
        series.append(mark.nav)
    return series


def _return_pct(nav: Decimal, capital: Decimal) -> Decimal:
    with localcontext(_CONTEXT):
        return (nav / capital - _ONE) * _HUNDRED


def _window_score(
    label: str,
    sessions: Sequence[date],
    *,
    complete: bool,
    manager: ManagerMandate,
    control: ControlMandate,
    bench: BenchMandate,
    marks: Mapping[str, Mapping[date, BookMark]],
    decisions: Mapping[str, ScoredDecision],
    outcomes: Mapping[str, DecisionOutcome],
    inputs: ScoreboardInputs,
) -> WindowScore:
    start, end = sessions[0], sessions[-1]
    m_nav = _nav_series(manager.id, manager.opening_capital_inr, sessions, marks)
    c_nav = _nav_series(control.id, control.opening_capital_inr, sessions, marks)
    b_nav = _nav_series(bench.id, bench.opening_capital_inr, sessions, marks)
    m_ret = _return_pct(m_nav[-1], manager.opening_capital_inr)
    c_ret = _return_pct(c_nav[-1], control.opening_capital_inr)
    b_ret = _return_pct(b_nav[-1], bench.opening_capital_inr)
    excess = m_ret - c_ret
    m_dd, c_dd, b_dd = max_drawdown_pp(m_nav), max_drawdown_pp(c_nav), max_drawdown_pp(b_nav)

    in_window = [
        d for d in decisions.values() if d.book_id == manager.id and start <= d.decided_on <= end
    ]
    resolved = [
        (d, outcomes[d.key])
        for d in in_window
        if d.key in outcomes and outcomes[d.key].resolved_on <= end
    ]
    brier = brier_score((d.p_beat_bench, o.beat) for d, o in resolved)
    if complete:
        verdict, checks = apply_rule(
            excess_vs_control_pp=excess,
            manager_max_drawdown_pp=m_dd,
            bench_max_drawdown_pp=b_dd,
            brier=brier,
            resolved=len(resolved),
        )
    else:
        _, checks = apply_rule(
            excess_vs_control_pp=excess,
            manager_max_drawdown_pp=m_dd,
            bench_max_drawdown_pp=b_dd,
            brier=brier,
            resolved=len(resolved),
        )
        verdict = ScoreVerdict.IN_PROGRESS

    secondary = _secondary(
        manager=manager,
        control=control,
        sessions=sessions,
        marks=marks,
        in_window=in_window,
        resolved=resolved,
        excess_vs_bench=m_ret - b_ret,
        inputs=inputs,
    )
    return WindowScore(
        label=label,
        start=start,
        end=end,
        sessions=len(sessions),
        complete=complete,
        manager_return_pct=_q(m_ret, _PP),
        control_return_pct=_q(c_ret, _PP),
        bench_return_pct=_q(b_ret, _PP),
        excess_vs_control_pp=_q(excess, _PP),
        manager_max_drawdown_pp=_q(m_dd, _PP),
        control_max_drawdown_pp=_q(c_dd, _PP),
        bench_max_drawdown_pp=_q(b_dd, _PP),
        brier=None if brier is None else _q(brier, _SCORE),
        resolved_decisions=len(resolved),
        checks=checks,
        verdict=verdict,
        secondary=secondary,
    )


def _group(label: str, rows: Sequence[tuple[bool, Decimal]]) -> GroupStat:
    """Count, hits, hit rate and mean excess (pp) of (hit, excess_pp) rows."""
    n = len(rows)
    hits = sum(1 for hit, _ in rows if hit)
    if n == 0:
        return GroupStat(label=label, n=0, hits=0, hit_rate=None, mean_excess_pp=None)
    with localcontext(_CONTEXT):
        rate = Decimal(hits) / Decimal(n)
        mean = sum((x for _, x in rows), _ZERO) / Decimal(n)
    return GroupStat(
        label=label, n=n, hits=hits, hit_rate=_q(rate, _SCORE), mean_excess_pp=_q(mean, _PP)
    )


def _directional_hit(p: Decimal, beat: bool) -> bool | None:
    """Did the forecast point the right way? None at exactly 0.5 (no direction)."""
    if p == _HALF:
        return None
    return (p > _HALF) == beat


def _turnover_x(navs: Sequence[Decimal], turnover: Decimal) -> Decimal:
    marks = navs[1:]
    if not marks:
        return _ZERO
    with localcontext(_CONTEXT):
        mean = sum(marks, _ZERO) / Decimal(len(marks))
        return _ZERO if mean == _ZERO else turnover / mean


def _session_sum(
    book_id: str, sessions: Sequence[date], marks: Mapping[str, Mapping[date, BookMark]], field: str
) -> Decimal:
    by_session = marks.get(book_id, {})
    total = _ZERO
    for session in sessions:
        mark = by_session.get(session)
        if mark is not None:
            total += getattr(mark, field)
    return total


def _secondary(
    *,
    manager: ManagerMandate,
    control: ControlMandate,
    sessions: Sequence[date],
    marks: Mapping[str, Mapping[date, BookMark]],
    in_window: Sequence[ScoredDecision],
    resolved: Sequence[tuple[ScoredDecision, DecisionOutcome]],
    excess_vs_bench: Decimal,
    inputs: ScoreboardInputs,
) -> SecondaryMetrics:
    start, end = sessions[0], sessions[-1]
    buys = [d for d in in_window if d.action is DecisionAction.BUY]
    resolved_buys = [(d, o) for d, o in resolved if d.action is DecisionAction.BUY]

    # hit rate of BUYs on p > 0.5
    confident = [(o.beat, o.excess_pp) for d, o in resolved_buys if d.p_beat_bench > _HALF]
    buy_hit = _group("BUY p>0.5", confident)

    # return by confidence tercile (resolved BUYs, ranked by p, then session, then ISIN)
    ranked = sorted(
        resolved_buys, key=lambda pair: (pair[0].p_beat_bench, pair[0].decided_on, pair[0].isin)
    )
    terciles: list[list[tuple[bool, Decimal]]] = [[], [], []]
    for rank, (_, outcome) in enumerate(ranked):
        terciles[(3 * rank) // len(ranked)].append((outcome.beat, outcome.excess_pp))
    tercile_stats = tuple(
        _group(name, rows) for name, rows in zip(("low", "middle", "high"), terciles, strict=True)
    )

    # turnover and cost drag
    m_nav = _nav_series(manager.id, manager.opening_capital_inr, sessions, marks)
    c_nav = _nav_series(control.id, control.opening_capital_inr, sessions, marks)
    with localcontext(_CONTEXT):
        m_cost_bp = (
            _session_sum(manager.id, sessions, marks, "costs") / manager.opening_capital_inr * _BP
        )
        c_cost_bp = (
            _session_sum(control.id, sessions, marks, "costs") / control.opening_capital_inr * _BP
        )
    m_turn = _turnover_x(m_nav, _session_sum(manager.id, sessions, marks, "turnover"))
    c_turn = _turnover_x(c_nav, _session_sum(control.id, sessions, marks, "turnover"))

    # BUYs from outside the shortlist
    outside = [d for d in buys if d.in_shortlist is False]
    outside_resolved = [(o.beat, o.excess_pp) for d, o in resolved_buys if d.in_shortlist is False]
    with localcontext(_CONTEXT):
        outside_share = None if not buys else Decimal(len(outside)) / Decimal(len(buys))

    # rail refusals, model calls
    def refusals(book_id: str) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for refusal in inputs.refusals:
            if refusal.book_id == book_id and start <= refusal.session <= end:
                counts.update(refusal.rails)
        return dict(sorted(counts.items()))

    calls = [c for c in inputs.calls if c.book_id == manager.id and start <= c.session <= end]

    # horizon adherence: BUY horizons inside the mandate's band
    band = manager.horizon
    in_band = [d for d in buys if band.min_sessions <= d.horizon_sessions <= band.max_sessions]
    with localcontext(_CONTEXT):
        in_band_share = None if not buys else Decimal(len(in_band)) / Decimal(len(buys))

    # Amendment 1 (f)
    with_base = [(d, o) for d, o in resolved if d.base_rate_p is not None]
    shrunk = brier_score(
        (shrunk_probability(d.p_beat_bench, d.base_rate_p), o.beat)  # type: ignore[arg-type]
        for d, o in with_base
    )
    by_regime: dict[str, list[tuple[bool, Decimal]]] = defaultdict(list)
    for d, o in resolved_buys:
        by_regime[d.regime or _UNKNOWN].append((o.beat, o.excess_pp))
    by_edge: dict[str, list[tuple[bool, Decimal]]] = defaultdict(list)
    for d, o in resolved:
        hit = _directional_hit(d.p_beat_bench, o.beat)
        if hit is not None:
            by_edge[d.edge_type].append((hit, o.excess_pp))
    tiers = Counter(d.cap_tier or _UNKNOWN for d in buys)
    control_tiers = Counter(
        b.cap_tier or _UNKNOWN
        for b in inputs.control_buys
        if b.book_id == control.id and start <= b.session <= end
    )

    return SecondaryMetrics(
        excess_vs_bench_pp=_q(excess_vs_bench, _PP),
        buy_hit_rate_p_gt_half=buy_hit,
        confidence_terciles=tercile_stats,
        manager_turnover_x=_q(m_turn, _SCORE),
        control_turnover_x=_q(c_turn, _SCORE),
        manager_cost_drag_bp=_q(m_cost_bp, _PP),
        control_cost_drag_bp=_q(c_cost_bp, _PP),
        buys=len(buys),
        outside_shortlist_buys=len(outside),
        outside_shortlist_buy_share=None if outside_share is None else _q(outside_share, _SCORE),
        outside_shortlist_excess=_group("outside shortlist", outside_resolved),
        rail_refusals=refusals(manager.id),
        control_rail_refusals=refusals(control.id),
        model_calls=len(calls),
        tokens_in=sum(c.tokens_in for c in calls),
        tokens_out=sum(c.tokens_out for c in calls),
        model_cost_inr=sum((c.cost_inr for c in calls), _ZERO),
        horizon_band=f"{band.min_sessions}-{band.max_sessions}",
        buys_horizon_in_band=len(in_band),
        buys_horizon_in_band_share=None if in_band_share is None else _q(in_band_share, _SCORE),
        shrunk_brier=None if shrunk is None else _q(shrunk, _SCORE),
        shrunk_brier_n=len(with_base),
        excess_by_regime=tuple(_group(k, v) for k, v in sorted(by_regime.items())),
        cap_tier_mix_buys=dict(sorted(tiers.items())),
        cap_tier_mix_control_buys=dict(sorted(control_tiers.items())),
        hit_rate_by_edge_type=tuple(_group(k, v) for k, v in sorted(by_edge.items())),
    )


def _q(value: Decimal, exp: Decimal) -> Decimal:
    with localcontext(_CONTEXT):
        return value.quantize(exp)


def _canonical(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


# ── writing the inputs: marks and outcomes ───────────────────────────────────────────────────────


def mark_book(
    book: FundBook,
    session: date,
    *,
    execution: ExecutionReport | None,
    delisted: DelistedNames | None = None,
) -> BookMark:
    """A manager or control book's mark at ``session``'s close.

    ``execution`` is the session's `FundBook.execute` report (its fills, costs and interest); None
    on a session the book did not execute. A held name with no close that ``delisted`` (default:
    the book's own listing record) says has delisted is valued at its last traded raw close
    (module docstring). Raises `BookError` for any other held name with no close — a book that
    cannot be valued is never marked at a guess.
    """
    invested = _ZERO
    positions = 0
    if delisted is None:
        delisted = book.delisted
    for isin, quantity in sorted(book.account.quantities().items()):
        if quantity <= 0:
            continue
        price = book.market.close(isin, session)
        if price is None:
            last = None if delisted is None else delisted.last_traded(isin, session)
            if last is None:
                raise BookError(
                    f"{book.book_id}: held {isin} has no close on {session.isoformat()}; the "
                    "book cannot be marked"
                )
            price = last.raw_close
        invested += price * quantity
        positions += 1
    fills = () if execution is None else tuple(f for f in execution.fills if f.session == session)
    cash = book.account.cash_value
    return BookMark(
        book_id=book.book_id,
        session=session,
        nav=cash + invested,
        cash=cash,
        positions=positions,
        turnover=sum((f.gross for f in fills), _ZERO),
        costs=sum((f.cost.total for f in fills), _ZERO),
        interest=_ZERO if execution is None else execution.interest_credited,
    )


def mark_entry(mark: BookMark, *, clock: Clock) -> tuple[JournalEntry, EvidenceBundle]:
    """The journal line (and its evidence) that persists ``mark`` in its book's stream."""
    evidence = EvidenceBundle(
        case_id=mark.book_id,
        trading_date=mark.session,
        actor=Actor.EXEC,
        items=(
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="m17_mark",
                label="nav",
                as_of=mark.session,
                value=mark.nav,
            ),
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="m17_mark",
                label="cash",
                as_of=mark.session,
                value=mark.cash,
            ),
        ),
    )
    entry = JournalEntry(
        ts=clock.now(),
        trading_date=mark.session,
        case_id=mark.book_id,
        actor=Actor.EXEC,
        decision=Decision.HEARTBEAT,
        evidence_snapshot_ref=evidence.ref().ref,
        payload={
            "event": MARK_EVENT,
            "book": mark.book_id,
            "mode": PAPER_MODE,
            "record": mark.record_json(),
        },
    )
    return entry, evidence


class OutcomePrices(Protocol):
    """Adjusted closes and bench levels, as of the sessions asked about."""

    def adjusted_close(self, isin: str, session: date) -> Decimal | None: ...

    def bench_level(self, session: date) -> Decimal | None: ...


def resolve_outcome(
    decision: ScoredDecision,
    *,
    calendar: Sequence[date],
    as_of: date,
    prices: OutcomePrices,
    exited_on: date | None = None,
    delisted: DelistedNames | None = None,
) -> DecisionOutcome | None:
    """``decision``'s outcome if it has resolved by ``as_of``, else None.

    It resolves at the session ``horizon_sessions`` after the decision session in ``calendar`` —
    or, for a BUY whose position was fully sold earlier, at ``exited_on``. Both returns are close
    to close over the same sessions — except for a name ``delisted`` says had delisted by the
    resolution session, whose return runs to its last traded close (module docstring). Raises
    `OutcomeError` when it is due and a level is missing for any other reason, and
    `ScoreboardError` when the decision session is not in ``calendar``.
    """
    try:
        index = list(calendar).index(decision.decided_on)
    except ValueError as exc:
        raise ScoreboardError(
            f"{decision.key}: decision session is not a session of the calendar"
        ) from exc
    due_index = index + decision.horizon_sessions
    reason = OutcomeReason.HORIZON
    resolved_on: date | None = calendar[due_index] if due_index < len(calendar) else None
    if (
        decision.action is DecisionAction.BUY
        and exited_on is not None
        and exited_on > decision.decided_on
        and (resolved_on is None or exited_on < resolved_on)
    ):
        resolved_on, reason = exited_on, OutcomeReason.EXITED
    if resolved_on is None or resolved_on > as_of:
        return None
    final = prices.adjusted_close(decision.isin, resolved_on)
    last_traded: date | None = None
    if final is None and delisted is not None:
        last = delisted.last_traded(decision.isin, resolved_on)
        if last is not None and decision.decided_on <= last.session < resolved_on:
            final, last_traded = last.adjusted_close, last.session
    levels = (
        prices.adjusted_close(decision.isin, decision.decided_on),
        final,
        prices.bench_level(decision.decided_on),
        prices.bench_level(resolved_on),
    )
    if any(level is None or level <= _ZERO for level in levels):
        raise OutcomeError(
            f"{decision.key} is due on {resolved_on.isoformat()} but a close or bench level is "
            f"missing: {levels}"
        )
    n0, n1, b0, b1 = (level for level in levels if level is not None)
    with localcontext(_CONTEXT):
        name_return = n1 / n0 - _ONE
        bench_return = b1 / b0 - _ONE
    return DecisionOutcome(
        decision_key=decision.key,
        book_id=decision.book_id,
        isin=decision.isin,
        decided_on=decision.decided_on,
        resolved_on=resolved_on,
        sessions=list(calendar).index(resolved_on) - index,
        name_return=_q(name_return, _SCORE),
        bench_return=_q(bench_return, _SCORE),
        reason=reason,
        name_last_traded=last_traded,
    )


def outcome_entry(outcome: DecisionOutcome, *, clock: Clock) -> tuple[JournalEntry, EvidenceBundle]:
    """The journal line (and its evidence) that persists ``outcome`` in its book's stream."""
    evidence = EvidenceBundle(
        case_id=outcome.book_id,
        trading_date=outcome.resolved_on,
        actor=Actor.SYSTEM,
        items=tuple(
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="m17_outcome",
                label=label,
                isin=outcome.isin,
                as_of=outcome.resolved_on,
                value=value,
            )
            for label, value in (
                ("name_return", outcome.name_return),
                ("bench_return", outcome.bench_return),
            )
        ),
    )
    entry = JournalEntry(
        ts=clock.now(),
        trading_date=outcome.resolved_on,
        case_id=outcome.book_id,
        actor=Actor.SYSTEM,
        decision=Decision.HEARTBEAT,
        isin=outcome.isin,
        evidence_snapshot_ref=evidence.ref().ref,
        payload={
            "event": OUTCOME_EVENT,
            "book": outcome.book_id,
            "mode": PAPER_MODE,
            "record": outcome.record_json(),
        },
    )
    return entry, evidence


# ── reading the inputs back out of the journal ───────────────────────────────────────────────────

#: The flat payload keys of a manager decision line (``payload.event = FM_DECISION``). The adapter
#: contract M17.4 writes to — or replaces `scored_decision_from_entry` with its own reader. Every
#: value is a string (journal payloads are strings only); ``in_shortlist`` is "true"/"false";
#: ``base_rate_cell`` is a JSON object string carrying at least ``p``.
DECISION_PAYLOAD_KEYS: Final = (
    "action",
    "horizon_sessions",
    "p_beat_bench",
    "target_weight",
    "expected_excess_pct",
    "edge_type",
    "base_rate_cell",
    "regime",
    "cap_tier",
    "in_shortlist",
    "stop_pct",
)


def decision_payload(decision: ScoredDecision) -> dict[str, str]:
    """The flat payload `scored_decision_from_entry` reads (the writer side of the contract)."""
    payload = {"event": DECISION_EVENT, "action": decision.action.value}
    payload["horizon_sessions"] = str(decision.horizon_sessions)
    payload["p_beat_bench"] = str(decision.p_beat_bench)
    payload["edge_type"] = decision.edge_type
    for key, value in (
        ("target_weight", decision.target_weight),
        ("expected_excess_pct", decision.expected_excess_pct),
        ("stop_pct", decision.stop_pct),
    ):
        if value is not None:
            payload[key] = str(value)
    if decision.base_rate_p is not None:
        payload["base_rate_cell"] = _canonical({"p": str(decision.base_rate_p)}).decode("ascii")
    if decision.regime is not None:
        payload["regime"] = decision.regime
    if decision.cap_tier is not None:
        payload["cap_tier"] = decision.cap_tier
    if decision.in_shortlist is not None:
        payload["in_shortlist"] = "true" if decision.in_shortlist else "false"
    return payload


def scored_decision_from_entry(entry: JournalEntry) -> ScoredDecision | None:
    """ADAPTER (provisional until M17.4 lands its schema): the `ScoredDecision` in ``entry``.

    Reads an entry whose ``payload.event`` is `DECISION_EVENT`, with the keys of
    `DECISION_PAYLOAD_KEYS`; None for any other entry. Raises `ScoreboardError` for a decision line
    that is malformed, because a decision that cannot be scored would otherwise vanish from Brier.
    """
    payload = entry.payload
    if payload.get("event") != DECISION_EVENT:
        return None
    if entry.case_id is None or entry.isin is None:
        raise ScoreboardError(f"a {DECISION_EVENT} line needs a book (case_id) and an isin")
    try:
        base_rate: Decimal | None = None
        if "base_rate_cell" in payload:
            cell = json.loads(payload["base_rate_cell"])
            base_rate = None if cell.get("p") in (None, "") else Decimal(str(cell["p"]))
        shortlist = payload.get("in_shortlist")
        return ScoredDecision(
            book_id=entry.case_id,
            decided_on=entry.trading_date,
            isin=entry.isin,
            action=DecisionAction(payload["action"]),
            horizon_sessions=int(payload["horizon_sessions"]),
            p_beat_bench=Decimal(payload["p_beat_bench"]),
            target_weight=_opt_decimal(payload.get("target_weight")),
            expected_excess_pct=_opt_decimal(payload.get("expected_excess_pct")),
            edge_type=payload["edge_type"],
            base_rate_p=base_rate,
            regime=payload.get("regime"),
            cap_tier=payload.get("cap_tier"),
            in_shortlist=None if shortlist is None else shortlist == "true",
            stop_pct=_opt_decimal(payload.get("stop_pct")),
        )
    except (KeyError, ValueError, ArithmeticError, ValidationError) as exc:
        raise ScoreboardError(
            f"malformed {DECISION_EVENT} line for {entry.case_id} {entry.isin} on "
            f"{entry.trading_date.isoformat()}: {exc}"
        ) from exc


def _opt_decimal(value: str | None) -> Decimal | None:
    return None if value is None or value == "" else Decimal(value)


def _record[R: _Record](entry: JournalEntry, model: type[R]) -> R:
    try:
        return model.model_validate_json(entry.payload["record"])
    except (KeyError, ValidationError) as exc:
        raise ScoreboardError(
            f"malformed {entry.payload.get('event')} line for {entry.case_id} on "
            f"{entry.trading_date.isoformat()}: {exc}"
        ) from exc


def inputs_from_journal(entries: Iterable[JournalEntry], roster: Roster) -> ScoreboardInputs:
    """Every scoreboard input, read back out of the M17 journal streams.

    ``entries`` are the books' entries in append order (any other entries are ignored). S0 is the
    first `S0_EVENT` entry's session. A control BUY takes the cap tier its book's latest rebalance
    line recorded for the name.
    """
    books = {book.id for book in roster.books}
    managers = {m.id for m in roster.managers}
    controls = {c.id for c in roster.controls}
    s0: date | None = None
    marks: list[BookMark] = []
    decisions: list[ScoredDecision] = []
    outcomes: list[DecisionOutcome] = []
    refusals: list[RailRefusal] = []
    calls: list[ModelCall] = []
    control_buys: list[ControlBuy] = []
    tiers: dict[str, dict[str, str | None]] = defaultdict(dict)
    for entry in entries:
        event = entry.payload.get("event")
        if event == S0_EVENT and s0 is None:
            s0 = entry.trading_date
            continue
        if entry.case_id not in books:
            continue
        book_id = entry.case_id
        if event == MARK_EVENT:
            marks.append(_record(entry, BookMark))
        elif event == OUTCOME_EVENT:
            outcomes.append(_record(entry, DecisionOutcome))
        elif event == DECISION_EVENT:
            scored = scored_decision_from_entry(entry)
            if scored is not None:
                decisions.append(scored)
        elif event == CONTROL_REBALANCE_EVENT and book_id in controls:
            tiers[book_id] = {
                isin: (None if tier == "" else tier)
                for isin, tier in json.loads(entry.payload["cap_tiers"]).items()
            }
        elif (
            entry.decision is Decision.BUY
            and event == _STAGED_EVENT
            and book_id in controls
            and entry.isin is not None
        ):
            control_buys.append(
                ControlBuy(
                    book_id=book_id,
                    session=entry.trading_date,
                    isin=entry.isin,
                    cap_tier=tiers[book_id].get(entry.isin),
                )
            )
        if entry.decision is Decision.RAIL_BLOCK:
            rails = tuple(r for r in entry.payload.get("rails", "").split(",") if r)
            if rails:
                refusals.append(
                    RailRefusal(
                        book_id=book_id, session=entry.trading_date, isin=entry.isin, rails=rails
                    )
                )
        if entry.tokens is not None and entry.model is not None and book_id in managers:
            calls.append(
                ModelCall(
                    book_id=book_id,
                    session=entry.trading_date,
                    model=entry.model,
                    tokens_in=entry.tokens.tokens_in,
                    tokens_out=entry.tokens.tokens_out,
                    cost_inr=entry.tokens.cost_inr,
                )
            )
    return ScoreboardInputs(
        s0=s0,
        marks=tuple(marks),
        decisions=tuple(decisions),
        outcomes=tuple(outcomes),
        refusals=tuple(refusals),
        calls=tuple(calls),
        control_buys=tuple(control_buys),
    )


# ── today's decisions (status page, digest) ──────────────────────────────────────────────────────

#: Bookkeeping lines that are not decisions: marks, outcomes, reconciliations.
_BOOKKEEPING: Final = frozenset({MARK_EVENT, OUTCOME_EVENT, "RECONCILIATION"})


class DecisionLine(_Record):
    """One journaled decision on a session, as the status page and the digest show it.

    Deliberately narrow: no rationale, no prompt, no evidence text — what was decided, never the
    words the model was given or wrote.
    """

    book_id: str
    trading_date: date
    decision: str
    event: str | None
    isin: str | None
    action: str | None
    target_weight: str | None
    p_beat_bench: str | None
    horizon_sessions: str | None
    rails: str | None
    #: A voided decision's contract `ReasonCode` values (M17.12), comma-separated; codes only,
    #: because a breach message can quote words the model wrote.
    refused_for: str | None = None


def todays_decisions(
    entries: Iterable[JournalEntry], roster: Roster, *, session: date | None = None
) -> tuple[date | None, tuple[DecisionLine, ...]]:
    """The M17 books' decisions on ``session`` (default: the latest session any book journaled)."""
    books = {book.id for book in roster.books}
    mine = [e for e in entries if e.case_id in books]
    day = session if session is not None else max((e.trading_date for e in mine), default=None)
    if day is None:
        return None, ()
    lines: list[DecisionLine] = []
    for entry in mine:
        event = entry.payload.get("event")
        if entry.trading_date != day or event in _BOOKKEEPING:
            continue
        if entry.decision is Decision.HEARTBEAT and event != CONTROL_REBALANCE_EVENT:
            continue
        lines.append(
            DecisionLine(
                book_id=entry.case_id or "",
                trading_date=entry.trading_date,
                decision=entry.decision.value,
                event=event,
                isin=entry.isin,
                action=entry.payload.get("action"),
                target_weight=entry.payload.get("target_weight"),
                p_beat_bench=entry.payload.get("p_beat_bench"),
                horizon_sessions=entry.payload.get("horizon_sessions"),
                rails=entry.payload.get("rails"),
                refused_for=entry.payload.get("reason_codes") or None,
            )
        )
    return day, tuple(lines)
