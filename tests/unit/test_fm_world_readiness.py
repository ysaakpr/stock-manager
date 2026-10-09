"""M17.11 — the M17 readiness probe no longer waits on L2; it waits on what waiting can fix.

Before: "L2 fresh for the 10 most liquid names" — and L2 is extended weekly, so the job hit its
120-minute cap every night. Now (`backtest.fm_world.overlay_readiness`): L1 has the session, and
the corporate-action overlay can price every action on the sampled names. L2's lag is logged, not
waited for. A split with no quantified terms (a curation or reconciliation can still land) is a
wait reason; a demerger (the engine never prices one) is not, because no wait ends it — the
Commons exclude that name instead. An unreadable store is a wait reason.

Offline: a synthetic lake under ``tmp_path`` and an in-memory store.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from analyst.commons import LakeCommonsSource
from backtest.fm_world import overlay_readiness
from dataplatform.clock import IST, FrozenClock
from dataplatform.corpactions import (
    ActionType,
    FaceValueTerms,
    UnquantifiedTerms,
    build_chain_for_isin,
)
from dataplatform.corpactions.taxonomy import Terms
from dataplatform.identity.master import Exchange
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.ingest.models import PriceRow
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import materialize_isin
from dataplatform.store.l2_overlay import OverlayActions

SESSION = date(2026, 10, 9)
L2_LAST = date(2026, 10, 1)
SPLIT_NAME = "INE002A01018"
UNQUANTIFIED = "INE009A01021"
DEMERGED = "INE0IQ001011"
NAMES = (SPLIT_NAME, UNQUANTIFIED, DEMERGED)
DAYS = [date(2026, 9, 28) + timedelta(days=i) for i in range(12)]
DAYS = [d for d in DAYS if d.weekday() < 5 and d <= SESSION]


def _action(isin: str, kind: ActionType, ex: date, terms: Terms) -> CorporateAction:
    return CorporateAction(
        isin=isin,
        ex_date=ex,
        action_type=kind,
        terms=terms,
        source="nse_corp_actions",
        raw_text=kind.value,
        knowable_date=ex - timedelta(days=3),
    )


class _Store:
    def __init__(self, actions: Sequence[CorporateAction], *, broken: bool = False) -> None:
        self.actions = tuple(actions)
        self.broken = broken

    def ex_between(self, after: date, through: date) -> OverlayActions:
        if self.broken:
            raise ConnectionError("store down")
        return OverlayActions(
            after=after,
            through=through,
            reconciled=tuple(a for a in self.actions if after < a.ex_date <= through),
            unreconciled=(),
        )


def _row(isin: str, day: date, close: Decimal) -> PriceRow:
    return PriceRow(
        isin=isin,
        symbol=isin[2:8],
        series="EQ",
        trade_date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        last=close,
        prev_close=close,
        total_traded_qty=1_000,
        total_traded_value=close * 1_000,
        total_trades=10,
    )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    def write(days: Sequence[date]) -> None:
        for day in days:
            close = Decimal("100") if day < date(2026, 10, 6) else Decimal("50")
            write_prices_raw(
                [_row(isin, day, close) for isin in NAMES],
                exchange=Exchange.NSE,
                data_root=tmp_path,
            )

    write([d for d in DAYS if d <= L2_LAST])
    for isin in NAMES:
        materialize_isin(
            isin, chain=build_chain_for_isin(isin, []), actions=[], data_root=tmp_path, curated=()
        )
    write([d for d in DAYS if d > L2_LAST])
    return tmp_path


def _probe(lake: Path, store: _Store | None) -> tuple[str, ...]:
    clock = FrozenClock(datetime(2026, 10, 9, 21, 0, tzinfo=IST))
    with LakeCommonsSource(clock=clock, data_root=lake, actions=store) as source:
        return overlay_readiness(source, frozenset(NAMES), DAYS)


def test_l2s_lag_alone_is_not_a_wait_reason(lake: Path) -> None:
    ex = date(2026, 10, 6)
    split = _action(
        SPLIT_NAME,
        ActionType.SPLIT,
        ex,
        FaceValueTerms(from_value=Decimal(10), to_value=Decimal(5)),
    )
    # Every sampled partition ends 10-01, the session is 10-09: the old probe waited here.
    assert _probe(lake, _Store([])) == ()
    # A priced split inside the lag is composed by the overlay, not waited for.
    assert _probe(lake, _Store([split])) == ()


def test_an_uncomputable_action_on_a_sampled_name_is_a_wait_reason(lake: Path) -> None:
    ex = date(2026, 10, 6)
    unpriced_split = _action(UNQUANTIFIED, ActionType.SPLIT, ex, UnquantifiedTerms())
    demerger = _action(DEMERGED, ActionType.DEMERGER, ex, UnquantifiedTerms())
    (reason,) = _probe(lake, _Store([unpriced_split, demerger]))
    assert UNQUANTIFIED in reason and "cannot price" in reason
    assert DEMERGED not in reason


def test_an_unreadable_store_is_a_wait_reason_and_no_store_is_too(lake: Path) -> None:
    (down,) = _probe(lake, _Store([], broken=True))
    assert "corporate-action overlay" in down and "unreadable" in down
    (unwired,) = _probe(lake, None)
    assert "no corporate-action store" in unwired
