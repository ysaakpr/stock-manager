"""A6: the rotation engine — dial semantics, sleeve trading authority, every order tagged.

This is where §5.5's two sleeves are given teeth. It reads the tactical/core boundary off the
**ratified** policy set (§5.2 policy 3) and refuses to run on anything less (`UnratifiedDialError`):
the agent may only rotate inside a mix a human approved. From there it offers exactly three trading
verbs, one per authority the plan grants:

* **`tactical_trade`** — full buy/sell authority inside the tactical sleeve. Rotation trades, cycle
  expressions, temporary positions. The dial is a target, not a cap, so a tactical trade is not
  refused for pulling the sleeve past its target — but A8 is still binding, so a trade that would
  breach a rail is blocked and a `RAIL_BLOCK` is journaled (invariant #6). All orders tagged
  `TACTICAL`.
* **`core_tilt`** — steering new SIP money *within* the core (§5.5: "new money steering within core
  = allowed"). A buy that grows an existing core holding. It is not a membership change, so it needs
  no verdict; but it may not add a new name (that is A4's ratified-thesis path) or sell (that is an
  exit), and it refuses both as `CoreMembershipError`. All orders tagged `CORE`.
* **`core_exit`** — the only way core membership shrinks: a sell backed by a `BROKEN` break
  condition (§5.5 / decision #4). Without a BROKEN verdict it raises `CoreMembershipError`, because
  a core position that leaves the book on anything less than a broken thesis is exactly the
  discretion the sleeve model removes. All orders tagged `CORE`.

Every verb produces a `RotationDecision` carrying the *tagged* order — the sleeve is written onto
the order's `tag` so X1 carries it to the broker, and onto the journal line's `sleeve` so the
evidence pack can split turnover by sleeve (§5.7). An order with no sleeve is impossible to produce
here: the tag comes from the verb, not the caller (acceptance criterion 4).

Resizing the boundary — moving the dial — is not a verb. It is `sleeves.resize_dial`, which returns
a new *proposal* version requiring ratification; a new engine built on the old ratified version
keeps trading the old mix until the new one is ratified (acceptance criterion 3). A6 has no broker
and no setter for the dial: it decides and journals, X1 places, and the boundary moves only through
governance.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Final

from analyst.cases import PolicySet, PolicyStatus, RiskRails, RotationDial
from analyst.journal import (
    Actor,
    BreakConditionEvaluation,
    Decision,
    EvidenceBundle,
    Journal,
    JournalEntry,
    RecordedEntry,
    Sleeve,
    Verdict,
)
from analyst.rails import HouseholdExposure, Portfolio, ProposedOrder, RailAssessment, RailEngine
from analyst.rotation.sleeves import (
    CoreMembershipError,
    SleeveAllocation,
    SleeveTargets,
    UnratifiedDialError,
    allocate,
    sleeve_targets,
)
from dataplatform.clock import Clock, SystemClock
from dataplatform.logging import get_logger
from execution.broker import Side

__all__ = ["RotationDecision", "RotationEngine"]

_LOG = get_logger(__name__)

#: A trade's decision type follows its side; there is no third option a rotation order can be.
_DECISION_FOR_SIDE: Final[dict[Side, Decision]] = {
    Side.BUY: Decision.BUY,
    Side.SELL: Decision.SELL,
}


@dataclass(frozen=True, slots=True)
class RotationDecision:
    """The outcome of one rotation verb: the tagged order, the rail verdict, and the journal line.

    What it does: carry the order as A6 tagged it (its `request.tag` and this `sleeve` agree), the
    rail assessment that cleared or blocked it, and the journal entry that recorded it.
    `entry` is the trade's own `BUY`/`SELL` line when the order was placed, and `None` when a rail
    blocked it — in which case A8 has already journaled the `RAIL_BLOCK`, and there was no trade to
    record. `placed` is the single question a caller asks before handing the order to X1.
    What it never does: place the order. A6 decides and records; X1 places.
    """

    order: ProposedOrder
    sleeve: Sleeve
    assessment: RailAssessment
    entry: RecordedEntry | None

    @property
    def placed(self) -> bool:
        """True when every rail cleared the order — the only case in which X1 should place it."""
        return self.assessment.allowed


class RotationEngine:
    """A6 wired to the ratified dial, the rails and the journal (§5.5).

    What it does: hold the ratified policy set, expose the sleeve targets its dial implies, and
    offer the three trading verbs — tactical, core tilt, core exit — each clearing A8 and journaling
    its outcome. The dial is read-only here; moving it is `resize_dial`, a governance act.
    What it assumes: `rail_engine` and `journal` write to the *same* transaction (the daily loop
    owns it), so a rail block and the trade around it land atomically; `clock` is injected (B10).
    What it never does: run on an unratified mix (raises at construction), take an override on a
    rail, or produce an order without a sleeve tag.
    """

    __slots__ = ("_clock", "_journal", "_policy", "_rails")

    def __init__(
        self,
        policy_set: PolicySet,
        rail_engine: RailEngine,
        journal: Journal,
        *,
        clock: Clock | None = None,
    ) -> None:
        if policy_set.status is not PolicyStatus.RATIFIED:
            raise UnratifiedDialError(
                f"a rotation engine runs on a RATIFIED policy set, got {policy_set.status.value} "
                f"version {policy_set.version}: the agent rotates inside a mix a human approved, "
                "and moving the dial is a new ratified version (§5.5), never an ad-hoc setting"
            )
        self._policy = policy_set
        self._rails = rail_engine
        self._journal = journal
        self._clock = SystemClock() if clock is None else clock

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(case_id={self._policy.case_id!r}, "
            f"version={self._policy.version}, tactical_pct={self.dial.tactical_pct})"
        )

    # ── the ratified boundary (read-only) ──────────────────────────────────────────────────────

    @property
    def dial(self) -> RotationDial:
        """The ratified dial (§5.2 policy 3). Read-only — moving it is `resize_dial`."""
        return self._policy.rotation_dial

    @property
    def rails(self) -> RiskRails:
        """The ratified rails A8 enforces on every order this engine emits (§5.2 policy 4)."""
        return self._policy.rails

    def targets(self, case_value: Decimal) -> SleeveTargets:
        """The rupee target for each sleeve at `case_value`, under the ratified dial (§5.5)."""
        return sleeve_targets(self.dial, case_value)

    def allocation(
        self, portfolio: Portfolio, *, tactical_isins: frozenset[str]
    ) -> SleeveAllocation:
        """Where `portfolio` sits against its dial targets, given the tactical membership (§5.5)."""
        return allocate(portfolio, self.dial, tactical_isins=tactical_isins)

    # ── the three trading verbs ─────────────────────────────────────────────────────────────────

    def tactical_trade(
        self,
        order: ProposedOrder,
        portfolio: Portfolio,
        *,
        trading_date: date,
        rationale: str,
        household: HouseholdExposure | None = None,
        actor: Actor = Actor.T2,
        evidence: EvidenceBundle | None = None,
    ) -> RotationDecision:
        """Trade inside the tactical sleeve — full buy/sell authority, rails still binding (§5.5).

        What it does: tag the order `TACTICAL`, clear it through A8, and — if it passes — journal a
        `BUY`/`SELL` line with the sleeve and the rationale. A tactical trade is never refused for
        pulling the sleeve past its dial target (the dial is a target, not a rail), but a trade that
        would breach a rail is blocked and A8 journals the `RAIL_BLOCK` (invariant #6).
        What it assumes: `rationale` is the trade's recorded reason — §0 requires every trade carry
        one, and the journal refuses a `BUY`/`SELL` without it.
        What it never does: place the order, or accept a bypass. `household` adds the cross-case
        rail context; it does not grant an exception.
        """
        return self._decide(
            order,
            portfolio,
            sleeve=Sleeve.TACTICAL,
            trading_date=trading_date,
            rationale=rationale,
            household=household,
            actor=actor,
            evidence=evidence,
        )

    def core_tilt(
        self,
        order: ProposedOrder,
        portfolio: Portfolio,
        *,
        trading_date: date,
        rationale: str,
        household: HouseholdExposure | None = None,
        actor: Actor = Actor.T2,
        evidence: EvidenceBundle | None = None,
    ) -> RotationDecision:
        """Steer new money within the core: a buy that grows an existing core holding (§5.5).

        What it does: tag the order `CORE`, clear it through A8, and journal a `BUY` on success.
        This is the one core trade that needs no verdict, because it changes no membership — it
        tilts SIP money toward a cycle-favored name the case already holds.
        What it assumes: the order is a *buy into a name already held*. A sell is a membership
        change (use `core_exit`); a buy of a name the book does not hold would *add* a core member,
        which is A4's ratified-thesis path, not a tilt — both are refused as `CoreMembershipError`,
        because "membership changes on a BROKEN verdict only" (decision #4) is meant on both sides.
        What it never does: place the order, or add a core name without a ratified thesis.
        """
        if order.side is not Side.BUY:
            raise CoreMembershipError(
                f"a core tilt is a buy that steers new money within the core; a {order.side.value} "
                "of a core name is a membership change — exit via core_exit with a BROKEN verdict"
            )
        if portfolio.lot(order.isin) is None:
            raise CoreMembershipError(
                f"core tilt buys more of a name already held; {order.isin} is not in the book, so "
                "this would add a core member — that is A4's ratified-thesis path, not a rotation "
                "tilt (§5.5: membership changes on a BROKEN verdict only)"
            )
        return self._decide(
            order,
            portfolio,
            sleeve=Sleeve.CORE,
            trading_date=trading_date,
            rationale=rationale,
            household=household,
            actor=actor,
            evidence=evidence,
        )

    def core_exit(
        self,
        order: ProposedOrder,
        portfolio: Portfolio,
        *,
        evaluations: Sequence[BreakConditionEvaluation],
        trading_date: date,
        rationale: str,
        household: HouseholdExposure | None = None,
        actor: Actor = Actor.T2,
        evidence: EvidenceBundle | None = None,
    ) -> RotationDecision:
        """Reduce or exit a core holding — permitted only on a `BROKEN` break condition (§5.5).

        What it does: require at least one `BROKEN` verdict among `evaluations`, then tag the order
        `CORE`, clear it through A8, and journal a `SELL` carrying every evaluation considered — so
        the record shows not just that a core name was sold but which condition broke.
        What it assumes: `evaluations` are the break-condition verdicts from the review that reached
        this exit (A4/A5). A core sell with no `BROKEN` among them raises `CoreMembershipError`:
        core membership changes on a broken thesis, and on nothing weaker (decision #4).
        What it never does: place the order, or sell a core name on a WEAKENED or INTACT verdict —
        that is precisely the discretion the sleeve model removes.
        """
        if not any(evaluation.verdict is Verdict.BROKEN for evaluation in evaluations):
            verdicts = ", ".join(evaluation.verdict.value for evaluation in evaluations) or "none"
            raise CoreMembershipError(
                f"a core sell needs a BROKEN break condition (§5.5 / decision #4); the verdicts "
                f"were [{verdicts}]. Core membership changes on a broken thesis and nothing weaker"
            )
        if order.side is not Side.SELL:
            raise CoreMembershipError(
                f"core_exit reduces a core holding; got a {order.side.value}. A core buy is a tilt "
                "(core_tilt) or a new member (A4), never an exit"
            )
        return self._decide(
            order,
            portfolio,
            sleeve=Sleeve.CORE,
            trading_date=trading_date,
            rationale=rationale,
            household=household,
            actor=actor,
            evidence=evidence,
            break_conditions=tuple(evaluations),
        )

    # ── the shared path every verb takes ────────────────────────────────────────────────────────

    def _decide(
        self,
        order: ProposedOrder,
        portfolio: Portfolio,
        *,
        sleeve: Sleeve,
        trading_date: date,
        rationale: str,
        household: HouseholdExposure | None,
        actor: Actor,
        evidence: EvidenceBundle | None,
        break_conditions: tuple[BreakConditionEvaluation, ...] = (),
    ) -> RotationDecision:
        """Tag, clear against A8, and journal one rotation order — the path all three verbs share.

        A blocked order is not journaled here: `guard_order` has already written the `RAIL_BLOCK`
        naming every breached rail (invariant #6, §0), and there is no trade to record. A cleared
        order is journaled as a `BUY`/`SELL` with its sleeve, rationale and any break conditions.
        """
        tagged = _tag(order, sleeve)
        assessment = self._rails.guard_order(
            tagged,
            portfolio,
            self.rails,
            trading_date=trading_date,
            household=household,
            sleeve=sleeve,
        )
        if not assessment.allowed:
            _LOG.info(
                "rotation.blocked",
                case_id=portfolio.case_id,
                isin=tagged.isin,
                side=tagged.side.value,
                sleeve=sleeve.value,
                rails=",".join(rail.value for rail in assessment.breached_rails),
            )
            return RotationDecision(order=tagged, sleeve=sleeve, assessment=assessment, entry=None)
        entry = self._journal.append(
            JournalEntry(
                ts=self._clock.now(),
                trading_date=trading_date,
                case_id=portfolio.case_id,
                actor=actor,
                decision=_DECISION_FOR_SIDE[tagged.side],
                isin=tagged.isin,
                sleeve=sleeve,
                rationale=rationale,
                break_conditions_evaluated=break_conditions,
            ),
            evidence=evidence,
        )
        _LOG.info(
            "rotation.trade",
            case_id=portfolio.case_id,
            isin=tagged.isin,
            side=tagged.side.value,
            sleeve=sleeve.value,
            entry_id=entry.id,
        )
        return RotationDecision(order=tagged, sleeve=sleeve, assessment=assessment, entry=entry)


def _tag(order: ProposedOrder, sleeve: Sleeve) -> ProposedOrder:
    """Return the order with its request `tag` set to the sleeve — CORE or TACTICAL.

    The tag is authoritative and comes from the verb, not the caller: whatever tag the incoming
    request carried is overwritten, so an order this engine emits always carries exactly the sleeve
    that authorized it (acceptance criterion 4), and X1 reads that tag to file the fill.
    """
    return replace(order, request=replace(order.request, tag=sleeve.value))
