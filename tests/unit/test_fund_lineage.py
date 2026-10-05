"""D2 fund lineage: an ETF unit split links the retired INF ISIN to the new one, given evidence.

Every fixture here is real. The L1 bars in `fixtures/fund_lineage/prices_raw_nse.csv` are NSE EQ
rows copied verbatim out of the server lake (with AXISBANK on the same sessions, so the session
calendar is the real one); the five `PR` bundles in `fixtures/nse_pr_bundle/fund_unit_splits/` are
NSE's own archives reduced to their `Bc` member and readme, byte-for-byte (`manifest.json` holds the
member hashes, re-checked below). The cases, each as measured over the whole lake:

* BANKBEES / GOLDBEES 2019-12-19 — the unit split is broadcast from PR111219 with its ex-date on
  the old ISIN's last session. Accepted.
* UTISXN50 2021-02-17 — ex-date on the old ISIN's *second-to-last* session (it printed two
  post-split sessions before the switch). Accepted.
* HDFCNIFIT 2024-02-02 — ex-date on the new ISIN's first session. Accepted.
* AXISNIFTY 2020-07-23 — the split is broadcast, but the units did not trade on 2020-07-24, so the
  two ISINs are not on consecutive sessions. Rejected.
* HDFCSENSEX 2024-02-02 — switched ISIN the same day as HDFCNIFIT, at a 10x lower price, and NSE
  broadcast no event for it. Rejected — the case that proves price continuity alone is not enough.
* HDFCLIQUID 2025-02-28 — an event on the switch, `CHANGE IN ATTRIBUTE`, that is not a split.

Offline: L1 and L0 are written under `tmp_path`; no network, no Postgres.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Final

import pytest

from dataplatform.clock import FrozenClock
from dataplatform.corpactions.factors import FactorChain
from dataplatform.corpactions.manual_actions import ExplainedMove, ManualActions
from dataplatform.corpactions.taxonomy import ActionType
from dataplatform.identity.fund_lineage import (
    FundEdgeVerdict,
    FundIsinSwitch,
    RejectReason,
    UnitEvent,
    evaluate_switches,
    read_bc_unit_events,
    read_fund_switches,
    unit_event_type,
)
from dataplatform.identity.lineage import LineageResolver
from dataplatform.identity.master import Exchange
from dataplatform.ingest.models import PriceRow
from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID, PrBundle
from dataplatform.quality.l2_continuity import StepClass, scan
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import materialize_isin, materialized_isins, prune_retired, read_adjusted

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures"
BARS_CSV: Final = FIXTURES / "fund_lineage" / "prices_raw_nse.csv"
BUNDLES: Final = FIXTURES / "nse_pr_bundle" / "fund_unit_splits"

BANKBEES_OLD, BANKBEES_NEW = "INF732E01078", "INF204KB15I9"
UTISXN50_OLD, UTISXN50_NEW = "INF789F1AHR6", "INF789F1AUU3"

#: symbol → (verdict, ex-date of the evidence or None). The expected outcome of every candidate
#: switch the fixtures hold — the whole survey, not a sample of it.
EXPECTED: Final[dict[str, tuple[RejectReason | None, date | None]]] = {
    "BANKBEES": (None, date(2019, 12, 19)),
    "GOLDBEES": (None, date(2019, 12, 19)),
    "AXISNIFTY": (RejectReason.SESSION_GAP, None),
    "UTISXN50": (None, date(2021, 2, 17)),
    "HDFCNIFIT": (None, date(2024, 2, 2)),
    "HDFCSENSEX": (RejectReason.NO_CORROBORATING_EVENT, None),
    "HDFCLIQUID": (RejectReason.EVENT_NOT_A_UNIT_REBASE, date(2025, 2, 28)),
}


def _write_lake(root: Path) -> None:
    by_date: dict[date, list[PriceRow]] = {}
    with BARS_CSV.open(newline="") as fh:
        for r in csv.DictReader(fh):
            day = date.fromisoformat(r["trade_date"])
            by_date.setdefault(day, []).append(
                PriceRow(
                    isin=r["isin"],
                    symbol=r["symbol"],
                    series=r["series"],
                    trade_date=day,
                    open=Decimal(r["open"]),
                    high=Decimal(r["high"]),
                    low=Decimal(r["low"]),
                    close=Decimal(r["close"]),
                    last=Decimal(r["last"]),
                    prev_close=Decimal(r["prev_close"]),
                    total_traded_qty=int(r["total_traded_qty"]),
                    total_traded_value=Decimal(r["total_traded_value"]),
                    total_trades=int(r["total_trades"]),
                )
            )
    for rows in by_date.values():
        write_prices_raw(rows, exchange=Exchange.NSE, data_root=root)
    store = L0Store(clock=FrozenClock(date(2026, 10, 5)), data_root=root)
    for path in sorted(BUNDLES.glob("PR*.zip")):
        payload = path.read_bytes()
        with PrBundle(payload, filename=path.name) as bundle:
            day = bundle.publication_date
        store.put(PR_BUNDLE_SOURCE_ID, day, path.name, payload)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    _write_lake(tmp_path)
    return tmp_path


def _survey(lake: Path) -> dict[str, FundEdgeVerdict]:
    switches, sessions = read_fund_switches(data_root=lake)
    events, unreadable = read_bc_unit_events(
        switches, clock=FrozenClock(date(2026, 10, 5)), data_root=lake
    )
    assert unreadable == ()
    return {v.switch.symbol: v for v in evaluate_switches(switches, sessions, events, {})}


# ── the fixtures are what they claim to be ──────────────────────────────────────────────────


def test_bc_members_are_byte_for_byte_the_archived_ones() -> None:
    manifest = json.loads((BUNDLES / "manifest.json").read_text(encoding="utf-8"))
    assert {m["archive_filename"] for m in manifest} == {p.name for p in BUNDLES.glob("PR*.zip")}
    for entry in manifest:
        with zipfile.ZipFile(io.BytesIO((BUNDLES / entry["archive_filename"]).read_bytes())) as z:
            member = z.read(entry["bc_member"])
        assert hashlib.sha256(member).hexdigest() == entry["bc_member_sha256"]


# ── the purpose classifier ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        # Every unit-split spelling the fund switches carry in the lake's Bc files.
        ("FV SPLIT RS.10 TO RS.7", ActionType.SPLIT),
        ("FV SPLT FR RS100 TO RS10", ActionType.SPLIT),
        ("FVSPLT FRM RS 10 TO RE 1", ActionType.SPLIT),
        ("FVSPLTFRM 761.25TO76.125", ActionType.SPLIT),
        ("FVSPLTFRM100TO1", ActionType.SPLIT),
        ("FV SPLT FRM RS 299.92 TO", ActionType.SPLIT),
        ("FVSPLT RS100 TO RS2PERSH", ActionType.SPLIT),
        ("BON-1:25/SPLIT RS10TORS.5", ActionType.SPLIT),
        ("BONUS 1:1", ActionType.BONUS),
        ("CHANGE IN ATTRIBUTE", None),
        ("AGM/DIVIDEND - RS 2.50 PER SHARE", None),
        ("INTERIM DIVIDEND", None),
        ("", None),
    ],
)
def test_unit_event_type(purpose: str, expected: ActionType | None) -> None:
    assert unit_event_type(purpose) is expected


# ── the survey over the real fixtures ───────────────────────────────────────────────────────


def test_every_candidate_gets_the_measured_verdict(lake: Path) -> None:
    verdicts = _survey(lake)
    assert set(verdicts) == set(EXPECTED)
    for symbol, (reason, ex_date) in EXPECTED.items():
        v = verdicts[symbol]
        assert v.reason is reason, symbol
        assert v.accepted is (reason is None), symbol
        assert (None if v.event is None else v.event.ex_date) == ex_date, symbol


def test_accepted_edges_are_the_switch_itself(lake: Path) -> None:
    v = _survey(lake)["BANKBEES"]
    assert v.edge is not None
    assert (v.edge.predecessor_isin, v.edge.successor_isin) == (BANKBEES_OLD, BANKBEES_NEW)
    assert v.edge.effective_date == date(2019, 12, 20)
    assert v.edge.gap_sessions == 0
    assert v.edge.corroborating_action is ActionType.SPLIT
    assert v.edge.confidence == "CORROBORATED"
    # The evidence is the earliest broadcast, and names the L0 payload it came out of.
    assert v.event is not None
    assert v.event.knowable_date == date(2019, 12, 11)
    assert v.event.l0_key == "nse_pr_bundle/2019-12-11/PR111219.zip"


def test_ex_date_on_the_old_isins_second_to_last_session_corroborates(lake: Path) -> None:
    v = _survey(lake)["UTISXN50"]
    assert (v.switch.predecessor_last, v.switch.successor_first) == (
        date(2021, 2, 18),
        date(2021, 2, 19),
    )
    assert v.edge is not None and v.edge.successor_isin == UTISXN50_NEW


# ── nothing is accepted without the event ───────────────────────────────────────────────────


def test_no_edge_is_accepted_without_the_corroborating_event(lake: Path) -> None:
    """The guard against the inversion that matters: contiguity alone must never make an edge.

    The same switches, the same calendar, every Bc event withheld: had the evaluator accepted on
    consecutive sessions alone, BANKBEES, GOLDBEES, UTISXN50 and HDFCNIFIT would all pass here.
    """
    switches, sessions = read_fund_switches(data_root=lake)
    verdicts = evaluate_switches(switches, sessions, {}, {})
    assert not any(v.accepted for v in verdicts)
    by_symbol = {v.switch.symbol: v for v in verdicts}
    for symbol in ("BANKBEES", "GOLDBEES", "UTISXN50", "HDFCNIFIT"):
        assert by_symbol[symbol].reason is RejectReason.NO_CORROBORATING_EVENT


def test_an_event_outside_the_switch_window_does_not_corroborate(lake: Path) -> None:
    switches, sessions = read_fund_switches(data_root=lake)
    bankbees = next(s for s in switches if s.symbol == "BANKBEES")
    # Three sessions before the switch: the old ISIN's third-to-last, one past the window.
    early = _event("BANKBEES", date(2019, 12, 17), "FVSPLT FRM RS 10 TO RS 1")
    other_symbol = _event("GOLDBEES", date(2019, 12, 19), "FVSPLT FRM RS100 TO RS 1")
    (v,) = evaluate_switches(
        [bankbees],
        sessions,
        {
            ("BANKBEES", "EQ", early.ex_date): early,
            ("GOLDBEES", "EQ", other_symbol.ex_date): other_symbol,
        },
        {},
    )
    assert v.reason is RejectReason.NO_CORROBORATING_EVENT


def test_an_event_on_another_series_does_not_corroborate(lake: Path) -> None:
    switches, sessions = read_fund_switches(data_root=lake)
    bankbees = next(s for s in switches if s.symbol == "BANKBEES")
    bl = _event("BANKBEES", date(2019, 12, 19), "FVSPLT FRM RS 10 TO RS 1", series="BL")
    (v,) = evaluate_switches([bankbees], sessions, {("BANKBEES", "BL", bl.ex_date): bl}, {})
    assert v.reason is RejectReason.NO_CORROBORATING_EVENT


def test_a_feed_split_on_the_old_isin_corroborates(lake: Path) -> None:
    switches, sessions = read_fund_switches(data_root=lake)
    hdfcsensex = next(s for s in switches if s.symbol == "HDFCSENSEX")
    feed = {(hdfcsensex.predecessor_isin, date(2024, 2, 2)): ActionType.SPLIT}
    (v,) = evaluate_switches([hdfcsensex], sessions, {}, feed)
    assert v.accepted and v.evidence_source == "feed"
    # Filed against the *new* ISIN it explains nothing about the old one's retirement.
    (v,) = evaluate_switches(
        [hdfcsensex],
        sessions,
        {},
        {(hdfcsensex.successor_isin, date(2024, 2, 2)): ActionType.SPLIT},
    )
    assert v.reason is RejectReason.NO_CORROBORATING_EVENT


# ── the structural conditions ───────────────────────────────────────────────────────────────

_SESSIONS: Final = (date(2019, 12, 18), date(2019, 12, 19), date(2019, 12, 20))


def _switch(
    old: str = "INF000000001",
    new: str = "INF000000002",
    *,
    symbol: str = "X",
    old_last_anywhere: date | None = None,
    new_first_anywhere: date | None = None,
) -> FundIsinSwitch:
    return FundIsinSwitch(
        symbol=symbol,
        series="EQ",
        predecessor_isin=old,
        successor_isin=new,
        predecessor_last=date(2019, 12, 19),
        successor_first=date(2019, 12, 20),
        predecessor_last_anywhere=old_last_anywhere or date(2019, 12, 19),
        successor_first_anywhere=new_first_anywhere or date(2019, 12, 20),
    )


def _event(symbol: str, ex_date: date, purpose: str, *, series: str = "EQ") -> UnitEvent:
    return UnitEvent(
        symbol=symbol,
        series=series,
        ex_date=ex_date,
        purpose=purpose,
        action=unit_event_type(purpose),
        knowable_date=ex_date,
        l0_key=None,
    )


def _events(*symbols: str) -> dict[tuple[str, str, date], UnitEvent]:
    return {
        (s, "EQ", date(2019, 12, 19)): _event(s, date(2019, 12, 19), "FVSPLT FRM RS 10 TO RS 1")
        for s in symbols
    }


def test_the_old_isin_trading_on_elsewhere_is_not_a_succession() -> None:
    late = _switch(old_last_anywhere=date(2019, 12, 20))
    early = _switch(new_first_anywhere=date(2019, 12, 18))
    reasons = [v.reason for v in evaluate_switches([late, early], _SESSIONS, _events("X"), {})]
    assert reasons == [RejectReason.NOT_SEQUENTIAL, RejectReason.NOT_SEQUENTIAL]


def test_one_predecessor_claimed_twice_is_refused_both_times() -> None:
    a = _switch(new="INF000000002", symbol="X")
    b = _switch(new="INF000000003", symbol="Y")
    verdicts = evaluate_switches([a, b], _SESSIONS, _events("X", "Y"), {})
    assert [v.reason for v in verdicts] == [RejectReason.NOT_ONE_TO_ONE] * 2


# ── L2: the stitched series is continuous, adjusted once, and the retired partition goes ─────


def _resolver(*verdicts: FundEdgeVerdict) -> LineageResolver:
    return LineageResolver(
        {
            v.edge.predecessor_isin: (v.edge.successor_isin, v.edge.effective_date)
            for v in verdicts
            if v.edge is not None
        }
    )


def test_stitched_bankbees_is_continuous_and_split_once(lake: Path) -> None:
    """Hand-computed: the 1:10 split on 2019-12-19 scales every earlier close by 0.1, once.

        2019-12-18 (old ISIN, pre-split)  3286.95 x 0.1 = 328.6950
        2019-12-19 (old ISIN, ex)          329.51 x 1   = 329.5100
        2019-12-20 (new ISIN, first)       330.91 x 1   = 330.9100

    Inverted (x 10) the 18th reads 32,869.50; applied twice (x 0.01) it reads 32.8695.
    """
    # Before the edge: both ISINs were built from their own bars (what main does today).
    for isin in (BANKBEES_OLD, BANKBEES_NEW):
        materialize_isin(isin, chain=FactorChain(isin=isin, rows=()), actions=(), data_root=lake)
    resolver = _resolver(_survey(lake)["BANKBEES"])
    chain = resolver.chain_to(BANKBEES_NEW)
    assert chain == (BANKBEES_OLD, BANKBEES_NEW)

    report = materialize_isin(
        BANKBEES_NEW,
        chain=FactorChain(isin=BANKBEES_NEW, rows=()),
        actions=(),
        data_root=lake,
        history_isins=chain,
    )
    assert [(s.ex_date, s.price_factor) for s in report.implied_splits] == [
        (date(2019, 12, 19), Decimal("0.1"))
    ]
    bars = {b.trade_date: b for b in read_adjusted(BANKBEES_NEW, data_root=lake)}
    assert bars[date(2019, 12, 18)].adj_close == Decimal("328.6950")
    assert bars[date(2019, 12, 19)].adj_close == Decimal("329.5100")
    assert bars[date(2019, 12, 20)].adj_close == Decimal("330.9100")
    assert min(bars) == date(2019, 11, 15), "the survivor's series starts at the old ISIN's"
    closes = [bars[d].adj_close for d in sorted(bars)]
    assert all(Decimal("0.8") < b / a < Decimal("1.2") for a, b in pairwise(closes))

    pruned = prune_retired(resolver.survivor_of, data_root=lake)
    assert pruned == (BANKBEES_OLD,)
    assert BANKBEES_NEW in materialized_isins(data_root=lake)


def test_a_curated_split_on_the_retired_isin_reaches_the_survivor_once(lake: Path) -> None:
    """UTISXN50's 2021-02-17 split is curated against INF789F1AHR6 (`manual_actions.yaml`).

    The ex-day printed 8.33x (10x and a +20% band print), which the implied scan rejects, so the
    curated row is the only factor there is — and it is filed under the ISIN the edge retires. The
    stitched survivor must carry it, re-keyed and naming where it was filed, with no implied twin:

        2021-02-16 (pre-split)  403.49 x 0.1 = 40.3490
        2021-02-17 (ex)          48.42 x 1   = 48.4200

    Inverted the 16th reads 4,034.90; looked up by the survivor alone it stays 403.49.
    """
    resolver = _resolver(_survey(lake)["UTISXN50"])
    report = materialize_isin(
        UTISXN50_NEW,
        chain=FactorChain(isin=UTISXN50_NEW, rows=()),
        actions=(),
        data_root=lake,
        history_isins=resolver.chain_to(UTISXN50_NEW),
    )
    assert [(a.isin, a.filed_against_isin, a.ex_date) for a in report.curated] == [
        (UTISXN50_NEW, UTISXN50_OLD, date(2021, 2, 17))
    ]
    assert report.implied_splits == ()
    bars = {b.trade_date: b for b in read_adjusted(UTISXN50_NEW, data_root=lake)}
    assert bars[date(2021, 2, 16)].adj_close == Decimal("40.3490")
    assert bars[date(2021, 2, 17)].adj_close == Decimal("48.4200")
    report_cont = scan(None, survivor_of=resolver.survivor_of, data_root=lake)
    assert [step for step, _ in report_cont.steps if step.isin == UTISXN50_NEW] == []


def test_a_curated_date_on_the_retired_isin_classifies_the_survivors_step(lake: Path) -> None:
    resolver = _resolver(_survey(lake)["UTISXN50"])
    materialize_isin(
        UTISXN50_NEW,
        chain=FactorChain(isin=UTISXN50_NEW, rows=()),
        actions=(),
        data_root=lake,
        history_isins=resolver.chain_to(UTISXN50_NEW),
        curated=(),  # leave the 8.33x step in, so there is a step to classify
    )
    allowlist = ManualActions(
        actions=(),
        explained_moves=(
            ExplainedMove(
                isin=UTISXN50_OLD,
                company="UTISXN50",
                trade_date=date(2021, 2, 17),
                kind="test",
                reason="filed under the ISIN the edge retires",
                checked=date(2026, 10, 5),
                sources=(),
            ),
        ),
    )
    report = scan(None, survivor_of=resolver.survivor_of, data_root=lake, curated=allowlist)
    (classified,) = [(step, c) for step, c in report.steps if step.isin == UTISXN50_NEW]
    assert classified[0].trade_date == date(2021, 2, 17)
    assert classified[1] is StepClass.EXPLAINED_MOVE
    # Without the survivor mapping the retired ISIN's date reaches nothing, and the step is red.
    report = scan(None, survivor_of=lambda isin: isin, data_root=lake, curated=allowlist)
    assert [c for step, c in report.steps if step.isin == UTISXN50_NEW] == [StepClass.UNEXPLAINED]
