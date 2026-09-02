"""A7: the cash manager — park idle cash the same session, deploy it only when §5.6 permits.

§5.6 gives cash two states and exactly two transitions between them, and this module is those
transitions:

* **Park.** Exit proceeds and the monthly SIP instalment do not sit as idle rupees — they are put
  into the liquid ETF (LIQUIDCASE / LIQUIDBEES) *the same session* they arrive (decision #10), on
  the same on-exchange broker path as any other order. `park` turns a rupee amount into a
  whole-share ETF buy (no fractional shares — the India constraint applies to the ETF too), clears
  it through A8, and journals it as a `CASH`-sleeve decision. Parked cash keeps earning while it
  waits for somewhere ratified to go.
* **Deploy.** Parked cash leaves the ETF only when the agent has somewhere the plan allows: a
  **ratified-thesis core replacement** or a **tactical opportunity** (§5.6, §5.5). `deploy` refuses
  to move cash into a real position without a `BuyAuthorization` from A4 proving exactly that — a
  `CORE` authorization exists only against a ratified thesis, a `TACTICAL` one only with a journaled
  rationale — so "deployment happens only against a ratified-thesis replacement or a tactical
  opportunity" is enforced by the type of thing `deploy` demands, not by a comment asking nicely.

Both transitions are journaled decisions with a rationale (§0, invariant #9) and both pass A8
(invariant #6) — the cash leg is an order like any other, and an order that would breach a rail is
blocked and the block journaled, cash or not. The manager holds no broker: it decides and records,
and X1 places, exactly as A6 does. The manager runs on a **ratified** `PolicySet` (§3 item 2: the
cash policy is reserved to human ratification), refusing an unratified one at construction, so the
parking instrument and the deployment rules it uses are always ones a human approved.

Money is `Decimal` and share counts are `int` (CLAUDE.md); identity is the ISIN (invariant #2); the
clock is injected (B10). Nothing here reads the network.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Final

from analyst.cases import CashPolicy, PolicySet, PolicyStatus, RiskRails
from analyst.cash.queue import CashSource, DeploymentQueue
from analyst.journal import (
    Actor,
    BreakConditionEvaluation,
    Decision,
    EvidenceBundle,
    Journal,
    JournalEntry,
    RecordedEntry,
    Sleeve,
)
from analyst.rails import HouseholdExposure, Portfolio, ProposedOrder, RailAssessment, RailEngine
from analyst.thesis import BuyAuthorization
from dataplatform.clock import Clock, SystemClock
from dataplatform.logging import get_logger
from execution.broker import Exchange, OrderRequest, OrderType, Side

__all__ = [
    "PARKING_SECTOR",
    "BelowMinimumDeploymentError",
    "CashError",
    "CashManager",
    "DeploymentDecision",
    "ParkingDecision",
    "UndeployableError",
    "UnratifiedCashPolicyError",
]

_LOG = get_logger(__name__)

_ZERO: Final = Decimal(0)

#: The sector label the parking ETF trades under. A liquid ETF is not in any equity sector, so it is
#: grouped on its own rather than aggregated into a real sector's `max_sector_pct` cap — parking
#: ₹2 lakh in LIQUIDBEES is not concentration in "IT". Callers may override it, but the default
#: the cash leg out of the sector rails it has no business loading.
PARKING_SECTOR: Final = "CASH"


# ── errors ───────────────────────────────────────────────────────────────────────────────────


class CashError(Exception):
    """Base for every cash-manager refusal, so callers can catch the module. Fails loud."""


class UnratifiedCashPolicyError(CashError):
    """A cash manager was built on a policy set that is not RATIFIED.

    The cash policy — where idle cash parks and what releases it — is reserved to human ratification
    (§3 item 2). Parking or deploying under a boundary nobody ratified would be trading a mix a
    human never approved, so it is refused at construction, the same way A6 refuses an unratified
    dial.
    """


class UndeployableError(CashError):
    """A deployment was attempted without what §5.6 requires to move cash into a position.

    Cash leaves the parking ETF only against a ratified-thesis core replacement or a tactical
    opportunity (§5.6). A deployment with no `BuyAuthorization`, one for the `CASH` leg (that is
    parking, not deployment), one that names a different instrument than the order, or one for a
    different case, is refused here — the authorization is the proof the plan demands, and a
    deployment without matching proof is exactly the discretion §5.6 removes.
    """


class BelowMinimumDeploymentError(CashError):
    """A deployment tranche was smaller than the ratified `min_deployment_inr`; the cash waits.

    §5.2 policy 6 sets the smallest tranche worth deploying — below it, the churn (costs, tracking
    noise) is not worth the deployment, so the cash stays parked until enough accumulates. This is a
    ratified policy number, so the manager enforces it rather than deploying a sub-scale tranche.
    """


# ── decisions ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ParkingDecision:
    """The outcome of one `park`: the ETF buy A7 sized, the rail verdict, and the journal line.

    What it does: carry the whole-share ETF `order` (None when the idle cash did not reach one ETF
    share — there is nothing to park and nothing to journal), the `assessment` that cleared or
    blocked it, the `entry` written on a cleared park (None when a rail blocked it — A8 has already
    journaled the `RAIL_BLOCK`), the `shares` bought, and the `residual_cash` too small to park that
    carries to the next session.
    What it never does: place the order. A7 decides and records; X1 places.
    """

    order: ProposedOrder | None
    assessment: RailAssessment | None
    entry: RecordedEntry | None
    shares: int
    residual_cash: Decimal

    @property
    def parked(self) -> bool:
        """True only when an order was sized and every rail cleared it — the case X1 acts on."""
        return self.order is not None and self.assessment is not None and self.assessment.allowed


@dataclass(frozen=True, slots=True)
class DeploymentDecision:
    """The outcome of one `deploy`: the tagged buy, its sleeve, the rail verdict, and the queue.

    What it does: carry the `order` tagged with the sleeve that authorized it (CORE or TACTICAL),
    that `sleeve`, the rail `assessment`, the `entry` journaled on a cleared deployment (None when a
    rail blocked it — A8 journaled the `RAIL_BLOCK`), the `queue` after the deployed cash was
    released (unchanged when the order was blocked), and the `deployed` rupees drawn from the queue.
    What it never does: place the order. A7 decides and records; X1 places.
    """

    order: ProposedOrder
    sleeve: Sleeve
    assessment: RailAssessment
    entry: RecordedEntry | None
    queue: DeploymentQueue
    deployed: Decimal

    @property
    def placed(self) -> bool:
        """True when every rail cleared the deployment — the only case X1 should place it."""
        return self.assessment.allowed


# ── the manager ──────────────────────────────────────────────────────────────────────────────


class CashManager:
    """A7 wired to the ratified cash policy, the rails and the journal (§5.6).

    What it does: hold the ratified `PolicySet`, expose the parking instrument its cash policy sets,
    and offer the two cash verbs — `park` (idle cash → liquid ETF, same session) and `deploy`
    (parked cash → a ratified position). Each clears A8 (invariant #6) and journals its outcome.
    What it assumes: `rail_engine` and `journal` write to the *same* transaction the daily loop
    owns, so a rail block and the trade around it land atomically; `clock` is injected (B10).
    What it never does: run on an unratified policy (raises at construction), place an order, or
    deploy cash without a `BuyAuthorization` proving §5.6's precondition.
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
            raise UnratifiedCashPolicyError(
                f"a cash manager runs on a RATIFIED policy set, got {policy_set.status.value} "
                f"version {policy_set.version}: where idle cash parks and what releases it is a "
                "ratified policy (§3 item 2), never an ad-hoc setting"
            )
        self._policy = policy_set
        self._rails = rail_engine
        self._journal = journal
        self._clock = SystemClock() if clock is None else clock

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(case_id={self._policy.case_id!r}, "
            f"version={self._policy.version}, parking={self.cash_policy.parking_symbol})"
        )

    # ── the ratified policy (read-only) ──────────────────────────────────────────────────────────

    @property
    def cash_policy(self) -> CashPolicy:
        """The ratified cash policy: parking instrument, deploy deadline, minimum tranche (§5.2)."""
        return self._policy.cash_policy

    @property
    def rails(self) -> RiskRails:
        """The ratified rails A8 enforces on every cash order this manager emits (§5.2 policy 4)."""
        return self._policy.rails

    # ── park: idle cash → the liquid ETF, same session ──────────────────────────────────────────

    def park(
        self,
        portfolio: Portfolio,
        *,
        sources: Mapping[CashSource, Decimal],
        price: Decimal,
        trading_date: date,
        sector: str = PARKING_SECTOR,
        household: HouseholdExposure | None = None,
        actor: Actor = Actor.T2,
        evidence: EvidenceBundle | None = None,
    ) -> ParkingDecision:
        """Park idle cash in the liquid ETF the same session it arrived (§5.6, decision #10).

        What it does: total the cash in `sources` (exit proceeds, SIP instalment), size the largest
        whole-share ETF buy that fits it at `price`, clear that buy through A8, and — if it passes —
        journal a `CASH`-sleeve `BUY` naming the sources. The remainder too small for one ETF share
        is returned as `residual_cash` to carry forward.
        What it assumes: `price` is the ETF's reference price for this session, and `sources` totals
        the cash actually to be parked; the parking ISIN comes from the ratified cash policy, never
        the caller (invariant #2 — the cash leg joins on ISIN like everything else).
        What it never does: buy a fractional share, place the order, or bypass a rail — a parking
        order that would breach a cap is blocked and A8 journals the `RAIL_BLOCK` (invariant #6).
        """
        amount = self._total_sources(sources)
        unit = _require_positive_money("price", price)
        shares = int(amount // unit)
        residual = amount - unit * shares
        if shares == 0:
            # Idle cash below one ETF share: nothing to park this session, carry it forward. Not a
            # decision to journal — the daily loop's heartbeat records the session was considered.
            _LOG.info(
                "cash.park_skipped",
                case_id=portfolio.case_id,
                idle=str(amount),
                price=str(unit),
                reason="below one ETF share",
            )
            return ParkingDecision(
                order=None, assessment=None, entry=None, shares=0, residual_cash=residual
            )

        order = self._cash_order(shares, unit, sector)
        assessment = self._rails.guard_order(
            order,
            portfolio,
            self.rails,
            trading_date=trading_date,
            household=household,
            sleeve=Sleeve.CASH,
        )
        if not assessment.allowed:
            _LOG.info(
                "cash.park_blocked",
                case_id=portfolio.case_id,
                isin=order.isin,
                shares=shares,
                rails=",".join(rail.value for rail in assessment.breached_rails),
            )
            return ParkingDecision(
                order=order,
                assessment=assessment,
                entry=None,
                shares=shares,
                residual_cash=residual,
            )

        entry = self._journal.append(
            JournalEntry(
                ts=self._clock.now(),
                trading_date=trading_date,
                case_id=portfolio.case_id,
                actor=actor,
                decision=Decision.BUY,
                isin=order.isin,
                sleeve=Sleeve.CASH,
                rationale=self._park_rationale(amount, shares, unit, sources),
                payload=self._park_payload(sources, shares, unit, residual),
            ),
            evidence=evidence,
        )
        _LOG.info(
            "cash.parked",
            case_id=portfolio.case_id,
            isin=order.isin,
            shares=shares,
            deployed=str(unit * shares),
            residual=str(residual),
            entry_id=entry.id,
        )
        return ParkingDecision(
            order=order, assessment=assessment, entry=entry, shares=shares, residual_cash=residual
        )

    # ── deploy: parked cash → a ratified position ───────────────────────────────────────────────

    def deploy(
        self,
        order: ProposedOrder,
        portfolio: Portfolio,
        *,
        authorization: BuyAuthorization,
        queue: DeploymentQueue,
        trading_date: date,
        rationale: str,
        household: HouseholdExposure | None = None,
        actor: Actor = Actor.T2,
        evidence: EvidenceBundle | None = None,
        break_conditions: Sequence[BreakConditionEvaluation] = (),
    ) -> DeploymentDecision:
        """Deploy parked cash into a position — only against §5.6's ratified precondition.

        What it does: verify the `BuyAuthorization` is the real thing — a `CORE` replacement (which
        A4 mints only against a ratified thesis) or a `TACTICAL` opportunity (only with a journaled
        rationale), for this order's ISIN and this case — tag the order with the authorized sleeve,
        clear it through A8, and on success journal a `BUY` and release the deployed cash from the
        queue. A deployment smaller than the ratified `min_deployment_inr` is refused (the cash
        waits); one A8 blocks is journaled as a `RAIL_BLOCK` and the queue left untouched.
        What it assumes: `authorization` was obtained from `analyst.thesis.authorize_buy` for this
        buy — holding one is proof §5.5/§5.6's gate was passed. `queue` holds at least the order's
        value; deployment draws from queued cash (exit proceeds or SIP money), not from thin air.
        What it never does: deploy without a matching authorization (raises `UndeployableError`),
        place the order, or bypass a rail. The authorization adds no rail exception; A8 still binds.
        """
        sleeve = self._deployable_sleeve(order, portfolio, authorization)
        if order.value < self.cash_policy.min_deployment_inr:
            raise BelowMinimumDeploymentError(
                f"deployment of {order.value} for {order.isin} is below the ratified minimum "
                f"tranche of {self.cash_policy.min_deployment_inr} (§5.2 policy 6); the cash waits "
                "until enough accumulates to be worth the deployment"
            )

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
                "cash.deploy_blocked",
                case_id=portfolio.case_id,
                isin=tagged.isin,
                sleeve=sleeve.value,
                rails=",".join(rail.value for rail in assessment.breached_rails),
            )
            return DeploymentDecision(
                order=tagged,
                sleeve=sleeve,
                assessment=assessment,
                entry=None,
                queue=queue,
                deployed=_ZERO,
            )

        drained, released = queue.release(order.value)
        entry = self._journal.append(
            JournalEntry(
                ts=self._clock.now(),
                trading_date=trading_date,
                case_id=portfolio.case_id,
                actor=actor,
                decision=Decision.BUY,
                isin=tagged.isin,
                sleeve=sleeve,
                rationale=rationale,
                break_conditions_evaluated=tuple(break_conditions),
                payload=_deploy_payload(authorization, released),
            ),
            evidence=evidence,
        )
        _LOG.info(
            "cash.deployed",
            case_id=portfolio.case_id,
            isin=tagged.isin,
            sleeve=sleeve.value,
            deployed=str(released),
            queue_remaining=str(drained.total),
            entry_id=entry.id,
        )
        return DeploymentDecision(
            order=tagged,
            sleeve=sleeve,
            assessment=assessment,
            entry=entry,
            queue=drained,
            deployed=released,
        )

    # ── internals ────────────────────────────────────────────────────────────────────────────────

    def _deployable_sleeve(
        self, order: ProposedOrder, portfolio: Portfolio, authorization: BuyAuthorization
    ) -> Sleeve:
        """The sleeve a deployment is authorized under, or raise — §5.6's gate in one place.

        A deployment must be a buy (cash goes *into* a position), against an authorization for this
        exact instrument and case, and for a real sleeve — CORE or TACTICAL, never CASH (parking is
        `park`, not a deployment). Each failure names what was wrong, since the caller is the daily
        loop and "invalid" tells it nothing it can act on.
        """
        if order.side is not Side.BUY:
            raise UndeployableError(
                f"a deployment puts cash into a position, so it is a buy; got a {order.side.value} "
                f"for {order.isin}"
            )
        if authorization.sleeve is Sleeve.CASH:
            raise UndeployableError(
                f"a CASH authorization is the parking leg, not a deployment; use park() to move "
                f"idle cash into the ETF. {order.isin} needs a CORE or TACTICAL authorization"
            )
        if authorization.isin != order.isin:
            raise UndeployableError(
                f"the authorization is for {authorization.isin} but the order buys {order.isin}: a "
                "deployment must be backed by an authorization for the instrument it buys (§5.6)"
            )
        if authorization.case_id != portfolio.case_id:
            raise UndeployableError(
                f"the authorization is for case {authorization.case_id} but the book is "
                f"{portfolio.case_id}: a deployment is authorized per case (§5.5)"
            )
        return authorization.sleeve

    def _cash_order(self, shares: int, price: Decimal, sector: str) -> ProposedOrder:
        """The parking ETF buy: a whole-share market buy of the ratified parking ISIN, tagged CASH.

        The ISIN is the cash policy's, never the caller's — the parking instrument is a ratified
        policy, and joining the cash leg on anything but its ISIN is invariant #2's forbidden
        shortcut. The `CASH` tag rides to the broker so X1 files the fill under the cash sleeve.
        """
        return ProposedOrder(
            request=OrderRequest(
                isin=self.cash_policy.parking_isin,
                side=Side.BUY,
                quantity=shares,
                exchange=Exchange.NSE,
                order_type=OrderType.MARKET,
                tag=Sleeve.CASH.value,
            ),
            price=price,
            sector=sector,
        )

    def _total_sources(self, sources: Mapping[CashSource, Decimal]) -> Decimal:
        """Total and validate the cash to park — a positive `Decimal` sum over named sources."""
        if not sources:
            raise CashError(
                "park needs at least one cash source (exit proceeds or a SIP instalment); there is "
                "nothing to park otherwise"
            )
        total = _ZERO
        for source, amount in sources.items():
            total += _require_positive_money(f"source {source.value}", amount)
        return total

    def _park_rationale(
        self, amount: Decimal, shares: int, price: Decimal, sources: Mapping[CashSource, Decimal]
    ) -> str:
        """The recorded reason for a park — the amount, the sources, and the instrument (§0)."""
        parts = ", ".join(f"{source.value} {value}" for source, value in sources.items())
        return (
            f"parking {amount} idle cash ({parts}) as {shares} shares of "
            f"{self.cash_policy.parking_symbol} at {price} the same session (§5.6, decision #10)"
        )

    def _park_payload(
        self,
        sources: Mapping[CashSource, Decimal],
        shares: int,
        price: Decimal,
        residual: Decimal,
    ) -> dict[str, str]:
        """Machine-readable park detail: per-source amounts, share count, residual (str only)."""
        payload = {f"source_{source.value}": str(value) for source, value in sources.items()}
        payload["shares"] = str(shares)
        payload["price"] = str(price)
        payload["residual_cash"] = str(residual)
        return payload


def _tag(order: ProposedOrder, sleeve: Sleeve) -> ProposedOrder:
    """Return the order with its request `tag` set to the authorizing sleeve — CORE or TACTICAL.

    As in A6, the tag is authoritative and comes from the authorization, not the caller: an order
    A7 deploys always carries the sleeve that backed it, and X1 reads that tag to file the fill.
    """
    return replace(order, request=replace(order.request, tag=sleeve.value))


def _deploy_payload(authorization: BuyAuthorization, released: Decimal) -> dict[str, str]:
    """Machine-readable deployment detail: what backed it and how much cash was released.

    A CORE deployment records the ratified thesis version and content hash it was authorized on; a
    TACTICAL one records the lightweight rationale A4 required. Either way the journal shows not
    just that cash was deployed but the ratified thing it was deployed against (§5.6, §5.7).
    """
    payload: dict[str, str] = {"deployed_inr": str(released), "sleeve": authorization.sleeve.value}
    if authorization.thesis_version is not None:
        payload["thesis_version"] = str(authorization.thesis_version)
    if authorization.thesis_content_hash is not None:
        payload["thesis_content_hash"] = authorization.thesis_content_hash
    if authorization.rationale is not None:
        payload["tactical_rationale"] = authorization.rationale
    return payload


def _require_positive_money(name: str, value: object) -> Decimal:
    """Refuse a `float` or a non-positive amount at the cash boundary (CLAUDE.md).

    Typed `object` so the `float` guard runs against what the caller actually passed, the same shape
    the rails and the queue use; a price or an amount that arrived as a float sizes the wrong order.
    """
    if isinstance(value, float):
        raise CashError(
            f"{name} must be a Decimal, got float {value!r}; money is never float (CLAUDE.md)"
        )
    if not isinstance(value, Decimal):
        raise CashError(f"{name} must be a Decimal, got {type(value).__name__}")
    if value <= _ZERO:
        raise CashError(f"{name} must be positive, got {value}")
    return value
