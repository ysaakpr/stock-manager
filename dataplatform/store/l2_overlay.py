"""D4 · M17.11 — the L2 overlay: what L2 will hold for the sessions it has not been rebuilt over.

L2 ``prices_adjusted`` is extended by the weekly ``ca_refresh`` drain and by a manual ``l2_fill``,
so it lags L1 by up to a week (measured 2026-10-09: 1,715 traded names end 2026-10-01, L1 at
10-09). A reader that lays L2 over the raw L1 close, as the Commons do, shows a raw close for every
session L2 lacks, and a split that went ex inside that lag shows as a -50 % "return". This module
computes, for one ISIN and one window of sessions, the bars a rebuild would write, without writing
anything:

* **The events.** Every reconciled corporate action with ``ex_after < ex_date <= as_of`` and
  ``knowable_date <= as_of`` (PIT: an action not yet knowable never adjusts a price), plus the
  sourced curated rows (`corpactions.manual_actions`) L2 composes, deduplicated against the feeds
  by `l2.curated_actions`. Each is classified by what the engine does with it
  (:class:`OverlayKind`).
* **The factors.** A split or bonus is composed by `factors.with_events`, the engine's own
  composition, never re-derived here. The cumulative factor for a bar is L2's own
  ``cum_price_factor`` for that bar (1 for a bar L2 does not hold) folded with the new events by
  `FactorChain.price_factor_asof(base=...)`: the same running product, in the same order, that a
  rebuild over the extended chain computes. Values are rounded by the writer's own quantizers
  (`l2.quantize_price` and friends).
* **What cannot be priced.** The engine composes no price factor for a merger, demerger, scheme
  or DVR conversion (a unit factor and a structural break: the return across it is undefined,
  ``factors`` module docstring), none for a rights issue (not in the chain), and refuses a split or
  bonus without quantified terms. An unreconciled split or bonus never reaches the chain at all.
  Each of these is returned as an event of its kind, so the caller can flag the ISIN and keep it
  out of anything that reads a return across it, rather than show the raw step.

**Equal to a rebuild.** For an ISIN whose L2 stops at ``L`` and an event set ``E`` ex after
``L``, the bars returned equal what `l2.materialize_isin` writes over the same L1 bars with ``E``
composed into L2's chain, Decimal for Decimal (``tests/unit/test_l2_overlay.py``). That holds
whenever L2's stored cumulative factor is the exact product (every factor that terminates within
the writer's eighteen places, which is every split and every bonus whose total share count has no
prime factor but 2 and 5). For a chain carrying a non-terminating factor (a 1:2 bonus's 2/3) the
stored factor is already rounded, and the result can differ from a rebuild by one unit in the
eighteenth place of the factor; the four-place price differs only when the exact value sits
within ~1e-13 of a rounding boundary.

What it never does: write L0, L1 or L2, invent a factor, compose an implied split (that needs bars
after the ex-date, which a session cannot see), or read a corporate action knowable after the
session.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import duckdb

from dataplatform.corpactions.factors import (
    PRICE_EVENT_TYPES,
    STRUCTURAL_BREAK_TYPES,
    FactorChain,
    FactorError,
    with_events,
)
from dataplatform.corpactions.manual_actions import default_manual_actions
from dataplatform.corpactions.taxonomy import ActionType
from dataplatform.logging import get_logger
from dataplatform.store.l2 import (
    PRICES_ADJUSTED_DATASET,
    AdjustedBar,
    RawBar,
    curated_actions,
    quantize_factor,
    quantize_price,
    quantize_volume,
)
from dataplatform.store.paths import l2_isin_partition_path

if TYPE_CHECKING:
    from dataplatform.config import Settings
    from dataplatform.ingest.corp_actions import CorporateAction

__all__ = [
    "EXCLUDING_KINDS",
    "LEVEL_ACTION_TYPES",
    "OverlaidBar",
    "OverlayActionSource",
    "OverlayActions",
    "OverlayError",
    "OverlayEvent",
    "OverlayKind",
    "StoreOverlayActions",
    "l2_last_dates",
    "overlay_bars",
    "plan_events",
    "stale_l2_events",
]

_LOG = get_logger(__name__)
_ONE: Final = Decimal(1)

#: The action types that move a price level on the ex-date. Dividends do not: they are absent from
#: the price-adjusted series by the engine's convention, so the overlay ignores them as L2 does.
LEVEL_ACTION_TYPES: Final[frozenset[ActionType]] = (
    PRICE_EVENT_TYPES | STRUCTURAL_BREAK_TYPES | frozenset({ActionType.RIGHTS})
)
#: L2 relative mismatch beyond which a split L2's span covers is judged absent from its factors.
#: Factors are stored at eighteen places; a present factor agrees to ~1e-17, a missing one is off
#: by its whole ratio (0.5, 0.2), so the bound only has to separate those two.
_STALE_TOLERANCE: Final = Decimal("1e-9")


class OverlayError(ValueError):
    """L2's own factors disagree with the events the overlay was handed: no honest bar exists."""


class OverlayKind(StrEnum):
    """What the L2 engine does with one corporate action, and so what the overlay can do."""

    #: A split or bonus with quantified terms: its factor is composed, history back-adjusted.
    APPLIED = "APPLIED"
    #: A merger, demerger, scheme or DVR conversion: a unit factor and a structural break. The
    #: level gap is real and the return across it is undefined.
    BREAK = "BREAK"
    #: A rights issue: the engine composes no factor for it at all.
    UNPRICED = "UNPRICED"
    #: A split or bonus the engine cannot price (no quantified terms), one not yet reconciled, or
    #: one L2's span covers without its factor. Waiting can fix these: a curation, a
    #: reconciliation or a rebuild.
    UNCOMPUTABLE = "UNCOMPUTABLE"


#: The kinds that leave a raw step in the series a return would be read across.
EXCLUDING_KINDS: Final[frozenset[OverlayKind]] = frozenset(
    {OverlayKind.BREAK, OverlayKind.UNPRICED, OverlayKind.UNCOMPUTABLE}
)


@dataclass(frozen=True, slots=True)
class OverlayEvent:
    """One corporate action inside an overlay window and what the overlay does with it.

    ``action`` is the composed action for an ``APPLIED`` event and ``None`` otherwise.
    """

    isin: str
    ex_date: date
    action_type: ActionType
    knowable_date: date
    kind: OverlayKind
    detail: str
    action: CorporateAction | None = None


@dataclass(frozen=True, slots=True)
class OverlaidBar:
    """One bar as a rebuild would write it: the adjusted OHLCV and the factors that produced it.

    ``adjusted`` is whether the overlay changed it from what L2 (or the raw bar, where L2 has
    none) holds today. There is no total-return close: dividends are not part of the overlay.
    """

    isin: str
    exchange: str
    trade_date: date
    adj_open: Decimal
    adj_high: Decimal
    adj_low: Decimal
    adj_close: Decimal
    adj_volume: Decimal
    cum_price_factor: Decimal
    cum_qty_factor: Decimal
    adjusted: bool


@dataclass(frozen=True, slots=True)
class OverlayActions:
    """The store's level actions ex in ``(after, through]``: reconciled, and not."""

    after: date
    through: date
    reconciled: tuple[CorporateAction, ...]
    unreconciled: tuple[CorporateAction, ...]

    def by_isin(self) -> tuple[dict[str, list[CorporateAction]], dict[str, list[CorporateAction]]]:
        """The two sets keyed by ISIN."""
        reconciled: dict[str, list[CorporateAction]] = {}
        unreconciled: dict[str, list[CorporateAction]] = {}
        for action in self.reconciled:
            reconciled.setdefault(action.isin, []).append(action)
        for action in self.unreconciled:
            unreconciled.setdefault(action.isin, []).append(action)
        return reconciled, unreconciled


class OverlayActionSource(Protocol):
    """Where the overlay reads corporate actions. Raises when the store cannot be read."""

    def ex_between(self, after: date, through: date) -> OverlayActions:
        """Every level action with ``after < ex_date <= through``, any knowable date."""
        ...


class StoreOverlayActions:
    """`OverlayActionSource` over the Postgres ``corporate_actions`` table, read-only.

    Reads through `load_reconciled_actions` (the factor chain's own door) and
    `load_unreconciled_actions`, one query each per window, and keeps the answer for the life of
    the object: one session's build asks for the same window several times.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: dict[tuple[date, date], OverlayActions] = {}

    def ex_between(self, after: date, through: date) -> OverlayActions:
        key = (after, through)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        from dataplatform.corpactions.reconcile import (
            load_reconciled_actions,
            load_unreconciled_actions,
        )
        from dataplatform.store.db import connection

        with connection(self._settings) as conn:
            reconciled = tuple(
                a
                for a in load_reconciled_actions(conn, ex_after=after, ex_through=through)
                if a.action_type in LEVEL_ACTION_TYPES
            )
            unreconciled = load_unreconciled_actions(
                conn,
                ex_after=after,
                ex_through=through,
                action_types=PRICE_EVENT_TYPES | {ActionType.RIGHTS} | STRUCTURAL_BREAK_TYPES,
            )
        found = OverlayActions(
            after=after, through=through, reconciled=reconciled, unreconciled=unreconciled
        )
        _LOG.info(
            "l2_overlay.actions_read",
            after=after.isoformat(),
            through=through.isoformat(),
            reconciled=len(reconciled),
            unreconciled=len(unreconciled),
        )
        self._cache[key] = found
        return found


def plan_events(
    isin: str,
    *,
    recorded: Iterable[CorporateAction],
    unreconciled: Iterable[CorporateAction] = (),
    curated: Sequence[CorporateAction] | None = None,
    after: date,
    as_of: date,
) -> tuple[OverlayEvent, ...]:
    """Classify ``isin``'s level actions ex in ``(after, as_of]`` and knowable by ``as_of``.

    What it does: keeps the window and the PIT bound (an action whose ``knowable_date`` is after
    ``as_of`` is dropped, logged, and never adjusts anything), adds the curated rows a feed has
    not published (``curated=None`` reads the repo's file), and classifies each action by what the
    engine does with it. A split or bonus is ``APPLIED`` only if `factors.with_events` composes
    it; one it refuses is ``UNCOMPUTABLE``. An unreconciled row is an event too: an unreconciled
    split is ``UNCOMPUTABLE`` unless a reconciled row already states the same event.

    What it never does: compute a factor of its own, or read anything but its arguments and, for
    ``curated=None``, the curated YAML.
    """

    def admitted(action: CorporateAction) -> bool:
        if not after < action.ex_date <= as_of or action.action_type not in LEVEL_ACTION_TYPES:
            return False
        if action.knowable_date > as_of:
            _LOG.info(
                "l2_overlay.not_yet_knowable",
                isin=isin,
                ex_date=action.ex_date.isoformat(),
                action_type=action.action_type.value,
                knowable_date=action.knowable_date.isoformat(),
                as_of=as_of.isoformat(),
            )
            return False
        return True

    feed = tuple(a for a in recorded if admitted(a))
    rows = (
        tuple(a.as_action() for a in default_manual_actions().actions_for(isin))
        if curated is None
        else tuple(curated)
    )
    manual = curated_actions(isin, feed, tuple(a for a in rows if admitted(a))) if rows else ()
    events: list[OverlayEvent] = []
    stated = {(a.ex_date, a.action_type) for a in (*feed, *manual)}
    for action in (*feed, *manual):
        events.append(_classify(isin, action))
    for action in unreconciled:
        if not admitted(action) or (action.ex_date, action.action_type) in stated:
            continue
        stated.add((action.ex_date, action.action_type))
        event = _classify(isin, action)
        if event.kind is OverlayKind.APPLIED:
            event = OverlayEvent(
                isin=isin,
                ex_date=action.ex_date,
                action_type=action.action_type,
                knowable_date=action.knowable_date,
                kind=OverlayKind.UNCOMPUTABLE,
                detail=(
                    f"{action.action_type.value} ex {action.ex_date.isoformat()} is not "
                    "reconciled, so no factor is composed for it"
                ),
            )
        events.append(event)
    return tuple(sorted(events, key=lambda e: (e.ex_date, e.action_type.value, e.kind.value)))


def _classify(isin: str, action: CorporateAction) -> OverlayEvent:
    def event(
        kind: OverlayKind, detail: str, composed: CorporateAction | None = None
    ) -> OverlayEvent:
        return OverlayEvent(
            isin=isin,
            ex_date=action.ex_date,
            action_type=action.action_type,
            knowable_date=action.knowable_date,
            kind=kind,
            detail=detail,
            action=composed,
        )

    what = f"{action.action_type.value} ex {action.ex_date.isoformat()}"
    if action.action_type in PRICE_EVENT_TYPES:
        try:
            with_events(FactorChain(isin=isin), [action])
        except FactorError as exc:
            return event(OverlayKind.UNCOMPUTABLE, f"{what}: the engine cannot price it ({exc})")
        return event(OverlayKind.APPLIED, f"{what}: factor composed", action)
    if action.action_type in STRUCTURAL_BREAK_TYPES:
        return event(
            OverlayKind.BREAK,
            f"{what}: a structural break; the engine composes no price factor, so the move "
            "across it is not a return",
        )
    return event(
        OverlayKind.UNPRICED,
        f"{what}: the engine composes no rights factor, so the move across it is not a return",
    )


def overlay_bars(
    isin: str,
    *,
    events: Sequence[CorporateAction],
    raw_bars: Sequence[RawBar],
    l2_bars: Mapping[date, AdjustedBar],
) -> tuple[OverlaidBar, ...]:
    """The bars a rebuild with ``events`` composed into L2's chain writes for ``raw_bars``.

    What it does: composes ``events`` (the ``APPLIED`` actions ex after L2's last bar) with
    `factors.with_events`. For each raw bar, the cumulative factor is L2's own for that bar (1 where
    L2 holds none) folded with the new events; the bar is that factor times the raw values,
    rounded by the writer's quantizers. A bar no new event scales, which L2 holds, is L2's bar
    unchanged. One venue: every bar in both inputs is the same exchange's.

    What it assumes: ``l2_bars`` are L2's bars of the window for the same venue, and every event
    is ex after the last of them, so L2's factors know none of them. When L2's last bar already
    carries a factor for later events (L2 was built from a chain that knew them), that factor must
    equal the events' own product, and then L2's bars stand as they are; anything else raises
    `OverlayError`, because no bar computed from the two would be what a rebuild writes.

    What it never does: re-derive a factor, read I/O, or emit a bar for a date ``raw_bars`` lacks.
    """
    chain = with_events(FactorChain(isin=isin), events) if events else FactorChain(isin=isin)
    composed = False
    if l2_bars and chain.rows:
        last = l2_bars[max(l2_bars)]
        held = (last.cum_price_factor, last.cum_qty_factor)
        if held != (_ONE, _ONE):
            expected = (
                quantize_factor(chain.price_factor_asof(last.trade_date)),
                quantize_factor(chain.qty_factor_asof(last.trade_date)),
            )
            if held != expected:
                raise OverlayError(
                    f"{isin}: L2's last bar {last.trade_date.isoformat()} carries factors {held} "
                    f"for events after it, but the overlay's events compose to {expected}"
                )
            composed = True
    out: list[OverlaidBar] = []
    for raw in sorted(raw_bars, key=lambda b: b.trade_date):
        day = raw.trade_date
        l2 = l2_bars.get(day)
        scaled = any(row.ex_date > day for row in chain.rows)
        if l2 is not None and (composed or not scaled):
            out.append(
                OverlaidBar(
                    isin=isin,
                    exchange=l2.exchange,
                    trade_date=day,
                    adj_open=l2.adj_open,
                    adj_high=l2.adj_high,
                    adj_low=l2.adj_low,
                    adj_close=l2.adj_close,
                    adj_volume=l2.adj_volume,
                    cum_price_factor=l2.cum_price_factor,
                    cum_qty_factor=l2.cum_qty_factor,
                    adjusted=False,
                )
            )
            continue
        base_price = _ONE if l2 is None else l2.cum_price_factor
        base_qty = _ONE if l2 is None else l2.cum_qty_factor
        price = chain.price_factor_asof(day, base=base_price)
        qty = chain.qty_factor_asof(day, base=base_qty)
        out.append(
            OverlaidBar(
                isin=isin,
                exchange=raw.exchange,
                trade_date=day,
                adj_open=quantize_price(raw.open * price),
                adj_high=quantize_price(raw.high * price),
                adj_low=quantize_price(raw.low * price),
                adj_close=quantize_price(raw.close * price),
                adj_volume=quantize_volume(Decimal(raw.volume) * qty),
                cum_price_factor=quantize_factor(price),
                cum_qty_factor=quantize_factor(qty),
                adjusted=scaled,
            )
        )
    return tuple(out)


def stale_l2_events(
    isin: str, events: Iterable[OverlayEvent], l2_bars: Mapping[date, AdjustedBar]
) -> tuple[OverlayEvent, ...]:
    """The ``APPLIED`` events inside L2's span whose factor L2's bars do not carry.

    An action ingested after L2 was last built over its ex-date leaves L2 with the raw step at
    that ex-date until the drain rebuilds it. That is not a lag the overlay can extend over, so
    each such event comes back ``UNCOMPUTABLE`` (a rebuild is owed) for the caller to flag. Judged
    on L2's own bars either side of the ex-date: the step in ``cum_price_factor`` across it must
    be the event's factor. An event with no L2 bar on one side is not judged.
    """
    days = sorted(l2_bars)
    out: list[OverlayEvent] = []
    applied = [e for e in events if e.kind is OverlayKind.APPLIED and e.action is not None]
    by_date: dict[date, list[CorporateAction]] = {}
    for event in applied:
        assert event.action is not None
        by_date.setdefault(event.ex_date, []).append(event.action)
    for ex_date, actions in sorted(by_date.items()):
        before = [d for d in days if d < ex_date]
        after = [d for d in days if d >= ex_date]
        if not before or not after:
            continue
        factor = with_events(FactorChain(isin=isin), actions).rows[0].price_factor
        prev, at = l2_bars[before[-1]], l2_bars[after[0]]
        expected = at.cum_price_factor * factor
        if abs(prev.cum_price_factor - expected) <= _STALE_TOLERANCE * expected:
            continue
        for event in applied:
            if event.ex_date == ex_date:
                out.append(
                    OverlayEvent(
                        isin=isin,
                        ex_date=ex_date,
                        action_type=event.action_type,
                        knowable_date=event.knowable_date,
                        kind=OverlayKind.UNCOMPUTABLE,
                        detail=(
                            f"{event.action_type.value} ex {ex_date.isoformat()}: L2 holds the "
                            "sessions either side without its factor; a rebuild is owed"
                        ),
                    )
                )
    return tuple(out)


def l2_last_dates(
    con: duckdb.DuckDBPyConnection,
    isins: Iterable[str],
    *,
    exchange: str = "NSE",
    data_root: Path | None = None,
) -> dict[str, date]:
    """The last ``exchange`` session each ISIN's L2 partition holds; an ISIN with none is absent."""
    files = {
        isin: path
        for isin in sorted(set(isins))
        if (
            path := l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=data_root)
        ).exists()
    }
    if not files:
        return {}
    rows = con.execute(
        "SELECT isin, max(trade_date) FROM read_parquet($files) WHERE exchange = $exchange "
        "GROUP BY isin",
        {"files": [str(p) for p in files.values()], "exchange": exchange},
    ).fetchall()
    return {str(isin): last for isin, last in rows if last is not None}
