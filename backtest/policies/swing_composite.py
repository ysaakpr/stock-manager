"""A composite-signal swing policy: 7-90 day holds, with entry and exit as separate rules (§7, X2).

Where momentum v2 (:mod:`backtest.policies.momentum_v2`) rotates a book monthly on one price signal,
this policy is built for the *swing* horizon — a name is entered, held for weeks rather than a year,
and exited on a rule that is written down rather than implied by the next rebalance. Three things
differ, and each is a measured finding rather than a preference:

* **The entry score is a composite of three near-orthogonal signals**, not a single trailing return.
  Measured over the whole 2016-2026 lake (4.27M bars, ~900-name liquid cross-section, weekly
  samples), the top decile of each signal beats the equal-weight universe over 63 sessions by:
  52-week-high proximity 1.50 %, 5-day delivery share 2.59 %, 12-1 momentum 2.02 % — and the
  equal-weighted composite of the three by **3.35 % (t = 13.3)**, half again the best single part.
  The three carry different information: where the price sits in its own year, how much of the
  recent tape was *delivery* rather than intraday churn, and the classical trend.

* **Delivery share is the strongest single signal in the store and nothing else reads it.** L1 has
  carried ``deliv_pct`` since 2016; its rank-IC t-statistic (10.7 at 21 sessions, 14.8 at 90) is the
  highest of any signal tested, price signals included. It is the one genuinely India-specific
  edge available here: a rising delivery share is buying that intends to hold the stock overnight.

* **Exit is three explicit rules, not one.** A rank band (hysteresis) carries the position while it
  is merely drifting; a re-underwrite at ``max_hold`` forces a still-held name to re-earn its place;
  a *wide* trailing stop caps the tail. Tight stops were measured and rejected — see below.

**The holding period is a cost decision, and the data settles it.** The composite's excess accrues
at a near-constant ~0.2 %/week out to 125 sessions: the marginal 5-day slice earns about as much at
day 120 as at day 20, so there is no burst to capture early. Alpha is therefore linear in time held
while friction is paid per *trade* — 0.223 % statutory (``execution/costs/rates.yaml``) plus roughly
0.22 % of modelled slippage, about 0.45 % the round trip. Against a top-decile excess of 0.35 % over
5 sessions, **a 7-day rotation is underwater before it starts**; over 21 sessions the same entries
earn 1.29 % and over 63 sessions 3.35 %. Identical entries exited purely on time net ~20 %/yr at a
10-session hold against ~28 %/yr at 63. The defaults below therefore sit at the *long* end of the
7-90 day band, and ``min_hold_sessions`` exists to stop a name being churned inside a fortnight.

**What raises trade frequency without paying for it twice** is the rebalance *interval*, not the
holding period. Deciding every ``rebalance_interval_sessions`` (10 — a fortnight) instead of monthly
roughly doubles the decision points, while the sell band means most decisions change nothing: a name
is bought into the top ``top_n`` but only sold once it leaves the wider top ``sell_band``. More
opportunities to act, the same average holding period, and turnover set by the band rather than the
calendar.

**Stops, measured on 33,106 real forward paths.** Capital freed early held idle to the horizon (no
free redeployment credit), per-trade net return: no stop 6.33 %, 20 % stop 6.32 %, 12 % stop 6.03 %,
8 % stop 5.50 %, 25 % trailing 6.43 %, 10 % trailing 4.91 %, and a 15 % profit target with a 10 %
stop **2.17 %**. Two conclusions are wired in as defaults: a tight stop destroys the edge (an 8 %
stop turns a +3.4 % median into -5.5 % and a 57 % win rate into 44 %, because a momentum name's
ordinary path passes through an 8 % drawdown), and a profit target is the single worst rule tested,
since truncating the winners is exactly what removes momentum's payoff. The default is a **25 %
trailing stop** — return-neutral (6.43 % vs 6.33 %) while cutting the 5th-percentile outcome from
-25.3 % to -24.0 %, and materially more at tighter settings for those who want it. It is checked
every session against that session's close, so it is a real stop and not a month-end approximation.

**M12.1 widened the engine without moving a default.** The entry score was always a weighted sum of
rank-normalised legs; only three legs existed. Five more now do — the 5-session return, the
21-session return, delivery *acceleration* (its 5-session mean over its 63-session mean), turnover
expansion and proximity to a 50-session mean — plus volatility as a scored leg rather than only the
screen it already was. Every weight is **signed**, so a leg can be asked for its reverse: a negative
``weight_return_5`` is the short-term reversal family, which nothing in this repo had measured, and
a positive one is short-term trend. Every new weight defaults to zero, so the policy above this
paragraph is bit-for-bit the one M10.7 measured, and a leg the lake cannot compute for a name takes
its neutral value (0 for a return, 1 for a ratio) rather than dropping the name — the candidate set
must not move with the weight vector, or an arm-to-arm comparison becomes a universe comparison.

M12.1 also added the ``regime_filter`` gate momentum v2 has carried since M9.5 and this policy did
not: no new buys while the published NIFTY 50 sits below its own 200-session mean. It suppresses
*buys only*. Every exit is staged before the gate is consulted, because risk-off must never trap a
position the band, the re-underwrite or the stop has already decided to sell — the book runs down
through its own exits rather than being liquidated on the gate. A session with no regime reading at
all is treated as risk-**off**: an absent regime is not a licence to buy.

X2 closed two ways the composite could rank on noise. A delivery-derived leg contributes only on a
session where at least :data:`DELIVERY_COVERAGE_THRESHOLD` (80 %) of the candidates carry a real
delivery print; below it the leg is dropped and the composite is struck over the remaining legs.
Before 2016-09-02 the lake has no delivery at all, every name's stand-in was the same 0.0, and the
ISIN tie-break chose the basket. And tied values now share their mean rank instead of being ranked
in ISIN order. A session on which *no* weighted leg survives the gate is not ranked at all — no
band or re-underwrite sell, no buy — because a zero-information composite ordered by ISIN is not a
decision.

Point-in-time (invariant #7): every figure on a record — the 252-session high, the delivery mean,
the 12-1 return, the volatility, every M12.1 leg — is struck over sessions on or before the record's
``knowable_date``, and every read goes through ``ctx.pit.admit``, the regime reading included. The
trailing stop reads only the current session's close.

**The stop is split-invariant.** A trailing stop on raw closes (invariant #3) sees a 2:1 split as a
50 % crash: the peak was struck on the pre-split scale, the close is on the post-split one. The fix
rescales the stop's reference — the running peak, and nothing else — on the ex-date, from the one
place that fact is knowable on the decision date without a corporate-action read: the policy's own
account. ``backtest.book_actions`` applies a split or bonus to the broker before the session's
decision, exactly as the depository credits a live account; the policy sees the holding's share
count change while its total cost basis does not. Nothing the policy trades can do that — a fill
moves count *and* basis, a settlement moves a lot from pending to settled without changing either —
so a count change at an unchanged basis is a split or bonus, and ``old / new`` shares is its
inverse ratio (floored counts make a bonus's ratio approximate to one share in the holding). An ISIN
reissue (the retired holding carried to its survivor at the same basis) keeps the position's age
and peak rather than starting a fresh one. The ``corporate_actions`` store, whose ``knowable_date``
is the ingest day, is never read: the reference moves on the day the account changes and never
before, which is what a live holder sees. The raw-price *signal* legs (the 52-week-high proximity,
the returns) are not rescaled here — they are struck by the data source, not the policy.

What it never does: read a wall clock (time is ``ctx.clock``), key on a symbol (ISIN only —
invariant #2), hold a cost model (the injected broker owns the one shared model — invariants #4/#5),
or reach data outside the point-in-time context. It carries per-position state (entry session and
running peak) across sessions, seeded only from what it has itself observed, so a replay from the
same start reproduces every decision exactly.
"""

from __future__ import annotations

from collections import ChainMap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from analyst.cases import RiskRails
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from backtest.band_hits import BAND_HIT_BLOCK_RATIONALE, BandHitData, band_hit_blocked
from backtest.cap_tiers import CapTier, CapTierData, TierSleeve
from backtest.policies.momentum_v2 import RegimeReading
from backtest.policies.sizing import account_order_ceiling
from backtest.replay import SessionContext, SessionDecision
from backtest.sip import MIN_ORDER_VALUE_INR, simulate_sip_instalment
from dataplatform.query.pit import Dataset
from execution.broker import Exchange, Holding, OrderRequest, Side

__all__ = [
    "DELIVERY_COVERAGE_THRESHOLD",
    "RegimeReading",
    "SwingCompositeData",
    "SwingCompositeParameters",
    "SwingCompositePolicy",
    "SwingRecord",
    "active_legs",
    "composite_scores",
    "delivery_coverage",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_WEIGHT_QUANTUM = Decimal("0.00000001")

#: The share of a session's scored candidates that must carry a real NSE delivery print before a
#: delivery-derived leg (``delivery_share``, ``delivery_trend``) contributes to the composite (X2).
#: Below it the leg is dropped for that session and the composite is struck over the remaining
#: weighted legs — never scored off imputed values. Fixed a priori at 80 %: at most one candidate in
#: five may be carrying the median stand-in, so the leg ranks the cross-section on what was measured
#: It was not tuned against returns. It does bite on real data, and a reader must know where: before
#: 2016-09-02 the lake has no delivery prints at all (0 %, the ISIN-ordered noise this gate exists
#: to stop), and on the liquid universe after it coverage runs ~72 % in 2016 rising past 80 % around
#: 2019-2020 and ~92 % by 2026 — so the delivery legs switch on part-way through a decade window.
DELIVERY_COVERAGE_THRESHOLD = Decimal("0.80")

#: Each delivery-derived leg and the record flag that says its value was measured, not imputed.
_DELIVERY_LEGS: dict[str, str] = {
    "delivery_share": "delivery_observed",
    "delivery_trend": "delivery_trend_observed",
}


@dataclass(frozen=True, slots=True)
class SwingRecord:
    """One candidate this session: the three entry signals, a risk screen, a price, and its date.

    * ``high_proximity`` — the close divided by the highest close of the trailing 252 sessions, so
      ``1.0`` is a fresh 52-week high and ``0.7`` is 30 % below one. The strongest price signal
      measured here (rank-IC 0.101 at 63 sessions against 0.072 for the trailing return).
    * ``delivery_share`` — the mean NSE delivery percentage over the trailing 5 sessions, as a ratio
      (``0.55`` is 55 % delivered). The highest-t-statistic signal in the store.
    * ``momentum_12_1`` — the ``t-12m .. t-1m`` return, the classical momentum definition, kept
      because it carries information the other two do not.
    * ``volatility`` — trailing daily-return volatility, used only to *exclude* the most volatile
      tail (high volatility was the strongest negative signal measured; the lowest-volatility decile
      is not itself attractive, so volatility screens rather than scores).
    * ``price`` — the raw close the whole-share sizing and the trailing stop read (invariant #3:
      the adjusted series is for the signal, never for a fill).

    M12.1 added five more legs, each of which the lake already carried and no policy read. They are
    *features*, not directions: the sign lives in the weight, so ``weight_return_5`` negative is the
    short-term reversal family and positive is short-term trend, and neither is asserted here.

    * ``return_5`` — the 5-session return. The input to the one classical short-horizon effect
      nothing in this repo had measured.
    * ``momentum_1m`` — the 21-session return: the swing horizon's own trend, which the 12-1 leg
      deliberately excludes (12-1 skips the most recent month).
    * ``delivery_trend`` — the 5-session delivery mean over its 63-session mean, so ``1.2`` is a
      fifth more delivery than usual. Where ``delivery_share`` is the *level*, this is the change,
      and it is the largest name-level coefficient in X2's fitted forecast.
    * ``turnover_expansion`` — the 5-session mean traded value over its 63-session mean: whether the
      tape is getting busier in this name.
    * ``ma_proximity`` — the close over its own 50-session mean.

    ``delivery_observed`` / ``delivery_trend_observed`` (X2) mark whether the two delivery legs were
    measured or imputed; :func:`composite_scores` reads them to gate a delivery leg on coverage.

    Never holds a ``float``, a non-positive price, or a non-positive high proximity.
    """

    isin: str
    high_proximity: Decimal
    delivery_share: Decimal
    momentum_12_1: Decimal
    volatility: Decimal
    price: Decimal
    knowable_date: date
    # M12.1 legs. Neutral defaults, so a record built for the three original legs — or for the
    # per-session marks, which carry only a price — is unchanged and scores these at zero weight.
    return_5: Decimal = _ZERO
    momentum_1m: Decimal = _ZERO
    delivery_trend: Decimal = _ONE
    turnover_expansion: Decimal = _ONE
    ma_proximity: Decimal = _ONE
    # X2. Whether ``delivery_share`` / ``delivery_trend`` came from real prints in the window, or
    # are the stand-ins (the session median; the neutral 1) the lake could not measure. True by
    # default, so a record built without the flags is read as measured.
    delivery_observed: bool = True
    delivery_trend_observed: bool = True
    # Round 2, H1 (backtest.policies.residual_momentum). ``None`` is "excluded from the leg" — not
    # computed, or fewer than 200 valid sessions — and ranks at the leg's mean, never at a value.
    residual_momentum: Decimal | None = None

    def __post_init__(self) -> None:
        if self.residual_momentum is not None and not isinstance(self.residual_momentum, Decimal):
            raise TypeError("residual_momentum must be a Decimal or None")
        for name in (
            "high_proximity",
            "delivery_share",
            "momentum_12_1",
            "volatility",
            "price",
            "return_5",
            "momentum_1m",
            "delivery_trend",
            "turnover_expansion",
            "ma_proximity",
        ):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(
                    f"{name} must be a Decimal — money/signal is never float (CLAUDE.md)"
                )
        if self.price <= _ZERO:
            raise ValueError(f"price must be positive, got {self.price}")
        if self.high_proximity <= _ZERO:
            raise ValueError(f"high_proximity must be positive, got {self.high_proximity}")
        if self.volatility < _ZERO:
            raise ValueError(f"volatility must be non-negative, got {self.volatility}")


#: Parameters added after run digests were first persisted, each with the default at which it is
#: left out of ``repr`` — the run ledger keys a run on ``repr(parameters)``, so every arm that does
#: not use a newer leg keeps the digest it was persisted (and frozen as a baseline) under.
_DIGEST_OPTIONAL_PARAMETERS: dict[str, object] = {
    "weight_residual_momentum": _ZERO,
    "redeploy_next_session": False,
}


@dataclass(frozen=True, slots=True, repr=False)
class SwingCompositeParameters:
    """The stated knobs. Every default is a measured finding or a round number, never a fit.

    Entry:

    * ``top_n`` (20) — names held, matching every other policy here so a comparison isolates the
      signal rather than the basket size.
    * ``weight_high`` / ``weight_delivery`` / ``weight_momentum`` (1 each) — equal weights on the
      three rank-normalised components. Equal because the three measured within a whisker of each
      other and any other split would be a fit.
    * ``exclude_vol_fraction`` (0.10) — drop the most volatile tenth of the candidate set before
      scoring. Volatility's negative relationship with forward return is the strongest measured
      (rank-IC -0.081 at 63 sessions), but it is not monotone at the low end, so it is a screen.

    Exit — three rules, any of which can fire:

    * ``sell_band`` (60, i.e. 3x ``top_n``) — hysteresis. A holding is sold once its composite rank
      leaves the top ``sell_band``, not merely the top ``top_n``, so a name drifting between rank 20
      and 60 is carried instead of churned. Three times the basket is the round multiple, and the
      band is the *whole* turnover control: 81 % of a top-20 composite basket is still inside the
      top 40 one fortnight later and ~95 % inside the top 100, so the band and not the calendar is
      what sets how much this policy trades. The report sweeps it.
    * ``max_hold_sessions`` (63, ~90 calendar days — the top of the band) — a re-underwrite, not a
      forced liquidation: at this age a holding must still be inside the top ``top_n`` to be
      carried, otherwise it is sold. Forcing out a name that still ranks first would pay a round
      trip to buy it straight back.
    * ``trailing_stop`` (0.25) — sell when the close falls this far below the position's highest
      close since entry. Checked every session. Wide by design: tighter settings were measured and
      reduce return (see the module docstring). ``None`` disables it.
    * ``resell_cooldown_sessions`` (21, a trading month) — how long to wait before re-staging a
      sell that did not fill. Only ever binds on a name that has stopped printing and so cannot be
      filled at all; see :class:`_Position`.
    * ``min_hold_sessions`` (5, ~7 calendar days — the bottom of the requested band) — no rule
      except the trailing stop may sell inside this age. Stops a name entered on one rebalance from
      being sold on the next before its thesis has had a fortnight to work.

    Cadence and sizing:

    * ``rebalance_interval_sessions`` (10) — decide every fortnight rather than monthly. This is the
      knob that raises trade frequency; the sell band, not the calendar, sets turnover.
    * ``buy_budget_fraction`` (0.98) — the mechanical execution margin, as elsewhere.
    * ``sleeve`` — the journal sleeve every trade is tagged with.
    * ``redeploy_next_session`` (off) — once the sale proceeds of a rebalance that sold something
      have *settled*, deploy the free cash into that rebalance's own target set, instead of letting
      it sit until the next decision session. See :meth:`SwingCompositePolicy._redeploy`. Off by
      default, and absent from ``repr`` when off (see ``_DIGEST_OPTIONAL_PARAMETERS``).
    """

    top_n: int = 20
    sell_band: int = 60
    rebalance_interval_sessions: int = 10
    max_hold_sessions: int = 63
    min_hold_sessions: int = 5
    trailing_stop: Decimal | None = Decimal("0.25")
    resell_cooldown_sessions: int = 21
    exclude_vol_fraction: Decimal = Decimal("0.10")
    weight_high: Decimal = _ONE
    weight_delivery: Decimal = _ONE
    weight_momentum: Decimal = _ONE
    # M12.1: signed weights on the new legs, every one zero by default so the arm above this line
    # is bit-for-bit the M10.7 policy. A negative weight ranks a leg's reverse — that is how the
    # reversal family is expressed, rather than by storing a negated feature.
    weight_return_5: Decimal = _ZERO
    weight_momentum_1m: Decimal = _ZERO
    weight_delivery_trend: Decimal = _ZERO
    weight_turnover_expansion: Decimal = _ZERO
    weight_ma_proximity: Decimal = _ZERO
    weight_volatility: Decimal = _ZERO
    regime_filter: bool = False
    buy_budget_fraction: Decimal = Decimal("0.98")
    sleeve: Sleeve = Sleeve.TACTICAL
    # Round 2, H1: the residual-momentum leg (backtest.policies.residual_momentum). Zero by default,
    # and absent from ``repr`` at zero (see _DIGEST_OPTIONAL_PARAMETERS).
    weight_residual_momentum: Decimal = _ZERO
    # Idle-cash fix: momentum v2's ``redeploy_next_session``, settlement-aware. Off by default, and
    # absent from ``repr`` when off, so no arm persisted before it changes digest.
    redeploy_next_session: bool = False

    def __repr__(self) -> str:
        shown = (
            f"{f.name}={getattr(self, f.name)!r}"
            for f in fields(self)
            if f.name not in _DIGEST_OPTIONAL_PARAMETERS
            or getattr(self, f.name) != _DIGEST_OPTIONAL_PARAMETERS[f.name]
        )
        return f"{type(self).__qualname__}({', '.join(shown)})"

    def __post_init__(self) -> None:
        if self.top_n <= 0:
            raise ValueError(f"top_n must be positive, got {self.top_n}")
        if self.sell_band < self.top_n:
            raise ValueError(
                f"sell_band {self.sell_band} must be at least top_n {self.top_n} — an outer band "
                "narrower than the buy set would sell a name the same session it was bought"
            )
        for name in (
            "rebalance_interval_sessions",
            "max_hold_sessions",
            "resell_cooldown_sessions",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")
        if self.min_hold_sessions < 0:
            raise ValueError(f"min_hold_sessions must be >= 0, got {self.min_hold_sessions}")
        if self.min_hold_sessions >= self.max_hold_sessions:
            raise ValueError(
                f"min_hold_sessions {self.min_hold_sessions} must be below max_hold_sessions "
                f"{self.max_hold_sessions}"
            )
        for name in (
            "buy_budget_fraction",
            "exclude_vol_fraction",
            "weight_high",
            "weight_delivery",
            "weight_momentum",
            "weight_return_5",
            "weight_momentum_1m",
            "weight_delivery_trend",
            "weight_turnover_expansion",
            "weight_ma_proximity",
            "weight_volatility",
            "weight_residual_momentum",
        ):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be a Decimal")
        if not (_ZERO < self.buy_budget_fraction <= _ONE):
            raise ValueError(
                f"buy_budget_fraction must be in (0, 1], got {self.buy_budget_fraction}"
            )
        if not (_ZERO <= self.exclude_vol_fraction < _ONE):
            raise ValueError(
                f"exclude_vol_fraction must be in [0, 1), got {self.exclude_vol_fraction}"
            )
        if self.trailing_stop is not None:
            if not isinstance(self.trailing_stop, Decimal):
                raise TypeError("trailing_stop must be a Decimal or None")
            if not (_ZERO < self.trailing_stop < _ONE):
                raise ValueError(f"trailing_stop must be in (0, 1), got {self.trailing_stop}")


@runtime_checkable
class SwingCompositeData(Protocol):
    """Where the policy reads its world — the injected seam the point-in-time context wraps.

    * ``is_rebalance(session)`` — is today a decision session? The interval is the data source's to
      apply, because it owns the trading calendar; the policy only asks.
    * ``signal(as_of)`` — the candidate set as a guardable :class:`~dataplatform.query.pit.Dataset`,
      already narrowed to the investable/liquid universe.
    * ``marks(as_of)`` — this session's raw closes for held names, so the trailing stop can be
      checked on *every* session rather than only on a rebalance. Returned as a dataset for the same
      reason: it is admitted through the guard like everything else.
    """

    def is_rebalance(self, session: date) -> bool:
        """Whether ``session`` is a decision session."""

    def signal(self, as_of: date) -> Dataset[SwingRecord]:
        """The PIT candidate set as of ``as_of``."""

    def marks(self, as_of: date) -> Dataset[SwingRecord]:
        """This session's raw closes, as records, for stop checking."""

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        """The broad-market regime as of ``as_of``, as a one-element guardable dataset (M12.1).

        Read only when ``regime_filter`` is on. The reading is momentum v2's
        :class:`~backtest.policies.momentum_v2.RegimeReading` unchanged, so both policies are gated
        by the same definition of risk-off and a difference between them is never the definition.
        """


def _weighted_legs(params: SwingCompositeParameters) -> tuple[tuple[str, Decimal], ...]:
    """Every leg with a non-zero weight, in a fixed order."""
    legs = (
        ("high_proximity", params.weight_high),
        ("delivery_share", params.weight_delivery),
        ("momentum_12_1", params.weight_momentum),
        ("return_5", params.weight_return_5),
        ("momentum_1m", params.weight_momentum_1m),
        ("delivery_trend", params.weight_delivery_trend),
        ("turnover_expansion", params.weight_turnover_expansion),
        ("ma_proximity", params.weight_ma_proximity),
        ("volatility", params.weight_volatility),
        ("residual_momentum", params.weight_residual_momentum),
    )
    return tuple((attribute, weight) for attribute, weight in legs if weight != _ZERO)


def delivery_coverage(records: Sequence[SwingRecord], attribute: str) -> Decimal:
    """The share of ``records`` whose delivery-derived ``attribute`` was measured (X2).

    Zero for an empty set. Assumes ``attribute`` is one of the delivery legs.
    """
    if not records:
        return _ZERO
    flag = _DELIVERY_LEGS[attribute]
    observed = sum(1 for record in records if getattr(record, flag))
    return Decimal(observed) / Decimal(len(records))


def active_legs(
    records: Sequence[SwingRecord], params: SwingCompositeParameters
) -> tuple[tuple[str, Decimal], ...]:
    """The weighted legs that contribute this session: a delivery leg only at sufficient coverage.

    A delivery-derived leg whose coverage over ``records`` is below
    :data:`DELIVERY_COVERAGE_THRESHOLD` is left out, so the composite is struck over the rest. An
    empty result means no leg can rank the candidates at all — the caller must not rank them
    (ordering by ISIN is what a zero-information composite would otherwise collapse to).
    """
    return tuple(
        (attribute, weight)
        for attribute, weight in _weighted_legs(params)
        if attribute not in _DELIVERY_LEGS
        or delivery_coverage(records, attribute) >= DELIVERY_COVERAGE_THRESHOLD
    )


def composite_scores(
    records: Sequence[SwingRecord], params: SwingCompositeParameters
) -> dict[str, Decimal]:
    """Rank-normalise each active leg across ``records`` and return the weighted composite per ISIN.

    Each component is converted to its cross-sectional rank scaled onto ``[-1, +1]``
    (``2 * rank / (n + 1) - 1``) rather than a z-score, because every one of these signals has fat
    tails — a name at a 400 % trailing return would otherwise dominate a z-scored basket. **Tied
    values share their mean rank** (X2): before, ties were broken by ISIN, which handed a block of
    imputed or neutral-filled values — a 28 % median-imputed delivery block, every unmeasurable
    ``return_5`` at 0 — a spread of ranks in ISIN order, noise that looked like signal. A tie now
    scores identically, and a leg on which every name ties adds the same constant to every score.

    Only :func:`active_legs` contribute: a delivery leg below the coverage threshold is dropped and
    the composite reweights over the remaining legs. Returns ``{}`` when no leg is active, so no
    caller can mistake a zero-information composite for a ranking.

    Assumes ``records`` is the already-screened candidate set. Never reads a clock or a store.
    """
    legs = active_legs(records, params)
    if not records or not legs:
        return {}
    scores: dict[str, Decimal] = dict.fromkeys((r.isin for r in records), _ZERO)
    for attribute, weight in legs:
        # A name excluded from a leg (a ``None`` value — only residual momentum has one) is ranked
        # on the others and scores 0 here, the leg's mean rank; the rest rank among themselves.
        ranked = [r for r in records if getattr(r, attribute) is not None]
        n = len(ranked)
        scale = Decimal(n + 1)
        order = sorted(ranked, key=lambda r: getattr(r, attribute))
        start = 0
        while start < n:
            value = getattr(order[start], attribute)
            end = start
            while end + 1 < n and getattr(order[end + 1], attribute) == value:
                end += 1
            # 1-based ranks start+1 .. end+1 share their mean.
            rank = Decimal(start + end + 2) / _TWO
            leg_score = weight * (_TWO * rank / scale - _ONE)
            for record in order[start : end + 1]:
                scores[record.isin] += leg_score
            start = end + 1
    return scores


@dataclass
class _Position:
    """What the policy remembers about a holding: age, running peak, and its last sell attempt.

    ``sell_staged_ago`` counts the sessions since a sell was last staged for this holding, or
    ``None`` if none has been. It exists because an exit is not guaranteed to execute: a name that
    stops printing — suspended, delisted, moved off the segment — has no reference bar for the
    broker to fill against, so the sell is rejected and the position stays on the book. Without
    this the exit rules would re-stage that same sell on every rebalance for the rest of the run,
    which is both operationally wrong (an unfillable order is not a new decision) and would report
    a handful of stuck names as hundreds of trades.
    """

    entered_on: date
    sessions_held: int = 0
    peak: Decimal = _ZERO
    sell_staged_ago: int | None = None
    lots: _Lots | None = None


@dataclass(frozen=True, slots=True)
class _Lots:
    """One ISIN's shares on the account — settled and pending — and their total cost basis."""

    quantity: int
    cost: Decimal


#: How close two cost bases must be to count as unchanged. The broker keeps each lot's total cost
#: and reports ``cost / quantity``; ``quantity x average_price`` recovers it to Decimal precision
#: (~1e-27 relative), while the smallest trade the policy can make moves it by a whole share.
_SAME_COST = Decimal("1e-12")


@dataclass(frozen=True, slots=True)
class _PendingTarget:
    """A rebalance's target set, awaiting the settlement of that rebalance's sale proceeds.

    ``target`` and ``sleeve_of`` are exactly what the rebalance sized its buys against; nothing is
    re-scored while it waits. ``decided_on`` is the rebalance session, for the journal.
    """

    decided_on: date
    target: dict[str, SwingRecord]
    sleeve_of: dict[str, TierSleeve] | None


def _same_cost(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(abs(a), abs(b)) * _SAME_COST


def _account_lots(ctx: SessionContext, *, before: date | None = None) -> dict[str, _Lots]:
    """Each ISIN's settled holding plus pending buys, or only those filled before ``before``.

    ``before=ctx.session`` leaves out this session's fills, so what remains is exactly the lots the
    account held at the previous decision, moved on only by settlement (neutral) or a corporate
    action.
    """
    quantity: dict[str, int] = {}
    cost: dict[str, Decimal] = {}
    lines: list[tuple[str, int, Decimal]] = [
        (h.isin, h.quantity, h.average_price) for h in ctx.broker.holdings()
    ]
    lines += [
        (p.isin, p.quantity, p.average_price)
        for p in ctx.broker.positions()
        if before is None or p.session < before
    ]
    for isin, qty, average in lines:
        quantity[isin] = quantity.get(isin, 0) + qty
        cost[isin] = cost.get(isin, _ZERO) + Decimal(qty) * average
    return {isin: _Lots(quantity[isin], cost[isin]) for isin in quantity}


def _rescale_reference(position: _Position, now: _Lots | None) -> None:
    """Put the stop's peak on the post-split scale when the share count moved at an unchanged basis.

    ``position.lots`` is what the previous decision saw; ``now`` is the same lots today, before this
    session's fills. Equal basis and a different count is a split or bonus (nothing the policy
    trades does that), and ``old / new`` shares is the inverse of its ratio. Anything else leaves
    the peak be.
    """
    before = position.lots
    if before is None or now is None or now.quantity <= 0 or now.quantity == before.quantity:
        return
    if not _same_cost(before.cost, now.cost):
        return
    position.peak = position.peak * Decimal(before.quantity) / Decimal(now.quantity)


class SwingCompositePolicy:
    """Composite-signal swing trading: 7-90 day holds with stated entry and exit rules.

    Construct it with a :class:`SwingCompositeData` source and :class:`SwingCompositeParameters`. It
    satisfies :class:`backtest.replay.Policy`, so the same object runs under replay and live
    (invariant #5). Every session it ages its positions and checks the trailing stop; on a decision
    session it additionally re-scores the universe and applies the band and re-underwrite rules.
    """

    __slots__ = (
        "_band_hits",
        "_data",
        "_order_caps",
        "_params",
        "_pending",
        "_positions",
        "_sleeves",
        "_tiers",
    )

    def __init__(
        self,
        data: SwingCompositeData,
        params: SwingCompositeParameters | None = None,
        *,
        band_hits: BandHitData | None = None,
        order_caps: RiskRails | None = None,
        tiers: CapTierData | None = None,
        sleeves: Sequence[TierSleeve] | None = None,
    ) -> None:
        self._data = data
        self._params = params if params is not None else SwingCompositeParameters()
        self._positions: dict[str, _Position] = {}
        #: The last rebalance's target, while its sale proceeds settle (``redeploy_next_session``).
        #: ``None`` when nothing is pending. A pure function of earlier decisions and the broker's
        #: reported book, so a replay reproduces it.
        self._pending: _PendingTarget | None = None
        # X2 H2: band-hit avoidance is on exactly when a source is injected. It is not a field of
        # SwingCompositeParameters on purpose — a new field would change the repr, and so the
        # persisted digest, of every arm already run, the frozen baseline included.
        self._band_hits = band_hits
        # The rails A8 will clear this policy's orders against. With them, no buy is sized past the
        # per-order ceiling (``analyst.rails.order_value_ceiling``): a buy the rail is bound to
        # refuse leaves its cash idle, the next rebalance spreads the larger idle balance over the
        # same names, and every buy grows past the cap until the book is all cash. Sized to the
        # ceiling, a name below its weight is topped up across rebalances instead. Not a field of
        # the parameters, for the same digest reason as ``band_hits``.
        self._order_caps = order_caps
        # X2 cap tiers (backtest.cap_tiers): with a tier source and its sleeves, the book is bought
        # tier by tier — each sleeve takes its top names by *within-tier* composite rank, and each
        # holding is judged against its own tier's band. The composite itself is unchanged: it is
        # still struck over the whole candidate set, so a tier arm ranks on exactly the scores the
        # untiered arm does. Injected, not a parameter, for the same digest reason as above.
        if (tiers is None) != (sleeves is None):
            raise ValueError("a tiered book needs both a tier source and its sleeves")
        self._tiers = tiers
        self._sleeves: tuple[TierSleeve, ...] = tuple(sleeves or ())
        if sleeves is not None:
            if not self._sleeves:
                raise ValueError("a tiered book needs at least one sleeve")
            if len({sleeve.tier for sleeve in self._sleeves}) != len(self._sleeves):
                raise ValueError("a tier may carry at most one sleeve")
            if sum(sleeve.top_n for sleeve in self._sleeves) != self._params.top_n:
                raise ValueError(
                    f"the sleeves buy {sum(sleeve.top_n for sleeve in self._sleeves)} names but "
                    f"top_n is {self._params.top_n}; they must agree"
                )
            if band_hits is not None:
                raise ValueError("band-hit avoidance is not wired for a tiered book")

    def decide(self, ctx: SessionContext) -> SessionDecision:
        """Age the book and check stops every session; re-score and rotate on a decision session."""
        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        marks = {
            record.isin: record.price for record in ctx.pit.admit(self._data.marks(ctx.session))
        }
        self._age(
            ctx.session,
            held,
            marks,
            carried=_account_lots(ctx, before=ctx.session),
            book=_account_lots(ctx),
        )

        stopped = self._stop_outs(held, marks)
        self._record_sell(stopped)
        if self._data.is_rebalance(ctx.session):
            # A new rebalance supersedes whatever the last one left pending.
            self._pending = None
            return self._rebalance(ctx, held, marks, stopped)
        if self._pending is not None:
            return self._redeploy(ctx, self._pending, marks, stopped)
        return self._session_decision(ctx, stopped, note="stop check only; no rebalance due")

    # ── position bookkeeping ─────────────────────────────────────────────────────────────────────

    def _age(
        self,
        session: date,
        held: Mapping[str, Holding],
        marks: Mapping[str, Decimal],
        *,
        carried: Mapping[str, _Lots],
        book: Mapping[str, _Lots],
    ) -> None:
        """Advance each holding's age and running peak; forget names no longer on the book.

        ``carried`` is the account as the previous decision left it, moved on only by settlement and
        corporate actions; ``book`` is the whole account now, remembered for the next session. A
        split or bonus rescales the peak before today's close is compared to it (module docstring).
        """
        self._follow_reissues(held, carried)
        for isin in list(self._positions):
            if isin not in held:
                del self._positions[isin]
        for isin in sorted(held):
            position = self._positions.get(isin)
            if position is None:
                position = _Position(entered_on=session)
                self._positions[isin] = position
            else:
                position.sessions_held += 1
                if position.sell_staged_ago is not None:
                    position.sell_staged_ago += 1
                _rescale_reference(position, carried.get(isin))
            mark = marks.get(isin)
            if mark is not None and mark > position.peak:
                position.peak = mark
            position.lots = book.get(isin)

    def _follow_reissues(self, held: Mapping[str, Holding], carried: Mapping[str, _Lots]) -> None:
        """Move a position to its survivor ISIN when the account carried the holding over a reissue.

        A face-value split usually retires the ISIN: the book carries the holding 1:1 to the
        survivor and rescales it there, at the same total basis. So a tracked name that left the
        book with no sell staged last session, and an untracked one that arrived at its basis, are
        one position — its age, peak and sell history continue rather than start again.
        """
        gone = [
            isin
            for isin, position in sorted(self._positions.items())
            if isin not in held and position.lots is not None and position.sell_staged_ago != 0
        ]
        for arrival in sorted(isin for isin in held if isin not in self._positions):
            lots = carried.get(arrival)
            if lots is None:
                continue
            for isin in gone:
                previous = self._positions[isin].lots
                if previous is not None and _same_cost(previous.cost, lots.cost):
                    self._positions[arrival] = self._positions.pop(isin)
                    gone.remove(isin)
                    break

    def _may_sell(self, isin: str) -> bool:
        """Whether a sell may be staged for ``isin`` now — i.e. no recent attempt is outstanding.

        A holding still on the book ``resell_cooldown_sessions`` after its last staged sell has had
        that sell rejected (a filled one would have removed it), so the exit is retried; inside the
        cooldown it is not re-staged.
        """
        position = self._positions.get(isin)
        if position is None or position.sell_staged_ago is None:
            return True
        return position.sell_staged_ago >= self._params.resell_cooldown_sessions

    def _record_sell(self, orders: Sequence[tuple[OrderRequest, str]]) -> None:
        """Stamp each staged sell on its position, so a rejected one is not re-staged at once."""
        for order, _ in orders:
            position = self._positions.get(order.isin)
            if position is not None:
                position.sell_staged_ago = 0

    def _stop_outs(
        self, held: Mapping[str, Holding], marks: Mapping[str, Decimal]
    ) -> list[tuple[OrderRequest, str]]:
        """Full-quantity sells for holdings whose close has fallen past the trailing stop."""
        stop = self._params.trailing_stop
        if stop is None:
            return []
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            position = self._positions.get(isin)
            mark = marks.get(isin)
            if position is None or mark is None or position.peak <= _ZERO:
                continue
            if not self._may_sell(isin):
                continue
            trigger = position.peak * (_ONE - stop)
            if mark <= trigger:
                quantity = held[isin].quantity
                sells.append(
                    (
                        OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="SWING"),
                        f"trailing stop: {mark} is {stop:%} or more below the peak {position.peak} "
                        f"since entry on {position.entered_on.isoformat()}; selling {quantity}",
                    )
                )
        return sells

    # ── the decision session ─────────────────────────────────────────────────────────────────────

    def _rebalance(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        marks: Mapping[str, Decimal],
        stopped: Sequence[tuple[OrderRequest, str]],
    ) -> SessionDecision:
        """Score the universe, apply the three exit rules, then size buys toward the target set."""
        candidates = tuple(ctx.pit.admit(self._data.signal(ctx.session)))
        if candidates and not active_legs(candidates, self._params):
            # X2. Every weighted leg is delivery-derived and delivery is too thin to rank on. No
            # ranking is struck — so no band or re-underwrite sell (they read ranks) and no buy.
            # Ordering by ISIN is what this session would otherwise have traded on.
            return self._session_decision(ctx, stopped, note=self._unscoreable_note(candidates))
        # Score the *whole* candidate set, so a holding is ranked among everything that is
        # scoreable. The volatility screen then narrows only what may be *bought*: screening before
        # scoring would leave a holding that turned volatile unranked, and the exit rules would read
        # that as "gone from the universe" and liquidate it on a risk screen it was never sold on.
        scores = composite_scores(candidates, self._params)
        by_isin = {record.isin: record for record in candidates}
        # Descending score, ties by ISIN — the rank a holding is judged against.
        ranked = sorted(candidates, key=lambda r: (-scores[r.isin], r.isin))
        rank_of = {record.isin: rank for rank, record in enumerate(ranked, start=1)}
        buyable = {record.isin for record in self._screen(ranked)}
        sleeve_of: dict[str, TierSleeve] | None = None
        if self._tiers is not None:
            tier_of = {m.isin: m.tier for m in ctx.pit.admit(self._tiers.tiers(ctx.session))}
            rank_of, sleeve_of, tiered_choice = self._tiered(ranked, buyable, tier_of)
        # X2 H2: no *new* buy of a name that hit a price band in the lookback. It narrows only what
        # may be bought, like the volatility screen, so a blocked holding keeps its rank and is
        # never sold for it; the next-ranked unblocked name takes the slot.
        would_choose = [record.isin for record in ranked if record.isin in buyable]
        blocked = self._band_hit_blocked(ctx)
        buyable -= blocked
        chosen = [record for record in ranked if record.isin in buyable][: self._params.top_n]
        if sleeve_of is not None:
            chosen = tiered_choice

        already_selling = {order.isin for order, _ in stopped}
        rule_sells = self._rule_sells(held, rank_of, already_selling, sleeve_of)
        self._record_sell(rule_sells)
        sells = list(stopped) + rule_sells
        exiting = {order.isin for order, _ in sells}
        target = {record.isin: record for record in chosen if record.isin not in exiting}
        # M12.1's regime gate suppresses *buys* only. Every exit above this line has already been
        # staged, which is the whole point: risk-off must never trap a position the band, the
        # re-underwrite or the stop has decided to sell. With no buys the book runs down through its
        # own exits rather than being liquidated on the gate.
        if self._params.regime_filter and not self._risk_on(ctx):
            target = {}
            would_choose = []
        buys, drift = self._buys(
            ctx, {isin: h.quantity for isin, h in held.items()}, target, marks, sleeve_of
        )
        if self._params.redeploy_next_session and target and sells:
            # Only a rebalance that sold something leaves proceeds to deploy once they settle.
            self._pending = _PendingTarget(
                decided_on=ctx.session,
                target=dict(target),
                sleeve_of=dict(sleeve_of) if sleeve_of is not None else None,
            )

        orders = tuple(order for order, _ in (*sells, *buys))
        entries = tuple(self._entry(ctx, order, note) for order, note in (*sells, *buys))
        evidence = self._evidence(
            ctx.session, chosen, scores, by_isin, drift, len(candidates), sleeve_of
        )
        withheld = [
            isin
            for isin in would_choose[: self._params.top_n]
            if isin in blocked and isin not in exiting
        ]
        if withheld:
            evidence, blocked_lines = self._band_hit_entries(ctx, held, withheld, evidence)
            entries += blocked_lines
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _redeploy(
        self,
        ctx: SessionContext,
        pending: _PendingTarget,
        marks: Mapping[str, Decimal],
        stopped: Sequence[tuple[OrderRequest, str]],
    ) -> SessionDecision:
        """Deploy settled cash into the last rebalance's target set, once its proceeds have settled.

        A rebalance's sells fill the next session and pay out on that fill's settlement cycle (T+2
        before 2023-01-27, T+1 after, counted in trading sessions by the broker). While any sale
        proceeds are still in settlement the session only checks stops and says it is waiting —
        nothing is bought on cash the account does not yet hold, and the budget is ``available``
        (settled cash) exactly as on a rebalance. The first session with nothing in settlement
        sizes buys toward the stored target through the same :meth:`_buys` path, rail ceiling
        included, and the pending target is spent. The next rebalance supersedes it either way.

        Decides by the stored target only: no signal, regime or band-hit read, no re-ranking. The
        only input it reads that a stop-check session does not is the broker's own book. A target
        name stopped out since the rebalance is dropped from the target rather than bought back,
        and one without a mark this session is left out of this pass (never priced on a guess).
        """
        for order, _ in stopped:
            pending.target.pop(order.isin, None)
        margins = ctx.broker.margins()
        if margins.unsettled_proceeds > _ZERO:
            note = (
                f"redeploy pending: {margins.unsettled_proceeds} of sale proceeds still settling "
                f"from the {pending.decided_on.isoformat()} rebalance; nothing bought"
            )
            return self._session_decision(ctx, stopped, note=note)
        self._pending = None
        target = {isin: record for isin, record in pending.target.items() if isin in marks}
        quantities = {isin: lots.quantity for isin, lots in _account_lots(ctx).items()}
        buys, drift = self._buys(
            ctx,
            quantities,
            target,
            marks,
            pending.sleeve_of,
            prices={isin: marks[isin] for isin in target},
            note=f"redeploy settled proceeds of the {pending.decided_on.isoformat()} rebalance",
        )
        if not buys:
            return self._session_decision(
                ctx,
                stopped,
                note=f"redeploy session for the {pending.decided_on.isoformat()} rebalance: "
                "nothing affordable reduces drift",
            )
        orders = tuple(order for order, _ in (*stopped, *buys))
        entries = tuple(self._entry(ctx, order, note) for order, note in (*stopped, *buys))
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="redeploy_budget",
                    as_of=ctx.session,
                    value=margins.available * self._params.buy_budget_fraction,
                    detail={
                        "rebalance": pending.decided_on.isoformat(),
                        "names_bought": str(len(buys)),
                        "tracking_drift": str(drift),
                    },
                    text="settled proceeds of the last rebalance's sells, deployed into its target"
                    f"; {len(stopped)} trailing-stop exit(s)",
                ),
            ),
        )
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _tiered(
        self,
        ranked: Sequence[SwingRecord],
        buyable: set[str],
        tier_of: Mapping[str, CapTier],
    ) -> tuple[dict[str, int], dict[str, TierSleeve], list[SwingRecord]]:
        """Within-tier ranks, each ranked name's sleeve, and each sleeve's buys (X2 cap tiers).

        A name's within-tier rank is its position, in the composite order, among the ranked names
        of its own tier. A name in no sleeve's tier is left unranked — the exit rules read that as
        "gone from this book's universe", exactly as they read a name that left the liquid set.
        Each sleeve buys its ``top_n`` best buyable names; a tier with fewer buyable names leaves
        its slots empty rather than handing them to another tier.
        """
        sleeves = {sleeve.tier: sleeve for sleeve in self._sleeves}
        seen = dict.fromkeys(sleeves, 0)
        picked = dict.fromkeys(sleeves, 0)
        rank_of: dict[str, int] = {}
        sleeve_of: dict[str, TierSleeve] = {}
        chosen: list[SwingRecord] = []
        for record in ranked:
            tier = tier_of.get(record.isin)
            if tier is None or tier not in sleeves:
                continue
            sleeve = sleeves[tier]
            seen[tier] += 1
            rank_of[record.isin] = seen[tier]
            sleeve_of[record.isin] = sleeve
            if record.isin in buyable and picked[tier] < sleeve.top_n:
                picked[tier] += 1
                chosen.append(record)
        return rank_of, sleeve_of, chosen

    def _unscoreable_note(self, candidates: Sequence[SwingRecord]) -> str:
        coverage = ", ".join(
            f"{attribute} {delivery_coverage(candidates, attribute):.1%}"
            for attribute, _ in _weighted_legs(self._params)
            if attribute in _DELIVERY_LEGS
        )
        return (
            f"rebalance not ranked: every weighted leg is delivery-derived and delivery coverage "
            f"of the {len(candidates)} candidates ({coverage}) is below "
            f"{DELIVERY_COVERAGE_THRESHOLD:.0%}"
        )

    def _risk_on(self, ctx: SessionContext) -> bool:
        """Whether the broad market sits at or above its own trailing mean (M12.1).

        Assumes the data source serves exactly one reading for the session, admitted through the
        same point-in-time guard as every other read. A session with no reading at all is treated
        as risk-**off**: an absent regime is not a licence to buy, and silently defaulting to
        risk-on would make the gate disappear on exactly the sessions the store is thinnest.
        """
        readings = tuple(ctx.pit.admit(self._data.regime(ctx.session)))
        if not readings:
            return False
        return readings[0].risk_on

    def _band_hit_blocked(self, ctx: SessionContext) -> frozenset[str]:
        """The ISINs H2 bars from a buy this session; empty when the filter is off (X2)."""
        if self._band_hits is None:
            return frozenset()
        hits = ctx.pit.admit(self._band_hits.band_hits(ctx.session))
        return band_hit_blocked(hits, as_of=ctx.session, window=self._band_hits.window(ctx.session))

    def _band_hit_entries(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        withheld: Sequence[str],
        evidence: EvidenceBundle,
    ) -> tuple[EvidenceBundle, tuple[JournalEntry, ...]]:
        """The session's evidence with the withheld names added, and one no-op line for each.

        ``withheld`` are the names the session would have targeted without H2 that the filter
        blocked and no exit is already selling: buys that did not happen *because of* H2, the count
        the smoke reports. A no-op is still a decision (invariant #9), so each is journaled against
        the evidence that names it. A withheld name already on the book is kept, and only its top-up
        toward equal weight is withheld; its line says so.
        """
        items = tuple(
            EvidenceItem(
                kind=EvidenceKind.POLICY,
                source="nse_pr_bundle",
                label="band_hit_blocked",
                isin=isin,
                as_of=ctx.session,
                text="hit a daily price band in the last 5 sessions",
            )
            for isin in withheld
        )
        evidence = EvidenceBundle(
            trading_date=evidence.trading_date,
            actor=evidence.actor,
            items=(*evidence.items, *items),
        )
        ref = evidence.ref().ref
        entries = tuple(
            JournalEntry(
                ts=ctx.clock.now(),
                trading_date=ctx.session,
                actor=Actor.T0,
                decision=Decision.HEARTBEAT,
                isin=isin,
                evidence_snapshot_ref=ref,
                rationale=(
                    f"{BAND_HIT_BLOCK_RATIONALE}: composite top-{self._params.top_n} but hit a "
                    "daily price band in the last 5 sessions; "
                    + ("held, kept, not topped up" if isin in held else "not bought")
                ),
            )
            for isin in withheld
        )
        return evidence, entries

    def _screen(self, candidates: Sequence[SwingRecord]) -> tuple[SwingRecord, ...]:
        """The names eligible to be *bought*: all but the most volatile ``exclude_vol_fraction``."""
        fraction = self._params.exclude_vol_fraction
        if fraction == _ZERO or not candidates:
            return tuple(candidates)
        keep = len(candidates) - int(len(candidates) * fraction)
        if keep <= 0:
            return ()
        # Ascending volatility, ties by ISIN: the calmest `keep` names survive, deterministically.
        by_vol = sorted(candidates, key=lambda r: (r.volatility, r.isin))
        return tuple(by_vol[:keep])

    def _rule_sells(
        self,
        held: Mapping[str, Holding],
        rank_of: Mapping[str, int],
        already_selling: set[str],
        sleeve_of: Mapping[str, TierSleeve] | None = None,
    ) -> list[tuple[OrderRequest, str]]:
        """Band and re-underwrite exits, in ISIN order. Names inside ``min_hold`` are carried.

        On a tiered book (``sleeve_of``) a rank is within the holding's tier and is judged against
        that tier's sleeve; a holding no sleeve's tier contains is sold as gone from the universe.
        """
        params = self._params
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            if isin in already_selling:
                continue
            position = self._positions.get(isin)
            age = position.sessions_held if position is not None else 0
            if age < params.min_hold_sessions or not self._may_sell(isin):
                continue
            # A name that has dropped out of the scored universe entirely (delisted from the liquid
            # set, or no longer computable) ranks worse than any surviving name.
            rank = rank_of.get(isin)
            quantity = held[isin].quantity
            top_n, sell_band, rank_name = params.top_n, params.sell_band, "composite rank"
            sleeve = sleeve_of.get(isin) if sleeve_of is not None else None
            if sleeve is not None:
                top_n, sell_band = sleeve.top_n, sleeve.sell_band
                rank_name = f"{sleeve.tier.value}-tier composite rank"
            if rank is None and sleeve_of is not None:
                reason = (
                    "no longer ranked in a liquidity-rank tier this book holds "
                    f"({', '.join(s.tier.value for s in self._sleeves)}) or in the scored "
                    f"universe; selling {quantity} shares"
                )
            elif rank is None:
                reason = f"no longer in the scored universe; selling {quantity} shares"
            elif rank > sell_band:
                reason = (
                    f"{rank_name} {rank} left the top-{sell_band} band "
                    f"(bought into top-{top_n}); selling {quantity} shares"
                )
            elif age >= params.max_hold_sessions and rank > top_n:
                reason = (
                    f"held {age} sessions (max {params.max_hold_sessions}) and rank {rank} no "
                    f"longer re-qualifies for the top-{top_n}; selling {quantity} shares"
                )
            else:
                continue
            sells.append(
                (OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="SWING"), reason)
            )
        return sells

    def _buys(
        self,
        ctx: SessionContext,
        held: Mapping[str, int],
        target: Mapping[str, SwingRecord],
        marks: Mapping[str, Decimal],
        sleeve_of: Mapping[str, TierSleeve] | None = None,
        *,
        prices: Mapping[str, Decimal] | None = None,
        note: str | None = None,
    ) -> tuple[list[tuple[OrderRequest, str]], Decimal]:
        """Whole-share buys toward equal weight over the target set, sized from currently-free cash.

        Budget is the cash *already* free times ``buy_budget_fraction`` — never this session's sale
        proceeds, which have not settled — so a buy is never rejected for cash it does not yet hold.
        With ``order_caps`` no buy is sized past A8's per-order ceiling, for the same reason.
        ``held`` is the share count already owned per ISIN; ``prices`` overrides the records' own
        (the redeploy pass sizes at this session's marks), and ``note`` prefixes each rationale.
        """
        if not target:
            return [], _ZERO
        budget = ctx.broker.margins().available * self._params.buy_budget_fraction
        if prices is None:
            prices = {isin: record.price for isin, record in target.items()}
        weights = _equal_weights(sorted(target))
        existing_value = {
            isin: Decimal(held[isin]) * prices[isin] for isin in target if isin in held
        }
        allocation = simulate_sip_instalment(
            instalment=budget,
            targets=weights,
            prices=prices,
            existing_value=existing_value,
            min_order_value=MIN_ORDER_VALUE_INR,
            # A lot is marked at its signal mark, else this session's target price, else cost.
            order_ceiling=account_order_ceiling(
                self._order_caps, ctx.broker, ChainMap(dict(marks), dict(prices))
            ),
        )
        buys = [
            (
                order.to_order_request(exchange=Exchange.NSE, tag="SWING"),
                (f"{note}; " if note is not None else "")
                + f"{self._basket_label(order.isin, sleeve_of)}: 52w-high proximity "
                f"{target[order.isin].high_proximity}, delivery "
                f"{target[order.isin].delivery_share}, 12-1 "
                f"{target[order.isin].momentum_12_1:+}; buy {order.quantity} @ {order.price}",
            )
            for order in allocation.orders
        ]
        return buys, allocation.tracking_drift

    def _basket_label(self, isin: str, sleeve_of: Mapping[str, TierSleeve] | None) -> str:
        if sleeve_of is None or isin not in sleeve_of:
            return f"composite top-{self._params.top_n}"
        sleeve = sleeve_of[isin]
        return f"{sleeve.tier.value}-tier (liquidity rank) composite top-{sleeve.top_n}"

    # ── journal + evidence ───────────────────────────────────────────────────────────────────────

    def _session_decision(
        self, ctx: SessionContext, sells: Sequence[tuple[OrderRequest, str]], *, note: str
    ) -> SessionDecision:
        """A non-rebalance session: the heartbeat, plus any stop-outs it fired."""
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="held_names",
                    as_of=ctx.session,
                    value=Decimal(len(self._positions)),
                    text=f"{note}; {len(sells)} trailing-stop exit(s)",
                ),
            ),
        )
        orders = tuple(order for order, _ in sells)
        entries = tuple(self._entry(ctx, order, note) for order, note in sells)
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _entry(self, ctx: SessionContext, order: OrderRequest, rationale: str) -> JournalEntry:
        """A BUY/SELL journal entry for one order — rationale, sleeve and ISIN, per §0/§5.7."""
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

    def _evidence(
        self,
        session: date,
        chosen: Sequence[SwingRecord],
        scores: Mapping[str, Decimal],
        by_isin: Mapping[str, SwingRecord],
        tracking_drift: Decimal,
        universe_size: int,
        sleeve_of: Mapping[str, TierSleeve] | None = None,
    ) -> EvidenceBundle:
        """The scored table the decision was made on, as one content-addressed bundle."""
        items = [
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="L1+L2",
                label="swing_composite",
                isin=record.isin,
                as_of=session,
                value=scores[record.isin],
                detail={
                    "rank": str(rank),
                    "high_proximity": str(record.high_proximity),
                    "delivery_share": str(record.delivery_share),
                    "momentum_12_1": str(record.momentum_12_1),
                    "price": str(record.price),
                    # Only on an arm that weights it, so every other arm's journal is unchanged.
                    **(
                        {"residual_momentum": str(record.residual_momentum)}
                        if self._params.weight_residual_momentum != _ZERO
                        else {}
                    ),
                    # Only on a tiered arm, for the same reason.
                    **(
                        {"tier": sleeve_of[record.isin].tier.value} if sleeve_of is not None else {}
                    ),
                },
            )
            for rank, record in enumerate(chosen, start=1)
        ]
        items.append(
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="book",
                label="tracking_drift",
                as_of=session,
                value=tracking_drift,
                text=f"equal-weight top-{self._params.top_n} of {universe_size} scored candidates"
                + self._gated_note(by_isin),
            )
        )
        return EvidenceBundle(trading_date=session, actor=Actor.T0, items=tuple(items))

    def _gated_note(self, by_isin: Mapping[str, SwingRecord]) -> str:
        """Which weighted delivery legs the coverage gate dropped this session, for the journal."""
        records = tuple(by_isin.values())
        active = {attribute for attribute, _ in active_legs(records, self._params)}
        gated = [
            f"{attribute} ({delivery_coverage(records, attribute):.1%} covered)"
            for attribute, _ in _weighted_legs(self._params)
            if attribute in _DELIVERY_LEGS and attribute not in active
        ]
        if not gated:
            return ""
        return (
            f"; below the {DELIVERY_COVERAGE_THRESHOLD:.0%} delivery-coverage floor, not scored: "
            + ", ".join(gated)
        )


def _equal_weights(isins: Sequence[str]) -> dict[str, Decimal]:
    """Equal weights over ``isins`` that sum to exactly 1 — the remainder lands on the last name."""
    if not isins:
        raise ValueError("cannot weight an empty basket")
    n = len(isins)
    each = (_ONE / Decimal(n)).quantize(_WEIGHT_QUANTUM)
    weights = dict.fromkeys(isins[:-1], each)
    weights[isins[-1]] = _ONE - each * (n - 1)
    return weights
