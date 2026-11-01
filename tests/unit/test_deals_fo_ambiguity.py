"""An ambiguous master no longer fails a whole deals file or F&O session — #141's rule, shared.

`deals.resolve` and `fo_aggregates.build_aggregates` let `AmbiguousSymbolError` propagate, so one
symbol the identity master held two open windows for failed every deal of the day, or every
aggregate of the F&O session (and in `fo_backfill` escaped the session and ended the run) — the
pattern PR #141 fixed for delivery after CCAVENUE (2026-10-26 .. 28).

Neither file names an ISIN, so the evidence a session has is its own bhavcopy: when it states
exactly one of the master's candidates for the symbol, the row is placed there (layer 1). Otherwise
the row is quarantined — a deal held back from L1, an underlier's aggregate written ISIN-less — and
raised as an ERROR quality flag carrying what a human needs (layer 2). More than five distinct
ambiguous symbols in one file still fail it. The fixtures are real excerpts of the 2026-10-26 L0
payloads (`tests/fixtures/nse_deals/PROVENANCE.md`,
`tests/fixtures/nse_fo/ambiguity_26102026/PROVENANCE.md`); the broken master is constructed from two
real reissues, SSWL (INE802C01025 → 01033, 2022) and KOTAKBANK (INE237A01028 → 01036, 2026-01-14),
left with both windows open — the shape the 2026-10-10 and 2026-10-24 refreshes produced.
Offline: no network, no Postgres.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.identity.master import (
    AmbiguousSymbolError,
    Exchange,
    IdentityMaster,
    InMemoryReconciliationQueue,
    SymbolWindow,
)
from dataplatform.identity.session import SessionIdentity
from dataplatform.ingest.ambiguity import MAX_AMBIGUOUS_QUARANTINED, Ambiguity
from dataplatform.ingest.models import PriceRow
from dataplatform.ingest.nse import bhavcopy_udiff, deals, fo_bhavcopy
from dataplatform.ingest.nse.deals import (
    AMBIGUOUS_DEALS_CHECK,
    BULK_SOURCE_ID,
    AmbiguousDealsFileError,
    DealRow,
)
from dataplatform.ingest.nse.fo_bhavcopy import FoContractRow
from dataplatform.store import fo_aggregates
from dataplatform.store.fo_aggregates import (
    AMBIGUOUS_UNDERLYING_CHECK,
    FO_AGGREGATES_DATASET,
    AmbiguousFoFileError,
    UnderlyingKind,
)
from dataplatform.store.l1 import session_identity, write_prices_raw

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures"
DEALS_DIR: Final = FIXTURES / "nse_deals" / "ambiguity_26102026"
FO_DIR: Final = FIXTURES / "nse_fo" / "ambiguity_26102026"
BULK_FILE: Final = "bulk_26102026_EXCERPT.csv"
FO_FILE: Final = "BhavCopy_NSE_FO_0_0_0_20261026_F_0000_EXCERPT.csv"
CM_FILE: Final = "BhavCopy_NSE_CM_0_0_0_20261026_F_0000_EXCERPT.csv"

DAY: Final = date(2026, 10, 26)
SSWL_OLD: Final = "INE802C01025"
SSWL_NEW: Final = "INE802C01033"
PCJEWELLER: Final = "INE785M01021"
KOTAK_OLD: Final = "INE237A01028"
KOTAK_NEW: Final = "INE237A01036"
RELIANCE: Final = "INE002A01018"


def _window(symbol: str, isin: str, valid_from: date = date(2005, 6, 6)) -> SymbolWindow:
    return SymbolWindow(
        exchange=Exchange.NSE, symbol=symbol, valid_from=valid_from, valid_to=None, isin=isin
    )


def _deals_master(queue: InMemoryReconciliationQueue | None = None) -> IdentityMaster:
    """Two open SSWL windows (before and after its reissue); PCJEWELLER sound."""
    return IdentityMaster(
        (
            _window("SSWL", SSWL_OLD),
            _window("SSWL", SSWL_NEW),
            _window("PCJEWELLER", PCJEWELLER, date(2012, 12, 27)),
        ),
        queue=queue,
    )


def _fo_master(queue: InMemoryReconciliationQueue | None = None) -> IdentityMaster:
    """Two open KOTAKBANK windows (before and after its 2026-01-14 reissue); RELIANCE sound."""
    return IdentityMaster(
        (
            _window("KOTAKBANK", KOTAK_OLD, date(2003, 4, 21)),
            _window("KOTAKBANK", KOTAK_NEW, date(2003, 4, 21)),
            _window("RELIANCE", RELIANCE, date(1995, 11, 29)),
        ),
        queue=queue,
    )


def _deal_rows() -> tuple[DealRow, ...]:
    return deals.parse(
        (DEALS_DIR / BULK_FILE).read_bytes(), source=BULK_SOURCE_ID, filename=BULK_FILE
    )


def _prices(directory: Path) -> tuple[PriceRow, ...]:
    return bhavcopy_udiff.parse((directory / CM_FILE).read_bytes(), filename=CM_FILE)


def _session(rows: tuple[PriceRow, ...]) -> SessionIdentity:
    return SessionIdentity.from_statements(rows, exchange=Exchange.NSE, trade_date=DAY)


def _restated(rows: tuple[PriceRow, ...], symbol: str, isin: str) -> tuple[PriceRow, ...]:
    """The session's prices with one symbol's stated ISIN replaced — a statement we control."""
    return tuple(r.model_copy(update={"isin": isin}) if r.symbol == symbol else r for r in rows)


def _contracts() -> tuple[FoContractRow, ...]:
    return fo_bhavcopy.parse_text((FO_DIR / FO_FILE).read_text(), filename=FO_FILE)


# ── the fixtures are what they claim ────────────────────────────────────────────────────────


def test_the_fixtures_are_the_real_session_and_the_masters_really_are_ambiguous() -> None:
    assert [(r.symbol, r.client_name) for r in _deal_rows()] == [
        ("PCJEWELLER", "HRTI PRIVATE LIMITED"),
        ("PCJEWELLER", "JUMP TRADING FINANCIAL INDIA PRIVATE LIMITED"),
        ("SSWL", "JUNOMONETA FINSOL PRIVATE LIMITED"),
        ("SSWL", "PLUTUS WEALTH MANAGEMENT LLP"),
    ]
    assert {(p.symbol, p.isin) for p in _prices(DEALS_DIR)} == {
        ("PCJEWELLER", PCJEWELLER),
        ("SSWL", SSWL_NEW),
    }
    assert {(c.underlying, c.trade_date) for c in _contracts()} == {
        ("KOTAKBANK", DAY),
        ("RELIANCE", DAY),
        ("NIFTY", DAY),
    }
    assert {(p.symbol, p.isin) for p in _prices(FO_DIR)} == {
        ("KOTAKBANK", KOTAK_NEW),
        ("RELIANCE", RELIANCE),
    }
    with pytest.raises(AmbiguousSymbolError):
        _deals_master().try_resolve("SSWL", DAY)
    with pytest.raises(AmbiguousSymbolError):
        _fo_master().try_resolve("KOTAKBANK", DAY)


# ── the shared rule ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stated", "settled"),
    [
        ((), None),  # the session said nothing
        (("INE999Z01019",), None),  # it named an ISIN the master does not hold for the symbol
        ((SSWL_NEW,), SSWL_NEW),  # it named one candidate
        ((SSWL_NEW, "INE999Z01019"), SSWL_NEW),  # one candidate plus an unrelated series' ISIN
        ((SSWL_OLD, SSWL_NEW), None),  # it named both: nothing settled
    ],
)
def test_only_exactly_one_stated_candidate_settles(
    stated: tuple[str, ...], settled: str | None
) -> None:
    ambiguity = Ambiguity(candidates=(SSWL_OLD, SSWL_NEW), stated=stated)
    assert ambiguity.settled == settled
    assert ambiguity.reason.startswith("ambiguous")


def test_a_symbol_lookup_sees_every_series_and_only_its_own_session() -> None:
    stated = (
        *_prices(DEALS_DIR),
        _prices(DEALS_DIR)[1].model_copy(update={"series": "BE", "isin": SSWL_OLD}),
    )
    session = _session(stated)
    assert session.stated_for_symbol("sswl ", DAY, exchange=Exchange.NSE) == {SSWL_NEW, SSWL_OLD}
    assert session.stated_for_symbol("SSWL", date(2026, 10, 27), exchange=Exchange.NSE) == set()
    assert session.stated_for_symbol("SSWL", DAY, exchange=Exchange.BSE) == set()


# ── deals: layer 1, the session settles ──────────────────────────────────────────────────────


def test_the_session_places_the_ambiguous_deals_on_the_isin_it_names() -> None:
    queue = InMemoryReconciliationQueue()
    out = deals.resolve(
        _deal_rows(),
        _deals_master(queue),
        source=BULK_SOURCE_ID,
        session=_session(_prices(DEALS_DIR)),
    )
    assert out.ambiguous == () and out.unresolved == ()
    assert out.ambiguity_resolved_by_session == 2
    assert [(r.symbol, r.isin) for r in out.resolved] == [
        ("PCJEWELLER", PCJEWELLER),
        ("PCJEWELLER", PCJEWELLER),
        ("SSWL", SSWL_NEW),
        ("SSWL", SSWL_NEW),
    ]
    # Settling the file does not settle the master: the conflict stays queued for a human.
    assert {c.isins for c in queue.items} == {(SSWL_OLD, SSWL_NEW)}


def test_a_session_isin_outside_the_candidates_is_not_used() -> None:
    session = _session(_restated(_prices(DEALS_DIR), "SSWL", "INE999Z01019"))
    out = deals.resolve(_deal_rows(), _deals_master(), source=BULK_SOURCE_ID, session=session)
    assert out.ambiguity_resolved_by_session == 0
    assert [h.row.symbol for h in out.ambiguous] == ["SSWL", "SSWL"]
    assert all(h.ambiguity.stated == ("INE999Z01019",) for h in out.ambiguous)
    assert {r.symbol for r in out.resolved} == {"PCJEWELLER"}


# ── deals: layer 2, quarantined and flagged, the file publishes ──────────────────────────────


def test_without_the_session_the_ambiguous_deals_are_held_and_the_rest_resolve() -> None:
    out = deals.resolve(_deal_rows(), _deals_master(), source=BULK_SOURCE_ID, l0_key="k")
    assert [(r.symbol, r.isin) for r in out.resolved] == [
        ("PCJEWELLER", PCJEWELLER),
        ("PCJEWELLER", PCJEWELLER),
    ]
    assert [(h.row.client_name, h.l0_key) for h in out.ambiguous] == [
        ("JUNOMONETA FINSOL PRIVATE LIMITED", "k"),
        ("PLUTUS WEALTH MANAGEMENT LLP", "k"),
    ]
    assert all(h.ambiguity.candidates == (SSWL_OLD, SSWL_NEW) for h in out.ambiguous)
    # Nothing silently dropped: every parsed deal is resolved, unresolved or held.
    assert len(out.resolved) + len(out.unresolved) + len(out.ambiguous) == len(_deal_rows())


def test_held_deals_raise_one_error_flag_per_file_and_symbol_carrying_every_row() -> None:
    out = deals.resolve(_deal_rows(), _deals_master(), source=BULK_SOURCE_ID, l0_key="k")
    [finding] = deals.ambiguity_findings(out.ambiguous)

    assert finding.check_name == AMBIGUOUS_DEALS_CHECK
    assert finding.severity == "ERROR"
    assert finding.source == deals.DEALS_DATASET
    assert finding.isin is None
    assert finding.logical_date == DAY
    assert finding.detail["symbol"] == "SSWL"
    assert finding.detail["candidates"] == [SSWL_OLD, SSWL_NEW]
    assert finding.detail["deals_source"] == BULK_SOURCE_ID
    assert finding.detail["rows"] == [
        {
            "deal_type": "BULK",
            "security_name": "Steel Strips Wheels Limit",
            "client_name": "JUNOMONETA FINSOL PRIVATE LIMITED",
            "side": "BUY",
            "quantity": 988200,
            "price": "503.01",
            "remarks": None,
            "l0_key": "k",
        },
        {
            "deal_type": "BULK",
            "security_name": "Steel Strips Wheels Limit",
            "client_name": "PLUTUS WEALTH MANAGEMENT LLP",
            "side": "BUY",
            "quantity": 1127919,
            "price": "506.86",
            "remarks": None,
            "l0_key": "k",
        },
    ]
    again = deals.ambiguity_findings(
        deals.resolve(_deal_rows(), _deals_master(), source=BULK_SOURCE_ID, l0_key="k").ambiguous
    )
    assert [f.fingerprint for f in again] == [finding.fingerprint]


# ── deals: the threshold ─────────────────────────────────────────────────────────────────────


def _ambiguous_deals(symbols: int, rows_each: int = 1) -> tuple[IdentityMaster, list[DealRow]]:
    """`symbols` symbols each with two open windows, `rows_each` deals apiece."""
    template = _deal_rows()[2]
    windows: list[SymbolWindow] = []
    rows: list[DealRow] = []
    for k in range(symbols):
        symbol = f"REISSUED{k}"
        windows += [_window(symbol, f"INE9{k:02d}A01011"), _window(symbol, f"INE9{k:02d}A01029")]
        rows += [
            template.model_copy(update={"symbol": symbol, "quantity": n + 1})
            for n in range(rows_each)
        ]
    return IdentityMaster(tuple(windows)), rows


def test_up_to_the_threshold_the_deals_file_is_kept() -> None:
    master, rows = _ambiguous_deals(MAX_AMBIGUOUS_QUARANTINED)
    out = deals.resolve(rows, master, source=BULK_SOURCE_ID)
    assert len(out.ambiguous) == MAX_AMBIGUOUS_QUARANTINED


def test_over_the_threshold_the_deals_file_fails() -> None:
    master, rows = _ambiguous_deals(MAX_AMBIGUOUS_QUARANTINED + 1)
    with pytest.raises(AmbiguousDealsFileError, match="broken master"):
        deals.resolve(rows, master, source=BULK_SOURCE_ID)


def test_the_deals_threshold_counts_symbols_not_rows() -> None:
    """One reissued symbol with a dozen clients is one defect; it must not fail the file."""
    master, rows = _ambiguous_deals(1, rows_each=MAX_AMBIGUOUS_QUARANTINED * 3)
    out = deals.resolve(rows, master, source=BULK_SOURCE_ID)
    assert len(out.ambiguous) == MAX_AMBIGUOUS_QUARANTINED * 3


# ── F&O: layer 1 and layer 2 ─────────────────────────────────────────────────────────────────


def test_the_session_places_the_ambiguous_underlier_on_the_isin_it_names() -> None:
    queue = InMemoryReconciliationQueue()
    derived = fo_aggregates.derive_aggregates(
        _contracts(), master=_fo_master(queue), session=_session(_prices(FO_DIR))
    )
    assert derived.ambiguous == () and derived.ambiguity_resolved_by_session == 1
    assert {a.underlying: a.isin for a in derived.aggregates} == {
        "KOTAKBANK": KOTAK_NEW,
        "NIFTY": None,
        "RELIANCE": RELIANCE,
    }
    assert {c.isins for c in queue.items} == {(KOTAK_OLD, KOTAK_NEW)}


def test_an_unsettled_underlier_keeps_its_numbers_and_loses_only_its_isin() -> None:
    settled = fo_aggregates.derive_aggregates(
        _contracts(), master=_fo_master(), session=_session(_prices(FO_DIR))
    )
    held = fo_aggregates.derive_aggregates(
        _contracts(),
        master=_fo_master(),
        session=_session(_restated(_prices(FO_DIR), "KOTAKBANK", "INE999Z01019")),
    )
    by = {a.underlying: a for a in held.aggregates}
    assert by["KOTAKBANK"].isin is None
    assert by["KOTAKBANK"].underlying_kind is UnderlyingKind.STOCK
    assert by["RELIANCE"].isin == RELIANCE
    [kotak] = held.ambiguous
    assert (kotak.underlying, kotak.ambiguity.candidates) == ("KOTAKBANK", (KOTAK_OLD, KOTAK_NEW))
    assert held.unresolved == 0  # ambiguous is not "unknown"
    # Every number but the ISIN is the settled derivation's: nothing about the row was dropped.
    reference = {a.underlying: a for a in settled.aggregates}["KOTAKBANK"]
    assert by["KOTAKBANK"] == reference.model_copy(update={"isin": None})
    assert by["KOTAKBANK"].total_oi == 33104000 + 122186000
    assert by["KOTAKBANK"].basis == Decimal("-0.9000")


def test_an_unsettled_underlier_raises_one_error_flag_scoped_to_fo_aggregates() -> None:
    held = fo_aggregates.derive_aggregates(_contracts(), master=_fo_master()).ambiguous
    [finding] = fo_aggregates.ambiguity_findings(held)
    assert finding.check_name == AMBIGUOUS_UNDERLYING_CHECK
    assert finding.severity == "ERROR"
    assert finding.source == FO_AGGREGATES_DATASET
    assert finding.isin is None
    assert finding.detail["symbol"] == "KOTAKBANK"
    assert finding.detail["candidates"] == [KOTAK_OLD, KOTAK_NEW]
    assert finding.detail["session_isins"] == []
    [again] = fo_aggregates.ambiguity_findings(
        fo_aggregates.derive_aggregates(_contracts(), master=_fo_master()).ambiguous
    )
    assert again.fingerprint == finding.fingerprint


def _ambiguous_underliers(n: int) -> tuple[IdentityMaster, list[FoContractRow]]:
    kotak = [c for c in _contracts() if c.underlying == "KOTAKBANK"]
    windows: list[SymbolWindow] = []
    rows: list[FoContractRow] = []
    for k in range(n):
        symbol = f"REISSUED{k}"
        windows += [_window(symbol, f"INE9{k:02d}A01011"), _window(symbol, f"INE9{k:02d}A01029")]
        rows += [c.model_copy(update={"underlying": symbol}) for c in kotak]
    return IdentityMaster(tuple(windows)), rows


def test_up_to_the_threshold_the_fo_session_is_kept() -> None:
    master, rows = _ambiguous_underliers(MAX_AMBIGUOUS_QUARANTINED)
    derived = fo_aggregates.derive_aggregates(rows, master=master)
    assert len(derived.ambiguous) == MAX_AMBIGUOUS_QUARANTINED
    assert len(derived.aggregates) == MAX_AMBIGUOUS_QUARANTINED


def test_over_the_threshold_the_fo_session_fails() -> None:
    master, rows = _ambiguous_underliers(MAX_AMBIGUOUS_QUARANTINED + 1)
    with pytest.raises(AmbiguousFoFileError, match="broken master"):
        fo_aggregates.derive_aggregates(rows, master=master)
    with pytest.raises(AmbiguousFoFileError):
        fo_aggregates.build_aggregates(rows, master=master)


# ── the session's statement, read back from L1 ───────────────────────────────────────────────


def test_session_identity_reads_the_sessions_own_prices_raw(tmp_path: Path) -> None:
    write_prices_raw(_prices(DEALS_DIR), exchange=Exchange.NSE, data_root=tmp_path)
    session = session_identity(DAY, data_root=tmp_path)
    assert session is not None
    assert session.stated_for_symbol("SSWL", DAY, exchange=Exchange.NSE) == {SSWL_NEW}
    # No partition: no evidence — the caller quarantines rather than guesses.
    assert session_identity(date(2026, 10, 27), data_root=tmp_path) is None
    # Before the ISIN era a prices_raw ISIN is the master's inference, not the session's word.
    assert session_identity(date(2010, 1, 4), data_root=tmp_path) is None
