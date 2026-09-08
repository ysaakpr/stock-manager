"""The `ffix` reader: dated index constituent membership with free-float weightage (W2).

Three things here are worth a test rather than a reading.

**The furniture rule.** An `ffix` file interleaves constituent rows with banner lines and
all-blank separators, and the separator count per file varies (six in the very first file, 34 in
the 2011 one). The
brief for this task warned that skipping them positionally would work on the file you looked at
and eat a constituent on the file you did not, so `test_a_separator_row_is_skipped_by_rule…` puts
a separator immediately *above* a constituent and asserts the constituent survives.

**The index identity.** Membership comes from `INDEX_FLG` and never from the banner. In March 2013
the banner renamed `S&P CNX Nifty Sec.` → `CNX Nifty Sec.` while `INDEX_FLG` stayed `NIFTY`; a
reader keyed on the banner would have invented a new index and reported the whole NIFTY 50 as
having been removed and re-added. Two fixtures straddle that rename, including the single-session
relapse on 2013-04-09.

**`Decimal`, exactly.** Two tests fail if a `float` is reintroduced anywhere between the bytes
and an `FfixRow`: one published free-float market cap that `float` holds as
77678920242.839996337890625, and one index whose twelve published weights `float` accumulates to
100.00999999999999 instead of 100.01.

Offline and deterministic: four frozen era fixtures under `tests/fixtures/nse_pr_bundle/`, each a
reduced copy of a real bundle from L0 with its `ffix` member intact (see each `manifest.json`).
Nothing here reads the lake or the network.
"""

from __future__ import annotations

import inspect
import json
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle import bundle as bundle_module
from dataplatform.ingest.nse.pr_bundle.bundle import MemberKind, PrBundle
from dataplatform.ingest.nse.pr_bundle.ffix import (
    FFIX_COLUMNS,
    FFIX_FIRST_SESSION,
    FFIX_LAST_SESSION,
    FfixFile,
    FfixRow,
    parse_ffix,
    parse_ffix_bundle,
)

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle"

#: The four frozen `ffix` eras and the session each publishes. `ffix_three_index` is the archive's
#: very first bundle; the other three are the boundaries the corpus sweep actually found.
ERAS: Final[tuple[tuple[str, str, date], ...]] = (
    ("ffix_three_index", "PR040110.zip", date(2010, 1, 4)),
    ("ffix_seventeen_index", "PR310111.zip", date(2011, 1, 31)),
    ("ffix_renamed_banner", "PR040313.zip", date(2013, 3, 4)),
    ("ffix_banner_relapse", "PR090413.zip", date(2013, 4, 9)),
)

_HEADER: Final = ",".join(FFIX_COLUMNS)


def _era(name: str) -> FfixFile:
    """Parse one era fixture through the bundle, so `knowable_date` comes from the payload."""
    era, archive, _ = next(e for e in ERAS if e[0] == name)
    with PrBundle((FIXTURES / era / archive).read_bytes(), filename=archive) as bundle:
        return parse_ffix_bundle(bundle)


def _payload(*body: str) -> bytes:
    """A synthetic `ffix` file: the real header plus the given body lines."""
    return ("\n".join((_HEADER, *body)) + "\n").encode("latin-1")


def _parse(*body: str) -> FfixFile:
    return parse_ffix(_payload(*body), filename="ffix010110.csv", knowable_date=date(2010, 1, 1))


# ── the fixtures are what they claim to be ───────────────────────────────────────────────────


@pytest.mark.parametrize(("era", "archive", "session"), ERAS)
def test_each_era_fixture_matches_its_manifest(era: str, archive: str, session: date) -> None:
    """A fixture that drifted from the L0 payload it was reduced from proves nothing.

    The manifest records the *original* bundle's sha256 and the `ffix` member's own sha256; only
    the second is checkable from the reduced fixture, and it is the byte range under test.
    """
    import hashlib

    manifest = json.loads((FIXTURES / era / "manifest.json").read_text())
    assert manifest["session"] == session.isoformat()
    assert manifest["archive_filename"] == archive

    with zipfile.ZipFile(FIXTURES / era / archive) as archive_zip:
        member = archive_zip.read(manifest["ffix_member"])
    assert len(member) == manifest["ffix_member_bytes"]
    assert hashlib.sha256(member).hexdigest() == manifest["ffix_member_sha256"]


# ── knowable_date comes from the payload, never from a clock ─────────────────────────────────


@pytest.mark.parametrize(("era", "archive", "session"), ERAS)
def test_knowable_date_is_the_bundles_own_publication_date(
    era: str, archive: str, session: date
) -> None:
    """Each era carries a *different* knowable date, which an ingest-time clock cannot produce.

    This is the invariant-#7 test. `corporate_actions` has 47,887 rows sharing one knowable_date
    because a sibling ingester used `clock.now().date()`; four fixtures parsed in one process
    yielding four distinct dates is what rules that out here.
    """
    parsed = _era(era)
    assert parsed.knowable_date == session
    assert {row.knowable_date for row in parsed.rows} == {session}


def test_the_four_eras_do_not_share_a_knowable_date() -> None:
    assert len({_era(era).knowable_date for era, _, _ in ERAS}) == len(ERAS)


def test_the_ffix_parsers_accept_no_clock() -> None:
    """Neither entry point takes a `Clock`, so "now" cannot be injected as a knowable date."""
    for func in (parse_ffix, parse_ffix_bundle):
        params = set(inspect.signature(func).parameters)
        assert "clock" not in params, f"{func.__name__} must not accept a clock: {params}"
    assert "knowable_date" in inspect.signature(parse_ffix).parameters


# ── the payload is index membership, and the registry now says so ────────────────────────────


def test_the_member_kind_is_registered_as_parsed_and_not_as_fixed_income() -> None:
    """`MemberKind.FFIX` said "Fixed income" until 2026-09-08. It is index membership.

    The docstring is the artefact that misled the repo for three years, so it is asserted on
    rather than left to review.
    """
    assert MemberKind.FFIX.parsed_by_w2

    # Asserted against the *source*, because an enum member's trailing string literal is not
    # attached as its `__doc__` — `MemberKind.FFIX.__doc__` is the class docstring. The registry
    # entry is a source artefact, and the source is therefore the only place to check it.
    registry = Path(inspect.getsourcefile(bundle_module) or "").read_text(encoding="utf-8")
    entry = registry.split('FFIX = "ffix"', 1)[1].split('MCAP = "mcap"', 1)[0]
    assert "Dated index constituent membership with free-float weightage" in entry
    assert "free-float **index membership** file" in registry
    # The old claim may only survive as the quoted mistake it was, never as the description.
    assert '"Fixed income" until 2026-09-08 and that was wrong' in entry
    assert '"""Fixed income."""' not in registry


def test_the_first_bundle_carries_the_three_headline_indices_at_their_nominal_sizes() -> None:
    """NIFTY 50, JR. NIFTY 50, CNX 100 100 — the check that these are complete lists, not samples.

    If the reader were dropping rows (a positional separator skip, say) these counts would come
    out short, and a short constituent list is indistinguishable from a reconstitution event once
    it reaches the census.
    """
    parsed = _era("ffix_three_index")

    assert parsed.indices == ("NIFTY", "JR. NIFTY", "CNX 100")
    assert len(parsed.constituents("NIFTY")) == 50
    assert len(parsed.constituents("JR. NIFTY")) == 50
    assert len(parsed.constituents("CNX 100")) == 100
    assert len(parsed.rows) == 200


def test_all_seventeen_indices_are_present_once_the_sectorals_arrive() -> None:
    parsed = _era("ffix_seventeen_index")

    assert len(parsed.indices) == 17
    assert set(parsed.indices) == {
        "NIFTY",
        "JR. NIFTY",
        "CNX 100",
        "CNX 500",
        "CNX Midcap",
        "Nifty Midcap 50",
        "BANK Nifty",
        "CNX IT",
        "CNX Infrastructure",
        "CNX Realty",
        "CNX ENERGY",
        "CNX FMCG",
        "CNX MNC",
        "CNX PHARMA",
        "CNX PSE",
        "CNX PSU BANK",
        "CNX SERVICE",
    }
    assert len(parsed.constituents("CNX 500")) == 500


def test_a_parsed_row_carries_the_published_values_verbatim() -> None:
    """One hand-checked row from the first file, field by field, as text→`Decimal`."""
    abb = next(
        row
        for row in _era("ffix_three_index").rows
        if row.index_name == "NIFTY" and row.symbol == "ABB"
    )

    assert abb.series == "EQ"
    assert abb.security_name == "ABB LTD."
    assert abb.issue_cap == Decimal("211908375")
    assert abb.investible_factor == Decimal("0.478924")
    assert abb.close_price == Decimal("765.40")
    assert abb.ff_market_cap == Decimal("77678920242.84")
    assert abb.weightage == Decimal("0.51")
    assert abb.l0_key is None


def test_no_row_carries_an_isin_field() -> None:
    """Invariant #2 says ISIN is the only join key, so this dataset must offer no fake one.

    A `symbol` column that a later task mistakes for a join key is the failure mode; the absence
    of an `isin` field is what makes that mistake impossible rather than merely discouraged.
    """
    fields = FfixRow.model_fields
    assert "isin" not in fields
    assert not any("isin" in name.lower() for name in fields)
    assert "symbol" in fields


# ── furniture is skipped by rule, never by position ──────────────────────────────────────────


def test_the_first_files_furniture_rows_are_all_accounted_for() -> None:
    """Measured, against the brief's estimate of "eight junk rows" in this file: it is **nine**.

    `ffix040110.csv` is 210 lines: 1 header + 200 constituents + 6 all-blank separators + 3
    banners. The point of asserting the full accounting rather than one number is that every line
    of the file has to land in exactly one bucket — a reader that silently dropped a constituent
    would still satisfy a lone separator count.
    """
    parsed = _era("ffix_three_index")

    assert parsed.separator_rows == 6
    assert len(parsed.announced_indices) == 3
    assert len(parsed.rows) == 200
    assert 1 + len(parsed.rows) + parsed.separator_rows + len(parsed.announced_indices) == 210
    assert all(row.index_name.strip() for row in parsed.rows)


def test_a_separator_row_is_skipped_by_rule_and_the_row_below_it_survives() -> None:
    """The regression a positional skip would cause: a separator eating the next constituent.

    Two separators sit directly above `CIPLA`, which a "skip rows 2 and 3" reader would drop.
    """
    parsed = _parse(
        "NIFTY,ABB,EQ,ABB LTD.,211908375,0.478924,       765.40,       77678920242.84,0.51",
        " , , , , , , , ",
        " , , , , , , , ",
        "NIFTY,CIPLA,EQ,CIPLA LTD,802921357,0.618757,       337.55,     167699299066.98,1.1",
    )

    assert parsed.separator_rows == 2
    assert [row.symbol for row in parsed.rows] == ["ABB", "CIPLA"]


def test_a_banner_row_is_announced_and_never_becomes_an_index_or_a_constituent() -> None:
    parsed = _parse(
        " , , ,S&P CNX Nifty Sec., , , , ",
        " , , , , , , , ",
        "NIFTY,ABB,EQ,ABB LTD.,211908375,0.478924,       765.40,       77678920242.84,0.51",
    )

    assert parsed.indices == ("NIFTY",)
    assert parsed.announced_indices == ("S&P CNX Nifty Sec.",)
    assert [row.symbol for row in parsed.rows] == ["ABB"]


def test_the_banner_rename_does_not_move_the_index_flg() -> None:
    """2013-03-04: the banner becomes `CNX Nifty Sec.`; `INDEX_FLG` stays `NIFTY`.

    Keying membership off the banner would report all 50 NIFTY constituents removed on this date
    and 50 added to a brand-new index — a fabricated reconstitution event of the largest possible
    size, on a date nothing happened.
    """
    renamed = _era("ffix_renamed_banner")

    assert "CNX Nifty Sec." in renamed.announced_indices
    assert "S&P CNX Nifty Sec." not in renamed.announced_indices
    assert "NIFTY" in renamed.indices
    assert len(renamed.constituents("NIFTY")) == 50


def test_the_old_banner_relapses_for_one_session_without_disturbing_membership() -> None:
    """2013-04-09 ships the pre-rename banner again; from 2013-04-10 the rename sticks for good.

    A one-session relapse is the shape a rename-sniffing reader gets wrong, which is why it has a
    fixture of its own.
    """
    relapse = _era("ffix_banner_relapse")

    assert "S&P CNX Nifty Sec." in relapse.announced_indices
    assert "CNX Nifty Sec." not in relapse.announced_indices
    assert "NIFTY" in relapse.indices
    assert len(relapse.constituents("NIFTY")) == 50

    # Membership between the two fixtures did move, and by exactly the April 2013 NIFTY
    # reconstitution — which is the point: a real four-symbol change, not the 50-in-50-out
    # artefact a banner-keyed reader would have manufactured from the rename.
    before = _era("ffix_renamed_banner").symbols("NIFTY")
    after = relapse.symbols("NIFTY")
    assert after - before == {"INDUSINDBK", "NMDC"}
    assert before - after == {"SIEMENS", "WIPRO"}


def test_numeric_fields_survive_their_leading_pad() -> None:
    """`CLOSE_PRIC` and `FF_MKT_CAP` are right-aligned in a padded field, corpus-wide."""
    row = _parse(
        "NIFTY,ABB,EQ,ABB LTD.,211908375,0.478924,       765.40,              77678920242.84,0.51"
    ).rows[0]

    assert row.close_price == Decimal("765.40")
    assert row.ff_market_cap == Decimal("77678920242.84")


# ── Decimal, not float ───────────────────────────────────────────────────────────────────────


def test_every_numeric_field_is_a_decimal() -> None:
    row = _era("ffix_three_index").rows[0]
    for field in (
        "issue_cap",
        "investible_factor",
        "close_price",
        "ff_market_cap",
        "weightage",
    ):
        value = getattr(row, field)
        assert isinstance(value, Decimal), f"{field} is {type(value).__name__}, not Decimal"
        assert not isinstance(value, float)


def test_a_free_float_market_cap_is_held_exactly_and_float_cannot_hold_it() -> None:
    """ABB's `FF_MKT_CAP` on 2010-01-04 is a value binary floating point does not represent.

    Published: 77678920242.84. As a `float` that is 77678920242.839996337890625 — off by 3.7
    paise on one constituent of one index on one day, and free-float market cap is the
    denominator every weight in the file is computed against. This is the test that fails if a
    `float` is reintroduced anywhere on the path from bytes to `FfixRow`.
    """
    published = "77678920242.84"
    abb = next(
        row
        for row in _era("ffix_three_index").rows
        if row.index_name == "NIFTY" and row.symbol == "ABB"
    )

    assert abb.ff_market_cap == Decimal(published)
    assert Decimal(float(published)) != Decimal(published)


def test_a_weight_sum_that_float_accumulation_would_drift_is_exact() -> None:
    """BANK Nifty on 2011-01-31: `Decimal` sums to 100.01, `float` to 100.00999999999999.

    Twelve two-decimal weights are enough to break `float` accumulation, and the drift is in the
    direction that makes a rails or rebalance check on "weights sum to 100" fail intermittently
    rather than never.
    """
    weights = [row.weightage for row in _era("ffix_seventeen_index").constituents("BANK Nifty")]

    assert sum(weights, Decimal(0)) == Decimal("100.01")
    assert Decimal(repr(sum(float(w) for w in weights))) != Decimal("100.01")


@pytest.mark.parametrize(("era", "_archive", "_session"), ERAS)
def test_each_index_weights_to_about_one_hundred_percent(
    era: str, _archive: str, _session: date
) -> None:
    """Every index in every era sums to 100 ± the source's own rounding.

    A dropped constituent shows up here as a short sum, so this is a whole-file completeness check
    and not a restatement of the source's arithmetic.
    """
    parsed = _era(era)
    for index_name in parsed.indices:
        total = sum((row.weightage for row in parsed.constituents(index_name)), Decimal(0))
        assert abs(total - Decimal(100)) < Decimal("0.5"), (era, index_name, total)


# ── it fails loud and specific ───────────────────────────────────────────────────────────────


def test_an_unexpected_header_names_the_header_it_got_and_line_one() -> None:
    with pytest.raises(ParseError) as caught:
        parse_ffix(
            b"INDEX_FLG,SYMBOL,SERIES\nNIFTY,ABB,EQ\n",
            filename="ffix010110.csv",
            knowable_date=date(2010, 1, 1),
        )
    assert "unexpected header" in str(caught.value)
    assert caught.value.line == 1


def test_a_constituent_row_with_the_wrong_field_count_raises_and_names_its_line() -> None:
    """A short *constituent* row is an error; a short *banner* row is furniture. Both are 8 fields.

    So the field-count check must run after the blank-`INDEX_FLG` test, and this asserts it does
    by feeding a row that is short and has a filled INDEX_FLG.
    """
    with pytest.raises(ParseError) as caught:
        _parse("NIFTY,ABB,EQ,ABB LTD.,211908375,0.478924,765.40,77678920242.84")
    assert "expected 9 columns, got 8" in str(caught.value)
    assert caught.value.line == 2


def test_a_constituent_row_with_no_symbol_raises() -> None:
    with pytest.raises(ParseError, match="has no SYMBOL"):
        _parse("NIFTY, ,EQ,ABB LTD.,211908375,0.478924,765.40,77678920242.84,0.51")


@pytest.mark.parametrize(
    ("bad_row", "field"),
    [
        ("NIFTY,ABB,EQ,ABB LTD.,n/a,0.478924,765.40,77678920242.84,0.51", "ISSUE_CAP"),
        ("NIFTY,ABB,EQ,ABB LTD.,211908375,-,765.40,77678920242.84,0.51", "INVESTIBLE_FACTOR"),
        ("NIFTY,ABB,EQ,ABB LTD.,211908375,0.478924,,77678920242.84,0.51", "CLOSE_PRIC"),
        ("NIFTY,ABB,EQ,ABB LTD.,211908375,0.478924,765.40,NA,0.51", "FF_MKT_CAP"),
        ("NIFTY,ABB,EQ,ABB LTD.,211908375,0.478924,765.40,77678920242.84,--", "WEIGHTAGE"),
    ],
)
def test_a_non_numeric_field_raises_naming_the_column(bad_row: str, field: str) -> None:
    """It never coerces to zero. A zero weight silently rebalances a reconstructed index."""
    with pytest.raises(ParseError) as caught:
        _parse(bad_row)
    assert field in str(caught.value)
    assert "not a number" in str(caught.value)


def test_an_empty_body_raises_rather_than_reading_as_zero_constituents() -> None:
    with pytest.raises(ParseError, match="empty response body"):
        parse_ffix(b"   \n", filename="ffix010110.csv", knowable_date=date(2010, 1, 1))


def test_markup_wearing_a_two_hundred_raises() -> None:
    with pytest.raises(ParseError, match="markup, not CSV"):
        parse_ffix(
            b"<html><body>error</body></html>",
            filename="ffix010110.csv",
            knowable_date=date(2010, 1, 1),
        )


def test_a_bundle_with_no_ffix_member_raises_naming_what_it_does_carry() -> None:
    """The normal case after 2013-04-30, and a planning mistake worth a specific message."""
    with PrBundle(
        (FIXTURES / "classic" / "PR020113.zip").read_bytes(), filename="PR020113.zip"
    ) as bundle:
        assert not bundle.has(MemberKind.FFIX)
        with pytest.raises(ParseError, match="carries no 'ffix' member"):
            parse_ffix_bundle(bundle)


# ── the pinned span ──────────────────────────────────────────────────────────────────────────


def test_the_pinned_span_brackets_every_era_fixture() -> None:
    """The span is a measurement off all 4,124 bundles; the fixtures must sit inside it."""
    assert date(2010, 1, 4) == FFIX_FIRST_SESSION
    assert date(2013, 4, 30) == FFIX_LAST_SESSION
    assert all(FFIX_FIRST_SESSION <= session <= FFIX_LAST_SESSION for _, _, session in ERAS)
