"""DQ-5 — the index-change announcements parse into dated, tracked-index events.

One frozen release per layout era (`tests/fixtures/nifty_index_press_releases/<era>/`), each the
exchange's own bytes. The assertions are the facts the release states — which company, which index,
which way, effective when — so a parser that misreads the layout fails on a named fact, and every
layout it cannot read must say so (`unparsed` or `ParseError`) rather than yield nothing quietly.
Offline: a socket is a test bug.
"""

from __future__ import annotations

import socket
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.ingest.index_changes import (
    ChangeAction,
    IndexChangeEvent,
    canonical_index_slug,
    is_membership_candidate,
    parse_date_phrase,
    parse_press_release_listing,
    parse_press_release_pdf,
    press_release_url,
)
from dataplatform.ingest.models import ParseError

FIXTURES: Final = Path("tests/fixtures/nifty_index_press_releases")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; index-change tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _parse(repo_root: Path, era: str, name: str, announced: date) -> Any:
    path = repo_root / FIXTURES / era / name
    return parse_press_release_pdf(path.read_bytes(), filename=name, announced=announced)


def _facts(events: tuple[IndexChangeEvent, ...]) -> set[tuple[str, str, str | None, date]]:
    return {(e.index_slug, e.action.value, e.symbol, e.effective) for e in events}


# ── the listing ────────────────────────────────────────────────────────────────────────────────


def test_the_listing_yields_each_release_with_its_announcement_date(repo_root: Path) -> None:
    path = repo_root / FIXTURES / "listing/press_release_listing_excerpt.html"
    releases = parse_press_release_listing(path.read_bytes(), filename=path.name)
    by_name = {r.filename: r for r in releases}
    semi_annual = by_name["ind_prs10082026.pdf"]
    assert semi_annual.announced == date(2026, 8, 10)
    assert semi_annual.title == "Replacements in indices w.e.f. September 30, 2026"
    assert semi_annual.title_effective == date(2026, 9, 30)
    assert [r.announced for r in releases] == sorted((r.announced for r in releases), reverse=True)
    assert semi_annual.url == "https://niftyindices.com/Press_Release/ind_prs10082026.pdf"


def test_a_page_with_no_press_items_is_a_gate_not_an_empty_archive() -> None:
    with pytest.raises(ParseError, match="no pressItem"):
        parse_press_release_listing(b"<!DOCTYPE html><html>app shell</html>", filename="x.html")


def test_a_release_url_is_only_ever_a_release_filename() -> None:
    with pytest.raises(ParseError):
        press_release_url("../../etc/passwd")


@pytest.mark.parametrize(
    "title",
    [
        "Replacements in indices w.e.f. September 30, 2026",
        "Exclusion of Jio Financial Services Limited from Nifty indices w.e.f. September 7, 2023",
        "Corporate Adjustment for ITC Ltd. in Nifty Indices",
        "Deferment of Index Rebalancing",
        "Change in Nifty 500 & Nifty Smallcap 100 Indices w.e.f December 14, 2015.",
        "Revision in Nifty Transportation & Logistics index methodology and replacements in "
        "indices w.e.f. August 08, 2022",
    ],
)
def test_membership_titles_are_candidates(title: str) -> None:
    assert is_membership_candidate(title)


@pytest.mark.parametrize(
    "title",
    [
        "Changes in Nifty Fixed Income indices w.e.f. August 21, 2026",
        "Inclusion in Nifty IPO index w.e.f. August 18, 2026",
        "Change in NIFTY PSU Bank Index w.e.f. March 29, 2019",
        "Revision in stock selection methodology of the Nifty Next 50 index",
        "Higher frequency for real time index dissemination of Nifty 50",
        "Replacement in ESG indices w.e.f. March 30, 2026",
    ],
)
def test_other_titles_are_not_fetched(title: str) -> None:
    assert not is_membership_candidate(title)


# ── names and dates ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Nifty 50", "nifty50"),
        ("S&P CNX Nifty Index", "nifty50"),
        ("CNX Nifty Junior", "niftynext50"),
        ("NIFTY 500 Index", "nifty500"),
        ("Nifty Midcap 150", "niftymidcap150"),
        ("NIFTY Smallcap 250 Index", "niftysmallcap250"),
        ("Nifty50 Equal Weight", None),
        ("Nifty 500 Healthcare", None),
        ("Nifty Midcap 150 Momentum 50", None),
        ("Nifty MidSmallcap 400", None),
    ],
)
def test_only_tracked_index_headings_alias(heading: str, slug: str | None) -> None:
    assert canonical_index_slug(heading) == slug


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("September 30, 2026 (close of September 29, 2026)", date(2026, 9, 30)),
        ("Sept. 30,2026", date(2026, 9, 30)),
        ("Apr. 25, 2003", date(2003, 4, 25)),
        ("30th September 2026", date(2026, 9, 30)),
        ("Aug 10, 2026", date(2026, 8, 10)),
        # ind_prs21012019.pdf: the text layer splits the day; only before a comma is it rejoined.
        ("January 2 8, 2019 (close of January 25, 2019)", date(2019, 1, 28)),
        ("May 3 2019", date(2019, 5, 3)),
        ("the close of trading", None),
    ],
)
def test_dates_in_every_style_the_releases_use(text: str, expected: date | None) -> None:
    assert parse_date_phrase(text) == expected


# ── one release per layout era ─────────────────────────────────────────────────────────────────


def test_a_modern_release_yields_its_changes_with_the_wrapped_effective_date(
    repo_root: Path,
) -> None:
    """2024-10-10: "effective from October" / "16, 2024" — the date wraps across lines."""
    parsed = _parse(repo_root, "2024_modern", "ind_prs10102024.pdf", date(2024, 10, 10))
    assert parsed.unparsed == ()
    effective = date(2024, 10, 16)
    assert _facts(parsed.events) == {
        ("nifty500", "exclude", "TV18BRDCST", effective),
        ("nifty500", "include", "AKUMS", effective),
        ("niftysmallcap250", "exclude", "TV18BRDCST", effective),
        ("niftysmallcap250", "include", "AKUMS", effective),
    }
    assert all(e.announced == date(2024, 10, 10) for e in parsed.events)
    tv18 = next(e for e in parsed.events if e.symbol == "TV18BRDCST")
    assert tv18.company_name == "TV18 Broadcast Ltd."


def test_a_spin_off_exclusion_reads_its_index_list(repo_root: Path) -> None:
    """2023-09-05: JIOFIN leaves the indices in an "Index Name" table, effective 2023-09-07."""
    parsed = _parse(repo_root, "2023_spinoff_exclusion", "ind_prs05092023.pdf", date(2023, 9, 5))
    effective = date(2023, 9, 7)
    assert _facts(parsed.events) == {
        (slug, "exclude", "JIOFIN", effective)
        for slug in ("nifty50", "nifty100", "nifty200", "nifty500")
    }


def test_a_revocation_table_withdraws_and_replaces(repo_root: Path) -> None:
    """2024-03-19: IREDA's inclusion and V-Guard's exclusion revoked; BSE moves up instead."""
    parsed = _parse(repo_root, "2024_revocation", "ind_prs19032024.pdf", date(2024, 3, 19))
    effective = date(2024, 3, 28)
    facts = _facts(parsed.events)
    assert ("nifty500", "revoke_include", "IREDA", effective) in facts
    assert ("nifty500", "revoke_exclude", "VGUARD", effective) in facts
    assert ("niftymidcap150", "include", "BSE", effective) in facts
    assert ("niftysmallcap250", "exclude", "BSE", effective) in facts
    assert ("nifty200", "revoke_include", "IREDA", effective) in facts
    ireda = next(e for e in parsed.events if e.symbol == "IREDA")
    assert ireda.company_name == "Indian Renewable Energy Dev. Agency Ltd."
    assert ChangeAction.REVOKE_INCLUDE.revoked is ChangeAction.INCLUDE


def test_a_day_split_by_the_text_layer_still_dates_the_release(repo_root: Path) -> None:
    """2019-01-21: "effective from January 2 8, 2019" — unread, NIFTY 500 depth stopped here."""
    parsed = _parse(repo_root, "2019_split_day", "ind_prs21012019.pdf", date(2019, 1, 21))
    assert parsed.unparsed == ()
    effective = date(2019, 1, 28)
    assert _facts(parsed.events) == {
        (slug, action, symbol, effective)
        for slug in ("nifty500", "niftysmallcap250")
        for action, symbol in (
            ("exclude", "DENABANK"),
            ("exclude", "VIJAYABANK"),
            ("include", "CORPBANK"),
            ("include", "CREDITACC"),
        )
    }


def test_a_single_effective_date_stated_after_the_tables_dates_them(repo_root: Path) -> None:
    """2018-08-01: the release states its one effective date below its tables, not above."""
    parsed = _parse(repo_root, "2018_trailing_date", "ind_prs01082018.pdf", date(2018, 8, 1))
    assert parsed.unparsed == ()
    effective = date(2018, 8, 8)
    assert _facts(parsed.events) == {
        (slug, action, symbol, effective)
        for slug in ("nifty500", "niftysmallcap250")
        for action, symbol in (("exclude", "TECHNO"), ("include", "BDL"))
    }


def test_a_detached_layout_is_unparsed_not_misread(repo_root: Path) -> None:
    """2007: every statement first, every table after — rows cannot be attributed, so none are."""
    parsed = _parse(repo_root, "2007_detached", "ind_prs12092007.pdf", date(2007, 9, 12))
    assert parsed.events == ()
    assert set(parsed.tracked_sections) == {"nifty50", "nifty100", "nifty500"}
    assert len(parsed.unparsed) == 3
    assert all("detached" in problem for problem in parsed.unparsed)


def test_scrip_name_tables_are_read_not_taken_for_a_detached_layout(repo_root: Path) -> None:
    """2016-10-17: "Sr. No. Scrip Name Symbol" tables — the DQ-5.1 gate's "detached layout".

    Exact facts, actions included: an inclusion read as an exclusion (or the other way round)
    fails here, and so does dropping the "Scrip Name" header (every section unparsed again).
    """
    parsed = _parse(repo_root, "2016_scrip_name", "ind_prs17102016.pdf", date(2016, 10, 17))
    assert parsed.unparsed == ()
    eff = date(2016, 11, 15)
    assert _facts(parsed.events) == {
        ("niftynext50", "exclude", "CAIRN", eff),
        ("niftynext50", "include", "HAVELLS", eff),
        ("nifty100", "exclude", "CAIRN", eff),
        ("nifty100", "include", "HAVELLS", eff),
        ("nifty200", "exclude", "CAIRN", eff),
        ("nifty200", "include", "CROMPTON", eff),
        ("nifty500", "exclude", "CAIRN", eff),
        ("nifty500", "include", "CROMPTON", eff),
        ("niftymidcap150", "exclude", "HAVELLS", eff),
        ("niftymidcap150", "include", "CROMPTON", eff),
    }


def test_each_section_takes_the_date_its_own_intro_clause_gives(repo_root: Path) -> None:
    """2015-01-23: CNX 200/500 change on Feb 2, Nifty Midcap 50 on Feb 23 — one sentence.

    The nearest date stated before a section is Feb 23 for all of them; the tracked sections are
    named in the Feb 2 clause. The sections are numbered "1." — unread before M14.1.
    """
    parsed = _parse(repo_root, "2015_dated_clauses", "ind_prs23012015.pdf", date(2015, 1, 23))
    assert parsed.unparsed == ()
    eff = date(2015, 2, 2)
    assert _facts(parsed.events) == {
        ("nifty200", "exclude", "ARVIND", eff),
        ("nifty200", "include", "CRISIL", eff),
        ("nifty500", "exclude", "ARVIND", eff),
        ("nifty500", "include", "LAOPALA", eff),
    }


def test_parts_of_an_intro_dated_apart_date_their_own_sections(repo_root: Path) -> None:
    """2016-10-17: part B is Oct 24, parts A and C Nov 15; every tracked section is part C."""
    parsed = _parse(repo_root, "2016_scrip_name", "ind_prs17102016.pdf", date(2016, 10, 17))
    assert {e.effective for e in parsed.events} == {date(2016, 11, 15)}


def test_columns_printed_apart_are_unparsed_never_dropped(repo_root: Path) -> None:
    """2014-02-27: the text layer prints table columns apart — the truly detached layout.

    Before M14.1 the CNX Nifty and Junior sections vanished without a word, and the NIFTY 50 walk
    crossed the 2014-03-28 change without applying it. A tracked section must yield events or say
    why it did not.
    """
    parsed = _parse(repo_root, "2014_columns_apart", "ind_prs27022014.pdf", date(2014, 2, 27))
    assert parsed.events == ()
    assert set(parsed.tracked_sections) == {
        "nifty50",
        "niftynext50",
        "nifty100",
        "nifty200",
        "nifty500",
    }
    unread = {canonical_index_slug(problem.split(":")[0]) for problem in parsed.unparsed}
    assert unread == set(parsed.tracked_sections)


def test_an_image_only_release_fails_loud(repo_root: Path) -> None:
    path = repo_root / FIXTURES / "2023_image_only/ind_prs19062023.pdf"
    with pytest.raises(ParseError, match="image-only"):
        parse_press_release_pdf(path.read_bytes(), filename=path.name, announced=date(2023, 6, 19))


def test_html_in_place_of_a_pdf_is_refused() -> None:
    with pytest.raises(ParseError, match="not a PDF"):
        parse_press_release_pdf(
            b"<!DOCTYPE html>", filename="ind_prs01012026.pdf", announced=date(2026, 1, 1)
        )
