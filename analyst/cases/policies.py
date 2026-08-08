"""A1: the §5.2 ratified policy set — seven policies, versioned, immutable once ratified.

A case is only allowed to act inside a policy set a human ratified. This module is that object:
the seven policies of §5.2 (capital plan, horizon and benchmarks, rotation dial, risk rails, exit
menu, cash policy, monitoring cadence), carried as one versioned document rather than seven
settings, because §5.1 says the whole thing is ratified in **one** act — "one document, one
ratification" — and a set assembled from seven separate approvals is not the thing the plan
describes.

Three rules are enforced here rather than left to the service:

* **Immutable once ratified.** Every model is `frozen`, so an assignment raises rather than
  silently editing a ratified policy. A change is `revise()`, which returns a *new* version in
  `PROPOSAL` carrying `supersedes_version`, leaving the ratified version exactly as it was. This
  is the object half of decisions #4/#5/#9; the database half is `policy_set`'s append-only
  trigger, and the service refuses to write a version number that already exists.
* **A ratification pins content, not intent.** `Ratification.content_hash` is the sha256 of the
  canonical bytes of the seven policies. It is checked against the set it is attached to, so a
  ratification cannot travel from the document that was reviewed to a document that was not —
  which is exactly what M5.8's "cannot ratify a proposal that changed since it was displayed"
  criterion needs, and what makes the ratification record a governance artifact rather than a
  timestamp.
* **A fixture ratification is not a human one.** `RatificationKind.FIXTURE` is B9's reference-case
  path: it exists so M5 can be built and self-tested without blocking on a live ratification, and
  `funds_real_money` is False on it forever. The refusal itself lives in `lifecycle.py`, where
  funding happens.

Money is `Decimal` (CLAUDE.md) and percentages are `Decimal` in 0..100, mirroring the `percent`
domain in `0001_init.sql` — a rail comparison that is off by a float epsilon is a rail that did
not hold. Nothing here reads a clock, a database or the network: `Ratification.at` is supplied by
the caller from an injected `Clock` (B10).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from analyst.journal import Money, canonical_bytes, digest_of

__all__ = [
    "POLICY_FIELDS",
    "CapitalPlan",
    "CashPolicy",
    "ExitMenu",
    "ExitStrategy",
    "HorizonAndBenchmarks",
    "MonitoringCadence",
    "Percent",
    "PolicyError",
    "PolicySet",
    "PolicyStatus",
    "PolicyVersionError",
    "Ratification",
    "RatificationKind",
    "RatificationMismatchError",
    "RiskRails",
    "RotationDial",
    "T2Cadence",
    "TriggerSensitivity",
    "policy_digest",
]


def _reject_float(value: Any) -> Any:
    """Refuse a `float` where an exact decimal is required.

    The same rule `analyst.journal.models` applies to money, applied to percentages, because a
    rail is a comparison: pydantic would coerce `14.999999999999998` into a `Decimal` that reads
    as 15% in a proposal and blocks an order that should have passed. Percentages here are
    written as `Decimal("15")` or `"15"`, never `15.0`.
    """
    if isinstance(value, float):
        raise ValueError(
            f"a percentage must be an exact decimal, got float {value!r}; pass a Decimal or a "
            "string (CLAUDE.md: a rail that is off by a float epsilon is a rail that did not hold)"
        )
    return value


#: A percentage in 0..100, exactly as the `percent` domain constrains it.
Percent = Annotated[Decimal, BeforeValidator(_reject_float)]

#: Shape of the parking instrument's identifier. ISIN only — a cash policy naming a *symbol* and
#: leaving the service to look up prices with it is invariant #2's forbidden shortcut.
ISIN_PATTERN: Final = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"


class PolicyError(Exception):
    """Base for every policy-set failure, so callers can catch the module."""


class PolicyVersionError(PolicyError):
    """A version was built or requested that the versioning rules do not permit."""


class RatificationMismatchError(PolicyError):
    """A ratification was attached to content other than the content it was granted for.

    The whole point of pinning a hash: a ratification that can be moved onto an edited document
    ratifies nothing, and the governance model (#4/#5/#9) rests on it not being movable.
    """


# ── the seven policies of §5.2 ───────────────────────────────────────────────────────────────


class CapitalPlan(BaseModel):
    """§5.2 policy 1 — how money arrives: SIP amount, day of month, top-up rules.

    What it does: carries the instalment plan and answers when instalments fall due.
    What it assumes: `day_of_month` names a date that exists in every month — capped at 28 like
    `case_.sip_day_of_month`, so a February instalment never silently skips a month.
    What it never does: consult a trading calendar. These are the *nominal* instalment dates; the
    session an instalment is actually deployed in is A7's problem, and mixing the two here would
    make the plan depend on a calendar that only covers a few years.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sip_amount_inr: Money = Field(gt=0, description="Rupees per instalment. Decimal, never float.")
    day_of_month: int = Field(
        ge=1, le=28, description="Nominal instalment day, 1..28 so it exists in February."
    )
    top_up_rule: str | None = Field(
        default=None,
        min_length=1,
        description="Free-text rule for discretionary top-ups, e.g. 'bonus in April'.",
    )

    def instalments(self, start: date, end: date) -> Iterator[date]:
        """Every nominal instalment date in the inclusive range, ascending."""
        if start > end:
            raise ValueError(f"start {start.isoformat()} is after end {end.isoformat()}")
        due = self.next_instalment(start)
        while due <= end:
            yield due
            due = self.next_instalment(_first_of_next_month(due))

    def next_instalment(self, on_or_after: date) -> date:
        """The first instalment date on or after `on_or_after`."""
        this_month = on_or_after.replace(day=self.day_of_month)
        if this_month >= on_or_after:
            return this_month
        return _first_of_next_month(on_or_after).replace(day=self.day_of_month)

    def is_due(self, day: date) -> bool:
        """Whether `day` is a nominal instalment date under this plan."""
        return day.day == self.day_of_month


class HorizonAndBenchmarks(BaseModel):
    """§5.2 policy 2 — target years and the benchmark pair every report is measured against.

    Two benchmarks rather than one because §5.7's evidence pack reports returns against both: a
    broad market index and a theme proxy answer different questions ("was equity the right place"
    and "was this theme the right expression"), and a case judged against only one of them can
    look like a success while having been a bad idea.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    horizon_years: int = Field(gt=0, le=50, description="Target holding horizon, in years.")
    benchmark_primary: str = Field(
        min_length=1, description="Broad-market total-return benchmark, e.g. 'NIFTY-TRI'."
    )
    benchmark_secondary: str | None = Field(
        default=None, min_length=1, description="Theme proxy, e.g. a NIFTY IT/CPSE blend."
    )


class RotationDial(BaseModel):
    """§5.2 policy 3 — the tactical sleeve as a percentage of case capital (§5.5).

    `tactical_pct` is the whole dial: core is the remainder, and storing both would let them
    disagree. Moving this number resizes the sleeve boundary, which §5.5 calls a policy change —
    so it moves only by ratifying a new version, never by an agent adjusting it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tactical_pct: Percent = Field(
        ge=0, le=100, description="Tactical sleeve target as % of case value. Core is 100 - this."
    )

    @property
    def core_pct(self) -> Percent:
        """The core sleeve's share — derived, never stored, so the two cannot drift."""
        return Decimal(100) - self.tactical_pct


class RiskRails(BaseModel):
    """§5.2 policy 4 — the caps A8 enforces (invariant #6).

    What it does: carries the deterministic limits — max position %, max sector %, minimum
    holdings, the drawdown that forces a review, and the per-order sanity caps.
    What it assumes: A8 reads these and nothing else; there is no override parameter here or
    anywhere downstream, because a rail with a bypass is not a rail.
    What it never does: express the drawdown trigger as a negative number. The `percent` domain is
    0..100, so §5.2's "-25% peak-to-trough" is `drawdown_review_pct = 25` — a magnitude, with the
    direction in the name.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_position_pct: Percent = Field(
        gt=0, le=100, description="Largest single holding, as % of case value."
    )
    max_sector_pct: Percent = Field(
        gt=0, le=100, description="Largest single sector, as % of case value."
    )
    min_holdings: int = Field(ge=1, description="Fewest holdings the core may be concentrated to.")
    drawdown_review_pct: Percent = Field(
        gt=0, le=100, description="Peak-to-trough fall that forces a review, as a magnitude."
    )
    max_order_value_inr: Money = Field(
        gt=0, description="Per-order sanity cap in rupees — a fat-finger guard, not a strategy."
    )
    max_order_pct_of_case: Percent = Field(
        gt=0, le=100, description="Per-order sanity cap as % of case value."
    )

    @model_validator(mode="after")
    def _position_cap_must_admit_min_holdings(self) -> RiskRails:
        """Refuse a pair of caps no portfolio can satisfy.

        `min_holdings` positions each capped at `max_position_pct` must be able to hold 100% of
        the case, or every fully-invested book breaches a rail on the day it is built and A8
        blocks orders forever. Catching it at ratification time is the difference between a
        rejected proposal and a case that mysteriously cannot trade.
        """
        if self.max_position_pct * self.min_holdings < 100:
            raise ValueError(
                f"max_position_pct {self.max_position_pct}% x min_holdings {self.min_holdings} = "
                f"{self.max_position_pct * self.min_holdings}% cannot reach 100%: no fully "
                "invested book satisfies both rails, so every order would be blocked"
            )
        if self.max_sector_pct < self.max_position_pct:
            raise ValueError(
                f"max_sector_pct {self.max_sector_pct}% is below max_position_pct "
                f"{self.max_position_pct}%: a single position at its cap would breach its own "
                "sector's cap, so the position rail could never be reached"
            )
        return self


class ExitStrategy(StrEnum):
    """One way out of a position, from §5.6's ratified menu."""

    STAGED = "STAGED"
    """Sold over 2-3 sessions. The default: it is the one that respects liquidity."""

    IMMEDIATE = "IMMEDIATE"
    """Sold in one session. For integrity breaks, where waiting is the risk."""

    EXIT_AND_REDEPLOY = "EXIT_AND_REDEPLOY"
    """Sold and the proceeds queued straight into a named replacement (§5.6)."""


class ExitMenu(BaseModel):
    """§5.2 policy 5 — the exit strategies the agent may choose among, and when.

    A menu rather than a single rule because §5.6 gives the agent the choice *within* what was
    ratified: it may pick staged or immediate on the evidence, but it may not invent a strategy
    the human never approved. `immediate_allowed_on` names the break-condition types that unlock
    `IMMEDIATE` — §5.6's "immediate allowed on integrity events".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: tuple[ExitStrategy, ...] = Field(
        min_length=1, description="Strategies the agent may select. Anything absent is forbidden."
    )
    default: ExitStrategy = Field(description="What is used when nothing unlocks another.")
    immediate_allowed_on: tuple[str, ...] = Field(
        default=(),
        description="Break-condition types that unlock IMMEDIATE, e.g. ('integrity',).",
    )

    @field_validator("allowed", "immediate_allowed_on")
    @classmethod
    def _deduplicate_and_order(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        """Normalize to a sorted, duplicate-free tuple.

        Order and duplication are not policy, but they *are* content: two menus differing only in
        the order they were typed would hash differently and look like a policy change at
        ratification time.
        """
        return tuple(sorted(dict.fromkeys(value)))

    @model_validator(mode="after")
    def _default_must_be_on_the_menu(self) -> ExitMenu:
        if self.default not in self.allowed:
            raise ValueError(
                f"default exit {self.default.value} is not on the ratified menu "
                f"({', '.join(strategy.value for strategy in self.allowed)}): the agent would "
                "have to choose an unratified strategy on its first exit"
            )
        if ExitStrategy.IMMEDIATE in self.allowed and not self.immediate_allowed_on:
            raise ValueError(
                "IMMEDIATE is on the menu but immediate_allowed_on names no trigger: an "
                "unconditional immediate exit is a discretion the human did not grant (§5.6)"
            )
        return self


class CashPolicy(BaseModel):
    """§5.2 policy 6 — where idle cash waits and what releases it (§5.6, decision #10).

    The parking instrument is identified by ISIN, not by 'LIQUIDCASE': the cash leg is an
    on-exchange order like any other, so it joins to prices the same way everything else does
    (invariant #2). `parking_symbol` is carried alongside for the human reading the proposal, and
    is display text — nothing queries with it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    parking_isin: str = Field(
        pattern=ISIN_PATTERN, description="Liquid ETF the deployment queue parks in."
    )
    parking_symbol: str = Field(
        min_length=1, description="Human-readable ticker for the proposal document. Display only."
    )
    deploy_within_sessions: int = Field(
        ge=0, le=60, description="Sessions proceeds may sit parked before deployment is forced."
    )
    min_deployment_inr: Money = Field(
        gt=0, description="Smallest tranche worth deploying; below this, cash waits."
    )


class T2Cadence(StrEnum):
    """How often §5.4's scheduled deep review runs."""

    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"


class TriggerSensitivity(StrEnum):
    """How readily T0 escalates to T1 (§5.4). Cost lives on this dial."""

    LOW = "LOW"
    STANDARD = "STANDARD"
    HIGH = "HIGH"


class MonitoringCadence(BaseModel):
    """§5.2 policy 7 — T2 frequency and T1 trigger sensitivity (§5.4).

    Ratified rather than configured because it is the case's cost dial as well as its vigilance
    dial: T1 and T2 are strong-model calls, and how often they fire is a decision with a rupee
    figure attached (§5.7's token burn).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    t2_cadence: T2Cadence = Field(description="Scheduled deep-review frequency.")
    t1_sensitivity: TriggerSensitivity = Field(description="How readily T0 escalates to T1.")


#: The seven policies of §5.2, in the order the plan tables them. Named once here because three
#: things must agree about what "the policy set" is: the content hash, `revise()`'s allowed keys,
#: and the test that asserts every §5.2 row is present.
POLICY_FIELDS: Final[tuple[str, ...]] = (
    "capital_plan",
    "horizon",
    "rotation_dial",
    "rails",
    "exit_menu",
    "cash_policy",
    "monitoring",
)


# ── ratification ─────────────────────────────────────────────────────────────────────────────


class RatificationKind(StrEnum):
    """Who ratified, in the only sense that matters: a human, or the B9 test fixture.

    Mirrors `policy_set.ratification_kind`'s CHECK constraint. The distinction is in the schema,
    in the model and in the funding path on purpose — three places, because the one thing it
    prevents is real money moving on a policy nobody read.
    """

    HUMAN = "HUMAN"
    """A person reviewed the proposal and approved it. The only kind real money may run on."""

    FIXTURE = "FIXTURE"
    """B9's reference-case ratification. Paper and tests only, forever."""

    @property
    def funds_real_money(self) -> bool:
        """Whether this kind of ratification may back a `FUNDED(real)` case (AGENTIC_CONTEXT §3.6).

        A property rather than a comparison at each call site so that "which ratifications are
        good enough for real money" is answered in exactly one place.
        """
        return self is RatificationKind.HUMAN


class Ratification(BaseModel):
    """The governance artifact: who approved what, when, and pinned to which exact content.

    What it does: records an approval against a content hash, so the document that was reviewed is
    recoverable from the record.
    What it assumes: `at` came from an injected `Clock` (B10) and `content_hash` was computed from
    the policies being ratified — `PolicySet.ratified_with` checks that rather than trusting it.
    What it never does: carry an opinion about the policies. It is evidence that an approval
    happened, not a second copy of what was approved.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    by: str = Field(min_length=1, description="Who approved — a person, or the B9 fixture's name.")
    at: datetime = Field(description="When, tz-aware, from an injected Clock.")
    kind: RatificationKind = Field(description="HUMAN or FIXTURE (B9).")
    content_hash: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$", description="`sha256:<hex>` of the policies approved."
    )

    @field_validator("at")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        """A naive instant is an error: a ratification time that cannot be ordered is not one."""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                f"ratified_at must be tz-aware, got naive {value.isoformat()!r}; take it from an "
                "injected Clock"
            )
        return value

    @field_validator("by")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a ratification must name who granted it")
        return value


class PolicyStatus(StrEnum):
    """Where a policy-set version stands.

    Not a column on `policy_set` — every row in that table is ratified by construction, and which
    version is current is `max(version)`. The enum exists because a *proposal* is a policy set
    too, and it lives on the case until someone ratifies it.
    """

    PROPOSAL = "PROPOSAL"
    """Drafted, awaiting ratification. May be edited freely; may not back a decision."""

    RATIFIED = "RATIFIED"
    """Approved and current. Immutable — a change is a new version (decisions #4/#5/#9)."""

    SUPERSEDED = "SUPERSEDED"
    """Approved, and later replaced by a higher version. Kept because "which rails were in
    force when that order was placed" must stay answerable."""


def policy_digest(policies: Mapping[str, Any]) -> str:
    """`sha256:<hex>` of the canonical bytes of the seven policies.

    What it does: hashes exactly the ratifiable content — the seven §5.2 policies — and nothing
    else, so the same policies proposed twice hash the same and a ratification can be checked
    against what it claims to cover.
    What it assumes: `policies` is already JSON-safe (a `model_dump(mode="json")`), and carries
    every key in `POLICY_FIELDS`.
    What it never does: include the version, the status or the ratification. A hash over those
    would change when a proposal is renumbered, and would make an identical document look like a
    different one.
    """
    missing = [field for field in POLICY_FIELDS if field not in policies]
    if missing:
        raise PolicyError(
            f"cannot hash a partial policy set; missing {', '.join(missing)} "
            "(§5.2 ratifies all seven policies as one document)"
        )
    return "sha256:" + digest_of(
        canonical_bytes({field: policies[field] for field in POLICY_FIELDS})
    )


class PolicySet(BaseModel):
    """One version of a case's §5.2 policy set: the seven policies plus its governance state.

    What it does: holds the ratifiable document, hashes it, and produces the next version when it
    is edited.
    What it assumes: version numbers are assigned here and checked at the database, where
    `policy_set_version_unique` and the append-only trigger make a rewritten version impossible.
    What it never does: change. Every model in this module is frozen, so `set.rails = ...` raises;
    `revise()` returns a new object in `PROPOSAL` and leaves this one untouched. That is the whole
    of "a ratified policy set is immutable" — not a convention, a `ValidationError`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1, description="The case these policies govern.")
    version: int = Field(ge=1, description="1-based, per case. Assigned when the version is made.")
    status: PolicyStatus = Field(
        default=PolicyStatus.PROPOSAL, description="PROPOSAL, RATIFIED or SUPERSEDED."
    )
    supersedes_version: int | None = Field(
        default=None, ge=1, description="The version this one replaces; None for v1."
    )
    capital_plan: CapitalPlan = Field(description="§5.2 policy 1 — SIP amount, day, top-ups.")
    horizon: HorizonAndBenchmarks = Field(description="§5.2 policy 2 — years and benchmark pair.")
    rotation_dial: RotationDial = Field(description="§5.2 policy 3 — tactical sleeve %.")
    rails: RiskRails = Field(description="§5.2 policy 4 — the caps A8 enforces.")
    exit_menu: ExitMenu = Field(description="§5.2 policy 5 — ratified exit strategies.")
    cash_policy: CashPolicy = Field(description="§5.2 policy 6 — parking and deployment.")
    monitoring: MonitoringCadence = Field(description="§5.2 policy 7 — T2 cadence, T1 trigger.")
    ratification: Ratification | None = Field(
        default=None, description="Present exactly when the status is RATIFIED or SUPERSEDED."
    )

    @model_validator(mode="after")
    def _governance_state_must_be_consistent(self) -> PolicySet:
        """A status and a ratification record that disagree would make the audit trail a guess."""
        approved = self.status in (PolicyStatus.RATIFIED, PolicyStatus.SUPERSEDED)
        if approved and self.ratification is None:
            raise ValueError(
                f"a {self.status.value} policy set must carry the ratification that approved it; "
                "an approved policy with no record of who approved it is not governed"
            )
        if not approved and self.ratification is not None:
            raise ValueError(
                f"a {self.status.value} policy set must not carry a ratification: proposing is "
                "never ratifying (§5.1)"
            )
        if self.version == 1 and self.supersedes_version is not None:
            raise ValueError("version 1 supersedes nothing")
        if self.version > 1 and self.supersedes_version is None:
            raise ValueError(
                f"version {self.version} must name the version it supersedes, or the history of "
                "which rails were in force when is not reconstructable"
            )
        if self.supersedes_version is not None and self.supersedes_version >= self.version:
            raise ValueError(
                f"version {self.version} cannot supersede version {self.supersedes_version}: "
                "versions only move forward"
            )
        if self.ratification is not None and self.ratification.content_hash != self.content_hash:
            raise RatificationMismatchError(
                f"ratification pins {self.ratification.content_hash} but these policies hash to "
                f"{self.content_hash}: the approved document is not this one"
            )
        return self

    @property
    def policies(self) -> Mapping[str, Any]:
        """Just the seven §5.2 policies, JSON-safe — the ratifiable content."""
        dumped = self.model_dump(mode="json")
        return {field: dumped[field] for field in POLICY_FIELDS}

    @property
    def content_hash(self) -> str:
        """`sha256:<hex>` over the seven policies. What a ratification pins."""
        return policy_digest(self.policies)

    @property
    def is_ratified(self) -> bool:
        """Whether decisions may be made under this version."""
        return self.status is PolicyStatus.RATIFIED

    def revise(self, **changes: Any) -> PolicySet:
        """Return the next version, in `PROPOSAL`, with `changes` applied.

        What it does: builds version N+1 from this one, carrying `supersedes_version`, dropping
        the ratification and re-validating the whole document — so an edit that makes the rails
        self-contradictory is refused here rather than at ratification time.
        What it assumes: `changes` name §5.2 policies. Editing `version` or `status` through here
        is refused, because those are the bookkeeping this method owns.
        What it never does: touch this object, or produce a version that changes nothing. A
        no-op revision would create a second version needing a second ratification for no reason,
        and would make the version history unreadable.
        """
        unknown = sorted(set(changes) - set(POLICY_FIELDS))
        if unknown:
            raise PolicyVersionError(
                f"revise() edits the seven §5.2 policies, not {', '.join(unknown)}; "
                f"editable: {', '.join(POLICY_FIELDS)}"
            )
        if not changes:
            raise PolicyVersionError(
                "revise() with no changes would create an identical version needing a fresh "
                "ratification for nothing"
            )
        document = self.model_dump()
        document.update(changes)
        document.update(
            {
                "version": self.version + 1,
                "supersedes_version": self.version,
                "status": PolicyStatus.PROPOSAL,
                "ratification": None,
            }
        )
        revised = PolicySet.model_validate(document)
        if revised.content_hash == self.content_hash:
            raise PolicyVersionError(
                f"the revision is byte-identical to version {self.version} "
                f"({self.content_hash}); nothing to re-ratify"
            )
        return revised

    def ratified_with(self, ratification: Ratification) -> Self:
        """Return this version as `RATIFIED`, checking the ratification covers this content.

        What it does: attaches the approval and flips the status, once.
        What it assumes: the caller obtained `content_hash` from *this* document — a ratification
        granted for an earlier draft is refused, which is M5.8's "cannot ratify a proposal that
        changed since it was displayed" enforced in the model rather than in a web handler.
        What it never does: re-ratify. A version already ratified raises; the way to change a
        ratified policy is `revise()`.
        """
        if self.status is not PolicyStatus.PROPOSAL:
            raise PolicyVersionError(
                f"version {self.version} is already {self.status.value}; a ratified policy set is "
                "immutable and a change is a new version (revise())"
            )
        if ratification.content_hash != self.content_hash:
            raise RatificationMismatchError(
                f"ratification pins {ratification.content_hash} but version {self.version} hashes "
                f"to {self.content_hash}: the document changed after it was displayed"
            )
        return self.model_validate(
            {
                **self.model_dump(),
                "status": PolicyStatus.RATIFIED,
                "ratification": ratification.model_dump(),
            }
        )

    def superseded(self) -> Self:
        """Return this ratified version marked `SUPERSEDED`, for the history view."""
        if self.status is not PolicyStatus.RATIFIED:
            raise PolicyVersionError(
                f"only a RATIFIED version can be superseded, not {self.status.value}"
            )
        return self.model_validate({**self.model_dump(), "status": PolicyStatus.SUPERSEDED})


def _first_of_next_month(day: date) -> date:
    """The first day of the month after `day`. Avoids any month-length arithmetic."""
    return date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)
