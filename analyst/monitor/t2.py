"""A5 · T2 — the scheduled deep review that reads a whole case, on the case's ratified cadence.

§5.4's third tier: on a per-case cadence (monthly by default, from the ratified `MonitoringCadence`)
a strong model reviews the *whole* case rather than a single flagged holding — every thesis, the
cycle/rotation context (sector relative strength, breadth, flows), a theme-development scan, and the
universe refresh A3 produced — and returns three things: a **case health report** with a per-thesis
assessment, **rotation-steering updates**, and a **refreshed candidate bench**. This module is that
tier, and it holds three properties the plan is explicit about — each proved in
`tests/unit/test_t2.py` with an inversion that fails if the logic is reversed:

* **A run produces a case health report with per-thesis assessments and a refreshed bench.** The
  model assesses each ratified thesis (`INTACT / WEAKENED / BROKEN` plus a one-line read), and the
  answer is schema-validated to cover *exactly* the case's theses — a review that skips a thesis or
  invents one is malformed, retried, then escalated, never read loosely. The bench is *not* the
  model's free text: it is derived deterministically from A3's `ThemeMap` (M5.5) — the point-in-time
  candidates that the case does not already hold, ranked by disclosed purity — so a candidate never
  enters the bench that was not in the PIT universe (§4.5, invariant #8's universe analogue).

* **A policy change it recommends becomes a PROPOSAL, never an applied change.** The one policy the
  deep review may want to move is the rotation dial (§5.2 policy 3). If the model recommends a
  different tactical percentage, T2 does not apply it: it builds the next policy-set version via
  `resize_dial` — which returns a `PROPOSAL` carrying `supersedes_version`, leaving the ratified
  version untouched — and journals a `POLICY_PROPOSAL` line. The human ratifies it through the M5.8
  path; T2 proposes, it never ratifies (§5.1, decisions #4/#5/#9). This is AGENTIC_CONTEXT §3.2 at
  this tier.

* **Rotation-steering updates stay inside the ratified dial.** The steering T2 emits — which core
  names to tilt new SIP money toward (§5.5: "new money steering within core = allowed") and where
  the book sits against its sleeve targets — is computed against the **ratified** dial, never the
  percentage the model wished for. `RotationSteering.dial_tactical_pct` is always the ratified one;
  a wished-for change is the proposal path above, not an applied steering. A tilt toward a name the
  case does not hold in the core would be a membership change (A4's ratified-thesis path, decision
  #4), so it is rejected here rather than steered into.

Money is `Decimal` (the token cost and every sleeve target), time comes from an injected `Clock`
(B10: the journal `ts`), identity is ISIN (#2), and nothing here reads the network — under `StubLLM`
the whole tier runs deterministically, which is what this module's tests depend on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from accounting.tokens import MeteredCompletion, MeteredLLM
from analyst.cases.policies import (
    MonitoringCadence,
    PolicySet,
    PolicyStatus,
    RotationDial,
    T2Cadence,
)
from analyst.journal import (
    Actor,
    Decision,
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
    Journal,
    JournalEntry,
    Sleeve,
    TokenSpend,
    Verdict,
)
from analyst.llm import DEFAULT_MODEL, Message, Role
from analyst.mapper import ThemeMap
from analyst.rails import Portfolio
from analyst.rotation import SleeveAllocation, allocate, resize_dial
from analyst.thesis import Thesis
from dataplatform.clock import Clock, SystemClock
from dataplatform.logging import get_logger

__all__ = [
    "SYSTEM_PROMPT",
    "T2_MODEL",
    "T2_PURPOSE",
    "BenchCandidate",
    "CaseHealth",
    "CaseHealthReport",
    "CycleContext",
    "MalformedReviewError",
    "PolicyProposal",
    "RotationSteering",
    "T2Request",
    "T2Review",
    "T2Reviewer",
    "ThesisAssessment",
    "ThesisAssessmentVerdict",
    "build_messages",
    "check_covers_theses",
    "next_due",
    "parse_review",
    "render_review",
]

_LOG = get_logger(__name__)

#: The strong model §5.4 calls for at T2 — the same opus-tier default as T1, priced on the dated
#: card. Named here so a test can compute the prompt digest the reviewer will send and register a
#: deterministic reply under `StubLLM`.
T2_MODEL: Final[str] = DEFAULT_MODEL

#: What the burn report groups this spend under (§5.7). One label per tier, so "what did the deep
#: review cost this quarter" is a single sum.
T2_PURPOSE: Final[str] = "t2_review"

#: Roughly how many days each cadence spans, for `next_due`. A calendar-month step would need
#: month arithmetic the deep review does not otherwise do; the review is scheduled by the daily
#: loop, and this is only the "has enough time passed" question, so a fixed span per cadence is the
#: right amount of precision — the loop, not this number, owns the exact session.
_CADENCE_DAYS: Final[Mapping[T2Cadence, int]] = {
    T2Cadence.WEEKLY: 7,
    T2Cadence.MONTHLY: 30,
    T2Cadence.QUARTERLY: 91,
}


class MalformedReviewError(Exception):
    """The model's answer could not be read as a well-formed T2 review.

    Raised for invalid JSON, a shape the schema rejects, or an assessment set that does not cover
    exactly the case's ratified theses. The caller retries and then escalates — it never guesses
    what the model meant, because a thesis assessment feeds a case-health call the human reads.
    """


def _reject_float(value: Any) -> Any:
    """Refuse a `float` where an exact decimal is required (the `policies.py` rule).

    A recommended tactical percentage is content that becomes a ratifiable proposal; a `float`
    coerced to `Decimal` reads back with a binary tail and would hash a proposal differently than
    the number the human sees. The model is told to answer it as a string.
    """
    if isinstance(value, float):
        raise ValueError(
            f"a percentage must be an exact decimal, got float {value!r}; answer it as a string "
            "(a rail/proposal off by a float epsilon is a change nobody actually approved)"
        )
    return value


#: A percentage in 0..100, exactly as the dial's `Percent` domain constrains it — a recommended
#: value outside that range is a malformed answer, retried then escalated, never silently clamped.
_RecommendedPct = Annotated[Decimal, BeforeValidator(_reject_float)]


def _reject_float_value(name: str, value: object) -> None:
    """Refuse a float where the cycle context expects an exact decimal (CLAUDE.md).

    Typed `object` rather than `Decimal` so the guard is not dead code to the type checker: it has
    to run against whatever a caller actually passed, the same reason `sleeves._require_case_value`
    takes `object`.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{name} must be a Decimal, never a float; a prompt rendered over a float does not "
            "hash reproducibly (CLAUDE.md)"
        )


# ── cadence ──────────────────────────────────────────────────────────────────────────────────────


def next_due(cadence: T2Cadence, last_review: date | None, *, on: date) -> bool:
    """Whether a T2 deep review is due on `on`, given when it last ran (§5.4's per-case cadence).

    What it does: answers the scheduling question the daily loop asks before it assembles a
    `T2Request` — enough time has passed since `last_review` for this case's ratified cadence
    (monthly by default). A case that has never had a deep review (`last_review is None`) is due.
    What it assumes: `on` is the session being considered and `last_review` is the trading date of
    the previous T2 run, both in Asia/Kolkata; the daily loop owns the exact session, this owns only
    "has the interval elapsed".
    What it never does: run the review, or read a clock — the dates are arguments, so the same call
    replays identically (B10).
    """
    if last_review is None:
        return True
    return (on - last_review).days >= _CADENCE_DAYS[cadence]


# ── the strong-model answer (schema-validated) ───────────────────────────────────────────────────


class CaseHealth(StrEnum):
    """The overall health of a case at one deep review — the report's headline (§5.4)."""

    GREEN = "GREEN"
    """Every thesis intact and the cycle context supportive; nothing needs a human eye."""

    AMBER = "AMBER"
    """Something is weakening — a thesis, the cycle, or a rail drifting — watch, do not act yet."""

    RED = "RED"
    """A thesis broke, or the review could not be completed; escalated to the human."""


class ThesisAssessmentVerdict(BaseModel):
    """One thesis, as the T2 model judged it in the deep review (§5.4's per-thesis assessment).

    `isin` ties the assessment to a ratified core thesis, because the journal records the review
    *per instrument* and a review of an unnamed holding cannot be checked against the outcome later
    (§5.7). The verdict is the same closed vocabulary T1 uses, so "T1/T2 verdicts vs subsequent
    outcomes" (§5.7's decision review) reads one scale across both tiers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(
        pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$",
        description="The core holding assessed — ISIN, the only join key (#2).",
    )
    verdict: Verdict = Field(description="INTACT / WEAKENED / BROKEN, the state of the thesis.")
    assessment: str = Field(
        min_length=1, description="The one-line read behind the verdict — the 'because'."
    )


class T2Review(BaseModel):
    """The whole T2 answer: per-thesis assessments, the cycle read, and the steering it wants.

    What it does: carries the structured deep review a strong model returned over the whole case.
    What it assumes: `assessments` names exactly the case's ratified theses — enforced not here but
    in `check_covers_theses`, which has the theses to compare against. `recommended_tactical_pct` is
    the dial the model *wishes* for; whether it is applied is decided in code (it never is — it
    becomes a proposal).
    What it never does: exist in a shape the schema does not accept. `extra="forbid"` means an
    invented field is a parse failure, not silently dropped context.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_health: CaseHealth = Field(description="GREEN / AMBER / RED — the report headline.")
    assessments: tuple[ThesisAssessmentVerdict, ...] = Field(
        default=(), description="One assessment per ratified core thesis (§5.4)."
    )
    cycle_read: str = Field(
        min_length=1, description="One line on sector RS, breadth and flows — the cycle context."
    )
    theme_development: str = Field(
        min_length=1, description="One line on how the theme has developed since the last review."
    )
    steering_rationale: str = Field(
        min_length=1, description="One line on where new SIP money should tilt within the core."
    )
    steering_tilts: tuple[str, ...] = Field(
        default=(),
        description="Core ISINs to steer new SIP money toward — must be names already held.",
    )
    recommended_tactical_pct: _RecommendedPct | None = Field(
        default=None,
        ge=0,
        le=100,
        description="A dial the model wishes for, as a decimal string; becomes a PROPOSAL, never "
        "an applied change. Null when the model is content with the ratified dial.",
    )

    @model_validator(mode="after")
    def _assessment_isins_unique(self) -> T2Review:
        isins = [a.isin for a in self.assessments]
        duplicates = sorted({i for i in isins if isins.count(i) > 1})
        if duplicates:
            raise ValueError(
                f"a thesis was assessed twice: {', '.join(duplicates)}; one assessment per holding"
            )
        return self

    @property
    def broken(self) -> tuple[ThesisAssessmentVerdict, ...]:
        """The theses the model judged BROKEN — each escalated to the human/A7 (§5.5)."""
        return tuple(a for a in self.assessments if a.verdict is Verdict.BROKEN)


def _strip_code_fence(text: str) -> str:
    """Drop a leading/trailing ``` fence a model may wrap JSON in, leaving the JSON body.

    Tolerant of exactly the one thing a model routinely adds — a ```json … ``` wrapper — and
    nothing more: anything else that is not JSON stays not-JSON and is caught as malformed.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def parse_review(text: str) -> T2Review:
    """Parse a model's raw answer into a schema-validated `T2Review`.

    What it does: strips a code fence if the model added one, then validates the JSON against the
    schema.
    What it never does: guess. Invalid JSON or a shape the schema rejects raises
    `MalformedReviewError` with the reason, so the caller retries or escalates rather than reading a
    half-formed answer as a review.
    """
    body = _strip_code_fence(text)
    if not body:
        raise MalformedReviewError("the model returned an empty answer; there is no review to read")
    try:
        return T2Review.model_validate_json(body)
    except ValidationError as error:
        raise MalformedReviewError(
            f"the model's answer is not a well-formed deep review: {error}"
        ) from error


def check_covers_theses(review: T2Review, theses: Sequence[Thesis]) -> None:
    """Refuse a review that does not assess exactly the case's ratified theses.

    What it does: compares the ISINs the model assessed against the case's theses, and raises if
    they differ in either direction.
    What it never does: accept a review that skipped a thesis or invented one. A skipped thesis is a
    holding the review never looked at; an invented one is an assessment of nothing. Both are
    `MalformedReviewError`, retried then escalated — not read as a partial review.
    """
    expected = {thesis.isin for thesis in theses}
    got = {a.isin for a in review.assessments}
    if got == expected:
        return
    missing = sorted(expected - got)
    extra = sorted(got - expected)
    parts: list[str] = []
    if missing:
        parts.append(f"did not assess {', '.join(missing)}")
    if extra:
        parts.append(f"assessed unknown holding(s) {', '.join(extra)}")
    raise MalformedReviewError(
        "the review does not cover the case's theses exactly: " + "; ".join(parts)
    )


# ── the request and its cycle context ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CycleContext:
    """The cycle/rotation context a deep review reads: sector RS, breadth, flows (§5.4).

    What it does: carry the market-cycle facts the model is shown alongside the theses — each
    sector's relative strength, a market-breadth reading, and a one-line note on flows — so the
    review's cycle read is grounded in supplied facts rather than the model's memory.
    What it assumes: `sector_rs` pairs a sector name with a relative-strength reading (a `Decimal`,
    not money but exact so the rendered prompt is reproducible); it is rendered sorted, so the order
    a caller built it in never changes the prompt digest.
    What it never does: carry a float. Relative strength and breadth are exact decimals for the same
    reason every number the journal touches is (a content-addressed prompt over a float is not
    reproducible).
    """

    sector_rs: tuple[tuple[str, Decimal], ...]
    breadth_pct: Decimal
    flows_note: str

    def __post_init__(self) -> None:
        for name, value in self.sector_rs:
            _reject_float_value(f"sector RS for {name!r}", value)
        _reject_float_value("breadth_pct", self.breadth_pct)


@dataclass(frozen=True, slots=True)
class T2Request:
    """Everything one deep review reads: the ratified policy set, the theses, the book, the context.

    What it does: pairs the case's *ratified* policy set (the dial and cadence the review steers
    inside) with the ratified core theses, the current book and tactical membership, the cycle
    context, and A3's universe refresh (`ThemeMap`, M5.5) — the whole case §5.4 says T2 reviews.
    What it assumes: `policy_set` is RATIFIED (steering off an unratified dial would steer inside a
    mix nobody approved) and every thesis is ratified and belongs to `case_id`; `tactical_isins` is
    the current tactical membership (the daily loop knows it from the sleeve tags on the orders that
    opened the positions).
    What it never does: hold a broker or a rail. T2 reviews and proposes; A6/A7/A8 dispose.
    """

    policy_set: PolicySet
    trading_date: date
    theses: tuple[Thesis, ...]
    portfolio: Portfolio
    tactical_isins: frozenset[str]
    cycle: CycleContext
    theme_map: ThemeMap

    def __post_init__(self) -> None:
        if self.policy_set.status is not PolicyStatus.RATIFIED:
            raise ValueError(
                f"a deep review steers inside a RATIFIED policy set, got "
                f"{self.policy_set.status.value} version {self.policy_set.version}: the dial it "
                "steers within must be a mix a human approved (§5.5)"
            )
        for thesis in self.theses:
            if not thesis.is_ratified:
                raise ValueError(
                    f"thesis for {thesis.isin} is {thesis.status.value}, not RATIFIED: T2 reviews "
                    "the ratified theses in force, and a core buy is impossible without one (§5.3)"
                )
            if thesis.case_id != self.policy_set.case_id:
                raise ValueError(
                    f"thesis for {thesis.isin} belongs to case {thesis.case_id!r}, not "
                    f"{self.policy_set.case_id!r}: a deep review is of one case"
                )

    @property
    def case_id(self) -> str:
        """The case under review — from the ratified policy set."""
        return self.policy_set.case_id

    @property
    def cadence(self) -> T2Cadence:
        """The ratified deep-review cadence (§5.2 policy 7)."""
        monitoring: MonitoringCadence = self.policy_set.monitoring
        return monitoring.t2_cadence

    @property
    def dial(self) -> RotationDial:
        """The ratified rotation dial (§5.2 policy 3) — what steering stays inside."""
        return self.policy_set.rotation_dial


# ── the prompt (deterministic, so a StubLLM reply keys on its digest) ────────────────────────────


#: The system prompt that fixes the model's role and its answer schema. Part of the prompt digest,
#: so it is a module constant a test can reference verbatim.
SYSTEM_PROMPT: Final[str] = (
    "You are the T2 reviewer of an equity monitoring system. You are shown a whole case on its "
    "scheduled deep-review cadence: every ratified core thesis, the book, the cycle context "
    "(sector relative strength, market breadth, flows), and the refreshed theme value chain with "
    "its listed proxies. Assess the case as a whole.\n\n"
    "Answer with a single JSON object and nothing else, in this shape:\n"
    "{\n"
    '  "case_health": "GREEN|AMBER|RED",\n'
    '  "assessments": [ {"isin": "<holding ISIN>", "verdict": "INTACT|WEAKENED|BROKEN", '
    '"assessment": "<one line>"} ],\n'
    '  "cycle_read": "<one line on sector RS, breadth, flows>",\n'
    '  "theme_development": "<one line on how the theme has developed>",\n'
    '  "steering_rationale": "<one line on where new SIP money should tilt>",\n'
    '  "steering_tilts": ["<core ISIN already held>", ...],\n'
    '  "recommended_tactical_pct": "<decimal string, or null to keep the ratified dial>"\n'
    "}\n\n"
    "Assess exactly the theses shown — one assessment per holding, no more, no fewer. Only tilt "
    "toward names the case already holds in the core; adding a name is a separate ratified-thesis "
    "step. Any change to the rotation dial is a proposal for a human to ratify, never applied — "
    "state it as recommended_tactical_pct and the system will route it to ratification."
)

#: The one-line instruction appended in the user turn, restating the output contract at the point
#: the model answers.
_USER_INSTRUCTION: Final[str] = (
    "\n\nReturn only the JSON deep-review object described in the system prompt."
)


def render_review(request: T2Request) -> str:
    """Render the case into the brief the deep review reads — deterministic, so it hashes stably.

    What it does: lays out the ratified dial and cadence, each ratified thesis with its break
    conditions, the current book, the cycle context (sorted), and the refreshed theme value chain
    with its top proxies — everything §5.4 says the deep review considers, in a fixed order so the
    same case always renders to the same bytes (and the same `StubLLM` digest).
    What it never does: read a clock or the network. The `ThemeMap` is passed in already resolved.
    """
    lines: list[str] = []
    lines.append(f"Case: {request.case_id}")
    lines.append(f"Review date: {request.trading_date.isoformat()}")
    lines.append(f"Cadence: {request.cadence.value}")
    lines.append(f"Ratified rotation dial: tactical {request.dial.tactical_pct}% of case value")
    lines.append("")

    lines.append("Theses (ratified core holdings):")
    for thesis in sorted(request.theses, key=lambda t: t.isin):
        lines.append(f"- {thesis.isin} (purity {thesis.theme_purity}): {thesis.driver}")
        for bc in thesis.break_conditions:
            lines.append(f"    [{bc.id}/{bc.type.value}/{bc.evaluation_tier.value}] {bc.condition}")
    lines.append("")

    portfolio = request.portfolio
    lines.append(f"Book (case value {portfolio.total_value}, cash {portfolio.cash}):")
    for lot in sorted(portfolio.lots, key=lambda lot: lot.isin):
        sleeve = "TACTICAL" if lot.isin in request.tactical_isins else "CORE"
        lines.append(
            f"- {lot.isin} [{sleeve}] sector {lot.sector}: {lot.quantity} @ {lot.price} "
            f"= {lot.value}"
        )
    lines.append("")

    lines.append("Cycle context:")
    lines.append(f"- market breadth: {request.cycle.breadth_pct}%")
    lines.append(f"- flows: {request.cycle.flows_note}")
    for sector, rs in sorted(request.cycle.sector_rs):
        lines.append(f"- sector RS {sector}: {rs}")
    lines.append("")

    theme_map = request.theme_map
    lines.append(
        f"Theme refresh — {theme_map.theme!r} as of {theme_map.as_of.isoformat()} "
        f"(universe {theme_map.universe_size} names):"
    )
    for stage in theme_map.value_chain:
        lines.append(f"- stage {stage.name}: {stage.description}")
    for candidate in theme_map.candidates:
        lines.append(
            f"- candidate {candidate.isin} ({candidate.name}) in {candidate.value_chain_stage}, "
            f"purity {candidate.purity.score}"
        )
    return "\n".join(lines)


def build_messages(rendered_prompt: str) -> tuple[Message, ...]:
    """The conversation a deep review sends — the rendered case as the single user turn.

    Exposed (not private) so a test can reconstruct the exact request and register a deterministic
    `StubLLM` reply against its digest — the prompt is `build_messages(render_review(...))` +
    `T2_MODEL` + `SYSTEM_PROMPT`, which is what `prompt_digest` hashes.
    """
    return (Message(role=Role.USER, content=rendered_prompt + _USER_INSTRUCTION),)


# ── the deterministic outputs (derived in code, not trusted from the model) ──────────────────────


@dataclass(frozen=True, slots=True)
class ThesisAssessment:
    """One thesis's assessment in the case health report — the model's verdict, kept for the record.

    What it does: pair a holding's ISIN with the verdict and one-line read the deep review returned,
    and the thesis's ratified theme purity for context.
    What it never does: change core membership — a BROKEN assessment is *escalated* (to the human /
    A7's exit path), it is not an exit T2 places (§5.5, invariant #6).
    """

    isin: str
    verdict: Verdict
    assessment: str
    theme_purity: Decimal


@dataclass(frozen=True, slots=True)
class BenchCandidate:
    """One name on the refreshed candidate bench: an A3 proxy the case does not yet hold (§5.4).

    What it does: carry a candidate the deep review surfaces for future consideration — its ISIN,
    display name, disclosed purity and value-chain stage — drawn from A3's `ThemeMap` (M5.5).
    What it assumes: it was confirmed inside the PIT universe as of the theme map's date (the mapper
    dropped the rest) and the case does not already hold it (T2 filters held names from the bench).
    What it never does: become a buy — the bench is a watchlist a human reviews, and adding a core
    name is A4's ratified-thesis path (decision #4).
    """

    isin: str
    name: str
    purity: Decimal
    value_chain_stage: str


def _refresh_bench(theme_map: ThemeMap, held: frozenset[str]) -> tuple[BenchCandidate, ...]:
    """The refreshed bench: A3's candidates the case does not hold, ranked by disclosed purity.

    What it does: drop every candidate the book already holds and rank the rest by purity (highest
    first, ISIN breaking ties), so the bench is exactly the surviving PIT candidates worth watching.
    What it never does: widen the universe — the bench can only contain names the mapper kept inside
    the PIT universe (§4.5), so a hardcoded name can never appear here (M5.5's acceptance 3, carried
    through the T2 tier).
    """
    return tuple(
        sorted(
            (
                BenchCandidate(
                    isin=candidate.isin,
                    name=candidate.name,
                    purity=candidate.purity.score,
                    value_chain_stage=candidate.value_chain_stage,
                )
                for candidate in theme_map.candidates
                if candidate.isin not in held
            ),
            key=lambda bench: (-bench.purity, bench.isin),
        )
    )


@dataclass(frozen=True, slots=True)
class RotationSteering:
    """Rotation-steering updates from a deep review, bounded by the ratified dial (§5.5).

    What it does: report where the book sits against its sleeve targets and which core names to tilt
    new SIP money toward — `dial_tactical_pct` is the **ratified** percentage the steering operates
    under, never the one the model wished for. New money steering within the core is what §5.5
    permits without a governance act; resizing the sleeve boundary is a policy change, which reaches
    the report as a `PolicyProposal`, not as steering.
    What it assumes: `allocation` was computed against the ratified dial and the current tactical
    membership; `core_tilts` are names the case already holds in the core (a tilt toward a name it
    does not hold would be a membership change, refused before this is built).
    What it never does: move the dial. `dial_tactical_pct` equalling the ratified dial is the whole
    of "steering stays inside the ratified dial" (acceptance criterion 3).
    """

    dial_tactical_pct: Decimal
    allocation: SleeveAllocation
    core_tilts: tuple[str, ...]
    rationale: str

    @property
    def tactical_target_inr(self) -> Decimal:
        """The rupee tactical target the ratified dial implies — the ceiling steering respects."""
        return self.allocation.targets.tactical_target

    @property
    def tactical_drift_inr(self) -> Decimal:
        """Where the tactical sleeve sits against its target: positive is over, negative is room."""
        return self.allocation.tactical_drift


@dataclass(frozen=True, slots=True)
class PolicyProposal:
    """A policy change the deep review recommends — as a PROPOSAL, never an applied change (§3.2).

    What it does: carry the proposed next policy-set version (in `PROPOSAL`, superseding the
    ratified one) alongside the before/after the human reviews and the id of the `POLICY_PROPOSAL`
    journal line that recorded it.
    What it assumes: `proposed_policy_set` came from `resize_dial` on the ratified set, so it holds
    `supersedes_version` and drops the ratification — the ratified version is untouched.
    What it never does: ratify. Proposing is never ratifying (§5.1); the human ratifies through the
    M5.8 path, and only then may a new engine run on it.
    """

    kind: str
    proposed_policy_set: PolicySet
    from_tactical_pct: Decimal
    to_tactical_pct: Decimal
    rationale: str
    journal_entry_id: int


@dataclass(frozen=True, slots=True)
class CaseHealthReport:
    """One deep review's output: case health, per-thesis assessments, steering, bench, proposals.

    What it does: gather everything §5.4 says T2 produces — the health headline and per-thesis
    assessments, the rotation-steering updates (inside the ratified dial), the refreshed candidate
    bench, and any policy changes the review wants (each a proposal, never applied) — plus the model
    and token/rupee cost the review was produced at (§5.7).
    `escalated` is True when a thesis broke (handed to the human / A7) or the model's answer was
    malformed after retries (handed to the human). `token_spend` sums every attempt, so the burn
    report never loses the cost of a retried call.
    """

    case_id: str
    trading_date: date
    cadence: T2Cadence
    health: CaseHealth
    thesis_assessments: tuple[ThesisAssessment, ...]
    steering: RotationSteering
    bench: tuple[BenchCandidate, ...]
    proposals: tuple[PolicyProposal, ...]
    cycle_read: str
    theme_development: str
    escalated: bool
    journal_entry_ids: tuple[int, ...]
    token_spend: TokenSpend
    model: str
    attempts: int
    rejection: str | None = None

    @property
    def broken_theses(self) -> tuple[str, ...]:
        """ISINs the review judged BROKEN — each escalated for the exit path (§5.5)."""
        return tuple(a.isin for a in self.thesis_assessments if a.verdict is Verdict.BROKEN)


# ── the reviewer ─────────────────────────────────────────────────────────────────────────────────


class T2Reviewer:
    """§5.4's T2 tier wired to the metered model, the journal, the mapper refresh and the dial.

    What it does: on `review()`, snapshots the case evidence, asks the metered model for a
    `T2Review` (retrying a malformed answer up to `max_attempts`, then escalating it), derives the
    bench and steering deterministically, routes any recommended dial change to a proposal, and
    journals the run — a per-thesis line for each assessment (ESCALATE on a break, else HOLD), a
    `POLICY_PROPOSAL` for each proposal, and a `HEARTBEAT` summarizing the review.
    What it assumes: the caller owns the transaction (the `Journal` never commits), the `MeteredLLM`
    is built on the same dated price card the burn report reads, and `clock` is injected (B10).
    What it never does: apply a policy change, place an order, touch a rail, or read a malformed
    answer as a review — a change the model wants is a proposal, a break is an escalation, and an
    unparseable answer is handed to the human, never guessed.
    """

    __slots__ = ("_clock", "_journal", "_llm", "_max_attempts", "_model")

    def __init__(
        self,
        llm: MeteredLLM,
        journal: Journal,
        *,
        clock: Clock | None = None,
        model: str = T2_MODEL,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
        self._llm = llm
        self._journal = journal
        self._clock = SystemClock() if clock is None else clock
        self._model = model
        self._max_attempts = max_attempts

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._model!r}, max_attempts={self._max_attempts})"

    def review(self, request: T2Request) -> CaseHealthReport:
        """Run the deep review for one case and record its outcome (§5.4).

        Retries a malformed model answer up to `max_attempts`, then escalates it; derives the bench
        and steering in code (never from the model's free text); routes any recommended dial change
        to a `PROPOSAL`; and journals the run. The bench and steering are produced even on the
        malformed path, because they need no model — only the per-thesis assessments and the health
        headline are lost when the answer will not parse.
        """
        held = frozenset(lot.isin for lot in request.portfolio.lots)
        bench = _refresh_bench(request.theme_map, held)
        allocation = allocate(
            request.portfolio, request.dial, tactical_isins=request.tactical_isins
        )

        rendered = render_review(request)
        ref = self._journal.snapshot(self._evidence(request, rendered, bench)).ref
        messages = build_messages(rendered)

        review, token_spend, model_used, attempts, malformed = self._ask(request, messages)

        if review is None:
            steering = RotationSteering(
                dial_tactical_pct=request.dial.tactical_pct,
                allocation=allocation,
                core_tilts=(),
                rationale="review could not be completed; steering held at the ratified dial",
            )
            return self._escalate_malformed(
                request,
                ref=ref,
                reason=malformed or "no answer",
                steering=steering,
                bench=bench,
                token_spend=token_spend,
                model=model_used,
                attempts=attempts,
            )

        return self._record(
            request,
            review=review,
            ref=ref,
            allocation=allocation,
            bench=bench,
            token_spend=token_spend,
            model=model_used,
            attempts=attempts,
        )

    # ── the model call ───────────────────────────────────────────────────────────────────────────

    def _ask(
        self, request: T2Request, messages: tuple[Message, ...]
    ) -> tuple[T2Review | None, TokenSpend, str, int, str | None]:
        """Ask the metered model for a review, retrying a malformed answer; sum each attempt's cost.

        Returns the parsed review (or None after the last attempt), the summed token spend across
        every attempt (a retried call's cost is spend that happened and must not vanish), the model
        the calls were billed to, how many attempts were made, and the last malformed reason.
        """
        total_in = 0
        total_out = 0
        total_cost = Decimal(0)
        model_used = self._model
        malformed: str | None = None
        review: T2Review | None = None
        attempts = 0

        for attempt in range(1, self._max_attempts + 1):
            attempts = attempt
            completion = self._llm.complete(
                messages,
                model=self._model,
                purpose=T2_PURPOSE,
                system=SYSTEM_PROMPT,
                case_id=request.case_id,
                on=request.trading_date,
            )
            spend = _spend_of(completion)
            total_in += spend.tokens_in
            total_out += spend.tokens_out
            total_cost += spend.cost_inr
            model_used = completion.priced.model
            try:
                candidate = parse_review(completion.text)
                check_covers_theses(candidate, request.theses)
                _validate_tilts(candidate, request)
            except MalformedReviewError as error:
                malformed = str(error)
                _LOG.warning(
                    "t2.malformed",
                    case_id=request.case_id,
                    trading_date=request.trading_date.isoformat(),
                    attempt=attempt,
                    reason=malformed,
                )
                continue
            review = candidate
            break

        token_spend = TokenSpend(tokens_in=total_in, tokens_out=total_out, cost_inr=total_cost)
        return review, token_spend, model_used, attempts, malformed

    # ── recording an in-policy review ─────────────────────────────────────────────────────────────

    def _record(
        self,
        request: T2Request,
        *,
        review: T2Review,
        ref: str,
        allocation: SleeveAllocation,
        bench: tuple[BenchCandidate, ...],
        token_spend: TokenSpend,
        model: str,
        attempts: int,
    ) -> CaseHealthReport:
        """Journal the deep review and assemble the case health report."""
        purity_by_isin = {thesis.isin: thesis.theme_purity for thesis in request.theses}
        assessments = tuple(
            ThesisAssessment(
                isin=a.isin,
                verdict=a.verdict,
                assessment=a.assessment,
                theme_purity=purity_by_isin[a.isin],
            )
            for a in review.assessments
        )

        steering = RotationSteering(
            dial_tactical_pct=request.dial.tactical_pct,
            allocation=allocation,
            core_tilts=review.steering_tilts,
            rationale=review.steering_rationale,
        )

        entry_ids: list[int] = []

        # A HEARTBEAT summarizing the run — invariant #9: the review is recorded with what it saw.
        summary = self._journal.heartbeat(
            self._evidence(request, render_review(request), bench),
            trading_date=request.trading_date,
            case_id=request.case_id,
            actor=Actor.T2,
            rationale=(
                f"T2 deep review ({request.cadence.value}): case {review.case_health.value}. "
                f"Cycle: {review.cycle_read} Theme: {review.theme_development}"
            ),
        )
        entry_ids.append(summary.id)

        # One line per thesis assessment: a break is escalated (to the human / A7), the rest HOLD.
        for a in review.assessments:
            broken = a.verdict is Verdict.BROKEN
            entry = self._journal.append(
                JournalEntry(
                    ts=self._clock.now(),
                    trading_date=request.trading_date,
                    case_id=request.case_id,
                    actor=Actor.T2,
                    decision=Decision.ESCALATE if broken else Decision.HOLD,
                    isin=a.isin,
                    sleeve=Sleeve.CORE,
                    evidence_snapshot_ref=ref,
                    rationale=f"T2 {a.verdict.value}: {a.assessment}",
                )
            )
            entry_ids.append(entry.id)

        # Any recommended dial change becomes a PROPOSAL — never an applied change (§3.2).
        proposals = self._propose_dial(
            request, review, ref=ref, model=model, token_spend=token_spend
        )
        entry_ids.extend(proposal.journal_entry_id for proposal in proposals)

        escalated = bool(review.broken)
        _LOG.info(
            "t2.reviewed",
            case_id=request.case_id,
            trading_date=request.trading_date.isoformat(),
            health=review.case_health.value,
            theses=len(assessments),
            broken=len(review.broken),
            bench=len(bench),
            proposals=len(proposals),
            cost_inr=str(token_spend.cost_inr),
        )
        return CaseHealthReport(
            case_id=request.case_id,
            trading_date=request.trading_date,
            cadence=request.cadence,
            health=review.case_health,
            thesis_assessments=assessments,
            steering=steering,
            bench=bench,
            proposals=proposals,
            cycle_read=review.cycle_read,
            theme_development=review.theme_development,
            escalated=escalated,
            journal_entry_ids=tuple(entry_ids),
            token_spend=token_spend,
            model=model,
            attempts=attempts,
        )

    def _propose_dial(
        self,
        request: T2Request,
        review: T2Review,
        *,
        ref: str,
        model: str,
        token_spend: TokenSpend,
    ) -> tuple[PolicyProposal, ...]:
        """Turn a recommended dial into a PROPOSAL journal line, or nothing if the dial is unmoved.

        The ratified set is never edited: `resize_dial` returns the next version in `PROPOSAL`
        carrying `supersedes_version`. A recommendation equal to the ratified dial is not a change
        and produces no proposal.
        """
        wanted = review.recommended_tactical_pct
        current = request.dial.tactical_pct
        if wanted is None or wanted == current:
            return ()

        proposed = resize_dial(request.policy_set, wanted)
        rationale = (
            f"T2 recommends resizing the tactical dial from {current}% to {wanted}% "
            f"(case {review.case_health.value}; {review.cycle_read}); proposed for ratification, "
            "not applied"
        )
        entry = self._journal.append(
            JournalEntry(
                ts=self._clock.now(),
                trading_date=request.trading_date,
                case_id=request.case_id,
                actor=Actor.T2,
                decision=Decision.POLICY_PROPOSAL,
                evidence_snapshot_ref=ref,
                rationale=rationale,
                model=model,
                tokens=token_spend,
                payload={
                    "policy": "rotation_dial",
                    "from_tactical_pct": str(current),
                    "to_tactical_pct": str(wanted),
                    "proposed_version": str(proposed.version),
                    "supersedes_version": str(request.policy_set.version),
                    "status": proposed.status.value,
                },
            )
        )
        _LOG.info(
            "t2.dial_proposal",
            case_id=request.case_id,
            from_pct=str(current),
            to_pct=str(wanted),
            proposed_version=proposed.version,
            entry_id=entry.id,
        )
        return (
            PolicyProposal(
                kind="rotation_dial",
                proposed_policy_set=proposed,
                from_tactical_pct=current,
                to_tactical_pct=wanted,
                rationale=rationale,
                journal_entry_id=entry.id,
            ),
        )

    # ── the malformed-escalate ending ─────────────────────────────────────────────────────────────

    def _escalate_malformed(
        self,
        request: T2Request,
        *,
        ref: str,
        reason: str,
        steering: RotationSteering,
        bench: tuple[BenchCandidate, ...],
        token_spend: TokenSpend,
        model: str,
        attempts: int,
    ) -> CaseHealthReport:
        """Journal an ESCALATE when the model would not return a parseable review after retries.

        No per-thesis assessments are recorded — none parsed — but the token cost of the failed
        attempts is, because it is spend that happened. The bench and steering (both deterministic)
        still populate the report; the human is handed the raw failure.
        """
        rationale = (
            f"T2 model returned a malformed deep review after {attempts} attempt(s); escalated to "
            f"the human. Last reason: {reason}"
        )
        entry = self._journal.append(
            JournalEntry(
                ts=self._clock.now(),
                trading_date=request.trading_date,
                case_id=request.case_id,
                actor=Actor.T2,
                decision=Decision.ESCALATE,
                evidence_snapshot_ref=ref,
                rationale=rationale,
                model=model,
                tokens=token_spend,
                payload={"malformed": reason, "attempts": str(attempts)},
            )
        )
        _LOG.error(
            "t2.escalated_malformed",
            case_id=request.case_id,
            trading_date=request.trading_date.isoformat(),
            attempts=attempts,
            reason=reason,
            entry_id=entry.id,
        )
        return CaseHealthReport(
            case_id=request.case_id,
            trading_date=request.trading_date,
            cadence=request.cadence,
            health=CaseHealth.RED,
            thesis_assessments=(),
            steering=steering,
            bench=bench,
            proposals=(),
            cycle_read="unavailable: the deep review could not be parsed",
            theme_development="unavailable: the deep review could not be parsed",
            escalated=True,
            journal_entry_ids=(entry.id,),
            token_spend=token_spend,
            model=model,
            attempts=attempts,
            rejection=reason,
        )

    # ── evidence ─────────────────────────────────────────────────────────────────────────────────

    def _evidence(
        self, request: T2Request, rendered_prompt: str, bench: tuple[BenchCandidate, ...]
    ) -> EvidenceBundle:
        """The bundle the review was made on — the theses, the book, the cycle, the refreshed bench.

        Always carries at least the STATUS item (the review's own context), so a bundle over a case
        with no bench candidate still satisfies `EvidenceBundle`'s min-one-item rule and records
        what the review saw. The rendered prompt is attached verbatim, so the exact model input is
        reconstructable.
        """
        items: list[EvidenceItem] = [
            EvidenceItem(
                kind=EvidenceKind.STATUS,
                source="t2:context",
                label="deep_review",
                as_of=request.trading_date,
                text=(
                    f"cadence {request.cadence.value}, dial tactical "
                    f"{request.dial.tactical_pct}%, breadth {request.cycle.breadth_pct}%, "
                    f"flows: {request.cycle.flows_note}"
                ),
                detail={
                    "theme": request.theme_map.theme,
                    "theme_as_of": request.theme_map.as_of.isoformat(),
                    "universe_size": str(request.theme_map.universe_size),
                },
            ),
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="book",
                label="case_value",
                as_of=request.trading_date,
                value=request.portfolio.total_value,
                detail={"holding_count": str(request.portfolio.holding_count)},
            ),
        ]
        for thesis in sorted(request.theses, key=lambda t: t.isin):
            items.append(
                EvidenceItem(
                    kind=EvidenceKind.THESIS,
                    source="A4 thesis",
                    label=f"{thesis.isin}:driver",
                    isin=thesis.isin,
                    as_of=request.trading_date,
                    value=thesis.theme_purity,
                    text=thesis.driver,
                    detail={"version": str(thesis.version)},
                )
            )
        for candidate in bench:
            items.append(
                EvidenceItem(
                    kind=EvidenceKind.FUNDAMENTAL,
                    source="A3 theme mapper",
                    label=f"{candidate.isin}:bench_purity",
                    isin=candidate.isin,
                    as_of=request.theme_map.as_of,
                    value=candidate.purity,
                    detail={"value_chain_stage": candidate.value_chain_stage},
                )
            )
        return EvidenceBundle(
            case_id=request.case_id,
            trading_date=request.trading_date,
            actor=Actor.T2,
            rendered_prompt=rendered_prompt,
            items=tuple(items),
        )


def _spend_of(completion: MeteredCompletion) -> TokenSpend:
    """The token/rupee cost of one metered call, as the journal records it."""
    return completion.priced.token_spend


def _validate_tilts(review: T2Review, request: T2Request) -> None:
    """Refuse a steering tilt toward a name the case does not hold in the core (§5.5).

    New SIP money steering within the core is allowed; adding a name is A4's ratified-thesis path,
    not a rotation tilt (decision #4). A tilt toward a name that is not a held core holding is
    therefore treated as a malformed review — the model may steer, but it may not smuggle a
    membership change through the steering field.
    """
    core_held = {
        lot.isin for lot in request.portfolio.lots if lot.isin not in request.tactical_isins
    }
    stray = sorted(isin for isin in review.steering_tilts if isin not in core_held)
    if stray:
        raise MalformedReviewError(
            f"steering tilts toward {', '.join(stray)}, which the case does not hold in the core: "
            "new money steers within the core, and adding a name is a ratified-thesis step, not a "
            "tilt (§5.5, decision #4)"
        )
