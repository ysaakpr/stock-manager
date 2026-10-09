"""M17.11 — the L2 overlay equals a rebuild, and what it cannot price it says so.

L2 lags L1 by up to a week. For the sessions it lacks, the Commons used to read the raw L1 close,
so a split that went ex inside the lag showed as a -50 % session. The overlay
(`dataplatform.store.l2_overlay`) computes what a rebuild will hold. These tests pin that:

- a synthetic ISIN with an old split already in L2, then a demerger, a bonus and a split inside
  the overlay window: the overlay's bars equal `materialize_isin` over the same L1 bars with the
  full chain, Decimal for Decimal, on every column a rebuild writes but the total-return close;
- the same for generated split/bonus ratios, prices and ex-dates (a property test);
- an inverted factor fails the comparison (the guard the equality test is worth nothing without);
- PIT: an action knowable after the session never reaches the overlay;
- what the engine cannot price (a demerger, a rights issue, an unquantified split, an
  unreconciled split, a split L2's span covers without its factor) comes back as that kind.

Offline: synthetic L1 under ``tmp_path``, no database, no network.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from analyst.commons.sheets import PriceOverlayNote, SourceUnavailableError
from analyst.commons.sources import LakeCommonsSource
from dataplatform.clock import IST, FrozenClock
from dataplatform.corpactions import (
    ActionType,
    FaceValueTerms,
    RatioTerms,
    UnquantifiedTerms,
    build_chain_for_isin,
)
from dataplatform.corpactions.taxonomy import DividendKind, DividendTerms, ExchangeRatioTerms, Terms
from dataplatform.identity.master import Exchange
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.ingest.models import PriceRow
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import (  # the writer's own bytes
    PRICES_ADJUSTED_DATASET,
    AdjustedBar,
    RawBar,
    _bars_to_table,
    _write_table,
    materialize_isin,
    open_connection,
    read_adjusted,
    read_raw_bars_from_l1,
)
from dataplatform.store.l2_overlay import (
    L2Tail,
    OverlaidBar,
    OverlayActions,
    OverlayError,
    OverlayKind,
    l2_tails,
    overlay_bars,
    plan_events,
    stale_l2_events,
)
from dataplatform.store.paths import l2_isin_partition_path

ISIN = "INE0IQ001011"
OLD_SPLIT = date(2026, 6, 1)
L2_LAST = date(2026, 9, 30)
SESSION = date(2026, 10, 9)
DEMERGER = date(2026, 10, 6)
BONUS = date(2026, 10, 5)
SPLIT = date(2026, 10, 8)
_COLUMNS = (
    "trade_date",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "adj_volume",
    "cum_price_factor",
    "cum_qty_factor",
)


def _weekdays(start: date, end: date) -> list[date]:
    out: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


DAYS = _weekdays(date(2026, 5, 18), SESSION)


def action(
    kind: ActionType, ex_date: date, terms: Terms, *, knowable: date | None = None
) -> CorporateAction:
    return CorporateAction(
        isin=ISIN,
        ex_date=ex_date,
        action_type=kind,
        terms=terms,
        source="nse_corp_actions",
        raw_text=kind.value.title(),
        knowable_date=knowable or ex_date - timedelta(days=7),
    )


def split(ex_date: date, frm: str, to: str, **kw: date) -> CorporateAction:
    return action(
        ActionType.SPLIT,
        ex_date,
        FaceValueTerms(from_value=Decimal(frm), to_value=Decimal(to)),
        **kw,
    )


def bonus(ex_date: date, new: str, held: str, **kw: date) -> CorporateAction:
    return action(
        ActionType.BONUS,
        ex_date,
        RatioTerms(new_shares=Decimal(new), held_shares=Decimal(held)),
        **kw,
    )


def demerger(ex_date: date, **kw: date) -> CorporateAction:
    return action(ActionType.DEMERGER, ex_date, UnquantifiedTerms(), **kw)


def _price_rows(days: Sequence[date], closes: dict[date, Decimal]) -> dict[date, PriceRow]:
    rows: dict[date, PriceRow] = {}
    for i, day in enumerate(days):
        close = closes[day]
        rows[day] = PriceRow(
            isin=ISIN,
            symbol="VERANDA",
            series="EQ",
            trade_date=day,
            open=(close * Decimal("1.0123")).quantize(Decimal("0.01")),
            high=(close * Decimal("1.0377")).quantize(Decimal("0.01")),
            low=(close * Decimal("0.9719")).quantize(Decimal("0.01")),
            close=close,
            last=close,
            prev_close=close,
            total_traded_qty=100_003 + 7 * i,
            total_traded_value=close * 100_003,
            total_trades=10,
        )
    return rows


def _closes(steps: Sequence[tuple[date, Decimal]], seed: int = 0) -> dict[date, Decimal]:
    """A wandering close that steps down by each (ex_date, price ratio) on its ex-date."""
    level = Decimal("1000.00")
    out: dict[date, Decimal] = {}
    for i, day in enumerate(DAYS):
        for ex_date, ratio in steps:
            if ex_date == day:
                level *= ratio
        wobble = Decimal(((i * 37 + seed * 11) % 23) - 11) / Decimal(1000)
        out[day] = (level * (1 + wobble)).quantize(Decimal("0.01"))
    return out


def _write_l1(root: Path, rows: dict[date, PriceRow], days: Sequence[date]) -> None:
    for day in days:
        write_prices_raw([rows[day]], exchange=Exchange.NSE, data_root=root)


def _l2(root: Path) -> dict[date, AdjustedBar]:
    return {b.trade_date: b for b in read_adjusted(ISIN, data_root=root) if b.exchange == "NSE"}


def _row(bar: object) -> tuple[OverlaidBar, ...]:
    return tuple(getattr(bar, c) for c in _COLUMNS)


def _scenario(
    root: Path,
    old: Sequence[CorporateAction],
    window: Sequence[CorporateAction],
    steps: Sequence[tuple[date, Decimal]],
    *,
    seed: int = 0,
) -> tuple[dict[date, AdjustedBar], tuple[RawBar, ...], tuple[AdjustedBar, ...]]:
    """L2 built over L1 through L2_LAST with ``old``; then L1 through SESSION and a full rebuild.

    Returns L2 as the lag left it, the raw bars the overlay reads, and the rebuilt partition.
    """
    rows = _price_rows(DAYS, _closes(steps, seed))
    lagged = [d for d in DAYS if d <= L2_LAST]
    _write_l1(root, rows, lagged)
    materialize_isin(
        ISIN, chain=build_chain_for_isin(ISIN, old), actions=old, data_root=root, curated=()
    )
    before = _l2(root)
    _write_l1(root, rows, [d for d in DAYS if d > L2_LAST])
    everything = (*old, *window)
    materialize_isin(
        ISIN,
        chain=build_chain_for_isin(ISIN, everything),
        actions=everything,
        data_root=root,
        curated=(),
    )
    rebuilt = read_adjusted(ISIN, data_root=root)
    raw = read_raw_bars_from_l1(ISIN, data_root=root)
    return before, raw, rebuilt


def _overlay(
    before: dict[date, AdjustedBar], raw: Sequence[RawBar], window: Sequence[CorporateAction]
) -> tuple[OverlaidBar, ...]:
    events = plan_events(ISIN, recorded=window, curated=(), after=L2_LAST, as_of=SESSION)
    applied = [e.action for e in events if e.kind is OverlayKind.APPLIED and e.action is not None]
    return overlay_bars(ISIN, events=applied, raw_bars=raw, l2_bars=before)


WINDOW = (bonus(BONUS, "1", "2"), demerger(DEMERGER), split(SPLIT, "10", "5"))
# What the price does on each ex-date: a 1:2 bonus (2/3), the demerger shedding 86 %, a 2:1 split.
STEPS = (
    (OLD_SPLIT, Decimal("0.2")),
    (BONUS, Decimal(2) / Decimal(3)),
    (DEMERGER, Decimal("0.14")),
    (SPLIT, Decimal("0.5")),
)


def test_the_overlay_equals_a_rebuild_across_a_split_a_bonus_and_a_demerger(
    tmp_path: Path,
) -> None:
    before, raw, rebuilt = _scenario(tmp_path, (split(OLD_SPLIT, "10", "2"),), WINDOW, STEPS)
    assert max(before) == L2_LAST
    overlaid = _overlay(before, raw, WINDOW)
    assert [_row(b) for b in overlaid] == [_row(b) for b in rebuilt]
    # The history moved: every bar before the bonus carries both new factors on top of L2's.
    first = overlaid[0]
    assert first.cum_price_factor == Decimal("0.066666666666666667")
    assert all(b.adjusted for b in overlaid if b.trade_date < SPLIT)
    assert not any(b.adjusted for b in overlaid if b.trade_date >= SPLIT)


def test_an_inverted_factor_does_not_equal_the_rebuild(tmp_path: Path) -> None:
    before, raw, rebuilt = _scenario(tmp_path, (split(OLD_SPLIT, "10", "2"),), WINDOW, STEPS)
    # The same events with the split read the wrong way round (5 -> 10, a consolidation).
    inverted = (WINDOW[0], WINDOW[1], split(SPLIT, "5", "10"))
    overlaid = _overlay(before, raw, inverted)
    assert [_row(b) for b in overlaid] != [_row(b) for b in rebuilt]
    # And the raw fallback the overlay replaces is not the rebuild either.
    raw_fallback = [
        before[b.trade_date].adj_close if b.trade_date in before else b.close for b in raw
    ]
    assert raw_fallback != [b.adj_close for b in rebuilt]


#: Splits and bonuses whose factors terminate within L2's eighteen places, so a stored L2 factor
#: is the exact product and equality holds on every column (module docstring of l2_overlay).
_RATIOS = st.sampled_from(
    [
        ("SPLIT", "10", "2"),
        ("SPLIT", "10", "5"),
        ("SPLIT", "10", "1"),
        ("SPLIT", "2", "1"),
        ("BONUS", "1", "1"),
        ("BONUS", "1", "4"),
        ("BONUS", "3", "2"),
        ("BONUS", "1", "2"),
        ("BONUS", "2", "1"),
    ]
)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    events=st.lists(
        st.tuples(_RATIOS, st.integers(min_value=0, max_value=6)),
        min_size=1,
        max_size=3,
        unique_by=lambda e: e[1],
    ),
    seed=st.integers(min_value=0, max_value=50),
)
def test_property_overlay_equals_rebuild_for_generated_events(
    tmp_path_factory: pytest.TempPathFactory,
    events: list[tuple[tuple[str, str, str], int]],
    seed: int,
) -> None:
    root = tmp_path_factory.mktemp("overlay")
    lag = [d for d in DAYS if d > L2_LAST]
    window: list[CorporateAction] = []
    steps: list[tuple[date, Decimal]] = [(OLD_SPLIT, Decimal("0.5"))]
    for (kind, a, b), index in events:
        ex_date = lag[index]
        if kind == "SPLIT":
            window.append(split(ex_date, a, b))
            steps.append((ex_date, Decimal(b) / Decimal(a)))
        else:
            window.append(bonus(ex_date, a, b))
            steps.append((ex_date, Decimal(b) / (Decimal(a) + Decimal(b))))
    before, raw, rebuilt = _scenario(root, (split(OLD_SPLIT, "2", "1"),), window, steps, seed=seed)
    assert [_row(b) for b in _overlay(before, raw, window)] == [_row(b) for b in rebuilt]


def test_no_action_knowable_after_the_session_reaches_the_overlay() -> None:
    late = split(SPLIT, "10", "5", knowable=SESSION + timedelta(days=1))
    assert plan_events(ISIN, recorded=[late], curated=(), after=L2_LAST, as_of=SESSION) == ()
    # Knowable on the session itself is in.
    today = split(SPLIT, "10", "5", knowable=SESSION)
    (event,) = plan_events(ISIN, recorded=[today], curated=(), after=L2_LAST, as_of=SESSION)
    assert event.kind is OverlayKind.APPLIED
    # And the window bounds hold: ex on L2's last day is L2's, ex after the session is not yet.
    assert (
        plan_events(
            ISIN, recorded=[split(L2_LAST, "10", "5")], curated=(), after=L2_LAST, as_of=SESSION
        )
        == ()
    )
    assert (
        plan_events(
            ISIN,
            recorded=[split(SESSION + timedelta(days=3), "10", "5")],
            curated=(),
            after=L2_LAST,
            as_of=SESSION,
        )
        == ()
    )


def test_what_the_engine_cannot_price_is_named_by_kind() -> None:
    rights = action(ActionType.RIGHTS, BONUS, UnquantifiedTerms())
    merger = action(
        ActionType.MERGER,
        BONUS,
        ExchangeRatioTerms(shares_received=Decimal(1), shares_held=Decimal(2)),
    )
    unquantified = action(ActionType.SPLIT, SPLIT, UnquantifiedTerms())
    pending = split(date(2026, 10, 7), "10", "1")
    dividend = action(
        ActionType.DIVIDEND,
        BONUS,
        DividendTerms(dividend_kind=DividendKind.FINAL, amount_inr=Decimal(5)),
    )
    kinds = {
        (e.action_type, e.kind)
        for e in plan_events(
            ISIN,
            recorded=[demerger(DEMERGER), rights, merger, unquantified, dividend],
            unreconciled=[pending],
            curated=(),
            after=L2_LAST,
            as_of=SESSION,
        )
    }
    assert kinds == {
        (ActionType.DEMERGER, OverlayKind.BREAK),
        (ActionType.MERGER, OverlayKind.BREAK),
        (ActionType.RIGHTS, OverlayKind.UNPRICED),
        (ActionType.SPLIT, OverlayKind.UNCOMPUTABLE),
    }
    # An unreconciled row that a reconciled one already states is not a second event.
    stated = split(date(2026, 10, 7), "10", "1")
    (event,) = plan_events(
        ISIN, recorded=[stated], unreconciled=[pending], curated=(), after=L2_LAST, as_of=SESSION
    )
    assert event.kind is OverlayKind.APPLIED


def test_a_demerger_alone_leaves_the_level_as_a_rebuild_does(tmp_path: Path) -> None:
    """The engine composes a unit factor for a demerger: the overlay equals the rebuild, and the
    level gap stays. That is why the caller must flag it rather than read a return across it."""
    window = (demerger(DEMERGER),)
    steps = ((DEMERGER, Decimal("0.14")),)
    before, raw, rebuilt = _scenario(tmp_path, (), window, steps)
    events = plan_events(ISIN, recorded=window, curated=(), after=L2_LAST, as_of=SESSION)
    assert [e.kind for e in events] == [OverlayKind.BREAK]
    overlaid = overlay_bars(ISIN, events=[], raw_bars=raw, l2_bars=before)
    assert [_row(b) for b in overlaid] == [_row(b) for b in rebuilt]
    closes = {b.trade_date: b.adj_close for b in overlaid}
    prev = max(d for d in closes if d < DEMERGER)
    assert closes[DEMERGER] / closes[prev] < Decimal("0.2")


def test_l2_already_carrying_the_window_factor_stands_and_a_disagreeing_one_raises(
    tmp_path: Path,
) -> None:
    rows = _price_rows(DAYS, _closes(((SPLIT, Decimal("0.5")),)))
    _write_l1(tmp_path, rows, [d for d in DAYS if d <= L2_LAST])
    known = split(SPLIT, "10", "5")
    # L2 built from a chain that already knew the coming split (its ex-date after L2's last bar).
    materialize_isin(
        ISIN,
        chain=build_chain_for_isin(ISIN, [known]),
        actions=[known],
        data_root=tmp_path,
        curated=(),
    )
    before = _l2(tmp_path)
    _write_l1(tmp_path, rows, [d for d in DAYS if d > L2_LAST])
    materialize_isin(
        ISIN,
        chain=build_chain_for_isin(ISIN, [known]),
        actions=[known],
        data_root=tmp_path,
        curated=(),
    )
    rebuilt = read_adjusted(ISIN, data_root=tmp_path)
    raw = read_raw_bars_from_l1(ISIN, data_root=tmp_path)
    overlaid = overlay_bars(ISIN, events=[known], raw_bars=raw, l2_bars=before)
    assert [_row(b) for b in overlaid] == [_row(b) for b in rebuilt]
    with pytest.raises(OverlayError):
        overlay_bars(ISIN, events=[split(SPLIT, "10", "2")], raw_bars=raw, l2_bars=before)


def test_a_split_inside_l2s_span_without_its_factor_is_uncomputable(tmp_path: Path) -> None:
    inside = date(2026, 9, 15)
    rows = _price_rows(DAYS, _closes(((inside, Decimal("0.5")),)))
    _write_l1(tmp_path, rows, [d for d in DAYS if d <= L2_LAST])
    # L2 built before the split was ingested: its raw step is still in the partition.
    materialize_isin(
        ISIN,
        chain=build_chain_for_isin(ISIN, []),
        actions=[],
        data_root=tmp_path,
        curated=(),
        infer_splits=False,
    )
    stale = _l2(tmp_path)
    events = plan_events(
        ISIN, recorded=[split(inside, "10", "5")], curated=(), after=date(2026, 5, 1), as_of=SESSION
    )
    (flag,) = stale_l2_events(ISIN, events, stale)
    assert flag.kind is OverlayKind.UNCOMPUTABLE and "rebuild is owed" in flag.detail
    # Once L2 carries it, nothing is flagged.
    materialize_isin(
        ISIN,
        chain=build_chain_for_isin(ISIN, [split(inside, "10", "5")]),
        actions=[split(inside, "10", "5")],
        data_root=tmp_path,
        curated=(),
    )
    assert stale_l2_events(ISIN, events, _l2(tmp_path)) == ()


def test_l2_tails_read_each_partitions_last_nse_bar(tmp_path: Path) -> None:
    rows = _price_rows(DAYS, _closes(()))
    _write_l1(tmp_path, rows, [d for d in DAYS if d <= L2_LAST])
    materialize_isin(
        ISIN, chain=build_chain_for_isin(ISIN, []), actions=[], data_root=tmp_path, curated=()
    )
    con = open_connection()
    try:
        assert l2_tails(con, [ISIN, "INE002A01018"], data_root=tmp_path) == {
            ISIN: L2Tail(L2_LAST, Decimal(1), Decimal(1))
        }
    finally:
        con.close()


# ── through the Commons source: the reads the sheets, screens and dossiers make ──────────────────


class _Actions:
    """`OverlayActionSource` over fixed actions; counts reads, and can be made unreadable."""

    def __init__(
        self,
        reconciled: Sequence[CorporateAction],
        unreconciled: Sequence[CorporateAction] = (),
        *,
        broken: bool = False,
    ) -> None:
        self.reconciled = tuple(reconciled)
        self.unreconciled = tuple(unreconciled)
        self.broken = broken
        self.reads = 0

    def ex_between(self, after: date, through: date) -> OverlayActions:
        self.reads += 1
        if self.broken:
            raise ConnectionError("store down")

        def inside(a: CorporateAction) -> bool:
            return after < a.ex_date <= through

        return OverlayActions(
            after=after,
            through=through,
            reconciled=tuple(a for a in self.reconciled if inside(a)),
            unreconciled=tuple(a for a in self.unreconciled if inside(a)),
        )


def _source(root: Path, actions: _Actions | None) -> LakeCommonsSource:
    clock = FrozenClock(datetime(2026, 10, 9, 21, 0, tzinfo=IST))
    return LakeCommonsSource(clock=clock, data_root=root, actions=actions)


def test_the_commons_reads_equal_the_rebuild_across_the_lag(tmp_path: Path) -> None:
    before, _, rebuilt = _scenario(tmp_path, (split(OLD_SPLIT, "10", "2"),), WINDOW, STEPS)
    # Put L2 back to what the lag left: the partition as it stood before the rebuild.
    _restore_l2(tmp_path, before)
    window = [d for d in DAYS if d >= date(2026, 9, 1)]
    with _source(tmp_path, _Actions(WINDOW)) as source:
        closes = {
            r.trade_date: r.close for r in source.adjusted_closes(frozenset({ISIN}), window).records
        }
        bars = {b.trade_date: b for b in source.price_bars(frozenset({ISIN}), window, "EQ").records}
        notes = source.price_overlay_notes(frozenset({ISIN}), window).records
    expected = {b.trade_date: b for b in rebuilt if b.trade_date >= window[0]}
    assert closes == {d: b.adj_close for d, b in expected.items()}
    assert {d: (b.high, b.low, b.close, b.volume) for d, b in bars.items()} == {
        d: (b.adj_high, b.adj_low, b.adj_close, b.adj_volume) for d, b in expected.items()
    }
    kinds = {n.kind for n in notes}
    # The demerger cannot be priced: the name is excluded for the session, and L2 lags.
    assert kinds == {PriceOverlayNote.EXCLUDED, PriceOverlayNote.LAGGING}
    (excluded,) = [n for n in notes if n.kind == PriceOverlayNote.EXCLUDED]
    assert "DEMERGER ex 2026-10-06" in excluded.reason and excluded.session == SESSION


def test_without_the_overlay_the_lagged_split_shows_as_a_return(tmp_path: Path) -> None:
    """The defect this task fixes, kept visible: no store, and the split is a -50 % session."""
    window_events = (split(SPLIT, "10", "5"),)
    before, _, _ = _scenario(tmp_path, (), window_events, ((SPLIT, Decimal("0.5")),))
    _restore_l2(tmp_path, before)
    window = [d for d in DAYS if d >= date(2026, 9, 1)]
    prev = max(d for d in window if d < SPLIT)
    with _source(tmp_path, None) as source:
        raw = {
            r.trade_date: r.close for r in source.adjusted_closes(frozenset({ISIN}), window).records
        }
        with pytest.raises(SourceUnavailableError):
            source.price_overlay_notes(frozenset({ISIN}), window)
    with _source(tmp_path, _Actions(window_events)) as source:
        fixed = {
            r.trade_date: r.close for r in source.adjusted_closes(frozenset({ISIN}), window).records
        }
    assert raw[SPLIT] / raw[prev] < Decimal("0.6")
    assert Decimal("0.9") < fixed[SPLIT] / fixed[prev] < Decimal("1.1")


VERANDA = (
    (date(2026, 9, 29), "223.31"),
    (date(2026, 9, 30), "220.75"),
    (date(2026, 10, 1), "213.80"),
    (date(2026, 10, 5), "212.76"),
    (date(2026, 10, 6), "29.72"),
    (date(2026, 10, 7), "31.20"),
    (date(2026, 10, 8), "32.76"),
    (date(2026, 10, 9), "34.39"),
)


def test_an_ine0iq001011_shaped_demerger_is_excluded_not_read_as_minus_86_percent(
    tmp_path: Path,
) -> None:
    """2026-10-09 on the real lake: L2 ends 10-01, the demerger went ex 10-06 with unquantified
    terms, and the Commons showed 212.76 -> 29.72 as a return."""
    days = [d for d, _ in VERANDA]
    rows = _price_rows(days, {d: Decimal(c) for d, c in VERANDA})
    _write_l1(tmp_path, rows, [d for d in days if d <= date(2026, 10, 1)])
    materialize_isin(
        ISIN, chain=build_chain_for_isin(ISIN, []), actions=[], data_root=tmp_path, curated=()
    )
    _write_l1(tmp_path, rows, [d for d in days if d > date(2026, 10, 1)])
    store = _Actions([demerger(date(2026, 10, 6), knowable=date(2026, 10, 6))])
    with _source(tmp_path, store) as source:
        notes = source.price_overlay_notes(frozenset({ISIN}), days).records
        closes = {
            r.trade_date: r.close for r in source.adjusted_closes(frozenset({ISIN}), days).records
        }
    (excluded,) = [n for n in notes if n.kind == PriceOverlayNote.EXCLUDED]
    assert excluded.isin == ISIN and "structural break" in excluded.reason
    # The level is what the engine says it is (a unit factor): the gap is real, hence the exclusion.
    assert closes[date(2026, 10, 6)] == Decimal("29.72")
    # A session before the demerger was knowable sees nothing to exclude.
    early = [d for d in days if d <= date(2026, 10, 5)]
    with _source(
        tmp_path, _Actions([demerger(date(2026, 10, 6), knowable=date(2026, 10, 6))])
    ) as source:
        assert not [
            n
            for n in source.price_overlay_notes(frozenset({ISIN}), early).records
            if n.kind == PriceOverlayNote.EXCLUDED
        ]


def test_an_unreadable_store_is_a_source_failure_never_raw_prices(tmp_path: Path) -> None:
    before, _, _ = _scenario(tmp_path, (), (split(SPLIT, "10", "5"),), ((SPLIT, Decimal("0.5")),))
    _restore_l2(tmp_path, before)
    window = [d for d in DAYS if d >= date(2026, 9, 1)]
    with _source(tmp_path, _Actions((), broken=True)) as source:
        with pytest.raises(SourceUnavailableError, match="corporate"):
            source.adjusted_closes(frozenset({ISIN}), window)
        with pytest.raises(SourceUnavailableError, match="corporate"):
            source.price_overlay_notes(frozenset({ISIN}), window)


def _restore_l2(root: Path, bars: dict[date, AdjustedBar]) -> None:
    """Write ``bars`` back as the ISIN's partition, as the lagging drain left it."""
    table = _bars_to_table([bars[d] for d in sorted(bars)])
    _write_table(table, l2_isin_partition_path(PRICES_ADJUSTED_DATASET, ISIN, data_root=root))


def test_a_split_known_only_after_l2_was_built_is_still_checked_against_l2(tmp_path: Path) -> None:
    """INE2FMX01012's shape: ex 09-28 inside L2's span, knowable only 10-06 (after L2's last
    bar). Knowable by the session, so the session must ask whether L2 carries it."""
    inside = date(2026, 9, 15)
    rows = _price_rows(DAYS, _closes(((inside, Decimal("0.5")),)))
    _write_l1(tmp_path, rows, [d for d in DAYS if d <= L2_LAST])
    materialize_isin(
        ISIN,
        chain=build_chain_for_isin(ISIN, []),
        actions=[],
        data_root=tmp_path,
        curated=(),
        infer_splits=False,
    )
    _write_l1(tmp_path, rows, [d for d in DAYS if d > L2_LAST])
    late = split(inside, "10", "5", knowable=date(2026, 10, 6))
    window = [d for d in DAYS if d >= date(2026, 9, 1)]
    with _source(tmp_path, _Actions([late])) as source:
        notes = source.price_overlay_notes(frozenset({ISIN}), window).records
    (excluded,) = [n for n in notes if n.kind == PriceOverlayNote.EXCLUDED]
    assert "rebuild is owed" in excluded.reason
    # A session before it was knowable sees nothing to check.
    early = [d for d in window if d <= date(2026, 10, 5)]
    with _source(tmp_path, _Actions([late])) as source:
        assert not [
            n
            for n in source.price_overlay_notes(frozenset({ISIN}), early).records
            if n.kind == PriceOverlayNote.EXCLUDED
        ]
