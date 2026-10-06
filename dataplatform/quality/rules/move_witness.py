"""The PR-bundle witness for an unexplained move — what NSE itself printed about that session.

`unexplained_move` raises an ERROR for a >20% close-to-close move with no reconciled corporate
action on its ex-date. That rule's input is the `corporate_actions` table, which is sparse before
2016 and whose `knowable_date` is ingest day; the move is often perfectly explicable from the
exchange's own same-session files, and a human triaging the flag should see that first.

This rule annotates; **it never suppresses, downgrades or resolves.** For every finding the
unexplained-move rule would raise on the same input, it looks up `SentinelInput.move_witnesses`
for `(isin, session)` and, when NSE printed something about that security that session, emits one
INFO finding under `unexplained_move_witness` naming each witness:

* `corp_ind:<code>` — the `Pd` member's `CORP_IND` ex-marker (`XB` bonus, `XR` rights, `XO`
  other, `XD` dividend, `XI` interest, and combinations). An entitlement marker (`B`, `R` or `O`
  in the code) is a strong witness that the price gap is a corporate action the CA feed missed; a
  pure `XD`/`XI` explains a >20% move only if the payout was that large, and is labelled weak.
* `band_hit:H` / `band_hit:L` — the `bh` member: the security hit its upper or lower band. Only
  counted when the side agrees with the move's direction (an up-move with `H`).
* `ca_broadcast` — a `Bc` row for the security with this session as its ex-date.

The ERROR flag stays open either way; resolving it is a human's call (invariant #10's interlock
is not this rule's to loosen). Pure: reads only its `SentinelInput`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from dataplatform.quality.rules.unexplained_move import (
    DEFAULT_MOVE_THRESHOLD,
    UNEXPLAINED_MOVE_CHECK,
    UnexplainedMoveRule,
)
from dataplatform.quality.sentinel import (
    QualityFinding,
    SentinelInput,
    Severity,
    finding_fingerprint,
    register,
)

#: The `quality_flag.check_name` every witness annotation is filed under.
MOVE_WITNESS_CHECK: Final = "unexplained_move_witness"

#: `CORP_IND` letters that change the share count or the claim on it — a price gap's cause.
_ENTITLEMENT_LETTERS: Final = frozenset("BRO")


def witness_strength(witness: str) -> str:
    """`strong` (entitlement marker, broadcast ex-date), `weak` (dividend/interest only), or
    `band` (a band hit). Raises on a witness string this rule does not know."""
    kind, _, value = witness.partition(":")
    if kind == "corp_ind":
        return "strong" if set(value[1:]) & _ENTITLEMENT_LETTERS else "weak"
    if kind == "band_hit":
        return "band"
    if kind == "ca_broadcast":
        return "strong"
    raise ValueError(f"unknown move witness {witness!r}")


def applicable_witnesses(witnesses: Iterable[str], change: Decimal) -> tuple[str, ...]:
    """The witnesses that can bear on a move of sign `change`: a band hit only on its own side."""
    side = "H" if change > 0 else "L"
    return tuple(
        sorted(w for w in witnesses if not w.startswith("band_hit:") or w == f"band_hit:{side}")
    )


@dataclass(frozen=True)
class MoveWitnessRule:
    """INFO annotation of each unexplained move that a PR-bundle witness bears on."""

    name: str = MOVE_WITNESS_CHECK
    severity: Severity = "INFO"
    threshold: Decimal = DEFAULT_MOVE_THRESHOLD

    def evaluate(self, data: SentinelInput) -> Iterable[QualityFinding]:
        """One INFO finding per would-be `unexplained_move` finding that has a witness."""
        rule = UnexplainedMoveRule(threshold=self.threshold)
        for flagged in rule.evaluate(data):
            assert flagged.isin is not None  # the move rule always names its ISIN
            change = flagged.observed_value
            assert change is not None
            found = applicable_witnesses(
                data.move_witnesses.get((flagged.isin, flagged.logical_date), ()), change
            )
            if not found:
                continue
            strengths = sorted({witness_strength(w) for w in found})
            yield QualityFinding(
                logical_date=flagged.logical_date,
                check_name=self.name,
                severity=self.severity,
                isin=flagged.isin,
                source=flagged.source,
                observed_value=change,
                threshold=self.threshold,
                detail={
                    "witnesses": list(found),
                    "strength": strengths,
                    "annotates": UNEXPLAINED_MOVE_CHECK,
                    "annotates_fingerprint": flagged.fingerprint,
                    "resolves": False,
                },
                fingerprint=finding_fingerprint(self.name, flagged.isin, flagged.logical_date),
            )


register(MoveWitnessRule())
