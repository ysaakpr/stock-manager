"""X2: where a run's idle cash came from — measured from its saved ledger and NAV, never replayed.

The cap-tier report (``ops/gates/cap-tiers-2026-10-05.md``) first put every arm's high cash share
down to the ₹1.2 lakh per-order cap. An audit of the saved runs showed that was the secondary
cause. This module is that audit, kept as code so the report states the measured split instead
of a guess. Cash on each NAV session is rebuilt from the ledger: external deposits, every fill's
net cash, dividends and interest, each counted on its date. That cash is then split three ways:

* **waiting proceeds** — sale cash received on or after the run's last buy session. The policies
  size a buy from cash that is already free, never from that session's own sale proceeds (they
  have not settled). So a rotation's sales, like a stop-out between rebalances, sit idle until
  the next decision session buys. Capped at the cash on hand, so it never exceeds what is there.
* **ceiling-bound leftover** — the rest of the cash, when the last buy session had at least one
  buy at the per-order ceiling (``analyst.rails.order_value_ceiling``: the smaller of the rupee
  cap and the percentage of the case). That buy was cut short, so the shortfall stayed in cash.
* **other leftover** — the rest, on any other session: whole-share rounding, the buy budget's
  margin, a basket with fewer candidates than slots, dividends, interest, and the cash held
  before the run's first buy.

A buy is **at the ceiling** when one more share at its own price would have gone over the ceiling
(``net + net / quantity > ceiling``). The ceiling is computed from the NAV on the last NAV session
before the fill date. That session is the decision session, and its close is the mark the policy
sized against. The net amount includes the buy's charges, so a buy within one share plus charges
of the ceiling also counts. That is the resolution a ledger without fill prices allows.

**Buy-free spans.** An arm whose tier is empty does not trade: no candidates means no buys, and
the book stays in cash. The ledger cannot see candidates, so buys stand in for them. A decision
session is credited with a buy when a buy fills after it and on or before the next decision
session (a decision fills on the next session). The longest run of uncredited decision sessions
is the arm's longest buy-free span. A span longer than :data:`EMPTY_TIER_DECISIONS` is flagged.

What this module never does: replay a run, change a book, or round money through a float.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from analyst.cases import RiskRails
from analyst.rails import order_value_ceiling
from backtest.tax import RunLedger
from backtest.xirr import Cashflow, XIRRError, xirr
from execution.broker import Side

__all__ = [
    "EMPTY_TIER_DECISIONS",
    "BuyFreeSpan",
    "IdleCash",
    "IdleCashError",
    "ceiling_buys",
    "idle_cash",
    "longest_buy_free_span",
    "xirr_from_first_buy",
]

#: The most consecutive decision sessions an arm may go without a buy before the report flags it
#: as an empty tier. 25 is one trading year at the swing cadence of 10 sessions. It sits above
#: every buy-free span a regime gate produced in the 2026-10-05 cap-tier runs: the longest was 17
#: decision sessions (M10.7 + regime gate, from 2015-08-13), and Momentum v2's regime filter
#: managed 9 monthly decisions. A flag therefore means the arm had nothing to buy, not that a gate
#: kept it out. A year is also where the gap starts to matter: an annualised headline over the
#: window has then absorbed at least a full year of cash. Every arm's longest span is printed, so a
#: shorter gap below the flag can still be seen.
EMPTY_TIER_DECISIONS: Final = 25

_ZERO = Decimal(0)


class IdleCashError(ValueError):
    """The ledger and NAV do not describe one run, so the cash cannot be split."""


@dataclass(frozen=True, slots=True)
class IdleCash:
    """A run's cash, averaged over its NAV sessions, split by cause; and its ceiling-bound buys.

    Every ``mean_*_share`` is the mean, over NAV sessions, of that cash divided by the session's
    NAV. The three parts add up to ``mean_cash_share`` exactly.
    """

    sessions: int
    mean_cash_share: Decimal
    mean_waiting_share: Decimal
    mean_ceiling_share: Decimal
    mean_other_share: Decimal
    buys: int
    ceiling_buys: int

    @property
    def waiting_share_of_cash(self) -> Decimal | None:
        """The share of the average cash that was waiting proceeds (None when there was no cash)."""
        if not self.mean_cash_share:
            return None
        return self.mean_waiting_share / self.mean_cash_share


@dataclass(frozen=True, slots=True)
class BuyFreeSpan:
    """A run of consecutive decision sessions with no buy, from ``start`` (the first of them)."""

    decisions: int
    start: date
    #: The fill date of the buy that ended the span, or None if no buy came before the run ended.
    next_buy: date | None

    @property
    def flagged(self) -> bool:
        """Whether the span is longer than :data:`EMPTY_TIER_DECISIONS` (an empty tier)."""
        return self.decisions > EMPTY_TIER_DECISIONS


def _nav_before(nav: Sequence[tuple[date, Decimal]], when: date) -> Decimal | None:
    """The NAV on the last NAV session strictly before ``when``, or None if there is none."""
    index = bisect.bisect_left([d for d, _ in nav], when) - 1
    return nav[index][1] if index >= 0 else None


def ceiling_buys(
    ledger: RunLedger, nav: Sequence[tuple[date, Decimal]], *, rails: RiskRails
) -> frozenset[int]:
    """Indices into ``ledger.trades`` of every buy at the per-order ceiling (module docstring).

    Assumes ``nav`` is the run's own dated NAV path, ascending. A buy filled before any NAV session
    is checked against the rupee cap alone, the ceiling ``order_value_ceiling`` gives a case with
    no value yet.
    """
    dates = [d for d, _ in nav]
    out: set[int] = set()
    for index, trade in enumerate(ledger.trades):
        if trade.side is not Side.BUY:
            continue
        position = bisect.bisect_left(dates, trade.trade_date) - 1
        case_value = nav[position][1] if position >= 0 else _ZERO
        ceiling = order_value_ceiling(rails, case_value)
        if trade.net_amount + trade.net_amount / trade.quantity > ceiling:
            out.add(index)
    return frozenset(out)


def idle_cash(
    ledger: RunLedger, nav: Sequence[tuple[date, Decimal]], *, rails: RiskRails
) -> IdleCash:
    """Rebuild a run's daily cash from its ledger and split it by cause (module docstring).

    Assumes ``nav`` is the run's own pre-tax NAV path, ascending, every value positive. Raises
    ``IdleCashError`` when the rebuilt cash goes negative or ends above the terminal NAV. Either
    means the ledger and the path are not one run, and a split of them would be fiction.
    """
    if not nav:
        raise IdleCashError("no NAV sessions to measure cash on")
    at_ceiling = ceiling_buys(ledger, nav, rails=rails)
    # (date, cash delta, is-sale, trade index or -1); external flows are XIRR-signed (deposit < 0).
    events: list[tuple[date, Decimal, bool, int]] = [
        (flow.when, -flow.amount, False, -1) for flow in ledger.external_flows
    ]
    for index, trade in enumerate(ledger.trades):
        buy = trade.side is Side.BUY
        events.append(
            (trade.trade_date, -trade.net_amount if buy else trade.net_amount, not buy, index)
        )
    events += [(d.received, d.amount, False, -1) for d in ledger.dividends]
    events += [(i.received, i.amount, False, -1) for i in ledger.interest]
    events.sort(key=lambda event: event[0])

    cash = waiting = _ZERO
    last_buy_hit_ceiling = False
    total_cash = total_waiting = total_ceiling = total_other = _ZERO
    cursor = 0
    for session, value in nav:
        while cursor < len(events) and events[cursor][0] <= session:
            day = events[cursor][0]
            day_buys: list[int] = []
            day_sales = _ZERO
            while cursor < len(events) and events[cursor][0] == day:
                _, delta, sale, index = events[cursor]
                cash += delta
                if sale:
                    day_sales += delta
                elif index >= 0:
                    day_buys.append(index)
                cursor += 1
            if day_buys:
                # A buy is sized from cash already free, never from its own session's sale
                # proceeds (``SwingComposite._buys``): those wait a full cycle too.
                waiting = day_sales
                last_buy_hit_ceiling = any(index in at_ceiling for index in day_buys)
            else:
                waiting += day_sales
        if cash < _ZERO:
            raise IdleCashError(f"rebuilt cash {cash} is negative on {session}")
        held_waiting = min(waiting, cash)
        leftover = cash - held_waiting
        total_cash += cash / value
        total_waiting += held_waiting / value
        if last_buy_hit_ceiling:
            total_ceiling += leftover / value
        else:
            total_other += leftover / value
    if cash > ledger.terminal_nav:
        raise IdleCashError(f"rebuilt cash {cash} exceeds the terminal NAV {ledger.terminal_nav}")
    count = Decimal(len(nav))
    return IdleCash(
        sessions=len(nav),
        mean_cash_share=total_cash / count,
        mean_waiting_share=total_waiting / count,
        mean_ceiling_share=total_ceiling / count,
        mean_other_share=total_other / count,
        buys=sum(1 for t in ledger.trades if t.side is Side.BUY),
        ceiling_buys=len(at_ceiling),
    )


def longest_buy_free_span(
    decisions: Sequence[date], buy_dates: Sequence[date]
) -> BuyFreeSpan | None:
    """The longest run of decision sessions not credited with a buy, or None if every one was.

    A decision session is credited when a buy fills after it and on or before the next decision
    session. A buy on or before the first decision credits nothing. Ties go to the earliest span.
    """
    ordered = sorted(decisions)
    credited = [False] * len(ordered)
    for when in buy_dates:
        index = bisect.bisect_left(ordered, when) - 1
        if index >= 0:
            credited[index] = True
    best: tuple[int, int] | None = None  # (length, start index)
    run_start, run = 0, 0
    for index, hit in enumerate(credited):
        if hit:
            run = 0
            continue
        if run == 0:
            run_start = index
        run += 1
        if best is None or run > best[0]:
            best = (run, run_start)
    if best is None:
        return None
    length, start = best
    start_date = ordered[start]
    after = sorted(d for d in buy_dates if d > start_date)
    return BuyFreeSpan(decisions=length, start=start_date, next_buy=after[0] if after else None)


def xirr_from_first_buy(
    ledger: RunLedger, nav: Sequence[tuple[date, Decimal]]
) -> tuple[date, Decimal] | None:
    """The run's XIRR measured from its first populated session, with that session's date.

    The start is the last NAV session before the first buy's fill: the decision session, with the
    book all cash, invested at its NAV. External flows after it count, and the terminal NAV is the
    final inflow. This number is for information only. It measures what the arm did once it had
    something to hold, and it is not the arm's return: the investor's money was in the run from
    the window's start. None when the run never bought, or bought before any NAV session.
    """
    buys = [t.trade_date for t in ledger.trades if t.side is Side.BUY]
    if not buys:
        return None
    first = min(buys)
    index = bisect.bisect_left([d for d, _ in nav], first) - 1
    if index < 0:
        return None
    start, value = nav[index]
    flows = [Cashflow(start, -value)]
    flows += [flow for flow in ledger.external_flows if flow.when > start]
    flows.append(Cashflow(ledger.terminal_date, ledger.terminal_nav))
    try:
        return start, xirr(flows)
    except XIRRError:
        return None
