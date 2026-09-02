"""A6: the vocabulary of the rotation engine — the dial's arithmetic and the sleeve split.

This module is data and pure arithmetic, not decisions. It answers three questions the engine
(`engine.py`) needs and the downstream cash manager (A7) and monitor (A5) will ask too:

* **What does the dial mean in rupees?** `sleeve_targets` turns the ratified `RotationDial` (§5.2
  policy 3) and a case value into the tactical and core rupee targets of §5.5 — tactical target =
  `d%` of case value, core = the remainder. The dial is a *target*, not a rail: the hard cap on
  any single trade is A8, and the dial steers within it.
* **Where does the book actually sit against those targets?** `allocate` splits a `Portfolio` into
  its core and tactical value given which ISINs are in the tactical sleeve, and reports the drift
  from target so a caller can see how far a rotation has pulled the book from the ratified mix.
* **What tag does an order carry?** `RotationSleeve` is CORE or TACTICAL — the two sleeves the
  rotation engine trades in. CASH exists on `analyst.journal.Sleeve` for the deployment queue (A7),
  but a rotation order is never cash, so it is rejected here rather than silently accepted.

Resizing the boundary between the two sleeves — moving the dial — is a **policy change** (§5.5),
which is why there is no setter for it anywhere: the dial is read off the ratified `PolicySet`, and
changing it is `resize_dial`, which returns a new *proposal* version that must be ratified before
anything runs on it. That is the acceptance-criterion-3 contract, enforced by construction.

Money and percentages are `Decimal` (CLAUDE.md) — a sleeve target off by a float epsilon is a
target the drift arithmetic then reports wrongly. Identity is the ISIN (invariant #2); the tactical
membership `allocate` takes is a set of ISINs, never symbols. Nothing here reads a clock, a database
or the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from analyst.cases import PolicySet, PolicyStatus, RotationDial
from analyst.journal import Sleeve
from analyst.rails import Portfolio

__all__ = [
    "CoreMembershipError",
    "DialResizeError",
    "RotationError",
    "RotationSleeve",
    "SleeveAllocation",
    "SleeveTargets",
    "UnratifiedDialError",
    "allocate",
    "resize_dial",
    "sleeve_targets",
]

_ZERO: Final = Decimal(0)
_HUNDRED: Final = Decimal(100)

#: The two sleeves the rotation engine trades in (§5.5). A rotation order is CORE or TACTICAL; the
#: CASH sleeve is the deployment queue's (A7), never a rotation decision's, so it is not admitted.
RotationSleeve: Final = frozenset({Sleeve.CORE, Sleeve.TACTICAL})


# ── errors ───────────────────────────────────────────────────────────────────────────────────


class RotationError(Exception):
    """Base for every rotation-engine failure, so callers can catch the module."""


class CoreMembershipError(RotationError):
    """A core-membership change was attempted outside the one contract that permits it.

    §5.5 / decision #4: core membership changes on a `BROKEN` verdict only. A core sell without a
    BROKEN verdict, or a "tilt" that would add or remove a core name rather than steer new money
    within the existing members, is refused here — the exit path is A6's `core_exit` with a broken
    break condition, and adding a name is A4's ratified-thesis path, never a rotation tilt.
    """


class UnratifiedDialError(RotationError):
    """A rotation engine was built on a policy set that is not RATIFIED.

    The dial is a ratified policy (§5.2 policy 3): the agent may only rotate inside a mix a human
    approved. Operating on a `PROPOSAL` would mean trading under a boundary nobody ratified, so it
    is refused at construction — which is what makes "changing the dial requires a new ratified
    policy version" true rather than aspirational (acceptance criterion 3).
    """


class DialResizeError(RotationError):
    """A dial resize was requested against something other than the ratified current version.

    Resizing is `revise()` on the ratified set, producing the next proposal; asking to resize a
    proposal or a superseded version is a bookkeeping error, because the thing being resized is
    always the mix currently in force.
    """


# ── the dial's arithmetic ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SleeveTargets:
    """The rupee target for each sleeve at one case value, under one ratified dial (§5.5).

    What it does: carry the tactical percentage the dial fixes and the case value it is applied to,
    and derive the two rupee targets — tactical = `d%` of case value, core = the remainder.
    What it assumes: `case_value` is the case's total value (deployed plus idle), the same figure
    every percentage in this system is a fraction of.
    What it never does: store the core target as a second number that could drift from the tactical
    one — it is derived, exactly as `RotationDial.core_pct` derives the core share.
    """

    case_value: Decimal
    tactical_pct: Decimal

    @property
    def tactical_target(self) -> Decimal:
        """Rupees the tactical sleeve targets: `tactical_pct%` of case value."""
        return self.case_value * self.tactical_pct / _HUNDRED

    @property
    def core_target(self) -> Decimal:
        """Rupees the core sleeve targets: the remainder after tactical."""
        return self.case_value - self.tactical_target


def sleeve_targets(dial: RotationDial, case_value: Decimal) -> SleeveTargets:
    """Turn the ratified dial and a case value into the two sleeve targets (§5.5).

    What it does: read the ratified `tactical_pct` off the dial and pair it with the case value, so
    a caller has the rupee target for each sleeve without re-deriving the split.
    What it assumes: `case_value` is a `Decimal` and not negative — a case with no value has both
    targets at zero, not a target that is a fraction of a negative.
    What it never does: invent the percentage. It comes from the ratified policy; this function is
    arithmetic, not policy.
    """
    value = _require_case_value(case_value)
    return SleeveTargets(case_value=value, tactical_pct=dial.tactical_pct)


def _require_case_value(value: object) -> Decimal:
    """Refuse a float or a non-negative case value; a target is a fraction and its base is exact.

    Typed `object` rather than `Decimal` on purpose: the float guard has to run against whatever the
    caller actually passed (the same reason `_reject_float` in the policy set takes `Any`), and a
    parameter already typed `Decimal` would make the check dead code to the type checker.
    """
    if isinstance(value, float):
        raise TypeError(
            f"case_value must be a Decimal, got float {value!r}; a sleeve target off by a "
            "float epsilon steers the wrong amount (CLAUDE.md)"
        )
    if not isinstance(value, Decimal):
        raise TypeError(f"case_value must be a Decimal, got {type(value).__name__}")
    if value < _ZERO:
        raise ValueError(f"case_value must not be negative, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class SleeveAllocation:
    """Where a book sits against its dial targets: value and drift per sleeve (§5.5).

    What it does: report the current rupee value of each sleeve and its signed drift from target —
    positive when the sleeve is over its target, negative when it has room to add. Cash counts
    toward neither sleeve's *value* but is part of the case value the targets are computed from, so
    an under-deployed book shows both sleeves below target with the shortfall sitting in cash.
    What it never does: decide anything. Drift is a fact the engine and the cash manager read; the
    dial is a target, and the only hard cap on closing the drift is A8.
    """

    targets: SleeveTargets
    tactical_value: Decimal
    core_value: Decimal
    cash: Decimal

    @property
    def tactical_drift(self) -> Decimal:
        """Tactical value minus its target: positive is over the sleeve, negative is headroom."""
        return self.tactical_value - self.targets.tactical_target

    @property
    def core_drift(self) -> Decimal:
        """Core value minus its target: positive is over the sleeve, negative is headroom."""
        return self.core_value - self.targets.core_target


def allocate(
    portfolio: Portfolio, dial: RotationDial, *, tactical_isins: frozenset[str]
) -> SleeveAllocation:
    """Split a book into its core and tactical value and report drift from the dial's targets.

    What it does: sum the value of the lots whose ISIN is in `tactical_isins` as the tactical
    sleeve, the rest as core, and pair that with the rupee targets `sleeve_targets` derives from the
    dial and the book's total value.
    What it assumes: `tactical_isins` is the current tactical membership — the daily loop knows it
    from the tags on the orders that opened the positions; the core sleeve is everything else held.
    An ISIN in `tactical_isins` the book does not hold contributes zero, which is correct: a sleeve
    is valued by what is in it, not by what could be.
    What it never does: key on a symbol (invariant #2) or value a lot at anything but its marked
    price.
    """
    tactical_value = sum((lot.value for lot in portfolio.lots if lot.isin in tactical_isins), _ZERO)
    core_value = portfolio.invested - tactical_value
    return SleeveAllocation(
        targets=sleeve_targets(dial, portfolio.total_value),
        tactical_value=tactical_value,
        core_value=core_value,
        cash=portfolio.cash,
    )


# ── resizing the boundary is a policy change ───────────────────────────────────────────────────


def resize_dial(policy_set: PolicySet, new_tactical_pct: Decimal) -> PolicySet:
    """Resize the tactical/core boundary — the §5.5 policy change — as a new *proposal* version.

    What it does: build the next policy-set version with the dial moved to `new_tactical_pct`, via
    `PolicySet.revise`, which returns it in `PROPOSAL` carrying `supersedes_version`. The returned
    version is **not** ratified: it cannot back a rotation engine until a human ratifies it, which
    is the whole of "resizing the boundary requires ratification" (§5.5, acceptance criterion 3).
    What it assumes: `policy_set` is the RATIFIED current version — the mix actually in force is the
    thing being resized. A float percentage is refused by `RotationDial` itself, and a no-op resize
    (the same percentage) is refused by `revise`, since re-ratifying an identical dial is nothing.
    What it never does: ratify. Proposing is never ratifying (§5.1); the human ratifies the returned
    version through the M5.8 path, and only then may a new engine run on it.
    """
    if policy_set.status is not PolicyStatus.RATIFIED:
        raise DialResizeError(
            f"a dial resize edits the ratified current version, but this set is "
            f"{policy_set.status.value}; resize the mix that is actually in force"
        )
    return policy_set.revise(rotation_dial=RotationDial(tactical_pct=new_tactical_pct))
