"""D3: price-implied splits — the share-basis changes no corporate-action feed ever published.

Both CA feeds the platform reads (NSE `corporates-corporateActions?index=equities`, BSE's
corporate-action list) cover listed *equity*. Neither carries a mutual-fund unit split, so an
ETF's 1:10 or 1:100 unit sub-division is in no feed at all: GOLDBEES (INF732E01102) closed at
3,359.60 on 2019-12-18 and 33.55 on 2019-12-19, and with no factor behind that step the L2
adjusted series showed a -99% day. Measured on the server lake 2026-10-05: 34 of the 96
short-gap >2x adjusted-close steps were ETF/fund unit splits, and a further handful were equity
splits from before 2016 that neither feed's history reaches (TATAMTRDVR's 2011-09-12 5:1, a
`IN9` DVR ISIN). This module recognises such a step from L1 itself, so L2 can adjust it.

**What it does.** Scans one venue's bars — already multiplied by the recorded factor chain, so a
recorded split never shows as a step here — for a session-to-session level change that only a
change in the share basis explains. Every one of these must hold, or nothing is implied:

* **Short gap.** The two bars are at most `max_gap_days` calendar days apart. Across a suspension
  or a months-long stretch in the trade-to-trade series a large move can be genuine; those are
  classified by the quality check, never adjusted here.
* **A clean multiple.** The pre-step close over the ex-day open, or over the ex-day close, sits
  within a tight tolerance of one of `CANDIDATE_MULTIPLES` (2, 2.5, 3, 4, 5, 10, 20, 25, 50, 100)
  and the other of the two ratios agrees with it loosely, so a single bad print cannot make one.
  An Indian equity's daily band is 20% at most, so a halving inside five days with no recorded
  event is not a market move.
* **Volume moved with the basis.** On a sub-division the share count — and so the traded
  quantity — rises by the multiple; on a consolidation it falls. The ex-day quantity against the
  median of the preceding sessions' must move the same way, by at least a quarter of the multiple
  (and by half again at the least, for a sub-division).
  A demerger or a crash halves the price without multiplying the volume.
* **Not a tick bounce.** Both raw closes are at least ₹1, and the level does not return within
  five sessions: a penny name moving ₹0.05 ↔ ₹0.10 is "exactly 2x" with no change in the basis.
* **Nothing recorded explains it.** No structural break (merger, demerger, scheme, DVR) and no
  price event on a *neighbouring* date within `guard_days`: the first is a real change in what the
  security is, the second a recorded split whose ex-date disagrees with L1 by a day or two — adding
  an implied twin there would adjust the same event twice. Those reach a human through the quality
  check instead. A recorded event on the *same* day does not block: the step left after it is
  applied is what is measured, which is how a 1:25 bonus that came with an unpublished 1:2 split
  (INE096L01025, 2016-08-11) is caught.

**What it assumes.** The bars are one ISIN's, one venue's, in trade-date order, already multiplied
by the recorded chain's cumulative price factor (so the recorded events are not re-detected).

**What it never does.** Read a database, a clock or the network; persist anything (the caller —
the L2 materializer — carries the implied events into the partition it writes, where
`cum_price_factor` shows them); or imply an event from a single price without the volume moving
with it. An implied split is a derived fact with its evidence attached (`ImpliedSplit`), labelled
with its own source id, never passed off as a feed's.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import TYPE_CHECKING, Final

from dataplatform.corpactions.factors import PRICE_EVENT_TYPES, STRUCTURAL_BREAK_TYPES
from dataplatform.corpactions.taxonomy import ActionType, DividendTerms, FaceValueTerms

if TYPE_CHECKING:
    from dataplatform.ingest.corp_actions import CorporateAction

__all__ = [
    "CANDIDATE_MULTIPLES",
    "IMPLIED_SOURCE",
    "ImpliedSplit",
    "SessionBar",
    "detect_implied_splits",
]

#: The source id an implied action carries — never a feed's, so it can never be mistaken for one.
IMPLIED_SOURCE: Final = "l1_price_implied"

#: Share multiples a sub-division (or, inverted, a consolidation) is recognised at, each as the
#: `(from, to)` face-value pair `FaceValueTerms` states it with: 2.5 is a ₹5 → ₹2 split.
CANDIDATE_MULTIPLES: Final[tuple[tuple[Decimal, Decimal], ...]] = tuple(
    (Decimal(a), Decimal(b))
    for a, b in (
        (2, 1),
        (5, 2),
        (3, 1),
        (4, 1),
        (5, 1),
        (10, 1),
        (20, 1),
        (25, 1),
        (50, 1),
        (100, 1),
    )
)

#: How far the better of the two price ratios may sit from the multiple. Tighter for the small
#: multiples, where a genuine large move is the nearer confusion; 6% for 5x and above, where the
#: ex-day's own move (TATAMTRDVR fell 5% on its 2011 split day) is the only noise.
_TIGHT_TOLERANCE: Final = Decimal("0.03")
_WIDE_TOLERANCE: Final = Decimal("0.06")
_WIDE_FROM: Final = Decimal(5)

#: How far the *other* ratio may sit: an illiquid ETF's ex-day close can drift well off its open
#: (HNGSNGBEES opened at exactly 1/10 of the prior close and closed 12% above it).
_LOOSE_TOLERANCE: Final = Decimal("0.20")

#: Sessions of volume history the ex-day quantity is compared against.
_VOLUME_LOOKBACK: Final = 20

#: The least ex-day volume surge a sub-division needs whatever its multiple: at 2x the price
#: evidence alone is nearest to a genuine halving, so an unchanged volume is not enough.
_MIN_SURGE: Final = Decimal("1.5")

#: The least raw price either side of a step may trade at. Below it the price moves in whole
#: ticks — a ₹0.05 ↔ ₹0.10 bounce is exactly 2x every time, with no change in the share basis
#: (measured: 70 such "splits" on six penny names before this floor).
_MIN_RAW_PRICE: Final = Decimal(1)

#: Sessions after the ex-day a genuine basis change must not revert in: a level that is back
#: within the loose tolerance of the pre-step close this soon was a bad print or a tick bounce.
_REVERSAL_LOOKAHEAD: Final = 5

#: Dividends at least this fraction of the prior close are a recorded explanation for a step
#: (Majesco's ₹974 on a ₹985 share, 2020-12-23); smaller ones explain nothing at a 2x scale.
_LARGE_DIVIDEND: Final = Decimal("0.25")


@dataclass(frozen=True, slots=True)
class SessionBar:
    """One session of one venue's series, in the recorded chain's adjusted terms.

    `open`/`close` are the raw prices times the recorded cumulative price factor; `volume` is the
    raw traded quantity times the recorded cumulative quantity factor. Zero volume is allowed.
    `raw_close` is the untouched L1 close (it defaults to `close`), for the tick-size floor.
    """

    trade_date: date
    open: Decimal
    close: Decimal
    volume: Decimal
    raw_close: Decimal | None = None

    @property
    def traded_close(self) -> Decimal:
        return self.close if self.raw_close is None else self.raw_close


@dataclass(frozen=True, slots=True)
class ImpliedSplit:
    """A share-basis change read off L1, with the evidence it was read from.

    `from_value`/`to_value` state it as `FaceValueTerms` do (`10 → 1` is a 1:10 sub-division,
    `1 → 10` a 10:1 consolidation); the face values themselves are unknown and these are the
    ratio only. `close_ratio`/`open_ratio` are the prior close over the ex-day close/open, and
    `volume_ratio` the ex-day quantity over the prior sessions' median.
    """

    isin: str
    ex_date: date
    from_value: Decimal
    to_value: Decimal
    close_ratio: Decimal
    open_ratio: Decimal
    volume_ratio: Decimal | None

    @property
    def price_factor(self) -> Decimal:
        """The factor this event scales earlier prices by — `to / from`, the SPLIT convention."""
        return self.to_value / self.from_value

    def as_action(self, *, knowable_date: date | None = None) -> CorporateAction:
        """This event as a SPLIT `CorporateAction` under `IMPLIED_SOURCE`, for the factor math.

        `knowable_date` defaults to the ex-date: the ex-day bar is what reveals the event, so it is
        knowable no earlier than the session it happened in (invariant #7).
        """
        from dataplatform.ingest.corp_actions import CorporateAction

        evidence = (
            f"implied from L1: prior close / ex-day close {self.close_ratio:.4f}, "
            f"/ ex-day open {self.open_ratio:.4f}, volume x"
            f"{'n/a' if self.volume_ratio is None else f'{self.volume_ratio:.2f}'}; "
            f"no feed published this event"
        )
        return CorporateAction(
            isin=self.isin,
            ex_date=self.ex_date,
            action_type=ActionType.SPLIT,
            terms=FaceValueTerms(from_value=self.from_value, to_value=self.to_value),
            source=IMPLIED_SOURCE,
            raw_text=evidence,
            knowable_date=self.ex_date if knowable_date is None else knowable_date,
        )


def detect_implied_splits(
    isin: str,
    bars: Sequence[SessionBar],
    recorded: Iterable[CorporateAction] = (),
    *,
    max_gap_days: int = 5,
    guard_days: int = 7,
) -> tuple[ImpliedSplit, ...]:
    """Every share-basis change in one venue's series that no recorded action explains.

    What it does: walks consecutive bars and returns an `ImpliedSplit` for each step that passes
    every test in the module docstring — short gap, clean multiple on both price ratios, volume
    moved with the basis, nothing recorded nearby — in ex-date order.

    What it assumes: `bars` are one venue's sessions for `isin`, sorted by trade date, already in
    the recorded chain's terms; `recorded` are the ISIN's reconciled actions.

    What it never does: imply a step across a gap longer than `max_gap_days`, beside a recorded
    structural break, large dividend or a recorded price event on a neighbouring date, or without
    the traded quantity moving with it.
    """
    blocked, neighbouring = _recorded_guards(recorded, bars)
    guard = timedelta(days=guard_days)
    out: list[ImpliedSplit] = []
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1], bars[i]
        if (cur.trade_date - prev.trade_date).days > max_gap_days:
            continue
        if prev.close <= 0 or cur.close <= 0 or cur.open <= 0:
            continue
        close_ratio = prev.close / cur.close
        open_ratio = prev.close / cur.open
        match = _match_multiple(close_ratio, open_ratio)
        if match is None:
            continue
        from_value, to_value = match
        if min(prev.traded_close, cur.traded_close) < _MIN_RAW_PRICE:
            continue
        if _reverts(bars, i):
            continue
        if any(abs((cur.trade_date - d).days) <= guard_days for d in blocked):
            continue
        if any(d != cur.trade_date and abs(cur.trade_date - d) <= guard for d in neighbouring):
            continue
        volume_ratio = _volume_ratio(bars, i)
        if not _volume_moved(volume_ratio, from_value / to_value):
            continue
        out.append(
            ImpliedSplit(
                isin=isin,
                ex_date=cur.trade_date,
                from_value=from_value,
                to_value=to_value,
                close_ratio=close_ratio,
                open_ratio=open_ratio,
                volume_ratio=volume_ratio,
            )
        )
    return tuple(out)


def _match_multiple(close_ratio: Decimal, open_ratio: Decimal) -> tuple[Decimal, Decimal] | None:
    """The `(from, to)` pair both ratios agree on, or `None` — a split if >1, else consolidation."""
    for from_value, to_value in CANDIDATE_MULTIPLES:
        multiple = from_value / to_value
        tight = _WIDE_TOLERANCE if multiple >= _WIDE_FROM else _TIGHT_TOLERANCE
        split = (multiple, (from_value, to_value))
        consolidation = (1 / multiple, (to_value, from_value))
        for target, pair in (split, consolidation):
            near = sorted((abs(close_ratio / target - 1), abs(open_ratio / target - 1)))
            if near[0] <= tight and near[1] <= _LOOSE_TOLERANCE:
                return pair
    return None


def _reverts(bars: Sequence[SessionBar], i: int) -> bool:
    """Whether a close in the next sessions after bar `i` is back at the old level."""
    before = bars[i - 1].close
    for later in bars[i + 1 : i + 1 + _REVERSAL_LOOKAHEAD]:
        if abs(later.close / before - 1) <= _LOOSE_TOLERANCE:
            return True
    return False


def _volume_ratio(bars: Sequence[SessionBar], i: int) -> Decimal | None:
    """Ex-day quantity over the median traded quantity of up to `_VOLUME_LOOKBACK` prior sessions.

    `None` when no prior session traded at all — there is then no baseline to move against.
    """
    prior = [b.volume for b in bars[max(0, i - _VOLUME_LOOKBACK) : i] if b.volume > 0]
    if not prior:
        return None
    return bars[i].volume / Decimal(median(prior))


def _volume_moved(ratio: Decimal | None, multiple: Decimal) -> bool:
    """Whether the traded quantity moved with a share-basis change of `multiple` (>1 sub-division).

    A sub-division multiplies the share count, so the ex-day quantity must be at least
    `max(1.5, multiple / 4)` times its baseline; a consolidation divides it, so at most
    `min(1, 4 x multiple)`. No baseline is no evidence, and no evidence implies nothing.
    """
    if ratio is None:
        return False
    if multiple > 1:
        return ratio >= max(_MIN_SURGE, multiple / 4)
    return ratio <= min(Decimal(1), 4 * multiple)


def _recorded_guards(
    recorded: Iterable[CorporateAction], bars: Sequence[SessionBar]
) -> tuple[frozenset[date], frozenset[date]]:
    """`(blocking dates, neighbouring price-event dates)` from the recorded actions.

    Blocking: a structural break, or a dividend at least `_LARGE_DIVIDEND` of the close before it —
    a recorded explanation for a large step. Neighbouring: a recorded split/bonus, which blocks an
    implied event on any *other* date within the guard (a one-day ex-date disagreement) but not on
    its own date (the residual there is a second, unpublished event).
    """
    closes = [(b.trade_date, b.close) for b in bars]
    blocked: set[date] = set()
    neighbouring: set[date] = set()
    for action in recorded:
        if action.action_type in STRUCTURAL_BREAK_TYPES:
            blocked.add(action.ex_date)
        elif action.action_type in PRICE_EVENT_TYPES:
            neighbouring.add(action.ex_date)
        elif action.action_type is ActionType.DIVIDEND and isinstance(action.terms, DividendTerms):
            amount = action.terms.amount_inr
            before = [c for d, c in closes if d < action.ex_date]
            if amount is not None and before and amount >= _LARGE_DIVIDEND * before[-1]:
                blocked.add(action.ex_date)
    return frozenset(blocked), frozenset(neighbouring)
