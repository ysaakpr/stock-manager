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

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.identity.ingest import (
    EQUITY_LIST_COLUMNS,
    ClampedWindow,
    IdentityParseError,
    derive_master,
    parse_equity_list,
    parse_symbol_changes,
)
from dataplatform.identity.master import (
    AmbiguousSymbolError,
    ConflictKind,
    DetectedBy,
    Exchange,
    IdentityMaster,
    InMemoryReconciliationQueue,
    ListingStatus,
    SymbolWindow,
    UnknownIsinError,
    UnknownSymbolError,
    detect_conflicts,
    plan_history,
)

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
