"""Measuring the acquired PR-bundle corpus, and proving the reconcile can fail (W2 close-out).

Two things here are worth a test rather than a reading. The first is the reconcile: the W1 closer
shipped one whose second direction was structurally incapable of failing, so these tests assert
that each direction *does* fire on an injected date and that the served set is read off L0 rather
than off a plan. The second is every count that goes into the report — a per-year availability
table is exactly the kind of artefact nobody re-derives by hand, so a bundle carrying two members
of one family, or a member present but unreadable, must not quietly become the wrong number.

Offline and deterministic: the four era fixtures in `tests/fixtures/nse_pr_bundle/`, written into
a `tmp_path` lake by the test itself.
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
from dataplatform.ingest.calendar import Holiday, TradingCalendar, trading_calendar
from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID, MemberKind
from dataplatform.ingest.nse.pr_bundle.survey import (
    date_of_archive_name,
    payload_counts,
    prove_reconcile_can_fail,
    purpose_tags,
    render_report,
    sessions_in_l0,
    survey_corpus,
)
from dataplatform.store.l0 import L0Store

IST = ZoneInfo("Asia/Kolkata")
FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle"

#: The four era fixtures, and the session each one publishes.
ERAS: Final[tuple[tuple[str, str, date], ...]] = (
    ("ix_era", "PR040110.zip", date(2010, 1, 4)),
    ("classic", "PR020113.zip", date(2013, 1, 2)),
    ("mcap_upper", "PR010724.zip", date(2024, 7, 1)),
    ("lowercase", "PR040926.zip", date(2026, 9, 4)),
)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A lake at `tmp_path` holding the four era fixtures under their real keys."""
    store = L0Store(clock=FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=IST)), data_root=tmp_path)
    for era, name, day in ERAS:
        store.put(PR_BUNDLE_SOURCE_ID, day, name, (FIXTURES / era / name).read_bytes())
    return tmp_path


def _store(root: Path) -> L0Store:
    return L0Store(clock=FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=IST)), data_root=root)


# ── the served set comes off L0, not off a plan ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("PR040110.zip", date(2010, 1, 4)),
        ("PR040926.zip", date(2026, 9, 4)),
        ("pr040110.zip", date(2010, 1, 4)),
        ("PR300210.zip", None),  # 30 February is not a date
        ("Bc040110.csv", None),
        ("PR040110.zip.meta.json", None),
    ],
)
def test_date_of_archive_name(filename: str, expected: date | None) -> None:
    assert date_of_archive_name(filename) == expected


def test_sessions_in_l0_lists_the_payloads_and_nothing_else(lake: Path) -> None:
    assert sessions_in_l0(lake) == tuple(day for _, _, day in ERAS)


def test_sessions_in_l0_is_empty_for_a_tree_with_no_bundles(tmp_path: Path) -> None:
    # The failure this guards is a worktree-relative lake root resolving to an empty second lake:
    # it must read as zero bundles, never as "the campaign is fine".
    assert sessions_in_l0(tmp_path) == ()


def test_sessions_in_l0_does_not_read_the_sidecars(lake: Path) -> None:
    """The served set must survive losing every sidecar — that is what makes it plan-independent.

    A served set derived from `L0Store.iter_refs` (which reads sidecars, written by the fetch that
    followed the plan) would go empty here. This one does not.
    """
    for sidecar in (lake / "L0" / PR_BUNDLE_SOURCE_ID).glob("*/*/*.meta.json"):
        sidecar.chmod(0o600)
        sidecar.unlink()

    assert sessions_in_l0(lake) == tuple(day for _, _, day in ERAS)


# ── the reconcile, and the proof it can fail ─────────────────────────────────────────────────


def _calendar() -> TradingCalendar:
    return trading_calendar()


def test_both_reconcile_directions_fire_on_an_injected_date() -> None:
    calendar = _calendar()
    start, end = date(2024, 1, 1), date(2024, 3, 31)
    served = calendar.expected_data_dates(start, end)

    proofs = prove_reconcile_can_fail(calendar, served, start, end)

    control = next(proof for proof in proofs if proof.direction.startswith("control"))
    assert "0 missing, 0 unexpected" in control.detail

    injections = [proof for proof in proofs if not proof.direction.startswith("control")]
    assert len(injections) == 3
    assert all(proof.fired for proof in injections), [
        (proof.direction, proof.injected, proof.detail) for proof in injections if not proof.fired
    ]
    # One of each direction, plus the second expected-side injection that the broken W1 shape
    # could not have detected.
    assert sum(1 for proof in injections if proof.direction.startswith("served")) == 1
    assert sum(1 for proof in injections if proof.direction.startswith("expected")) == 2


def test_the_expected_side_injection_names_the_holiday_it_removed() -> None:
    calendar = _calendar()
    start, end = date(2024, 1, 1), date(2024, 3, 31)
    served = calendar.expected_data_dates(start, end)

    proof = next(
        p
        for p in prove_reconcile_can_fail(calendar, served, start, end)
        if "removed the declared holiday" in p.injected
    )

    assert proof.fired
    stamp = proof.injected.split("removed the declared holiday ")[1].split(" ")[0]
    assert date.fromisoformat(stamp) in {h.date for h in calendar.holidays(start, end)}


def test_the_injections_do_not_mutate_the_calendar_or_the_served_set() -> None:
    calendar = _calendar()
    start, end = date(2024, 1, 1), date(2024, 3, 31)
    served = calendar.expected_data_dates(start, end)
    before = list(served)
    holidays_before = {h.date for h in calendar.holidays(start, end)}

    prove_reconcile_can_fail(calendar, served, start, end)

    assert served == before
    assert {h.date for h in calendar.holidays(start, end)} == holidays_before
    assert calendar.reconcile(served, start, end).ok


def test_a_served_set_missing_a_session_is_reported_as_missing(lake: Path) -> None:
    """The direction the W1 closer's reconcile could not detect, end to end off a real lake."""
    calendar = _calendar()
    start, end = date(2010, 1, 4), date(2010, 1, 8)

    survey = survey_corpus(_store(lake), calendar=calendar, start=start, end=end)

    # The fixture lake holds only 2010-01-04 in this window; the other four sessions are real.
    assert survey.bundles == 1
    assert survey.reconciliation.missing == (
        date(2010, 1, 5),
        date(2010, 1, 6),
        date(2010, 1, 7),
        date(2010, 1, 8),
    )
    assert survey.reconciliation.unexpected == ()


def test_a_bundle_on_a_closed_date_is_reported_as_unexpected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sunday = date(2010, 1, 10)
    # Stored under a Sunday's key on purpose: the calendar calls it closed, so direction (b) must
    # name it. Nothing but an L0 listing could produce this date.
    store.put(
        PR_BUNDLE_SOURCE_ID,
        sunday,
        "PR100110.zip",
        (FIXTURES / "ix_era" / "PR040110.zip").read_bytes(),
    )

    survey = survey_corpus(
        store, calendar=_calendar(), start=date(2010, 1, 9), end=date(2010, 1, 10)
    )

    assert survey.reconciliation.unexpected == (sunday,)


def test_a_calendar_with_no_holiday_to_remove_says_so_rather_than_claiming_success() -> None:
    """An injection that cannot be built must not be reported as a check that fired."""
    calendar = TradingCalendar(
        coverage_start=date(2024, 1, 1),
        coverage_end=date(2024, 1, 31),
        provenance=trading_calendar().provenance,
        _holidays={},
        _sources={2024: "test"},
    )
    start, end = date(2024, 1, 1), date(2024, 1, 31)

    proof = next(
        p
        for p in prove_reconcile_can_fail(
            calendar, calendar.expected_data_dates(start, end), start, end
        )
        if p.direction.startswith("expected") and p.injected == "none available"
    )

    assert not proof.fired
    assert "could not be built" in proof.detail


def test_a_weekend_holiday_entry_is_never_chosen_as_the_injection_victim() -> None:
    """Removing a Sunday entry changes nothing, so it must not be offered as a fired injection."""
    calendar = TradingCalendar(
        coverage_start=date(2024, 1, 1),
        coverage_end=date(2024, 1, 31),
        provenance=trading_calendar().provenance,
        _holidays={date(2024, 1, 7): Holiday(date=date(2024, 1, 7), name="a sunday entry")},
        _sources={2024: "test"},
    )
    start, end = date(2024, 1, 1), date(2024, 1, 31)

    proof = next(
        p
        for p in prove_reconcile_can_fail(
            calendar, calendar.expected_data_dates(start, end), start, end
        )
        if p.direction.startswith("expected") and "removed the declared holiday" not in p.injected
    )

    assert proof.injected == "none available"
    assert "could not be built" in proof.detail


# ── availability counting ────────────────────────────────────────────────────────────────────


#: A member list the trimmed fixtures do not have. `tests/fixtures/nse_pr_bundle/PROVENANCE.md`
#: keeps only the three parsed members plus the readme, so the two counting behaviours that need
#: the *other* members — a family shipping two files in one bundle, and the `ffix` census — are
#: exercised against a bundle this test builds. The `ffix` header is the real one, measured off
#: `ffix040110.csv` in the lake; its three rows are synthetic and labelled as such.
_FFIX_HEADER: Final = (
    "INDEX_FLG,SYMBOL,SERIES,SECURITY,ISSUE_CAP,INVESTIBLE_FACTOR,CLOSE_PRIC,FF_MKT_CAP,WEIGHTAGE"
)
_SYNTHETIC_FFIX: Final = "\n".join(
    [
        _FFIX_HEADER,
        " , , ,S&P CNX Nifty Sec., , , , ",
        "NIFTY,SYNTHA,EQ,SYNTHETIC A,100,0.5,10.00,500.00,0.50",
        "NIFTY,SYNTHB,EQ,SYNTHETIC B,200,0.5,10.00,1000.00,1.00",
        "CNX 100,SYNTHA,EQ,SYNTHETIC A,100,0.5,10.00,500.00,0.25",
    ]
)


def _bundle_with_extra_members(day: date) -> bytes:
    """The `ix_era` fixture's real members, plus an `ffix` and a doubled `fo`."""
    source = FIXTURES / "ix_era" / "PR040110.zip"
    buffer = BytesIO()
    with (
        zipfile.ZipFile(source) as original,
        zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as built,
    ):
        for info in original.infolist():
            built.writestr(info.filename, original.read(info.filename))
        built.writestr("ffix040110.csv", _SYNTHETIC_FFIX)
        # One family, two files, one bundle — the shape that made 2010 report 502 `fo` days.
        built.writestr("fo04012010.csv", "synthetic\n")
        built.writestr("fo04012010.doc", "synthetic\n")
    del day
    return buffer.getvalue()


@pytest.fixture
def rich_lake(tmp_path: Path) -> Path:
    """A one-bundle lake whose bundle carries `ffix` and two `fo` members."""
    _store(tmp_path).put(
        PR_BUNDLE_SOURCE_ID,
        date(2010, 1, 4),
        "PR040110.zip",
        _bundle_with_extra_members(date(2010, 1, 4)),
    )
    return tmp_path


def test_the_availability_table_counts_bundles_not_member_files(rich_lake: Path) -> None:
    """`fo04012010.csv` sits beside `fo04012010.doc`: one day of availability, not two."""
    survey = survey_corpus(
        _store(rich_lake), calendar=_calendar(), start=date(2010, 1, 4), end=date(2010, 1, 4)
    )

    by_name = {span.name: span for span in survey.families}
    fo = by_name[MemberKind.FO.value]
    assert fo.instances == 2
    assert fo.bundles == 1
    assert fo.doubled == 1
    assert survey.years[0].members[MemberKind.FO.value] == 1
    for span in survey.families:
        assert span.bundles <= survey.bundles, span
        assert sum(span.per_year.values()) == span.bundles, span


def test_the_ffix_census_finds_the_nifty_50_the_ix_member_never_carries(rich_lake: Path) -> None:
    """The report's largest finding, pinned: `ffix` is the free-float *index* file, not debt."""
    survey = survey_corpus(
        _store(rich_lake), calendar=_calendar(), start=date(2010, 1, 4), end=date(2010, 1, 4)
    )

    names = {span.index_name for span in survey.ffix.indices}
    assert names == {"NIFTY", "CNX 100"}
    assert survey.ffix.rows == 3
    assert survey.ffix.bundles == 1
    assert list(survey.ffix.headers) == [_FFIX_HEADER]
    # The point of the finding: the same bundle's `Ix` member carries no NIFTY 50 at all.
    assert not any("NIFTY 50" in span.index_name.upper() for span in survey.ix.indices)


def test_the_ffix_census_reads_only_the_index_column(rich_lake: Path) -> None:
    """It is a census, not a parse: no typed row, no Decimal, no weightage arithmetic."""
    survey = survey_corpus(
        _store(rich_lake), calendar=_calendar(), start=date(2010, 1, 4), end=date(2010, 1, 4)
    )

    nifty = next(span for span in survey.ffix.indices if span.index_name == "NIFTY")
    assert nifty.rows == 2
    assert nifty.dates == 1
    assert nifty.first_seen == nifty.last_seen == date(2010, 1, 4)


def test_first_and_last_seen_bound_every_family(lake: Path) -> None:
    survey = survey_corpus(
        _store(lake), calendar=_calendar(), start=date(2010, 1, 4), end=date(2026, 9, 4)
    )

    for span in survey.families:
        assert span.first_seen <= span.last_seen
        assert span.first_seen in {day for _, _, day in ERAS}


def test_mcap_is_absent_before_its_era_and_present_after(lake: Path) -> None:
    survey = survey_corpus(
        _store(lake), calendar=_calendar(), start=date(2010, 1, 4), end=date(2026, 9, 4)
    )

    assert survey.mcap.first_seen == date(2024, 7, 1)
    assert survey.mcap.categories
    assert set(survey.mcap.categories) <= {"Listed", "Permitted"}


def test_ix_presence_and_parse_are_counted_separately(lake: Path) -> None:
    survey = survey_corpus(
        _store(lake), calendar=_calendar(), start=date(2010, 1, 4), end=date(2026, 9, 4)
    )

    assert survey.ix.present_bundles == 1
    assert survey.ix.parsed_bundles == 1
    assert survey.ix.first_present == date(2010, 1, 4)
    assert any("CNX 500" in span.index_name for span in survey.ix.indices)


def test_a_bundle_that_cannot_be_opened_is_collected_not_raised(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put(PR_BUNDLE_SOURCE_ID, date(2013, 1, 2), "PR020113.zip", b"not a zip at all")

    survey = survey_corpus(
        store, calendar=_calendar(), start=date(2013, 1, 2), end=date(2013, 1, 2)
    )

    assert survey.bc is None
    assert [failure.member for failure in survey.failures] == ["(zip)"]
    assert "not a zip archive" in survey.failures[0].message


# ── purpose tagging ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        ("ANNUAL GENERAL MEETING", ("AGM",)),
        ("AGM/DIV-RS 2 PER SHARE", ("DIVIDEND", "AGM")),
        ("DIV/REDEMPTION", ("DIVIDEND", "REDEMPTION")),
        # `SUB-DIVISION` contains the letters of `DIV` and is a split, never a dividend.
        ("SUB-DIVISION OF SHARES", ("SPLIT",)),
        ("BONUS 1:1", ("BONUS",)),
        ("FACE VALUE SPLIT FROM RS 10 TO RS 1", ("SPLIT",)),
        ("INTEREST PAYMENT", ("INTEREST",)),
        ("SCHEME OF AMALGAMATION", ("SCHEME",)),
        ("SOMETHING NSE HAS NOT PUBLISHED", ()),
    ],
)
def test_purpose_tags_may_overlap_and_may_be_empty(purpose: str, expected: tuple[str, ...]) -> None:
    assert set(purpose_tags(purpose)) == set(expected)


def test_purpose_tagging_is_case_insensitive() -> None:
    assert purpose_tags("Annual General Meeting") == purpose_tags("ANNUAL GENERAL MEETING")


# ── rendering, and the payload census ────────────────────────────────────────────────────────


def test_the_report_renders_every_section(lake: Path) -> None:
    survey = survey_corpus(
        _store(lake), calendar=_calendar(), start=date(2010, 1, 4), end=date(2026, 9, 4)
    )

    body = render_report(survey, corp_actions_baseline=47887)

    for heading in (
        "## 0.",
        "## 1.",
        "## 2.",
        "## 3.",
        "## 4.",
        "## 5.",
        "## 6.",
        "## 7.",
    ):
        assert heading in body
    assert str(lake / "L0") in body
    assert "47,887" in body
    # Sections with nothing to report must say so rather than render an empty table.
    assert "_Not run in this pass._" in body
    assert "_No worktree lake compared in this pass._" in body


def test_payload_counts_are_per_source_and_exclude_sidecars(lake: Path) -> None:
    assert payload_counts(lake / "L0") == {PR_BUNDLE_SOURCE_ID: len(ERAS)}
    assert payload_counts(lake / "nowhere") == {}
