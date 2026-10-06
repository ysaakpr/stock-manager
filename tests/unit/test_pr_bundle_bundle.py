"""Opening a PR bundle, and dating it from its own contents (W2).

The bundle's publication date is the input to every `knowable_date` this source produces, so the
derivation itself needs to be as hard to break as the stamping is. These tests pin it from the
real fixtures and from the member manifests recorded off the wire.

They also run the `MemberKind` registry against the **complete** member lists of the original
bundles — recorded in each fixture's `manifest.json` — rather than against the reduced set the
trimmed fixture zips carry (see `tests/fixtures/nse_pr_bundle/PROVENANCE.md`). A member name that
the real archive produced and `MemberKind` does not know is a registry hole, and the reduced zips
would hide it.

Offline and deterministic (B8): every byte read here comes from `tests/fixtures/nse_pr_bundle/`.
"""

from __future__ import annotations

import json
import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Final

import pytest

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle.bundle import (
    ARCHIVE_START,
    URL_TEMPLATE,
    MemberKind,
    PrBundle,
    url_for,
)

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle"

ERAS: Final[tuple[tuple[str, str, date], ...]] = (
    ("ix_era", "PR040110.zip", date(2010, 1, 4)),
    ("classic", "PR020113.zip", date(2013, 1, 2)),
    ("mcap_upper", "PR010724.zip", date(2024, 7, 1)),
    ("lowercase", "PR040926.zip", date(2026, 9, 4)),
)


def _bundle(era: str, name: str) -> PrBundle:
    return PrBundle((FIXTURES / era / name).read_bytes(), filename=name)


def _manifest(era: str) -> dict[str, object]:
    loaded = json.loads((FIXTURES / era / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


# ── dating ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("era", "name", "session"), ERAS)
def test_publication_date_comes_from_the_members(era: str, name: str, session: date) -> None:
    with _bundle(era, name) as bundle:
        assert bundle.publication_date == session


def test_both_date_widths_coexist_in_one_zip_and_still_agree() -> None:
    """`Bc010724.csv` (DDMMYY) sits beside `MCAP01072024.csv` (DDMMYYYY) in the 2024 bundle.

    This is the hazard that makes a per-bundle date format impossible: the width is read per
    member, and the two must still resolve to the same session.
    """
    with _bundle("mcap_upper", "PR010724.zip") as bundle:
        widths = {
            len(m.name) - len(m.name.rstrip("0123456789.csvCSV"))
            for m in bundle.members
            if m.member_date is not None
        }
        dates = {m.member_date for m in bundle.members if m.member_date is not None}
        names = sorted(m.name for m in bundle.members if m.member_date is not None)

    assert "Bc010724.csv" in names and "MCAP01072024.csv" in names
    assert len(widths) > 1, "the fixture must retain both DDMMYY and DDMMYYYY members"
    assert dates == {date(2024, 7, 1)}


def test_members_that_disagree_about_the_date_raise() -> None:
    """A bundle whose own members cannot agree what session they are must not date an action."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("Bc020113.csv", "x")
        zf.writestr("Pr030113.csv", "x")
    with pytest.raises(ParseError, match="disagree about the bundle's date"):
        PrBundle(buffer.getvalue(), filename="PR020113.zip")


def test_an_archive_name_contradicting_its_members_raises() -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("Bc020113.csv", "x")
    with pytest.raises(ParseError, match="not the same session"):
        PrBundle(buffer.getvalue(), filename="PR090113.zip")


def test_an_undated_bundle_raises_rather_than_guessing() -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("Readme.txt", "x")
    with pytest.raises(ParseError, match="cannot be dated from its own contents"):
        PrBundle(buffer.getvalue(), filename="bundle.zip")


def test_html_wearing_a_200_is_not_a_bundle() -> None:
    with pytest.raises(ParseError, match="not a zip archive"):
        PrBundle(b"<html>404</html>", filename="PR020113.zip")


# ── member registry ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("era", "name", "session"), ERAS)
def test_every_real_member_name_is_registered(era: str, name: str, session: date) -> None:
    """Run the registry against the original bundles' complete member lists, not the trim."""
    manifest = _manifest(era)
    members = manifest["original_members"]
    assert isinstance(members, list)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for entry in members:
            assert isinstance(entry, dict)
            zf.writestr(str(entry["name"]), b"")
    with PrBundle(buffer.getvalue(), filename=str(manifest["archive_filename"])) as bundle:
        assert bundle.publication_date == session
        unknown = [m.name for m in bundle.unknown_members]
    assert not unknown, f"{era}: MemberKind does not know {unknown}"


def test_the_four_parsed_members_are_flagged_as_such() -> None:
    """`FFIX` joined this set on 2026-09-08, when the member turned out to be index membership."""
    parsed = {k for k in MemberKind if k.parsed_by_w2}
    assert parsed == {MemberKind.BC, MemberKind.FFIX, MemberKind.IX, MemberKind.MCAP}


def test_member_lookup_is_case_insensitive_across_the_2025_cutover() -> None:
    """`Bc020113.csv` and `bc04092026.csv` are the same kind: casing changed, identity did not."""
    with _bundle("classic", "PR020113.zip") as old:
        old_member = old.member(MemberKind.BC)
    with _bundle("lowercase", "PR040926.zip") as new:
        new_member = new.member(MemberKind.BC)
    assert old_member is not None and old_member.name == "Bc020113.csv"
    assert new_member is not None and new_member.name == "bc04092026.csv"


def test_ix_and_mcap_absence_is_reported_not_raised() -> None:
    """Absence is a measured property of the era, so `has` answers it without an exception."""
    with _bundle("classic", "PR020113.zip") as bundle:
        assert bundle.has(MemberKind.BC)
        assert not bundle.has(MemberKind.IX), "Ix was gone by 2013"
        assert not bundle.has(MemberKind.MCAP), "mcap had not appeared by 2013"
        with pytest.raises(ParseError, match="carries no 'ix' member"):
            bundle.read(MemberKind.IX)


def test_readme_members_are_documentation_not_unknown_reports() -> None:
    with _bundle("ix_era", "PR040110.zip") as bundle:
        docs = [m for m in bundle.members if m.is_documentation]
    assert [m.name for m in docs] == ["Readme.txt"]


# ── url ──────────────────────────────────────────────────────────────────────────────────────


def test_url_for_matches_the_registered_template() -> None:
    assert url_for(date(2010, 1, 4)).endswith("/PR040110.zip")
    assert url_for(date(2026, 9, 4)).endswith("/PR040926.zip")
    assert url_for(date(2024, 7, 1)) == URL_TEMPLATE.format(DDMMYY="010724")


def test_the_archive_start_is_the_measured_floor() -> None:
    """2009-12-31 and 2010-01-01 are 404; 2010-01-04 is 200 (smoke fetch, 2026-09-08)."""
    assert date(2010, 1, 4) == ARCHIVE_START
