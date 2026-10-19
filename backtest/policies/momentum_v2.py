"""M9.5 — momentum policy v2: 12-1, turnover banding, regime filter, vol-scaling (§7, X2).

The naive policy (:mod:`backtest.policies.naive_momentum`) existed to validate the *engine*, on
inputs that were still being made trustworthy. With M9.2 (adjusted prices), M9.3 (an investable,
liquid universe) and M9.4 (a real total-return benchmark) in place, this evolves the strategy into
a documented *v2* — four independent, a-priori improvements, **each behind its own toggle** so the
"a-priori vs tuned" line stays checkable and each change can be measured in isolation:

* **12-1 momentum** (``use_12_1``) — rank on the ``t-12m .. t-1m`` return, *skipping the most
  recent month*, instead of the raw ``0..12m`` return. The last month is the one that mean-reverts
  (the short-term reversal effect); dropping it is the standard academic momentum definition
  (Jegadeesh-Titman) and is chosen once, not fitted.
* **Turnover banding / hysteresis** (``sell_band``) — buy into the top ``top_n`` but only *sell* a
  holding once it has fallen out of a wider outer band (the top ``sell_band``, e.g. top-30). A name
  drifting between rank 20 and rank 30 is held rather than churned, which is what cuts the
  ~2079-sell turnover the naive ten-year run showed.
* **Regime filter** (``regime_filter``) — hold the momentum basket only while the index is above its
  own 200-day moving average; when it is below, sell the basket and *park in the liquid sleeve*
  (hold cash), re-entering when the regime turns back up. The classic trend-following overlay that
  sidesteps the worst of a bear leg.
* **Volatility-scaled weights** (``vol_scaled``) — size each name at ``~ 1 / vol`` (risk parity)
  rather than pure equal weight, so a jumpy name does not dominate the book's risk. Weights are
  computed over the same top ``top_n`` and normalised to sum to one.

* **Redeploy proceeds next session** (``redeploy_next_session``) — a mechanical fix to a gap the
  first four leave open. A rebalance sells on session R (filling R+1) and sizes its buys from the
  cash that is *already* free on R, so the sale proceeds land on R+1 and then sit idle until the
  next monthly rebalance: with banding, two to five names a month are sold and 10-25 % of the book
  waits in cash for a month, every month. With the toggle on, the policy remembers the basket it
  chose on R and, on R+1, deploys the freed cash toward the same target weights at R+1's prices —
  one more allocator pass, no new signal, no second look at the ranking. It is an execution
  improvement, not a return knob: the basket is the one already decided.

* **Daily regime checks** (``regime_daily_reentry``, ``regime_daily_exit``, ``regime_daily_band``)
  — M14.5. The regime filter above is read only on the monthly rebalance session, so a book parked
  in cash waits up to a month to re-enter after the index recovers its average, and an invested
  book rides a breakdown until the month turns. With ``regime_daily_reentry`` a parked book
  re-enters, through a full rebalance, on the first non-rebalance session whose regime reads
  risk-on — "parked" is the policy's remembered flag (set by every park, cleared by a risk-on
  rebalance), not an empty book; with ``regime_daily_exit`` an unparked book holding settled
  shares is parked on the first non-rebalance session reading risk-off. ``regime_daily_band``
  widens both daily triggers (re-enter at ``MA * (1 + band)``, exit below ``MA * (1 - band)``) to
  damp whipsaw; the monthly rule is never banded. "Parked" is remembered
  (:attr:`MomentumV2Policy.parked`), not read from the book: A8's minimum-holdings rail refuses
  the sells that would take a book below its floor, so a parked book is rarely empty — it keeps
  its last few names. Both off by default, and absent from the parameters' ``repr`` while off,
  so every configuration defined before them keeps its run fingerprint.

* **Volatility target** (``vol_target_annual``) — a portfolio-level overlay on top of whichever
  basket the other toggles chose. From each name's trailing monthly-return volatility (already on
  the record) and one stated average pairwise correlation, the basket's annualised volatility is
  estimated as ``sqrt((1-rho) * sum(w_i^2 s_i^2) + rho * (sum(w_i s_i))^2)``, and the book is
  scaled to ``min(1, target / estimate)`` of its capital — the rest parked in cash. A book that is
  over its exposure sells pro-rata; one under it buys only up to the cap. It is the standard
  risk-parity-at-the-portfolio-level overlay, chosen once (15 %, rho 0.3) and never fitted.

* **Absolute momentum, residual ranking, profitability** (``absolute_momentum``,
  ``residual_ranking``, ``profitability_filter``) — M16.1, three research variants on D13, each
  documented in :mod:`backtest.policies.momentum_v2_overlays` with its own data seam. A1 leaves a
  basket slot in cash when the name's 12-1 return does not beat the repo-based cash hurdle; A3 ranks
  on the round-2 H1 residual-momentum score instead of the 12-1 return; A4 drops a candidate
  without positive TTM profit in a filing at most 200 days old. Off by default and absent from the
  parameters' ``repr`` while off, like the M14.5 switches.

* **Per-stock entry and review** (``daily_slot_fill``, ``review_sessions``, ``daily_regime_gate``,
  ``daily_regime_park``) — M14.6, research variants on D13 asking whether a whole-book monthly
  cadence costs the book its exposure. ``daily_slot_fill`` re-ranks the universe every session
  (the same point-in-time 12-1 ranking a rebalance uses, recomputed from that session's closes) and,
  while the book holds fewer than ``top_n`` names and the regime gate is open, buys the
  best-ranked names not held whose rank is within ``top_n`` into the free slots, each sized to its
  inverse-volatility share of the book it completes. ``review_sessions`` replaces the calendar
  rebalance with a clock per holding: ``review_sessions`` sessions after its entry (or its last
  review) a holding is sold if it ranks outside ``sell_band``, else kept, topped up toward its
  share when it ranks within ``top_n``, and its clock restarted. ``daily_regime_gate`` reads the
  regime every session and lets risk-off block entries and top-ups without selling anything;
  ``daily_regime_park`` makes a risk-off session park the whole book as well. Off by default and
  absent from the parameters' ``repr`` while off, so D13's fingerprint is unchanged.

* **Valuation gate** (``valuation_gate``, ``valuation_mean_days``, ``valuation_splice``) — research
  (``ops/studies/preregistration-pe-timing-2026-10-19.md``): the market P/E (NIFTY 500, published
  by NSE) read against its own trailing ``valuation_mean_days``-session mean, *cheap* when strictly
  below it. It is a second reading on the same regime seam (:class:`ValuationReading` rides on the
  :class:`RegimeReading`), read on the same monthly rebalance session, and it acts in one of three
  ways (:class:`ValuationGate`): ``ENTRY`` keeps the price regime for parking and re-entry and
  blocks every buy of a rebalance that reads *rich*; ``REPLACE`` parks and re-enters on the P/E
  reading instead of the price regime; ``BOTH`` holds the basket only while the price regime is on
  *and* the P/E reads cheap. Off by default and absent from the parameters' ``repr`` while off.

* **Drawdown-study comparators** (``valuation_series``, ``regime_monthly_band``,
  ``exposure_fraction``) — research (``ops/studies/preregistration-pe-drawdown-2026-10-19.md``).
  ``valuation_series`` points the valuation gate at another published series (NIFTY 50 P/E, or the
  NIFTY 500 close itself for the price-only analogue). ``regime_monthly_band`` makes the monthly
  regime stricter: the basket is held only while the index is at or above ``MA * (1 + band)``.
  ``exposure_fraction`` caps the basket at a constant share of capital on every rebalance (and
  its next-session redeploy), the rest in cash — D13 at a fixed lower exposure, with no timing
  information of its own. Off by default and absent from the parameters' ``repr`` while off.

With **all six toggles off** the policy reproduces the naive top-N decision exactly — that is the
"naive" baseline the increment report is struck against, and a unit test pins the parity. Every knob
is a stated parameter, echoed verbatim into the report, so nothing here is silently tuned.

Point-in-time (invariant #7): the 12-1 and 0-12 returns, the volatility estimate and the regime
reading are all carried on records tagged with the date they became knowable, and every one is read
through ``ctx.pit.admit`` — a datum not yet knowable on the session date trips the guard rather than
leaking. The volatility estimate and the moving average read only closes on or before the session.

What it never does: read a wall clock (time is ``ctx.clock`` — B10), key on a symbol (ISIN only —
invariant #2), hold a cost model (the injected broker owns the one shared model — invariant #4/#5),
or reach data outside the point-in-time context. Given the same inputs it returns the same decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import date
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from analyst.cases import RiskRails
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from backtest.policies.industry_gate import apply_industry_gate
from backtest.policies.momentum_v2_overlays import (
    AbsoluteMomentumData,
    ProfitabilityData,
    ResidualRankingData,
    absolute_momentum_evidence,
    profitability_evidence,
    profitable,
    residual_scores,
)
from backtest.policies.sizing import account_order_ceiling
from backtest.replay import SessionContext, SessionDecision
from backtest.sip import MIN_ORDER_VALUE_INR, simulate_sip_instalment
from dataplatform.query.pit import Dataset
from execution.broker import Exchange, Holding, OrderRequest, Position, Side

__all__ = [
    "D13_ABS_MOM",
    "D13_DAILY_SLOT_FILL",
    "D13_INDUSTRY_GATE",
    "D13_PER_STOCK_CLOCK",
    "D13_PER_STOCK_DAILY_GATE",
    "D13_PER_STOCK_DAILY_PARK",
    "D13_PER_STOCK_NO_REGIME",
    "D13_PROFIT_FILTER",
    "D13_RESID_MOM",
    "PAPER_RATIFIED_2026_09_06",
    "PER_STOCK_REVIEW_SESSIONS",
    "PE_GATE_MEAN_DAYS",
    "MomentumV2Data",
    "MomentumV2Parameters",
    "MomentumV2Policy",
    "MomentumV2Record",
    "RegimeReading",
    "ValuationGate",
    "ValuationReading",
    "basket_volatility_annual",
    "d13_valuation_gate",
    "inverse_vol_weights",
    "regime_parked",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_WEIGHT_QUANTUM = Decimal("0.00000001")

#: The evidence label a regime-parking decision files its reading under (:func:`regime_parked`).
_REGIME_LABEL: Final = "regime_index_vs_ma"


@dataclass(frozen=True, slots=True)
class MomentumV2Record:
    """One candidate this session: two momentum signals, its price, its vol, and when it was known.

    Carries *both* momentum definitions so the ``use_12_1`` toggle selects between them without a
    second data read:

    * ``momentum_0_12`` — the trailing ``0..12m`` total return (the naive signal, a plain ratio:
      ``0.25`` is +25 %);
    * ``momentum_12_1`` — the ``t-12m .. t-1m`` total return, skipping the most recent month.

    ``price`` is the current raw close the whole-share allocation and valuation use; ``volatility``
    is the trailing return volatility used only when ``vol_scaled`` is on (a positive dispersion, in
    the same ratio units); ``knowable_date`` is the date all four became knowable — the session's
    own close for an EOD close. The point-in-time guard checks ``knowable_date`` against the
    session's
    ``as_of`` (invariant #7).

    Never holds a ``float`` (all figures are ``Decimal``), a non-positive price (unpriceable names
    cannot be sized into whole shares), or a non-positive volatility (it would divide by zero under
    inverse-vol weighting).
    """

    isin: str
    momentum_0_12: Decimal
    momentum_12_1: Decimal
    price: Decimal
    volatility: Decimal
    knowable_date: date

    def __post_init__(self) -> None:
        for name in ("momentum_0_12", "momentum_12_1", "price", "volatility"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(
                    f"{name} must be a Decimal — money/signal is never float (CLAUDE.md)"
                )
        if self.price <= _ZERO:
            raise ValueError(f"price must be positive, got {self.price}")
        if self.volatility <= _ZERO:
            raise ValueError(f"volatility must be positive, got {self.volatility}")

    def momentum(self, *, use_12_1: bool) -> Decimal:
        """The ranking signal under the chosen definition: 12-1 if ``use_12_1`` else 0-12."""
        return self.momentum_12_1 if use_12_1 else self.momentum_0_12


class ValuationGate(StrEnum):
    """How the P/E reading acts on the momentum book (pre-registration, PE timing, §3).

    * ``ENTRY`` — the price regime parks and re-enters exactly as D13; a rebalance that reads *rich*
      places no buy at all (no new name, no top-up, no next-session redeploy). Its band sells
      still happen, and the proceeds wait in cash.
    * ``REPLACE`` — the P/E reading parks (rich) and re-enters (cheap) instead of the price regime.
    * ``BOTH`` — the basket is held only while the price regime is on *and* the P/E reads cheap;
      either turning parks it.
    """

    ENTRY = "entry"
    REPLACE = "replace"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class ValuationReading:
    """The market P/E as of a session against its own trailing simple mean.

    ``pe`` is the published index P/E for the session; ``trailing_mean`` the simple mean of the last
    ``mean_days`` published P/Es knowable by the session, the session's own included.
    ``spliced`` says the pre-2021-03-31 points in that window were rescaled across NSE's switch
    from standalone to consolidated earnings (``backtest.valuation_gate``). ``knowable_date`` is the
    session the figures were published for — knowable that evening, as an index close is.

    **Cheap** is strictly below the mean: equal reads rich.
    """

    pe: Decimal
    trailing_mean: Decimal
    mean_days: int
    knowable_date: date
    spliced: bool = False

    def __post_init__(self) -> None:
        for name in ("pe", "trailing_mean"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be a Decimal — never float (CLAUDE.md)")
            if getattr(self, name) <= _ZERO:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")
        if self.mean_days <= 0:
            raise ValueError(f"mean_days must be positive, got {self.mean_days}")

    @property
    def cheap(self) -> bool:
        """Whether the P/E is strictly below its trailing mean."""
        return self.pe < self.trailing_mean

    def detail(self) -> dict[str, str]:
        """The reading as evidence/journal detail strings."""
        return {
            "pe": str(self.pe),
            "pe_trailing_mean": str(self.trailing_mean),
            "pe_mean_days": str(self.mean_days),
            "pe_cheap": "true" if self.cheap else "false",
            "pe_spliced": "true" if self.spliced else "false",
        }


@dataclass(frozen=True, slots=True)
class RegimeReading:
    """The market regime as of a session: the index level and its own moving average.

    ``index_level`` is the benchmark index's close on the session; ``moving_average`` is its
    trailing ``N``-day simple moving average (N stated in the parameters). ``risk_on`` is the trend
    filter: hold the basket only while the level sits at or above its average. ``knowable_date`` is
    the session close both were computed on — the guard refuses a reading dated past the session.

    The moving average is computed over closes on or before the session, so the reading is
    point-in-time by construction (invariant #7): no future close enters it.
    """

    index_level: Decimal
    moving_average: Decimal
    knowable_date: date
    #: The market P/E against its own trailing mean (the valuation gate), when a gate reads it.
    valuation: ValuationReading | None = None

    def __post_init__(self) -> None:
        for name in ("index_level", "moving_average"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be a Decimal — never float (CLAUDE.md)")
            if getattr(self, name) <= _ZERO:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")

    @property
    def knowable_through(self) -> date:
        """The latest date anything in the reading became knowable — what the PIT guard checks.

        The price reading's own date, or the valuation reading's if that is later, so a P/E dated
        past the session trips the guard even when the price reading would not.
        """
        if self.valuation is None:
            return self.knowable_date
        return max(self.knowable_date, self.valuation.knowable_date)

    @property
    def risk_on(self) -> bool:
        """Whether the index is at or above its moving average — the basket is held only if so."""
        return self.index_level >= self.moving_average

    def clears_above(self, band: Decimal) -> bool:
        """Whether the level is at or above ``MA * (1 + band)`` — the daily re-entry trigger."""
        return self.index_level >= self.moving_average * (_ONE + band)

    def breaks_below(self, band: Decimal) -> bool:
        """Whether the level is below ``MA * (1 - band)`` — the daily exit trigger."""
        return self.index_level < self.moving_average * (_ONE - band)


@dataclass(frozen=True, slots=True, repr=False)
class MomentumV2Parameters:
    """The v2 knobs — the naive top-N plus four independently-toggleable, a-priori improvements.

    * ``top_n`` — names held (and the *buy* band), equal- or vol-weighted. 20, the naive default.
    * ``use_12_1`` — rank on the 12-1 return (skip the most recent month) instead of 0-12.
    * ``sell_band`` — the outer hysteresis band: a holding is sold only once it leaves the top
      ``sell_band``. Must be ``>= top_n``; ``None`` disables banding (sell the moment a name leaves
      the top ``top_n`` — the naive rule). E.g. ``top_n=20, sell_band=30`` buys the top-20 but holds
      anything still inside the top-30.
    * ``regime_filter`` — hold the basket only while the index is above its ``regime_ma_days``-day
      moving average; below it, sell the basket and park in the liquid (CASH) sleeve.
    * ``vol_scaled`` — size names at ``~ 1/vol`` instead of equal weight.
    * ``redeploy_next_session`` — on the session after a rebalance, deploy the cash the rebalance's
      sells released into the basket chosen at that rebalance (same weights, that day's prices).
    * ``vol_target_annual`` — scale the basket to this annualised volatility (``None`` = fully
      invested, the pre-overlay behaviour); ``assumed_correlation`` is the one stated average
      pairwise correlation the estimate uses (0.3, the long-run large-cap figure, chosen once).
    * ``regime_ma_days`` — the moving-average window for the regime filter (200, the standard).
    * ``buy_budget_fraction`` — the share of free cash a rebalance deploys (a mechanical execution
      margin, not a return knob — carried over from the naive policy).
    * ``sleeve`` / ``parking_sleeve`` — journal sleeves for basket trades and for regime parking.
    * ``regime_daily_reentry`` / ``regime_daily_exit`` / ``regime_daily_band`` — M14.5: read the
      regime on every non-rebalance session too, re-entering an all-cash book on risk-on and/or
      parking an invested one on risk-off, with an optional symmetric band around the average.
      Require ``regime_filter``. Off by default and left out of ``repr`` while at their defaults,
      so the run fingerprint of every pre-M14.5 configuration is unchanged.
    * ``industry_gate`` — M16.2: each rebalance, admit only names whose industry maps to one of the
      top-5 NSE sectoral indices by 6-1 month return (:mod:`backtest.policies.industry_gate`). Its
      K, look-back and cadence are fixed there, not here. Off by default, out of ``repr`` while off.
    * ``absolute_momentum`` / ``residual_ranking`` / ``profitability_filter`` — M16.1 (A1, A3, A4;
      :mod:`backtest.policies.momentum_v2_overlays`). Off by default and out of ``repr`` while off,
      for the same reason. Each needs its data source to serve the matching overlay seam.
    * ``daily_slot_fill`` / ``review_sessions`` / ``daily_regime_gate`` / ``daily_regime_park`` —
      M14.6's per-stock entry variants (see the module docstring). ``review_sessions`` needs
      ``daily_slot_fill`` (a book whose holdings leave one by one must refill one by one), and the
      daily regime switches need ``review_sessions`` and ``regime_filter``. Defined on D13's own
      toggles only: none combines with the M14.5 daily regime checks, the vol target or an M16
      overlay. Off by default and out of ``repr`` while off.

    With ``use_12_1``, ``sell_band``, ``regime_filter``, ``vol_scaled``, ``redeploy_next_session``
    and ``vol_target_annual`` all off/``None`` the policy is the naive top-N policy exactly. Every
    field is fixed by construction and reported
    verbatim, so the "no tuning" claim stays checkable.
    """

    top_n: int = 20
    use_12_1: bool = False
    sell_band: int | None = None
    regime_filter: bool = False
    vol_scaled: bool = False
    redeploy_next_session: bool = False
    vol_target_annual: Decimal | None = None
    assumed_correlation: Decimal = Decimal("0.3")
    regime_ma_days: int = 200
    buy_budget_fraction: Decimal = Decimal("0.98")
    sleeve: Sleeve = Sleeve.TACTICAL
    parking_sleeve: Sleeve = Sleeve.CASH
    regime_daily_reentry: bool = field(default=False, repr=False)
    regime_daily_exit: bool = field(default=False, repr=False)
    regime_daily_band: Decimal = field(default=Decimal("0"), repr=False)
    industry_gate: bool = field(default=False, repr=False)
    absolute_momentum: bool = field(default=False, repr=False)
    residual_ranking: bool = field(default=False, repr=False)
    profitability_filter: bool = field(default=False, repr=False)
    daily_slot_fill: bool = field(default=False, repr=False)
    review_sessions: int | None = field(default=None, repr=False)
    daily_regime_gate: bool = field(default=False, repr=False)
    daily_regime_park: bool = field(default=False, repr=False)
    valuation_gate: ValuationGate | None = field(default=None, repr=False)
    valuation_mean_days: int | None = field(default=None, repr=False)
    valuation_splice: bool = field(default=False, repr=False)
    valuation_series: str | None = field(default=None, repr=False)
    regime_monthly_band: Decimal = field(default=Decimal("0"), repr=False)
    exposure_fraction: Decimal | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.top_n <= 0:
            raise ValueError(f"top_n must be positive, got {self.top_n}")
        if self.sell_band is not None and self.sell_band < self.top_n:
            raise ValueError(
                f"sell_band must be >= top_n (the outer band cannot be tighter than the buy band): "
                f"got sell_band={self.sell_band}, top_n={self.top_n}"
            )
        if self.regime_ma_days <= 0:
            raise ValueError(f"regime_ma_days must be positive, got {self.regime_ma_days}")
        if self.vol_target_annual is not None:
            if not isinstance(self.vol_target_annual, Decimal):
                raise TypeError("vol_target_annual must be a Decimal")
            if self.vol_target_annual <= _ZERO:
                raise ValueError(
                    f"vol_target_annual must be positive, got {self.vol_target_annual}"
                )
        if not isinstance(self.assumed_correlation, Decimal):
            raise TypeError("assumed_correlation must be a Decimal")
        if not (_ZERO <= self.assumed_correlation <= _ONE):
            raise ValueError(
                f"assumed_correlation must be in [0, 1], got {self.assumed_correlation}"
            )
        if not isinstance(self.buy_budget_fraction, Decimal):
            raise TypeError("buy_budget_fraction must be a Decimal")
        if not (_ZERO < self.buy_budget_fraction <= _ONE):
            raise ValueError(
                f"buy_budget_fraction must be in (0, 1], got {self.buy_budget_fraction}"
            )
        if not isinstance(self.regime_daily_band, Decimal):
            raise TypeError("regime_daily_band must be a Decimal")
        if not (_ZERO <= self.regime_daily_band < _ONE):
            raise ValueError(f"regime_daily_band must be in [0, 1), got {self.regime_daily_band}")
        daily = self.regime_daily_reentry or self.regime_daily_exit
        if daily and not self.regime_filter:
            raise ValueError("the daily regime checks extend regime_filter; turn it on")
        if self.regime_daily_band != _ZERO and not daily:
            raise ValueError("regime_daily_band applies only to a daily regime check")
        self._check_per_stock()
        self._check_valuation()
        self._check_drawdown_comparators()

    def _check_valuation(self) -> None:
        """The valuation gate's own dependencies, and what it never combines with."""
        if self.valuation_gate is None:
            if self.valuation_mean_days is not None or self.valuation_splice:
                raise ValueError("valuation_mean_days and valuation_splice need a valuation_gate")
            return
        if not isinstance(self.valuation_gate, ValuationGate):
            raise TypeError("valuation_gate must be a ValuationGate")
        if not self.regime_filter:
            raise ValueError("the valuation gate is read on the regime seam; turn on regime_filter")
        if self.valuation_mean_days is None or self.valuation_mean_days <= 0:
            raise ValueError(
                f"valuation_mean_days must be positive, got {self.valuation_mean_days}"
            )
        clashing = [
            name
            for name in ("regime_daily_reentry", "regime_daily_exit", "daily_slot_fill")
            if getattr(self, name)
        ]
        if clashing:
            raise ValueError(
                "the valuation gate is read on D13's monthly rebalance only; "
                f"turn off {', '.join(clashing)}"
            )

    def _check_drawdown_comparators(self) -> None:
        """The PE-drawdown study's switches: their dependencies, and what they never join."""
        if self.valuation_series is not None:
            if self.valuation_gate is None:
                raise ValueError("valuation_series needs a valuation_gate")
            if not isinstance(self.valuation_series, str) or not self.valuation_series:
                raise TypeError("valuation_series must be a non-empty series id")
        if not isinstance(self.regime_monthly_band, Decimal):
            raise TypeError("regime_monthly_band must be a Decimal")
        if not (_ZERO <= self.regime_monthly_band < _ONE):
            raise ValueError(
                f"regime_monthly_band must be in [0, 1), got {self.regime_monthly_band}"
            )
        if self.regime_monthly_band != _ZERO:
            if not self.regime_filter:
                raise ValueError("regime_monthly_band tightens regime_filter; turn it on")
            if self.regime_daily or self.valuation_gate is not None:
                raise ValueError(
                    "regime_monthly_band is a monthly price-only rule; turn off the daily regime "
                    "checks and the valuation gate"
                )
        if self.exposure_fraction is None:
            return
        if not isinstance(self.exposure_fraction, Decimal):
            raise TypeError("exposure_fraction must be a Decimal")
        if not (_ZERO < self.exposure_fraction < _ONE):
            raise ValueError(f"exposure_fraction must be in (0, 1), got {self.exposure_fraction}")
        clashing = [
            name
            for name in (
                "absolute_momentum",
                "daily_slot_fill",
                "regime_daily_reentry",
                "regime_daily_exit",
            )
            if getattr(self, name)
        ]
        if self.vol_target_annual is not None:
            clashing.append("vol_target_annual")
        if self.review_sessions is not None:
            clashing.append("review_sessions")
        if self.valuation_gate is not None:
            clashing.append("valuation_gate")
        if clashing:
            raise ValueError(
                "exposure_fraction is a constant cap on D13's own book; turn off "
                + ", ".join(clashing)
            )

    def _check_per_stock(self) -> None:
        """M14.6: the per-stock switches' own dependencies, and what they never combine with."""
        if self.review_sessions is not None:
            if self.review_sessions <= 0:
                raise ValueError(f"review_sessions must be positive, got {self.review_sessions}")
            if not self.daily_slot_fill:
                raise ValueError("review_sessions frees slots one by one; turn on daily_slot_fill")
        if self.daily_regime_gate and (self.review_sessions is None or not self.regime_filter):
            raise ValueError("daily_regime_gate extends a per-stock book's regime_filter")
        if self.daily_regime_park and not self.daily_regime_gate:
            raise ValueError("daily_regime_park extends daily_regime_gate; turn it on")
        if self.daily_slot_fill:
            clashing = [
                name
                for name in (
                    "regime_daily_reentry",
                    "regime_daily_exit",
                    "industry_gate",
                    "absolute_momentum",
                    "residual_ranking",
                    "profitability_filter",
                )
                if getattr(self, name)
            ]
            if self.vol_target_annual is not None:
                clashing.append("vol_target_annual")
            if clashing:
                raise ValueError(
                    "the per-stock switches are defined on D13's own toggles only; "
                    f"turn off {', '.join(clashing)}"
                )

    @property
    def regime_daily(self) -> bool:
        """Whether the regime is read on non-rebalance sessions too (either daily switch on)."""
        return self.regime_daily_reentry or self.regime_daily_exit

    def __repr__(self) -> str:
        """The dataclass rendering, with each ``repr=False`` (M14.5) field shown only off default.

        A run's identity is the ``repr`` of its parameters (``backtest.run_ledger``): a field added
        to the dataclass would otherwise change the fingerprint of every configuration ever
        persisted — D13's included — though none of them changed behaviour. Rendered this way, a
        default-off configuration renders exactly as it did before the field existed, and a
        switched-on one never shares a fingerprint with it.
        """
        shown = ", ".join(
            f"{f.name}={getattr(self, f.name)!r}"
            for f in fields(self)
            if f.repr or getattr(self, f.name) != f.default
        )
        return f"{type(self).__qualname__}({shown})"


#: The momentum-sleeve configuration the owner ratified for **paper mode** on 2026-09-06 — the
#: four M9.5 toggles plus next-session redeployment of sale proceeds — on the evidence in
#: `ops/gates/M9-momentum-v2-report.md` (ten-year increment table) and
#: `ops/gates/M10-fundamentals-signal-report.md` (per-regime comparison). Recorded in
#: `HUMAN_DECISIONS.md` (D13). A fixture/paper ratification is never valid for real money (B9); the
#: volatility target stays off pending a read of its cost (BACKLOG).
PAPER_RATIFIED_2026_09_06: Final = MomentumV2Parameters(
    top_n=20,
    use_12_1=True,
    sell_band=30,
    regime_filter=True,
    vol_scaled=True,
    redeploy_next_session=True,
)

#: M16.2 arm A2: D13 with the point-in-time industry-momentum gate on, and nothing else changed.
#: Research configuration only — paper trading runs D13.
D13_INDUSTRY_GATE: Final = MomentumV2Parameters(
    top_n=20,
    use_12_1=True,
    sell_band=30,
    regime_filter=True,
    vol_scaled=True,
    redeploy_next_session=True,
    industry_gate=True,
)

#: M16.1's three variants, each D13 with exactly one overlay switched on, named so an arm set can
#: reference them (:mod:`backtest.policies.momentum_v2_overlays`). Research configurations only:
#: none is ratified for paper or real money.
#: A1 — a basket slot whose 12-1 return does not beat the repo-based cash hurdle stays in cash.
D13_ABS_MOM: Final = replace(PAPER_RATIFIED_2026_09_06, absolute_momentum=True)
#: A3 — rank on the round-2 H1 residual-momentum score instead of the 12-1 return.
D13_RESID_MOM: Final = replace(PAPER_RATIFIED_2026_09_06, residual_ranking=True)
#: A4 — only names with positive TTM profit in a filing at most 200 days old are eligible.
D13_PROFIT_FILTER: Final = replace(PAPER_RATIFIED_2026_09_06, profitability_filter=True)

#: M14.6's per-stock entry variants on D13 — research configurations only; paper trading runs D13.
#: The review clock is 21 sessions, D13's monthly cadence expressed in sessions (one trading month).
PER_STOCK_REVIEW_SESSIONS: Final = 21
#: V1 — exits exactly as D13 (monthly, sell band 30); free slots refilled every session.
D13_DAILY_SLOT_FILL: Final = replace(PAPER_RATIFIED_2026_09_06, daily_slot_fill=True)
#: V2 — a 21-session review clock per holding, free slots refilled daily; regime read as D13 does.
D13_PER_STOCK_CLOCK: Final = replace(D13_DAILY_SLOT_FILL, review_sessions=PER_STOCK_REVIEW_SESSIONS)
#: V3 — V2 with the regime read every session; risk-off blocks entries and sells nothing extra.
D13_PER_STOCK_DAILY_GATE: Final = replace(D13_PER_STOCK_CLOCK, daily_regime_gate=True)
#: V3b — V3 where a risk-off session also parks the whole book.
D13_PER_STOCK_DAILY_PARK: Final = replace(D13_PER_STOCK_DAILY_GATE, daily_regime_park=True)
#: V4 — V2 with no regime filter at all.
D13_PER_STOCK_NO_REGIME: Final = replace(D13_PER_STOCK_CLOCK, regime_filter=False)

#: PE timing (``ops/studies/preregistration-pe-timing-2026-10-19.md``): the trailing-mean lengths
#: fixed before any run — about a month, a quarter, and the 200 sessions D13's price regime uses.
PE_GATE_MEAN_DAYS: Final = (21, 63, 200)


def d13_valuation_gate(
    gate: ValuationGate, mean_days: int, *, splice: bool = False, series: str | None = None
) -> MomentumV2Parameters:
    """D13 with the valuation gate on — a research configuration only; paper runs D13.

    ``series`` names another published series for the gate (``None``: the NIFTY 500 P/E).
    """
    return replace(
        PAPER_RATIFIED_2026_09_06,
        valuation_gate=gate,
        valuation_mean_days=mean_days,
        valuation_splice=splice,
        valuation_series=series,
    )


@runtime_checkable
class MomentumV2Data(Protocol):
    """Where the v2 policy reads its world — the injected seam the point-in-time context wraps.

    * ``is_rebalance(session)`` — is today a rebalance session (first trading session of the month)?
    * ``signal(as_of)`` — the candidate set as a guardable :class:`Dataset` of
      :class:`MomentumV2Record`, already narrowed to the investable PIT universe and to names with
      both momentum definitions, a price and a volatility. With ``redeploy_next_session`` on, the
      policy also reads it on the session *after* each rebalance, for that day's prices of the
      basket already chosen — a source must serve those sessions too (the L1 one does).
    * ``regime(as_of)`` — the :class:`RegimeReading` for the session, as a one-element guardable
      dataset. Only read when ``regime_filter`` is on; a source that does not model regime need not
      implement anything more than a stub when the filter is off. With a daily regime check on it
      is read on *every* session, and ``signal`` on any session a daily re-entry fires.

    A test supplies an in-memory implementation; the ten-year run supplies one backed by L1 through
    the query layer. The policy never reaches past this surface.
    """

    def is_rebalance(self, session: date) -> bool:
        """Whether ``session`` is a rebalance session."""

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        """The PIT candidate set as of ``as_of``, as a guardable dataset."""

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        """The regime reading as of ``as_of``, as a one-element guardable dataset."""


class MomentumV2Policy:
    """Top-N momentum v2 — 12-1 ranking, turnover banding, regime filter and vol-scaled weights.

    Construct it with a :class:`MomentumV2Data` source and :class:`MomentumV2Parameters`. It
    satisfies :class:`backtest.replay.Policy`, so the replay engine drives it session by session; it
    reads data only through ``ctx.pit`` (invariant #7), account state only through ``ctx.broker``
    (invariant #5) and time only through ``ctx.clock`` (B10). It holds no cost model and touches no
    store.

    Determinism: every branch is a pure function of the session's admitted candidates, the admitted
    regime reading and the broker's reported book, with ties broken by ISIN, so a replay reproduces
    the decision exactly.
    """

    __slots__ = (
        "_clocks",
        "_cooldown",
        "_data",
        "_entries_placed",
        "_entry_open",
        "_order_caps",
        "_params",
        "_parked",
        "_pending",
        "_session_count",
    )

    def __init__(
        self,
        data: MomentumV2Data,
        params: MomentumV2Parameters | None = None,
        *,
        order_caps: RiskRails | None = None,
    ) -> None:
        self._data = data
        self._params = params if params is not None else MomentumV2Parameters()
        # M16.1: an overlay switched on against a source with no method for it is refused here,
        # not on the first rebalance a month into a run. The check is runtime_checkable's, so it
        # sees only that the method exists: the L1 source always has all three and raises
        # BacktestError on the first call when its input was not supplied.
        for switch, seam in (
            ("absolute_momentum", AbsoluteMomentumData),
            ("residual_ranking", ResidualRankingData),
            ("profitability_filter", ProfitabilityData),
        ):
            if getattr(self._params, switch) and not isinstance(data, seam):
                raise TypeError(
                    f"{switch} is on but the data source does not serve {seam.__name__}"
                )
        # The rails A8 will clear this policy's orders against. With them, no buy is sized past the
        # per-order ceiling (``backtest.policies.sizing.account_order_ceiling``): a buy the rail is
        # bound to refuse leaves its cash idle, the next rebalance spreads the larger idle balance
        # over the same names, and the book drifts to cash. Sized to the ceiling, the rest stays in
        # cash and tops the name up at the next rebalance. Not a field of the parameters, so the
        # parameters' repr is unchanged; the run spec records the sizing instead.
        self._order_caps = order_caps
        #: The basket weights decided at the last rebalance, awaiting the proceeds of its sells
        #: (``redeploy_next_session``). ``None`` when nothing is pending. Deterministic state: it is
        #: a pure function of the previous session's decision, so a replay reproduces it.
        self._pending: dict[str, Decimal] | None = None
        #: Whether the last regime decision parked the basket (M14.5). Set by every park, cleared
        #: by every risk-on rebalance; read only by the daily regime checks, so a configuration
        #: without them behaves exactly as before it existed.
        self._parked = False
        #: M14.6 state, read only by the per-stock switches. ``_session_count`` numbers the
        #: sessions this policy has decided; ``_clocks`` maps each held name to the session number
        #: of its entry or last review; ``_entries_placed`` are the names an entry buy was placed
        #: for last session, and ``_cooldown`` the session number until which a name whose entry
        #: did not fill (a rail refused it, or the market had no bar) is passed over, so one
        #: unbuyable name cannot hold a free slot empty session after session.
        self._session_count = 0
        self._clocks: dict[str, int] = {}
        self._entries_placed: set[str] = set()
        self._cooldown: dict[str, int] = {}
        #: Entrants whose entry is still completing: an entry sized from the cash free on its
        #: session (sale proceeds settle a session or two later, and A8 caps one order) is topped
        #: up toward its share on later sessions until the name's first review — under V1, until
        #: the next rebalance, which re-sizes the whole basket anyway.
        self._entry_open: set[str] = set()

    @property
    def pending(self) -> Mapping[str, Decimal] | None:
        """The basket a rebalance chose and the next session will redeploy into, or ``None``.

        The policy's only state between sessions. A replay holds it in memory for the whole walk; a
        forward runner that lives one session per process (the daily paper job, M13.1) reads it
        after a session and hands it back through :meth:`resume` before the next one.
        """
        return None if self._pending is None else dict(self._pending)

    @property
    def parked(self) -> bool:
        """Whether the basket is parked by the regime filter — the daily re-entry's trigger state.

        Only the daily regime checks (M14.5) read it. A forward runner that adopts one must carry
        it across sessions with :meth:`resume`, as it carries :attr:`pending`.
        """
        return self._parked

    def resume(self, pending: Mapping[str, Decimal] | None, *, parked: bool = False) -> None:
        """Restore the state :attr:`pending` reported at the end of the previous decided session.

        What it assumes: ``pending`` is exactly what this policy reported after the last session it
        decided for this book, so resuming and then deciding is the same decision an uninterrupted
        replay would make; likewise ``parked`` (read only under a daily regime check). What it
        never does: invent a target — ``None`` clears it, or resume a per-stock (M14.6) book,
        whose clocks are research state no forward runner carries — that raises.
        """
        if self._params.daily_slot_fill:
            raise NotImplementedError(
                "the M14.6 per-stock switches are research-only: resume carries no review clocks"
            )
        self._pending = None if pending is None else dict(pending)
        self._parked = parked

    def decide(self, ctx: SessionContext) -> SessionDecision:
        """Decide this session: a heartbeat off a rebalance, a full rebalance on one.

        With ``redeploy_next_session`` on, the session right after a rebalance is a *deployment*
        session: the cash that rebalance's sells released is put into the basket it chose.
        With ``review_sessions`` (M14.6) there is no calendar rebalance: every session is a
        per-stock session (:meth:`_per_stock`). With ``daily_slot_fill`` alone, a session that is
        neither a rebalance nor a redeploy fills the book's free slots (:meth:`_slot_fill`).
        """
        self._session_count += 1
        if self._params.review_sessions is not None:
            return self._per_stock(ctx)
        if self._data.is_rebalance(ctx.session):
            self._entry_open = set()
            return self._rebalance(ctx)
        if self._params.regime_daily:
            daily = self._daily_regime(ctx)
            if daily is not None:
                return daily
        if self._pending is not None:
            pending, self._pending = self._pending, None
            return self._redeploy(ctx, pending)
        if self._params.daily_slot_fill:
            return self._slot_fill(ctx)
        return self._heartbeat(ctx)

    def _daily_regime(self, ctx: SessionContext) -> SessionDecision | None:
        """M14.5: the regime read off a rebalance session — a park, a re-entry, or ``None``.

        An invested book (not :attr:`parked`, settled shares held) reading below the band is parked
        exactly as a risk-off rebalance parks it; shares still in settlement cannot be delivered
        and stay until the next park attempt — the next risk-off rebalance. A parked book reading
        above the band re-enters through a full :meth:`_rebalance` on this session, which re-reads
        the regime through the same PIT guard. A parked book is never re-parked daily: the names
        A8's minimum-holdings floor kept are not offered to it again every session.
        """
        (reading,) = ctx.pit.admit(self._data.regime(ctx.session))
        band = self._params.regime_daily_band
        if self._parked:
            if self._params.regime_daily_reentry and reading.clears_above(band):
                return self._rebalance(ctx)
            return None
        if self._params.regime_daily_exit and ctx.broker.holdings() and reading.breaks_below(band):
            self._pending = None  # nothing is redeployed into a basket being parked
            return self._park(ctx, reading)
        return None

    # ── M14.6: per-stock entry and review ────────────────────────────────────────────────────────

    def _slot_fill(self, ctx: SessionContext) -> SessionDecision:
        """V1: a non-rebalance session fills the book's free slots, if the regime gate is open.

        The gate is D13's own reading: open unless the last rebalance parked the book. Nothing is
        sold here — exits stay D13's monthly band.
        """
        names = self._names_held(ctx)
        self._note_unfilled_entries(names)
        if self._params.regime_filter and self._parked:
            return self._per_stock_evidence(ctx, held=len(names), due=0, gate="parked")
        ranked = self._ranked(ctx)
        slots = self._params.top_n - len(names)
        buys = self._fill(ctx, ranked, names=names, selling=set(), slots=slots, topups=[])
        return self._per_stock_decision(ctx, [], buys, held=len(names), due=0, gate="open")

    def _per_stock(self, ctx: SessionContext) -> SessionDecision:
        """V2-V4: review the holdings that are due, refill free slots, under the regime gate.

        The order of a session: the regime (a park, when the reading calls for one, is the whole
        decision); then every settled holding whose clock has run ``review_sessions`` sessions is
        reviewed against today's ranking — sold outside ``sell_band``, else kept with its clock
        restarted (topped up toward its share when within ``top_n``); then the slots the book has
        free once today's sells are counted are filled from the top of the ranking. Sale proceeds
        settle later, so a slot freed today is filled from cash already free, or on a later session.
        """
        params = self._params
        now = self._session_count
        holdings = {holding.isin: holding for holding in ctx.broker.holdings()}
        names = self._names_held(ctx)
        self._note_unfilled_entries(names)
        # A name that left the book (sold, merged away) drops its clock; one that arrived without an
        # entry here (a reissued ISIN) starts its clock on the session it is first seen.
        self._clocks = {isin: start for isin, start in self._clocks.items() if isin in names}
        for isin in sorted(names - set(self._clocks)):
            self._clocks[isin] = now
        self._entry_open &= names

        gate = "open"
        if params.regime_filter:
            if params.daily_regime_gate:
                (reading,) = ctx.pit.admit(self._data.regime(ctx.session))
                if not reading.risk_on:
                    gate = "risk-off"
                    if params.daily_regime_park:
                        # Park on the first risk-off session, and re-issue on each month's first
                        # session while risk-off persists (as D13's monthly rule does), so names
                        # A8's floor kept are offered again monthly rather than daily.
                        if not self._parked or self._data.is_rebalance(ctx.session):
                            return self._park(ctx, reading)
                        return self._per_stock_evidence(ctx, held=len(names), due=0, gate="parked")
                else:
                    self._parked = False
            else:
                if self._data.is_rebalance(ctx.session):
                    (reading,) = ctx.pit.admit(self._data.regime(ctx.session))
                    if not reading.risk_on:
                        return self._park(ctx, reading)
                    self._parked = False
                if self._parked:
                    return self._per_stock_evidence(ctx, held=len(names), due=0, gate="parked")

        ranked = self._ranked(ctx)
        rank = {record.isin: position for position, record in enumerate(ranked, start=1)}
        band = params.sell_band if params.sell_band is not None else params.top_n
        assert params.review_sessions is not None
        due = sorted(
            isin for isin in holdings if now - self._clocks[isin] >= params.review_sessions
        )
        sells: list[tuple[OrderRequest, str]] = []
        topups: list[str] = []
        for isin in due:
            # The review happened whatever it decides: a sale A8 refuses is retried at the next
            # review, never every session. An entry still completing is complete once reviewed.
            self._clocks[isin] = now
            self._entry_open.discard(isin)
            position = rank.get(isin)
            if position is None or position > band:
                quantity = holdings[isin].quantity
                where = "unranked today" if position is None else f"rank {position}"
                sells.append(
                    (
                        OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="MOMENTUM"),
                        f"per-stock review: {where}, outside the top-{band} band; "
                        f"liquidating {quantity} shares",
                    )
                )
            elif position <= params.top_n and gate == "open":
                topups.append(isin)
        buys: list[tuple[OrderRequest, str]] = []
        if gate == "open":
            selling = {order.isin for order, _ in sells}
            slots = params.top_n - len(names - selling)
            buys = self._fill(ctx, ranked, names=names, selling=selling, slots=slots, topups=topups)
        return self._per_stock_decision(ctx, sells, buys, held=len(names), due=len(due), gate=gate)

    def _names_held(self, ctx: SessionContext) -> set[str]:
        """Every name the book owns: settled holdings and filled buys still in settlement."""
        return {h.isin for h in ctx.broker.holdings()} | {p.isin for p in ctx.broker.positions()}

    def _note_unfilled_entries(self, names: set[str]) -> None:
        """Cool down, for one review period, each entry placed last session that never filled."""
        wait = self._params.review_sessions or PER_STOCK_REVIEW_SESSIONS
        for isin in sorted(self._entries_placed - names):
            self._cooldown[isin] = self._session_count + wait
        self._entries_placed = set()
        self._cooldown = {
            i: until for i, until in self._cooldown.items() if until > self._session_count
        }

    def _ranked(self, ctx: SessionContext) -> list[MomentumV2Record]:
        """Today's candidates, ranked as a rebalance ranks them: momentum descending, then ISIN."""
        use_12_1 = self._params.use_12_1
        candidates = ctx.pit.admit(self._data.signal(ctx.session))
        return sorted(
            candidates, key=lambda record: (-record.momentum(use_12_1=use_12_1), record.isin)
        )

    def _fill(
        self,
        ctx: SessionContext,
        ranked: Sequence[MomentumV2Record],
        *,
        names: set[str],
        selling: set[str],
        slots: int,
        topups: Sequence[str],
    ) -> list[tuple[OrderRequest, str]]:
        """Entry buys for ``slots`` free slots plus top-ups of ``topups``, sized as D13 sizes.

        Entrants are the best-ranked names within ``top_n`` that the book does not own (and that
        are not cooling down after an entry that did not fill); earlier entrants whose entry is
        still completing (``_entry_open``) are topped up alongside them. The book they complete
        — what is held and not being sold today, plus the entrants — is weighted as a rebalance
        weights its basket (inverse volatility when ``vol_scaled``), each name's target is its
        weight of the book's value (cash, settling proceeds and every lot at today's price), and
        each buy is the gap to that target. Free cash times ``buy_budget_fraction`` caps the
        total, shared pro rata; A8's per-order ceiling caps each order. Never sells, never buys a
        name ranked outside ``top_n``.
        """
        params = self._params
        by_isin = {record.isin: record for record in ranked}
        entrants = (
            [
                record.isin
                for record in ranked[: params.top_n]
                if record.isin not in names and record.isin not in self._cooldown
            ][: max(0, slots)]
            if slots > 0
            else []
        )
        completing = sorted((self._entry_open & names) - selling - set(topups))
        wanted = [*entrants, *(isin for isin in (*topups, *completing) if isin in by_isin)]
        if not wanted:
            return []
        book = sorted(((names - selling) | set(entrants)) & set(by_isin))
        weights = self._weights_for({isin: by_isin[isin] for isin in book})
        price_of = {record.isin: record.price for record in ranked}
        lots: list[Holding | Position] = [*ctx.broker.holdings(), *ctx.broker.positions()]
        existing: dict[str, Decimal] = {}
        value = ctx.broker.margins().cash_value
        for lot in lots:
            marked = Decimal(lot.quantity) * price_of.get(lot.isin, lot.average_price)
            existing[lot.isin] = existing.get(lot.isin, _ZERO) + marked
            value += marked
        gaps = {
            isin: weights[isin] * value - existing.get(isin, _ZERO)
            for isin in wanted
            if isin in weights
        }
        gaps = {isin: gap for isin, gap in gaps.items() if gap > _ZERO}
        total = sum(gaps.values(), _ZERO)
        budget = min(ctx.broker.margins().available * params.buy_budget_fraction, total)
        if budget <= _ZERO:
            return []
        shares = {isin: (gap / total).quantize(_WEIGHT_QUANTUM) for isin, gap in gaps.items()}
        last = sorted(shares)[-1]
        shares[last] = _ONE - sum((w for isin, w in shares.items() if isin != last), _ZERO)
        if shares[last] <= _ZERO:  # quantisation left the last name nothing; drop it
            del shares[last]
            if not shares:
                return []
            last = sorted(shares)[-1]
            shares[last] = _ONE - sum((w for isin, w in shares.items() if isin != last), _ZERO)
        allocation = simulate_sip_instalment(
            instalment=budget,
            targets=shares,
            prices={isin: by_isin[isin].price for isin in shares},
            min_order_value=MIN_ORDER_VALUE_INR,
            order_ceiling=account_order_ceiling(self._order_caps, ctx.broker, price_of),
        )
        rank = {record.isin: position for position, record in enumerate(ranked, start=1)}
        entering = set(entrants)
        buys: list[tuple[OrderRequest, str]] = []
        for order in allocation.orders:
            kind = (
                "entry into a free slot"
                if order.isin in entering
                else ("entry completing" if order.isin in completing else "review top-up")
            )
            buys.append(
                (
                    order.to_order_request(exchange=Exchange.NSE, tag="MOMENTUM"),
                    f"per-stock {kind}: rank {rank[order.isin]}, 12-1 momentum "
                    f"{by_isin[order.isin].momentum(use_12_1=params.use_12_1):+}; "
                    f"buy {order.quantity} @ {order.price}",
                )
            )
            if order.isin in entering:
                self._entries_placed.add(order.isin)
                self._entry_open.add(order.isin)
                self._clocks[order.isin] = self._session_count
        return buys

    def _per_stock_decision(
        self,
        ctx: SessionContext,
        sells: Sequence[tuple[OrderRequest, str]],
        buys: Sequence[tuple[OrderRequest, str]],
        *,
        held: int,
        due: int,
        gate: str,
    ) -> SessionDecision:
        orders = tuple(order for order, _ in (*sells, *buys))
        entries = tuple(self._entry(ctx, order, note) for order, note in (*sells, *buys))
        evidence = self._per_stock_evidence(ctx, held=held, due=due, gate=gate).evidence
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _per_stock_evidence(
        self, ctx: SessionContext, *, held: int, due: int, gate: str
    ) -> SessionDecision:
        """The per-stock session's book state as evidence; a heartbeat when nothing is placed."""
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="per_stock_session",
                    as_of=ctx.session,
                    value=Decimal(held),
                    detail={"reviews_due": str(due), "regime_gate": gate},
                    text="M14.6 per-stock session: names held, reviews due, regime gate",
                ),
            ),
        )
        return SessionDecision(evidence=evidence)

    # ── branches ──────────────────────────────────────────────────────────────────────────────────

    def _heartbeat(self, ctx: SessionContext) -> SessionDecision:
        """A no-order session: return the heartbeat evidence the engine stamps (inv. #9)."""
        holdings = ctx.broker.holdings()
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="held_names",
                    value=Decimal(len(holdings)),
                    text="no rebalance due this session",
                ),
            ),
        )
        return SessionDecision(evidence=evidence)

    def _rebalance(self, ctx: SessionContext) -> SessionDecision:
        """Rank the admitted candidates, apply the toggled overlays, and stage the orders."""
        # Regime filter: read the regime through the PIT guard *first*. When it is risk-off, the
        # basket is not held at all — sell everything and park in the liquid sleeve (invariant #7:
        # the reading is admitted, so a future-dated regime trips the guard rather than leaking).
        self._pending = None
        reading: RegimeReading | None = None
        if self._params.regime_filter:
            (reading,) = ctx.pit.admit(self._data.regime(ctx.session))
            if not self._invested(reading):
                return self._park(ctx, reading)
        self._parked = False
        # ENTRY valuation gate: the price regime kept the basket, but a rich P/E blocks every buy.
        entries_blocked = (
            reading is not None
            and self._params.valuation_gate is ValuationGate.ENTRY
            and not self._valuation(reading).cheap
        )

        candidates = ctx.pit.admit(self._data.signal(ctx.session))
        gate = None
        if self._params.industry_gate:  # M16.2: narrow to the top sectoral indices' names
            candidates, gate = apply_industry_gate(self._data, ctx, candidates)
        use_12_1 = self._params.use_12_1
        overlay_evidence: list[EvidenceItem] = []
        if self._params.profitability_filter:
            # A4: the eligible set narrows before anything is ranked (filings through the guard).
            assert isinstance(self._data, ProfitabilityData)
            offered = len(candidates)
            readings = ctx.pit.admit(self._data.profitability(ctx.session))
            candidates = profitable(candidates, readings, as_of=ctx.session)
            overlay_evidence.append(profitability_evidence(ctx.session, offered, len(candidates)))
        scores: Mapping[str, Decimal] | None = None
        if self._params.residual_ranking:
            # A3: the H1 score is the ranking key; a name the panel excludes cannot be ranked.
            assert isinstance(self._data, ResidualRankingData)
            scores = residual_scores(ctx.pit.admit(self._data.residual_momentum(ctx.session)))
            ranking = scores
            ranked = sorted(
                (record for record in candidates if record.isin in ranking),
                key=lambda record: (-ranking[record.isin], record.isin),
            )
        else:
            ranked = sorted(
                candidates, key=lambda record: (-record.momentum(use_12_1=use_12_1), record.isin)
            )
        chosen = ranked[: self._params.top_n]
        # The outer band that governs sells: the top ``sell_band`` names (banding), or just the
        # top ``top_n`` (no banding — the naive rule). A held name inside this band is retained.
        band = self._params.sell_band if self._params.sell_band is not None else self._params.top_n
        keep = {record.isin for record in ranked[:band]}
        target = {record.isin: record for record in chosen}
        slots: dict[str, Decimal] | None = None
        if self._params.absolute_momentum:
            # A1: weights are struck over the whole chosen basket, then a name that does not beat
            # the cash hurdle gives up its slot to cash — never to the next-ranked name. It is not
            # kept either: a held name failing the hurdle is sold even inside the sell band.
            assert isinstance(self._data, AbsoluteMomentumData)
            (repo,) = ctx.pit.admit(self._data.repo_rate(ctx.session))
            hurdle = repo.hurdle
            slots = self._weights_for(target) if target else {}
            passing = {r.isin for r in ranked[:band] if r.momentum_12_1 > hurdle}
            keep &= passing
            failing = [isin for isin in target if isin not in passing]
            # Held names the band alone would keep but the hurdle sells — the sells A1 asks for.
            in_band = {r.isin for r in ranked[:band]}
            selling = [
                h.isin for h in ctx.broker.holdings() if h.isin in in_band and h.isin not in passing
            ]
            overlay_evidence.append(
                absolute_momentum_evidence(ctx.session, repo, chosen, failing, selling)
            )
            target = {isin: record for isin, record in target.items() if isin in passing}
            slots = {isin: weight for isin, weight in slots.items() if isin in target}
        prices = {record.isin: record.price for record in target.values()}

        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        sells = self._sells(held, keep)
        # A1's cash slots cap the basket at the passing names' share of capital.
        exposure = _ONE if slots is None else sum(slots.values(), _ZERO)
        trims: list[tuple[OrderRequest, str]] = []
        if self._params.vol_target_annual is not None and target:
            vol_exposure = self._exposure(target)
            exposure = vol_exposure if slots is None else (exposure * vol_exposure)
            trims = self._trims(held, target, prices, exposure, ctx, ranked)
        if self._params.exposure_fraction is not None and target:
            exposure = self._params.exposure_fraction
            trims = self._trims(held, target, prices, exposure, ctx, ranked)
        buys, drifts_note = self._buys(
            ctx,
            held,
            target,
            prices,
            use_12_1=use_12_1,
            exposure=exposure,
            ranked=ranked,
            scores=scores,
        )
        if trims:
            buys = []  # a book being cut back to its exposure does not also add to it
        if entries_blocked:
            buys = []  # valuation rich: no new name, no top-up, and nothing redeployed tomorrow
        if reading is not None and self._params.valuation_gate is not None:
            overlay_evidence.append(
                self._valuation_evidence(ctx.session, self._valuation(reading), entries_blocked)
            )
        if self._params.redeploy_next_session and target and sells and not entries_blocked:
            # Only a rebalance that sold something leaves proceeds to deploy tomorrow. Under A1
            # the slot weights go (summing below one), so the redeploy also leaves the cash slots.
            self._pending = self._weights_for(target) if slots is None else slots
            if self._params.exposure_fraction is not None:
                # The redeploy fills the basket up to the cap's share of capital, never past it.
                fraction = self._params.exposure_fraction
                self._pending = {isin: w * fraction for isin, w in self._pending.items()}

        orders = tuple(order for order, _ in (*sells, *trims, *buys))
        entries = tuple(self._entry(ctx, order, note) for order, note in (*sells, *trims, *buys))
        evidence = self._evidence(
            ctx.session,
            chosen,
            drifts_note,
            use_12_1=use_12_1,
            exposure=exposure,
            scores=scores,
            extra=overlay_evidence,
        )
        if gate is not None:
            evidence = gate.annotate(evidence)
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _park(self, ctx: SessionContext, reading: RegimeReading) -> SessionDecision:
        """Regime risk-off: sell the whole basket and hold cash (the liquid sleeve).

        With nothing held the decision is a single HOLD of the parking sleeve carrying the reading,
        so the journal row itself says why the book is in cash. It places no order either way.
        """
        self._parked = True
        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            quantity = held[isin].quantity
            sells.append(
                (
                    OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="MOMENTUM"),
                    f"{self._park_reason(reading)}; parking {quantity} shares to liquid sleeve",
                )
            )
        orders = tuple(order for order, _ in sells)
        entries = tuple(self._park_entry(ctx, order, note) for order, note in sells)
        if not entries:
            # Nothing held, nothing to sell: the decision is still "risk-off, stay in cash", and
            # the journal row says so itself rather than falling through to the engine's bare
            # HEARTBEAT (invariant #9). A HOLD of the parking sleeve places no order.
            entries = (self._regime_hold(ctx, reading),)
        detail = {
            "moving_average": str(reading.moving_average),
            "ma_days": str(self._params.regime_ma_days),
            "risk_on": "false",
        }
        text = "index below its moving average — basket parked to the liquid sleeve"
        if self._params.valuation_gate is not None:
            detail = {
                **detail,
                "price_risk_on": "true" if reading.risk_on else "false",
                "valuation_gate": self._params.valuation_gate.value,
                **self._valuation(reading).detail(),
            }
            text = f"{self._park_reason(reading)} — basket parked to the liquid sleeve"
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.PRICE,
                    source="benchmark",
                    label=_REGIME_LABEL,
                    as_of=ctx.session,
                    value=reading.index_level,
                    detail=detail,
                    text=text,
                ),
            ),
        )
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    # ── the valuation gate (PE timing) ───────────────────────────────────────────────────────────

    def _valuation(self, reading: RegimeReading) -> ValuationReading:
        """The P/E reading a valuation gate needs — a source that did not serve one fails loud."""
        if reading.valuation is None:
            raise ValueError(
                f"the valuation gate is on but the regime reading for {reading.knowable_date} "
                "carries no P/E reading; the source must serve one"
            )
        return reading.valuation

    def _price_on(self, reading: RegimeReading) -> bool:
        """The monthly price regime: at or above the MA, or ``MA * (1 + band)`` when banded."""
        band = self._params.regime_monthly_band
        return reading.risk_on if band == _ZERO else reading.clears_above(band)

    def _invested(self, reading: RegimeReading) -> bool:
        """Whether the rebalance holds the basket: D13's price regime, or the valuation gate's."""
        gate = self._params.valuation_gate
        if gate is None or gate is ValuationGate.ENTRY:
            return self._price_on(reading)
        cheap = self._valuation(reading).cheap
        if gate is ValuationGate.REPLACE:
            return cheap
        return self._price_on(reading) and cheap

    def _park_reason(self, reading: RegimeReading) -> str:
        """Why a rebalance parks — D13's own words when no valuation gate parks it."""
        price = (
            f"index {reading.index_level} < {self._params.regime_ma_days}d MA "
            f"{reading.moving_average}"
        )
        if self._params.regime_monthly_band != _ZERO:
            price += f" x (1 + {self._params.regime_monthly_band})"
        gate = self._params.valuation_gate
        if gate is None or gate is ValuationGate.ENTRY:
            return f"regime risk-off ({price})"
        valuation = self._valuation(reading)
        pe = (
            f"P/E {valuation.pe} >= {valuation.mean_days}-session mean "
            f"{valuation.trailing_mean.quantize(Decimal('0.0001'))}"
        )
        reasons = []
        if gate is ValuationGate.BOTH and not reading.risk_on:
            reasons.append(price)
        if not valuation.cheap:
            reasons.append(pe)
        return f"valuation gate ({gate.value}) off ({'; '.join(reasons)})"

    def _valuation_evidence(
        self, session: date, valuation: ValuationReading, entries_blocked: bool
    ) -> EvidenceItem:
        """The P/E reading a rebalance considered, and whether it blocked the buys (inv. #9)."""
        assert self._params.valuation_gate is not None
        return EvidenceItem(
            kind=EvidenceKind.PRICE,
            source="valuation",
            label="valuation_gate",
            as_of=session,
            value=valuation.pe,
            detail={
                "valuation_gate": self._params.valuation_gate.value,
                "entries_blocked": "true" if entries_blocked else "false",
                **valuation.detail(),
            },
            text=(
                "market P/E at or above its trailing mean — no buys this rebalance"
                if entries_blocked
                else "market P/E read against its trailing mean"
            ),
        )

    # ── sells ─────────────────────────────────────────────────────────────────────────────────────

    def _sells(self, held: Mapping[str, Holding], keep: set[str]) -> list[tuple[OrderRequest, str]]:
        """One full-quantity SELL for each settled holding outside the keep band, in ISIN order."""
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            if isin in keep:
                continue
            quantity = held[isin].quantity
            band = (
                self._params.sell_band if self._params.sell_band is not None else self._params.top_n
            )
            sells.append(
                (
                    OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="MOMENTUM"),
                    f"left the top-{band} momentum band; liquidating {quantity} shares",
                )
            )
        return sells

    # ── buys ──────────────────────────────────────────────────────────────────────────────────────

    def _buys(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        target: Mapping[str, MomentumV2Record],
        prices: Mapping[str, Decimal],
        *,
        use_12_1: bool,
        exposure: Decimal = _ONE,
        ranked: Sequence[MomentumV2Record] = (),
        scores: Mapping[str, Decimal] | None = None,
    ) -> tuple[list[tuple[OrderRequest, str]], Decimal]:
        """Whole-share buys toward the model weights, sized from free cash; return them and drift.

        Weights are equal over the top ``top_n`` (naive) or inverse-volatility (``vol_scaled``).
        Budget is the cash *currently free* times ``buy_budget_fraction`` — never sale proceeds
        staged this session, which have not settled — and the allocation accounts for existing
        holdings in the surviving names so it tops up toward the model rather than double-buying.
        Under a volatility target (``exposure < 1``) the budget is further capped so the basket's
        value does not exceed ``exposure`` of the book's capital. With ``order_caps`` no buy is
        sized past A8's per-order ceiling, the book valued at the ranked candidates' prices.
        """
        if not target:
            return [], _ZERO
        budget = ctx.broker.margins().available * self._params.buy_budget_fraction
        if exposure < _ONE:
            capital = self._capital(ctx, held, prices, ranked)
            in_basket = sum(
                (Decimal(held[isin].quantity) * prices[isin] for isin in target if isin in held),
                _ZERO,
            )
            headroom = capital * exposure - in_basket
            budget = min(budget, max(_ZERO, headroom))
            if budget <= _ZERO:
                return [], _ZERO
        weights = self._weights_for(target)
        existing_value = {
            isin: Decimal(held[isin].quantity) * prices[isin] for isin in target if isin in held
        }
        allocation = simulate_sip_instalment(
            instalment=budget,
            targets=weights,
            prices=prices,
            existing_value=existing_value,
            min_order_value=MIN_ORDER_VALUE_INR,
            order_ceiling=account_order_ceiling(
                self._order_caps, ctx.broker, {**{r.isin: r.price for r in ranked}, **prices}
            ),
        )
        if scores is not None:  # A3: the rationale names the key the name was ranked on
            buys = [
                (
                    order.to_order_request(exchange=Exchange.NSE, tag="MOMENTUM"),
                    f"top-{self._params.top_n} residual momentum {scores[order.isin]:+} (H1); "
                    f"buy {order.quantity} @ {order.price}",
                )
                for order in allocation.orders
            ]
            return buys, allocation.tracking_drift
        buys = [
            (
                order.to_order_request(exchange=Exchange.NSE, tag="MOMENTUM"),
                f"top-{self._params.top_n} momentum "
                f"{target[order.isin].momentum(use_12_1=use_12_1):+} "
                f"({'12-1' if use_12_1 else '0-12'}); buy {order.quantity} @ {order.price}",
            )
            for order in allocation.orders
        ]
        return buys, allocation.tracking_drift

    def _exposure(self, target: Mapping[str, MomentumV2Record]) -> Decimal:
        """``min(1, vol_target / basket_vol)`` for the basket at its model weights."""
        assert self._params.vol_target_annual is not None
        weights = self._weights_for(target)
        estimate = basket_volatility_annual(
            {isin: target[isin].volatility for isin in target},
            weights,
            correlation=self._params.assumed_correlation,
        )
        if estimate <= _ZERO:
            return _ONE
        return min(_ONE, (self._params.vol_target_annual / estimate).quantize(_WEIGHT_QUANTUM))

    def _capital(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        prices: Mapping[str, Decimal],
        ranked: Sequence[MomentumV2Record],
    ) -> Decimal:
        """Cash (settled or in settlement) plus every holding marked at its candidate price.

        Unpriced holdings are carried at average cost. Proceeds still in settlement count: they are
        the account's, and leaving them out would shrink the book for a session or two after every
        sale — under T+2 a rebalance's own sells would cut its target weights.
        """
        price_of = {record.isin: record.price for record in ranked}
        price_of.update(prices)
        marked = sum(
            (
                Decimal(holding.quantity) * price_of.get(isin, holding.average_price)
                for isin, holding in held.items()
            ),
            _ZERO,
        )
        return ctx.broker.margins().cash_value + marked

    def _trims(
        self,
        held: Mapping[str, Holding],
        target: Mapping[str, MomentumV2Record],
        prices: Mapping[str, Decimal],
        exposure: Decimal,
        ctx: SessionContext,
        ranked: Sequence[MomentumV2Record],
    ) -> list[tuple[OrderRequest, str]]:
        """Pro-rata partial sells that bring the held basket down to ``exposure`` of capital."""
        in_basket = {
            isin: Decimal(held[isin].quantity) * prices[isin] for isin in target if isin in held
        }
        current = sum(in_basket.values(), _ZERO)
        if current <= _ZERO:
            return []
        allowed = self._capital(ctx, held, prices, ranked) * exposure
        if current <= allowed:
            return []
        cut = (current - allowed) / current
        trims: list[tuple[OrderRequest, str]] = []
        for isin in sorted(in_basket):
            # Round the trim *up*: a risk overlay that leaves the book a share over its cap has not
            # enforced the cap. Whole shares, never more than is held.
            quantity = min(
                held[isin].quantity,
                int((Decimal(held[isin].quantity) * cut).to_integral_value(ROUND_CEILING)),
            )
            if quantity <= 0:
                continue
            trims.append(
                (
                    OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="MOMENTUM"),
                    (
                        f"vol target {self._params.vol_target_annual}: basket exposure cut to "
                        if self._params.vol_target_annual is not None
                        else "exposure cap: basket exposure cut to "
                    )
                    + f"{exposure} of capital; trimming {quantity} shares",
                )
            )
        return trims

    def _weights_for(self, target: Mapping[str, MomentumV2Record]) -> dict[str, Decimal]:
        """The basket's model weights: inverse-vol when ``vol_scaled``, else equal."""
        if self._params.vol_scaled:
            return inverse_vol_weights({isin: target[isin].volatility for isin in target})
        return _equal_weights(sorted(target))

    # ── redeploy: the session after a rebalance ───────────────────────────────────────────────────

    def _redeploy(self, ctx: SessionContext, weights: Mapping[str, Decimal]) -> SessionDecision:
        """Put the cash yesterday's sells released into yesterday's basket at today's prices.

        Reads today's admitted candidate set only for the *prices* of the names already chosen;
        the ranking is not revisited. A basket name with no price today is left out of this pass
        (its weight is renormalised away), never guessed. If nothing is affordable the session is
        journalled as a heartbeat that says so.
        """
        records = {record.isin: record for record in ctx.pit.admit(self._data.signal(ctx.session))}
        priced = {isin: w for isin, w in weights.items() if isin in records}
        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        budget = ctx.broker.margins().available * self._params.buy_budget_fraction
        if not priced or budget <= _ZERO:
            return self._redeploy_heartbeat(ctx, budget, reason="no priced basket name or no cash")
        total = sum(priced.values(), _ZERO)
        weights_norm = {isin: (w / total).quantize(_WEIGHT_QUANTUM) for isin, w in priced.items()}
        last = sorted(weights_norm)[-1]
        weights_norm[last] = _ONE - sum(
            (w for isin, w in weights_norm.items() if isin != last), _ZERO
        )
        prices = {isin: records[isin].price for isin in priced}
        existing_value = {
            isin: Decimal(held[isin].quantity) * prices[isin] for isin in priced if isin in held
        }
        share = sum(weights.values(), _ZERO)
        if share < _ONE:
            # A1 (M16.1): slot weights summing below one mean the hurdle left slots in cash. The
            # redeploy fills the passing names up to their share of capital and never into those.
            in_basket = sum(
                (
                    Decimal(held[isin].quantity)
                    * (records[isin].price if isin in records else held[isin].average_price)
                    for isin in weights
                    if isin in held
                ),
                _ZERO,
            )
            capital = self._capital(ctx, held, prices, tuple(records.values()))
            budget = min(budget, max(_ZERO, capital * share - in_basket))
            if budget <= _ZERO:
                return self._redeploy_heartbeat(ctx, budget, reason="basket at its cash cap")
        allocation = simulate_sip_instalment(
            instalment=budget,
            targets=weights_norm,
            prices=prices,
            existing_value=existing_value,
            min_order_value=MIN_ORDER_VALUE_INR,
            order_ceiling=account_order_ceiling(
                self._order_caps,
                ctx.broker,
                {isin: record.price for isin, record in records.items()},
            ),
        )
        if not allocation.orders:
            return self._redeploy_heartbeat(ctx, budget, reason="no affordable share reduces drift")
        buys = [
            (
                order.to_order_request(exchange=Exchange.NSE, tag="MOMENTUM"),
                f"redeploy: proceeds of yesterday's rebalance sells into the chosen basket; "
                f"buy {order.quantity} @ {order.price}",
            )
            for order in allocation.orders
        ]
        orders = tuple(order for order, _ in buys)
        entries = tuple(self._entry(ctx, order, note) for order, note in buys)
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="redeploy_budget",
                    as_of=ctx.session,
                    value=budget,
                    detail={
                        "names_bought": str(len(buys)),
                        "tracking_drift": str(allocation.tracking_drift),
                    },
                    text="cash freed by yesterday's rebalance sells, deployed into its basket",
                ),
            ),
        )
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _redeploy_heartbeat(
        self, ctx: SessionContext, budget: Decimal, *, reason: str
    ) -> SessionDecision:
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="redeploy_budget",
                    as_of=ctx.session,
                    value=budget,
                    text=f"redeploy session, nothing bought: {reason}",
                ),
            ),
        )
        return SessionDecision(evidence=evidence)

    # ── journal + evidence ──────────────────────────────────────────────────────────────────────

    def _entry(self, ctx: SessionContext, order: OrderRequest, rationale: str) -> JournalEntry:
        """A BUY/SELL journal entry for one basket order — rationale, sleeve and ISIN (§0/§5.7)."""
        decision = Decision.BUY if order.side is Side.BUY else Decision.SELL
        return JournalEntry(
            ts=ctx.clock.now(),
            trading_date=ctx.session,
            actor=Actor.T0,
            decision=decision,
            isin=order.isin,
            sleeve=self._params.sleeve,
            rationale=rationale,
        )

    def _park_entry(self, ctx: SessionContext, order: OrderRequest, rationale: str) -> JournalEntry:
        """A SELL entry for a regime-parking exit — tagged with the parking (CASH) sleeve."""
        return JournalEntry(
            ts=ctx.clock.now(),
            trading_date=ctx.session,
            actor=Actor.T0,
            decision=Decision.SELL,
            isin=order.isin,
            sleeve=self._params.parking_sleeve,
            rationale=rationale,
        )

    def _regime_hold(self, ctx: SessionContext, reading: RegimeReading) -> JournalEntry:
        """A HOLD of the parking sleeve for a risk-off rebalance that had nothing to sell."""
        return JournalEntry(
            ts=ctx.clock.now(),
            trading_date=ctx.session,
            actor=Actor.T0,
            decision=Decision.HOLD,
            sleeve=self._params.parking_sleeve,
            rationale=(
                "regime risk-off; holding cash"
                if self._params.valuation_gate is None
                else f"{self._park_reason(reading)}; holding cash"
            ),
            payload={
                "regime": "risk_off",
                "index_level": str(reading.index_level),
                "moving_average": str(reading.moving_average),
                "ma_days": str(self._params.regime_ma_days),
                "risk_on": "false",
                **(
                    {}
                    if self._params.valuation_gate is None
                    else {
                        "valuation_gate": self._params.valuation_gate.value,
                        **self._valuation(reading).detail(),
                    }
                ),
            },
        )

    def _evidence(
        self,
        session: date,
        chosen: list[MomentumV2Record],
        tracking_drift: Decimal,
        *,
        use_12_1: bool,
        exposure: Decimal = _ONE,
        scores: Mapping[str, Decimal] | None = None,
        extra: Sequence[EvidenceItem] = (),
    ) -> EvidenceBundle:
        """The ranked momentum table the decision was made on, as one content-addressed bundle.

        Under A3 (``scores``) the table carries the residual-momentum key it was ranked on; any
        M16.1 overlay's own item (``extra``) follows the table.
        """
        label = "momentum_12_1" if use_12_1 else "momentum_0_12"
        if scores is not None:
            label = "residual_momentum"
        items = [
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="L2",
                label=label,
                isin=record.isin,
                as_of=session,
                value=(
                    record.momentum(use_12_1=use_12_1) if scores is None else scores[record.isin]
                ),
                detail={
                    "price": str(record.price),
                    "volatility": str(record.volatility),
                    "rank": str(rank),
                },
            )
            for rank, record in enumerate(chosen, start=1)
        ]
        weighting = "inverse-vol" if self._params.vol_scaled else "equal-weight"
        items.append(
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="book",
                label="tracking_drift",
                as_of=session,
                value=tracking_drift,
                text=f"{weighting} top-{self._params.top_n} rebalance",
            )
        )
        if self._params.vol_target_annual is not None:
            items.append(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="vol_target_exposure",
                    as_of=session,
                    value=exposure,
                    detail={
                        "vol_target_annual": str(self._params.vol_target_annual),
                        "assumed_correlation": str(self._params.assumed_correlation),
                    },
                    text="share of capital the basket may occupy under the volatility target",
                )
            )
        if self._params.exposure_fraction is not None:
            items.append(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="exposure_cap",
                    as_of=session,
                    value=exposure,
                    text="constant share of capital the basket may occupy (equal-exposure D13)",
                )
            )
        items.extend(extra)
        return EvidenceBundle(trading_date=session, actor=Actor.T0, items=tuple(items))


# ── weighting ───────────────────────────────────────────────────────────────────────────────────


def _equal_weights(isins: list[str]) -> dict[str, Decimal]:
    """Equal weights over ``isins`` that sum to exactly 1 — the remainder lands on the last name.

    ``Decimal(1)/Decimal(n)`` does not generally have an exact ``n``-fold sum, so the last ISIN
    absorbs the rounding residue; the result sums to exactly 1 and every weight is positive, which
    the allocator's weight check (M4.7) requires. Deterministic in the (sorted) ISIN order.
    """
    if not isins:
        raise ValueError("cannot weight an empty basket")
    n = len(isins)
    each = (_ONE / Decimal(n)).quantize(_WEIGHT_QUANTUM)
    weights = dict.fromkeys(isins[:-1], each)
    weights[isins[-1]] = _ONE - each * (n - 1)
    return weights


_MONTHS_PER_YEAR_SQRT = Decimal(12).sqrt()


def basket_volatility_annual(
    volatilities: Mapping[str, Decimal],
    weights: Mapping[str, Decimal],
    *,
    correlation: Decimal,
) -> Decimal:
    """Annualised basket volatility from monthly name volatilities and one assumed correlation.

    ``sqrt((1 - rho) * sum(w_i^2 s_i^2) + rho * (sum(w_i s_i))^2)``, then times ``sqrt(12)``. With
    ``rho = 1`` this is the weighted average volatility (names move together); with ``rho = 0`` the
    pure diversification case. Every input is a Decimal; the result is exact to Decimal precision.
    """
    if not volatilities or not weights:
        raise ValueError("cannot estimate the volatility of an empty basket")
    weighted_sq = _ZERO
    weighted = _ZERO
    for isin, weight in weights.items():
        vol = volatilities[isin]
        if not isinstance(vol, Decimal) or not isinstance(weight, Decimal):
            raise TypeError("volatilities and weights must be Decimal")
        if vol <= _ZERO:
            raise ValueError(f"volatility for {isin} must be positive, got {vol}")
        weighted_sq += weight * weight * vol * vol
        weighted += weight * vol
    variance = (_ONE - correlation) * weighted_sq + correlation * weighted * weighted
    return variance.sqrt() * _MONTHS_PER_YEAR_SQRT


def inverse_vol_weights(volatilities: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Weights proportional to ``1 / vol``, summing to exactly 1 — the risk-parity sizing (M9.5).

    A less volatile name earns a larger weight, so no single jumpy name dominates the book's risk.
    The raw ``1/vol`` figures are normalised by their sum; the last ISIN (in sorted order) absorbs
    the quantisation residue so the result sums to exactly 1 and every weight stays positive, which
    the allocator's weight check requires. Deterministic in the sorted ISIN order.

    Assumes every volatility is a positive ``Decimal``; a non-positive vol is a bug (it would give a
    zero or negative weight, or divide by zero), and is refused loudly rather than silently dropped.
    """
    if not volatilities:
        raise ValueError("cannot weight an empty basket")
    isins = sorted(volatilities)
    inverses: dict[str, Decimal] = {}
    for isin in isins:
        vol = volatilities[isin]
        if not isinstance(vol, Decimal):
            raise TypeError(f"volatility for {isin} must be a Decimal, got {vol!r}")
        if vol <= _ZERO:
            raise ValueError(f"volatility for {isin} must be positive, got {vol}")
        inverses[isin] = _ONE / vol
    total = sum(inverses.values(), _ZERO)
    weights: dict[str, Decimal] = {}
    running = _ZERO
    for isin in isins[:-1]:
        weight = (inverses[isin] / total).quantize(_WEIGHT_QUANTUM)
        weights[isin] = weight
        running += weight
    # The last name absorbs the residue so the weights sum to exactly 1.
    weights[isins[-1]] = _ONE - running
    return weights


def regime_parked(evidence: EvidenceBundle) -> bool:
    """Whether ``evidence`` is a rebalance the regime filter parked in cash (risk-off).

    Reads the bundle the policy decided on, so a caller that only holds the evidence — the paper
    session's record — can say why the book is in cash without re-reading the regime. Never infers
    risk-off from the absence of orders: a risk-on rebalance can also place none.
    """
    return any(
        item.label == _REGIME_LABEL and item.detail.get("risk_on") == "false"
        for item in evidence.items
    )
