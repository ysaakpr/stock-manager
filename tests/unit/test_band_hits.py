"""X2 H2 — price-band-hit avoidance: the filter, the PIT boundary, and the same-session join.

The pre-registration (``ops/studies/preregistration-signals-2026-09-29.md`` §3) fixes the rule:
no *new* buy of a name that hit a daily price band in any of the last five sessions, knowable on the
PR bundle's own publication date, symbol mapped to ISIN only through that same session's bhavcopy,
existing holdings never force-sold. Each clause has a test here that fails if it is inverted or
removed:

* a name hit 3 sessions ago is not bought, and one hit 6 sessions ago is — at the pure function and
  through the policy, so deleting the filter from the policy fails a test;
* a blocked holding is neither sold nor topped up;
* a hit is used on its publication date and not one session before it;
* a symbol resolves only through the listing it is handed, and an ambiguous or absent one is
  counted, not guessed.

Offline: an in-memory policy source, and for the read path a temporary lake built from one frozen
PR-bundle fixture and a synthetic L1 partition. No network, no wall clock.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from analyst.journal.models import Decision
from backtest.band_hits import (
    BAND_HIT_AVOIDANCE_IDENTITY,
    BAND_HIT_BLOCK_RATIONALE,
    BAND_HIT_LOOKBACK_SESSIONS,
    BandHit,
    BandHitIndex,
    band_hit_blocked,
    lookback_window,
    resolve_session,
)
from backtest.policies.swing_composite import (
    RegimeReading,
    SwingCompositeParameters,
    SwingCompositePolicy,
    SwingRecord,
)
from backtest.replay import SessionContext, SessionDecision
from backtest.run import UniverseParameters, backtest_spec
from backtest.sweep import ARMS, BAND_HIT_ARM, _arm_spec
from dataplatform.clock import FrozenClock
from dataplatform.ingest.nse.pr_bundle import BandHitRow, BandSide
from dataplatform.query.pit import Dataset, PitContext, PitError
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import l1_partition_path
from execution.broker import Exchange, Holding, Margins, Position, Side

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle"

#: Consecutive days stand in for trading sessions; only their order matters.
CALENDAR = tuple(date(2020, 1, 1) + timedelta(days=n) for n in range(30))
SESSION = CALENDAR[20]

A = "INE001A01036"
B = "INE002A01018"
C = "INE009A01021"
D = "INE040A01034"


def _ago(n: int) -> date:
    return CALENDAR[CALENDAR.index(SESSION) - n]


def _hit(isin: str, session: date, *, knowable: date | None = None) -> BandHit:
    return BandHit(
        isin=isin,
        session=session,
        knowable_date=session if knowable is None else knowable,
        side=BandSide.UPPER,
        symbol="SYM",
        series="EQ",
    )


# ── the pure filter ──────────────────────────────────────────────────────────────────────────────


def test_the_lookback_is_five_sessions_ending_on_the_decision_session() -> None:
    window = lookback_window(CALENDAR, SESSION)
    assert BAND_HIT_LOOKBACK_SESSIONS == 5
    assert window == tuple(_ago(n) for n in (4, 3, 2, 1, 0))


@pytest.mark.parametrize(
    ("ago", "blocked"), [(0, True), (3, True), (4, True), (5, False), (6, False)]
)
def test_a_hit_inside_the_window_blocks_and_one_outside_does_not(ago: int, blocked: bool) -> None:
    hits = [_hit(A, _ago(ago))]
    result = band_hit_blocked(hits, as_of=SESSION, window=lookback_window(CALENDAR, SESSION))
    assert (A in result) is blocked


def test_the_lower_band_blocks_too() -> None:
    hit = BandHit(A, _ago(1), _ago(1), BandSide.LOWER, "SYM", "EQ")
    assert band_hit_blocked([hit], as_of=SESSION, window=lookback_window(CALENDAR, SESSION)) == {A}


def test_a_hit_not_yet_published_raises_rather_than_being_dropped() -> None:
    """A same-day hit whose bundle publishes tomorrow must not reach today's decision."""
    leak = _hit(A, SESSION, knowable=SESSION + timedelta(days=1))
    with pytest.raises(PitError, match="knowable only from"):
        band_hit_blocked([leak], as_of=SESSION, window=lookback_window(CALENDAR, SESSION))


# ── the same-session join ────────────────────────────────────────────────────────────────────────


def _row(symbol: str, series: str = "EQ", session: date = SESSION) -> BandHitRow:
    return BandHitRow(
        session=session,
        publication_date=session,
        symbol=symbol,
        series=series,
        security_name=symbol,
        side=BandSide.UPPER,
    )


def test_resolution_uses_only_the_listing_it_is_handed() -> None:
    listing = {("ALPHA", "EQ"): A, ("TWIN", "EQ"): None}
    hits, unresolved = resolve_session(
        [_row("ALPHA"), _row("ALPHA", "BE"), _row("TWIN"), _row("GHOST")], listing
    )
    assert [(h.isin, h.symbol) for h in hits] == [(A, "ALPHA")]
    assert hits[0].knowable_date == SESSION
    # Same symbol, other series: absent, not borrowed from the EQ row.
    assert [(r.symbol, r.series, why) for r, why in unresolved] == [
        ("ALPHA", "BE", "absent"),
        ("TWIN", "EQ", "ambiguous"),
        ("GHOST", "EQ", "absent"),
    ]


def test_rows_from_two_sessions_are_refused() -> None:
    with pytest.raises(ValueError, match="more than one session"):
        resolve_session([_row("A"), _row("B", session=_ago(1))], {})


# ── the index: PIT on the publication date ───────────────────────────────────────────────────────


def test_the_index_serves_a_hit_from_its_publication_date_and_not_before() -> None:
    late = _hit(A, SESSION, knowable=CALENDAR[21])
    index = BandHitIndex([late], CALENDAR)
    assert PitContext(as_of=SESSION).admit(index.band_hits(SESSION)) == ()
    assert PitContext(as_of=CALENDAR[21]).admit(index.band_hits(CALENDAR[21])) == (late,)


def test_the_index_serves_only_the_window() -> None:
    index = BandHitIndex([_hit(A, _ago(3)), _hit(B, _ago(6))], CALENDAR)
    assert {h.isin for h in index.band_hits(SESSION).records} == {A}


# ── through the policy ───────────────────────────────────────────────────────────────────────────


def _rec(isin: str, score: str) -> SwingRecord:
    value = Decimal(score)
    return SwingRecord(
        isin=isin,
        high_proximity=value,
        delivery_share=value,
        momentum_12_1=value,
        volatility=Decimal("0.02"),
        price=Decimal("100"),
        knowable_date=SESSION,
    )


#: A > B > C > D on every leg.
_RECORDS = (_rec(A, "0.9"), _rec(B, "0.8"), _rec(C, "0.7"), _rec(D, "0.6"))


class _Data:
    def is_rebalance(self, session: date) -> bool:
        return True

    def signal(self, as_of: date) -> Dataset[SwingRecord]:
        return Dataset.declaring("swing", _RECORDS, knowable_date=lambda r: r.knowable_date)

    def marks(self, as_of: date) -> Dataset[SwingRecord]:
        return Dataset.declaring("marks", _RECORDS, knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        reading = RegimeReading(Decimal("110"), Decimal("100"), as_of)
        return Dataset.declaring("regime", (reading,), knowable_date=lambda r: r.knowable_date)


class _LeakingHits:
    """A band-hit source that serves a hit before its bundle is published."""

    def window(self, as_of: date) -> tuple[date, ...]:
        return lookback_window(CALENDAR, as_of)

    def band_hits(self, as_of: date) -> Dataset[BandHit]:
        leak = _hit(A, as_of, knowable=as_of + timedelta(days=1))
        return Dataset.declaring("leak", (leak,), knowable_date=lambda h: h.knowable_date)


class _Broker:
    def __init__(self, holdings: tuple[Holding, ...] = ()) -> None:
        self._holdings = holdings

    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def positions(self) -> tuple[Position, ...]:
        return ()

    def margins(self) -> Margins:
        return Margins(available=Decimal("100000"), utilised=Decimal("0"))


_PARAMS = SwingCompositeParameters(top_n=2, sell_band=2, exclude_vol_fraction=Decimal("0"))


def _decide(
    hits: list[BandHit] | None,
    *,
    session: date = SESSION,
    holdings: tuple[Holding, ...] = (),
    source: object | None = None,
) -> SessionDecision:
    band_hits = (
        source if source is not None else (None if hits is None else BandHitIndex(hits, CALENDAR))
    )
    policy = SwingCompositePolicy(_Data(), _PARAMS, band_hits=band_hits)  # type: ignore[arg-type]
    ctx = SessionContext(
        session=session,
        pit=PitContext(as_of=session),
        broker=_Broker(holdings),  # type: ignore[arg-type]
        clock=FrozenClock(session),
    )
    return policy.decide(ctx)


def _bought(decision: SessionDecision) -> set[str]:
    return {o.isin for o in decision.orders if o.side is Side.BUY}


def _blocked_lines(decision: SessionDecision) -> list[str]:
    return [
        e.isin or ""
        for e in decision.entries
        if e.decision is Decision.HEARTBEAT
        and (e.rationale or "").startswith(BAND_HIT_BLOCK_RATIONALE)
    ]


def test_without_the_filter_the_top_two_are_bought() -> None:
    assert _bought(_decide(None)) == {A, B}


def test_a_name_hit_three_sessions_ago_is_not_bought() -> None:
    """The next-ranked unblocked name takes its slot. Remove the filter and A is bought."""
    decision = _decide([_hit(A, _ago(3))])
    assert _bought(decision) == {B, C}
    assert _blocked_lines(decision) == [A]


def test_a_name_hit_six_sessions_ago_is_buyable() -> None:
    decision = _decide([_hit(A, _ago(6))])
    assert _bought(decision) == {A, B}
    assert _blocked_lines(decision) == []


def test_a_blocked_holding_is_neither_sold_nor_topped_up() -> None:
    """A is held and hit a band yesterday: it stays on the book and gets no buy."""
    held = (Holding(isin=A, exchange=Exchange.NSE, quantity=1, average_price=Decimal("100")),)
    decision = _decide([_hit(A, _ago(1))], holdings=held)
    assert not {o.isin for o in decision.orders if o.side is Side.SELL}
    assert A not in _bought(decision)
    assert _blocked_lines(decision) == [A]


def test_a_same_day_hit_is_not_used_before_its_bundle_is_published() -> None:
    """A hit on the session, published the next session, blocks nothing today but tomorrow."""
    late = _hit(A, SESSION, knowable=CALENDAR[21])
    assert _bought(_decide([late])) == {A, B}
    assert A not in _bought(_decide([late], session=CALENDAR[21]))


def test_a_source_that_leaks_an_unpublished_hit_is_refused_by_the_guard() -> None:
    with pytest.raises(PitError, match="point-in-time leak"):
        _decide(None, source=_LeakingHits())


# ── the arm and its spec ─────────────────────────────────────────────────────────────────────────


def test_the_arm_is_the_m10_7_composite_plus_the_filter_and_nothing_else() -> None:
    assert BAND_HIT_ARM.label == "Swing composite + band-hit avoidance (H2)"
    assert BAND_HIT_ARM.swing == ARMS[0].swing == SwingCompositeParameters()
    assert BAND_HIT_ARM.band_hit_avoidance
    assert BAND_HIT_ARM not in ARMS


def test_no_existing_arm_changes_its_spec_and_the_h2_arm_does() -> None:
    """Every existing digest is kept: the key is absent unless the filter is on."""
    universe = UniverseParameters(median_turnover_floor=Decimal("100000000"))
    kwargs: dict[str, object] = {
        "start": date(2013, 7, 1),
        "end": date(2014, 6, 30),
        "universe": universe,
        "opening_cash": Decimal("1000000"),
        "adjusted": True,
    }
    for arm in ARMS:
        assert "band_hit_avoidance" not in _arm_spec(arm, **kwargs)  # type: ignore[arg-type]
    spec = _arm_spec(BAND_HIT_ARM, **kwargs)  # type: ignore[arg-type]
    assert spec["band_hit_avoidance"] == BAND_HIT_AVOIDANCE_IDENTITY
    baseline = _arm_spec(ARMS[0], **kwargs)  # type: ignore[arg-type]
    assert {k: v for k, v in spec.items() if k != "band_hit_avoidance"} == baseline
    assert (
        backtest_spec(
            "swing_composite",
            parameters=ARMS[0].swing,
            **kwargs,  # type: ignore[arg-type]
        )
        == baseline
    )


# ── the read path, end to end, offline ───────────────────────────────────────────────────────────


def test_from_lake_resolves_against_the_same_sessions_l1_only(tmp_path: Path) -> None:
    """One real bundle (2013-07-01, 162 rows) against a synthetic three-row L1 for that session.

    ADSL resolves; AGCNET names two ISINs that session and is ambiguous; everything else is absent.
    A listing on the *next* session that would resolve ALPSINDUS is never consulted.
    """
    session = date(2013, 7, 1)
    store = L0Store(clock=FrozenClock(session), data_root=tmp_path)
    store.put(
        "nse_pr_bundle",
        session,
        "PR010713.zip",
        (FIXTURES / "bh_series" / "PR010713.zip").read_bytes(),
    )
    _write_l1(tmp_path, session, [("ADSL", "EQ", A), ("AGCNET", "EQ", B), ("AGCNET", "EQ", C)])
    _write_l1(tmp_path, date(2013, 7, 2), [("ALPSINDUS", "EQ", D)])

    index = BandHitIndex.from_lake(
        start=session, end=session, calendar=(session,), data_root=tmp_path
    )
    assert [h.isin for h in index.band_hits(session).records] == [A]
    year = index.resolution[2013]
    assert (year.bundles, year.rows, year.resolved, year.ambiguous) == (1, 162, 1, 1)
    assert year.unresolved == 161
    assert year.undated_bundles == 0 and year.sessions_without_bhavcopy == 0


def test_from_lake_counts_the_misserved_bundle_instead_of_dating_it(tmp_path: Path) -> None:
    session = date(2018, 1, 2)
    store = L0Store(clock=FrozenClock(session), data_root=tmp_path)
    store.put(
        "nse_pr_bundle",
        session,
        "PR020118.zip",
        (FIXTURES / "bh_misserved" / "PR020118.zip").read_bytes(),
    )
    index = BandHitIndex.from_lake(
        start=session, end=session, calendar=(session,), data_root=tmp_path
    )
    assert index.band_hits(session).records == ()
    assert index.resolution[2018].undated_bundles == 1
    assert index.resolution[2018].rows == 0


def _write_l1(root: Path, session: date, rows: list[tuple[str, str, str]]) -> None:
    path = l1_partition_path("prices_raw", session, data_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (isin VARCHAR, exchange VARCHAR, symbol VARCHAR, series VARCHAR, "
        "trade_date DATE)"
    )
    for symbol, series, isin in rows:
        con.execute("INSERT INTO t VALUES (?, 'NSE', ?, ?, ?)", [isin, symbol, series, session])
    con.execute("INSERT INTO t VALUES ('INE999Z01011', 'BSE', 'ADSL', 'EQ', ?)", [session])
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    con.close()
