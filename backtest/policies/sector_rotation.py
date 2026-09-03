"""M10.3 — a sector-rotation backtest policy (EXECUTION_PLAN §7, X2).

Momentum alone is a decaying, regime-dependent sleeve (the M9 finding that promoted M10), so this
policy diversifies the *signal*: instead of ranking single names, it first ranks *sectors* by their
members' aggregate momentum, keeps only the top ``top_k`` sectors, and then holds the top ``top_n``
momentum names drawn from those sectors' members. The bet is that momentum concentrates in
industries — that being in the right sectors, not just the right names, is where the edge is — and
the design is built to *measure* that bet against plain momentum on the identical universe (see the
M10 sector-rotation report), so the sector gate is the only thing that differs.

How it behaves each session — the same two-branch shape as the naive and v2 momentum policies:

* **Not a rebalance session** — it returns a bare evidence bundle and no orders, so the engine
  writes the one heartbeat per session invariant #9 requires ("checked, nothing due today").
* **A rebalance session** — it admits its candidates *only* through the injected point-in-time
  context (``ctx.pit.admit``), then:

    1. groups the admitted candidates by ``sector`` and scores each sector by the **mean** momentum
       of its members present this session (mean, not sum, so a sector is not favoured merely for
       having more constituents in the universe);
    2. ranks the sectors by that score and keeps the top ``top_k`` (ties broken by sector name);
    3. pools the members of those top sectors, ranks them by momentum, and takes the top ``top_n``
       (ties broken by ISIN) — the target basket;
    4. sells, in full, every settled holding no longer in the target, and allocates the cash
       currently free (less a small execution buffer) across the target names as whole shares,
       through the same M4.7 allocator the SIP tests cover.

Point-in-time (invariant #7): **sector membership is the one figure this policy adds over plain
momentum, and it is the one most tempting to leak.** Applying *today's* sector map to a past date is
survivorship bias — it silently assumes a name was in the industry (and the index) it is in now. The
guard against that is structural, not a convention: every candidate carries the ``knowable_date`` on
which its sector *and* its momentum became knowable, and the policy reads the candidate set only
through ``ctx.pit.admit``, so a record whose sector was resolved from a snapshot dated after the
session trips ``PitError`` rather than reaching a decision. The production data source resolves each
name's sector through ``membership_asof`` — the snapshot *in force on the decision date*, never
today's list — and stamps that snapshot's own capture date as the ``knowable_date`` (M3.9/M10.1). A
static current-day map used before M10.2's forward history has accrued is survivorship-biased by
construction; that limitation is stated in the report, and this policy does not paper over it.

What it never does: read a wall clock (time is ``ctx.clock`` — B10), key on a symbol (ISIN only —
invariant #2), hold a cost model (the injected broker owns the one shared model — invariant #4/#5),
or reach data outside the point-in-time context. Given the same inputs it returns the same decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from backtest.replay import SessionContext, SessionDecision
from backtest.sip import simulate_sip_instalment
from dataplatform.query.pit import Dataset
from execution.broker import Exchange, Holding, OrderRequest, Side

__all__ = [
    "SectorRotationData",
    "SectorRotationParameters",
    "SectorRotationPolicy",
    "SectorRotationRecord",
    "SectorScore",
    "rank_sectors",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_WEIGHT_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class SectorRotationRecord:
    """One candidate this session: its momentum, its price, its sector, and when all became known.

    What it carries: the ``isin`` (the only identity — invariant #2), its ``momentum`` (the trailing
    total return over the look-back window, as a plain ratio — ``0.25`` is +25 %), the current raw
    ``price`` the allocation and valuation use, the ``sector`` it belonged to *as of the decision
    date*, and ``knowable_date`` — the date on which the momentum *and* the sector membership became
    knowable. The point-in-time guard checks ``knowable_date`` against the session's ``as_of``
    (invariant #7): a record whose sector was read from a snapshot not yet published on the session
    is refused rather than silently used, which is the structural defence against applying a future
    (e.g. today's) sector map to a past decision.

    What it never does: hold a ``float`` (``momentum`` and ``price`` are ``Decimal``), a
    non-positive price (an unpriceable name cannot be sized into whole shares), or a blank sector (a
    name with no resolvable industry is not a candidate — it is dropped upstream, never guessed).
    """

    isin: str
    momentum: Decimal
    price: Decimal
    sector: str
    knowable_date: date

    def __post_init__(self) -> None:
        for name in ("momentum", "price"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(
                    f"{name} must be a Decimal — money/signal is never float (CLAUDE.md)"
                )
        if self.price <= _ZERO:
            raise ValueError(f"price must be positive, got {self.price}")
        if not self.sector or not self.sector.strip():
            raise ValueError(
                f"sector must be a non-empty industry label for {self.isin}; a name with no "
                "resolvable sector is not a sector-rotation candidate"
            )


@dataclass(frozen=True, slots=True)
class SectorScore:
    """One sector's aggregate momentum this session — the unit the sector ranking is struck on.

    ``sector`` is the industry label; ``mean_momentum`` is the arithmetic mean of the momentum of
    the sector's members present in the candidate set this session; ``member_count`` is how many
    such members there were. Mean (not sum) is deliberate: it scores a sector by how strongly its
    typical member is trending, not by how many of its names happen to be in the universe, so a
    broad sector is not mechanically favoured over a narrow one.
    """

    sector: str
    mean_momentum: Decimal
    member_count: int


@dataclass(frozen=True, slots=True)
class SectorRotationParameters:
    """The a-priori knobs — chosen once, stated, never tuned (M10.3: state parameters, no tuning).

    * ``top_k`` — how many sectors to stay invested in. The momentum-concentration bet is that a
      handful of industries carry the trend at any time; 5 is the round default (a NIFTY-500-shaped
      universe spans ~15-20 industries, so the top 5 is roughly the top quartile of sectors).
    * ``top_n`` — how many names to hold, drawn from the members of the top ``top_k`` sectors and
      equal-weighted. 20 matches the plain-momentum policy's basket size *exactly*, so a
      sector-rotation-vs-plain-momentum comparison on the same universe isolates the sector gate and
      nothing else (M10.3: report the two on the same universe).
    * ``buy_budget_fraction`` — the share of currently-free cash a rebalance deploys; a mechanical
      execution margin held a little under 1 so the next open plus slippage and charges still fit,
      **not** a return-tuning parameter (carried over from the momentum policies).
    * ``sleeve`` — the journal sleeve every trade is tagged with (§5.5); a rotation book is the
      tactical sleeve.

    All fixed by construction and echoed verbatim into the report, so the "no tuning" claim stays
    checkable.
    """

    top_k: int = 5
    top_n: int = 20
    buy_budget_fraction: Decimal = Decimal("0.98")
    sleeve: Sleeve = Sleeve.TACTICAL

    def __post_init__(self) -> None:
        if self.top_k <= 0:
            raise ValueError(f"top_k must be positive, got {self.top_k}")
        if self.top_n <= 0:
            raise ValueError(f"top_n must be positive, got {self.top_n}")
        if not isinstance(self.buy_budget_fraction, Decimal):
            raise TypeError("buy_budget_fraction must be a Decimal")
        if not (_ZERO < self.buy_budget_fraction <= _ONE):
            raise ValueError(
                f"buy_budget_fraction must be in (0, 1], got {self.buy_budget_fraction}"
            )


@runtime_checkable
class SectorRotationData(Protocol):
    """Where the policy reads its world — the injected seam the point-in-time context wraps.

    Two questions per session, both answered as of the session date and nothing later:

    * ``is_rebalance(session)`` — is today a rebalance session (the first trading session of its
      month)? The calendar rule lives with the data source, which knows the trading calendar.
    * ``signal(as_of)`` — the candidate set as a :class:`~dataplatform.query.pit.Dataset` of
      :class:`SectorRotationRecord`, already narrowed to the point-in-time investable universe and
      to names with a computable look-back return, a current price *and a resolvable sector as of
      the date*. It is returned as a ``Dataset`` (not a bare tuple) so the policy admits it through
      ``ctx.pit`` and the guard proves there is no membership or price leak.

    A test supplies an in-memory implementation; the ten-year run supplies one backed by L1 prices
    and the M3.9/M10.1 constituent snapshots (``membership_asof``) through the query layer. Either
    way the policy never reaches past this surface.
    """

    def is_rebalance(self, session: date) -> bool:
        """Whether ``session`` is a rebalance session."""

    def signal(self, as_of: date) -> Dataset[SectorRotationRecord]:
        """The PIT candidate set as of ``as_of``, as a guardable dataset."""


class SectorRotationPolicy:
    """Rank sectors by aggregate momentum, hold the top names within the top sectors (M10.3).

    Construct it with a :class:`SectorRotationData` source and :class:`SectorRotationParameters`. It
    satisfies :class:`backtest.replay.Policy`, so the replay engine drives it session by session; it
    reads data only through the injected ``ctx.pit`` (invariant #7), account state only through
    ``ctx.broker`` (the ``Broker`` protocol — invariant #5), and time only through ``ctx.clock``
    (B10). It holds no cost model and touches no store.

    Determinism: every branch is a pure function of the session's admitted candidates and the
    broker's reported book, with sector ties broken by sector name and name ties broken by ISIN, so
    a replay reproduces the decision exactly.
    """

    __slots__ = ("_data", "_params")

    def __init__(
        self, data: SectorRotationData, params: SectorRotationParameters | None = None
    ) -> None:
        self._data = data
        self._params = params if params is not None else SectorRotationParameters()

    def decide(self, ctx: SessionContext) -> SessionDecision:
        """Decide this session: a heartbeat off a rebalance, a full rebalance on one."""
        if not self._data.is_rebalance(ctx.session):
            return self._heartbeat(ctx)
        return self._rebalance(ctx)

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
        """Rank sectors, gate to the top-K, pick the top-N names within, and stage the orders."""
        # The one sanctioned read: admit the candidate dataset through the session's PIT scope. A
        # record whose sector (or momentum) is not yet knowable on ctx.session raises here
        # (invariant #7) — this is what stops a future sector map from reaching a past decision.
        candidates = ctx.pit.admit(self._data.signal(ctx.session))

        scores = rank_sectors(candidates)
        chosen_sectors = {score.sector for score in scores[: self._params.top_k]}
        in_sectors = [record for record in candidates if record.sector in chosen_sectors]
        ranked = sorted(in_sectors, key=lambda record: (-record.momentum, record.isin))
        chosen = ranked[: self._params.top_n]
        target = {record.isin: record for record in chosen}
        prices = {record.isin: record.price for record in chosen}

        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        sells = self._sells(held, target)
        buys, drift = self._buys(ctx, held, target, prices)

        orders = tuple(order for order, _ in (*sells, *buys))
        entries = tuple(self._entry(ctx, order, note) for order, note in (*sells, *buys))
        evidence = self._evidence(ctx.session, scores, chosen, drift)
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    # ── sells ─────────────────────────────────────────────────────────────────────────────────────

    def _sells(
        self, held: Mapping[str, Holding], target: Mapping[str, SectorRotationRecord]
    ) -> list[tuple[OrderRequest, str]]:
        """One full-quantity SELL for each settled holding no longer in the target, in ISIN order.

        A holding leaves the target either because its name dropped out of the top-N or because its
        whole sector fell out of the top-K — both are exits, liquidated in full, freeing the cash
        for the next rebalance so the strategy never spends unsettled sale proceeds.
        """
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            if isin in target:
                continue
            quantity = held[isin].quantity
            sells.append(
                (
                    OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="SECTOR_ROT"),
                    f"left the top-{self._params.top_k}-sector / top-{self._params.top_n}-name "
                    f"basket; liquidating {quantity} shares",
                )
            )
        return sells

    # ── buys ──────────────────────────────────────────────────────────────────────────────────────

    def _buys(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        target: Mapping[str, SectorRotationRecord],
        prices: Mapping[str, Decimal],
    ) -> tuple[list[tuple[OrderRequest, str]], Decimal]:
        """Whole-share buys toward the equal-weight target basket, sized from free cash.

        Budget is the cash *currently free* (``margins().available``) times ``buy_budget_fraction``
        — never sale proceeds staged this session, which have not settled. The allocation is the
        shared M4.7 greedy allocator, accounting for what is already held in the surviving names so
        it tops up toward equal weight rather than double-buying.
        """
        if not target:
            return [], _ZERO
        budget = ctx.broker.margins().available * self._params.buy_budget_fraction
        weights = _equal_weights(sorted(target))
        existing_value = {
            isin: Decimal(held[isin].quantity) * prices[isin] for isin in target if isin in held
        }
        allocation = simulate_sip_instalment(
            instalment=budget,
            targets=weights,
            prices=prices,
            existing_value=existing_value,
        )
        buys = [
            (
                order.to_order_request(exchange=Exchange.NSE, tag="SECTOR_ROT"),
                f"top-{self._params.top_k} sector "
                f"[{target[order.isin].sector}] name {target[order.isin].momentum:+} 12m; "
                f"buy {order.quantity} @ {order.price}",
            )
            for order in allocation.orders
        ]
        return buys, allocation.tracking_drift

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

    def _evidence(
        self,
        session: date,
        scores: Sequence[SectorScore],
        chosen: Sequence[SectorRotationRecord],
        tracking_drift: Decimal,
    ) -> EvidenceBundle:
        """The ranked sectors and the chosen names, as one content-addressed bundle (inv. #9)."""
        items: list[EvidenceItem] = [
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="L2",
                label="sector_mean_momentum",
                as_of=session,
                value=score.mean_momentum,
                detail={
                    "sector": score.sector,
                    "member_count": str(score.member_count),
                    "rank": str(rank),
                    "in_top_k": "true" if rank <= self._params.top_k else "false",
                },
            )
            for rank, score in enumerate(scores, start=1)
        ]
        items += [
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="L2",
                label="held_name_momentum",
                isin=record.isin,
                as_of=session,
                value=record.momentum,
                detail={"sector": record.sector, "price": str(record.price), "rank": str(rank)},
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
                text=(
                    f"equal-weight top-{self._params.top_n} names within the top-"
                    f"{self._params.top_k} momentum sectors"
                ),
            )
        )
        return EvidenceBundle(trading_date=session, actor=Actor.T0, items=tuple(items))


# ── sector scoring ──────────────────────────────────────────────────────────────────────────────


def rank_sectors(candidates: Sequence[SectorRotationRecord]) -> list[SectorScore]:
    """Score every sector by the mean momentum of its members present, best first (M10.3).

    Groups ``candidates`` by ``sector`` and scores each by the arithmetic mean of its members'
    momentum — mean, not sum, so a sector is judged by how strongly its typical member trends, not
    by how many of its names happen to be in the universe this session. Returns the scores sorted by
    descending mean momentum, ties broken by sector name so the order — and therefore which sectors
    fall inside the top-K cut — is deterministic across a replay.

    Assumes each candidate carries a non-empty sector (the record guarantees it). An empty candidate
    set yields an empty ranking, which the policy handles as "hold nothing this session".
    """
    grouped: dict[str, list[Decimal]] = {}
    for record in candidates:
        grouped.setdefault(record.sector, []).append(record.momentum)
    scores = [
        SectorScore(
            sector=sector,
            mean_momentum=sum(momenta, _ZERO) / Decimal(len(momenta)),
            member_count=len(momenta),
        )
        for sector, momenta in grouped.items()
    ]
    return sorted(scores, key=lambda score: (-score.mean_momentum, score.sector))


def _equal_weights(isins: list[str]) -> dict[str, Decimal]:
    """Equal weights over ``isins`` that sum to exactly 1 — the remainder lands on the last name.

    ``Decimal(1)/Decimal(n)`` does not generally have an exact ``n``-fold sum, so the last ISIN
    absorbs the rounding residue; the result sums to exactly 1 and every weight is positive, which
    is what the allocator's weight check (M4.7) requires. Deterministic in the (sorted) ISIN order.
    """
    if not isins:
        raise ValueError("cannot weight an empty basket")
    n = len(isins)
    each = (_ONE / Decimal(n)).quantize(_WEIGHT_QUANTUM)
    weights = dict.fromkeys(isins[:-1], each)
    weights[isins[-1]] = _ONE - each * (n - 1)
    return weights
