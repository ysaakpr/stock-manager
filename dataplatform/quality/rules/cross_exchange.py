"""The cross-exchange price-sanity tripwire (M3.3, M3 gate box 1).

A dual-listed ISIN prints a close on both NSE and BSE, and for the same company on the same day the
two should agree to within a fraction of a percent — cross-listing arbitrage keeps them tied. When
they diverge materially, one side's close is wrong: a corporate-action adjustment applied on one
exchange but not the other, a decimal shift in a parser, or a wrong print. That is a value the
sentinel exists to surface, and it is one the unexplained-move rule (M2.8) can miss entirely — a
close that is 5% off on one exchange because a bonus was adjusted there but not here produces no
>20% day-over-day jump, so nothing else notices it.

**Why the rule must not cry wolf — the thin-BSE-print problem.** The naive check ("closes differ by
>X% → flag") drowns in false positives, because a dual listing is very often deep on one exchange
and near-dead on the other. A BSE close backed by a handful of shares is a *stale sliver*: the last
trade may be hours old, struck far from where the deep NSE book actually cleared. That print is not
a data error — it is simply illiquid — and flagging it every day would train the operator to ignore
the check (invariant #10 only works if a raised flag means something). So the rule compares NSE and
BSE only when *both* sides carry real liquidity, and excludes a thin comparison print with the
reason recorded. Two gates, both on turnover (D2 already fixes turnover, not share count, as the
platform's liquidity yardstick — a penny stock's share volume dwarfs a heavyweight's):

* **Absolute floor** (`min_turnover`): a side whose session turnover is below this is a sliver, and
  a divergence against it is noise. Skipped.
* **Relative floor** (`thin_fraction`): even above the absolute floor, a side trading at a tiny
  fraction of the *other* exchange's turnover is the thin side of a lopsided pair; its price is not
  a credible second opinion. Skipped. This is the self-scaling gate — it handles a heavyweight and
  a small-cap with one number, where an absolute floor alone cannot.

The comparison is framed reference-vs-comparison, where the **reference is the higher-turnover
exchange** (its deeper book holds the more trustworthy price) and divergence is measured as a
fraction of the reference close. For the standard NSE-heavy dual listing the reference is NSE and
the comparison is BSE, which is exactly the "thin BSE volume" case the task names; the roles are
assigned by liquidity rather than hard-coded so the rare BSE-deep security is handled symmetrically.

**Severity is WARN by default, and deliberately so.** A divergence tells us one of the two closes is
wrong but *not which one* — attribution is genuinely impossible from the prices alone. Raising ERROR
would either halt the entire market (a market-wide flag for one ISIN's mismatch — the loudest
possible cry-wolf) or halt only the dataset we guessed at, which may not even be the one feeding the
canonical series (M3.2 already prefers the liquid exchange's price). WARN surfaces the divergence on
`/status/quality` for an operator to adjudicate without tripping the trading interlock. Severity is
a construction argument, so an operator who decides a given deployment should block on this can
raise it to ERROR without touching the rule.

This whole file is a *rule addition*: it imports from the engine, defines a class satisfying
`SentinelRule`, reads the `exchange_closes` input, and registers itself. `run_sentinel` is untouched
— the engine never learns what this rule checks (M2.8 acceptance criterion 3, still structural).
Pure: `evaluate` reads only its `SentinelInput` and touches no database and no clock.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from dataplatform.identity.master import Exchange
from dataplatform.quality.sentinel import (
    ExchangeClose,
    QualityFinding,
    SentinelInput,
    Severity,
    finding_fingerprint,
    register,
)

#: The `quality_flag.check_name` every cross-exchange finding is filed under.
CROSS_EXCHANGE_CHECK: Final = "cross_exchange_divergence"

#: Fractional gap between the two exchanges' closes (of the reference close) above which the pair is
#: flagged. 3% is generous: identical securities on two exchanges close within a fraction of a
#: percent, so only a genuine error — a missed adjustment, a decimal shift — reaches 3%. A gap *at*
#: the threshold is not flagged; the rule fires strictly above it.
DEFAULT_DIVERGENCE_THRESHOLD: Final = Decimal("0.03")

#: Absolute session-turnover floor (INR). A side below this is a stale sliver, not a price; a
#: divergence against it is illiquidity, not a data error. ₹1,00,000 ≈ a single modest trade.
DEFAULT_MIN_TURNOVER: Final = Decimal("100000")

#: Relative floor: the lower-turnover side must trade at least this fraction of the higher side's
#: turnover to count as a credible second opinion. 1% excludes the thin side of a lopsided listing.
DEFAULT_THIN_FRACTION: Final = Decimal("0.01")

#: Tie-break for reference selection when the two exchanges' turnover is exactly equal. NSE-first,
#: matching D2's documented primary-listing convention (`primary.py`), so the reference book — and
#: thus the divergence and `detail` — stays deterministic and consistent with the canonical series.
_REFERENCE_ORDER: Final = (Exchange.NSE, Exchange.BSE)


@dataclass(frozen=True)
class CrossExchangeRule:
    """A `SentinelRule` flagging NSE/BSE close divergence on genuinely dual-traded sessions.

    Pure: `evaluate` reads only `data.exchange_closes` and returns findings; no database, no clock.
    Every knob is a field so a stricter or blocking variant is a construction argument, not an edit:
    `threshold` (divergence that fires), `min_turnover` and `thin_fraction` (the two thin-print
    gates), and `severity` (WARN by default — see the module docstring for why attribution makes
    ERROR the wrong global default).
    """

    name: str = CROSS_EXCHANGE_CHECK
    severity: Severity = "WARN"
    threshold: Decimal = DEFAULT_DIVERGENCE_THRESHOLD
    min_turnover: Decimal = DEFAULT_MIN_TURNOVER
    thin_fraction: Decimal = DEFAULT_THIN_FRACTION

    def evaluate(self, data: SentinelInput) -> Iterable[QualityFinding]:
        """One finding per (isin, date) whose two liquid closes diverge beyond the threshold."""
        for _key, by_exchange in sorted(_group(data.exchange_closes).items()):
            if len(by_exchange) < 2:
                continue  # only one exchange printed this session — nothing to compare against

            # Reference = the deeper (higher-turnover) book; comparison = the thinner side. An exact
            # turnover tie falls back to the NSE-first convention so the pairing is deterministic.
            ordered = sorted(
                by_exchange.values(),
                key=lambda ec: (-ec.turnover, _REFERENCE_ORDER.index(ec.exchange)),
            )
            reference, comparison = ordered[0], ordered[1]

            if self._is_thin(reference, comparison):
                continue  # thin comparison print — illiquidity, not a data error (see docstring)

            divergence = abs(comparison.close - reference.close) / reference.close
            if divergence <= self.threshold:
                continue  # the two liquid books agree — matched prices, no flag

            yield self._finding(reference, comparison, divergence)

    def _is_thin(self, reference: ExchangeClose, comparison: ExchangeClose) -> bool:
        """True when the comparison side is too thinly traded for its close to be a real opinion.

        Either gate suffices: below the absolute rupee floor the print is a sliver on any scale;
        below the relative fraction it is the dead side of a lopsided listing even if not tiny in
        absolute terms. Excluding here — rather than flagging — is the whole "do not cry wolf" of
        the rule (acceptance 2), so the exclusion is intentional and documented, not a silent drop.
        """
        if comparison.turnover < self.min_turnover:
            return True
        return comparison.turnover < self.thin_fraction * reference.turnover

    def _finding(
        self, reference: ExchangeClose, comparison: ExchangeClose, divergence: Decimal
    ) -> QualityFinding:
        """Project a divergent pair onto a `QualityFinding`.

        `observed_value` is the divergence magnitude (a fraction of the reference close);
        `threshold` is what it breached. `detail` records both exchanges' closes, turnovers and
        sources plus which side was taken as reference, so a human can adjudicate *which* close is
        wrong — the one thing prices alone cannot tell us. Decimals are stringified: the exact value
        must survive the JSON round trip a float would corrupt. `source` is stamped to the
        comparison (thinner) side, the more probable culprit and the correct scope for a flag that
        must not halt trading on securities living only on the healthier exchange. The fingerprint
        is the (check, isin, date) key, so re-scanning the session raises nothing new.
        """
        detail: dict[str, object] = {
            "reference_exchange": reference.exchange.value,
            "reference_close": str(reference.close),
            "reference_turnover": str(reference.turnover),
            "reference_source": reference.source,
            "comparison_exchange": comparison.exchange.value,
            "comparison_close": str(comparison.close),
            "comparison_turnover": str(comparison.turnover),
            "comparison_source": comparison.source,
            "divergence": str(divergence),
            "min_turnover": str(self.min_turnover),
            "thin_fraction": str(self.thin_fraction),
        }
        return QualityFinding(
            logical_date=comparison.date,
            check_name=self.name,
            severity=self.severity,
            isin=comparison.isin,
            source=comparison.source,
            observed_value=divergence,
            threshold=self.threshold,
            detail=detail,
            fingerprint=finding_fingerprint(self.name, comparison.isin, comparison.date),
        )


def _group(
    closes: Iterable[ExchangeClose],
) -> dict[tuple[str, date], dict[Exchange, ExchangeClose]]:
    """Index closes by (isin, date) → exchange → close.

    Two closes for the same (isin, exchange, date) are a raw contradiction — one exchange publishes
    one close per session — and raise rather than letting one silently win, mirroring how M3.2's
    `canonical_daily` refuses to arbitrate duplicate raw rows.
    """
    grouped: dict[tuple[str, date], dict[Exchange, ExchangeClose]] = {}
    for close in closes:
        by_exchange = grouped.setdefault((close.isin, close.date), {})
        if close.exchange in by_exchange:
            raise ValueError(
                f"two closes for ISIN {close.isin!r} on {close.exchange.value} "
                f"{close.date.isoformat()}; one exchange publishes one close per session"
            )
        by_exchange[close.exchange] = close
    return grouped


register(CrossExchangeRule())
