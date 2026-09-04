"""M11.1 — the macro fact model, the index-valuation parser, and the PIT macro store.

Reads only frozen captures under `tests/fixtures/nifty_index_close/` — never the network (B8). The
tests that matter most are the ones that fail if a point-in-time or identity guarantee is inverted:
a release published after the as-of date must be absent, a revision must not overwrite, and a
pre-2015 index name must not be silently lost.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.ingest.macro import (
    Frequency,
    MacroFact,
    MacroRelease,
    Unit,
    canonical_index,
    load_index_aliases,
    parse_index_valuation,
    series_id,
)
from dataplatform.ingest.models import ParseError
from dataplatform.store.macro_series import (
    read_l1,
    read_latest,
    read_pit,
    series_history,
    write_release,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "nifty_index_close"
CNX_2015 = FIXTURES / "cnx_era" / "ind_close_all_06112015.csv"
CNX_2012 = FIXTURES / "cnx_era" / "ind_close_all_01102012.csv"
NIFTY_2015 = FIXTURES / "nifty_era" / "ind_close_all_10112015.csv"
NIFTY_2026 = FIXTURES / "nifty_era" / "ind_close_all_01092026.csv"


def _iip(release_date: date, value: str, *, revision_seq: int = 0) -> MacroFact:
    """One IIP observation for April 2020 — the series used to exercise revisions.

    A typed helper rather than a `**dict` spread: mypy --strict cannot see the field types through
    a dict, and a test that silences the checker is a test that stops proving the model's shape.
    """
    return MacroFact(
        series_id="IN.MOSPI.IIP.GENERAL",
        period_end=date(2020, 4, 30),
        release_date=release_date,
        frequency=Frequency.MONTHLY,
        unit=Unit.INDEX,
        value=Decimal(value),
        revision_seq=revision_seq,
        source="mospi",
    )


def _fact(
    sid: str = "IN.NSE.NIFTY_50.PE",
    *,
    period_end: date = date(2026, 9, 1),
    release_date: date | None = None,
    value: str = "20.34",
    revision_seq: int = 0,
) -> MacroFact:
    return MacroFact(
        series_id=sid,
        period_end=period_end,
        release_date=release_date or period_end,
        frequency=Frequency.DAILY,
        unit=Unit.RATIO,
        value=Decimal(value),
        revision_seq=revision_seq,
        source="nifty_index_close_snapshot",
    )


# ── the model's two dates, and the guard between them ────────────────────────────────────────────


def test_series_id_is_canonical_and_rejects_a_smuggled_level() -> None:
    assert series_id("in", "nse", "Nifty 50", "pe") == "IN.NSE.NIFTY_50.PE"
    assert series_id("IN", "NSE", "Nifty Bank", "DIV_YIELD") == "IN.NSE.NIFTY_BANK.DIV_YIELD"
    # a dot inside a level would silently add a hierarchy level; it must not survive
    assert series_id("IN", "MOSPI", "CPI.COMBINED", "YOY") == "IN.MOSPI.CPI_COMBINED.YOY"
    with pytest.raises(ValueError, match="non-empty"):
        series_id("IN", "NSE", "", "PE")


def test_a_fact_cannot_be_knowable_before_the_period_it_measures_ends() -> None:
    """The inversion guard: April CPI released in March is a parser reading the wrong column."""
    with pytest.raises(ValueError, match="precedes period_end"):
        MacroFact(
            series_id="IN.MOSPI.CPI_COMBINED.YOY",
            period_end=date(2020, 4, 30),
            release_date=date(2020, 3, 12),
            frequency=Frequency.MONTHLY,
            unit=Unit.PCT,
            value=Decimal("5.91"),
            source="mospi",
        )


def test_a_release_refuses_a_fact_dated_to_another_day() -> None:
    """A fact in the wrong partition is a silent PIT leak, so the release refuses to carry it."""
    with pytest.raises(ValueError, match="PIT leak"):
        MacroRelease(
            release_date=date(2026, 9, 1),
            source="nifty_index_close_snapshot",
            facts=(_fact(release_date=date(2026, 9, 2), period_end=date(2026, 9, 2)),),
        )


# ── the index identity history — the survivorship guard ──────────────────────────────────────────


def test_alias_table_maps_every_flagship_era_to_one_canonical_name() -> None:
    table = load_index_aliases()
    for published in ("S&P CNX Nifty", "CNX Nifty", "Nifty 50"):
        assert canonical_index(published, table=table) == "Nifty 50"
    assert canonical_index("CNX Nifty Junior", table=table) == "Nifty Next 50"
    assert canonical_index("CNX Bank", table=table) == "Nifty Bank"


def test_an_unknown_published_name_becomes_its_own_series_never_a_guess() -> None:
    """An unmapped rename must split visibly, not merge silently into the wrong index."""
    table = load_index_aliases()
    assert canonical_index("CNX Some Retired Index", table=table) == "CNX Some Retired Index"


def test_every_alias_row_carries_its_evidence() -> None:
    for alias in load_index_aliases().aliases:
        assert alias.evidence.strip(), f"{alias.published} has no evidence"


def test_the_flagship_series_is_continuous_across_the_2015_rename() -> None:
    """The whole point of the table: one series_id spans both naming eras.

    Without it, a consumer keyed on 'Nifty 50' reads nothing at all from the CNX-era file — which is
    three years of market history silently absent rather than visibly missing.
    """
    old = parse_index_valuation(CNX_2015.read_bytes(), filename=CNX_2015.name)
    new = parse_index_valuation(NIFTY_2015.read_bytes(), filename=NIFTY_2015.name)
    sid = "IN.NSE.NIFTY_50.CLOSE"
    old_close = next(f for f in old.facts if f.series_id == sid)
    new_close = next(f for f in new.facts if f.series_id == sid)
    assert old_close.period_end == date(2015, 11, 6)
    assert new_close.period_end == date(2015, 11, 10)
    # two sessions apart across one rename — a level jump would mean the alias is wrong
    assert abs(new_close.value - old_close.value) / old_close.value < Decimal("0.05")


# ── the parser, against real bytes from both eras ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "session", "indices"),
    [
        (CNX_2012, date(2012, 10, 1), 30),
        (CNX_2015, date(2015, 11, 6), 53),
        (NIFTY_2015, date(2015, 11, 10), 53),
        (NIFTY_2026, date(2026, 9, 1), 165),
    ],
)
def test_every_era_parses_with_the_session_as_both_dates(
    path: Path, session: date, indices: int
) -> None:
    release = parse_index_valuation(path.read_bytes(), filename=path.name)
    assert release.release_date == session
    assert {f.period_end for f in release.facts} == {session}
    assert {f.release_date for f in release.facts} == {session}
    assert len({f.series_id.rsplit(".", 1)[0] for f in release.facts}) == indices


def test_valuation_columns_are_read_with_their_units() -> None:
    release = parse_index_valuation(NIFTY_2026.read_bytes(), filename=NIFTY_2026.name)
    by_id = {f.series_id: f for f in release.facts}
    assert by_id["IN.NSE.NIFTY_50.CLOSE"].value == Decimal("24055.8")
    assert by_id["IN.NSE.NIFTY_50.PE"].value == Decimal("20.34")
    assert by_id["IN.NSE.NIFTY_50.PB"].value == Decimal("2.92")
    assert by_id["IN.NSE.NIFTY_50.DIV_YIELD"].value == Decimal("1.17")
    # a P/E is a multiple, not a percentage — a consumer dividing by 100 would be wrong for a decade
    assert by_id["IN.NSE.NIFTY_50.PE"].unit is Unit.RATIO
    assert by_id["IN.NSE.NIFTY_50.DIV_YIELD"].unit is Unit.PCT


def test_the_old_era_leading_dot_decimal_reads_exactly() -> None:
    """The 2012 file writes `.27` for a change column; Decimal must read it without a float hop."""
    release = parse_index_valuation(CNX_2012.read_bytes(), filename=CNX_2012.name)
    close = next(f for f in release.facts if f.series_id == "IN.NSE.NIFTY_50.CLOSE")
    assert close.value == Decimal("5718.8")


def test_markup_wearing_a_200_is_refused() -> None:
    with pytest.raises(ParseError, match="markup"):
        parse_index_valuation(b"<!DOCTYPE html><html>404</html>", filename="soft404.csv")


def test_an_empty_body_is_refused() -> None:
    with pytest.raises(ParseError, match="empty"):
        parse_index_valuation(b"   ", filename="empty.csv")


def test_a_file_mixing_two_sessions_is_refused() -> None:
    body = (
        b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
        b"Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
        b"Nifty 50,01-09-2026,1,1,1,100,0,0,0,0,20,3,1\n"
        b"Nifty Bank,02-09-2026,1,1,1,200,0,0,0,0,15,2,1\n"
    )
    with pytest.raises(ParseError, match="one session"):
        parse_index_valuation(body, filename="mixed.csv")


def test_an_unstated_value_is_absent_not_zero() -> None:
    """A blank P/E is 'this index states none', never 'this index earns nothing'."""
    body = (
        b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
        b"Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
        b"Nifty 50,01-09-2026,1,1,1,100,0,0,0,0,-,,1.17\n"
    )
    release = parse_index_valuation(body, filename="sparse.csv")
    ids = {f.series_id for f in release.facts}
    assert "IN.NSE.NIFTY_50.PE" not in ids
    assert "IN.NSE.NIFTY_50.PB" not in ids
    assert "IN.NSE.NIFTY_50.DIV_YIELD" in ids


# ── the store: partitioning, revisions, and the PIT read ─────────────────────────────────────────


def test_write_then_read_round_trips_a_real_release(tmp_path: Path) -> None:
    release = parse_index_valuation(NIFTY_2026.read_bytes(), filename=NIFTY_2026.name)
    path = write_release(release, data_root=tmp_path)
    assert path.parent.name == "date=2026-09-01"
    assert len(read_l1(date(2026, 9, 1), data_root=tmp_path)) == len(release.facts)


def test_a_rewrite_of_the_same_release_is_idempotent(tmp_path: Path) -> None:
    release = parse_index_valuation(NIFTY_2026.read_bytes(), filename=NIFTY_2026.name)
    first = write_release(release, data_root=tmp_path).read_bytes()
    second = write_release(release, data_root=tmp_path).read_bytes()
    assert first == second, "a re-derivation must be byte-identical (M1.5 determinism)"


def test_a_changed_value_under_one_key_fails_loudly(tmp_path: Path) -> None:
    """L0 is immutable: a re-parse producing a different number is a regression, not a revision."""
    source = "nifty_index_close_snapshot"
    write_release(
        MacroRelease(release_date=date(2026, 9, 1), source=source, facts=(_fact(value="20.34"),)),
        data_root=tmp_path,
    )
    with pytest.raises(ValueError, match="stable parse must be stable"):
        write_release(
            MacroRelease(
                release_date=date(2026, 9, 1), source=source, facts=(_fact(value="99.99"),)
            ),
            data_root=tmp_path,
        )


def test_read_pit_cannot_see_a_release_published_after_the_as_of_date(tmp_path: Path) -> None:
    """The inversion test: a later release must be physically absent, not merely filtered."""
    april = MacroFact(
        series_id="IN.MOSPI.CPI_COMBINED.YOY",
        period_end=date(2020, 4, 30),
        release_date=date(2020, 5, 12),
        frequency=Frequency.MONTHLY,
        unit=Unit.PCT,
        value=Decimal("5.91"),
        source="mospi",
    )
    write_release(
        MacroRelease(release_date=date(2020, 5, 12), source="mospi", facts=(april,)),
        data_root=tmp_path,
    )
    assert read_pit(date(2020, 5, 11), data_root=tmp_path) == ()
    assert len(read_pit(date(2020, 5, 12), data_root=tmp_path)) == 1


def test_a_revision_is_a_new_record_and_read_latest_prefers_it(tmp_path: Path) -> None:
    """India's IIP and GDP are revised; both versions must survive and PIT must respect the date."""
    first = _iip(date(2020, 6, 12), "53.6")
    revised = _iip(date(2020, 7, 10), "54.1")
    for fact in (first, revised):
        write_release(
            MacroRelease(release_date=fact.release_date, source="mospi", facts=(fact,)),
            data_root=tmp_path,
        )

    # both versions coexist forever
    assert len(read_pit(date(2020, 7, 10), data_root=tmp_path)) == 2
    # as of the day before the revision, the first print is still the best knowledge
    assert read_latest(date(2020, 7, 9), data_root=tmp_path)[0].value == Decimal("53.6")
    # after it, the revision wins — without the original having been destroyed
    assert read_latest(date(2020, 7, 10), data_root=tmp_path)[0].value == Decimal("54.1")


def test_a_same_day_republication_is_kept_apart_by_revision_seq(tmp_path: Path) -> None:
    write_release(
        MacroRelease(
            release_date=date(2020, 6, 12),
            source="mospi",
            facts=(
                _iip(date(2020, 6, 12), "53.6", revision_seq=0),
                _iip(date(2020, 6, 12), "54.1", revision_seq=1),
            ),
        ),
        data_root=tmp_path,
    )
    assert len(read_l1(date(2020, 6, 12), data_root=tmp_path)) == 2
    assert read_latest(date(2020, 6, 12), data_root=tmp_path)[0].value == Decimal("54.1")


def test_series_history_walks_one_series_in_period_order(tmp_path: Path) -> None:
    for path in (CNX_2012, CNX_2015, NIFTY_2015, NIFTY_2026):
        write_release(
            parse_index_valuation(path.read_bytes(), filename=path.name), data_root=tmp_path
        )
    history = series_history("IN.NSE.NIFTY_50.CLOSE", date(2026, 9, 1), data_root=tmp_path)
    assert [f.period_end for f in history] == [
        date(2012, 10, 1),
        date(2015, 11, 6),
        date(2015, 11, 10),
        date(2026, 9, 1),
    ]
    # and the 2012/2015 points are only there because the alias table spans the rename
    assert history[0].value == Decimal("5718.8")


def test_read_pit_on_an_empty_store_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_pit(date(2026, 9, 1), data_root=tmp_path) == ()
    with pytest.raises(FileNotFoundError):
        read_l1(date(2026, 9, 1), data_root=tmp_path)
