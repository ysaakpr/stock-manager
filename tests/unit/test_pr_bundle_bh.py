"""The `bh` reader: securities that hit a daily price band (X2, H2).

Eight frozen fixtures under `tests/fixtures/nse_pr_bundle/bh_*`, one per format shape found by
sniffing the member in every bundle L0 holds. The shape that matters most is `bh_sr_swapped`: the
one file whose `HIGH/LOW` and `SECURITY` columns trade places, which a positional reader turns into
a file of issuer names posing as band sides. Offline; nothing here reads the lake or the network.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Final

import pytest

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle import (
    BandSide,
    BhFile,
    MemberKind,
    PrBundle,
    parse_bh,
    parse_bh_bundle,
)

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle"

#: era dir, archive name, session, row count, first row (symbol, series, side).
ERAS: Final[tuple[tuple[str, str, date, int, tuple[str, str, BandSide] | None], ...]] = (
    ("bh_sr", "PR040110.zip", date(2010, 1, 4), 172, ("ARIES", "BE", BandSide.UPPER)),
    ("bh_index_flag", "PR110810.zip", date(2010, 8, 11), 63, ("RAYMOND", "EQ", BandSide.UPPER)),
    ("bh_sr_padded", "PR170910.zip", date(2010, 9, 17), 60, ("BELLCERATL", "BE", BandSide.UPPER)),
    ("bh_sr_swapped", "PR091110.zip", date(2010, 11, 9), 68, ("ABHISHEK", "BE", BandSide.UPPER)),
    ("bh_series", "PR010713.zip", date(2013, 7, 1), 162, ("ADSL", "EQ", BandSide.UPPER)),
    ("bh_empty", "PR170521.zip", date(2021, 5, 17), 0, None),
    ("bh_no_flag", "PR040926.zip", date(2026, 9, 4), 286, ("1015SCL28B", "AZ", BandSide.UPPER)),
)


def _parse(era: str, archive: str) -> BhFile:
    with PrBundle((FIXTURES / era / archive).read_bytes(), filename=archive) as bundle:
        return parse_bh_bundle(bundle)


@pytest.mark.parametrize(("era", "archive", "session", "count", "first"), ERAS)
def test_every_era_parses_by_header_name(
    era: str,
    archive: str,
    session: date,
    count: int,
    first: tuple[str, str, BandSide] | None,
) -> None:
    parsed = _parse(era, archive)
    assert parsed.publication_date == session
    assert len(parsed.rows) == count
    assert {row.session for row in parsed.rows} <= {session}
    assert {row.publication_date for row in parsed.rows} <= {session}
    if first is not None:
        row = parsed.rows[0]
        assert (row.symbol, row.series, row.side) == first
    assert all(row.side in (BandSide.UPPER, BandSide.LOWER) for row in parsed.rows)


@pytest.mark.parametrize("era", [e[0] for e in ERAS] + ["bh_misserved"])
def test_fixture_bh_member_is_byte_identical_to_the_manifest(era: str) -> None:
    manifest = json.loads((FIXTURES / era / "manifest.json").read_text(encoding="utf-8"))
    with zipfile.ZipFile(FIXTURES / era / manifest["archive_filename"]) as zf:
        body = zf.read(manifest["bh_member"])
    assert hashlib.sha256(body).hexdigest() == manifest["bh_member_sha256"]


def test_the_swapped_file_reads_the_side_not_the_issuer_name() -> None:
    """2010-11-09 is `SYMBOL,SR,HIGH/LOW,SECURITY,...`; position 3 is the issuer there."""
    parsed = _parse("bh_sr_swapped", "PR091110.zip")
    row = parsed.rows[0]
    assert row.side is BandSide.UPPER
    assert row.security_name == "ABHISHEK CORPORATION LTD"


def test_both_sides_are_read() -> None:
    sides = {row.side for row in _parse("bh_series", "PR010713.zip").rows}
    assert sides == {BandSide.UPPER, BandSide.LOWER}


def test_the_misserved_bundle_is_refused_rather_than_dated_by_its_name() -> None:
    """`PR020118.zip` holds the 2019-01-02 members; dating it 2018 would leak a year forward."""
    payload = (FIXTURES / "bh_misserved" / "PR020118.zip").read_bytes()
    with pytest.raises(ParseError, match="not the same session"):
        PrBundle(payload, filename="PR020118.zip")


def _synthetic(header: str, *body: str) -> BhFile:
    payload = ("\n".join((header, *body)) + "\n").encode("latin-1")
    return parse_bh(payload, filename="bh010113.csv", publication_date=date(2013, 1, 1))


def test_an_unknown_header_column_raises() -> None:
    with pytest.raises(ParseError, match="unexpected header cell"):
        _synthetic("SYMBOL,SERIES,SECURITY,HIGH/LOW,PRICE", "ABC,EQ,ABC LTD,H,10")


def test_a_missing_header_column_raises() -> None:
    with pytest.raises(ParseError, match="missing"):
        _synthetic("SYMBOL,SERIES,SECURITY", "ABC,EQ,ABC LTD")


def test_a_side_other_than_h_or_l_raises() -> None:
    with pytest.raises(ParseError, match="not H or L"):
        _synthetic("SYMBOL,SERIES,SECURITY,HIGH/LOW", "ABC,EQ,ABC LTD,X")


def test_a_value_beyond_the_header_raises() -> None:
    with pytest.raises(ParseError, match="beyond its"):
        _synthetic("SYMBOL,SERIES,SECURITY,HIGH/LOW", "ABC,EQ,ABC LTD,H,surprise")


def test_html_wearing_a_200_raises() -> None:
    with pytest.raises(ParseError, match="markup"):
        parse_bh(b"<html></html>", filename="bh010113.csv", publication_date=date(2013, 1, 1))


def test_a_bundle_without_bh_raises_and_says_so() -> None:
    with PrBundle(
        (FIXTURES / "classic" / "PR020113.zip").read_bytes(), filename="PR020113.zip"
    ) as bundle:
        assert not bundle.has(MemberKind.BH)
        with pytest.raises(ParseError, match="no 'bh' member"):
            parse_bh_bundle(bundle)
