"""A6: rotation engine — the per-case dial, the two sleeves, and every order tagged.

§5.5 splits a case into a core sleeve (thesis-backed, membership changing only on a `BROKEN`
verdict) and a tactical sleeve sized by the ratified dial (tactical target = `d%` of case value).
This package is that model: `sleeves.py` is the dial's arithmetic and the sleeve split, `engine.py`
is the three trading verbs — `tactical_trade`, `core_tilt`, `core_exit` — each clearing A8 (rails
still bind, invariant #6) and journaling its outcome with the sleeve tag.

Two properties this package exists to guarantee, both proved in `tests/unit/test_rotation.py`:

* **Core membership is protected.** A core sell without a `BROKEN` break condition raises; so does
  a tilt that would add or remove a core name. The only membership change is an exit on a broken
  thesis (decision #4).
* **The boundary moves only through governance.** The dial is read off the ratified `PolicySet` and
  there is no setter; a rotation engine refuses to run on an unratified set, and resizing the dial
  is `resize_dial`, which returns a new proposal version requiring ratification (§5.5).
"""

from analyst.rotation.engine import RotationDecision, RotationEngine
from analyst.rotation.sleeves import (
    CoreMembershipError,
    DialResizeError,
    RotationError,
    RotationSleeve,
    SleeveAllocation,
    SleeveTargets,
    UnratifiedDialError,
    allocate,
    resize_dial,
    sleeve_targets,
)

__all__ = [
    "CoreMembershipError",
    "DialResizeError",
    "RotationDecision",
    "RotationEngine",
    "RotationError",
    "RotationSleeve",
    "SleeveAllocation",
    "SleeveTargets",
    "UnratifiedDialError",
    "allocate",
    "resize_dial",
    "sleeve_targets",
]
