"""The unexplained-move tripwire — the sentinel's charter rule (M2 gate box 3).

Flags any close-to-close move larger than a threshold (20% by default) that no corporate action or
circuit-limit band explains. The reasoning: a >20% overnight move is almost always one of two
things. Either a corporate action landed — a split, bonus, merger or demerger legitimately gaps the
raw price on its ex-date — in which case a reconciled CA whose ex-date is the move's session is the
explanation, and there is nothing wrong. Or no such action exists, in which case the move is either
a genuine market event the platform is about to trade on blind, or (the common case) an unadjusted
price the CA feed missed. Both need a human, so the finding is raised at ERROR, which blocks trading
on that dataset via the interlock (invariant #10).

The circuit-limit escape hatch handles the other benign case: a scrip that simply rode its own
daily price band (the exchange caps the move; a 5%-band stock that opened and closed limit-up moved
exactly its band, not anomalously). When a band is known for the ISIN and the move sits within it,
the move is explained by the limit and no flag is raised.

This whole file is a *rule addition*: it imports from the engine, defines a class satisfying
`SentinelRule`, and registers it. The engine (`sentinel.run_sentinel`) is untouched — which is
acceptance criterion 3 of M2.8, made concrete.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from dataplatform.quality.sentinel import (
    CloseToCloseMove,
    QualityFinding,
    SentinelInput,
    Severity,
    finding_fingerprint,
    register,
)

#: The `quality_flag.check_name` every unexplained-move finding is filed under.
UNEXPLAINED_MOVE_CHECK: Final = "unexplained_move"

#: Default close-to-close magnitude above which a move needs an explanation. §M2 gate box 3 names
#: 20%. A move *at* the threshold is not flagged; the rule fires strictly above it.
DEFAULT_MOVE_THRESHOLD: Final = Decimal("0.20")


@dataclass(frozen=True)
class UnexplainedMoveRule:
    """A `SentinelRule` that flags >`threshold` close-to-close moves lacking a CA/circuit reason.

    Pure: `evaluate` reads only its `SentinelInput` (the moves, the reconciled corporate actions,
    and any known circuit bands) and returns findings. It never reads a database or a clock. The
    `threshold` is a field so a stricter or looser variant is a construction argument, not a code
    change.
    """

    name: str = UNEXPLAINED_MOVE_CHECK
    severity: Severity = "ERROR"
    threshold: Decimal = DEFAULT_MOVE_THRESHOLD

    def evaluate(self, data: SentinelInput) -> Iterable[QualityFinding]:
        """One ERROR finding per move above threshold that no CA or circuit band explains."""
        # Ex-dates of reconciled actions, per ISIN — the explanation lookup. A move whose session
        # is an ISIN's ex-date is a legitimate CA gap, not an anomaly.
        ex_dates: dict[str, set[date]] = {}
        for action in data.corporate_actions:
            ex_dates.setdefault(action.isin, set()).add(action.ex_date)

        for move in data.moves:
            magnitude = abs(move.pct_change)
            if magnitude <= self.threshold:
                continue

            if move.date in ex_dates.get(move.isin, set()):
                continue  # a corporate action explains this gap

            band = data.circuit_bands.get(move.isin)
            if band is not None and magnitude <= band:
                continue  # the move merely rode the exchange's own price limit

            yield self._finding(move)

    def _finding(self, move: CloseToCloseMove) -> QualityFinding:
        """Project one unexplained move onto a `QualityFinding`.

        `observed_value` is the signed change (so a human sees a -34% crash differently from a +34%
        spike); `threshold` is what it breached; `detail` carries the raw prices and the direction
        in JSON-safe strings (Decimals are stringified — the exact value survives the JSON round
        trip that a float would corrupt). The fingerprint is the (check, isin, date) key, so a
        re-scan of the same session raises nothing new.
        """
        change = move.pct_change
        detail: dict[str, object] = {
            "prev_close": str(move.prev_close),
            "close": str(move.close),
            "pct_change": str(change),
            "direction": "up" if change > 0 else "down",
            "explanation_checked": ["corporate_action", "circuit_band"],
        }
        return QualityFinding(
            logical_date=move.date,
            check_name=self.name,
            severity=self.severity,
            isin=move.isin,
            source=move.source,
            observed_value=change,
            threshold=self.threshold,
            detail=detail,
            fingerprint=finding_fingerprint(self.name, move.isin, move.date),
        )


register(UnexplainedMoveRule())
