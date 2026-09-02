"""M3.3 — the D7 cross-exchange price-sanity checker.

The two things asserted here are M3.3's two acceptance criteria:

1. **A synthetic divergence flags; matched prices do not.** `test_divergent_liquid_closes_flag` and
   `test_matched_closes_do_not_flag` are the pair. Both sides carry real liquidity, so the only
   variable is whether the closes agree — a 10% gap is flagged, closes within the band are not.

2. **Thin-volume BSE prints are excluded, with the rationale documented.** `test_thin_bse_*` show
   both gates (absolute rupee floor and relative fraction of the deeper book) suppressing a gap that
   would otherwise flag, and `test_same_gap_flags_once_bse_is_liquid` is the load-bearing contrast:
   the *identical* divergence flags once the thin side trades enough to be a credible price. The
   documented rationale rides on the finding itself — `test_finding_records_the_thinness_rule`
   asserts the thresholds the rule applied are in `detail`.

Offline and deterministic (AGENTIC_CONTEXT B8): no network, no Postgres, no clock. The rule is pure,
so every case is a `SentinelInput` in and `QualityFinding`s out.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from dataplatform.identity.master import Exchange
from dataplatform.quality import (
    ExchangeClose,
    QualityFinding,
    SentinelInput,
    default_rules,
    run_sentinel,
)
from dataplatform.quality.rules.cross_exchange import (
    CROSS_EXCHANGE_CHECK,
    CrossExchangeRule,
)

INFY = "INE009A01021"
TCS = "INE467B01029"
SESSION = date(2026, 9, 1)

NSE_SRC = "nse_bhavcopy"
BSE_SRC = "bse_bhavcopy"

# Liquidity levels used across the cases. "Deep" clears both thin gates against any of these; the
# thin levels are chosen to isolate one gate at a time (see the thin tests).
DEEP = Decimal("100000000")  # ₹10 crore turnover — an unambiguously liquid session


def ec(
    *,
    isin: str = INFY,
    on: date = SESSION,
    exchange: Exchange,
    close: str,
    turnover: Decimal,
    source: str,
) -> ExchangeClose:
    """An `ExchangeClose` with Decimal money, defaulting the fields a case does not vary."""
    return ExchangeClose(
        isin=isin,
        date=on,
        exchange=exchange,
        close=Decimal(close),
        turnover=turnover,
        source=source,
    )


def nse(close: str, turnover: Decimal = DEEP, *, isin: str = INFY) -> ExchangeClose:
    return ec(isin=isin, exchange=Exchange.NSE, close=close, turnover=turnover, source=NSE_SRC)


def bse(close: str, turnover: Decimal, *, isin: str = INFY) -> ExchangeClose:
    return ec(isin=isin, exchange=Exchange.BSE, close=close, turnover=turnover, source=BSE_SRC)


def run(
    *closes: ExchangeClose, rule: CrossExchangeRule | None = None
) -> tuple[QualityFinding, ...]:
    """Run just the cross-exchange rule over the given closes."""
    return run_sentinel(SentinelInput(exchange_closes=closes), rules=(rule or CrossExchangeRule(),))


# ── acceptance 1 — a synthetic divergence flags; matched prices do not ────────────────────────


def test_divergent_liquid_closes_flag() -> None:
    """NSE 100 vs BSE 110, both deeply traded → one WARN finding off the deeper book."""
    findings = run(nse("100", DEEP), bse("110", DEEP))

    assert len(findings) == 1
    (finding,) = findings
    assert finding.check_name == CROSS_EXCHANGE_CHECK
    assert finding.severity == "WARN"
    assert finding.isin == INFY
    assert finding.logical_date == SESSION
    assert finding.observed_value == Decimal("0.10")  # |110-100|/100, off the reference close
    assert finding.threshold == Decimal("0.03")


def test_matched_closes_do_not_flag() -> None:
    """Two liquid books that agree raise nothing — an exact match and a within-band gap alike."""
    assert run(nse("100", DEEP), bse("100", DEEP)) == ()
    assert run(nse("100", DEEP), bse("102", DEEP)) == ()  # +2%, inside the 3% band


def test_gap_exactly_at_threshold_is_not_flagged() -> None:
    """The rule fires strictly above the threshold; a gap sitting on 3% is not an anomaly."""
    assert run(nse("100", DEEP), bse("103", DEEP)) == ()  # exactly +3%


def test_single_exchange_session_is_not_compared() -> None:
    """An ISIN that printed on only one exchange this session has nothing to diverge from."""
    assert run(nse("100", DEEP)) == ()


def test_reference_is_the_higher_turnover_book() -> None:
    """When BSE is the deeper book, divergence is measured against BSE, not NSE."""
    # NSE thin-ish but still liquid enough to compare (2% of BSE); BSE the deep reference.
    findings = run(
        nse("120", Decimal("5000000")),  # ₹50 lakh
        bse("100", DEEP),  # ₹10 crore — the reference
    )
    (finding,) = findings
    assert finding.detail["reference_exchange"] == "BSE"
    assert finding.detail["reference_close"] == "100"
    assert finding.observed_value == Decimal("0.20")  # |120-100|/100, off the BSE reference


# ── acceptance 2 — thin BSE prints excluded, rationale documented ─────────────────────────────


def test_thin_bse_below_absolute_floor_is_excluded() -> None:
    """A 30% gap against a BSE print of ₹50k turnover (below the ₹1 lakh floor) is not flagged."""
    assert run(nse("100", DEEP), bse("130", Decimal("50000"))) == ()


def test_thin_bse_below_relative_fraction_is_excluded() -> None:
    """Above the absolute floor but only 0.5% of NSE turnover — the dead side of a lopsided pair."""
    # ₹5 lakh clears the ₹1 lakh floor, but is 0.5% of ₹10 crore, under the 1% relative gate.
    assert run(nse("100", DEEP), bse("130", Decimal("500000"))) == ()


def test_same_gap_flags_once_bse_is_liquid() -> None:
    """The load-bearing contrast: the identical 30% gap flags when BSE trades enough to be real."""
    # ₹2 crore is 20% of the NSE turnover — comfortably past both thin gates.
    findings = run(nse("100", DEEP), bse("130", Decimal("20000000")))
    assert len(findings) == 1
    assert findings[0].observed_value == Decimal("0.30")


def test_both_sides_thin_is_not_flagged() -> None:
    """A rarely-traded dual listing where both prints are slivers: a gap there is noise."""
    assert run(nse("100", Decimal("40000")), bse("130", Decimal("30000"))) == ()


def test_finding_records_the_thinness_rule() -> None:
    """The documented rationale rides on the flag: the thresholds applied and both sides' facts."""
    (finding,) = run(nse("100", DEEP), bse("110", DEEP))
    detail = finding.detail
    assert detail["min_turnover"] == "100000"
    assert detail["thin_fraction"] == "0.01"
    assert detail["reference_exchange"] == "NSE"
    assert detail["comparison_exchange"] == "BSE"
    assert detail["comparison_close"] == "110"
    assert detail["comparison_source"] == BSE_SRC
    # the flag scopes to the thinner (more probable culprit) side, not market-wide
    assert finding.source == BSE_SRC


# ── engine integration, configurability, robustness ───────────────────────────────────────────


def test_rule_is_auto_discovered() -> None:
    """The rule registers purely by living under `rules/` — no manual import, no engine edit."""
    names = {rule.name for rule in default_rules()}
    assert CROSS_EXCHANGE_CHECK in names


def test_threshold_is_a_construction_argument() -> None:
    """A stricter variant is another instance — the divergence threshold is data, not code."""
    closes = (nse("100", DEEP), bse("102", DEEP))  # +2%
    assert run(*closes) == ()  # default 3% rule ignores it
    strict = CrossExchangeRule(threshold=Decimal("0.01"))
    assert len(run(*closes, rule=strict)) == 1  # 1% rule flags it


def test_severity_is_configurable_to_error() -> None:
    """A deployment that must block trading raises it to ERROR without a code change."""
    blocking = CrossExchangeRule(severity="ERROR")
    (finding,) = run(nse("100", DEEP), bse("110", DEEP), rule=blocking)
    assert finding.severity == "ERROR"


def test_duplicate_exchange_close_is_a_loud_error() -> None:
    """Two closes for the same (isin, exchange, date) is a contradiction and raises, not wins."""
    with pytest.raises(ValueError, match="one exchange publishes one close per session"):
        run(nse("100", DEEP), nse("101", DEEP))


def test_per_isin_and_deterministic_across_runs() -> None:
    """Divergences are per ISIN, and two runs over the same input produce identical findings."""
    closes = (
        nse("100", DEEP, isin=INFY),
        bse("110", DEEP, isin=INFY),
        nse("200", DEEP, isin=TCS),
        bse("201", DEEP, isin=TCS),  # +0.5%, within band — TCS does not flag
    )
    first = run(*closes)
    second = run(*closes)
    assert first == second
    assert [f.isin for f in first] == [INFY]  # only INFY diverged
