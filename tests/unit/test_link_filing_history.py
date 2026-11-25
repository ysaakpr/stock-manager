"""M18.1: the proofs behind the 27 hand-linked NIFTY 500 filing histories (owner ruling 2026-11-23).

Offline: each case is the facts `gather_*_facts` reads, built by hand. The accepted cases are the
live shapes measured on 2026-11-25 (KOTAKBANK, a retired middle; HAVELLS, no stored split; HUDCO, a
bond filer). Each refused case changes one fact, so a test fails if that proof were dropped.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Final

import pytest

from dataplatform.identity.filing_isin import (
    FILING_ALIASES_PATH,
    FilingAlias,
    FilingEdge,
    FilingIsinResolver,
    FilingIsinStatus,
    load_filing_aliases,
)
from dataplatform.identity.link_filing_history import (
    MAX_SETTLEMENT_SESSIONS,
    PRE_ERA_PATH,
    AliasFacts,
    PreEraFacts,
    PreEraLink,
    judge_alias,
    judge_pre_era,
    load_pre_era_links,
)
from dataplatform.ingest.nse.eras import ISIN_ERA_START

KOTAK_OLD: Final = "INE237A01010"  # what NSE's feeds state
KOTAK_MID: Final = "INE237A01028"  # 2010-09-20 .. 2026-01-13
KOTAK_NOW: Final = "INE237A01036"  # live since 2026-01-14

KOTAK: Final = PreEraLink(
    symbol="KOTAKBANK",
    old_isin=KOTAK_OLD,
    successor_isin=KOTAK_MID,
    effective_date=date(2010, 9, 20),
    provenance="owner ruling 2026-11-23",
    note="",
)
KOTAK_FACTS: Final = PreEraFacts(
    filing_symbols={"KOTAKBANK": 27},
    old_in_master=False,
    old_windows=0,
    old_edges=(),
    successor_predecessors=(),
    old_price_rows=0,
    old_in_equity_list=0,
    successor_first_nse=date(2010, 9, 20),
    symbol_isins_at_era_start=(KOTAK_MID,),
    chain=(KOTAK_MID, KOTAK_NOW),
    survivor_status="ACTIVE",
    survivor_symbol="KOTAKBANK",
    pre_era_splits=(date(2010, 9, 13),),
    split_to_effective_sessions=5,
    successor_in_master=False,
)

HUDCO_BOND: Final = "INE031A07873"
HUDCO_EQ: Final = "INE031A01017"
HUDCO: Final = FilingAlias(
    filed_isin=HUDCO_BOND,
    isin=HUDCO_EQ,
    effective_date=date(2017, 5, 19),
    symbol="HUDCO",
    issuer="Housing & Urban Development Corporation Ltd.",
    provenance="owner ruling 2026-11-23",
    note="",
)
HUDCO_FACTS: Final = AliasFacts(
    filing_symbols={"HUDCO": 21},
    filed_in_master=False,
    filed_windows=0,
    filed_edges=0,
    equity_status="ACTIVE",
    equity_symbol="HUDCO",
    issuer_active_equities=(HUDCO_EQ,),
    filed_price_symbols={"HUDCO": 375},
    equity_first_nse=date(2017, 5, 19),
)


# ── pre-era links ──────────────────────────────────────────────────────────────────────────────


def test_kotakbank_is_proved() -> None:
    assert judge_pre_era(KOTAK, KOTAK_FACTS) == []


def test_a_link_with_no_stored_split_is_proved_on_the_rest_of_the_evidence() -> None:
    """HAVELLS: no pre-era action dates the reissue; the edge states L1's own first session."""
    facts = replace(KOTAK_FACTS, pre_era_splits=(), split_to_effective_sessions=None)
    assert judge_pre_era(KOTAK, facts) == []


def test_an_already_applied_edge_is_still_proved() -> None:
    facts = replace(
        KOTAK_FACTS,
        old_edges=((KOTAK_OLD, KOTAK_MID, KOTAK.effective_date, "MANUAL"),),
        successor_predecessors=(KOTAK_OLD,),
    )
    assert judge_pre_era(KOTAK, facts) == []


def test_an_old_isin_that_is_another_securitys_isin_is_refused() -> None:
    """The ISIN belongs to a security in the master: linking it would hand that security's
    filings to KOTAKBANK."""
    refused = judge_pre_era(KOTAK, replace(KOTAK_FACTS, old_in_master=True))
    assert refused == ["the old ISIN is a security in the master"]


def test_an_old_isin_with_a_symbol_window_is_refused() -> None:
    assert judge_pre_era(KOTAK, replace(KOTAK_FACTS, old_windows=1))


def test_an_old_isin_that_traded_is_refused() -> None:
    assert judge_pre_era(KOTAK, replace(KOTAK_FACTS, old_price_rows=3))
    assert judge_pre_era(KOTAK, replace(KOTAK_FACTS, old_in_equity_list=1))


def test_filings_that_name_another_symbol_are_refused() -> None:
    facts = replace(KOTAK_FACTS, filing_symbols={"KOTAKBANK": 26, "KOTAKMF": 1})
    assert any("name" in r for r in judge_pre_era(KOTAK, facts))
    assert judge_pre_era(KOTAK, replace(KOTAK_FACTS, filing_symbols={}))


def test_another_issuer_code_is_refused() -> None:
    link = replace(KOTAK, old_isin="INE238A01018")
    assert any("issuer" in r for r in judge_pre_era(link, KOTAK_FACTS))


def test_the_inverted_direction_is_refused() -> None:
    """Serial 02 -> 01 would run time backwards: a reissue only ever raises the serial."""
    link = replace(KOTAK, old_isin=KOTAK_MID, successor_isin=KOTAK_OLD)
    assert any("serial" in r for r in judge_pre_era(link, KOTAK_FACTS))


def test_a_skipped_serial_is_refused() -> None:
    link = replace(KOTAK, successor_isin=KOTAK_NOW)
    facts = replace(KOTAK_FACTS, symbol_isins_at_era_start=(KOTAK_NOW,))
    assert any("serial" in r for r in judge_pre_era(link, facts))


def test_another_edge_on_either_isin_is_refused() -> None:
    other = (KOTAK_OLD, "INE237A01044", date(2012, 1, 2), "L1_CONTIGUITY")
    assert judge_pre_era(KOTAK, replace(KOTAK_FACTS, old_edges=(other,)))
    assert judge_pre_era(KOTAK, replace(KOTAK_FACTS, successor_predecessors=("INE237A01002",)))


def test_a_successor_first_seen_inside_the_isin_era_is_refused() -> None:
    """L1 contiguity sees such a reissue; a hand-written edge must not pre-empt it."""
    first = ISIN_ERA_START
    facts = replace(KOTAK_FACTS, successor_first_nse=first, pre_era_splits=())
    assert judge_pre_era(replace(KOTAK, effective_date=first), facts)


def test_an_effective_date_off_the_successors_first_session_is_refused() -> None:
    assert judge_pre_era(replace(KOTAK, effective_date=date(2010, 9, 13)), KOTAK_FACTS)


def test_a_symbol_trading_as_another_isin_at_the_era_start_is_refused() -> None:
    facts = replace(KOTAK_FACTS, symbol_isins_at_era_start=(KOTAK_MID, "INE999A01015"))
    assert judge_pre_era(KOTAK, facts)


def test_a_survivor_that_is_not_the_symbol_or_not_active_is_refused() -> None:
    assert judge_pre_era(KOTAK, replace(KOTAK_FACTS, survivor_symbol="KOTAKMAH"))
    assert judge_pre_era(KOTAK, replace(KOTAK_FACTS, survivor_status="DELISTED"))


def test_a_split_far_from_the_effective_date_or_two_splits_are_refused() -> None:
    far = replace(KOTAK_FACTS, split_to_effective_sessions=MAX_SETTLEMENT_SESSIONS + 1)
    assert judge_pre_era(KOTAK, far)
    after = replace(KOTAK_FACTS, split_to_effective_sessions=-1)
    assert judge_pre_era(KOTAK, after)
    two = replace(KOTAK_FACTS, pre_era_splits=(date(2006, 3, 1), date(2010, 9, 13)))
    assert judge_pre_era(KOTAK, two)


# ── filing aliases ─────────────────────────────────────────────────────────────────────────────


def test_hudco_is_proved() -> None:
    assert judge_alias(HUDCO, HUDCO_FACTS) == []


def test_an_alias_across_issuers_or_onto_a_bond_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="different issuer codes"):
        replace(HUDCO, isin="INE053F01010")  # IRFC's equity: another issuer
    with pytest.raises(ValueError, match="not an equity"):
        replace(HUDCO, isin="INE031A07881")
    with pytest.raises(ValueError, match="lineage edge"):
        replace(HUDCO, filed_isin="INE031A01009")


def test_an_issuer_with_a_second_active_equity_is_refused() -> None:
    facts = replace(HUDCO_FACTS, issuer_active_equities=(HUDCO_EQ, "INE031A01025"))
    assert judge_alias(HUDCO, facts)


def test_a_bond_that_traded_under_another_symbol_is_refused() -> None:
    """The bond is tied to the issuer by NSE printing it under the issuer's symbol."""
    assert judge_alias(HUDCO, replace(HUDCO_FACTS, filed_price_symbols={"IRFC": 10}))
    assert judge_alias(HUDCO, replace(HUDCO_FACTS, filed_price_symbols={}))


def test_an_alias_whose_equity_or_filings_name_another_symbol_is_refused() -> None:
    assert judge_alias(HUDCO, replace(HUDCO_FACTS, equity_symbol="NHB"))
    assert judge_alias(HUDCO, replace(HUDCO_FACTS, filing_symbols={"HUDCO": 2, "NHB": 1}))


def test_a_filed_isin_the_store_already_knows_is_refused() -> None:
    assert judge_alias(HUDCO, replace(HUDCO_FACTS, filed_in_master=True))
    assert judge_alias(HUDCO, replace(HUDCO_FACTS, filed_edges=1))


def test_an_alias_effective_off_the_equitys_first_session_is_refused() -> None:
    assert judge_alias(HUDCO, replace(HUDCO_FACTS, equity_first_nse=date(2017, 5, 22)))


# ── the resolver ───────────────────────────────────────────────────────────────────────────────


def test_a_bond_filing_resolves_to_its_issuers_equity_from_the_listing() -> None:
    resolver = FilingIsinResolver((), (HUDCO,))
    after = resolver.resolve(HUDCO_BOND, date(2026, 11, 1))
    assert (after.isin, after.status) == (HUDCO_EQ, FilingIsinStatus.REKEYED)
    before = resolver.resolve(HUDCO_BOND, date(2017, 5, 18))
    assert (before.isin, before.status) == (HUDCO_BOND, FilingIsinStatus.PRE_REISSUE)
    # Another issuer's bond is not touched.
    assert resolver.resolve("INE053F07520", date(2026, 11, 1)).isin == "INE053F07520"


def test_an_alias_chains_through_a_later_reissue_and_adds_no_fan_in() -> None:
    reissued = "INE031A01025"
    edges = (
        FilingEdge(HUDCO_EQ, reissued, date(2024, 1, 2), "L1_CONTIGUITY", "CORROBORATED", 0),
        FilingEdge("INE031A01009", HUDCO_EQ, date(2017, 5, 19), "MANUAL", "DERIVED", 0),
    )
    resolver = FilingIsinResolver(edges, (HUDCO,))
    assert resolver.resolve(HUDCO_BOND, date(2026, 1, 1)).isin == reissued
    assert resolver.resolve("INE031A01009", date(2026, 1, 1)).isin == reissued


def test_an_alias_for_an_isin_a_lineage_edge_leaves_is_refused() -> None:
    edge = FilingEdge(HUDCO_BOND, HUDCO_EQ, date(2017, 5, 19), "MANUAL", "DERIVED", 0)
    with pytest.raises(ValueError, match="also leaves"):
        FilingIsinResolver((edge,), (HUDCO,))


def test_a_pre_era_manual_edge_carries_kotakbank_to_the_live_isin() -> None:
    edges = (
        FilingEdge(KOTAK_OLD, KOTAK_MID, date(2010, 9, 20), "MANUAL", "DERIVED", 0),
        FilingEdge(KOTAK_MID, KOTAK_NOW, date(2026, 1, 14), "L1_CONTIGUITY", "CORROBORATED", 0),
    )
    resolver = FilingIsinResolver(edges)
    live = resolver.resolve(KOTAK_OLD, date(2026, 10, 20))
    assert (live.isin, live.status, live.hops) == (KOTAK_NOW, FilingIsinStatus.REKEYED, 2)
    # Point in time: a 2025 filing lands on the ISIN live then, not today's.
    assert resolver.resolve(KOTAK_OLD, date(2025, 10, 20)).isin == KOTAK_MID


# ── the checked-in files ───────────────────────────────────────────────────────────────────────


def test_the_checked_in_files_load_and_name_the_27() -> None:
    links = load_pre_era_links()
    aliases = load_filing_aliases()
    assert len(links) == 23
    assert {a.symbol for a in aliases} == {"HUDCO", "IREDA", "IRFC", "TATACAP"}
    for link in links:
        assert link.old_isin[:7] == link.successor_isin[:7]
        assert int(link.successor_isin[9:11]) == int(link.old_isin[9:11]) + 1
        assert link.effective_date < ISIN_ERA_START
        assert link.provenance == "owner ruling 2026-11-23"
    olds = {link.old_isin for link in links}
    assert not olds & {link.successor_isin for link in links}
    assert not olds & {a.filed_isin for a in aliases}
    assert PRE_ERA_PATH.parent == FILING_ALIASES_PATH.parent


def test_a_malformed_alias_file_fails_loud(tmp_path: Path) -> None:
    bad = tmp_path / "a.yaml"
    bad.write_text(
        "version: 1\naliases:\n  - filed_isin: INE031A07873\n    isin: INE053F01010\n"
        "    effective_date: 2017-05-19\n    symbol: HUDCO\n    issuer: x\n    provenance: y\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="different issuer codes"):
        load_filing_aliases(bad)
