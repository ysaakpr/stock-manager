"""X2: the risk rails in the replay — the backtest's route through A8 (invariant #6).

Every order a replayed policy returns reaches the broker only after A8 has cleared it, through the
same ``RailEngine.guard_order`` the paper and real daily loop call (``analyst/cash``,
``analyst/rotation``). There is no second implementation of a cap here: this module builds the
inputs A8 needs — a ``Portfolio`` from the broker's book, a ``ProposedOrder`` priced and
sector-tagged — asks A8, and forwards only what it allowed. A blocked order is written to the
replay's journal as the ``RAIL_BLOCK`` line A8 itself produced, naming every rail it breached, so a
backtest's blocks are countable exactly as a paper run's are (§5.7's rail-breach count).

Three inputs make a railed run reproducible, and all three are recorded in its digest:

* **The rail policy** (``BacktestRailPolicy``) — the ratified ``RiskRails`` numbers, an id and a
  version. ``ratified_backtest_rail_policy`` builds it from the repo's ratified defaults; a caller
  may inject any other, and the run records which one was in force.
* **The sector classification** (``SectorMap``) — what the sector rail groups a holding under. The
  best source the repo holds is NSE's own industry classification of the Nifty Total Market list,
  captured to L0 on 2026-09-08 and checked in byte-identical as a test fixture. It is a
  *current-day* map applied backward: a name listed in 2016 and gone by 2026 has no label. See
  ``UNKNOWN_SECTOR`` for what happens to it.
* **The marks** — the reference price A8 values the book and the order at: the session's raw L1
  close, the same series the fill model trades on.

What this module never does: loosen a cap, skip a check, or decide whether to trade. An order A8
cannot be asked about — a buy the book cannot fund at the reference price, a sell of shares not
held — is not placed either; it is journalled as an ``ESCALATE`` by ``EXEC`` rather than let
through unchecked, because an order that reached the broker without a verdict is the bypass this
module exists to close.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any, Final

import structlog

from analyst.cases import RiskRails
from analyst.journal.evidence import canonical_bytes, digest_of
from analyst.journal.models import Actor, Decision, JournalEntry, RecordedEntry, Sleeve
from analyst.rails import Lot, Portfolio, ProposedOrder, RailEngine, apply_order
from dataplatform.clock import Clock
from execution.broker import Broker, Holding, OrderRequest, Position, Side

_log = structlog.get_logger(__name__)

__all__ = [
    "BACKTEST_CASE_ID",
    "RATIFIED_SECTOR_SOURCE",
    "UNEXECUTABLE_EVENT",
    "UNKNOWN_SECTOR",
    "BacktestRailPolicy",
    "GateOutcome",
    "RailGate",
    "SectorMap",
    "rail_blocks_by_rail",
    "ratified_backtest_rail_policy",
    "ratified_sector_map",
]

_ZERO: Final = Decimal(0)

#: The sector every ISIN the classification does not name is grouped under — one pooled bucket.
#:
#: The conservative choice, deliberately. Treating each unlabelled name as its own sector would make
#: the sector cap unreachable for exactly the names we know least about; pooling them means an
#: unlabelled holding *counts toward* a 35% cap shared with every other unlabelled holding. On a
#: backtest that reaches far behind the classification's date that can bind hard, and when it does
#: the breach detail names ``'UNKNOWN'`` — the block is the classification's gap made visible, not a
#: judgement that those names share a sector.
UNKNOWN_SECTOR: Final = "UNKNOWN"

#: The case a backtest's rail book belongs to when the policy's evidence names none.
BACKTEST_CASE_ID: Final = "BACKTEST"

#: The payload marker on an order the gate could not put to A8 (see the module docstring).
UNEXECUTABLE_EVENT: Final = "ORDER_UNEXECUTABLE_AT_REFERENCE"

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]

#: NSE's industry classification of the Nifty Total Market list, as captured to L0 on 2026-09-08
#: (``L0/nse_industry_classification/2026/09/ind_niftytotalmarket_list_20260908.csv``) and checked
#: in byte-identical as the D-series fixture. 755 ISINs, 22 NSE industries. Pinned by content hash
#: so a railed run cannot silently change because a newer snapshot landed in the lake.
RATIFIED_SECTOR_SOURCE: Final = (
    _REPO_ROOT / "tests/fixtures/nse_market_structure/2026-09-08/ind_niftytotalmarket_list.csv"
)
_RATIFIED_SECTOR_SHA256: Final = "6b23b6c155d7ffead0b8ef89e75d54e61e74f6d39e2aece7392aa02a9492e445"


# ── the sector classification ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SectorMap:
    """ISIN -> sector, with where it came from.

    What it does: answer the sector A8's sector rail groups a holding under, and ``UNKNOWN_SECTOR``
    for any ISIN it does not name (the pooled, cap-counting bucket).
    What it assumes: ``source`` and ``sha256`` identify the bytes the map was read from, so two runs
    that agree on them agreed on every label.
    What it never does: guess a label, or key on a symbol (invariant #2).
    """

    source: str
    sha256: str
    by_isin: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        for isin, sector in self.by_isin.items():
            if not sector.strip():
                raise ValueError(f"sector map names {isin} with an empty sector")
            if sector == UNKNOWN_SECTOR:
                raise ValueError(
                    f"sector map labels {isin} {UNKNOWN_SECTOR!r}, the name reserved for the "
                    "pooled bucket of unclassified ISINs — a real label must not collide with it"
                )

    def sector_of(self, isin: str) -> str:
        """The sector of ``isin``; ``UNKNOWN_SECTOR`` when the classification does not name it."""
        return self.by_isin.get(isin, UNKNOWN_SECTOR)

    def to_document(self) -> dict[str, Any]:
        """What a run records about the classification: its source, its hash and its size."""
        return {"source": self.source, "sha256": self.sha256, "isins": len(self.by_isin)}

    @classmethod
    def from_industry_csv(cls, path: Path, *, expected_sha256: str | None = None) -> SectorMap:
        """Read NSE's five-column constituent CSV (Company Name, Industry, Symbol, Series, ISIN).

        Refuses a file whose content hash differs from ``expected_sha256`` when one is given — a
        pinned classification that changed underneath a run is a different policy, not the same one.
        """
        raw = path.read_bytes()
        sha256 = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise ValueError(
                f"sector classification {path} hashes to {sha256}, not the pinned "
                f"{expected_sha256}: the rail policy names different bytes than the ones on disk"
            )
        by_isin: dict[str, str] = {}
        reader = csv.DictReader(raw.decode("utf-8-sig").splitlines())
        for row in reader:
            isin = (row.get("ISIN Code") or "").strip()
            industry = (row.get("Industry") or "").strip()
            if isin and industry:
                by_isin[isin] = industry
        if not by_isin:
            raise ValueError(f"sector classification {path} holds no ISIN,Industry rows")
        return cls(source=path.name, sha256=sha256, by_isin=by_isin)


@cache
def ratified_sector_map() -> SectorMap:
    """The pinned NSE industry classification (``RATIFIED_SECTOR_SOURCE``), hash-verified."""
    return SectorMap.from_industry_csv(
        RATIFIED_SECTOR_SOURCE, expected_sha256=_RATIFIED_SECTOR_SHA256
    )


# ── the policy in force ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BacktestRailPolicy:
    """The rails a replay enforces: the ``RiskRails`` numbers, the sector map, an id and a version.

    What it does: carry everything A8's verdict depends on besides the book and the order, and
    render it as one canonical document whose digest a run records — so "which rails were in force"
    is a byte-comparable fact about a run rather than a default someone has to remember.
    What it never does: carry an override. There is no field that exempts an order, a rail or a
    session; the only way to change a verdict is to run under a different, differently-named policy.
    """

    policy_id: str
    version: int
    rails: RiskRails
    sectors: SectorMap
    provenance: str

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise ValueError("a rail policy needs an id")
        if self.version < 1:
            raise ValueError(f"a rail policy version is a positive integer, got {self.version}")

    def to_document(self) -> dict[str, Any]:
        """The canonical record of this policy — what a run's digest covers."""
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "rails": self.rails.model_dump(mode="json"),
            "sectors": self.sectors.to_document(),
            "provenance": self.provenance,
        }

    def digest(self) -> str:
        """sha256 over the canonical document."""
        return digest_of(canonical_bytes(self.to_document()))

    @property
    def label(self) -> str:
        """``id@vN`` — the short name a report prints."""
        return f"{self.policy_id}@v{self.version}"


#: The capital plan's SIP in EXECUTION_PLAN §5.2's default column (₹10k monthly), and the
#: interview's rule that one order may not exceed twelve instalments (``analyst/interview/flow.py``,
#: ``_ORDER_VALUE_SIP_MULTIPLE``). The plan gives no number for the per-order caps; these two are
#: the only ratified derivation of them in the repo.
_RATIFIED_SIP_INR: Final = Decimal("10000")
_RATIFIED_ORDER_VALUE_SIP_MULTIPLE: Final = 12


def ratified_backtest_rail_policy() -> BacktestRailPolicy:
    """The default rail policy for a replay, from the ratified defaults and nothing else.

    EXECUTION_PLAN §5.2's default column fixes four numbers — 15% position, 35% sector, 8 holdings,
    a 25% drawdown review — which are also the interview's MEDIUM profile. The per-order caps are
    derived the way the interview derives them: the percentage cap equals the position cap, and the
    rupee cap is 12 x the ₹10k default SIP = ₹1,20,000.

    What it assumes, and a reader must know: that rupee cap is scaled to a ₹10k/month SIP case. A
    lump-sum backtest compounding past a few lakh will meet it on ordinary rebalance buys; that is
    the ratified number biting, not a defect of the gate. A different case capital is a different
    policy — inject one, under its own id, rather than editing this.
    """
    rails = RiskRails(
        max_position_pct=Decimal("15"),
        max_sector_pct=Decimal("35"),
        min_holdings=8,
        drawdown_review_pct=Decimal("25"),
        max_order_value_inr=_RATIFIED_SIP_INR * _RATIFIED_ORDER_VALUE_SIP_MULTIPLE,
        max_order_pct_of_case=Decimal("15"),
    )
    return BacktestRailPolicy(
        policy_id="ratified-default",
        version=1,
        rails=rails,
        sectors=ratified_sector_map(),
        provenance=(
            "EXECUTION_PLAN §5.2 default rails 15%/35%/8/-25%; per-order caps by the interview "
            "rule (pct = position cap, rupees = 12 x the ₹10k default SIP); sectors from NSE's "
            "Nifty Total Market industry classification of 2026-09-08, unlabelled ISINs pooled "
            "as UNKNOWN"
        ),
    )


# ── the gate ─────────────────────────────────────────────────────────────────────────────────


class _SessionRailJournal:
    """A ``RailJournal`` that collects A8's entries for the replay to journal in session order.

    It does not persist anything: the replay engine stamps the session's evidence onto each entry
    and writes it to the attached ``Journal`` (if any) alongside the policy's own, so a rail block
    lands in the same deterministic ``ReplayResult`` whether or not a database is attached.
    """

    __slots__ = ("_clock", "entries")

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.entries: list[JournalEntry] = []

    def append(self, entry: JournalEntry) -> RecordedEntry:
        self.entries.append(entry)
        return RecordedEntry(
            **entry.model_dump(), id=len(self.entries), recorded_at=self._clock.now()
        )


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """One session's verdict: the orders A8 cleared, in policy order, and what was journalled.

    ``refused`` is every order not placed — blocked by a rail or unexecutable — so the engine can
    retire the policy's own BUY/SELL line for it: in the paper loop a refused order is recorded by
    its ``RAIL_BLOCK`` alone, and a replay must not report a trade that never reached the broker.
    """

    allowed: tuple[OrderRequest, ...]
    entries: tuple[JournalEntry, ...]
    refused: tuple[OrderRequest, ...] = ()


class RailGate:
    """Clear a replayed session's orders through A8, in order, against the projected book.

    Construct it with the ``BacktestRailPolicy`` and a ``marks`` source (session -> ISIN -> raw
    close). ``clear`` builds the book A8 checks from the broker, then puts each order to
    ``RailEngine.guard_order``. Under settlement (T+2 before 2023, T+1 since) the book is built so
    each cap sees what the account is *exposed to and worth*, and spending sees only what it may
    spend:

    * **Lots**: settled holdings *plus* every pending (bought, unsettled) lot, summed per ISIN. A
      name bought yesterday is exposure today; ignoring it would let the next buy take the name past
      its position cap while its first lot is still in settlement.
    * **Cash in the book** is ``Margins.cash_value`` — settled cash plus unsettled sale proceeds —
      so the percentage caps are fractions of the account's full value, not of a value that dips by
      every sale until it settles.
    * **Spendable cash** is ``Margins.available`` alone, less every buy already cleared this
      session. A buy larger than that is not fundable; unsettled proceeds never fund it.

    Corporate actions are applied at the top of the session, before the policy decides, so the
    quantities read here are already post-split and the session's raw close is the post-split
    price. An allowed order is applied to the projected book before the next
    is checked, so twelve sells that would each be fine alone cannot together take the book under
    the minimum-holdings floor.

    What it assumes: the broker has no order staged from an earlier session (the engine fills them
    before the policy decides), so the book it reads is the book the orders will act on.
    What it never does: place an order. The engine places what ``clear`` allowed and nothing else.
    """

    __slots__ = ("_last_mark", "_marks", "_policy")

    def __init__(
        self,
        policy: BacktestRailPolicy,
        marks: Callable[[date], Mapping[str, Decimal]],
    ) -> None:
        self._policy = policy
        self._marks = marks
        self._last_mark: dict[str, Decimal] = {}

    @property
    def policy(self) -> BacktestRailPolicy:
        return self._policy

    def clear(
        self,
        session: date,
        orders: Sequence[OrderRequest],
        *,
        broker: Broker,
        clock: Clock,
        case_id: str | None,
        sleeves: Mapping[str, Sleeve],
    ) -> GateOutcome:
        """The subset of ``orders`` A8 allows, and the entries it (and the gate) journalled."""
        if not orders:
            return GateOutcome(allowed=(), entries=())
        self._last_mark.update(self._marks(session))
        sink = _SessionRailJournal(clock)
        engine = RailEngine(sink, clock=clock)
        book = self._book(broker, case_id or BACKTEST_CASE_ID)
        spendable = broker.margins().available
        allowed: list[OrderRequest] = []
        refused: list[OrderRequest] = []
        for request in orders:
            proposed = self._propose(request, session, book)
            reason = _unexecutable(proposed, book, spendable)
            if reason is not None:
                sink.entries.append(
                    _unexecutable_entry(clock, session, book.case_id, proposed, reason, sleeves)
                )
                _log.info(
                    "replay.order_unexecutable",
                    session=session.isoformat(),
                    isin=request.isin,
                    side=request.side.value,
                    reason=reason,
                )
                refused.append(request)
                continue
            assessment = engine.guard_order(
                proposed,
                book,
                self._policy.rails,
                trading_date=session,
                sleeve=sleeves.get(request.isin),
            )
            if not assessment.allowed:
                refused.append(request)
                continue
            # A8's own book transition, so the next order is checked against what this one leaves.
            book = apply_order(book, proposed)
            if proposed.side is Side.BUY:
                spendable -= proposed.value  # a sale's proceeds stay unsettled: never spendable
            allowed.append(request)
        return GateOutcome(
            allowed=tuple(allowed), entries=tuple(sink.entries), refused=tuple(refused)
        )

    def _price(self, isin: str, fallback: Decimal | None) -> Decimal | None:
        """Today's close, else the last close seen, else ``fallback`` (the broker's cost basis)."""
        mark = self._last_mark.get(isin)
        return mark if mark is not None else fallback

    def _book(self, broker: Broker, case_id: str) -> Portfolio:
        quantities: dict[str, int] = {}
        basis: dict[str, Decimal] = {}
        held: Sequence[Holding | Position] = (*broker.holdings(), *broker.positions())
        for lot in held:
            quantities[lot.isin] = quantities.get(lot.isin, 0) + lot.quantity
            basis.setdefault(lot.isin, lot.average_price)
        lots: list[Lot] = []
        for isin in sorted(quantities):
            if quantities[isin] <= 0:
                continue
            price = self._price(isin, basis[isin])
            if price is None or price <= _ZERO:
                raise ValueError(f"no positive mark for held {isin}: the rail book cannot value it")
            lots.append(
                Lot(
                    isin=isin,
                    sector=self._policy.sectors.sector_of(isin),
                    quantity=quantities[isin],
                    price=price,
                )
            )
        return Portfolio(case_id=case_id, lots=tuple(lots), cash=broker.margins().cash_value)

    def _propose(self, request: OrderRequest, session: date, book: Portfolio) -> ProposedOrder:
        price = self._price(request.isin, None)
        if price is None and request.side is Side.SELL:
            # A held name with no close yet is valued as ``_book`` values it: its broker cost
            # basis. A face-value split's successor ISIN can trade only in series BE for a while,
            # which the EQ-only reader never sees; refusing to value the exit would kill the run
            # over a holding the rails already carry at that price.
            held = next((lot for lot in book.lots if lot.isin == request.isin), None)
            if held is not None:
                price = held.price
                _log.warning(
                    "replay.sell_valued_at_cost_basis",
                    session=session.isoformat(),
                    isin=request.isin,
                    basis=str(price),
                    reason="no close on or before the session",
                )
        if price is None:
            # A buy of a name with no close on or before the decision session is a policy that
            # priced an order off something other than the market; fail loud rather than guess.
            raise ValueError(
                f"no close for {request.isin} on or before {session.isoformat()}: the rails cannot "
                "value the order, and an order they cannot value is not placed unchecked"
            )
        return ProposedOrder(
            request=request, price=price, sector=self._policy.sectors.sector_of(request.isin)
        )


def _unexecutable(order: ProposedOrder, book: Portfolio, spendable: Decimal) -> str | None:
    """Why ``order`` cannot be applied to ``book`` at all (A8's precondition), or None.

    A buy is measured against ``spendable`` (settled cash), not ``book.cash`` (which includes
    unsettled proceeds); a sell against every share on the book, settled or pending.
    """
    if order.side is Side.BUY:
        if order.value > spendable:
            return f"buy of {order.value} at the reference price exceeds free cash {spendable}"
        return None
    lot = book.lot(order.isin)
    held = 0 if lot is None else lot.quantity
    if order.quantity > held:
        return f"sell of {order.quantity} exceeds the {held} held"
    return None


def _unexecutable_entry(
    clock: Clock,
    session: date,
    case_id: str,
    order: ProposedOrder,
    reason: str,
    sleeves: Mapping[str, Sleeve],
) -> JournalEntry:
    return JournalEntry(
        ts=clock.now(),
        trading_date=session,
        case_id=case_id,
        actor=Actor.EXEC,
        decision=Decision.ESCALATE,
        isin=order.isin,
        sleeve=sleeves.get(order.isin),
        rationale=(
            f"{order.side.value} {order.quantity} {order.isin} not placed: {reason}; A8 cannot "
            "assess an order its book cannot hold, so it does not reach the broker unchecked"
        ),
        payload={"event": UNEXECUTABLE_EVENT, "reason": reason},
    )


def rail_blocks_by_rail(journal: Sequence[JournalEntry]) -> dict[str, int]:
    """Count ``RAIL_BLOCK`` entries per breached rail (an order breaching two counts under both)."""
    counts: dict[str, int] = {}
    for entry in journal:
        if entry.decision is not Decision.RAIL_BLOCK:
            continue
        rails = str((entry.payload or {}).get("rails", ""))
        for rail in filter(None, rails.split(",")):
            counts[rail] = counts.get(rail, 0) + 1
    return dict(sorted(counts.items()))
