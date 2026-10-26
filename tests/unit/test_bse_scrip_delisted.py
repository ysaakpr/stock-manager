"""D2: a delisted BSE scrip opens no current symbol window, and a later delisting closes one.

The one BSE scrip-master merge (2026-09-06) opened a current window for every scrip, delisted ones
included. BSE hands a dead scrip's ticker to a new one, so ten tickers then resolved two ways from
the snapshot date — identity_reconciliation ids 1-10 — and every Saturday's `identity_refresh`,
which judges conflicts over the whole master, failed on them.

The fixture is a record-verbatim cut of that snapshot (`tests/fixtures/bse_scrip_master/
PROVENANCE.md`): the ten reused tickers, a buyback counter that died beside its live scrip, and a
suspended scrip. Offline: no network, no database.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from dataplatform.identity.master import (
    ConflictKind,
    Exchange,
    IdentityMaster,
    ListingStatus,
    SymbolWindow,
    detect_conflicts,
    plan_history,
)
from dataplatform.ingest.bse import scrip_master
from dataplatform.ingest.bse.scrip_master import (
    BSE_SCRIP_MASTER_SOURCE,
    BseScrip,
    close_delisted_windows,
    delisted_isins,
    derive_master,
    parse_scrip_master,
    scrip_to_isin,
)

FIXTURE: Final = Path(__file__).parents[1] / "fixtures" / "bse_scrip_master" / "2026-09-06"
SNAPSHOT: Final = date(2026, 9, 6)

#: (ticker, delisted ISIN, live ISIN) — identity_reconciliation ids 1-10, in id order.
REUSED: Final = (
    ("ALPHA", "INE506B01016", "INF174KA1IA5"),
    ("BAYERCROP", "INE556A01013", "INE462A01022"),
    ("BOSTON", "INE122C01010", "INE109B01019"),
    ("CMSINFO", "INE915A01011", "INE925R01014"),
    ("IDEA", "INE311B01011", "INE669E01016"),
    ("MILLENNIUM", "INE258D01028", "INE570N01025"),
    ("ONIDA", "INE328B01015", "INE831A01028"),
    ("SAWACA", "INE185B01019", "INE248B01023"),
    ("SUMMITSEC", "INE852A01016", "INE519C01017"),
    ("SUPHA", "INE647C01016", "INE549S01036"),
)
RELIANCE: Final = "INE002A01018"  # Active 500325 and the delisted buyback counter 532611
BALLARPUR: Final = "INE294A01037"  # Suspended 500102


@pytest.fixture(scope="module")
def scrips() -> tuple[BseScrip, ...]:
    """The three statuses merged as `bse_scrip_refresh` merges them: one array, Active first."""
    records: list[object] = []
    for status in ("Active", "Suspended", "Delisted"):
        records.extend(json.loads((FIXTURE / f"ListofScripData_{status}.json").read_text()))
    parsed, skipped = parse_scrip_master(json.dumps(records))
    assert skipped == 0
    return parsed


def _master(scrips: tuple[BseScrip, ...]) -> IdentityMaster:
    derived = derive_master(scrips, snapshot_date=SNAPSHOT)
    return IdentityMaster(derived.windows, securities=derived.securities, listings=derived.listings)


# ── the snapshot: a delisted scrip opens no window ─────────────────────────────────────────────


def test_a_reused_ticker_resolves_to_its_live_isin_only(scrips: tuple[BseScrip, ...]) -> None:
    """IDEA is Vodafone Idea on the snapshot date, not Ideaspace Solutions: one answer, no raise.

    Fails if delisted scrips open windows again (the resolve raises AmbiguousSymbolError) and if
    the rule is inverted (only the dead scrip would hold the ticker).
    """
    master = _master(scrips)
    for ticker, _dead, live in REUSED:
        assert master.resolve(ticker, SNAPSHOT, exchange=Exchange.BSE) == live
        assert master.resolve(ticker, SNAPSHOT + timedelta(days=400), exchange=Exchange.BSE) == live


def test_the_snapshot_holds_none_of_the_ten_conflicts(scrips: tuple[BseScrip, ...]) -> None:
    derived = derive_master(scrips, snapshot_date=SNAPSHOT)
    assert detect_conflicts(derived.windows, source=BSE_SCRIP_MASTER_SOURCE) == ()


def test_the_pre_fix_rule_raised_exactly_the_ten(scrips: tuple[BseScrip, ...]) -> None:
    """The evidence for the fix: an open window per scrip, as merged on 2026-09-06, is ids 1-10."""
    # One window per ISIN's most-live scrip, delisted or not: RELIANCE's dead counter was never
    # the one chosen, so it is the only scrip left out.
    every_scrip = [
        SymbolWindow(Exchange.BSE, s.symbol, SNAPSHOT, None, s.isin, s.group, "pre-fix")
        for s in scrips
        if not (s.isin == RELIANCE and s.status is ListingStatus.DELISTED)
    ]
    conflicts = detect_conflicts(every_scrip, source=BSE_SCRIP_MASTER_SOURCE)
    assert {(c.kind, c.symbols, c.isins) for c in conflicts} == {
        (ConflictKind.SYMBOL_TO_ISIN, (ticker,), tuple(sorted((dead, live))))
        for ticker, dead, live in REUSED
    }


def test_a_delisted_isin_keeps_its_security_and_scrip_code(scrips: tuple[BseScrip, ...]) -> None:
    """Only the window goes: the SC_CODE -> ISIN fact the pre-2024 bhavcopy needs stays."""
    derived = derive_master(scrips, snapshot_date=SNAPSHOT)
    windows = {w.isin for w in derived.windows}
    securities = {s.isin: s for s in derived.securities}
    listings = {listing.isin: listing for listing in derived.listings}
    for _ticker, dead, live in REUSED:
        assert dead not in windows
        assert live in windows
        assert securities[dead].status is ListingStatus.DELISTED
        assert listings[dead].status is ListingStatus.DELISTED
        assert listings[dead].security_code is not None
    assert scrip_to_isin(scrips)["530297"] == "INE311B01011"  # Ideaspace's code still maps


def test_a_listed_isin_with_a_dead_buyback_counter_keeps_its_window(
    scrips: tuple[BseScrip, ...],
) -> None:
    derived = derive_master(scrips, snapshot_date=SNAPSHOT)
    reliance = [w for w in derived.windows if w.isin == RELIANCE]
    assert [(w.symbol, w.valid_to) for w in reliance] == [("RELIANCE", None)]
    assert RELIANCE not in delisted_isins(scrips)
    assert scrip_to_isin(scrips)["532611"] == RELIANCE  # the counter still resolves


def test_a_suspended_scrip_is_still_listed_and_keeps_its_window(
    scrips: tuple[BseScrip, ...],
) -> None:
    derived = derive_master(scrips, snapshot_date=SNAPSHOT)
    assert [(w.symbol, w.valid_to) for w in derived.windows if w.isin == BALLARPUR] == [
        ("BALLARPUR", None)
    ]


def test_delisted_isins_is_exactly_the_dead_side(scrips: tuple[BseScrip, ...]) -> None:
    assert delisted_isins(scrips) == frozenset(dead for _t, dead, _l in REUSED)


# ── a later snapshot: the window an earlier one opened is closed the day before ────────────────


IDEASPACE: Final = "INE311B01011"
LATER: Final = date(2026, 12, 5)


def _open(isin: str, symbol: str, since: date, exchange: Exchange = Exchange.BSE) -> SymbolWindow:
    return SymbolWindow(exchange, symbol, since, None, isin, "B", BSE_SCRIP_MASTER_SOURCE)


def test_a_scrip_delisted_since_the_last_snapshot_has_its_window_closed(
    scrips: tuple[BseScrip, ...],
) -> None:
    """Listed at the 2026-06 snapshot, delisted at this one: closed the day before, not left open.

    Fails if the close is dropped (the window stays open and claims a dead ticker is current) or
    dated wrong (a close on the snapshot date overlaps the ticker's next owner, who opens on it).
    """
    earlier = _open(IDEASPACE, "IDEA", date(2026, 6, 6))
    (closed,) = close_delisted_windows((earlier,), scrips, snapshot_date=LATER)
    assert closed.key == earlier.key
    assert closed.valid_to == LATER - timedelta(days=1)

    plan = plan_history((earlier,), (*derive_master(scrips, snapshot_date=LATER).windows, closed))
    assert plan.closes == (closed,)
    successor = _open("INE669E01016", "IDEA", LATER)
    assert not closed.overlaps(successor)


def test_the_close_never_touches_a_listed_an_nse_or_a_same_day_window(
    scrips: tuple[BseScrip, ...],
) -> None:
    stored = (
        _open("INE669E01016", "IDEA", date(2026, 6, 6)),  # listed: Vodafone Idea
        _open(RELIANCE, "RELIANCE", date(2026, 6, 6)),  # listed beside a dead counter
        _open(BALLARPUR, "BALLARPUR", date(2026, 6, 6)),  # suspended is listed
        _open(IDEASPACE, "IDEA", date(2026, 6, 6), Exchange.NSE),  # not a BSE window
        _open(IDEASPACE, "IDEA", LATER),  # opens on the snapshot itself: never current
        SymbolWindow(
            Exchange.BSE, "IDEA", date(2026, 1, 1), date(2026, 3, 1), IDEASPACE, "B", "x"
        ),  # already closed
    )
    assert close_delisted_windows(stored, scrips, snapshot_date=LATER) == ()


def test_derive_master_is_still_exported_from_the_module() -> None:
    assert scrip_master.derive_master is derive_master


# ── the offline re-derive reads the snapshot back out of L0, whole ──────────────────────────────


def test_the_snapshot_is_read_back_from_l0_whole_or_not_at_all(tmp_path: Path) -> None:
    from dataplatform.clock import IST, FrozenClock
    from dataplatform.ingest.bse_scrip_refresh import (
        SCRIP_STATUSES,
        SOURCE_ID,
        read_snapshot_from_l0,
    )
    from dataplatform.store.l0 import L0Store

    l0 = L0Store(clock=FrozenClock(datetime(2026, 9, 6, 20, 41, tzinfo=IST)), data_root=tmp_path)
    for status in SCRIP_STATUSES[:2]:
        name = f"ListofScripData_{status}.json"
        l0.put(SOURCE_ID, SNAPSHOT, name, (FIXTURE / name).read_bytes())
    with pytest.raises(FileNotFoundError, match="Delisted"):
        read_snapshot_from_l0(l0, SNAPSHOT)

    name = "ListofScripData_Delisted.json"
    l0.put(SOURCE_ID, SNAPSHOT, name, (FIXTURE / name).read_bytes())
    combined = json.loads(read_snapshot_from_l0(l0, SNAPSHOT))
    assert [r["Status"] for r in combined] == ["Active"] * 11 + ["Suspended"] + ["Delisted"] * 11
