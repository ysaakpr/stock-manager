"""The PR-bundle witness for an unexplained move — what NSE itself printed about that session.

`unexplained_move` raises an ERROR for a >20% close-to-close move with no reconciled corporate
action on its ex-date. That rule's input is the `corporate_actions` table, which is sparse before
2016 and whose `knowable_date` is ingest day; the move is often perfectly explicable from the
exchange's own same-session files, and a human triaging the flag should see that first.

This rule annotates; **it never suppresses, downgrades or resolves.** For every finding the
unexplained-move rule would raise on the same input, it looks up `SentinelInput.move_witnesses`
for `(isin, session)` and, when NSE printed something about that security that session, emits one
INFO finding under `unexplained_move_witness` naming each witness:

* `corp_ind:<code>` — the `Pd` member's `CORP_IND` ex-marker. Measured against
  `corporate_actions` on 2026-10-06: `XB` sits on 681 bonus ex-dates, `XR` on 473 rights, `XD`
  and `XDO` on 27,355 dividends, and `XO` on buybacks (266), demergers (202), splits (93) and
  schemes (62) — but also on 7,861 sessions with no corporate action at all. So `B`/`R` in the
  code is **strong**, `O` alone is **other**, a pure `XD`/`XI` is **weak** (it explains a >20%
  gap only for a payout that large). NSE does not mark splits: 276 of 399 split ex-dates carry
  no `CORP_IND`, which is why the broadcast witness below matters.
* `band_hit:H` / `band_hit:L` — the `bh` member: the security hit its upper or lower band.
  Counted only when the side agrees with the move's direction (an up-move with `H`); **band**.
* `ca_broadcast:<TAG>` — a `Bc` row for the security with this session as its ex-date, knowable
  by then, tagged by the coarse purpose keywords (`BONUS`, `SPLIT`, `RIGHTS`, `SCHEME`,
  `CAPITAL_REDUCTION` are strong; `BUYBACK`, `OPEN_OFFER`, `NAME_CHANGE` other; dividends,
  meetings, interest, redemptions and untagged text weak).

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
_ENTITLEMENT_LETTERS: Final = frozenset("BR")

#: Broadcast purpose tags (`pr_bundle.survey.PURPOSE_TAGS`) by what they say about a price gap.
_STRONG_PURPOSES: Final = frozenset({"BONUS", "SPLIT", "RIGHTS", "SCHEME", "CAPITAL_REDUCTION"})
_OTHER_PURPOSES: Final = frozenset({"BUYBACK", "OPEN_OFFER", "NAME_CHANGE"})

#: Strengths, strongest first — the order a summary picks a finding's best witness by.
STRENGTHS: Final[tuple[str, ...]] = ("strong", "band", "other", "weak")


def witness_strength(witness: str) -> str:
    """`strong`, `band`, `other` or `weak` — see the module docstring for what each means.

    Raises on a witness string this rule does not know, rather than guessing a strength.
    """
    kind, _, value = witness.partition(":")
    if kind == "corp_ind" and value.startswith("X") and len(value) > 1:
        letters = set(value[1:])
        if letters & _ENTITLEMENT_LETTERS:
            return "strong"
        return "other" if "O" in letters else "weak"
    if kind == "band_hit" and value in ("H", "L"):
        return "band"
    if kind == "ca_broadcast" and value:
        if value in _STRONG_PURPOSES:
            return "strong"
        return "other" if value in _OTHER_PURPOSES else "weak"
    raise ValueError(f"unknown move witness {witness!r}")


def best_strength(witnesses: Iterable[str]) -> str:
    """The strongest strength among `witnesses` (which must not be empty)."""
    found = {witness_strength(w) for w in witnesses}
    return next(s for s in STRENGTHS if s in found)


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
            present = {witness_strength(w) for w in found}
            strengths = [s for s in STRENGTHS if s in present]
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
