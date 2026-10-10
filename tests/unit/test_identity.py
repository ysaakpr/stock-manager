"""M1.7: the identity master's rules, asserted offline against frozen NSE files.

Everything here runs with no database, no network and no clock of its own (B8, B10). The three
acceptance criteria are exercised twice — once as pure logic here, once against Postgres in
`tests/integration/test_identity_ingest.py` — because the interesting failures are on different
sides of that line: getting a window boundary wrong is a logic bug, and losing a closed window on
re-ingest is a SQL bug.

The symbol-change case is real and is the whole reason invariant #2 exists: Cadila Healthcare
became Zydus Lifesciences on 2022-03-07 without changing ISIN, so a price table keyed on
`CADILAHC` and one keyed on `ZYDUSLIFE` describe the same company and would never join.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.identity.ingest import (
    EQUITY_LIST_COLUMNS,
    NSE_EQUITY_LIST_SOURCE,
    REISSUE_EVIDENCE_EQUITY_LIST,
    REISSUE_EVIDENCE_LINEAGE,
    ClampedWindow,
    DerivedMaster,
    IdentityParseError,
    L0EquityListSeries,
    ReissueBoundary,
    ReissueResolution,
    derive_master,
    equity_list_filename,
    parse_equity_list,
    parse_symbol_changes,
    resolve_reissues,
)
from dataplatform.identity.master import (
    AmbiguousSymbolError,
    ConflictKind,
    DetectedBy,
    Exchange,
    HistoryPlan,
    IdentityMaster,
    InMemoryReconciliationQueue,
    ListingStatus,
    SymbolWindow,
    UnknownIsinError,
    UnknownSymbolError,
    detect_conflicts,
    plan_history,
)
from dataplatform.store.l0 import L0Store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nse_equity_list" / "2026-08-08"

#: The two renames every assertion below is anchored on, taken verbatim from `symbolchange.csv`.
#: Both keep their ISIN across the rename, which is exactly what makes a symbol join wrong.
ZYDUS_ISIN = "INE010B01027"
ZYDUS_CHANGE = date(2022, 3, 7)
LTIM_ISIN = "INE214T01019"
LTIM_CHANGE = date(2022, 12, 5)


@pytest.fixture(scope="module")
def equity_list_text() -> str:
    return (FIXTURES / "EQUITY_L.csv").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def symbol_change_text() -> str:
    return (FIXTURES / "symbolchange.csv").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def master(equity_list_text: str, symbol_change_text: str) -> IdentityMaster:
    """The whole NSE master as of the frozen 2026-08-08 snapshot, built purely in memory."""
    derived = derive_master(
        parse_equity_list(equity_list_text),
        parse_symbol_changes(symbol_change_text),
        snapshot_date=date(2026, 8, 8),
    )
    return IdentityMaster(derived.windows, securities=derived.securities, listings=derived.listings)


# ── parsing the frozen files ────────────────────────────────────────────────────────────────


def test_equity_list_parses_every_row(equity_list_text: str) -> None:
    rows = parse_equity_list(equity_list_text)
    assert len(rows) == 2397
    zydus = next(row for row in rows if row.symbol == "ZYDUSLIFE")
    assert zydus.isin == ZYDUS_ISIN
    assert zydus.series == "EQ"
    assert zydus.listing_date == date(2000, 4, 18)
    assert zydus.face_value_inr == Decimal("1")
    assert isinstance(zydus.face_value_inr, Decimal), "money is Decimal, never float"


def test_equity_list_symbols_and_isins_are_unique_within_a_snapshot(equity_list_text: str) -> None:
    """The premise the ambiguity check rests on: one snapshot alone can never be ambiguous."""
    rows = parse_equity_list(equity_list_text)
    assert len({row.symbol for row in rows}) == len(rows)
    assert len({row.isin for row in rows}) == len(rows)


def test_a_reordered_header_is_rejected() -> None:
    """A silently reordered column would load company names into the ISIN field."""
    swapped = ",".join(reversed(EQUITY_LIST_COLUMNS)) + "\n"
    with pytest.raises(IdentityParseError, match="header changed"):
        parse_equity_list(swapped)


def test_a_short_row_is_rejected_not_skipped() -> None:
    text = ",".join(EQUITY_LIST_COLUMNS) + "\nACME,Acme Ltd,EQ\n"
    with pytest.raises(IdentityParseError, match="line 2"):
        parse_equity_list(text)


def test_a_symbol_pasted_into_the_isin_column_is_rejected() -> None:
    text = ",".join(EQUITY_LIST_COLUMNS) + "\nACME,Acme Ltd,EQ,01-JAN-2010,10,1,ACME,10\n"
    with pytest.raises(IdentityParseError, match="not an ISIN"):
        parse_equity_list(text)


def test_dates_are_parsed_without_consulting_the_locale() -> None:
    """`%b` reads month names from the process locale; this parser must not."""
    text = ",".join(EQUITY_LIST_COLUMNS) + "\nACME,Acme Ltd,EQ,06-oct-2008,10,1,INE111A01011,10\n"
    assert parse_equity_list(text)[0].listing_date == date(2008, 10, 6)
    with pytest.raises(IdentityParseError, match="DD-MON-YYYY"):
        parse_equity_list(
            ",".join(EQUITY_LIST_COLUMNS) + "\nACME,Acme Ltd,EQ,2008-10-06,10,1,INE111A01011,10\n"
        )


def test_symbol_changes_parse_headerless_and_headed_files(symbol_change_text: str) -> None:
    changes = parse_symbol_changes(symbol_change_text)
    assert len(changes) == 1054
    assert changes == tuple(sorted(changes)), "oldest first, so a chain walk can take the last"
    zydus = next(c for c in changes if c.new_symbol == "ZYDUSLIFE")
    assert (zydus.old_symbol, zydus.effective_date) == ("CADILAHC", ZYDUS_CHANGE)

    headed = "SM_NAME,SM_KEY_SYMBOL,SM_NEW_SYMBOL,SM_APPLICABLE_FROM\nAcme,OLD,NEW,01-JAN-2020\n"
    assert len(parse_symbol_changes(headed)) == 1


def test_symbol_changes_reject_an_unparseable_body_line() -> None:
    """A skipped rename is a symbol that resolves to the wrong company forever."""
    with pytest.raises(IdentityParseError, match="line 2"):
        parse_symbol_changes("Acme,OLD,NEW,01-JAN-2020\nAcme,OLD,NEW,not-a-date\n")


# ── acceptance 1: resolve across a real symbol change ───────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "on_date", "expected"),
    [
        ("CADILAHC", date(2015, 6, 1), ZYDUS_ISIN),
        ("CADILAHC", date(2022, 3, 6), ZYDUS_ISIN),  # last day of the old symbol
        ("ZYDUSLIFE", ZYDUS_CHANGE, ZYDUS_ISIN),  # first day of the new one
        ("ZYDUSLIFE", date(2026, 8, 7), ZYDUS_ISIN),
        ("LTI", date(2022, 12, 2), LTIM_ISIN),
        ("LTIM", LTIM_CHANGE, LTIM_ISIN),
    ],
)
def test_resolve_returns_the_right_isin_across_a_real_rename(
    master: IdentityMaster, symbol: str, on_date: date, expected: str
) -> None:
    """Acceptance 1. Both spellings of one company resolve to the one ISIN, as of the date."""
    assert master.resolve(symbol, on_date) == expected


def test_the_rename_boundary_is_exact_in_both_directions(master: IdentityMaster) -> None:
    """Fails if the window comparison is inverted or off by a day, which is the whole risk.

    The day before the change belongs to the old symbol and the change date itself to the new
    one; each is unknown on the other's side of the line.
    """
    day_before = date(2022, 3, 6)
    assert master.resolve("CADILAHC", day_before) == ZYDUS_ISIN
    assert master.try_resolve("ZYDUSLIFE", day_before) is None
    assert master.resolve("ZYDUSLIFE", ZYDUS_CHANGE) == ZYDUS_ISIN
    assert master.try_resolve("CADILAHC", ZYDUS_CHANGE) is None


def test_symbol_as_of_never_falls_back_to_the_current_name(master: IdentityMaster) -> None:
    """A 2015 report must print CADILAHC; printing ZYDUSLIFE misstates what was bought."""
    assert master.symbol_as_of(ZYDUS_ISIN, date(2015, 6, 1)) == "CADILAHC"
    assert master.symbol_as_of(ZYDUS_ISIN, date(2026, 8, 7)) == "ZYDUSLIFE"
    assert master.try_symbol_as_of(ZYDUS_ISIN, date(1999, 1, 1)) is None


def test_resolve_normalizes_the_raw_symbol_off_an_exchange_file(master: IdentityMaster) -> None:
    """NSE's own files disagree about leading spaces; a miss here looks like an unknown security."""
    assert master.resolve("  zyduslife ", date(2026, 8, 7)) == ZYDUS_ISIN


def test_unknown_identities_are_a_named_failure_not_a_wrong_answer(master: IdentityMaster) -> None:
    assert master.try_resolve("NOSUCHSYMBOL", date(2026, 8, 7)) is None
    with pytest.raises(UnknownSymbolError, match="NOSUCHSYMBOL"):
        master.resolve("NOSUCHSYMBOL", date(2026, 8, 7))
    with pytest.raises(UnknownIsinError):
        master.symbol_as_of("INE000000000", date(2026, 8, 7))
    with pytest.raises(UnknownIsinError):
        master.security("INE000000000")


def test_the_whole_snapshot_derives_without_a_single_ambiguity(
    equity_list_text: str, symbol_change_text: str
) -> None:
    """The real files are clean; ambiguity below is synthetic, not a fixture that drifted."""
    derived = derive_master(
        parse_equity_list(equity_list_text),
        parse_symbol_changes(symbol_change_text),
        snapshot_date=date(2026, 8, 8),
    )
    assert derived.securities and derived.windows
    assert len(derived.windows) > len(derived.securities), "renames must produce extra windows"
    assert detect_conflicts(derived.windows, source="nse_equity_list") == ()
    assert all(isinstance(entry, ClampedWindow) for entry in derived.clamped)
    assert derived.securities[0].status is ListingStatus.ACTIVE


def test_derivation_dates_only_from_the_source_files() -> None:
    """A clamped window stays clamped: coverage is never invented back to a listing date.

    NSE's `DATE OF LISTING` is the *current* entity's, so for a security renamed after a scheme
    it can post-date the rename. Widening the window to the listing date would make a resolve for
    2011 return an ISIN nothing in the files supports.
    """
    header = ",".join(EQUITY_LIST_COLUMNS)
    rows = parse_equity_list(f"{header}\nNEWCO,New Co,EQ,01-JAN-2020,10,1,INE111A01011,10\n")
    changes = parse_symbol_changes("New Co,OLDCO,NEWCO,01-JAN-2015\n")
    derived = derive_master(rows, changes, snapshot_date=date(2026, 8, 8))

    old = next(w for w in derived.windows if w.symbol == "OLDCO")
    assert old.valid_to == date(2014, 12, 31)
    assert old.valid_from == date(2014, 12, 31), "clamped to its end, not back-dated"
    assert derived.clamped == (
        ClampedWindow(
            isin="INE111A01011",
            symbol="OLDCO",
            listing_date=date(2020, 1, 1),
            clamped_to=date(2014, 12, 31),
        ),
    )
    master = IdentityMaster(derived.windows)
    assert master.try_resolve("OLDCO", date(2011, 1, 1)) is None


def test_a_rename_cycle_fails_loudly_instead_of_looping() -> None:
    rows = parse_equity_list(
        ",".join(EQUITY_LIST_COLUMNS) + "\nAAA,A Ltd,EQ,01-JAN-2000,10,1,INE111A01011,10\n"
    )
    changes = parse_symbol_changes("A Ltd,BBB,AAA,01-JAN-2020\nA Ltd,AAA,BBB,01-JAN-2019\n")
    derived = derive_master(rows, changes, snapshot_date=date(2026, 8, 8))
    # The `seen` set breaks the two-step cycle rather than the hop limit; either way it ends.
    assert [w.symbol for w in derived.windows] == ["AAA", "BBB", "AAA"]
    assert all(w.valid_from <= (w.valid_to or date.max) for w in derived.windows)


# ── acceptance 3: ambiguity raises and is queued ────────────────────────────────────────────


def _reused_symbol_windows() -> tuple[SymbolWindow, ...]:
    """Two ISINs claiming ACME over overlapping dates — a recycled symbol, badly dated."""
    return (
        SymbolWindow(
            exchange=Exchange.NSE,
            symbol="ACME",
            valid_from=date(2005, 1, 1),
            valid_to=date(2019, 12, 31),
            isin="INE222B01012",
            source="nse_symbol_change",
        ),
        SymbolWindow(
            exchange=Exchange.NSE,
            symbol="ACME",
            valid_from=date(2010, 1, 1),
            valid_to=None,
            isin="INE111A01011",
            source="nse_equity_list",
        ),
    )


def test_an_ambiguous_symbol_raises_and_lands_in_the_queue() -> None:
    """Acceptance 3, pure half: the resolve path never picks."""
    queue = InMemoryReconciliationQueue()
    master = IdentityMaster(_reused_symbol_windows(), queue=queue)

    with pytest.raises(AmbiguousSymbolError) as raised:
        master.resolve("ACME", date(2015, 6, 1))

    assert len(queue) == 1
    conflict = queue.items[0]
    assert conflict is raised.value.conflict
    assert conflict.kind is ConflictKind.SYMBOL_TO_ISIN
    assert conflict.detected_by is DetectedBy.RESOLVE
    assert conflict.on_date == date(2015, 6, 1)
    assert conflict.symbols == ("ACME",)
    assert conflict.isins == ("INE111A01011", "INE222B01012"), "sorted, so the row dedupes"


def test_try_resolve_is_lenient_about_unknown_and_strict_about_ambiguous() -> None:
    """The distinction M1.8 depends on: quarantine what we do not know, never guess."""
    master = IdentityMaster(_reused_symbol_windows())
    assert master.try_resolve("ACME", date(2000, 1, 1)) is None
    with pytest.raises(AmbiguousSymbolError):
        master.try_resolve("ACME", date(2015, 6, 1))


def test_ambiguity_is_scoped_to_the_date_not_the_symbol() -> None:
    """Outside the overlap the answer is unambiguous, and the master must still give it."""
    master = IdentityMaster(_reused_symbol_windows())
    assert master.resolve("ACME", date(2025, 1, 1)) == "INE111A01011"
    assert master.resolve("ACME", date(2007, 1, 1)) == "INE222B01012"


def test_the_queue_deduplicates_one_defect_across_many_dates() -> None:
    """A backfill meets a bad symbol on hundreds of dates; that is one thing to fix, not many."""
    queue = InMemoryReconciliationQueue()
    master = IdentityMaster(_reused_symbol_windows(), queue=queue)
    for _ in range(3):
        with pytest.raises(AmbiguousSymbolError):
            master.resolve("ACME", date(2015, 6, 1))
    assert len(queue) == 1


def test_detect_conflicts_finds_the_overlap_before_anything_asks() -> None:
    conflicts = detect_conflicts(_reused_symbol_windows(), source="nse_equity_list")
    assert len(conflicts) == 1
    assert conflicts[0].kind is ConflictKind.SYMBOL_TO_ISIN
    assert conflicts[0].detected_by is DetectedBy.INGEST
    assert conflicts[0].on_date == date(2010, 1, 1), "the first date both claims are valid"


def test_detect_conflicts_finds_the_reverse_direction_too() -> None:
    """One ISIN carrying two symbols on a date makes the as-of lookup a coin toss."""
    windows = (
        SymbolWindow(Exchange.NSE, "ONE", date(2020, 1, 1), None, "INE111A01011"),
        SymbolWindow(Exchange.NSE, "TWO", date(2021, 1, 1), None, "INE111A01011"),
    )
    conflicts = detect_conflicts(windows, source="test")
    assert [c.kind for c in conflicts] == [ConflictKind.ISIN_TO_SYMBOL]
    assert conflicts[0].symbols == ("ONE", "TWO")
    with pytest.raises(AmbiguousSymbolError):
        IdentityMaster(windows).symbol_as_of("INE111A01011", date(2022, 1, 1))


def test_adjacent_windows_do_not_overlap() -> None:
    """Fails if the overlap test uses `<=` where it needs `<`: a rename is not a conflict."""
    windows = (
        SymbolWindow(Exchange.NSE, "OLD", date(2010, 1, 1), date(2019, 12, 31), "INE111A01011"),
        SymbolWindow(Exchange.NSE, "NEW", date(2020, 1, 1), None, "INE111A01011"),
    )
    assert detect_conflicts(windows, source="test") == ()


def test_a_recycled_symbol_with_disjoint_windows_is_not_a_conflict() -> None:
    """Symbol reuse is legal and common; only overlapping claims are ambiguous."""
    windows = (
        SymbolWindow(Exchange.NSE, "ACME", date(2000, 1, 1), date(2009, 12, 31), "INE222B01012"),
        SymbolWindow(Exchange.NSE, "ACME", date(2010, 1, 1), None, "INE111A01011"),
    )
    assert detect_conflicts(windows, source="test") == ()
    master = IdentityMaster(windows)
    assert master.resolve("ACME", date(2005, 1, 1)) == "INE222B01012"
    assert master.resolve("ACME", date(2015, 1, 1)) == "INE111A01011"


def test_conflicts_on_different_exchanges_do_not_collide() -> None:
    windows = (
        SymbolWindow(Exchange.NSE, "ACME", date(2010, 1, 1), None, "INE111A01011"),
        SymbolWindow(Exchange.BSE, "ACME", date(2010, 1, 1), None, "INE222B01012"),
    )
    assert detect_conflicts(windows, source="test") == ()
    master = IdentityMaster(windows)
    assert master.resolve("ACME", date(2015, 1, 1), exchange=Exchange.NSE) == "INE111A01011"
    assert master.resolve("ACME", date(2015, 1, 1), exchange=Exchange.BSE) == "INE222B01012"


# ── acceptance 2: re-ingest appends, never rewrites ─────────────────────────────────────────

_OPEN = SymbolWindow(Exchange.NSE, "OLD", date(2010, 1, 1), None, "INE111A01011")
_CLOSED = SymbolWindow(Exchange.NSE, "OLD", date(2010, 1, 1), date(2019, 12, 31), "INE111A01011")
_NEXT = SymbolWindow(Exchange.NSE, "NEW", date(2020, 1, 1), None, "INE111A01011")


def test_re_deriving_the_same_windows_plans_no_writes() -> None:
    """Acceptance 2, pure half: idempotence is decided before any SQL runs."""
    plan = plan_history([_OPEN, _NEXT], [_OPEN, _NEXT])
    assert plan.is_empty
    assert plan.unchanged == 2
    assert plan.refusals == ()


def test_a_rename_closes_the_old_window_and_appends_the_new_one() -> None:
    plan = plan_history([_OPEN], [_CLOSED, _NEXT])
    assert plan.closes == (_CLOSED,)
    assert plan.inserts == (_NEXT,)
    assert plan.refusals == ()


def test_a_closed_window_is_never_moved_or_reopened() -> None:
    """Never overwrite history (§4.1) — yesterday's bhavcopy still says the old name."""
    moved = SymbolWindow(Exchange.NSE, "OLD", date(2010, 1, 1), date(2021, 6, 30), "INE111A01011")
    plan = plan_history([_CLOSED], [moved])
    assert plan.is_empty
    assert len(plan.refusals) == 1
    assert plan.refusals[0].stored == _CLOSED
    assert "never moved" in plan.refusals[0].reason

    reopened = plan_history([_CLOSED], [_OPEN])
    assert reopened.is_empty
    assert "never reopened" in reopened.refusals[0].reason


def test_a_window_the_run_did_not_look_at_is_left_alone() -> None:
    """A BSE ingest must not close NSE windows just by not mentioning them."""
    bse = SymbolWindow(Exchange.BSE, "OLD", date(2010, 1, 1), None, "INE111A01011")
    plan = plan_history([_OPEN, bse], [_OPEN])
    assert plan.is_empty and plan.refusals == ()


def test_a_rename_in_flight_is_not_mistaken_for_an_ambiguity() -> None:
    """The union of before and after holds two open windows for one ISIN; the result does not.

    Judging conflicts on the union would queue a reconciliation row for every rename the
    platform ever ingests — the exact failure mode that trains an operator to ignore the queue.
    """
    stored = [_OPEN]
    plan = plan_history(stored, [_CLOSED, _NEXT])
    assert detect_conflicts((*stored, *plan.inserts, *plan.closes), source="test") != ()
    assert detect_conflicts(plan.applied_to(stored), source="test") == ()
    assert plan.applied_to(stored) == (_NEXT, _CLOSED)


def test_applied_to_leaves_windows_the_plan_did_not_mention() -> None:
    bse = SymbolWindow(Exchange.BSE, "OLD", date(2010, 1, 1), None, "INE111A01011")
    plan = plan_history([_OPEN, bse], [_CLOSED, _NEXT])
    assert set(plan.applied_to([_OPEN, bse])) == {bse, _CLOSED, _NEXT}


# ── M18.1: an ISIN reissued under an unchanged symbol ─────────────────────────────────────────
#
# TCC's face-value split, 2026-09-04: the depository retired INE887D01016 and issued INE887D01024,
# NSE kept the symbol, and `EQUITY_L.csv` lists the new ISIN with the *original* listing date. The
# 2026-10-10 ingest stored both windows open from 2026-02-25 and every resolve of TCC raised.

TCC_OLD = "INE887D01016"
TCC_NEW = "INE887D01024"
TCC_LISTED = date(2026, 2, 25)
TCC_SWITCH = date(2026, 9, 4)
REISSUE_SNAPSHOT = date(2026, 10, 10)

#: What the store held before 2026-10-10: the old ISIN, open, from the listing date.
_TCC_STORED = (
    SymbolWindow(Exchange.NSE, "TCC", TCC_LISTED, None, TCC_OLD, "EQ", "nse_equity_list"),
)


def _tcc_derived() -> DerivedMaster:
    """The 2026-10-10 equity list as far as TCC goes: the new ISIN, the original listing date."""
    return derive_master(
        parse_equity_list(
            _equity_list_text(f"TCC,TCC Concept Limited,EQ,25-FEB-2026,1,1,{TCC_NEW},1")
        ),
        snapshot_date=REISSUE_SNAPSHOT,
    )


def _equity_list_text(*rows: str) -> str:
    return ",".join(EQUITY_LIST_COLUMNS) + "\n" + "".join(f"{row}\n" for row in rows)


class _FixedEvidence:
    """`ReissueEvidence` that answers one date, and records what it was asked."""

    def __init__(self, answer: date | None) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str, str, date]] = []

    def first_listed(
        self, symbol: str, old_isin: str, new_isin: str, *, through: date
    ) -> date | None:
        self.asked.append((symbol, old_isin, new_isin, through))
        return self.answer


def _resolve_tcc(
    *,
    lineage: dict[tuple[str, str], date] | None = None,
    evidence: _FixedEvidence | None = None,
    stored: tuple[SymbolWindow, ...] = _TCC_STORED,
) -> tuple[ReissueResolution, tuple[SymbolWindow, ...], HistoryPlan]:
    derived = _tcc_derived()
    resolution = resolve_reissues(
        derived.windows,
        stored,
        listed_isins=frozenset(s.isin for s in derived.securities),
        lineage={} if lineage is None else lineage,
        snapshot_date=REISSUE_SNAPSHOT,
        evidence=evidence,
    )
    plan = plan_history(stored, resolution.windows)
    return resolution, plan.applied_to(stored), plan


def test_current_derivation_alone_makes_a_reissue_ambiguous() -> None:
    """The defect, pinned: without the reissue step both windows open from the listing date."""
    plan = plan_history(_TCC_STORED, _tcc_derived().windows)
    conflicts = detect_conflicts(plan.applied_to(_TCC_STORED), source="test")
    assert [c.isins for c in conflicts] == [(TCC_OLD, TCC_NEW)]


def test_a_reissue_with_a_lineage_edge_splits_at_the_switch() -> None:
    resolution, applied, plan = _resolve_tcc(lineage={(TCC_OLD, TCC_NEW): TCC_SWITCH})

    assert plan.closes == (
        SymbolWindow(
            Exchange.NSE, "TCC", TCC_LISTED, date(2026, 9, 3), TCC_OLD, "EQ", "nse_equity_list"
        ),
    )
    assert [(w.isin, w.valid_from, w.valid_to) for w in plan.inserts] == [
        (TCC_NEW, TCC_SWITCH, None)
    ]
    assert plan.refusals == ()
    assert detect_conflicts(applied, source="test") == ()
    assert resolution.boundaries == (
        ReissueBoundary(
            Exchange.NSE, "TCC", TCC_OLD, TCC_NEW, TCC_SWITCH, REISSUE_EVIDENCE_LINEAGE
        ),
    )

    master = IdentityMaster(applied)
    assert master.try_resolve("TCC", date(2026, 8, 1)) == TCC_OLD
    assert master.try_resolve("TCC", date(2026, 9, 3)) == TCC_OLD
    assert master.try_resolve("TCC", TCC_SWITCH) == TCC_NEW
    assert master.try_resolve("TCC", date(2026, 10, 1)) == TCC_NEW


def test_lineage_wins_over_the_equity_list_series() -> None:
    evidence = _FixedEvidence(date(2026, 9, 8))
    resolution, _, _ = _resolve_tcc(lineage={(TCC_OLD, TCC_NEW): TCC_SWITCH}, evidence=evidence)
    assert resolution.boundaries[0].effective == TCC_SWITCH
    assert evidence.asked == [], "the series is only read when lineage has no edge"


def test_a_reissue_without_lineage_splits_at_the_first_list_showing_the_new_isin() -> None:
    evidence = _FixedEvidence(TCC_SWITCH)
    resolution, applied, plan = _resolve_tcc(evidence=evidence)

    assert evidence.asked == [("TCC", TCC_OLD, TCC_NEW, REISSUE_SNAPSHOT)]
    assert resolution.boundaries[0].evidence == REISSUE_EVIDENCE_EQUITY_LIST
    assert [w.valid_to for w in plan.closes] == [date(2026, 9, 3)]
    assert detect_conflicts(applied, source="test") == ()
    master = IdentityMaster(applied)
    assert master.try_resolve("TCC", date(2026, 8, 1)) == TCC_OLD
    assert master.try_resolve("TCC", date(2026, 10, 1)) == TCC_NEW


@pytest.mark.parametrize(
    "evidence", [None, _FixedEvidence(None)], ids=["no-series", "series-silent"]
)
def test_a_reissue_with_no_boundary_evidence_is_still_a_conflict(
    evidence: _FixedEvidence | None,
) -> None:
    """Never guess: no evidence leaves the windows as derived, and the overlap is queued."""
    resolution, applied, plan = _resolve_tcc(evidence=evidence)

    assert resolution.windows == _tcc_derived().windows
    assert resolution.boundaries == ()
    assert resolution.unresolved == ((Exchange.NSE, "TCC", TCC_OLD, TCC_NEW),)
    assert plan.closes == ()
    conflicts = detect_conflicts(applied, source="test")
    assert [c.isins for c in conflicts] == [(TCC_OLD, TCC_NEW)]
    queue = InMemoryReconciliationQueue()
    with pytest.raises(AmbiguousSymbolError):
        IdentityMaster(applied, queue=queue).try_resolve("TCC", date(2026, 10, 1))
    assert len(queue) == 1


@pytest.mark.parametrize(
    "bad",
    [TCC_LISTED, date(2026, 1, 1), date(2026, 10, 11)],
    ids=["at-old-start", "before-old-start", "after-snapshot"],
)
def test_a_boundary_outside_the_old_window_is_not_trusted(bad: date) -> None:
    resolution, applied, _ = _resolve_tcc(lineage={(TCC_OLD, TCC_NEW): bad})
    assert resolution.boundaries == ()
    assert detect_conflicts(applied, source="test") != ()


def test_the_boundary_is_never_inverted() -> None:
    """Fails if the switch is applied backwards — the new ISIN covering any pre-switch session,
    or the old one any session from the switch on."""
    _, applied, _ = _resolve_tcc(lineage={(TCC_OLD, TCC_NEW): TCC_SWITCH})
    new = [w for w in applied if w.isin == TCC_NEW]
    old = [w for w in applied if w.isin == TCC_OLD]
    assert len(new) == 1 and len(old) == 1
    day = TCC_LISTED
    while day <= REISSUE_SNAPSHOT:
        assert new[0].covers(day) == (day >= TCC_SWITCH), day
        assert old[0].covers(day) == (day < TCC_SWITCH), day
        day += timedelta(days=1)


def test_a_reconciled_reissue_re_ingests_as_a_no_op() -> None:
    """The next run derives the listing-date window again; it must align, not insert a second."""
    _, applied, _ = _resolve_tcc(lineage={(TCC_OLD, TCC_NEW): TCC_SWITCH})
    # A week on, lineage still says it — and with no lineage at all the store alone suffices.
    for lineage in ({(TCC_OLD, TCC_NEW): TCC_SWITCH}, {}):
        resolution, again, plan = _resolve_tcc(lineage=lineage, stored=applied)
        assert plan.is_empty, plan
        assert plan.refusals == ()
        assert resolution.boundaries == ()
        assert again == applied


def test_a_stale_listing_date_window_is_left_for_the_repair() -> None:
    """The 2026-10-10 state — B already stored from the listing date — is not patched around."""
    stale = SymbolWindow(Exchange.NSE, "TCC", TCC_LISTED, None, TCC_NEW, "EQ", "nse_equity_list")
    resolution, _, plan = _resolve_tcc(
        lineage={(TCC_OLD, TCC_NEW): TCC_SWITCH}, stored=(*_TCC_STORED, stale)
    )
    assert resolution.boundaries == ()
    assert plan.is_empty


def test_an_old_isin_still_listed_is_not_a_reissue() -> None:
    """If the old ISIN is in today's file it did not retire — that is a real conflict."""
    derived = derive_master(
        parse_equity_list(
            _equity_list_text(
                f"TCC,TCC Concept Limited,EQ,25-FEB-2026,1,1,{TCC_NEW},1",
                f"TCCOLD,TCC Concept Old,EQ,25-FEB-2026,1,1,{TCC_OLD},1",
            )
        ),
        snapshot_date=REISSUE_SNAPSHOT,
    )
    resolution = resolve_reissues(
        derived.windows,
        _TCC_STORED,
        listed_isins=frozenset(s.isin for s in derived.securities),
        lineage={(TCC_OLD, TCC_NEW): TCC_SWITCH},
        snapshot_date=REISSUE_SNAPSHOT,
    )
    assert resolution.boundaries == ()
    assert resolution.windows == derived.windows


def test_pre_switch_rename_history_stays_with_the_old_isin() -> None:
    """A rename chain walked from the new ISIN describes the old one's past; it is dropped."""
    derived = derive_master(
        parse_equity_list(
            _equity_list_text(f"TCC,TCC Concept Limited,EQ,01-JAN-2010,1,1,{TCC_NEW},1")
        ),
        parse_symbol_changes("TCC Concept Limited,OLDTCC,TCC,25-FEB-2026\n"),
        snapshot_date=REISSUE_SNAPSHOT,
    )
    stored = (
        *_TCC_STORED,
        SymbolWindow(Exchange.NSE, "OLDTCC", date(2010, 1, 1), date(2026, 2, 24), TCC_OLD),
    )
    resolution = resolve_reissues(
        derived.windows,
        stored,
        listed_isins=frozenset(s.isin for s in derived.securities),
        lineage={(TCC_OLD, TCC_NEW): TCC_SWITCH},
        snapshot_date=REISSUE_SNAPSHOT,
    )
    assert all(w.symbol != "OLDTCC" for w in resolution.windows if w.isin == TCC_NEW)
    plan = plan_history(stored, resolution.windows)
    assert detect_conflicts(plan.applied_to(stored), source="test") == ()


# ── the L0 equity-list series as boundary evidence ──────────────────────────────────────────

_UDIFF_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)


def _udiff_zip(on_date: date, isins: dict[str, str]) -> tuple[str, bytes]:
    """A minimal UDiFF cash bhavcopy for one session: `symbol -> ISIN`, one EQ row each."""
    day = on_date.isoformat()
    body = "".join(
        f"{day},{day},CM,NSE,STK,{n},{isin},{symbol},EQ,,,,,{symbol} LTD,10.00,11.00,9.00,"
        "10.50,10.50,10.00,,10.50,,,100,1050.00,10,F1,1,,,,,\n"
        for n, (symbol, isin) in enumerate(sorted(isins.items()), start=1)
    )
    name = f"BhavCopy_NSE_CM_0_0_0_{on_date.strftime('%Y%m%d')}_F_0000.csv"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, _UDIFF_HEADER + "\n" + body)
    return name + ".zip", buffer.getvalue()


def _series_store(
    tmp_path: Path,
    captures: dict[date, str | None],
    sessions: dict[date, str] | None = None,
    *,
    symbol: str = "TCC",
) -> L0Store:
    """Equity-list captures (`None`: the symbol is absent that day) and bhavcopy sessions."""
    store = L0Store(clock=FrozenClock(datetime(2026, 10, 10, 7, 0, tzinfo=IST)), data_root=tmp_path)
    for on_date, isin in captures.items():
        rows = ["ACME,Acme Limited,EQ,01-JAN-2010,10,1,INE111A01017,10"]
        if isin is not None:
            rows.append(f"{symbol},{symbol} Limited,EQ,25-FEB-2026,1,1,{isin},1")
        text = _equity_list_text(*rows)
        store.put(NSE_EQUITY_LIST_SOURCE, on_date, equity_list_filename(on_date), text.encode())
    for on_date, isin in (sessions or {}).items():
        name, payload = _udiff_zip(on_date, {symbol: isin, "ACME": "INE111A01017"})
        store.put("nse_bhavcopy_udiff", on_date, name, payload)
    return store


#: TCC's real shape: captures and sessions agree that 2026-09-04 is the first new-ISIN session.
_TCC_CAPTURES: dict[date, str | None] = {
    date(2026, 9, 2): TCC_OLD,
    date(2026, 9, 3): TCC_OLD,
    TCC_SWITCH: TCC_NEW,
    date(2026, 9, 7): TCC_NEW,
}
_TCC_SESSIONS = {
    date(2026, 9, 2): TCC_OLD,
    date(2026, 9, 3): TCC_OLD,
    TCC_SWITCH: TCC_NEW,
    date(2026, 9, 7): TCC_NEW,
}


def test_the_series_names_the_first_list_after_the_old_isin(tmp_path: Path) -> None:
    series = L0EquityListSeries(_series_store(tmp_path, _TCC_CAPTURES, _TCC_SESSIONS))
    assert series.first_listed("TCC", TCC_OLD, TCC_NEW, through=REISSUE_SNAPSHOT) == TCC_SWITCH
    # Not yet visible on or before the switch's eve.
    assert series.first_listed("TCC", TCC_OLD, TCC_NEW, through=date(2026, 9, 3)) is None


def test_the_series_does_not_date_a_switch_older_than_itself(tmp_path: Path) -> None:
    """The first capture already shows the new ISIN: the switch is before the series. Unknown."""
    store = _series_store(tmp_path, {date(2026, 9, 8): TCC_NEW, date(2026, 9, 9): TCC_NEW})
    assert (
        L0EquityListSeries(store).first_listed("TCC", TCC_OLD, TCC_NEW, through=REISSUE_SNAPSHOT)
        is None
    )


def test_a_flapping_series_is_not_evidence(tmp_path: Path) -> None:
    store = _series_store(
        tmp_path,
        {
            date(2026, 9, 2): TCC_OLD,
            date(2026, 9, 3): TCC_NEW,
            TCC_SWITCH: TCC_OLD,
            date(2026, 9, 7): TCC_NEW,
        },
        _TCC_SESSIONS,
    )
    assert (
        L0EquityListSeries(store).first_listed("TCC", TCC_OLD, TCC_NEW, through=REISSUE_SNAPSHOT)
        is None
    )


def test_a_symbol_absent_between_old_and_new_is_not_a_reissue(tmp_path: Path) -> None:
    """A capture without the symbol breaks the chain: gone and back is not proven to be one
    security, even under the same issuer code and with sessions that would agree."""
    store = _series_store(
        tmp_path,
        {
            date(2026, 9, 8): TCC_OLD,
            date(2026, 9, 15): None,
            date(2026, 9, 22): None,
            date(2026, 10, 1): TCC_NEW,
        },
        {date(2026, 9, 8): TCC_OLD, date(2026, 10, 1): TCC_NEW},
    )
    assert (
        L0EquityListSeries(store).first_listed("TCC", TCC_OLD, TCC_NEW, through=REISSUE_SNAPSHOT)
        is None
    )


def test_another_company_taking_a_vacated_symbol_is_queued_not_split(tmp_path: Path) -> None:
    """The reviewer's case: A open since 2013, absent from two captures, then an unrelated ISIN
    under the same symbol. No boundary, and the overlap reaches the reconciliation queue."""
    old, new = "INE345B01019", "INE9ZZZ01012"
    stored = (SymbolWindow(Exchange.NSE, "ZED", date(2013, 1, 1), None, old, "EQ"),)
    store = _series_store(
        tmp_path,
        {
            date(2026, 9, 8): old,
            date(2026, 9, 15): None,
            date(2026, 9, 22): None,
            date(2026, 10, 1): new,
        },
        {date(2026, 9, 8): old, date(2026, 10, 1): new},
        symbol="ZED",
    )
    derived = derive_master(
        parse_equity_list(_equity_list_text(f"ZED,Zed Limited,EQ,01-OCT-2026,1,1,{new},1")),
        snapshot_date=REISSUE_SNAPSHOT,
    )
    resolution = resolve_reissues(
        derived.windows,
        stored,
        listed_isins=frozenset(s.isin for s in derived.securities),
        lineage={},
        snapshot_date=REISSUE_SNAPSHOT,
        evidence=L0EquityListSeries(store),
    )
    assert resolution.boundaries == ()
    assert resolution.unresolved == ((Exchange.NSE, "ZED", old, new),)
    applied = plan_history(stored, resolution.windows).applied_to(stored)
    assert [c.isins for c in detect_conflicts(applied, source="test")] == [(old, new)]


def test_a_different_issuer_code_is_never_split_on_series_evidence() -> None:
    """Even a series that names a clean switch date cannot move a window across issuer codes."""
    other = "INE9ZZZ01012"
    evidence = _FixedEvidence(TCC_SWITCH)
    derived = derive_master(
        parse_equity_list(
            _equity_list_text(f"TCC,Someone Else Limited,EQ,25-FEB-2026,1,1,{other},1")
        ),
        snapshot_date=REISSUE_SNAPSHOT,
    )
    resolution = resolve_reissues(
        derived.windows,
        _TCC_STORED,
        listed_isins=frozenset(s.isin for s in derived.securities),
        lineage={},
        snapshot_date=REISSUE_SNAPSHOT,
        evidence=evidence,
    )
    assert resolution.boundaries == ()
    assert resolution.unresolved == ((Exchange.NSE, "TCC", TCC_OLD, other),)
    applied = plan_history(_TCC_STORED, resolution.windows).applied_to(_TCC_STORED)
    assert [c.isins for c in detect_conflicts(applied, source="test")] == [(TCC_OLD, other)]


def test_a_capture_a_day_early_is_refused_by_the_session(tmp_path: Path) -> None:
    """The ~19:15 IST capture on 09-03 already lists the new ISIN, but session 09-03 traded the
    old one. The series and the bhavcopy disagree, so there is no answer — never a day early."""
    store = _series_store(
        tmp_path,
        {date(2026, 9, 2): TCC_OLD, date(2026, 9, 3): TCC_NEW, TCC_SWITCH: TCC_NEW},
        {date(2026, 9, 2): TCC_OLD, date(2026, 9, 3): TCC_OLD, TCC_SWITCH: TCC_NEW},
    )
    assert (
        L0EquityListSeries(store).first_listed("TCC", TCC_OLD, TCC_NEW, through=REISSUE_SNAPSHOT)
        is None
    )


def test_a_switch_with_no_session_bhavcopy_is_refused(tmp_path: Path) -> None:
    sessions = {d: i for d, i in _TCC_SESSIONS.items() if d != TCC_SWITCH}
    store = _series_store(tmp_path, _TCC_CAPTURES, sessions)
    assert (
        L0EquityListSeries(store).first_listed("TCC", TCC_OLD, TCC_NEW, through=REISSUE_SNAPSHOT)
        is None
    )
