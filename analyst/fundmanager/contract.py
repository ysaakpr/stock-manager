"""A10 · M17.4 — the decision contract: what code enforces of what the prompt promises.

A decision that has the right shape (`analyst.fundmanager.schemas`) is checked here against the
bundle the manager was actually shown and against its own arithmetic. Every rule that fails is
named; any failure **voids that decision** — it is journaled with its reasons and never staged —
and the session's other decisions stand. The numbers are fixed here, once:

- **Citations.** Every ``[F:<id>]`` / ``[S:<id>]`` in any text field, every adjustment's citation
  and every ``evidence_refs`` entry must resolve to something in the bundle: a dossier field or
  listed fact (`analyst.commons.resolve_field`), a ``market.<field>``, a filing digest shown, a
  base-rate cell shown, a cost tier shown, or a snapshot shown. An unknown id voids the decision
  (Amendment 1 (d)). An adjustment with no citation at all is refused too: every move away from
  the base rate is paid for with a cited reason (prompt, step 6).
- **Scenarios.** When given, exactly one each of BULL, BASE and BEAR, with probabilities summing
  to 1 within :data:`SCENARIO_SUM_TOLERANCE` (0.01).
- **p_beat_bench agrees with the scenarios.** The scenario-implied probability of beating the
  bench is ``P* = Σ p_i·[x_i > 0] + ½·Σ p_i·[x_i = 0]`` over the scenarios' excess returns
  ``x_i`` — the mass on scenarios that beat, with a scenario exactly at zero counted as a coin
  flip. ``p_beat_bench`` is consistent iff ``|p_beat_bench - P*| ≤`` :data:`P_TOLERANCE` (0.15).
  Three points are a coarse distribution, hence the band; a p on the other side of the scenarios'
  own verdict is not inside it.
- **edge_type NONE never buys** (forces PASS/WATCH/HOLD; a SELL or TRIM of a name with no edge left
  is the consequence, not a breach).
- **The cost hurdle.** A BUY's round trip is the shared cost model plus SimBroker slippage on that
  name's close and median traded value for ``target_weight`` of the book (`bundle.round_trip`).
  The BUY stands only if all three are positive: the model's own ``cost_hurdle_check``,
  ``expected_excess_pct - round trip``, and ``scenario mean excess - round trip`` — so neither an
  optimistic headline number nor a mis-stated check can carry a trade the scenarios do not.
- **Stops.** A BUY's ``stop_pct`` is within [1.5, 3] x the name's ATR(14) as a percentage of its
  close (`atr14_pct` from its dossier) and at most 15 %; an unknown ATR refuses the BUY rather
  than guessing. A held stop may only tighten: ``new_stop_pct`` puts the stop at
  ``close x (1 - new_stop_pct/100)``, which must be at or above the current stop price. A
  ``stop_pct`` on anything but a BUY, or a ``new_stop_pct`` on a name not held, is refused.
- **Roles and weights.** Holdings get HOLD/TRIM/SELL, researched candidates BUY/WATCH/PASS, and a
  name neither held nor researched gets nothing (it has no dossier to decide on). A BUY's weight is
  in (0, the mandate's ``max_position_pct``]; a TRIM's is above 0 and below the current weight; a
  SELL's is 0 or absent; a WATCH or PASS carries none. No BUY of an excluded name, and none at all
  in a session whose surveillance list is stale (Amendment 1 (b)).
- **The base-rate cell.** It must be a cell the manager was shown, for this name's screen
  membership (S1-S4 it is on, COMPOSITE if shortlisted, NONE if neither), its cap tier or ALL and
  this session's regime or ALL; its quoted numbers must equal the table's within
  :data:`QUOTE_TOLERANCE`.

Every breach carries a stable :class:`ReasonCode` beside its message (M17.12): the message is for
the journal, the code is what a log line, the digest and the status page show, because a message
can quote words the model wrote (an adjustment's reason, a malformed ref) and a code never does.

What this module never does: call a model, read a clock, change a decision to make it pass, or
apply a rail (caps, participation, min hold are M17.5's, applied when M17.7 stages).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from analyst.commons import BaseRateCell, CommonsScreens, Dossier, UnknownFieldError, resolve_field
from analyst.fundmanager.bundle import ManagerBook, RoundTrip, SlippageCurve, round_trip
from analyst.fundmanager.schemas import (
    CANDIDATE_ACTIONS,
    HOLDING_ACTIONS,
    Action,
    EdgeType,
    ManagerDecision,
    Scenario,
    ScenarioName,
)
from execution.costs import CostModel

__all__ = [
    "CITATION_PATTERN",
    "P_TOLERANCE",
    "QUOTE_TOLERANCE",
    "SCENARIO_SUM_TOLERANCE",
    "STOP_ATR_MAX",
    "STOP_ATR_MIN",
    "STOP_MAX_PCT",
    "CitationIndex",
    "ContractVerdict",
    "DecisionContext",
    "NameFacts",
    "ReasonCode",
    "citations_of",
    "implied_p_beat",
    "scenario_mean",
    "shown_cells",
    "validate_decision",
]

SCENARIO_SUM_TOLERANCE: Final = Decimal("0.01")
P_TOLERANCE: Final = Decimal("0.15")
QUOTE_TOLERANCE: Final = Decimal("0.0005")
STOP_ATR_MIN: Final = Decimal("1.5")
STOP_ATR_MAX: Final = Decimal("3")
STOP_MAX_PCT: Final = Decimal("15")

#: ``[F:<id>]`` or ``[S:<id>]``; the id runs to the closing bracket and holds no whitespace.
CITATION_PATTERN: Final = re.compile(r"\[(?P<kind>[FS]):(?P<id>[^\[\]\s]+)\]")
_BARE_REF: Final = re.compile(r"^(?P<kind>[FS]):(?P<id>[^\[\]\s]+)$")

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_HALF: Final = Decimal("0.5")
_HUNDRED: Final = Decimal(100)
_ALL: Final = "ALL"
_RANKED_SCREENS: Final = frozenset({"S1", "S2", "S3", "S4"})


class ReasonCode(StrEnum):
    """Why the contract voided a decision, one code per rule (module docstring)."""

    WRONG_ROLE = "WRONG_ROLE"
    NOT_RESEARCHED = "NOT_RESEARCHED"
    MALFORMED_REF = "MALFORMED_REF"
    UNCITED_ADJUSTMENT = "UNCITED_ADJUSTMENT"
    UNKNOWN_CITATION = "UNKNOWN_CITATION"
    SCENARIO_SHAPE = "SCENARIO_SHAPE"
    SCENARIO_SUM = "SCENARIO_SUM"
    P_DISAGREES = "P_DISAGREES"
    CELL_NOT_SHOWN = "CELL_NOT_SHOWN"
    CELL_WRONG_SCREEN = "CELL_WRONG_SCREEN"
    CELL_WRONG_TIER = "CELL_WRONG_TIER"
    CELL_WRONG_REGIME = "CELL_WRONG_REGIME"
    CELL_MISQUOTED = "CELL_MISQUOTED"
    EDGE_NONE_BUY = "EDGE_NONE_BUY"
    BUYS_BLOCKED = "BUYS_BLOCKED"
    NO_FACTS = "NO_FACTS"
    EXCLUDED = "EXCLUDED"
    WEIGHT = "WEIGHT"
    NO_WHOLE_SHARE = "NO_WHOLE_SHARE"
    COST_CHECK = "COST_CHECK"
    BELOW_HURDLE = "BELOW_HURDLE"
    SCENARIOS_BELOW_HURDLE = "SCENARIOS_BELOW_HURDLE"
    NO_STOP = "NO_STOP"
    NO_ATR = "NO_ATR"
    STOP_OUTSIDE_ATR = "STOP_OUTSIDE_ATR"
    STOP_OVER_CEILING = "STOP_OVER_CEILING"
    STOP_ON_NON_BUY = "STOP_ON_NON_BUY"
    NEW_STOP_NOT_HELD = "NEW_STOP_NOT_HELD"
    STOP_LOOSENED = "STOP_LOOSENED"


_Breach = tuple[ReasonCode, str]


def _b(code: ReasonCode, message: str) -> _Breach:
    return code, message


# ── citations ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CitationIndex:
    """Every id the manager was shown, by kind. ``resolves`` is the whole question it answers."""

    dossiers: tuple[Dossier, ...]
    screens: CommonsScreens
    digest_ids: frozenset[str]
    cell_ids: frozenset[str]
    cost_ids: frozenset[str]
    snapshot_ids: frozenset[str]

    def resolves(self, kind: str, ident: str) -> bool:
        if kind == "S":
            return ident in self.snapshot_ids
        if ident in self.digest_ids or ident in self.cell_ids or ident in self.cost_ids:
            return True
        try:
            resolve_field(ident, dossiers=self.dossiers, screens=self.screens)
        except UnknownFieldError:
            return False
        return True


def citations_of(decision: ManagerDecision) -> tuple[tuple[str, str], ...]:
    """Every (kind, id) a decision cites, in order of first appearance, without repeats.

    Raises nothing: a malformed ``evidence_refs`` entry is reported by `validate_decision`.
    """
    found: list[tuple[str, str]] = []
    texts: list[str] = [*decision.texts(), *(a.citation for a in decision.adjustments)]
    for text in texts:
        found += [(m["kind"], m["id"]) for m in CITATION_PATTERN.finditer(text)]
    for ref in decision.evidence_refs:
        bare = _BARE_REF.match(ref.strip())
        if bare is not None:
            found.append((bare["kind"], bare["id"]))
        else:
            found += [(m["kind"], m["id"]) for m in CITATION_PATTERN.finditer(ref)]
    return tuple(dict.fromkeys(found))


def _malformed_refs(decision: ManagerDecision) -> list[str]:
    bad = []
    for ref in decision.evidence_refs:
        text = ref.strip()
        if _BARE_REF.match(text) is None and CITATION_PATTERN.fullmatch(text) is None:
            bad.append(ref)
    return bad


# ── arithmetic over the memo ─────────────────────────────────────────────────────────────────────


def implied_p_beat(scenarios: Sequence[Scenario]) -> Decimal:
    """``Σ p_i·[x_i > 0] + ½·Σ p_i·[x_i = 0]`` (module docstring)."""
    beat = sum((s.probability for s in scenarios if s.excess_pct > _ZERO), _ZERO)
    tied = sum((s.probability for s in scenarios if s.excess_pct == _ZERO), _ZERO)
    return beat + _HALF * tied


def scenario_mean(scenarios: Sequence[Scenario]) -> Decimal:
    """The probability-weighted mean excess of the scenarios, in percentage points."""
    return sum((s.probability * s.excess_pct for s in scenarios), _ZERO)


def _scenario_breaches(decision: ManagerDecision) -> list[_Breach]:
    scenarios = decision.scenarios
    if not scenarios:
        return []
    out: list[_Breach] = []
    names = sorted(s.name.value for s in scenarios)
    if names != sorted(n.value for n in ScenarioName):
        out.append(
            _b(
                ReasonCode.SCENARIO_SHAPE,
                f"scenarios must be one each of BULL, BASE and BEAR, got {names}",
            )
        )
    total = sum((s.probability for s in scenarios), _ZERO)
    if abs(total - _ONE) > SCENARIO_SUM_TOLERANCE:
        out.append(
            _b(
                ReasonCode.SCENARIO_SUM,
                f"scenario probabilities sum to {total}, not 1 (tolerance "
                f"{SCENARIO_SUM_TOLERANCE})",
            )
        )
    implied = implied_p_beat(scenarios)
    if abs(decision.p_beat_bench - implied) > P_TOLERANCE:
        out.append(
            _b(
                ReasonCode.P_DISAGREES,
                f"p_beat_bench {decision.p_beat_bench} disagrees with the scenarios' implied "
                f"P(excess > 0) of {implied} by more than {P_TOLERANCE}",
            )
        )
    return out


# ── context ──────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NameFacts:
    """What the contract needs to know about one universe name on the session."""

    isin: str
    close: Decimal
    median_traded_value: Decimal
    cap_tier: str
    screens: frozenset[str]
    shortlisted: bool
    excluded: bool
    atr14_pct: Decimal | None

    def base_rate_screens(self) -> frozenset[str]:
        """The screen keys of the base-rate cells this name belongs to."""
        keys = set(self.screens & _RANKED_SCREENS)
        if self.shortlisted:
            keys.add("COMPOSITE")
        return frozenset(keys) if keys else frozenset({"NONE"})


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Everything one session's decisions are checked against."""

    session: date
    book: ManagerBook
    researched: frozenset[str]
    facts: Mapping[str, NameFacts]
    citations: CitationIndex
    cells: Mapping[str, BaseRateCell]
    regime: str | None
    buys_blocked: bool
    max_position_pct: Decimal
    cost_model: CostModel
    slippage: SlippageCurve


@dataclass(frozen=True, slots=True)
class ContractVerdict:
    """The contract's answer for one decision: every breach, and what an accepted one implies.

    ``reasons`` are the breach messages (journaled); ``codes`` are their `ReasonCode` values in
    the same order (logged and shown), one per reason.
    """

    decision: ManagerDecision
    reasons: tuple[str, ...]
    citations: tuple[tuple[str, str], ...]
    round_trip: RoundTrip | None = None
    stop_price: Decimal | None = None
    base_rate: BaseRateCell | None = None
    codes: tuple[ReasonCode, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.reasons


def _close(value: Decimal | None, reference: Decimal) -> bool:
    return value is not None and abs(value - reference) <= QUOTE_TOLERANCE


def _cell_breaches(
    decision: ManagerDecision, ctx: DecisionContext, facts: NameFacts | None
) -> tuple[list[_Breach], BaseRateCell | None]:
    quote = decision.base_rate_cell
    if quote is None:
        return [], None
    cell = ctx.cells.get(quote.cell_id)
    if cell is None or cell.p_beat is None:
        return [
            _b(
                ReasonCode.CELL_NOT_SHOWN,
                f"base_rate_cell {quote.cell_id!r} is not a cell you were shown",
            )
        ], None
    out: list[_Breach] = []
    if facts is not None:
        if cell.screen not in facts.base_rate_screens():
            out.append(
                _b(
                    ReasonCode.CELL_WRONG_SCREEN,
                    f"base_rate_cell {cell.cell_id} is screen {cell.screen}; {decision.isin} "
                    f"belongs to {sorted(facts.base_rate_screens())}",
                )
            )
        if cell.tier not in (facts.cap_tier, _ALL):
            out.append(
                _b(
                    ReasonCode.CELL_WRONG_TIER,
                    f"base_rate_cell {cell.cell_id} is tier {cell.tier}; {decision.isin} is "
                    f"{facts.cap_tier}",
                )
            )
    if cell.regime not in ({ctx.regime, _ALL} if ctx.regime is not None else {_ALL}):
        out.append(
            _b(
                ReasonCode.CELL_WRONG_REGIME,
                f"base_rate_cell {cell.cell_id} is regime {cell.regime}, not {ctx.regime}",
            )
        )
    if not _close(quote.p_beat, cell.p_beat):
        out.append(
            _b(
                ReasonCode.CELL_MISQUOTED,
                f"quoted P(beat) {quote.p_beat} is not the table's {cell.p_beat}",
            )
        )
    for name, quoted, actual in (
        ("median excess", quote.median_excess, cell.median_excess),
        ("IQR", quote.iqr_excess, cell.iqr_excess),
    ):
        if quoted is not None and (actual is None or not _close(quoted, actual)):
            out.append(
                _b(ReasonCode.CELL_MISQUOTED, f"quoted {name} {quoted} is not the table's {actual}")
            )
    return out, (cell if not out else None)


def _buy_breaches(
    decision: ManagerDecision, ctx: DecisionContext, facts: NameFacts | None
) -> tuple[list[_Breach], RoundTrip | None, Decimal | None]:
    out: list[_Breach] = []
    if decision.edge_type is EdgeType.NONE:
        out.append(
            _b(ReasonCode.EDGE_NONE_BUY, "edge_type NONE forces PASS, WATCH or HOLD; it never buys")
        )
    if ctx.buys_blocked:
        out.append(
            _b(
                ReasonCode.BUYS_BLOCKED,
                "no new BUY is admitted this session: the GSM/ESM list is missing or stale",
            )
        )
    if facts is None:
        missing = _b(
            ReasonCode.NO_FACTS, f"{decision.isin} has no facts in today's universe to size a BUY"
        )
        return [*out, missing], None, None
    if facts.excluded:
        out.append(_b(ReasonCode.EXCLUDED, f"{decision.isin} is excluded from the universe today"))
    weight = decision.target_weight
    trip: RoundTrip | None = None
    if weight is None or weight <= _ZERO or weight > ctx.max_position_pct:
        out.append(
            _b(
                ReasonCode.WEIGHT,
                f"a BUY's target_weight must be in (0, {ctx.max_position_pct}], got {weight}",
            )
        )
    else:
        trip = round_trip(
            isin=decision.isin,
            session=ctx.session,
            price=facts.close,
            notional=ctx.book.nav * weight / _HUNDRED,
            median_traded_value=facts.median_traded_value,
            cost_model=ctx.cost_model,
            slippage=ctx.slippage,
        )
        if trip is None:
            out.append(
                _b(
                    ReasonCode.NO_WHOLE_SHARE,
                    f"target_weight {weight}% of the book buys no whole share of {decision.isin}",
                )
            )
    if decision.cost_hurdle_check is None or decision.cost_hurdle_check <= _ZERO:
        out.append(
            _b(
                ReasonCode.COST_CHECK,
                f"a BUY needs a positive cost_hurdle_check, got {decision.cost_hurdle_check}",
            )
        )
    if trip is not None:
        if decision.expected_excess_pct - trip.total_pct <= _ZERO:
            out.append(
                _b(
                    ReasonCode.BELOW_HURDLE,
                    f"expected_excess_pct {decision.expected_excess_pct} does not clear the "
                    f"{trip.total_pct}% round trip",
                )
            )
        mean = scenario_mean(decision.scenarios)
        if mean - trip.total_pct <= _ZERO:
            out.append(
                _b(
                    ReasonCode.SCENARIOS_BELOW_HURDLE,
                    f"the scenarios' mean excess {mean} does not clear the {trip.total_pct}% "
                    "round trip",
                )
            )

    stop = decision.stop_pct
    stop_price: Decimal | None = None
    if stop is None:
        out.append(_b(ReasonCode.NO_STOP, "a BUY must declare stop_pct"))
    elif facts.atr14_pct is None or facts.atr14_pct <= _ZERO:
        out.append(
            _b(
                ReasonCode.NO_ATR,
                f"{decision.isin} has no ATR(14) today, so no stop can be checked",
            )
        )
    else:
        atr = facts.atr14_pct * _HUNDRED
        low, high = STOP_ATR_MIN * atr, STOP_ATR_MAX * atr
        if stop < low or stop > high:
            out.append(
                _b(
                    ReasonCode.STOP_OUTSIDE_ATR,
                    f"stop_pct {stop} is outside 1.5-3 x ATR ({low:.4f}-{high:.4f}%) for "
                    f"{decision.isin}",
                )
            )
        if stop > STOP_MAX_PCT:
            out.append(
                _b(
                    ReasonCode.STOP_OVER_CEILING,
                    f"stop_pct {stop} exceeds the {STOP_MAX_PCT}% ceiling",
                )
            )
        stop_price = facts.close * (_ONE - stop / _HUNDRED)
    return out, trip, stop_price


def _stop_breaches(
    decision: ManagerDecision, ctx: DecisionContext
) -> tuple[list[_Breach], Decimal | None]:
    out: list[_Breach] = []
    if decision.stop_pct is not None and decision.action is not Action.BUY:
        out.append(
            _b(
                ReasonCode.STOP_ON_NON_BUY,
                "stop_pct belongs to a BUY; a held stop is tightened with new_stop_pct",
            )
        )
    new = decision.new_stop_pct
    if new is None:
        return out, None
    holding = ctx.book.holding(decision.isin)
    facts = ctx.facts.get(decision.isin)
    if holding is None:
        not_held = _b(
            ReasonCode.NEW_STOP_NOT_HELD,
            f"new_stop_pct applies to a holding; {decision.isin} is not held",
        )
        return [*out, not_held], None
    if facts is None:
        no_close = _b(
            ReasonCode.NO_FACTS, f"{decision.isin} has no close today to set a stop against"
        )
        return [*out, no_close], None
    if new > STOP_MAX_PCT:
        out.append(
            _b(
                ReasonCode.STOP_OVER_CEILING,
                f"new_stop_pct {new} exceeds the {STOP_MAX_PCT}% ceiling",
            )
        )
    price = facts.close * (_ONE - new / _HUNDRED)
    if holding.stop_price is not None and price < holding.stop_price:
        out.append(
            _b(
                ReasonCode.STOP_LOOSENED,
                f"new_stop_pct {new} would put the stop at {price:.4f}, below the current stop "
                f"{holding.stop_price}: a stop can be tightened, never loosened",
            )
        )
    return out, price


def _weight_breaches(decision: ManagerDecision, ctx: DecisionContext) -> list[_Breach]:
    weight = decision.target_weight
    holding = ctx.book.holding(decision.isin)
    if (
        decision.action is Action.TRIM
        and holding is not None
        and (weight is None or weight <= _ZERO or weight >= holding.weight_pct)
    ):
        return [
            _b(
                ReasonCode.WEIGHT,
                f"a TRIM's target_weight must be above 0 and below the current "
                f"{holding.weight_pct}%, got {weight}",
            )
        ]
    if decision.action is Action.SELL and weight not in (None, _ZERO):
        return [
            _b(
                ReasonCode.WEIGHT,
                f"a SELL exits the position; target_weight must be 0 or absent, got {weight}",
            )
        ]
    if decision.action in (Action.WATCH, Action.PASS) and weight not in (None, _ZERO):
        return [_b(ReasonCode.WEIGHT, f"a {decision.action.value} carries no weight, got {weight}")]
    return []


def validate_decision(decision: ManagerDecision, ctx: DecisionContext) -> ContractVerdict:
    """Every contract breach of one well-formed decision (module docstring), and its implications.

    What it does: checks every rule and reports all the breaches, not the first, so the journal
    says everything that was wrong with a voided decision, each with its `ReasonCode`.
    What it never does: repair a decision or raise for a breach.
    """
    breaches: list[_Breach] = []
    held = decision.isin in ctx.book.isins
    if held and decision.action not in HOLDING_ACTIONS:
        breaches.append(
            _b(
                ReasonCode.WRONG_ROLE,
                f"{decision.isin} is held: decide HOLD, TRIM or SELL, not {decision.action}",
            )
        )
    if not held:
        if decision.action not in CANDIDATE_ACTIONS:
            breaches.append(
                _b(
                    ReasonCode.WRONG_ROLE,
                    f"{decision.isin} is not held: decide BUY, WATCH or PASS, not "
                    f"{decision.action}",
                )
            )
        if decision.isin not in ctx.researched:
            breaches.append(
                _b(
                    ReasonCode.NOT_RESEARCHED,
                    f"{decision.isin} was neither held nor researched; there is no dossier to "
                    "decide on",
                )
            )

    citations = citations_of(decision)
    breaches += [
        _b(ReasonCode.MALFORMED_REF, f"malformed evidence ref {ref!r}")
        for ref in _malformed_refs(decision)
    ]
    breaches += [
        _b(ReasonCode.UNCITED_ADJUSTMENT, f"adjustment {a.reason!r} cites nothing resolvable")
        for a in decision.adjustments
        if CITATION_PATTERN.search(a.citation) is None and _BARE_REF.match(a.citation) is None
    ]
    breaches += [
        _b(ReasonCode.UNKNOWN_CITATION, f"unknown citation [{kind}:{ident}]")
        for kind, ident in citations
        if not ctx.citations.resolves(kind, ident)
    ]
    breaches += _scenario_breaches(decision)

    facts = ctx.facts.get(decision.isin)
    cell_breaches, cell = _cell_breaches(decision, ctx, facts)
    breaches += cell_breaches
    trip: RoundTrip | None = None
    stop_price: Decimal | None = None
    if decision.action is Action.BUY:
        buy_breaches, trip, stop_price = _buy_breaches(decision, ctx, facts)
        breaches += buy_breaches
    stop_breaches, tightened = _stop_breaches(decision, ctx)
    breaches += stop_breaches
    if tightened is not None:
        stop_price = tightened
    breaches += _weight_breaches(decision, ctx)
    unique = tuple(dict.fromkeys(breaches))
    return ContractVerdict(
        decision=decision,
        reasons=tuple(message for _, message in unique),
        citations=citations,
        round_trip=trip,
        stop_price=stop_price,
        base_rate=cell,
        codes=tuple(code for code, _ in unique),
    )


def shown_cells(
    cells: Iterable[BaseRateCell], *, regime: str | None, horizons: Sequence[int]
) -> dict[str, BaseRateCell]:
    """The cells `render.render_base_rates` shows, by id — the only ones a memo may cite."""
    regimes = {_ALL} | ({regime} if regime is not None else set())
    return {
        c.cell_id: c
        for c in cells
        if c.regime in regimes and c.horizon in horizons and c.p_beat is not None
    }
