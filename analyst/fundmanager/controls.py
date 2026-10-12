"""A10 · M17.6 / M17.14 — the no-LLM comparison books: ``CTRL-<book>``, ``STYLE-<manager>`` and
``BENCH-N500`` (§3, §8 Amendment 2 d).

**``CTRL-<book>``** holds equal weight across the top *N* names of the composite shortlist every
manager receives (*N* = the book's max positions, 15), rebalanced weekly for a swing manager's
book and every 21 sessions for a positional one's. There is one per manager book — the primary
₹10 L and the mirror ₹1 cr alike — each on its book's capital. It is a `FundBook` like the book it
controls — same rails, same paper account, same cost model, same capital — so every order it
places clears the M17 rails and every refusal is journaled. No model is called anywhere on this
path.

**``STYLE-<manager>``** (Amendment 2 d, **secondary**: reported, never used for pass/fail) runs the
very same rule on a different ranked list — up to 15 names from its manager's starting screens
(`style_candidates`): BRK S2 then S3, S2 first; EVT S4; TREND S1; FUND the S4 names passing the
playbook's quality filter (`passes_quality`: debt-equity below 1.5 outside financials, and a
positive TTM profit). Each target is still ``0.98 x mark / 15``, so a screen with fewer names
leaves the rest in cash, and an empty screen means cash.

The control's rule, fixed here a priori:

- **Due.** Weekly: the first session the control runs in an ISO week other than its last
  rebalance's. Every 21 sessions: 21 or more sessions after its last rebalance. The first session
  it runs is always a rebalance. A due rebalance with no shortlist for the session (the Commons
  failed) is not done; it stays due, and the book holds.
- **Targets.** The shortlist's first *N* entries by position. Each target is worth
  ``CONTROL_BUY_BUDGET_FRACTION`` times the book's mark at the close, divided by *N*: the 0.98
  every backtest's equal-weight buy budget uses (``backtest.policies.naive_momentum``), leaving
  room for costs and the next open's slippage.
- **Orders.** A held name that left the targets is sold in full. A held target more than
  ``CONTROL_REBALANCE_BAND`` (25 %) above its target value is trimmed to it; one more than 25 %
  below is topped up. Inside the band it is left alone, so the control does not pay a round trip
  on a few rupees of drift. A new target is bought.
- **Pending buys.** A buy the book cannot place yet — the sell that frees its slot or its cash has
  not filled and settled, or there is no close to size it — waits as *pending* and is retried on
  each following session, in shortlist order, until the next rebalance replaces the list. A buy is
  only offered to the rails if it fits the book's free name slots and 0.98 of its spendable cash,
  and its size is capped at the participation limit; a buy the rails still refuse is journaled
  ``RAIL_BLOCK`` and dropped until the next rebalance, never resized.

**``BENCH-N500``** is buy-and-hold of the backtests' NIFTY 500 TRI proxy from S0: the roster's
``benchmark`` label ``nifty500-tri-proxy`` resolves to the ``nifty500`` series of
``L1/benchmark_tri``, read through ``read_tri_series`` — the reader the backtests'
``_resolve_benchmark`` uses — in the same preference order: the exchange's published series, then
§4.1's computed estimate (``computed_price_plus_div``). The backtests' last resort, the L1 basket,
is not NIFTY 500 and is never used. The method is fixed when the bench opens and every later level
must come from it, so a series published mid-window cannot splice into the bench. The bench is
bought at the level of the close before S0 (the last level knowable at the S0 open) and marked at
``capital * level / base`` every close.

What this module never does: call a model, read a manager's book or decisions, resize an order the
rails refused, or read a level dated after the session it marks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path
from typing import Any, Final, Protocol

from analyst.commons.dossier import Dossier
from analyst.commons.screens import CommonsScreens, Screen
from analyst.commons.shortlist import Shortlist
from analyst.fundmanager.books import (
    PAPER_MODE,
    BookError,
    BookOrder,
    DecisionReport,
    FundBook,
    median_traded_value,
)
from analyst.fundmanager.mandate import (
    BenchMandate,
    ControlMandate,
    M17Rails,
    ManagerStyle,
    RebalanceUnit,
    StyleMandate,
)
from analyst.fundmanager.scoreboard import (
    CONTROL_REBALANCE_EVENT,
    BookMark,
    ControlBuy,
    mark_entry,
)
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry
from dataplatform.clock import Clock
from dataplatform.ingest.indices import TriSeries, read_tri_series
from dataplatform.logging import get_logger
from execution.broker import Side

__all__ = [
    "BENCHMARK_INDEX_SLUGS",
    "CONTROL_BUY_BUDGET_FRACTION",
    "CONTROL_REBALANCE_BAND",
    "FINANCIAL_SECTORS",
    "FUND_MAX_DEBT_EQUITY",
    "STYLE_RULE_VERSION",
    "BenchBook",
    "BenchJournal",
    "BenchState",
    "BenchmarkLevels",
    "BenchmarkUnavailableError",
    "ControlBook",
    "ControlError",
    "ControlSession",
    "ControlState",
    "LakeTriBenchmark",
    "QualityFacts",
    "RankedTargets",
    "passes_quality",
    "quality_from_dossier",
    "rebalance_due",
    "record_mark",
    "shortlist_targets",
    "style_candidates",
    "style_rule_bytes",
]

_LOG = get_logger(__name__)

#: The share of the book's mark a rebalance spreads across the targets (the backtests' 0.98).
CONTROL_BUY_BUDGET_FRACTION: Final = Decimal("0.98")
#: A held target is traded only when it is this far (as a fraction of its target) off target.
CONTROL_REBALANCE_BAND: Final = Decimal("0.25")
#: Amendment 2 (d) / the FUND playbook's quality filter: debt-equity strictly below this, outside
#: financials (the industries in `FINANCIAL_SECTORS`, NSE's classification, where leverage is the
#: business and the ratio says nothing about crash risk).
FUND_MAX_DEBT_EQUITY: Final = Decimal("1.5")
FINANCIAL_SECTORS: Final = frozenset({"Financial Services"})
#: The style books' selection rule, versioned: part of every style book's `mandate_hash`.
STYLE_RULE_VERSION: Final = "m17-style-books/1"
#: The roster's benchmark label → the ``L1/benchmark_tri`` index slug it names.
BENCHMARK_INDEX_SLUGS: Final[Mapping[str, str]] = {"nifty500-tri-proxy": "nifty500"}

_CONTEXT: Final = Context(prec=34, rounding=ROUND_HALF_EVEN)
_PAISE: Final = Decimal("0.01")
_RUPEE_6: Final = Decimal("0.000001")
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_HUNDRED: Final = Decimal(100)


class ControlError(BookError):
    """A control book cannot run its rule as stated — always loud."""


class BenchmarkUnavailableError(ControlError):
    """The bench's index series is not in the lake, or has no level for the session asked."""


# ── what a control or style book ranks ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RankedTargets:
    """One session's ranked list a control or style book equal-weights the top of.

    ``isins`` is in rank order, best first, without repeats; ``source`` and ``digest_label`` name
    the Commons build it came from and ``digest`` that build's digest, journaled with each
    rebalance; ``rule`` says how the list was cut from the build.
    """

    trading_date: date
    isins: tuple[str, ...]
    source: str
    digest_label: str
    digest: str
    rule: str

    def __post_init__(self) -> None:
        if len(set(self.isins)) != len(self.isins):
            raise ControlError(f"a ranked list repeats a name: {self.isins}")


def shortlist_targets(shortlist: Shortlist) -> RankedTargets:
    """The composite shortlist as a control's ranked list (verified first)."""
    shortlist.verify()
    return RankedTargets(
        trading_date=shortlist.trading_date,
        isins=tuple(e.isin for e in sorted(shortlist.entries, key=lambda e: e.position)),
        source="commons_shortlist",
        digest_label="shortlist_digest",
        digest=shortlist.shortlist_digest,
        rule="composite shortlist, by position",
    )


@dataclass(frozen=True, slots=True)
class QualityFacts:
    """What the FUND quality filter reads of one name: its dossier's fields (fractions)."""

    isin: str
    sector: str | None
    debt_equity: Decimal | None
    net_margin_ttm: Decimal | None
    pe_ttm: Decimal | None


def quality_from_dossier(dossier: Dossier) -> QualityFacts:
    """The quality facts of one dossier (a field of the wrong type reads as unknown)."""

    def number(name: str) -> Decimal | None:
        value = dossier.fields.get(name)
        return value if isinstance(value, Decimal) else None

    sector = dossier.fields.get("sector")
    return QualityFacts(
        isin=dossier.isin,
        sector=sector if isinstance(sector, str) else None,
        debt_equity=number("debt_equity"),
        net_margin_ttm=number("net_margin_ttm"),
        pe_ttm=number("pe_ttm"),
    )


def passes_quality(facts: QualityFacts) -> bool:
    """The FUND playbook's quality filter, as the FUND style book applies it.

    - **Positive TTM profit.** The lake states TTM PAT only through ratios whose sign is its sign:
      the TTM net margin (PAT over positive revenue) and the TTM P/E (positive market cap over
      PAT, unknown for a non-positive PAT). Either one positive passes; neither known fails.
    - **Debt-equity below 1.5 outside financials.** A financial name is exempt; any other name
      with no debt-equity on record fails — an unknown never passes a quality filter.
    """
    profitable = any(v is not None and v > _ZERO for v in (facts.net_margin_ttm, facts.pe_ttm))
    if not profitable:
        return False
    if facts.sector in FINANCIAL_SECTORS:
        return True
    return facts.debt_equity is not None and facts.debt_equity < FUND_MAX_DEBT_EQUITY


#: Each style's ranked list, in words (journaled with every style rebalance, and hashed).
_STYLE_RULES: Final[Mapping[ManagerStyle, str]] = {
    ManagerStyle.SWING_BREAKOUT: "S2 by rank, then S3 by rank (names already on S2 skipped)",
    ManagerStyle.SWING_EVENT: "S4 by rank",
    ManagerStyle.POSITIONAL_TREND: "S1 by rank",
    ManagerStyle.POSITIONAL_FUNDAMENTAL: (
        f"S4 by rank, keeping names with a positive TTM profit and, outside "
        f"{sorted(FINANCIAL_SECTORS)}, debt-equity below {FUND_MAX_DEBT_EQUITY}"
    ),
}


def style_rule_bytes() -> bytes:
    """The style books' selection rule as canonical bytes (part of each style `mandate_hash`)."""
    return json.dumps(
        {
            "version": STYLE_RULE_VERSION,
            "rules": {style.value: _STYLE_RULES[style] for style in ManagerStyle},
            "fund_max_debt_equity": str(FUND_MAX_DEBT_EQUITY),
            "financial_sectors": sorted(FINANCIAL_SECTORS),
            "buy_budget_fraction": str(CONTROL_BUY_BUDGET_FRACTION),
            "rebalance_band": str(CONTROL_REBALANCE_BAND),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def style_candidates(
    style: ManagerStyle,
    screens: CommonsScreens,
    *,
    quality: Mapping[str, QualityFacts] | None = None,
) -> RankedTargets:
    """A style book's ranked list on the screens' session (Amendment 2 d).

    BRK: S2 then S3, each by rank, a name on both kept at its S2 place. EVT: S4. TREND: S1. FUND:
    S4 names passing `passes_quality`, judged on ``quality`` (a name absent from it fails —
    the caller supplies the facts of every S4 name). Raises `ControlError` for a FUND list with
    no ``quality`` at all, rather than treating the whole screen as failing.
    """
    screens.verify()

    def ranked(screen: Screen) -> list[str]:
        return [e.isin for e in sorted(screens.ranked(screen), key=lambda e: e.position)]

    if style is ManagerStyle.SWING_BREAKOUT:
        isins = list(dict.fromkeys(ranked(Screen.S2) + ranked(Screen.S3)))
    elif style is ManagerStyle.SWING_EVENT:
        isins = ranked(Screen.S4)
    elif style is ManagerStyle.POSITIONAL_TREND:
        isins = ranked(Screen.S1)
    else:
        if quality is None:
            raise ControlError("the FUND style list needs the S4 names' quality facts")
        isins = [
            isin
            for isin in ranked(Screen.S4)
            if (facts := quality.get(isin)) is not None and passes_quality(facts)
        ]
    return RankedTargets(
        trading_date=screens.trading_date,
        isins=tuple(isins),
        source="commons_screens",
        digest_label="screens_digest",
        digest=screens.screens_digest,
        rule=f"{style.value}: {_STYLE_RULES[style]}",
    )


# ── CTRL-<book> and STYLE-<manager> ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ControlState:
    """What a control carries between sessions: its last rebalance, targets and pending buys.

    ``target_value`` is each target's rupee value as struck at the last rebalance; ``cap_tiers``
    the targets' cap tiers then (for the scoreboard's tier mix); ``pending`` the targets still to
    buy, in shortlist order.
    """

    last_rebalance: date | None = None
    targets: tuple[str, ...] = ()
    target_value: Decimal = _ZERO
    cap_tiers: Mapping[str, str | None] = field(default_factory=dict)
    pending: tuple[str, ...] = ()

    def to_document(self) -> dict[str, Any]:
        """Strings only, in a fixed order: persists beside the book's account document."""
        return {
            "last_rebalance": None
            if self.last_rebalance is None
            else self.last_rebalance.isoformat(),
            "targets": list(self.targets),
            "target_value": str(self.target_value),
            "cap_tiers": {isin: self.cap_tiers.get(isin) for isin in self.targets},
            "pending": list(self.pending),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ControlState:
        last = document["last_rebalance"]
        return cls(
            last_rebalance=None if last is None else date.fromisoformat(last),
            targets=tuple(document["targets"]),
            target_value=Decimal(document["target_value"]),
            cap_tiers=dict(document["cap_tiers"]),
            pending=tuple(document["pending"]),
        )


def rebalance_due(
    mandate: ControlMandate | StyleMandate,
    state: ControlState,
    session: date,
    *,
    sessions_since: int | None,
) -> bool:
    """Is ``session`` a rebalance session for ``mandate``? ``sessions_since`` counts the sessions
    in (last rebalance, session]; it is ignored for a weekly cadence and before the first one."""
    last = state.last_rebalance
    if last is None:
        return True
    if session <= last:
        return False
    if mandate.rebalance.unit is RebalanceUnit.WEEKLY:
        return session.isocalendar()[:2] != last.isocalendar()[:2]
    if sessions_since is None:
        raise ControlError(f"{mandate.id}: a session-count cadence needs sessions_since")
    return sessions_since >= mandate.rebalance.every


@dataclass(frozen=True, slots=True)
class ControlSession:
    """What one control session did: whether it rebalanced, what it asked, what the book did."""

    session: date
    rebalanced: bool
    orders: tuple[BookOrder, ...]
    report: DecisionReport
    buys: tuple[ControlBuy, ...]
    state: ControlState


class ControlBook:
    """``CTRL-<book>`` or ``STYLE-<manager>``: equal weight over the top *N* of a ranked list, no
    LLM — the composite shortlist for a control (`run`), the style's screens for a style book
    (`run_ranked` with `style_candidates`).

    What it does: decide the book's orders for one session by the rule in the module docstring
    and hand them to its `FundBook`, which clears them through the rails and stages them.
    What it assumes: ``book`` is this book's own, built from ``mandate`` (`FundDesk` checks
    isolation), and its fills for the session were already executed.
    What it never does: call a model, look outside its ranked list, or resize a refused order.
    """

    __slots__ = ("_book", "_mandate", "_rails", "_state")

    def __init__(
        self,
        mandate: ControlMandate | StyleMandate,
        book: FundBook,
        rails: M17Rails,
        *,
        state: ControlState | None = None,
    ) -> None:
        if book.book_id != mandate.id:
            raise ControlError(f"control {mandate.id} given book {book.book_id}")
        self._mandate = mandate
        self._book = book
        self._rails = rails
        self._state = ControlState() if state is None else state

    @property
    def state(self) -> ControlState:
        return self._state

    @property
    def book(self) -> FundBook:
        return self._book

    @property
    def top_n(self) -> int:
        """How many names the book equal-weights: the shortlist cut, or the style's max names."""
        mandate = self._mandate
        return mandate.shortlist_top_n if isinstance(mandate, ControlMandate) else mandate.max_names

    def run(
        self,
        session: date,
        shortlist: Shortlist | None,
        cap_tiers: Mapping[str, str | None],
    ) -> ControlSession:
        """One session of a control: rebalance on the shortlist if due (and one exists), then
        place whatever pending buys fit. ``cap_tiers`` maps the universe ISINs to their tier."""
        if not isinstance(self._mandate, ControlMandate):
            raise ControlError(f"{self._mandate.id} is not a control; it runs on its own list")
        ranked = None if shortlist is None else shortlist_targets(shortlist)
        return self.run_ranked(session, ranked, cap_tiers)

    def run_ranked(
        self,
        session: date,
        ranked: RankedTargets | None,
        cap_tiers: Mapping[str, str | None],
    ) -> ControlSession:
        """One session on ``ranked`` (None: the list could not be built, a due rebalance waits)."""
        book = self._book
        if book.kill_switch.is_tripped:
            report = book.decide(session, ())
            return ControlSession(session, False, (), report, (), self._state)

        state = self._state
        last = state.last_rebalance
        since = None if last is None else book.market.sessions_between(last, session)
        due = rebalance_due(self._mandate, state, session, sessions_since=since)
        held = {isin: q for isin, q in book.account.quantities().items() if q > 0}
        sells: list[BookOrder] = []
        rebalanced = False
        if due and ranked is None:
            _LOG.warning(
                "fm_controls.rebalance_deferred",
                book=self._mandate.id,
                session=session.isoformat(),
                reason="no ranked list for the session",
            )
        elif due and ranked is not None:
            state, sells = self._rebalance(session, ranked, cap_tiers, held)
            rebalanced = True

        buys = self._pending_buys(session, state, held, sold={o.isin for o in sells})
        orders = (*sells, *buys)
        report = book.decide(session, orders)
        staged_buys = {o.isin for o, _ in report.staged if o.side is Side.BUY}
        refused = {o.isin for o, _ in report.refused if o.side is Side.BUY}
        # A buy that was asked and staged, or refused by a rail, leaves pending; an unpriced one
        # (or one that did not fit yet) stays.
        state = replace(
            state, pending=tuple(i for i in state.pending if i not in staged_buys | refused)
        )
        self._state = state
        control_buys = tuple(
            ControlBuy(
                book_id=self._mandate.id,
                session=session,
                isin=order.isin,
                cap_tier=state.cap_tiers.get(order.isin),
            )
            for order, _ in report.staged
            if order.side is Side.BUY
        )
        _LOG.info(
            "fm_controls.session",
            book=self._mandate.id,
            session=session.isoformat(),
            rebalanced=rebalanced,
            orders=len(orders),
            staged=len(report.staged),
            refused=len(report.refused),
            pending=len(state.pending),
        )
        return ControlSession(session, rebalanced, tuple(orders), report, control_buys, state)

    # -- the rebalance ----------------------------------------------------------------------------

    def _rebalance(
        self,
        session: date,
        ranked: RankedTargets,
        cap_tiers: Mapping[str, str | None],
        held: Mapping[str, int],
    ) -> tuple[ControlState, list[BookOrder]]:
        if ranked.trading_date != session:
            what = "shortlist" if ranked.source == "commons_shortlist" else "style list"
            raise ControlError(
                f"{self._mandate.id}: {what} of {ranked.trading_date.isoformat()} offered "
                f"for the {session.isoformat()} rebalance"
            )
        n = self.top_n
        targets = ranked.isins[:n]
        nav = self._nav(session, held)
        with localcontext(_CONTEXT):
            each = (nav * CONTROL_BUY_BUDGET_FRACTION / Decimal(n)).quantize(
                _PAISE, rounding=ROUND_FLOOR
            )
        sells: list[BookOrder] = []
        pending: list[str] = []
        for isin, quantity in sorted(held.items()):
            if isin not in targets:
                sells.append(
                    BookOrder(
                        isin,
                        Side.SELL,
                        quantity,
                        f"{self._mandate.id} rebalance {session.isoformat()}: {isin} is no longer "
                        f"in the top {n} of its list ({ranked.source})",
                    )
                )
        for isin in targets:
            quantity = held.get(isin, 0)
            if quantity == 0:
                pending.append(isin)
                continue
            close = self._book.market.close(isin, session)
            if close is None:
                continue
            target_qty = _shares(each, close)
            with localcontext(_CONTEXT):
                high = Decimal(target_qty) * (_ONE + CONTROL_REBALANCE_BAND)
                low = Decimal(target_qty) * (_ONE - CONTROL_REBALANCE_BAND)
            if quantity > high and quantity > target_qty:
                sells.append(
                    BookOrder(
                        isin,
                        Side.SELL,
                        quantity - target_qty,
                        f"{self._mandate.id} rebalance {session.isoformat()}: trim {isin} back to "
                        f"equal weight ({quantity} held, {target_qty} target)",
                    )
                )
            elif quantity < low:
                pending.append(isin)
        state = ControlState(
            last_rebalance=session,
            targets=targets,
            target_value=each,
            cap_tiers={isin: cap_tiers.get(isin) for isin in targets},
            pending=tuple(pending),
        )
        self._journal_rebalance(session, ranked, state, nav)
        return state, sells

    def _journal_rebalance(
        self, session: date, ranked: RankedTargets, state: ControlState, nav: Decimal
    ) -> None:
        book = self._book
        items = [
            EvidenceItem(
                kind=EvidenceKind.POLICY,
                source=ranked.source,
                label=ranked.digest_label,
                as_of=session,
                text=ranked.digest,
            ),
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="m17_control",
                label="nav",
                as_of=session,
                value=nav,
            ),
        ]
        evidence = EvidenceBundle(
            case_id=self._mandate.id, trading_date=session, actor=Actor.SYSTEM, items=tuple(items)
        )
        entry = JournalEntry(
            ts=book.clock.now(),
            trading_date=session,
            case_id=self._mandate.id,
            actor=Actor.SYSTEM,
            decision=Decision.HEARTBEAT,
            evidence_snapshot_ref=evidence.ref().ref,
            rationale=(
                f"{'style' if isinstance(self._mandate, StyleMandate) else 'control'} rebalance: "
                f"equal weight across the top {len(state.targets)} of the {session.isoformat()} "
                f"list ({ranked.rule}), {state.target_value} each; at most {self.top_n} names"
            ),
            payload={
                "event": CONTROL_REBALANCE_EVENT,
                "book": self._mandate.id,
                "mode": PAPER_MODE,
                ranked.digest_label: ranked.digest,
                "rule": ranked.rule,
                "targets": ",".join(state.targets),
                "target_value": str(state.target_value),
                "cap_tiers": json.dumps(
                    {isin: state.cap_tiers.get(isin) or "" for isin in state.targets},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )
        book.journal.append(entry, evidence=evidence)

    # -- pending buys -----------------------------------------------------------------------------

    def _pending_buys(
        self, session: date, state: ControlState, held: Mapping[str, int], *, sold: set[str]
    ) -> list[BookOrder]:
        book = self._book
        mandate = self._mandate
        # A name being sold still holds its slot until the sale fills (FundBook clears buys
        # against the book before its sells), so the free slots are counted on what is held.
        slots = mandate.max_positions - len(held)
        with localcontext(_CONTEXT):
            budget = book.account.spendable_cash * CONTROL_BUY_BUDGET_FRACTION
        buys: list[BookOrder] = []
        for isin in state.pending:
            if isin in sold:
                continue
            close = book.market.close(isin, session)
            if close is None:
                continue  # stays pending: no close to size it at
            target_qty = _shares(state.target_value, close)
            quantity = min(
                target_qty - held.get(isin, 0), self._participation_cap(isin, session, close)
            )
            if quantity <= 0:
                continue
            new_name = isin not in held
            notional = close * quantity
            if (new_name and slots <= 0) or notional > budget:
                continue  # stays pending until a sale fills and settles
            buys.append(
                BookOrder(
                    isin,
                    Side.BUY,
                    quantity,
                    f"{mandate.id}: equal weight in {isin}, top {self.top_n} of the "
                    f"{(state.last_rebalance or session).isoformat()} list",
                )
            )
            budget -= notional
            if new_name:
                slots -= 1
        return buys

    def _participation_cap(self, isin: str, session: date, close: Decimal) -> int:
        """The most shares the participation rail would let one order carry (0 with no median)."""
        lookback = self._rails.participation_lookback_sessions
        history = [
            v
            for d, v in self._book.market.traded_values(isin, through=session, sessions=lookback)
            if d <= session
        ]
        if len(history) < lookback:
            return 0
        with localcontext(_CONTEXT):
            ceiling = (
                median_traded_value(history[-lookback:])
                * self._rails.participation_max_pct
                / _HUNDRED
            )
        return _shares(ceiling, close)

    def _nav(self, session: date, held: Mapping[str, int]) -> Decimal:
        total = self._book.account.cash_value
        for isin, quantity in held.items():
            close = self._book.valuation_close(isin, session)
            if close is None:
                raise ControlError(
                    f"{self._mandate.id}: held {isin} has no close on {session.isoformat()} and "
                    "has not delisted; the control cannot be valued, so it cannot be rebalanced"
                )
            total += close * quantity
        return total


def _shares(value: Decimal, price: Decimal) -> int:
    """Whole shares of ``price`` that ``value`` buys."""
    if price <= _ZERO:
        raise ControlError(f"a non-positive price {price} cannot size an order")
    with localcontext(_CONTEXT):
        return int((value / price).to_integral_value(rounding=ROUND_FLOOR))


# ── BENCH-N500 ───────────────────────────────────────────────────────────────────────────────────


class BenchmarkLevels(Protocol):
    """The bench's index levels, one per session, from one method."""

    @property
    def method(self) -> str: ...

    def level(self, session: date) -> Decimal | None:
        """The level for exactly ``session``, or None if the series has none."""
        ...


class LakeTriBenchmark:
    """The roster benchmark label's series out of ``L1/benchmark_tri``, through a session.

    Reads with ``read_tri_series`` (the backtests' reader), published first then §4.1's computed
    estimate — or exactly ``method`` when given, which is how a bench that opened on one method
    keeps it. Raises `BenchmarkUnavailableError` when the lake holds neither.
    """

    __slots__ = ("_series",)

    def __init__(
        self,
        label: str,
        *,
        through: date,
        method: str | None = None,
        data_root: Path | None = None,
    ) -> None:
        slug = BENCHMARK_INDEX_SLUGS.get(label)
        if slug is None:
            raise BenchmarkUnavailableError(f"unknown benchmark label {label!r}")
        series: TriSeries | None = read_tri_series(
            slug, through, method=method, data_root=data_root
        )
        if series is None:
            raise BenchmarkUnavailableError(
                f"no {slug!r} series{'' if method is None else f' ({method})'} in "
                f"L1/benchmark_tri through {through.isoformat()}: {label} cannot be marked. "
                "Ingest the published NIFTY 500 TRI (dataplatform.ingest.tri_backfill) or "
                "compute §4.1's estimate from the index close snapshots (ingest_tri_from_close)"
            )
        self._series = series

    @property
    def method(self) -> str:
        return self._series.method

    def level(self, session: date) -> Decimal | None:
        for point in reversed(self._series.points):
            if point.as_of == session:
                return point.tri_value
            if point.as_of < session:
                return None
        return None


@dataclass(frozen=True, slots=True)
class BenchState:
    """The bench's purchase: the session whose close it was bought at, that level, the method."""

    base_session: date
    base_level: Decimal
    method: str

    def to_document(self) -> dict[str, str]:
        return {
            "base_session": self.base_session.isoformat(),
            "base_level": str(self.base_level),
            "method": self.method,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, str]) -> BenchState:
        return cls(
            base_session=date.fromisoformat(document["base_session"]),
            base_level=Decimal(document["base_level"]),
            method=document["method"],
        )


class BenchJournal(Protocol):
    def append(self, entry: JournalEntry, *, evidence: EvidenceBundle | None = None) -> object: ...


class BenchBook:
    """``BENCH-N500``: buy-and-hold of the TRI proxy from S0, marked every close, no trading."""

    __slots__ = ("_clock", "_journal", "_mandate", "_state")

    def __init__(
        self,
        mandate: BenchMandate,
        *,
        journal: BenchJournal,
        clock: Clock,
        state: BenchState | None = None,
    ) -> None:
        self._mandate = mandate
        self._journal = journal
        self._clock = clock
        self._state = state

    @property
    def state(self) -> BenchState | None:
        return self._state

    def open(self, levels: BenchmarkLevels, base_session: date) -> BenchState:
        """Buy at ``base_session``'s close — the session before S0. Raises if it has no level."""
        if self._state is not None:
            raise ControlError(f"{self._mandate.id} is already open at {self._state.base_session}")
        level = levels.level(base_session)
        if level is None or level <= _ZERO:
            raise BenchmarkUnavailableError(
                f"{self._mandate.id}: no level on {base_session.isoformat()} to open at"
            )
        self._state = BenchState(base_session=base_session, base_level=level, method=levels.method)
        return self._state

    def mark(self, levels: BenchmarkLevels, session: date) -> BookMark:
        """The bench's mark at ``session``'s close, journaled in its stream."""
        state = self._state
        if state is None:
            raise ControlError(f"{self._mandate.id} is marked before it opened")
        if levels.method != state.method:
            raise BenchmarkUnavailableError(
                f"{self._mandate.id} opened on the {state.method} series and is offered "
                f"{levels.method}; the bench never splices two methods"
            )
        if session <= state.base_session:
            raise ControlError(f"{self._mandate.id}: {session} is not after its purchase")
        level = levels.level(session)
        if level is None:
            raise BenchmarkUnavailableError(
                f"{self._mandate.id}: no {state.method} level on {session.isoformat()}"
            )
        with localcontext(_CONTEXT):
            nav = (self._mandate.opening_capital_inr * level / state.base_level).quantize(_RUPEE_6)
        mark = BookMark(
            book_id=self._mandate.id,
            session=session,
            nav=nav,
            cash=_ZERO,
            positions=1,
            turnover=_ZERO,
            costs=_ZERO,
            interest=_ZERO,
        )
        entry, evidence = mark_entry(mark, clock=self._clock)
        self._journal.append(entry, evidence=evidence)
        return mark


def record_mark(mark: BookMark, book: FundBook) -> BookMark:
    """Journal ``mark`` in ``book``'s own stream; returns it."""
    entry, evidence = mark_entry(mark, clock=book.clock)
    book.journal.append(entry, evidence=evidence)
    return mark
