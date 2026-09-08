"""The symbol-keyed dated membership dataset and its census (W2).

The census's whole output is numbers nobody will re-derive by hand — a per-index constituent
table, 154 change events, a sector coverage percentage — so each one that goes into the committed
report is asserted here against a lake the test builds itself.

Four things get a test rather than a reading:

1. **A change event is diffed against the index's own previous session**, not the previous
   calendar day. An index that arrived late must not report its entire constituent list as one
   enormous change on the day it first appears, and the sectorals all arrive late.
2. **A session cannot be added twice.** This archive really does serve one payload under two date
   keys (the W2 close-out found it), and silently letting the second overwrite the first would
   erase whatever change event sat between them.
3. **The undatable-bundle recovery is narrow and payload-derived.** Two of the 827 bundles cannot
   be dated by `PrBundle`; their `ffix` member is dated from its own filename. That path must fire
   for exactly those shapes and must never invent a date from a clock or a calendar.
4. **The sector measures are three different questions.** A symbol holding two sectorals at once
   is a multi-assignment; one that gains and loses between consecutive assigned sessions has been
   reclassified; one whose sector union is wider than any single day's has changed sector
   *somehow*, including gradually. A reader that conflated them would answer the brief's sector
   question wrongly, so each has its own test with its own shape.

Offline and deterministic: the four `ffix` era fixtures written into a `tmp_path` lake, plus
synthetic bundles built in memory where a real one cannot produce the shape under test. Nothing
here reads the authoritative lake or the network.
"""

from __future__ import annotations

import zipfile
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import pytest

from dataplatform.clock import FrozenClock
from dataplatform.ingest.calendar import TradingCalendar, trading_calendar
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID
from dataplatform.ingest.nse.pr_bundle.ffix import FFIX_COLUMNS, parse_ffix
from dataplatform.ingest.nse.pr_bundle.membership import (
    NOMINAL_SIZES,
    SECTORAL_INDICES,
    MembershipCensus,
    SymbolKeyedIndexMembership,
    census_ffix_corpus,
    render_census,
)
from dataplatform.store.l0 import L0Store

IST: Final = ZoneInfo("Asia/Kolkata")
FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle"

#: The four frozen `ffix` eras, and the session each publishes.
ERAS: Final[tuple[tuple[str, str, date], ...]] = (
    ("ffix_three_index", "PR040110.zip", date(2010, 1, 4)),
    ("ffix_seventeen_index", "PR310111.zip", date(2011, 1, 31)),
    ("ffix_renamed_banner", "PR040313.zip", date(2013, 3, 4)),
    ("ffix_banner_relapse", "PR090413.zip", date(2013, 4, 9)),
)


def _store(root: Path) -> L0Store:
    return L0Store(clock=FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=IST)), data_root=root)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A lake at `tmp_path` holding the four `ffix` era fixtures under their real keys."""
    store = _store(tmp_path)
    for era, name, day in ERAS:
        store.put(PR_BUNDLE_SOURCE_ID, day, name, (FIXTURES / era / name).read_bytes())
    return tmp_path


def _calendar() -> TradingCalendar:
    return trading_calendar()


def _ffix(*rows: str) -> bytes:
    """A synthetic `ffix` payload from `INDEX_FLG,SYMBOL` pairs, padded to the real 9 columns."""
    body = [",".join(FFIX_COLUMNS)]
    for row in rows:
        index_name, symbol = row.split(":", 1)
        body.append(f"{index_name},{symbol},EQ,{symbol} LTD,1000,0.5,100.00,50000.00,1.00")
    return ("\n".join(body) + "\n").encode("latin-1")


def _bundle_zip(members: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _dataset(*sessions: tuple[date, tuple[str, ...]]) -> SymbolKeyedIndexMembership:
    """A dataset built from `(session, ("INDEX:SYMBOL", …))` pairs."""
    dataset = SymbolKeyedIndexMembership()
    for day, rows in sessions:
        dataset.add(parse_ffix(_ffix(*rows), filename="ffix010110.csv", knowable_date=day))
    return dataset


# ── the dataset is symbol-keyed, and refuses to merge a session ──────────────────────────────


def test_the_dataset_holds_symbols_and_offers_no_isin_anywhere() -> None:
    """Invariant #2: the dataset must not expose a join key it does not have."""
    dataset = _dataset((date(2010, 1, 4), ("NIFTY:ABB", "NIFTY:ACC")))

    assert dataset.constituents("NIFTY", date(2010, 1, 4)) == frozenset({"ABB", "ACC"})
    assert not any("isin" in name.lower() for name in dir(SymbolKeyedIndexMembership))


def test_adding_a_session_twice_raises_rather_than_merging() -> None:
    """The archive serves one payload under two date keys; a silent overwrite loses a change.

    On 2018-01-02 the host served the *2019* bundle (W2 close-out §3). If that ever happens inside
    the `ffix` span, the second add must stop the sweep rather than replace the first session's
    membership — which would erase every change event between the two.
    """
    dataset = _dataset((date(2010, 1, 4), ("NIFTY:ABB",)))

    with pytest.raises(ValueError, match="added twice"):
        dataset.add(
            parse_ffix(
                _ffix("NIFTY:ACC"), filename="ffix040110.csv", knowable_date=date(2010, 1, 4)
            )
        )


def test_an_absent_index_reads_as_empty_and_not_as_an_error() -> None:
    dataset = _dataset((date(2010, 1, 4), ("NIFTY:ABB",)))

    assert dataset.constituents("CNX 500", date(2010, 1, 4)) == frozenset()
    assert dataset.sessions_for("CNX 500") == ()


# ── change events are diffed against the index's own previous session ────────────────────────


def test_the_first_session_an_index_appears_on_is_never_a_change() -> None:
    """The sectorals all arrive mid-span; their arrival must not read as a mass addition."""
    dataset = _dataset(
        (date(2010, 1, 4), ("NIFTY:ABB",)),
        (date(2010, 1, 5), ("NIFTY:ABB", "CNX IT:INFY", "CNX IT:WIPRO")),
    )

    assert dataset.changes("CNX IT") == ()
    assert dataset.changes("NIFTY") == ()


def test_a_change_names_the_symbols_and_both_sessions() -> None:
    dataset = _dataset(
        (date(2010, 1, 4), ("NIFTY:ABB", "NIFTY:ACC")),
        (date(2010, 1, 5), ("NIFTY:ABB", "NIFTY:ACC")),
        (date(2010, 1, 6), ("NIFTY:ABB", "NIFTY:CIPLA")),
    )

    (change,) = dataset.changes("NIFTY")
    assert change.day == date(2010, 1, 6)
    assert change.previous_day == date(2010, 1, 5)
    assert change.added == ("CIPLA",)
    assert change.removed == ("ACC",)
    assert change.size == 2


def test_a_gap_in_an_index_diffs_against_its_previous_available_session() -> None:
    """An index absent for a session must not report its whole list as removed and re-added.

    This is the bug the "previous *available* session" rule exists to prevent: diffing against
    the previous calendar day would emit two enormous spurious events around any absence.
    """
    dataset = _dataset(
        (date(2010, 1, 4), ("NIFTY:ABB", "CNX IT:INFY")),
        (date(2010, 1, 5), ("NIFTY:ABB",)),
        (date(2010, 1, 6), ("NIFTY:ABB", "CNX IT:INFY")),
    )

    assert dataset.changes("CNX IT") == ()
    assert dataset.changes("NIFTY") == ()


def test_an_unchanged_set_across_many_sessions_emits_no_events() -> None:
    dataset = _dataset(
        *((date(2010, 1, day), ("NIFTY:ABB", "NIFTY:ACC")) for day in (4, 5, 6, 7, 8, 11, 12, 13))
    )

    assert dataset.changes("NIFTY") == ()
    assert len(dataset.sessions) == 8


# ── the sector measures are three different questions ────────────────────────────────────────


def test_a_simultaneous_multi_sector_symbol_is_not_counted_as_a_mover() -> None:
    census = _synthetic_census(
        (date(2011, 1, 31), ("CNX IT:INFY", "CNX SERVICE:INFY")),
        (date(2011, 2, 1), ("CNX IT:INFY", "CNX SERVICE:INFY")),
    )

    assert census.sectoral.symbols_with_any_assignment == 1
    assert census.sectoral.symbols_with_simultaneous_assignments == 1
    assert census.sectoral.max_simultaneous == 2
    assert census.sectoral.movers == ()
    assert census.sectoral.symbols_whose_sector_changed == ()


def test_a_symbol_that_gains_and_loses_on_one_date_is_a_reclassification() -> None:
    census = _synthetic_census(
        (date(2011, 1, 31), ("CNX IT:PATNI",)),
        (date(2011, 2, 1), ("CNX SERVICE:PATNI",)),
    )

    (move,) = census.sectoral.movers
    assert move.symbol == "PATNI"
    assert move.day == date(2011, 2, 1)
    assert move.previous_day == date(2011, 1, 31)
    assert move.left == ("CNX IT",)
    assert move.joined == ("CNX SERVICE",)


def test_a_dormant_session_widens_the_bracket_but_is_still_a_reclassification() -> None:
    """PATNI holds no sector on 2011-02-01 and reappears in a different one on 02-02.

    `previous_day` is the symbol's own previous *assigned* session, so this is one `SectorMove`
    with a two-session bracket rather than two events or none. Asserting the bracket is the point:
    a caller reading `day` as "the day it happened" would be wrong by a session here, and the
    field names and this test are what say so.
    """
    census = _synthetic_census(
        (date(2011, 1, 31), ("CNX IT:PATNI", "CNX FMCG:ITC")),
        (date(2011, 2, 1), ("CNX FMCG:ITC",)),
        (date(2011, 2, 2), ("CNX SERVICE:PATNI", "CNX FMCG:ITC")),
    )

    (move,) = census.sectoral.movers
    assert move.symbol == "PATNI"
    assert move.previous_day == date(2011, 1, 31)
    assert move.day == date(2011, 2, 2)
    assert census.sectoral.max_simultaneous == 1


def test_a_gradual_sector_migration_is_caught_by_the_wider_measure_only() -> None:
    """The shape a same-step test structurally cannot see, and the reason there are two measures.

    PATNI sits in `CNX IT`, then in both, then only in `CNX SERVICE`. No single step both gains
    and loses, so `movers` is empty — but its sector did change, and a point-in-time series that
    pinned it to `CNX SERVICE` would misclassify it for the first third of the span.
    """
    census = _synthetic_census(
        (date(2011, 1, 31), ("CNX IT:PATNI",)),
        (date(2011, 2, 1), ("CNX IT:PATNI", "CNX SERVICE:PATNI")),
        (date(2011, 2, 2), ("CNX SERVICE:PATNI",)),
    )

    assert census.sectoral.movers == ()
    assert census.sectoral.symbols_whose_sector_changed == ("PATNI",)
    assert census.sectoral.max_simultaneous == 2


def test_the_sector_measures_only_look_at_sectoral_indices() -> None:
    """A symbol moving between two *broad* indices is not a sector reclassification."""
    census = _synthetic_census(
        (date(2011, 1, 31), ("NIFTY:ABB",)),
        (date(2011, 2, 1), ("JR. NIFTY:ABB",)),
    )

    assert census.sectoral.symbols_with_any_assignment == 0
    assert census.sectoral.movers == ()
    assert census.sectoral.symbols_whose_sector_changed == ()


def test_the_sectoral_registry_and_the_nominal_sizes_do_not_overlap() -> None:
    """A sectoral index is as-many-as-qualify, so none of them may assert a nominal size."""
    assert not SECTORAL_INDICES & set(NOMINAL_SIZES)
    assert len(SECTORAL_INDICES) == 10


def test_coverage_is_none_until_the_denominator_is_measured() -> None:
    """The brief asked for a measurement, so an unmeasured denominator must not become a guess."""
    census = _synthetic_census((date(2011, 1, 31), ("CNX IT:INFY",)))

    assert census.sectoral.traded_universe_symbols is None
    assert census.sectoral.coverage_fraction is None
    assert "not measured in this run" in render_census(census)


# ── the census over the real era fixtures ────────────────────────────────────────────────────


def test_the_census_reads_the_four_era_fixtures_and_counts_them(lake: Path) -> None:
    census = census_ffix_corpus(
        _store(lake), calendar=_calendar(), start=date(2010, 1, 4), end=date(2013, 4, 30)
    )

    assert census.bundles_swept == 4
    assert census.ffix_sessions == 4
    assert census.first_session == date(2010, 1, 4)
    assert census.last_session == date(2013, 4, 9)
    # 200 rows in the first file, 1,029 in each of the other three.
    assert census.rows == 200 + 1029 * 3
    assert len(census.indices) == 17
    assert census.failures == ()
    assert census.recovered == ()


def test_no_headline_index_is_ever_off_its_nominal_size(lake: Path) -> None:
    """The completeness check: 50 of 50 and 500 of 500, with furniture interleaved throughout.

    A reader dropping rows would show up here before anywhere else, and an off-nominal count in
    the committed report has to be explained as a corporate event rather than left ambiguous.
    """
    census = census_ffix_corpus(
        _store(lake), calendar=_calendar(), start=date(2010, 1, 4), end=date(2013, 4, 30)
    )

    for index in census.indices:
        assert index.anomalies == (), (index.index_name, index.anomalies)
        if index.nominal_size is not None:
            assert index.min_count == index.max_count == index.nominal_size


def test_the_census_reports_the_real_april_2013_nifty_reconstitution(lake: Path) -> None:
    """Between the two 2013 fixtures the NIFTY genuinely changed, and by four symbols."""
    census = census_ffix_corpus(
        _store(lake), calendar=_calendar(), start=date(2013, 1, 1), end=date(2013, 4, 30)
    )

    nifty = next(index for index in census.indices if index.index_name == "NIFTY")
    (change,) = nifty.changes
    assert change.day == date(2013, 4, 9)
    assert change.previous_day == date(2013, 3, 4)
    assert change.added == ("INDUSINDBK", "NMDC")
    assert change.removed == ("SIEMENS", "WIPRO")


def test_the_banner_rename_produces_no_change_event(lake: Path) -> None:
    """The regression a banner-keyed reader would cause: 17 phantom indices and a mass churn."""
    census = census_ffix_corpus(
        _store(lake), calendar=_calendar(), start=date(2013, 1, 1), end=date(2013, 4, 30)
    )

    assert len(census.indices) == 17
    assert "CNX Nifty Sec." not in {index.index_name for index in census.indices}
    assert "S&P CNX Nifty Sec." in census.announced_banners
    assert "CNX Nifty Sec." in census.announced_banners


def test_a_gappy_lake_reports_the_missing_sessions_by_date(lake: Path) -> None:
    """The fixture lake holds 4 of 827 sessions, so the reconcile must say so loudly."""
    census = census_ffix_corpus(
        _store(lake), calendar=_calendar(), start=date(2010, 1, 4), end=date(2010, 1, 8)
    )

    assert not census.contiguous
    assert census.expected_sessions == 5
    assert census.missing_sessions == (
        date(2010, 1, 5),
        date(2010, 1, 6),
        date(2010, 1, 7),
        date(2010, 1, 8),
    )
    assert census.unexpected_sessions == ()


def test_the_report_renders_every_section_off_the_real_fixtures(lake: Path) -> None:
    census = census_ffix_corpus(
        _store(lake),
        calendar=_calendar(),
        start=date(2010, 1, 4),
        end=date(2013, 4, 30),
        universe=(1842, 832),
    )

    body = render_census(census, title="ffix census")

    for heading in (
        "# ffix census",
        "## 1. Per index",
        "## 2. Contiguity against the trading calendar",
        "## 3. Observable membership changes",
        "## 4. Distinct symbols",
        "## 5. Dated sectoral assignment",
        "## The honest limits",
        "## 7. Is this enough to validate a backward reconstruction",
    ):
        assert heading in body, heading
    # The L0 root the numbers came from is in the report, so a census run against the wrong lake
    # is visible in the artefact rather than only in the log.
    assert str(census.l0_root) in body
    assert "1,842" in body


# ── the undatable-bundle recovery ────────────────────────────────────────────────────────────


def test_a_bundle_whose_members_disagree_is_recovered_from_the_ffix_member_name(
    tmp_path: Path,
) -> None:
    """The 2011-08-19 shape: a stale member from the previous session blocks `PrBundle`'s dating.

    Recovery dates the `ffix` member from `ffix190811.csv` — the payload's own bytes, not a clock
    and not the archive filename.
    """
    payload = _bundle_zip(
        {
            "ffix190811.csv": _ffix("NIFTY:ABB"),
            "Gl190811.csv": b"x\n",
            "NPD180811.txt": b"stale\n",  # the previous session's file, left in the bundle
        }
    )
    store = _store(tmp_path)
    store.put(PR_BUNDLE_SOURCE_ID, date(2011, 8, 19), "PR190811.zip", payload)

    census = census_ffix_corpus(
        store, calendar=_calendar(), start=date(2011, 8, 19), end=date(2011, 8, 19)
    )

    (recovered,) = census.recovered
    assert recovered.filename == "PR190811.zip"
    assert recovered.ffix_member == "ffix190811.csv"
    assert recovered.recovered_date == date(2011, 8, 19)
    assert "disagree" in recovered.why
    assert census.ffix_sessions == 1
    assert census.failures == ()


def test_a_bundle_with_every_member_nested_in_a_directory_is_recovered(tmp_path: Path) -> None:
    """The 2013-01-10 shape: `nupr100113/` prefixes every name, so `PrBundle` dates nothing."""
    payload = _bundle_zip(
        {
            "nupr100113/ffix100113.csv": _ffix("NIFTY:ABB"),
            "nupr100113/Readme.txt": b"readme\n",
        }
    )
    store = _store(tmp_path)
    store.put(PR_BUNDLE_SOURCE_ID, date(2013, 1, 10), "PR100113.zip", payload)

    census = census_ffix_corpus(
        store, calendar=_calendar(), start=date(2013, 1, 10), end=date(2013, 1, 10)
    )

    (recovered,) = census.recovered
    assert recovered.ffix_member == "nupr100113/ffix100113.csv"
    assert recovered.recovered_date == date(2013, 1, 10)
    assert census.ffix_sessions == 1


def test_recovery_does_not_fire_when_there_are_two_candidate_ffix_members(
    tmp_path: Path,
) -> None:
    """Two members disagreeing about the date is not something to pick a winner from.

    The original `ParseError` propagates and the bundle lands in `failures`, which is what a
    format change worth a human look should do.
    """
    payload = _bundle_zip(
        {
            "ffix190811.csv": _ffix("NIFTY:ABB"),
            "sub/ffix180811.csv": _ffix("NIFTY:ACC"),
            "NPD180811.txt": b"stale\n",
        }
    )
    store = _store(tmp_path)
    store.put(PR_BUNDLE_SOURCE_ID, date(2011, 8, 19), "PR190811.zip", payload)

    census = census_ffix_corpus(
        store, calendar=_calendar(), start=date(2011, 8, 19), end=date(2011, 8, 19)
    )

    assert census.recovered == ()
    assert census.ffix_sessions == 0
    ((day, filename, message),) = census.failures
    assert day == date(2011, 8, 19)
    assert filename == "PR190811.zip"
    assert "disagree" in message


def test_recovery_never_dates_a_member_whose_name_carries_no_date(tmp_path: Path) -> None:
    """`ffix.csv` is undatable, and an undatable member must not be given today's date."""
    payload = _bundle_zip({"ffix.csv": _ffix("NIFTY:ABB"), "Readme.txt": b"x\n"})
    store = _store(tmp_path)
    store.put(PR_BUNDLE_SOURCE_ID, date(2011, 8, 19), "PR190811.zip", payload)

    census = census_ffix_corpus(
        store, calendar=_calendar(), start=date(2011, 8, 19), end=date(2011, 8, 19)
    )

    assert census.recovered == ()
    assert len(census.failures) == 1


def test_a_bundle_with_no_ffix_member_is_counted_and_not_a_failure(tmp_path: Path) -> None:
    """The normal case outside 2010-2013: absence is a fact about the era, not an error."""
    payload = _bundle_zip({"Bc020116.csv": b"x\n", "Readme.txt": b"y\n"})
    store = _store(tmp_path)
    store.put(PR_BUNDLE_SOURCE_ID, date(2016, 1, 2), "PR020116.zip", payload)

    census = census_ffix_corpus(
        store, calendar=_calendar(), start=date(2016, 1, 1), end=date(2016, 1, 2)
    )

    assert census.bundles_without_ffix == (date(2016, 1, 2),)
    assert census.failures == ()
    assert census.ffix_sessions == 0


def test_a_bundle_that_is_not_a_zip_is_collected_so_the_sweep_finishes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put(PR_BUNDLE_SOURCE_ID, date(2011, 1, 3), "PR030111.zip", b"<html>nope</html>")
    store.put(
        PR_BUNDLE_SOURCE_ID,
        date(2011, 1, 4),
        "PR040111.zip",
        _bundle_zip({"ffix040111.csv": _ffix("NIFTY:ABB")}),
    )

    census = census_ffix_corpus(
        store, calendar=_calendar(), start=date(2011, 1, 3), end=date(2011, 1, 4)
    )

    assert len(census.failures) == 1
    assert census.failures[0][0] == date(2011, 1, 3)
    # The sweep did not stop: the good bundle after the bad one still landed.
    assert census.ffix_sessions == 1


def test_a_broken_ffix_member_fails_loudly_rather_than_reading_as_an_empty_index(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.put(
        PR_BUNDLE_SOURCE_ID,
        date(2011, 1, 4),
        "PR040111.zip",
        _bundle_zip({"ffix040111.csv": b"WRONG,HEADER\n1,2\n"}),
    )

    census = census_ffix_corpus(
        store, calendar=_calendar(), start=date(2011, 1, 4), end=date(2011, 1, 4)
    )

    assert census.ffix_sessions == 0
    assert "unexpected header" in census.failures[0][2]


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _synthetic_census(*sessions: tuple[date, tuple[str, ...]]) -> MembershipCensus:
    """A census over an in-memory lake of synthetic bundles, one per given session.

    Used where the real fixtures cannot produce the shape under test — a sector reclassification
    on consecutive sessions, say. The calendar is the shipped one, so the sessions must be real
    trading days; `census_ffix_corpus` raises on an uncovered range rather than assuming.
    """
    import tempfile

    root = Path(tempfile.mkdtemp())
    store = _store(root)
    for day, rows in sessions:
        name = f"PR{day.strftime('%d%m%y')}.zip"
        member = f"ffix{day.strftime('%d%m%y')}.csv"
        store.put(PR_BUNDLE_SOURCE_ID, day, name, _bundle_zip({member: _ffix(*rows)}))
    days = [day for day, _ in sessions]
    census = census_ffix_corpus(store, calendar=_calendar(), start=min(days), end=max(days))
    assert census.failures == (), census.failures
    return census


def test_the_census_module_resolves_no_symbol_to_an_isin() -> None:
    """The invariant-#2 guard, at source level: no path here can reach `security_master`.

    Behavioural tests prove only that the paths they exercised avoided it. Reading the source
    proves there is no path at all — the same technique `test_pr_bundle_bc.py` uses for the clock.
    """
    import inspect
    import re

    from dataplatform.ingest.nse.pr_bundle import membership as module

    source = Path(inspect.getsourcefile(module) or "").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    # Docstrings legitimately *name* the identity work this module refuses to do; strip them
    # before grepping for a call, the same way `test_pr_bundle_bc.py` does for the clock.
    code = re.sub(r"\"\"\".*?\"\"\"", "", code, flags=re.DOTALL)
    for forbidden in ("security_master", "SecurityMaster", "resolve_isin", "isin="):
        assert forbidden not in code, f"membership.py reaches for identity: {forbidden}"


def test_a_parse_error_from_the_reader_is_a_parse_error_and_not_a_crash() -> None:
    """`ParseError` is the contract the census's failure collection is written against."""
    with pytest.raises(ParseError):
        parse_ffix(b"nope\n", filename="ffix010110.csv", knowable_date=date(2010, 1, 1))
