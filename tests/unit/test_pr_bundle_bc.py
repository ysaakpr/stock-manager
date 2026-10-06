"""`Bc` corporate actions, and the one guarantee this whole wave exists to buy (W2).

`knowable_date` on a `Bc` row is the bundle's own publication date. Not ingest time, not today,
not a clock a caller passed in — the date the file itself carries. That is the difference between
invariant #7 being a measurement and being a decoration: `dataplatform.ingest.bse.corp_actions`
stamps `clock.now().date()`, and the consequence is 47,887 rows in `corporate_actions` sharing a
single knowable date, so a PIT-honest backtest sees no corporate actions on any historical
decision date at all.

The first three tests here exist to fail if anyone reintroduces an ingest-time clock, and they
come at it from three independent directions, because one of them alone is too easy to satisfy:

1. `test_knowable_date_is_the_bundles_own_date_not_today` — the values equal the 2013 fixture's
   own session, which an ingest-time clock could never produce.
2. `test_the_bc_parsers_accept_no_clock` — the *signatures* refuse one, so a caller cannot inject
   "now" without changing the API.
3. `test_the_pr_bundle_package_reads_no_wall_clock` — the *source* of every module in the package
   contains no wall-clock call, so no one can read one internally either.

Offline and deterministic (B8): every byte read here comes from `tests/fixtures/nse_pr_bundle/`.
"""

from __future__ import annotations

import inspect
import re
from datetime import date
from pathlib import Path
from typing import Final

import pytest

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse import pr_bundle
from dataplatform.ingest.nse.pr_bundle import bc as bc_module
from dataplatform.ingest.nse.pr_bundle import bundle as bundle_module
from dataplatform.ingest.nse.pr_bundle import ffix as ffix_module
from dataplatform.ingest.nse.pr_bundle import ix as ix_module
from dataplatform.ingest.nse.pr_bundle import mcap as mcap_module
from dataplatform.ingest.nse.pr_bundle.bc import BC_COLUMNS, parse_bc, parse_bc_bundle
from dataplatform.ingest.nse.pr_bundle.bundle import MemberKind, PrBundle

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle"

#: (era directory, archive filename, the session the bundle publishes).
ERA_BUNDLES: Final[tuple[tuple[str, str, date], ...]] = (
    ("ix_era", "PR040110.zip", date(2010, 1, 4)),
    ("classic", "PR020113.zip", date(2013, 1, 2)),
    ("mcap_upper", "PR010724.zip", date(2024, 7, 1)),
    ("lowercase", "PR040926.zip", date(2026, 9, 4)),
)


def _bundle(era: str, name: str) -> PrBundle:
    return PrBundle((FIXTURES / era / name).read_bytes(), filename=name)


# ── the guarantee ────────────────────────────────────────────────────────────────────────────


def test_knowable_date_is_the_bundles_own_date_not_today() -> None:
    """Every row is stamped with the file's session, and that is demonstrably not ingest time.

    The `!= date.today()` assertion is the one that catches the regression this test exists for:
    a parser that went back to `clock.now().date()` would still produce rows, still type-check,
    and still pass a test that only checked the field was populated.
    """
    with _bundle("classic", "PR020113.zip") as bundle:
        rows = parse_bc_bundle(bundle)

    assert rows, "the 2013-01-02 bundle publishes corporate actions"
    assert {row.knowable_date for row in rows} == {date(2013, 1, 2)}


@pytest.mark.parametrize(("era", "name", "session"), ERA_BUNDLES)
def test_knowable_date_is_the_publication_date_in_every_era(
    era: str, name: str, session: date
) -> None:
    """The stamp holds across all four format eras, including the 2026 lowercase rewrite."""
    with _bundle(era, name) as bundle:
        assert bundle.publication_date == session
        rows = parse_bc_bundle(bundle)
    assert rows
    assert {row.knowable_date for row in rows} == {session}


def test_the_bc_parsers_accept_no_clock() -> None:
    """No `Bc` entry point takes a `Clock`, so "now" cannot be injected as a knowable date.

    `parse_bc` takes `knowable_date` explicitly and `parse_bc_bundle` supplies it from the bundle.
    A parameter named `clock` appearing on either is the regression, whatever it is wired to.
    """
    for func in (parse_bc, parse_bc_bundle):
        params = set(inspect.signature(func).parameters)
        assert "clock" not in params, f"{func.__name__} must not accept a clock: {params}"
    assert "knowable_date" in inspect.signature(parse_bc).parameters


def test_the_pr_bundle_package_reads_no_wall_clock() -> None:
    """No module in the package calls a wall clock, so none can date a row from ingest time.

    Source-level rather than behavioural on purpose: a mocked clock in a test proves only that
    *this* path avoided it. Grepping the modules proves there is no path at all.
    """
    forbidden = re.compile(
        r"\b(datetime\.now|datetime\.utcnow|date\.today|time\.time|clock\.now|clock\.today)\b"
    )
    for module in (bundle_module, bc_module, ffix_module, ix_module, mcap_module):
        source = Path(inspect.getsourcefile(module) or "").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        # Docstrings legitimately *name* the defect; strip them before grepping for calls.
        code = re.sub(r'""".*?"""', "", code, flags=re.DOTALL)
        found = forbidden.findall(code)
        assert not found, f"{module.__name__} reads a wall clock: {found}"


def test_bc_rows_carry_no_isin_field() -> None:
    """The source is symbol-keyed, so the model has nowhere to put an invented ISIN.

    ISIN is the only join key (invariant #2). A nullable `isin` on this model would be filled in
    by the first caller with a symbol table to hand, and that mapping would be wrong for exactly
    the historical rows this source exists to supply.
    """
    assert "isin" not in pr_bundle.BcRow.model_fields
    assert "symbol" in pr_bundle.BcRow.model_fields


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def test_parses_the_2013_book_with_slash_dates() -> None:
    with _bundle("classic", "PR020113.zip") as bundle:
        rows = parse_bc_bundle(bundle)

    first = rows[0]
    assert first.symbol == "SHARONBIO"
    assert first.series == "BE"
    assert first.book_closure_start == date(2013, 1, 7)
    assert first.book_closure_end == date(2013, 1, 8)
    assert first.ex_date == date(2013, 1, 3)
    assert first.record_date is None, "a single space means 'no date' in this era"
    assert first.purpose == "DIVID RS 1.80/- PER SHARE"


def test_parses_the_2026_book_with_dashed_dates() -> None:
    """The 2025-10 cutover changed the date shape from `DD/MM/YYYY` to `YYYY-MM-DD`."""
    with _bundle("lowercase", "PR040926.zip") as bundle:
        rows = parse_bc_bundle(bundle)

    first = rows[0]
    assert first.symbol == "1003SCL32"
    assert first.record_date == date(2026, 9, 4)
    assert first.ex_date == date(2026, 9, 4)
    assert first.purpose == "INTEREST PAYMENT"
    assert first.knowable_date == date(2026, 9, 4)


def test_an_action_may_have_no_ex_date() -> None:
    """A GOI-loan redemption is published with an empty `EX_DT`; that is data, not a defect."""
    with _bundle("lowercase", "PR040926.zip") as bundle:
        rows = parse_bc_bundle(bundle)

    redemptions = [r for r in rows if r.ex_date is None]
    assert redemptions, "the 2026-09-04 book carries at least one row with no ex-date"
    assert all(r.announced_before_ex_date is None for r in redemptions)


def test_purpose_is_kept_byte_for_byte() -> None:
    """Nothing is normalized on the way in — classification is the reconciliation task's job.

    A parser that helpfully rewrote `DIVID RS 1.80/- PER SHARE` into a typed dividend would make
    the promotion of these rows against the existing 47,887 impossible to audit.
    """
    with _bundle("mcap_upper", "PR010724.zip") as bundle:
        raw = bundle.read(MemberKind.BC).decode("latin-1")
        rows = parse_bc_bundle(bundle)
    for row in rows[:20]:
        assert row.purpose in raw


# ── failure is loud and located ──────────────────────────────────────────────────────────────


def test_a_wrong_header_raises_naming_the_file() -> None:
    payload = b"SERIES,SYMBOL,SURPRISE\nEQ,FOO,bar\n"
    with pytest.raises(ParseError) as excinfo:
        parse_bc(payload, filename="Bc010101.csv", knowable_date=date(2001, 1, 1))
    assert "Bc010101.csv" in str(excinfo.value)
    assert "unexpected header" in str(excinfo.value)


def test_an_unreadable_date_raises_rather_than_becoming_none() -> None:
    """A misparsed date and a missing one are worlds apart in an adjustment chain."""
    header = ",".join(BC_COLUMNS)
    payload = f"{header}\nEQ,FOO,Foo Ltd,13-31-2020, , ,01/01/2020, , ,DIVIDEND\n".encode()
    with pytest.raises(ParseError) as excinfo:
        parse_bc(payload, filename="Bc010120.csv", knowable_date=date(2020, 1, 1))
    assert "RECORD_DT" in str(excinfo.value)
    assert ":2:" in str(excinfo.value), "the error names the physical line"


def test_html_wearing_a_200_never_becomes_rows() -> None:
    with pytest.raises(ParseError, match="markup"):
        parse_bc(
            b"<html><body>error</body></html>",
            filename="Bc.csv",
            knowable_date=date(2013, 1, 2),
        )


def test_a_short_row_raises() -> None:
    header = ",".join(BC_COLUMNS)
    payload = f"{header}\nEQ,FOO,Foo Ltd\n".encode()
    with pytest.raises(ParseError, match="expected 10 columns, got 3"):
        parse_bc(payload, filename="Bc010120.csv", knowable_date=date(2020, 1, 1))


# ── the four shapes the full corpus found and the 39-request sample did not (2026-10-06) ────

BC_FIXTURES: Final = FIXTURES / "bc"


def _bc_fixture(era: str, archive: str) -> PrBundle:
    return PrBundle((BC_FIXTURES / era / archive).read_bytes(), filename=archive)


def test_an_unquoted_comma_in_security_is_rejoined_and_the_dates_still_read() -> None:
    with _bc_fixture("comma_in_security", "PR300810.zip") as bundle:
        rows = parse_bc_bundle(bundle)
    assert len(rows) == 920
    bond = next(r for r in rows if r.symbol == "ICIBK1107" and r.series == "N1")
    assert bond.security_name == "Regular Income Bond, Opti"
    assert bond.purpose == "INTEREST PAYMENT"
    assert (bond.record_date, bond.ex_date) == (date(2010, 9, 27), date(2010, 9, 24))


def test_an_unquoted_comma_in_purpose_is_rejoined_with_the_comma_kept() -> None:
    with _bc_fixture("comma_in_purpose", "PR111019.zip") as bundle:
        rows = parse_bc_bundle(bundle)
    tcs = next(r for r in rows if r.symbol == "TCS" and r.series == "EQ")
    assert tcs.purpose == "INT DIV-RS 5, SPL DIV-RS"
    assert tcs.ex_date == date(2019, 10, 17)


def test_a_day_first_dashed_date_reads_as_the_same_day_first_date() -> None:
    with _bc_fixture("day_dash_date", "PR290416.zip") as bundle:
        rows = parse_bc_bundle(bundle)
    dishman = rows[0]
    assert (dishman.symbol, dishman.purpose) == ("DISHMAN", "BONUS 1:1")
    assert dishman.record_date == date(2016, 5, 3)  # '03-05-2016', day first like the slashes
    assert dishman.ex_date == date(2016, 5, 2)


def test_a_member_published_empty_is_no_actions_but_a_bare_empty_body_still_raises() -> None:
    with _bc_fixture("published_empty", "PR100122.zip") as bundle:
        assert parse_bc_bundle(bundle) == ()
    with pytest.raises(ParseError, match="empty response body"):
        parse_bc(b"", filename="Bc100122.csv", knowable_date=date(2022, 1, 10))


def test_an_extra_comma_that_fits_neither_free_text_cell_raises() -> None:
    header = ",".join(BC_COLUMNS)
    # The extra cell sits among the dates, so neither rejoin leaves six date-shaped cells.
    payload = f"{header}\nEQ,FOO,Foo Ltd, ,oops,01/01/2020, ,01/01/2020, , ,DIVIDEND\n".encode()
    with pytest.raises(ParseError, match="not unambiguously inside SECURITY or PURPOSE"):
        parse_bc(payload, filename="Bc010120.csv", knowable_date=date(2020, 1, 1))
