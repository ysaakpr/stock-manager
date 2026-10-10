"""A10 · M17.4 — the two shapes a fund manager answers in: a research request and its decisions.

Pre-registration §4 steps 3-4 and §8 Amendment 1 (d). The Claude CLI accepts one structured-output
schema per call (`analyst.llm.claude_cli`), so a manager session is a sequence of calls, each with
exactly one of the two tools defined here:

- `RESEARCH_TOOL` (round 0 and every research round): holdings triage, at most ``max_isins``
  deep-dive requests and at most ``max_queries`` web queries or URLs. The JSON Schema carries the
  mandate's caps as ``maxItems``; :func:`parse_research` truncates an over-long answer anyway (a
  stub or a lax provider can ignore the schema) and reports exactly what it dropped, so the runtime
  can journal the truncation.
- `DECISION_TOOL` (the final call): one decision per holding and per researched candidate, or the
  whole-session ``NO_ACTION`` form with its reason.

**Two layers of refusal**, kept apart on purpose:

1. *Shape* — this module. A missing field, a wrong enum, a probability outside [0, 1], a
   ``SELL``/``TRIM`` without ``what_changed``, a ``BUY`` without its memo fields. These raise
   :class:`MalformedOutputError`, which earns the session its one repair retry.
2. *Contract* — `analyst.fundmanager.contract`. Checks that need the bundle the manager was shown
   or arithmetic over the memo (citations resolve, scenarios sum to 1, ``p_beat_bench`` agrees with
   them, ``edge_type`` NONE never buys, the cost hurdle, stop bounds, stops only tighten). A breach
   voids that one decision; it is journaled and never staged.

**Units.** Probabilities are fractions in [0, 1]. ``target_weight``, ``stop_pct``,
``new_stop_pct``, ``expected_excess_pct``, scenario ``excess_pct``, ``cost_hurdle_check`` and
adjustment ``size_pp`` are percentage points (10 = 10 %). ``stop_pct``/``new_stop_pct`` are the
distance of the stop below the decision session's close.

**Numbers.** A model writes JSON numbers, which arrive as Python floats. Each one is converted with
``Decimal(repr(value))`` — the shortest decimal that round-trips the float, so ``0.55`` becomes
``Decimal("0.55")`` and never ``0.55000000000000004440892098500626``. Strings of digits are accepted
too. A bool, NaN or infinity is refused.

What this module never does: read a clock, the bundle or the network, or decide whether a
well-formed decision is a good one.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from analyst.fundmanager.mandate import RoundLimits
from analyst.journal.models import ISIN_PATTERN
from analyst.llm import ToolSpec

__all__ = [
    "CANDIDATE_ACTIONS",
    "DECISION_TOOL_NAME",
    "HOLDING_ACTIONS",
    "RESEARCH_TOOL_NAME",
    "SCHEMA_VERSION",
    "Action",
    "Adjustment",
    "BaseRateQuote",
    "Direction",
    "EdgeType",
    "MalformedOutputError",
    "ManagerDecision",
    "ManagerDecisions",
    "QueryItem",
    "QueryKind",
    "ResearchItem",
    "ResearchRequest",
    "ResearchTruncation",
    "Scenario",
    "ScenarioName",
    "Triage",
    "TriageItem",
    "WhatChanged",
    "WhatChangedKind",
    "decision_schema",
    "decision_tool",
    "parse_decisions",
    "parse_research",
    "research_schema",
    "research_tool",
    "schema_bytes",
    "schema_digest",
]

#: Versioned identity of both schemas and the shape rules here. In `schema_bytes`, so a change is a
#: new `mandate_hash` (pre-registration §7).
SCHEMA_VERSION: Final = "m17-fm-schema/1"
RESEARCH_TOOL_NAME: Final = "research_request"
DECISION_TOOL_NAME: Final = "manager_decisions"

_MAX_TEXT: Final = 2_000
_MAX_SHORT: Final = 500
_MAX_LIST: Final = 5
_MAX_ADJUSTMENTS: Final = 8
_MAX_PREMORTEM: Final = 3
#: The longest horizon a decision may name: one trading year. Generous — the mandate band is
#: guidance (pre-registration §2), not a rail.
_MAX_HORIZON: Final = 260


class MalformedOutputError(ValueError):
    """The model's structured output does not have the shape the schema promises."""


# ── vocabulary ───────────────────────────────────────────────────────────────────────────────────


class Triage(StrEnum):
    """Round 0's verdict on one holding (prompt, round 0, step 1)."""

    CLEAR = "CLEAR"
    RESEARCH = "RESEARCH"
    DOUBT = "DOUBT"


class QueryKind(StrEnum):
    """A web request: free-text search terms, or one URL."""

    QUERY = "QUERY"
    URL = "URL"


class Action(StrEnum):
    """A manager's action on one name (pre-registration §4 step 4)."""

    BUY = "BUY"
    TRIM = "TRIM"
    SELL = "SELL"
    HOLD = "HOLD"
    PASS = "PASS"
    WATCH = "WATCH"


#: What a holding may be told; what a researched candidate may be told.
HOLDING_ACTIONS: Final = frozenset({Action.HOLD, Action.TRIM, Action.SELL})
CANDIDATE_ACTIONS: Final = frozenset({Action.BUY, Action.WATCH, Action.PASS})


class EdgeType(StrEnum):
    """Where the manager claims its edge lies. ``NONE`` is the honest answer for most names."""

    EARNINGS_MOMENTUM = "EARNINGS_MOMENTUM"
    TREND_LEADER = "TREND_LEADER"
    VOLUME_BREAKOUT = "VOLUME_BREAKOUT"
    PULLBACK_IN_LEADER = "PULLBACK_IN_LEADER"
    EVENT_DRIFT = "EVENT_DRIFT"
    FUNDAMENTAL_INFLECTION = "FUNDAMENTAL_INFLECTION"
    NONE = "NONE"


class WhatChangedKind(StrEnum):
    """What a SELL or TRIM must name against the opening rationale (pre-registration §4 step 4)."""

    INVALIDATION = "INVALIDATION"
    STOP = "STOP"
    TARGET = "TARGET"
    BETTER_USE = "BETTER_USE"


class ScenarioName(StrEnum):
    BULL = "BULL"
    BASE = "BASE"
    BEAR = "BEAR"


class Direction(StrEnum):
    """Which way an adjustment moves the probability away from the base rate."""

    UP = "UP"
    DOWN = "DOWN"


# ── numbers ──────────────────────────────────────────────────────────────────────────────────────


def _to_decimal(value: object) -> object:
    """A JSON number or numeric string as an exact `Decimal` (module docstring, "Numbers")."""
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is a bool, not a number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{value!r} is not a finite number")
        return Decimal(repr(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            parsed = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"{value!r} is not a number") from exc
        if not parsed.is_finite():
            raise ValueError(f"{value!r} is not a finite number")
        return parsed
    raise ValueError(f"{value!r} is not a number")


Num = Annotated[Decimal, BeforeValidator(_to_decimal), Field(allow_inf_nan=False)]
Prob = Annotated[Decimal, BeforeValidator(_to_decimal), Field(ge=0, le=1, allow_inf_nan=False)]
Pct = Annotated[Decimal, BeforeValidator(_to_decimal), Field(ge=0, le=100, allow_inf_nan=False)]
PositivePct = Annotated[
    Decimal, BeforeValidator(_to_decimal), Field(gt=0, le=100, allow_inf_nan=False)
]
Isin = Annotated[str, Field(pattern=ISIN_PATTERN)]
Text = Annotated[str, Field(min_length=1, max_length=_MAX_TEXT)]
Short = Annotated[str, Field(min_length=1, max_length=_MAX_SHORT)]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ── the research request ─────────────────────────────────────────────────────────────────────────


class TriageItem(_Strict):
    """One holding's round-0 triage: still valid, needs a check, or a likely exit."""

    isin: Isin
    triage: Triage
    note: Short


class ResearchItem(_Strict):
    """One deep-dive request: the name, the claim it tests, and the data most likely to kill it."""

    isin: Isin
    claim: Short
    kill_test: Short


class QueryItem(_Strict):
    """One web search or URL for the Commons fetcher, and why it is worth a query."""

    kind: QueryKind
    target: Short
    isin: Isin | None = None
    purpose: Short


class ResearchRequest(_Strict):
    """What round 0 or a research round asks for. Empty requests and queries mean "decide now"."""

    holdings: tuple[TriageItem, ...] = ()
    requests: tuple[ResearchItem, ...] = ()
    queries: tuple[QueryItem, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.requests and not self.queries


@dataclass(frozen=True, slots=True)
class ResearchTruncation:
    """What :func:`parse_research` dropped to bring a request within the mandate's caps."""

    isins_asked: int
    isins_dropped: tuple[str, ...]
    queries_asked: int
    queries_dropped: tuple[str, ...]

    @property
    def truncated(self) -> bool:
        return bool(self.isins_dropped or self.queries_dropped)


def _string(max_length: int = _MAX_SHORT) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def _nullable(schema: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(schema)
    kind = out["type"]
    out["type"] = [kind, "null"] if isinstance(kind, str) else [*kind, "null"]
    out.pop("minLength", None)
    return out


def _enum(values: type[StrEnum]) -> dict[str, Any]:
    return {"type": "string", "enum": [v.value for v in values]}


def _object(properties: Mapping[str, Any]) -> dict[str, Any]:
    """An object with every property required and no other: the flat shape the CLI fills best."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": dict(properties),
    }


_ISIN_SCHEMA: Final = {"type": "string", "pattern": ISIN_PATTERN}


def research_schema(limits: RoundLimits) -> dict[str, Any]:
    """The research-request JSON Schema, capped at the mandate's ``max_isins``/``max_queries``."""
    return _object(
        {
            "holdings": {
                "type": "array",
                "items": _object(
                    {"isin": _ISIN_SCHEMA, "triage": _enum(Triage), "note": _string()}
                ),
            },
            "requests": {
                "type": "array",
                "maxItems": limits.max_isins,
                "items": _object(
                    {"isin": _ISIN_SCHEMA, "claim": _string(), "kill_test": _string()}
                ),
            },
            "queries": {
                "type": "array",
                "maxItems": limits.max_queries,
                "items": _object(
                    {
                        "kind": _enum(QueryKind),
                        "target": _string(),
                        "isin": {"type": ["string", "null"], "pattern": ISIN_PATTERN},
                        "purpose": _string(),
                    }
                ),
            },
        }
    )


def research_tool(limits: RoundLimits) -> ToolSpec:
    return ToolSpec(
        name=RESEARCH_TOOL_NAME,
        description=(
            "Return your holdings triage and your research requests for this round: the names "
            "to deep-dive, each with its claim and kill test, and any web queries or URLs. Return "
            "empty requests and queries when more research would not change a decision."
        ),
        input_schema=research_schema(limits),
    )


def _validation_message(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors()[:20]:
        where = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"{where}: {err['msg']}")
    return "; ".join(lines)


def parse_research(
    arguments: Mapping[str, Any], limits: RoundLimits
) -> tuple[ResearchRequest, ResearchTruncation]:
    """Validate a research request and cut it to the mandate's caps.

    What it does: refuses a wrong shape (:class:`MalformedOutputError`); drops repeated ISINs and
    repeated (kind, target) queries, keeping the first; then keeps the first ``max_isins`` requests
    and the first ``max_queries`` queries and reports the rest as dropped.
    What it never does: reorder what it keeps, or refuse a request merely for asking too much —
    over-asking is truncated and journaled, not punished with a repair.
    """
    try:
        request = ResearchRequest.model_validate(dict(arguments))
    except ValidationError as exc:
        raise MalformedOutputError(
            f"the research request does not match the schema: {_validation_message(exc)}"
        ) from exc
    seen_isins: set[str] = set()
    unique_requests: list[ResearchItem] = []
    for item in request.requests:
        if item.isin not in seen_isins:
            seen_isins.add(item.isin)
            unique_requests.append(item)
    seen_queries: set[tuple[QueryKind, str]] = set()
    unique_queries: list[QueryItem] = []
    for query in request.queries:
        key = (query.kind, " ".join(query.target.casefold().split()))
        if key not in seen_queries:
            seen_queries.add(key)
            unique_queries.append(query)
    kept_requests = unique_requests[: limits.max_isins]
    kept_queries = unique_queries[: limits.max_queries]
    truncation = ResearchTruncation(
        isins_asked=len(request.requests),
        isins_dropped=tuple(i.isin for i in unique_requests[limits.max_isins :]),
        queries_asked=len(request.queries),
        queries_dropped=tuple(q.target for q in unique_queries[limits.max_queries :]),
    )
    kept = request.model_copy(
        update={"requests": tuple(kept_requests), "queries": tuple(kept_queries)}
    )
    return kept, truncation


# ── the decisions ────────────────────────────────────────────────────────────────────────────────


class WhatChanged(_Strict):
    kind: WhatChangedKind
    text: Text


class BaseRateQuote(_Strict):
    """The base-rate cell a memo starts from, with its numbers quoted from the table."""

    cell_id: Short
    p_beat: Prob
    median_excess: Num | None = None
    iqr_excess: Num | None = None


class Adjustment(_Strict):
    """One named, cited reason to move away from the base rate, its direction and rough size."""

    reason: Short
    direction: Direction
    size_pp: Pct
    citation: Short


class Scenario(_Strict):
    name: ScenarioName
    probability: Prob
    excess_pct: Num
    description: Short


class ManagerDecision(_Strict):
    """One decision on one name (pre-registration §4 step 4 + Amendment 1 (d)).

    Shape rules enforced here: a ``SELL`` or ``TRIM`` names ``what_changed``; a ``BUY`` carries its
    whole memo — thesis, catalyst, already-priced-in, base-rate cell, scenarios, cost hurdle check,
    premortem, stop, at least one invalidation and a target weight.
    """

    isin: Isin
    action: Action
    target_weight: Pct | None = None
    rationale: Text
    horizon_sessions: int = Field(ge=1, le=_MAX_HORIZON)
    p_beat_bench: Prob
    expected_excess_pct: Num
    stop_pct: PositivePct | None = None
    new_stop_pct: PositivePct | None = None
    invalidation: tuple[Short, ...] = Field(default=(), max_length=_MAX_LIST)
    evidence_refs: tuple[Short, ...] = Field(default=(), max_length=40)
    what_changed: WhatChanged | None = None
    thesis: Text | None = None
    catalyst: Short | None = None
    already_priced_in: Text | None = None
    edge_type: EdgeType
    base_rate_cell: BaseRateQuote | None = None
    adjustments: tuple[Adjustment, ...] = Field(default=(), max_length=_MAX_ADJUSTMENTS)
    scenarios: tuple[Scenario, ...] = Field(default=(), max_length=3)
    cost_hurdle_check: Num | None = None
    premortem: tuple[Short, ...] = Field(default=(), max_length=_MAX_PREMORTEM)

    @model_validator(mode="after")
    def _shape_by_action(self) -> ManagerDecision:
        if self.action in (Action.SELL, Action.TRIM) and self.what_changed is None:
            raise ValueError(
                f"a {self.action.value} must name what_changed (INVALIDATION, STOP, TARGET or "
                "BETTER_USE, plus text): a sale with no stated change is refused"
            )
        if self.action is Action.BUY:
            missing = [
                name
                for name in (
                    "thesis",
                    "catalyst",
                    "already_priced_in",
                    "base_rate_cell",
                    "cost_hurdle_check",
                    "stop_pct",
                    "target_weight",
                )
                if getattr(self, name) is None
            ]
            missing += [
                name
                for name in ("scenarios", "premortem", "invalidation")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(f"a BUY must carry its whole memo; missing {missing}")
        return self

    def texts(self) -> tuple[str, ...]:
        """Every free-text field, where a citation may appear."""
        out = [self.rationale]
        out += [t for t in (self.thesis, self.catalyst, self.already_priced_in) if t is not None]
        out += list(self.invalidation) + list(self.premortem)
        if self.what_changed is not None:
            out.append(self.what_changed.text)
        out += [a.reason for a in self.adjustments]
        out += [s.description for s in self.scenarios]
        return tuple(out)


class ManagerDecisions(_Strict):
    """The final call's answer: decisions, or the whole-session ``NO_ACTION`` form."""

    no_action: bool
    no_action_reason: Short | None = None
    decisions: tuple[ManagerDecision, ...] = ()

    @model_validator(mode="after")
    def _form(self) -> ManagerDecisions:
        if self.no_action:
            if self.no_action_reason is None:
                raise ValueError("the NO_ACTION form needs its one-line reason")
            if self.decisions:
                raise ValueError("the NO_ACTION form carries no decisions")
        elif not self.decisions:
            raise ValueError(
                "no decisions were returned; return the NO_ACTION form with its reason instead"
            )
        isins = [d.isin for d in self.decisions]
        repeated = sorted({i for i in isins if isins.count(i) > 1})
        if repeated:
            raise ValueError(f"one decision per name; repeated {repeated}")
        return self


_NUMBER: Final = {"type": "number"}
_PROB: Final = {"type": "number", "minimum": 0, "maximum": 1}
_PCT: Final = {"type": "number", "minimum": 0, "maximum": 100}


def _array(items: Mapping[str, Any], max_items: int) -> dict[str, Any]:
    return {"type": "array", "maxItems": max_items, "items": dict(items)}


def decision_schema() -> dict[str, Any]:
    """The final-call JSON Schema: flat, every key required, optional values nullable."""
    decision = _object(
        {
            "isin": _ISIN_SCHEMA,
            "action": _enum(Action),
            "target_weight": _nullable(_PCT),
            "rationale": _string(_MAX_TEXT),
            "horizon_sessions": {"type": "integer", "minimum": 1, "maximum": _MAX_HORIZON},
            "p_beat_bench": _PROB,
            "expected_excess_pct": _NUMBER,
            "stop_pct": _nullable(_PCT),
            "new_stop_pct": _nullable(_PCT),
            "invalidation": _array(_string(), _MAX_LIST),
            "evidence_refs": _array(_string(), 40),
            "what_changed": {
                "anyOf": [
                    _object({"kind": _enum(WhatChangedKind), "text": _string(_MAX_TEXT)}),
                    {"type": "null"},
                ]
            },
            "thesis": _nullable(_string(_MAX_TEXT)),
            "catalyst": _nullable(_string()),
            "already_priced_in": _nullable(_string(_MAX_TEXT)),
            "edge_type": _enum(EdgeType),
            "base_rate_cell": {
                "anyOf": [
                    _object(
                        {
                            "cell_id": _string(),
                            "p_beat": _PROB,
                            "median_excess": _nullable(_NUMBER),
                            "iqr_excess": _nullable(_NUMBER),
                        }
                    ),
                    {"type": "null"},
                ]
            },
            "adjustments": _array(
                _object(
                    {
                        "reason": _string(),
                        "direction": _enum(Direction),
                        "size_pp": _PCT,
                        "citation": _string(),
                    }
                ),
                _MAX_ADJUSTMENTS,
            ),
            "scenarios": _array(
                _object(
                    {
                        "name": _enum(ScenarioName),
                        "probability": _PROB,
                        "excess_pct": _NUMBER,
                        "description": _string(),
                    }
                ),
                3,
            ),
            "cost_hurdle_check": _nullable(_NUMBER),
            "premortem": _array(_string(), _MAX_PREMORTEM),
        }
    )
    return _object(
        {
            "no_action": {"type": "boolean"},
            "no_action_reason": _nullable(_string()),
            "decisions": {"type": "array", "items": decision},
        }
    )


def decision_tool() -> ToolSpec:
    return ToolSpec(
        name=DECISION_TOOL_NAME,
        description=(
            "Return today's decisions: HOLD, TRIM or SELL for every holding and BUY, WATCH or "
            "PASS for every researched candidate, each with its memo — or no_action true with "
            "its one-line reason."
        ),
        input_schema=decision_schema(),
    )


def parse_decisions(
    arguments: Mapping[str, Any],
    *,
    holdings: Sequence[str],
    researched: Sequence[str],
) -> ManagerDecisions:
    """Validate the final call's answer, or raise :class:`MalformedOutputError`.

    Beyond the schema: unless the answer is ``NO_ACTION``, every holding and every researched
    candidate must have a decision (prompt, final round), because a name the manager was asked
    about and silently skipped would vanish from its Brier score.
    """
    try:
        answer = ManagerDecisions.model_validate(dict(arguments))
    except ValidationError as exc:
        raise MalformedOutputError(
            f"the decisions do not match the schema: {_validation_message(exc)}"
        ) from exc
    if not answer.no_action:
        decided = {d.isin for d in answer.decisions}
        missing = [i for i in dict.fromkeys([*holdings, *researched]) if i not in decided]
        if missing:
            raise MalformedOutputError(
                f"every holding and every researched candidate needs a decision; missing {missing}"
            )
    return answer


def schema_bytes(limits: RoundLimits, *, system_prompt: str) -> bytes:
    """The canonical bytes of both schemas, the system prompt and the shape version.

    What `mandate_hash` takes as ``schema_bytes`` for a manager (pre-registration §7): any change
    to a field, a cap or the system prompt is a new manager.
    """
    document = {
        "version": SCHEMA_VERSION,
        "research": research_schema(limits),
        "decision": decision_schema(),
        "system_prompt": system_prompt,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def schema_digest(limits: RoundLimits, *, system_prompt: str) -> str:
    return hashlib.sha256(schema_bytes(limits, system_prompt=system_prompt)).hexdigest()
